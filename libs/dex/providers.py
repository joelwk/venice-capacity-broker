from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Sequence
import inspect

from libs.agentkit_ext.agentkit_wallet import get_address, send_tx
from libs.agentkit_ext.web3_utils import encode_contract_call, get_contract, get_web3
from libs.dex.routes import RouteLike, RoutePlan, as_route_plan, make_route

try:
    from libs.telemetry.logger import get_logger  # type: ignore

    _logger = get_logger("dex.agg")
except Exception:  # noqa: BLE001
    class _L:  # minimal stub
        def info(self, *args, **kwargs):
            return

        def debug(self, *args, **kwargs):
            return

        def warning(self, *args, **kwargs):
            return

    _logger = _L()  # type: ignore[assignment]

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # noqa: BLE001
    def _metrics_inc(name: str, value: int = 1, labels: Dict[str, str] | None = None) -> None:  # type: ignore
        return


Address = str




def _debug_routes_enabled() -> bool:
    flag = os.getenv("DIEM_DEBUG_ROUTES")
    if flag is None:
        return False
    return str(flag).strip().lower() in {"1", "true", "yes", "on"}


def _bucket_latency(operation: str, provider: str, latency_seconds: float) -> None:
    """Helper to record latency metrics by bucket."""
    try:
        if latency_seconds < 0.05:
            bucket = "lt_50ms"
        elif latency_seconds < 0.1:
            bucket = "lt_100ms"
        elif latency_seconds < 0.2:
            bucket = "lt_200ms"
        elif latency_seconds < 0.5:
            bucket = "lt_500ms"
        elif latency_seconds < 1.0:
            bucket = "lt_1s"
        elif latency_seconds < 2.0:
            bucket = "lt_2s"
        else:
            bucket = "ge_2s"
        _metrics_inc(
            f"dex_{operation}_latency_bucket_total",
            labels={"provider": provider, "bucket": bucket},
        )
    except Exception:
        pass


@dataclass
class Quote:
    provider: str
    amount_in: int
    amount_out: int
    route: Optional[RoutePlan] = None
    path: Optional[List[Address]] = None

    def __post_init__(self) -> None:
        if self.route is None:
            if self.path is None:
                raise ValueError("Quote requires either route or path")
            route = make_route(self.path)
            object.__setattr__(self, "route", route)
            object.__setattr__(self, "path", list(route.tokens))
        else:
            if self.path is None:
                object.__setattr__(self, "path", list(self.route.tokens))
            else:
                object.__setattr__(self, "path", list(self.path))

    def as_route(self) -> RoutePlan:
        assert self.route is not None
        return self.route


class DexProvider:
    name: str
    supports_exact_in: bool = True
    supports_exact_out: bool = False
    supports_reserve_math: bool = False
    supports_mid_price: bool = False

    def quote(self, amount_in: int, route: RoutePlan) -> Optional[Quote]:
        raise NotImplementedError

    def trade(self, amount_in: int, min_amount_out: int, route: RoutePlan) -> Dict[str, str]:
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Optional[Quote]:
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route: RoutePlan) -> Dict[str, str]:
        raise NotImplementedError


