from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from libs.agentkit_ext.web3_utils import get_contract, get_web3
from libs.agentkit_ext.agentkit_wallet import get_address, send_tx
try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # noqa: BLE001
    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return


Address = str


@dataclass
class Quote:
    provider: str
    amount_in: int
    amount_out: int
    path: List[Address]


class DexProvider:
    name: str

    def quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        raise NotImplementedError

    def trade(self, amount_in: int, min_amount_out: int, path: List[Address]) -> Dict[str, str]:
        raise NotImplementedError

    # --- Optional exact-out (buy-side) APIs ---
    def quote_exact_out(self, amount_out: int, path: List[Address]) -> Optional[Quote]:
        """Return a Quote with required amount_in to achieve amount_out along path.

        Providers may override; default is unsupported (None).
        """
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path: List[Address]) -> Dict[str, str]:
        """Execute exact-out swap, enforcing a maximum input (slippage guard)."""
        raise NotImplementedError

class UniswapV2DexProvider(DexProvider):
    name = "uniswap_v2"

    def __init__(self, router_address: Address) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.router = get_contract(self.w3, self.router_addr, "uniswap_v2_router.json")
        # Lazily resolve recipient during trade to avoid wallet requirement for quotes
        self.recipient: Optional[str] = None
        
    # --- telemetry helpers ---
    @staticmethod
    def _latency_bucket_name(seconds: float) -> str:
        # Buckets: <50ms, <100ms, <200ms, <500ms, <1s, <2s, >=2s
        s = float(seconds)
        if s < 0.05:
            return "lt_50ms"
        if s < 0.1:
            return "lt_100ms"
        if s < 0.2:
            return "lt_200ms"
        if s < 0.5:
            return "lt_500ms"
        if s < 1.0:
            return "lt_1s"
        if s < 2.0:
            return "lt_2s"
        return "ge_2s"

    def quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        t0 = time.perf_counter()
        try:
            amounts = self.router.functions.getAmountsOut(amount_in, path).call()
            q = Quote(provider=self.name, amount_in=amount_in, amount_out=int(amounts[-1]), path=path)
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            except Exception:
                pass
            try:
                _metrics_inc(
                    "dex_quote_latency_bucket_total",
                    labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
                )
            except Exception:
                pass
            return q
        except Exception:
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
            except Exception:
                pass
            return None

    def quote_exact_out(self, amount_out: int, path: List[Address]) -> Optional[Quote]:
        try:
            amounts = self.router.functions.getAmountsIn(amount_out, path).call()
            q = Quote(provider=self.name, amount_in=int(amounts[0]), amount_out=amount_out, path=path)
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            except Exception:
                pass
            return q
        except Exception:
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
            except Exception:
                pass
            return None

    def _ensure_allowance(self, token: Address, owner: Address, spender: Address, required: int) -> Optional[str]:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = erc20.encode_abi(fn_name="approve", args=[spender, required])
        return send_tx(token, bytes.fromhex(approve_data[2:]))

    def trade(self, amount_in: int, min_amount_out: int, path: List[Address]) -> Dict[str, str]:
        # Ensure allowance for input token to router
        token_in = path[0]
        # Resolve recipient lazily
        from web3 import Web3 as _Web3  # type: ignore
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, amount_in) or ""

        deadline = int(time.time()) + 20 * 60
        # Try standard swap first; if it fails, fallback to FOT-supporting swap
        t0 = time.perf_counter()
        try:
            fn = self.router.functions.swapExactTokensForTokens(
                amount_in, min_amount_out, path, recipient, deadline
            )
            built = fn.build_transaction({})  # only need data for AgentKit provider
            tx_hash = send_tx(self.router_addr, built["data"])
            try:
                _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "standard"})
            except Exception:
                pass
            try:
                _metrics_inc(
                    "dex_trade_latency_bucket_total",
                    labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
                )
            except Exception:
                pass
            return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}
        except Exception:
            # Fallback: fee-on-transfer supporting function
            try:
                fn2 = self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    amount_in, min_amount_out, path, recipient, deadline
                )
                built2 = fn2.build_transaction({})
                tx_hash2 = send_tx(self.router_addr, built2["data"])
                try:
                    _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "fot"})
                    _metrics_inc("fot_fallback_total", labels={"provider": self.name})
                except Exception:
                    pass
                try:
                    _metrics_inc(
                        "dex_trade_latency_bucket_total",
                        labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
                    )
                except Exception:
                    pass
                return {
                    "provider": self.name,
                    "tx_hash": tx_hash2,
                    "approval_tx": approve_hash,
                    "fot_fallback": "true",
                }
            except Exception as e:  # noqa: BLE001
                # Re-raise original behavior if fallback also fails
                try:
                    _metrics_inc("dex_trade_errors_total", labels={"provider": self.name, "path": "fot"})
                except Exception:
                    pass
                raise e

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path: List[Address]) -> Dict[str, str]:
        """Execute an exact-out trade using UniswapV2 swapTokensForExactTokens.

        Approves up to max_amount_in for safety and enforces slippage by contract arg.
        """
        token_in = path[0]
        from web3 import Web3 as _Web3  # type: ignore
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in) or ""

        deadline = int(time.time()) + 20 * 60
        t0 = time.perf_counter()
        try:
            fn = self.router.functions.swapTokensForExactTokens(
                int(amount_out), int(max_amount_in), path, recipient, deadline
            )
            built = fn.build_transaction({})
            tx_hash = send_tx(self.router_addr, built["data"])
            try:
                _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "exact_out"})
            except Exception:
                pass
            try:
                _metrics_inc(
                    "dex_trade_latency_bucket_total",
                    labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
                )
            except Exception:
                pass
            return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}
        except Exception as e:  # noqa: BLE001
            try:
                _metrics_inc("dex_trade_errors_total", labels={"provider": self.name, "path": "exact_out"})
            except Exception:
                pass
            raise e


