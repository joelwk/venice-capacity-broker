from __future__ import annotations

import time
from typing import Any, Dict, List, Optional


class MarketDataProvider:
    """Market data via on-chain DEX aggregator + Venice DIEM signals.

    - Quotes/prices: uses configured DEX providers (UniswapV2, Aerodrome)
      via `libs.dex.providers` with decimals-aware conversions.
    - DIEM signals: fetched from Venice API (`/v1/diem`).
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

    _diem_cache: Optional[Dict[str, Any]] = None
    _diem_cache_t: float = 0.0
    _vvv_cache: Optional[Dict[str, Any]] = None
    _vvv_cache_t: float = 0.0

    def diem_signals(self, ttl_s: int = 30, retries: int = 2, backoff_s: float = 0.5) -> Dict[str, Any]:
        """Fetch DIEM signals from Venice API with simple cache and retry."""
        now = time.time()
        if self._diem_cache and (now - self._diem_cache_t) < ttl_s:
            return self._diem_cache
        from libs.venice_sdk.client import VeniceClient

        client = VeniceClient()
        last_err: Optional[Exception] = None
        for i in range(retries + 1):
            try:
                res = client.get_diem_signals()
                self._diem_cache, self._diem_cache_t = res, time.time()
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                if i < retries:
                    time.sleep(backoff_s * (2**i))
        # If all retries fail, rethrow the last error
        raise RuntimeError(f"Failed to fetch DIEM signals: {last_err}")

    def vvv_signals(self, ttl_s: int = 30, retries: int = 2, backoff_s: float = 0.5) -> Dict[str, Any]:
        """Fetch VVV signals from Venice API with simple cache and retry."""
        now = time.time()
        if self._vvv_cache and (now - self._vvv_cache_t) < ttl_s:
            return self._vvv_cache
        from libs.venice_sdk.client import VeniceClient

        client = VeniceClient()
        last_err: Optional[Exception] = None
        for i in range(retries + 1):
            try:
                res = client.get_vvv_signals()
                self._vvv_cache, self._vvv_cache_t = res, time.time()
                return res
            except Exception as e:  # noqa: BLE001
                last_err = e
                if i < retries:
                    time.sleep(backoff_s * (2**i))
        raise RuntimeError(f"Failed to fetch VVV signals: {last_err}")

    def unified_signals(self, ttl_s: int = 30) -> Dict[str, Any]:
        """Return a merged signals struct with both VVV and DIEM.

        Uses individual caches; errors for either side are surfaced.
        """
        data = {"vvv": self.vvv_signals(ttl_s=ttl_s), "diem": self.diem_signals(ttl_s=ttl_s)}
        # Emit centralized signal event (best-effort)
        try:
            from libs.telemetry.events import emit as _emit

            _emit("signal.market.signals", data)
        except Exception:
            pass
        return data