class UniswapV2DexProvider(DexProvider):
    name = "uniswap_v2"
    supports_exact_out = True
    supports_reserve_math = True
    supports_mid_price = True

    def __init__(self, router_address: Address) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.router = get_contract(self.w3, self.router_addr, "uniswap_v2_router.json")
        self.recipient: Optional[str] = None

    @staticmethod
    def _latency_bucket_name(seconds: float) -> str:
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

    def _ensure_allowance(self, token: Address, owner: Address, spender: Address, required: int) -> Optional[str]:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = encode_contract_call(erc20, "approve", [spender, required])
        return send_tx(token, bytes.fromhex(approve_data[2:]))

    def quote(self, amount_in: int, route: RoutePlan) -> Optional[Quote]:
        from web3 import Web3 as _Web3  # type: ignore

        t0 = time.perf_counter()
        try:
            checksum_path = route.to_uniswap_v2_path(checksum=True)
            amounts = self.router.functions.getAmountsOut(amount_in, checksum_path).call()
            out_amt = int(amounts[-1]) if amounts else 0
            if out_amt <= 0:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "zero"})
                return None
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            _metrics_inc(
                "dex_quote_latency_bucket_total",
                labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
            )
            return Quote(provider=self.name, amount_in=amount_in, amount_out=out_amt, route=route)
        except Exception:
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
            return None

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Optional[Quote]:
        from web3 import Web3 as _Web3  # type: ignore

        try:
            checksum_path = route.to_uniswap_v2_path(checksum=True)
            amounts = self.router.functions.getAmountsIn(amount_out, checksum_path).call()
            in_amt = int(amounts[0]) if amounts else 0
            if in_amt <= 0:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "zero"})
                return None
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            return Quote(provider=self.name, amount_in=in_amt, amount_out=amount_out, route=route)
        except Exception:
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
            return None

    def trade(self, amount_in: int, min_amount_out: int, route: RoutePlan) -> Dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        checksum_path = route.to_uniswap_v2_path(checksum=True)
        token_in = checksum_path[0]
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, amount_in) or ""
        deadline = int(time.time()) + 20 * 60
        t0 = time.perf_counter()
        try:
            fn = self.router.functions.swapExactTokensForTokens(amount_in, min_amount_out, checksum_path, recipient, deadline)
            built = fn.build_transaction({})
            tx_hash = send_tx(self.router_addr, built["data"])
            _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "standard"})
            _metrics_inc(
                "dex_trade_latency_bucket_total",
                labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
            )
            return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}
        except Exception:
            try:
                fn2 = self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                    amount_in,
                    min_amount_out,
                    checksum_path,
                    recipient,
                    deadline,
                )
                built2 = fn2.build_transaction({})
                tx_hash2 = send_tx(self.router_addr, built2["data"])
                _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "fot"})
                _metrics_inc("fot_fallback_total", labels={"provider": self.name})
                _metrics_inc(
                    "dex_trade_latency_bucket_total",
                    labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
                )
                return {
                    "provider": self.name,
                    "tx_hash": tx_hash2,
                    "approval_tx": approve_hash,
                    "fot_fallback": "true",
                }
            except Exception as e:  # noqa: BLE001
                _metrics_inc("dex_trade_errors_total", labels={"provider": self.name, "path": "fot"})
                raise e

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route: RoutePlan) -> Dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        checksum_path = route.to_uniswap_v2_path(checksum=True)
        token_in = checksum_path[0]
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in) or ""
        deadline = int(time.time()) + 20 * 60
        t0 = time.perf_counter()
        try:
            fn = self.router.functions.swapTokensForExactTokens(amount_out, max_amount_in, checksum_path, recipient, deadline)
            built = fn.build_transaction({})
            tx_hash = send_tx(self.router_addr, built["data"])
            _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "exact_out"})
            _metrics_inc(
                "dex_trade_latency_bucket_total",
                labels={"provider": self.name, "bucket": self._latency_bucket_name(time.perf_counter() - t0)},
            )
            return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}
        except Exception as e:  # noqa: BLE001
            _metrics_inc("dex_trade_errors_total", labels={"provider": self.name, "path": "exact_out"})
            raise e