class AerodromeDexProvider(DexProvider):
    name = "aerodrome"

    def __init__(self, router_address: Address, stable: bool = True) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.router = get_contract(self.w3, self.router_addr, "aerodrome_router.json")
        self.recipient: Optional[str] = None
        self.stable = stable

    def _routes(self, path: List[Address], stable: Optional[bool] = None) -> List[Tuple[Address, Address, bool]]:
        # Build multi-hop routes for Aerodrome: [(tokenIn, tokenOut, stable), ...]
        from web3 import Web3 as _Web3  # type: ignore
        if len(path) < 2:
            raise ValueError("path must include at least [token_in, token_out]")
        st = bool(self.stable) if stable is None else bool(stable)
        hops: List[Tuple[Address, Address, bool]] = []
        for i in range(len(path) - 1):
            hops.append(
                (
                    _Web3.to_checksum_address(path[i]),
                    _Web3.to_checksum_address(path[i + 1]),
                    st,
                )
            )
        return hops

    def quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        # Try with configured stable flag first; if it fails, try toggled stable flag.
        # This helps when env stable setting doesn't match the actual pool type.
        t0 = time.perf_counter()
        try:
            routes = self._routes(path, stable=self.stable)
            amounts = self.router.functions.getAmountsOut(amount_in, routes).call()
            q = Quote(provider=self.name, amount_in=amount_in, amount_out=int(amounts[-1]), path=path)
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            except Exception:
                pass
            try:
                _bucket_latency("quote", self.name, time.perf_counter() - t0)
            except Exception:
                pass
            return q
        except Exception:
            pass
        try:
            routes = self._routes(path, stable=not bool(self.stable))
            amounts = self.router.functions.getAmountsOut(amount_in, routes).call()
            q = Quote(provider=self.name, amount_in=amount_in, amount_out=int(amounts[-1]), path=path)
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            except Exception:
                pass
            try:
                _bucket_latency("quote", self.name, time.perf_counter() - t0)
            except Exception:
                pass
            return q
        except Exception:
            try:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
            except Exception:
                pass
            return None

    def trade(self, amount_in: int, min_amount_out: int, path: List[Address]) -> Dict[str, str]:
        token_in = path[0]
        # Ensure allowance for input token to router
        from web3 import Web3 as _Web3  # type: ignore
        erc20_owner = self.recipient or _Web3.to_checksum_address(get_address())
        erc20_spender = self.router_addr
        try:
            erc20 = get_contract(self.w3, token_in, "erc20.json")
            current = int(erc20.functions.allowance(erc20_owner, erc20_spender).call())
        except Exception:
            current = 0
        approve_hash = ""
        if current < amount_in:
            approve_data = erc20.encode_abi(fn_name="approve", args=[self.router_addr, amount_in])
            approve_hash = send_tx(token_in, bytes.fromhex(approve_data[2:]))

        deadline = int(time.time()) + 20 * 60
        t0 = time.perf_counter()
        fn = self.router.functions.swapExactTokensForTokensSimple(
            amount_in,
            min_amount_out,
            _Web3.to_checksum_address(path[0]),
            _Web3.to_checksum_address(path[1]),
            bool(self.stable),
            erc20_owner,
            deadline,
        )
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        try:
            _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "simple"})
        except Exception:
            pass
        try:
            _bucket_latency("trade", self.name, time.perf_counter() - t0)
        except Exception:
            pass
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path: List[Address]) -> Dict[str, str]:
        # Ensure allowance for input token to router up to the max allowed amount
        token_in = path[0]
        from web3 import Web3 as _Web3  # type: ignore
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in) or ""

        deadline = int(time.time()) + 20 * 60
        fn = self.router.functions.swapTokensForExactTokens(
            amount_out, max_amount_in, path, recipient, deadline
        )
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}


