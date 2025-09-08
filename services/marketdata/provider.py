from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class MarketDataProvider:
    """Market data via DEX aggregator + Venice endpoints.

    - Quotes/prices: uses configured DEX providers (UniswapV2, Aerodrome)
      via `libs.dex.providers` with decimals-aware conversions.
    - VVV metrics: fetched via explicit endpoints (circulating supply, utilization, staking_yield).
    - DIEM balance/quotas: fetched via rate-limits endpoint (`/api_keys/rate_limits`).
    """

    def _erc20_decimals(self, address: str) -> int:
        from web3 import Web3  # lazy load
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
        return int(erc20.functions.decimals().call())

    def _path_from_env(self) -> List[str]:
        import os

        path_env = os.getenv("TRADE_PATH")
        if not path_env:
            raise EnvironmentError("TRADE_PATH must be set: comma-separated token addresses (in,out)")
        return [p.strip() for p in path_env.split(",")]

    def _quote_token_address(self) -> str:
        import os

        qt = os.getenv("QUOTE_TOKEN_ADDRESS")
        if not qt:
            raise EnvironmentError("QUOTE_TOKEN_ADDRESS must be set for convenience symbol pricing (e.g., USDC address)")
        return qt.strip()

    def _bridge_token_address(self) -> Optional[str]:
        """Return a fallback bridge token address for multi-hop quotes.

        Priority:
        - BRIDGE_TOKEN_ADDRESS env if provided
        - BASE/WETH by known chain id (Base mainnet default)
        """
        import os

        env_bt = (os.getenv("BRIDGE_TOKEN_ADDRESS") or os.getenv("WETH_ADDRESS") or "").strip()
        if env_bt:
            return env_bt
        # Default mapping for common networks (extend as needed)
        try:
            chain_id = int(os.getenv("BASE_CHAIN_ID") or os.getenv("CHAIN_ID") or 8453)
        except Exception:
            chain_id = 8453
        if chain_id == 8453:
            # Base mainnet WETH
            return "0x4200000000000000000000000000000000000006"
        return None

    def _weth_address(self) -> str:
        bt = self._bridge_token_address()
        if not bt:
            # Fallback to canonical Base WETH
            return "0x4200000000000000000000000000000000000006"
        return bt

    def _address_for_symbol(self, symbol: str) -> Optional[str]:
        import os

        s = symbol.upper()
        if s == "DIEM":
            return (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip() or None
        if s == "VVV":
            return (os.getenv("VVV_TOKEN_ADDRESS") or "").strip() or None
        return None

    def quote_all(self, amount_in: int, path: List[str]) -> List[Any]:
        """Return quotes across all configured providers for path.

        amount_in is in smallest units of the input token.
        """
        from libs.dex.providers import build_aggregator_from_env

        agg = build_aggregator_from_env()
        return agg.quote_all(amount_in, path)

    def best_price(self, path: List[str], amount_in_decimal: float = 1.0) -> Dict[str, Any]:
        """Compute best price for path given a decimal input amount.

        Returns dict with provider, amount_in/out (units), decimals, and price.
        """
        if len(path) < 2:
            raise ValueError("path must include at least [token_in, token_out]")
        dec_in = self._erc20_decimals(path[0])
        dec_out = self._erc20_decimals(path[-1])
        amount_in_units = int(amount_in_decimal * (10 ** dec_in))
        from libs.dex.providers import build_aggregator_from_env

        agg = build_aggregator_from_env()
        q = agg.best_quote(amount_in_units, path)
        # Fallback: if no direct quotes and path is a simple pair, try bridge token (e.g., WETH)
        if q is None and len(path) == 2:
            bt = self._bridge_token_address()
            if bt and bt.lower() not in {path[0].lower(), path[-1].lower()}:
                alt_path = [path[0], bt, path[-1]]
                try:
                    q = agg.best_quote(amount_in_units, alt_path)
                    if q is not None:
                        path = alt_path  # report the path actually used
                except Exception:
                    q = None
        if q is None:
            raise RuntimeError("No quotes available for provided path")
        price = (q.amount_out / (10 ** dec_out)) / (q.amount_in / (10 ** dec_in))
        return {
            "provider": q.provider,
            "amount_in": q.amount_in,
            "amount_out": q.amount_out,
            "decimals": {"in": dec_in, "out": dec_out},
            "price": price,
            "path": path,
        }

    def _mid_price_from_reserves(self, token_in: str, token_out: str) -> Optional[float]:
        """Compute infinitesimal mid price token_in->token_out from cached or fetched reserves.

        Returns price in token_out per token_in, decimals-aware.
        """
        try:
            from services.marketdata.etherscan_verify import (
                get_cached_pair_info_for_tokens,
                verify_trade_path,
            )
        except Exception:
            return None
        # Try cache first
        info = None
        try:
            info = get_cached_pair_info_for_tokens(token_in, token_out)
        except Exception:
            info = None
        if not info:
            try:
                rep = verify_trade_path([token_in, token_out])
                hops = rep.get("hops") or []
                if hops:
                    uv2 = (hops[0] or {}).get("uniswap_v2") or {}
                    if uv2.get("pair"):
                        info = {
                            "pair": uv2.get("pair"),
                            "reserves": uv2.get("reserves"),
                            "token0": uv2.get("token0"),
                            "token1": uv2.get("token1"),
                        }
            except Exception:
                info = None
        if not info:
            return None
        reserves = info.get("reserves")
        if not isinstance(reserves, tuple) or len(reserves) < 2:
            return None
        t0 = str(info.get("token0") or "")
        t1 = str(info.get("token1") or "")
        if not t0 or not t1:
            # If token mapping is unknown, we cannot compute directionally
            return None
        # Normalize addresses
        def _n(x: str) -> str:
            return ("0x" + str(x).lower().removeprefix("0x"))

        t0n, t1n = _n(t0), _n(t1)
        ain, aout = _n(token_in), _n(token_out)
        try:
            d0 = self._erc20_decimals(t0n)
            d1 = self._erc20_decimals(t1n)
        except Exception:
            return None
        r0 = float(reserves[0]) / float(10 ** d0)
        r1 = float(reserves[1]) / float(10 ** d1)
        if ain == t0n and aout == t1n:
            return r1 / r0 if r0 > 0 else None
        if ain == t1n and aout == t0n:
            return r0 / r1 if r1 > 0 else None
        return None

    def diem_price_with_fallback(self) -> Optional[float]:
        """Return DIEM price in QUOTE token using aggregator, then mid-price fallbacks.

        Strategy:
        1) Try aggregator best price for TRADE_PATH
        2) If unavailable and path is DIEM->WETH->QUOTE, compute
           price = mid(DIEM->WETH) * bestPrice(WETH->QUOTE) or mid(WETH->QUOTE)
        """
        try:
            path = self._path_from_env()
        except Exception:
            path = []
        if len(path) >= 2:
            try:
                bp = self.best_price(path, amount_in_decimal=1.0)
                return float(bp.get("price") or 0.0)
            except Exception:
                pass
        # Fallback only meaningful for 3-hop DIEM->WETH->QUOTE
        try:
            if len(path) == 3:
                diem = path[0]
                weth = path[1]
                quote = path[2]
            else:
                diem = (self._address_for_symbol("DIEM") or "").strip()
                weth = self._weth_address()
                quote = self._quote_token_address()
            if not diem or not weth or not quote:
                return None
            px_dw = self._mid_price_from_reserves(diem, weth) or 0.0
            if px_dw <= 0:
                return None
            # Try aggregator for WETH->QUOTE
            px_wq = 0.0
            try:
                b2 = self.best_price([weth, quote], amount_in_decimal=1.0)
                px_wq = float(b2.get("price") or 0.0)
            except Exception:
                px_wq = self._mid_price_from_reserves(weth, quote) or 0.0
            if px_wq <= 0:
                return None
            return float(px_dw * px_wq)
        except Exception:
            return None

    def prices(self, symbols: List[str]) -> Dict[str, float]:
        """Return prices for requested symbols.

        MVP+: DIEM resolved via TRADE_PATH, VVV via QUOTE_TOKEN_ADDRESS if set; others = 1.0
        """
        out: Dict[str, float] = {}
        for sym in symbols:
            SU = sym.upper()
            if SU == "DIEM":
                try:
                    path = self._path_from_env()
                    bp = self.best_price(path, amount_in_decimal=1.0)
                    out[sym] = float(bp["price"])  # DIEM per USDC (or vice-versa based on path)
                except Exception:
                    out[sym] = 1.0
            elif SU == "VVV":
                try:
                    token = self._address_for_symbol("VVV")
                    quote = self._quote_token_address()
                    if not token or not quote:
                        raise ValueError("VVV or QUOTE token address missing")
                    bp = self.best_price([token, quote], amount_in_decimal=1.0)
                    out[sym] = float(bp["price"])  # VVV per USDC
                except Exception:
                    out[sym] = 1.0
            else:
                out[sym] = 1.0
        # Emit centralized signal for prices (best-effort)
        try:
            from libs.telemetry.events import emit as _emit

            _emit("signal.market.prices", {"symbols": [str(s) for s in symbols], "prices": dict(out)})
        except Exception:
            pass
        return out

    _vvv_metrics_cache: Optional[Dict[str, Any]] = None
    _vvv_metrics_cache_t: float = 0.0
    _diem_balance_cache: Optional[Dict[str, Any]] = None
    _diem_balance_cache_t: float = 0.0

    def vvv_metrics(self, ttl_s: int = 30, retries: int = 2, backoff_s: float = 0.5) -> Dict[str, Any]:
        """Fetch VVV metrics (circulating supply, utilization, staking_yield) with cache and retry."""
        now = time.time()
        if self._vvv_metrics_cache and (now - self._vvv_metrics_cache_t) < ttl_s:
            return self._vvv_metrics_cache
        from libs.venice_sdk.client import VeniceClient

        client = VeniceClient()
        last_err: Optional[Exception] = None
        for i in range(retries + 1):
            try:
                res = client.get_vvv_metrics()
                self._vvv_metrics_cache, self._vvv_metrics_cache_t = res, time.time()
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                if i < retries:
                    time.sleep(backoff_s * (2**i))
        # Offline stub support
        try:
            import os as _os
            if (_os.getenv("VENICE_OFFLINE_SIGNALS") or "false").strip().lower() in {"1", "true", "yes", "on"}:
                stub = {
                    "offline": True,
                    "source": "stub",
                    "kind": "vvv_metrics",
                    "ts": int(time.time()),
                    "circulating_supply": None,
                    "utilization": None,
                    "staking_yield": None,
                }
                self._vvv_metrics_cache, self._vvv_metrics_cache_t = stub, time.time()
                return stub
        except Exception:
            pass
        raise RuntimeError(f"Failed to fetch VVV metrics: {last_err}")

    def diem_balance(self, ttl_s: int = 30, retries: int = 2, backoff_s: float = 0.5) -> Dict[str, Any]:
        """Fetch DIEM balance/quotas from rate-limits endpoint with cache and retry.

        Returns a dict with at least balances and remaining/limits if present.
        """
        now = time.time()
        if self._diem_balance_cache and (now - self._diem_balance_cache_t) < ttl_s:
            return self._diem_balance_cache
        from libs.venice_sdk.client import VeniceClient

        client = VeniceClient()
        last_err: Optional[Exception] = None
        for i in range(retries + 1):
            try:
                limits = client.get_rate_limits()
                # Normalize a compact shape for consumers; handle top-level or {data:{balances}}
                obj = limits or {}
                data = obj.get("data") if isinstance(obj, dict) else None
                if isinstance(data, dict):
                    balances = data.get("balances") or {}
                else:
                    balances = obj.get("balances") or {}
                diem_bal = balances.get("DIEM") or balances.get("diem")
                summary = {"balances": balances, "diem": diem_bal, "raw": limits}
                self._diem_balance_cache, self._diem_balance_cache_t = summary, time.time()
                return summary
            except Exception as e:  # noqa: BLE001
                last_err = e
                if i < retries:
                    time.sleep(backoff_s * (2**i))
        # Offline stub support
        try:
            import os as _os
            if (_os.getenv("VENICE_OFFLINE_SIGNALS") or "false").strip().lower() in {"1", "true", "yes", "on"}:
                stub = {"offline": True, "source": "stub", "kind": "diem_balance", "ts": int(time.time())}
                self._diem_balance_cache, self._diem_balance_cache_t = stub, time.time()
                return stub
        except Exception:
            pass
        raise RuntimeError(f"Failed to fetch DIEM balance: {last_err}")

    def unified_signals(self, ttl_s: int = 30) -> Dict[str, Any]:
        """Return a merged struct with VVV metrics and DIEM balance."""
        data = {"vvv": self.vvv_metrics(ttl_s=ttl_s), "diem": self.diem_balance(ttl_s=ttl_s)}
        # Emit centralized signal event (best-effort)
        try:
            from libs.telemetry.events import emit as _emit

            _emit("signal.market.signals", data)
        except Exception:
            pass
        return data

    # --- Etherscan v2 discovery helpers ---
    def discover_trade_path(self, path: List[str]) -> Dict[str, Any]:
        """Return discovery report for the path using Etherscan v2 helpers.

        Wraps services.marketdata.etherscan_verify.verify_trade_path.
        """
        from services.marketdata.etherscan_verify import verify_trade_path  # lazy import

        return verify_trade_path(path)

    def reserve_cap_units(self, path: List[str], take_bps: Optional[int] = None) -> Optional[int]:
        """Estimate a conservative max input units based on pool reserves.

        - Only applies for direct 2-token path (path[0] -> path[1]) on UniswapV2-like pools.
        - Caps input to a fraction of the input-side reserve: take_bps/10_000.
        - Env override RISK_MAX_POOL_TAKE_BPS if take_bps not provided (default 100 = 1%).
        Returns None when discovery or reserves unavailable.
        """
        if len(path) < 2:
            return None
        try:
            from services.marketdata.etherscan_verify import (
                verify_trade_path,
                get_reserves,
                get_token0,
                get_token1,
                get_cached_pair_info_for_tokens,
            )
        except Exception:
            return None
        tbps = take_bps
        if tbps is None:
            try:
                tbps = int((__import__("os").getenv("RISK_MAX_POOL_TAKE_BPS") or "100").strip() or 100)
            except Exception:
                tbps = 100
        tbps = int(tbps)
        if tbps <= 0:
            return None
        # Try cache first for (token_in -> token_out)
        pair = None
        rez = None
        t0 = None
        t1 = None
        try:
            cached = get_cached_pair_info_for_tokens(path[0], path[1])
            if isinstance(cached, dict):
                pair = cached.get("pair")
                rez = cached.get("reserves")
                t0 = cached.get("token0")
                t1 = cached.get("token1")
        except Exception:
            pass
        if not pair:
            disc = verify_trade_path(path)
            if not disc or not isinstance(disc, dict):
                return None
            hops = disc.get("hops") or []
            if not hops:
                return None
            hop0 = hops[0] or {}
            uv2 = hop0.get("uniswap_v2") or {}
            pair = uv2.get("pair")
            if not pair:
                return None
        # Fetch reserves and token0/1 to map to the input token
        try:
            rez = rez or uv2.get("reserves") or get_reserves(pair)
            if not isinstance(rez, tuple) or len(rez) < 2:
                return None
            t0 = t0 or uv2.get("token0") or get_token0(pair)
            t1 = t1 or uv2.get("token1") or get_token1(pair)
            if not t0 or not t1:
                return None
            # Normalize addresses without requiring web3 dependency
            def _norm(a: str) -> str:
                a = str(a).strip()
                return ("0x" + a.lower().removeprefix("0x")) if a else ""

            t0_n = _norm(str(t0))
            t1_n = _norm(str(t1))
            inp_n = _norm(path[0])
            if inp_n == t0_n:
                reserve_in = int(rez[0])
            elif inp_n == t1_n:
                reserve_in = int(rez[1])
            else:
                # If input is neither token0 nor token1, cannot map reliably
                return None
            cap = (reserve_in * tbps) // 10_000
            return int(cap)
        except Exception:
            return None