class AerodromeDexProvider(DexProvider):
    name = "aerodrome"

    def __init__(self, router_address: Address, stable: bool = True) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.router = get_contract(self.w3, self.router_addr, "aerodrome_router.json")
        self.recipient: Optional[str] = None
        self.stable = bool(stable)

    def _ensure_allowance(self, token: Address, owner: Address, spender: Address, required: int) -> Optional[str]:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = encode_contract_call(erc20, "approve", [spender, required])
        return send_tx(token, bytes.fromhex(approve_data[2:]))

    def _routes(self, route: RoutePlan, stable: Optional[bool] = None) -> List[tuple[Address, Address, bool]]:
        from web3 import Web3 as _Web3  # type: ignore

        path = route.to_uniswap_v2_path(checksum=True)
        st = bool(self.stable) if stable is None else bool(stable)
        hops: List[tuple[Address, Address, bool]] = []
        for i in range(len(path) - 1):
            hops.append((path[i], path[i + 1], st))
        return hops

    def _routes_with_mask(self, route: RoutePlan, mask: Sequence[bool]) -> List[tuple[Address, Address, bool]]:
        from web3 import Web3 as _Web3  # type: ignore

        path = route.to_uniswap_v2_path(checksum=True)
        if len(path) - 1 != len(mask):
            raise ValueError("mask length must equal hop count")
        hops: List[tuple[Address, Address, bool]] = []
        for i in range(len(path) - 1):
            hops.append((path[i], path[i + 1], bool(mask[i])))
        return hops

    def quote(self, amount_in: int, route: RoutePlan) -> Optional[Quote]:
        t0 = time.perf_counter()
        try:
            routes = self._routes(route, stable=self.stable)
            amounts = self.router.functions.getAmountsOut(amount_in, routes).call()
            out_amt = int(amounts[-1]) if amounts else 0
            if out_amt <= 0:
                return None
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            _bucket_latency("quote", self.name, time.perf_counter() - t0)
            return Quote(provider=self.name, amount_in=amount_in, amount_out=out_amt, route=route)
        except Exception:
            pass
        try:
            routes = self._routes(route, stable=not bool(self.stable))
            amounts = self.router.functions.getAmountsOut(amount_in, routes).call()
            out_amt = int(amounts[-1]) if amounts else 0
            if out_amt <= 0:
                return None
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            _bucket_latency("quote", self.name, time.perf_counter() - t0)
            return Quote(provider=self.name, amount_in=amount_in, amount_out=out_amt, route=route)
        except Exception:
            pass
        try:
            hops = len(route.hops)
            if hops >= 2 and hops <= 3:
                total = 1 << hops
                for bits in range(total):
                    if bits == 0 or bits == (total - 1):
                        continue
                    mask = [bool((bits >> i) & 1) for i in range(hops)]
                    try:
                        routes = self._routes_with_mask(route, mask)
                        amounts = self.router.functions.getAmountsOut(amount_in, routes).call()
                        out_amt = int(amounts[-1]) if amounts else 0
                        if out_amt <= 0:
                            continue
                        _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
                        _bucket_latency("quote", self.name, time.perf_counter() - t0)
                        return Quote(provider=self.name, amount_in=amount_in, amount_out=out_amt, route=route)
                    except Exception:
                        continue
        except Exception:
            pass
        _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
        return None

    def trade(self, amount_in: int, min_amount_out: int, route: RoutePlan) -> Dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        path = route.to_uniswap_v2_path(checksum=True)
        token_in = path[0]
        erc20_owner = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, erc20_owner, self.router_addr, amount_in) or ""
        deadline = int(time.time()) + 20 * 60
        t0 = time.perf_counter()
        fn = self.router.functions.swapExactTokensForTokensSimple(
            amount_in,
            min_amount_out,
            path[0],
            path[1],
            bool(self.stable),
            erc20_owner,
            deadline,
        )
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc("dex_trades_total", labels={"provider": self.name, "path": "simple"})
        _bucket_latency("trade", self.name, time.perf_counter() - t0)
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route: RoutePlan) -> Dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        path = route.to_uniswap_v2_path(checksum=True)
        token_in = path[0]
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in) or ""
        deadline = int(time.time()) + 20 * 60
        fn = self.router.functions.swapTokensForExactTokens(amount_out, max_amount_in, path, recipient, deadline)
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}