class DexAggregator:
    def __init__(self, providers: List[DexProvider]) -> None:
        self.providers = providers
        import os as _os
        self._circ_failures = int((_os.getenv("DEX_CIRCUIT_FAILURES") or "3").strip() or 3)
        self._circ_cool = int((_os.getenv("DEX_CIRCUIT_COOL_OFF_SECONDS") or "60").strip() or 60)
        self._circ: Dict[str, Dict[str, float | int]] = {}

    def _circ_is_open(self, provider: str) -> bool:
        st = self._circ.get(provider)
        if not st:
            return False
        ou = float(st.get("open_until", 0.0))
        return ou and time.time() < ou

    def _circ_on_success(self, provider: str) -> None:
        st = self._circ.get(provider)
        if st:
            st["failures"] = 0
            st["open_until"] = 0.0

    def _circ_on_failure(self, provider: str) -> None:
        st = self._circ.setdefault(provider, {"open_until": 0.0, "failures": 0})
        st["failures"] = int(st.get("failures", 0)) + 1
        if int(st["failures"]) >= int(self._circ_failures):
            st["open_until"] = float(time.time() + self._circ_cool)
            try:
                _metrics_inc("dex_circuit_open_total", labels={"provider": provider})
            except Exception:
                pass

    def quote_all(self, amount_in: int, path: List[Address]) -> List[Quote]:
        quotes: List[Quote] = []
        for p in self.providers:
            if self._circ_is_open(p.name):
                try:
                    _metrics_inc("dex_circuit_skips_total", labels={"provider": p.name})
                except Exception:
                    pass
                continue
            try:
                q = p.quote(amount_in, path)
            except Exception:
                self._circ_on_failure(p.name)
                q = None
            if q is not None:
                quotes.append(q)
        return quotes

    def best_quote(self, amount_in: int, path: List[Address]) -> Optional[Quote]:
        quotes = self.quote_all(amount_in, path)
        if not quotes:
            try:
                _metrics_inc("dex_agg_no_quotes_total")
            except Exception:
                pass
            return None
        best = max(quotes, key=lambda q: q.amount_out)
        try:
            _metrics_inc("dex_agg_selected_total", labels={"provider": best.provider})
        except Exception:
            pass
        return best

    def trade_best(self, amount_in: int, min_out_bps: int, path: List[Address]) -> Dict[str, str]:
        quote = self.best_quote(amount_in, path)
        if quote is None:
            raise RuntimeError("No quotes available from configured DEX providers")
        # Apply slippage tolerance in basis points
        min_out = quote.amount_out * (10_000 - min_out_bps) // 10_000
        # Find the provider instance by name
        provider = next(p for p in self.providers if p.name == quote.provider)
        try:
            out = provider.trade(amount_in, min_out, path)
            self._circ_on_success(provider.name)
            return out
        except Exception as e:  # noqa: BLE001
            try:
                _metrics_inc("dex_agg_trade_errors_total", labels={"provider": quote.provider, "mode": "exact_in"})
            except Exception:
                pass
            self._circ_on_failure(provider.name)
            raise e

    # --- Exact-out (buy-side) helpers ---
    def quote_all_exact_out(self, amount_out: int, path: List[Address]) -> List[Quote]:
        quotes: List[Quote] = []
        for p in self.providers:
            if self._circ_is_open(p.name):
                try:
                    _metrics_inc("dex_circuit_skips_total", labels={"provider": p.name})
                except Exception:
                    pass
                continue
            if p.name == "aerodrome":
                continue
            try:
                q = p.quote_exact_out(amount_out, path)
            except Exception:
                self._circ_on_failure(p.name)
                q = None
            if q is not None:
                quotes.append(q)
        return quotes

    def best_quote_exact_out(self, amount_out: int, path: List[Address]) -> Optional[Quote]:
        quotes = self.quote_all_exact_out(amount_out, path)
        if not quotes:
            try:
                _metrics_inc("dex_agg_no_quotes_total", labels={"mode": "exact_out"})
            except Exception:
                pass
            return None
        # For exact-out, prefer the smallest required input amount
        best = min(quotes, key=lambda q: q.amount_in)
        try:
            _metrics_inc("dex_agg_selected_total", labels={"provider": best.provider, "mode": "exact_out"})
        except Exception:
            pass
        return best

    def trade_best_exact_out(self, amount_out: int, max_in_bps: int, path: List[Address]) -> Dict[str, str]:
        quote = self.best_quote_exact_out(amount_out, path)
        if quote is None:
            raise RuntimeError("No exact-out quotes available from configured DEX providers")
        max_in = quote.amount_in * (10_000 + max_in_bps) // 10_000
        provider = next(p for p in self.providers if p.name == quote.provider)
        try:
            out = provider.trade_exact_out(amount_out, max_in, path)
            self._circ_on_success(provider.name)
            try:
                _metrics_inc("dex_agg_trade_total", labels={"provider": quote.provider, "mode": "exact_out"})
            except Exception:
                pass
            return out
        except Exception as e:  # noqa: BLE001
            try:
                _metrics_inc("dex_agg_trade_errors_total", labels={"provider": quote.provider, "mode": "exact_out"})
            except Exception:
                pass
            self._circ_on_failure(provider.name)
            raise e


def build_aggregator_from_env() -> DexAggregator:
    providers_env = os.getenv("DEX_PROVIDERS", "uniswap_v2,aerodrome")
    provider_names = [p.strip().lower() for p in providers_env.split(",") if p.strip()]
    providers: List[DexProvider] = []

    # Uniswap V2
    if "uniswap_v2" in provider_names:
        uni_addr = os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv("ROUTER_ADDRESS")
        if uni_addr:
            providers.append(UniswapV2DexProvider(uni_addr))

    # Aerodrome
    if "aerodrome" in provider_names:
        aero_addr = os.getenv("AERODROME_ROUTER_ADDRESS")
        if aero_addr:
            stable = os.getenv("AERODROME_STABLE", "true").lower() in {"1", "true", "yes", "y"}
            providers.append(AerodromeDexProvider(aero_addr, stable=stable))

    if not providers:
        raise EnvironmentError("No DEX providers configured. Set DEX_PROVIDERS and router envs.")

    return DexAggregator(providers)