class UniswapV3DexProvider(DexProvider):
    name = "uniswap_v3"
    supports_exact_out = True

    def __init__(
        self,
        router_address: Address,
        quoter_address: Address,
        *,
        default_fee: Optional[int] = None,
        allowed_fee_tiers: Optional[Sequence[int]] = None,
    ) -> None:
        from web3 import Web3  # type: ignore

        self.w3 = get_web3()
        self.router_addr = Web3.to_checksum_address(router_address)
        self.quoter_addr = Web3.to_checksum_address(quoter_address)
        self.router = get_contract(self.w3, self.router_addr, "uniswap_v3_router.json")
        self.quoter = get_contract(self.w3, self.quoter_addr, "uniswap_v3_quoter.json")
        self.recipient: Optional[str] = None
        self.default_fee = int(default_fee) if default_fee not in (None, "") else None
        if allowed_fee_tiers is None:
            self.allowed_fee_tiers: Optional[tuple[int, ...]] = None
        else:
            tiers = sorted({int(f) for f in allowed_fee_tiers})
            self.allowed_fee_tiers = tuple(tiers)

    def _ensure_route(self, route: RoutePlan) -> RoutePlan:
        filled = route
        if any(h.fee is None for h in filled.hops):
            if self.default_fee is None:
                raise ValueError("fee tier required for Uniswap V3 route")
            filled = route.with_default_fee(self.default_fee)
        if self.allowed_fee_tiers:
            for hop in filled.hops:
                if hop.fee not in self.allowed_fee_tiers:
                    raise ValueError(f"fee tier {hop.fee} not permitted for provider {self.name}")
        return filled

    def _ensure_allowance(self, token: Address, owner: Address, spender: Address, required: int) -> Optional[str]:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = encode_contract_call(erc20, "approve", [spender, required])
        return send_tx(token, bytes.fromhex(approve_data[2:]))

    def _normalize_quote_result(self, value: Any) -> int:
        if isinstance(value, (list, tuple)):
            return int(value[0]) if value else 0
        return int(value or 0)

    def quote(self, amount_in: int, route: RoutePlan) -> Optional[Quote]:
        t0 = time.perf_counter()
        try:
            effective_route = self._ensure_route(route)
            path_bytes = effective_route.to_uniswap_v3_path_bytes()
            result = self.quoter.functions.quoteExactInput(path_bytes, amount_in).call()
            out_amt = self._normalize_quote_result(result)
            if out_amt <= 0:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "zero"})
                return None
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok"})
            _bucket_latency("quote", self.name, time.perf_counter() - t0)
            return Quote(provider=self.name, amount_in=amount_in, amount_out=out_amt, route=effective_route)
        except Exception:
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err"})
            return None

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Optional[Quote]:
        try:
            effective_route = self._ensure_route(route)
            path_bytes = effective_route.to_uniswap_v3_path_bytes(reverse=True)
            result = self.quoter.functions.quoteExactOutput(path_bytes, amount_out).call()
            in_amt = self._normalize_quote_result(result)
            if in_amt <= 0:
                _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "zero", "mode": "exact_out"})
                return None
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "ok", "mode": "exact_out"})
            return Quote(provider=self.name, amount_in=in_amt, amount_out=amount_out, route=effective_route)
        except Exception:
            _metrics_inc("dex_quotes_total", labels={"provider": self.name, "status": "err", "mode": "exact_out"})
            return None

    def trade(self, amount_in: int, min_amount_out: int, route: RoutePlan) -> Dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        effective_route = self._ensure_route(route)
        token_in = _Web3.to_checksum_address(effective_route.tokens[0])
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, amount_in) or ""
        deadline = int(time.time()) + 20 * 60
        params = (
            effective_route.to_uniswap_v3_path_bytes(),
            recipient,
            deadline,
            amount_in,
            min_amount_out,
        )
        fn = self.router.functions.exactInput(params)
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc("dex_trades_total", labels={"provider": self.name, "mode": "exact_in"})
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route: RoutePlan) -> Dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        effective_route = self._ensure_route(route)
        token_in = _Web3.to_checksum_address(effective_route.tokens[0])
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in) or ""
        deadline = int(time.time()) + 20 * 60
        params = (
            effective_route.to_uniswap_v3_path_bytes(reverse=True),
            recipient,
            deadline,
            amount_out,
            max_amount_in,
        )
        fn = self.router.functions.exactOutput(params)
        built = fn.build_transaction({})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc("dex_trades_total", labels={"provider": self.name, "mode": "exact_out"})
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}


class DexAggregator:
    def __init__(self, providers: List[DexProvider]) -> None:
        self.providers = providers
        self._lock = Lock()
        self._circ_failures = int((os.getenv("DEX_CIRCUIT_FAILURES") or "3").strip() or 3)
        self._circ_cool = int((os.getenv("DEX_CIRCUIT_COOL_OFF_SECONDS") or "60").strip() or 60)
        self._circ: Dict[str, Dict[str, float | int]] = {}
        try:
            self._timeout = float((os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS") or "0.5").strip() or 0.5)
        except Exception:
            self._timeout = 0.5
        if self._timeout < 0:
            self._timeout = 0.0
        try:
            worker_env = os.getenv("DEX_AGGREGATOR_MAX_WORKERS")
            configured_workers = int(worker_env.strip()) if worker_env else 0
        except Exception:
            configured_workers = 0
        default_workers = len(self.providers) if self.providers else 1
        self._max_workers = max(1, configured_workers or default_workers)
        try:
            self._circ_backoff = float((os.getenv("DEX_CIRCUIT_BACKOFF_MULT") or "2.0").strip() or 2.0)
        except Exception:
            self._circ_backoff = 2.0
        if self._circ_backoff < 1.0:
            self._circ_backoff = 1.0
        try:
            self._circ_max_cool = float((os.getenv("DEX_CIRCUIT_MAX_COOL_SECONDS") or "300").strip() or 300.0)
        except Exception:
            self._circ_max_cool = 300.0
        if self._circ_max_cool <= 0:
            self._circ_max_cool = float(self._circ_cool)

    @staticmethod
    def _invoke_provider(provider: DexProvider, method: str, route: RoutePlan, *args):
        fn = getattr(provider, method)
        try:
            params = list(inspect.signature(fn).parameters)
            wants_route = bool(params and params[-1] in {"route", "route_plan", "plan"})
        except (ValueError, TypeError):
            wants_route = False
        arg = route if wants_route else list(route.tokens)
        return fn(*args, arg)  # type: ignore[misc]

    @staticmethod
    def _supports_exact_out(provider: DexProvider) -> bool:
        if provider.name == "aerodrome":
            return False
        if provider.supports_exact_out:
            return True
        # Detect overrides of quote_exact_out
        try:
            base_impl = DexProvider.quote_exact_out
            return provider.__class__.quote_exact_out is not base_impl
        except AttributeError:
            return False

    def _circ_is_open(self, provider: str) -> bool:
        with self._lock:
            st = self._circ.get(provider)
            if not st:
                return False
            ou = float(st.get("open_until", 0.0))
            return bool(ou and time.time() < ou)

    def _circ_on_success(self, provider: str) -> None:
        with self._lock:
            st = self._circ.get(provider)
            if st:
                st["failures"] = 0
                st["open_until"] = 0.0

    def _circ_on_failure(self, provider: str, reason: str = "error") -> None:
        with self._lock:
            st = self._circ.setdefault(provider, {"open_until": 0.0, "failures": 0})
            failures = int(st.get("failures", 0)) + 1
            st["failures"] = failures
            if failures >= int(self._circ_failures):
                extra = max(0, failures - self._circ_failures)
                cool = self._circ_cool * (self._circ_backoff ** extra)
                cool = min(cool, self._circ_max_cool)
                st["open_until"] = float(time.time() + cool)
                _metrics_inc("dex_circuit_open_total", labels={"provider": provider, "reason": reason, "cooldown": str(int(cool))})
                _metrics_inc("dex_circuit_open_total", labels={"provider": provider})
            else:
                st["open_until"] = float(st.get("open_until", 0.0))

    def _collect_quotes(self, providers: List[DexProvider], method: str, route_plan: RoutePlan, amount: int) -> List[Quote]:
        quotes: List[Quote] = []
        if not providers:
            return quotes
        max_workers = min(len(providers), self._max_workers)
        timeout = self._timeout if self._timeout and self._timeout > 0 else None
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(self._invoke_provider, provider, method, route_plan, amount): provider for provider in providers}
            for future, provider in future_map.items():
                try:
                    quote = future.result(timeout=timeout)
                except TimeoutError:
                    future.cancel()
                    if _debug_routes_enabled():
                        route_tokens = list(route_plan.tokens)
                        _logger.warning(
                            "dex aggregator timeout provider=%s method=%s route=%s amount=%s",
                            provider.name,
                            method,
                            route_tokens,
                            int(amount),
                        )
                    _metrics_inc("dex_agg_timeouts_total", labels={"provider": provider.name, "method": method})
                    self._circ_on_failure(provider.name, reason="timeout")
                    continue
                except Exception as exc:
                    if _debug_routes_enabled():
                        route_tokens = list(route_plan.tokens)
                        _logger.warning(
                            "dex aggregator error provider=%s method=%s route=%s amount=%s error=%s",
                            provider.name,
                            method,
                            route_tokens,
                            int(amount),
                            exc,
                        )
                    _metrics_inc("dex_agg_provider_errors_total", labels={"provider": provider.name, "method": method})
                    self._circ_on_failure(provider.name)
                    continue
                if quote is not None:
                    if quote.route is None:
                        object.__setattr__(quote, "route", route_plan)
                    quotes.append(quote)
                elif _debug_routes_enabled():
                    route_tokens = list(route_plan.tokens)
                    _logger.info("dex aggregator empty quote provider=%s method=%s route=%s amount=%s", provider.name, method, route_tokens, int(amount))
        return quotes

    def quote_all(self, amount_in: int, route: RouteLike) -> List[Quote]:
        route_plan = as_route_plan(route)
        active: List[DexProvider] = []
        for provider in self.providers:
            if self._circ_is_open(provider.name):
                _metrics_inc("dex_circuit_skips_total", labels={"provider": provider.name})
                continue
            active.append(provider)
        return self._collect_quotes(active, "quote", route_plan, amount_in)

    def best_quote(self, amount_in: int, route: RouteLike) -> Optional[Quote]:
        quotes = self.quote_all(amount_in, route)
        if not quotes:
            if _debug_routes_enabled():
                tokens = list(as_route_plan(route).tokens)
                _logger.warning("dex aggregator no quotes route=%s amount_in=%s mode=exact_in", tokens, int(amount_in))
            _metrics_inc("dex_agg_no_quotes_total")
            return None
        best = max(quotes, key=lambda q: q.amount_out)
        _metrics_inc("dex_agg_selected_total", labels={"provider": best.provider})
        return best

    def trade_best(self, amount_in: int, min_out_bps: int, route: RouteLike | None = None, **kwargs) -> Dict[str, str]:
        route = kwargs.get("route", route)
        if route is None and "path" in kwargs:
            route = kwargs["path"]
        if route is None:
            raise ValueError("route/path is required")
        quote = self.best_quote(amount_in, route)
        if quote is None:
            raise RuntimeError("No quotes available from configured DEX providers")
        min_out = quote.amount_out * (10_000 - min_out_bps) // 10_000
        provider = next(p for p in self.providers if p.name == quote.provider)
        try:
            result = self._invoke_provider(provider, "trade", quote.as_route(), amount_in, min_out)
            self._circ_on_success(provider.name)
            return result
        except Exception as exc:  # noqa: BLE001
            _metrics_inc("dex_agg_trade_errors_total", labels={"provider": quote.provider, "mode": "exact_in"})
            self._circ_on_failure(provider.name)
            raise exc

    def quote_all_exact_out(self, amount_out: int, route: RouteLike) -> List[Quote]:
        route_plan = as_route_plan(route)
        active: List[DexProvider] = []
        for provider in self.providers:
            if not self._supports_exact_out(provider):
                continue
            if self._circ_is_open(provider.name):
                _metrics_inc("dex_circuit_skips_total", labels={"provider": provider.name})
                continue
            active.append(provider)
        return self._collect_quotes(active, "quote_exact_out", route_plan, amount_out)

    def best_quote_exact_out(self, amount_out: int, route: RouteLike) -> Optional[Quote]:
        quotes = self.quote_all_exact_out(amount_out, route)
        if not quotes:
            if _debug_routes_enabled():
                tokens = list(as_route_plan(route).tokens)
                _logger.warning("dex aggregator no quotes route=%s amount_out=%s mode=exact_out", tokens, int(amount_out))
            _metrics_inc("dex_agg_no_quotes_total", labels={"mode": "exact_out"})
            return None
        best = min(quotes, key=lambda q: q.amount_in)
        _metrics_inc("dex_agg_selected_total", labels={"provider": best.provider, "mode": "exact_out"})
        return best

    def trade_best_exact_out(self, amount_out: int, max_in_bps: int, route: RouteLike | None = None, **kwargs) -> Dict[str, str]:
        route = kwargs.get("route", route)
        if route is None and "path" in kwargs:
            route = kwargs["path"]
        if route is None:
            raise ValueError("route/path is required")
        quote = self.best_quote_exact_out(amount_out, route)
        if quote is None:
            raise RuntimeError("No exact-out quotes available from configured DEX providers")
        max_in = quote.amount_in * (10_000 + max_in_bps) // 10_000
        provider = next(p for p in self.providers if p.name == quote.provider)
        try:
            result = self._invoke_provider(provider, "trade_exact_out", quote.as_route(), amount_out, max_in)
            self._circ_on_success(provider.name)
            _metrics_inc("dex_agg_trade_total", labels={"provider": quote.provider, "mode": "exact_out"})
            return result
        except Exception as exc:  # noqa: BLE001
            _metrics_inc("dex_agg_trade_errors_total", labels={"provider": quote.provider, "mode": "exact_out"})
            self._circ_on_failure(provider.name)
            raise exc


def _parse_providers_spec(raw: str) -> List[Dict[str, Any]]:
    value = (raw or "").strip()
    if not value:
        return []
    if value[0] in "[{":
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                parsed = [parsed]
            if isinstance(parsed, list):
                out: List[Dict[str, Any]] = []
                for item in parsed:
                    if isinstance(item, dict):
                        out.append(item)
                    else:
                        out.append({"name": str(item)})
                return out
        except Exception:
            _logger.warning("failed to parse DEX_PROVIDERS JSON; falling back to legacy parsing", exc_info=True)
    return [{"name": part.strip()} for part in value.split(",") if part.strip()]


def _provider_from_spec(spec: Dict[str, Any]) -> Optional[DexProvider]:
    name = str(spec.get("name", "")).strip().lower()
    if not name:
        return None
    if name == "uniswap_v2":
        router = spec.get("router") or os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv("ROUTER_ADDRESS")
        if not router:
            _logger.warning("uniswap_v2 selected but router address missing")
            return None
        return UniswapV2DexProvider(router)
    if name == "uniswap_v3":
        router = spec.get("router") or os.getenv("UNISWAP_V3_ROUTER_ADDRESS")
        quoter = spec.get("quoter") or os.getenv("UNISWAP_V3_QUOTER_ADDRESS")
        if not router or not quoter:
            _logger.warning("uniswap_v3 selected but router/quoter addresses missing")
            return None
        default_fee = spec.get("default_fee")
        if default_fee in (None, ""):
            env_default = os.getenv("UNISWAP_V3_DEFAULT_FEE")
            default_fee = int(env_default) if env_default else None
        fee_tiers = spec.get("fee_tiers") or spec.get("fees")
        if isinstance(fee_tiers, str):
            tiers = [int(part.strip()) for part in fee_tiers.split(",") if part.strip()]
            fee_tiers = tiers
        return UniswapV3DexProvider(
            router,
            quoter,
            default_fee=default_fee,
            allowed_fee_tiers=fee_tiers,
        )
    if name == "aerodrome":
        router = spec.get("router") or os.getenv("AERODROME_ROUTER_ADDRESS")
        if not router:
            _logger.warning("aerodrome selected but router address missing")
            return None
        stable_val = spec.get("stable")
        if stable_val is None:
            stable_env = str(os.getenv("AERODROME_STABLE", "true")).lower()
            stable_val = stable_env in {"1", "true", "yes", "y"}
        return AerodromeDexProvider(router, stable=bool(stable_val))
    _logger.warning("unknown dex provider name: %s", name)
    return None


def build_aggregator_from_env() -> DexAggregator:
    raw_spec = os.getenv("DEX_PROVIDERS", "uniswap_v2,aerodrome")
    specs = _parse_providers_spec(raw_spec)
    providers: List[DexProvider] = []
    for spec in specs:
        provider = _provider_from_spec(spec)
        if provider is not None:
            providers.append(provider)
    if not providers:
        raise EnvironmentError("No DEX providers configured. Set DEX_PROVIDERS and router envs.")
    return DexAggregator(providers)
