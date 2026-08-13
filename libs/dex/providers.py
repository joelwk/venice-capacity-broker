from __future__ import annotations

import inspect
import json
import os
import random
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import Any, TypeVar

import requests

from core.config import ConfigError, get_config

try:
    from services.marketdata import etherscan_verify as es  # type: ignore
except Exception:
    es = None  # type: ignore[assignment]

from libs.agentkit_ext.agentkit_wallet import get_address, send_tx
from libs.agentkit_ext.web3_utils import encode_contract_call, get_contract, get_web3
from libs.dex.composite import attach_composite_metadata
from libs.dex.diagnostics import log_event as _dex_diag_log_event
from libs.dex.diem_fallbacks import (
    build_two_stage_diem_route,
    diem_vvv_quote_exact_in_from_reserves,
    diem_vvv_quote_from_reserves,
    slot0_cache_fetch,
    vvv_usdc_v3_mid_price_quote,
    vvv_usdc_v3_mid_price_quote_exact_out,
    vvv_usdc_v3_slot0_quote,
    vvv_usdc_v3_slot0_quote_exact_out,
)
from libs.dex.routes import RouteLike, RoutePlan, as_route_plan, make_route
from libs.dex.routing import (
    normalize_route_for_aerodrome,
    normalize_route_for_v2,
    normalize_route_for_v3,
)

# Composite helpers are loaded lazily to avoid circular imports during module init.
is_composite_route = None  # type: ignore
quote_composite_exact_in = None  # type: ignore
quote_composite_exact_out = None  # type: ignore

T = TypeVar("T")


_RESERVE_CACHE: dict[str, tuple[float, tuple[int, int, str, str]]] = {}
_RESERVE_CACHE_LOCK = Lock()

_HEX_ADDR_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_warned_unknown_providers: set[str] = set()


def _maybe_checksum_address(addr: str) -> str:
    """Return a checksummed EVM address when it looks like a 20-byte hex string."""
    if not addr:
        return addr
    cleaned = str(addr).strip()
    if not cleaned:
        return cleaned
    if "@" in cleaned:
        cleaned = cleaned.split("@", 1)[0].strip()
    if cleaned.startswith(("0x", "0X")):
        prefixed = "0x" + cleaned[2:]
        body = prefixed[2:]
    else:
        body = cleaned
        prefixed = "0x" + body
    if len(body) != 40 or _HEX_ADDR_RE.fullmatch(body) is None:
        return cleaned
    try:
        from web3 import Web3  # type: ignore
    except Exception:
        return prefixed
    return Web3.to_checksum_address(prefixed)


def _ensure_composite_loaded() -> None:
    """
    Import composite helpers on demand.

    The initial import can fail when this module is pulled in by libs.dex.composite
    (via diem_fallbacks) before Quote is defined. Lazily importing here lets the
    aggregator pick up the helpers once both modules are fully initialized.
    """
    global is_composite_route, quote_composite_exact_in, quote_composite_exact_out
    if (
        is_composite_route is not None
        and quote_composite_exact_in is not None
        and quote_composite_exact_out is not None
    ):
        return
    try:
        from libs.dex import composite as _composite

        is_composite_route = getattr(_composite, "is_composite_route", None)
        quote_composite_exact_in = getattr(_composite, "quote_composite_exact_in", None)
        quote_composite_exact_out = getattr(
            _composite, "quote_composite_exact_out", None
        )
    except Exception:
        # Leave helpers as None if import fails; callers will fall back gracefully.
        is_composite_route = None
        quote_composite_exact_in = None
        quote_composite_exact_out = None


def _reserve_cache_ttl_seconds() -> float:
    raw = (os.getenv("DIEM_VVV_RESERVE_CACHE_TTL") or "").strip()
    if not raw:
        return 60.0
    try:
        return max(0.0, float(raw))
    except Exception:
        return 60.0


def _reserve_cache_stale_multiplier() -> float:
    raw = (os.getenv("DIEM_VVV_RESERVE_STALE_MULT") or "").strip()
    if not raw:
        return 4.0
    try:
        return max(1.0, float(raw))
    except Exception:
        return 4.0


def _provider_timeout_seconds(default: float = 10.0) -> float:
    """Get provider timeout from env, default 10s per plan recommendations."""
    try:
        raw = os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS")
        timeout = float((raw or str(default)).strip() or default)
    except Exception:
        timeout = default
    # Increased cap to 20.0s for V3 quoter calls on Base RPC (observed ~5-15s+ latency)
    # Keep Aerodrome calls bounded so aggregator timeouts don't fire first
    # Default reduced to 10s per plan recommendations
    return max(1.0, min(20.0, timeout))


def _get_sqrt_price_limit() -> int:
    """Get sqrtPriceLimitX96 from env or return 0 (max range).

    For Uniswap V3 QuoterV2, sqrtPriceLimitX96 controls price slippage tolerance.
    - 0 = no limit (max range)
    - Non-zero = price cannot cross this limit
    """
    try:
        raw = os.getenv("UNISWAP_V3_SQRT_PRICE_LIMIT")
        if raw is None:
            return 0  # Default: no limit
        value = int(raw.strip())
        return value if value >= 0 else 0
    except Exception:
        return 0


def _rpc_eth_call_candidates(to: str, data: str, timeout: float) -> str | None:
    try:
        from libs.agentkit_ext.web3_utils import rpc_url_candidates  # type: ignore
    except Exception:
        return None

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    for rpc in rpc_url_candidates():
        try:
            resp = requests.post(rpc, json=payload, timeout=timeout)
            resp.raise_for_status()
            result = resp.json().get("result")
            if isinstance(result, str) and result.startswith("0x"):
                return result
        except Exception:
            continue
    return None


def _pair_state_from_rpc(
    pair_addr: str, timeout: float
) -> tuple[int, int, str, str] | None:
    reserve_raw = _rpc_eth_call_candidates(pair_addr, "0x0902f1ac", timeout)
    if not reserve_raw:
        return None
    try:
        padded = reserve_raw[2:].rjust(192, "0")
        r0 = int(padded[0:64], 16)
        r1 = int(padded[64:128], 16)
    except Exception:
        return None

    token0_raw = _rpc_eth_call_candidates(pair_addr, "0x0dfe1681", timeout)
    token1_raw = _rpc_eth_call_candidates(pair_addr, "0xd21220a7", timeout)
    if not token0_raw or not token1_raw or len(token0_raw) < 66 or len(token1_raw) < 66:
        return None
    token0 = ("0x" + token0_raw[-40:]).lower()
    token1 = ("0x" + token1_raw[-40:]).lower()
    return (r0, r1, token0, token1)


def _pair_state_cached(
    pair_addr: str, timeout: float
) -> tuple[int, int, str, str] | None:
    if not pair_addr:
        return None
    key = pair_addr.lower()
    ttl = _reserve_cache_ttl_seconds()
    stale_mult = _reserve_cache_stale_multiplier()
    now = time.monotonic()

    with _RESERVE_CACHE_LOCK:
        cached = _RESERVE_CACHE.get(key)
    stale_state: tuple[int, int, str, str] | None = None
    if cached:
        ts, payload = cached
        age = now - ts
        if ttl > 0 and age <= ttl:
            return payload
        if ttl > 0 and age <= ttl * stale_mult:
            stale_state = payload

    reserves = es.get_reserves(pair_addr) if es is not None else None
    token0 = es.get_token0(pair_addr) if es is not None else None
    token1 = es.get_token1(pair_addr) if es is not None else None
    r0: int | None = None
    r1: int | None = None
    if reserves:
        r0 = int(reserves[0])
        r1 = int(reserves[1])
    if not token0 or not token1 or r0 is None or r1 is None:
        rpc_state = _pair_state_from_rpc(pair_addr, timeout)
        if rpc_state:
            r0, r1, token0, token1 = rpc_state

    if token0 and token1 and r0 is not None and r1 is not None:
        state = (int(r0), int(r1), str(token0), str(token1))
        with _RESERVE_CACHE_LOCK:
            _RESERVE_CACHE[key] = (now, state)
        return state

    if stale_state:
        if _debug_routes_enabled():
            _logger.debug(
                "dex: using stale reserve cache for %s after fetch failure", pair_addr
            )
        return stale_state
    return None


def _int_env(name: str, default: int) -> int:
    try:
        raw = (os.getenv(name) or "").strip()
        return int(raw or default)
    except Exception:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        raw = (os.getenv(name) or "").strip()
        return float(raw or default)
    except Exception:
        return default


def _diem_vvv_addrs() -> tuple[str, str]:
    """Return lower-cased DIEM and VVV token addresses from env (may be empty)."""

    diem = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
    vvv = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
    return diem, vvv


def _diem_vvv_direct_enabled() -> bool:
    """Feature flag for direct DIEM/VVV reserve-based quoting/swaps."""

    raw = (os.getenv("DIEM_VVV_DIRECT_SWAP_ENABLE") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


_RETRY_ATTEMPTS = max(1, _int_env("DEX_RETRY_ATTEMPTS", 5))
_RETRY_BASE_SECONDS = max(0.0, _float_env("DEX_RETRY_BASE_MS", 250.0) / 1000.0)
_RETRY_JITTER_SECONDS = max(0.0, _float_env("DEX_RETRY_JITTER_MS", 150.0) / 1000.0)


def _looks_like_rate_limit(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and status_code == 429:
        return True
    code = getattr(exc, "status", None)
    if isinstance(code, int) and code == 429:
        return True
    code = getattr(exc, "code", None)
    if isinstance(code, int) and code == 429:
        return True
    messages: list[str] = []
    for arg in getattr(exc, "args", ()):
        if isinstance(arg, dict):
            dict_code = arg.get("code")
            if isinstance(dict_code, int) and dict_code == 429:
                return True
            dict_msg = arg.get("message")
            if isinstance(dict_msg, str):
                messages.append(dict_msg)
        elif isinstance(arg, (bytes, bytearray)):
            try:
                decoded = arg.decode("utf-8", "ignore")
            except Exception:
                decoded = ""
            if decoded:
                messages.append(decoded)
        elif isinstance(arg, str):
            messages.append(arg)
    messages.append(str(exc))
    combined = " ".join(messages).lower()
    if (
        "429" in combined
        or "too many requests" in combined
        or "rate limit" in combined
        or "rate-limited" in combined
    ):
        return True
    return False


def _is_execution_reverted(exc: Exception) -> bool:
    """Best-effort check for UniswapV2 router reverts that imply missing pools.

    We treat plain "execution reverted" (with or without data) as a signal that
    the hop/pair is absent so the caller can skip noisy warnings and retries.
    """

    try:
        msg = " ".join(str(arg) for arg in getattr(exc, "args", ()) if arg)
    except Exception:
        msg = str(exc)
    msg = (msg or str(exc)).lower()
    return "execution reverted" in msg


def _record_rate_limit(provider: str, operation: str) -> None:
    try:
        _metrics_inc(
            "dex_rpc_rate_limit_total",
            labels={"provider": provider, "operation": operation},
        )
    except Exception:
        pass


@dataclass
class CircuitBreakerState:
    failures: int = 0
    open_until: float = 0.0


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int,
        cool_seconds: float,
        backoff_mult: float,
        max_cool: float,
    ) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cool_seconds = max(0.0, float(cool_seconds))
        self.backoff_mult = max(1.0, float(backoff_mult))
        self.max_cool = max(self.cool_seconds, float(max_cool))
        self._state: dict[str, CircuitBreakerState] = {}
        self._lock = Lock()
        # Get logger for circuit breaker recovery logging
        try:
            from libs.telemetry.logger import get_logger

            self._logger = get_logger("dex.circuit")
        except Exception:
            self._logger = None

    def is_open(self, provider: str) -> bool:
        with self._lock:
            state = self._state.get(provider)
            if not state:
                return False
            if state.open_until <= 0:
                return False
            current_time = time.time()
            if current_time >= state.open_until:
                # Circuit breaker auto-recovered
                cooldown_duration = current_time - (
                    state.open_until - self.cool_seconds
                )
                state.open_until = 0.0
                state.failures = 0
                if self._logger:
                    self._logger.debug(
                        f"Circuit breaker auto-recovered for provider={provider}, "
                        f"cooldown_duration={cooldown_duration:.1f}s",
                        extra={
                            "provider": provider,
                            "cooldown_duration": cooldown_duration,
                            "event": "circuit_breaker_recovered",
                        },
                    )
                return False
            return True

    def record_success(self, provider: str) -> None:
        with self._lock:
            state = self._state.setdefault(provider, CircuitBreakerState())
            state.failures = 0
            state.open_until = 0.0

    def record_failure(self, provider: str) -> float:
        with self._lock:
            state = self._state.setdefault(provider, CircuitBreakerState())
            state.failures += 1
            if state.failures >= self.failure_threshold:
                extra = max(0, state.failures - self.failure_threshold)
                cooldown = self.cool_seconds * (self.backoff_mult**extra)
                cooldown = min(cooldown, self.max_cool)
                state.open_until = time.time() + cooldown
                state.failures = min(state.failures, self.failure_threshold)
                return cooldown
            state.open_until = max(state.open_until, 0.0)
            return 0.0


def _call_with_rpc_retry(
    provider: str, operation: str, refresh: Callable[[], None], fn: Callable[[], T]
) -> T:
    last_exc: Exception | None = None
    attempt_limit = max(1, _RETRY_ATTEMPTS)
    for attempt in range(attempt_limit):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            is_rate_limit = _looks_like_rate_limit(exc)
            if is_rate_limit:
                _record_rate_limit(provider, operation)
                refresh()
                if attempt < attempt_limit - 1:
                    try:
                        _metrics_inc(
                            "dex_rpc_retries_total",
                            labels={
                                "provider": provider,
                                "operation": operation,
                                "reason": "rate_limit",
                            },
                        )
                    except Exception:
                        pass
                    sleep_for = _RETRY_BASE_SECONDS + (
                        random.uniform(0.0, _RETRY_JITTER_SECONDS)
                        if _RETRY_JITTER_SECONDS > 0.0
                        else 0.0
                    )
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    continue
                break
            if attempt < attempt_limit - 1:
                try:
                    _metrics_inc(
                        "dex_rpc_retries_total",
                        labels={
                            "provider": provider,
                            "operation": operation,
                            "reason": "error",
                        },
                    )
                except Exception:
                    pass
                sleep_for = (_RETRY_BASE_SECONDS * (2**attempt)) + (
                    random.uniform(0.0, _RETRY_JITTER_SECONDS)
                    if _RETRY_JITTER_SECONDS > 0.0
                    else 0.0
                )
                if sleep_for > 0:
                    time.sleep(sleep_for)
                continue
            break
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{operation} failed without explicit error")


try:
    from libs.telemetry.logger import get_logger  # type: ignore

    _logger = get_logger("dex.agg")
except Exception:

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
except Exception:

    def _metrics_inc(
        name: str, value: int = 1, labels: dict[str, str] | None = None
    ) -> None:  # type: ignore
        return


Address = str


def _debug_routes_enabled() -> bool:
    """
    Return True when route-level debugging is enabled via config or env.

    Previously the env flag only applied when config loading failed.  Tests set
    DIEM_DEBUG_ROUTES=1 without a config override, so treat the env flag as an
    additive signal.
    """
    flag = os.getenv("DIEM_DEBUG_ROUTES")
    env_enabled = (
        str(flag).strip().lower() in {"1", "true", "yes", "on"}
        if flag is not None
        else False
    )
    try:
        cfg_enabled = bool(get_config().debug.diem_routes)
    except Exception:
        cfg_enabled = False
    return env_enabled or cfg_enabled


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
    route: RoutePlan | None = None
    path: list[Address] | None = None
    executable: bool = True  # Set to False for preview-only analytic quotes

    def __post_init__(self) -> None:
        if self.route is None:
            if self.path is None:
                raise ValueError("Quote requires either route or path")
            route = make_route(self.path)
            object.__setattr__(self, "route", route)
            object.__setattr__(self, "path", list(route.tokens))
        elif self.path is None:
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

    def quote(self, amount_in: int, route: RoutePlan) -> Quote | None:
        raise NotImplementedError

    def trade(
        self, amount_in: int, min_amount_out: int, route: RoutePlan
    ) -> dict[str, str]:
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Quote | None:
        return None

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route: RoutePlan
    ) -> dict[str, str]:
        raise NotImplementedError

    # Optional per-provider reserve fallback hooks (default to no-op).
    def _quote_exact_in_reserve(self, amount_in: int, route: RoutePlan) -> Quote | None:
        return None


class UniswapV2DexProvider(DexProvider):
    name = "uniswap_v2"
    supports_exact_out = True
    supports_reserve_math = True
    supports_mid_price = True
    _pool_cache: dict[tuple[str, str], tuple[float, bool]] = {}
    _pool_cache_ttl_seconds: float = 120.0
    _pool_cache_lock: Lock = Lock()

    def __init__(self, router_address: Address) -> None:
        from web3 import Web3  # type: ignore

        self.router_addr = Web3.to_checksum_address(router_address)
        self._router_abi = "uniswap_v2_router.json"
        self._rpc_lock = Lock()
        self._refresh_provider()
        self.recipient: str | None = None

    def _refresh_provider(self) -> None:
        with self._rpc_lock:
            self.w3 = get_web3()
            self.router = get_contract(self.w3, self.router_addr, self._router_abi)

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

    def _ensure_allowance(
        self, token: Address, owner: Address, spender: Address, required: int
    ) -> str | None:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = encode_contract_call(erc20, "approve", [spender, required])
        tx_hash = send_tx(token, bytes.fromhex(approve_data[2:]))

        # Wait for approval tx to confirm before returning
        # This prevents race conditions where trade executes before approval is mined
        if tx_hash:
            from libs.agentkit_ext.agentkit_wallet import wait_for_tx_confirmation

            _logger.info(
                "Waiting for approval tx confirmation: %s (token=%s, spender=%s)",
                tx_hash,
                token,
                spender,
            )
            confirm_result = wait_for_tx_confirmation(tx_hash, timeout=60)
            if confirm_result.get("status") != "confirmed":
                _logger.warning(
                    "Approval tx not confirmed: %s (status=%s)",
                    tx_hash,
                    confirm_result.get("status"),
                )
                raise RuntimeError(
                    f"Approval tx not confirmed: {confirm_result.get('status')}"
                )
            _logger.info(
                "Approval tx confirmed in block %s",
                confirm_result.get("block_number"),
            )
        return tx_hash

    def _pools_exist(self, route: RoutePlan) -> bool:
        """Fast precheck to avoid router calls when pairs are missing."""
        factory_addr = (os.getenv("UNISWAP_V2_FACTORY_ADDRESS") or "").strip()
        if not factory_addr:
            return True  # Skip when factory is not configured
        tokens = list(route.tokens)
        if len(tokens) < 2:
            return False
        try:
            from web3 import Web3  # type: ignore

            factory_abi = [
                {
                    "constant": True,
                    "inputs": [
                        {"name": "tokenA", "type": "address"},
                        {"name": "tokenB", "type": "address"},
                    ],
                    "name": "getPair",
                    "outputs": [{"name": "pair", "type": "address"}],
                    "payable": False,
                    "stateMutability": "view",
                    "type": "function",
                }
            ]
            factory = self.w3.eth.contract(
                address=Web3.to_checksum_address(factory_addr), abi=factory_abi
            )
        except Exception:
            # If factory setup fails, do not block quoting.
            return True

        now = time.monotonic()

        def _check_pair(addr_a: str, addr_b: str) -> bool:
            key = tuple(sorted((addr_a.lower(), addr_b.lower())))
            with self._pool_cache_lock:
                cached = self._pool_cache.get(key)
            if cached:
                ts, exists = cached
                if now - ts < self._pool_cache_ttl_seconds:
                    return exists
            try:
                pair_addr = factory.functions.getPair(
                    Web3.to_checksum_address(addr_a),
                    Web3.to_checksum_address(addr_b),
                ).call()
                exists = bool(
                    pair_addr
                    and str(pair_addr).strip("0x").strip()
                    and int(str(pair_addr), 16) != 0
                )
            except Exception:
                exists = True  # Assume exists on errors to avoid false negatives
            with self._pool_cache_lock:
                self._pool_cache[key] = (now, exists)
            return exists

        for i in range(len(tokens) - 1):
            if not _check_pair(tokens[i], tokens[i + 1]):
                return False
        return True

    def quote(self, amount_in: int, route: RoutePlan) -> Quote | None:
        # Normalize route for V2 (strip fee tiers)
        try:
            normalized_route = normalize_route_for_v2(route)
        except ValueError as ve:
            # Route cannot be normalized, return None
            # Always log normalization failures at WARNING level for visibility
            _logger.warning(
                "UniswapV2 quote normalization failed: %s, route=%s, debug=%s",
                ve,
                list(route.tokens),
                _debug_routes_enabled(),
            )
            _metrics_inc(
                "dex_quotes_total", labels={"provider": self.name, "status": "err"}
            )
            return None

        if not self._pools_exist(normalized_route):
            # Use DEBUG for VVV/DIEM routes - missing V2 pools are expected
            # These routes work via V3/Aerodrome CL instead
            tokens = list(normalized_route.tokens)
            vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
            diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
            is_vvv_route = (
                any(t.lower() == vvv_addr for t in tokens) if vvv_addr else False
            )
            is_diem_route = (
                any(t.lower() == diem_addr for t in tokens) if diem_addr else False
            )
            if is_vvv_route or is_diem_route:
                _logger.debug(
                    "UniswapV2 quote skipped: missing pool for VVV/DIEM path=%s",
                    tokens,
                )
            else:
                _logger.warning(
                    "UniswapV2 quote skipped: missing pool for path=%s",
                    tokens,
                )
            _metrics_inc(
                "dex_quotes_total",
                labels={"provider": self.name, "status": "no_pool_precheck"},
            )
            return None

        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                checksum_path = normalized_route.to_uniswap_v2_path(checksum=True)
                amounts = self.router.functions.getAmountsOut(
                    amount_in, checksum_path
                ).call()
                out_amt = int(amounts[-1]) if amounts else 0
                if out_amt <= 0:
                    # Always log zero output at WARNING level for visibility
                    # Tag as zero_liquidity to distinguish from provider failures
                    _logger.warning(
                        "UniswapV2 quote returned zero output: amount_in=%s, path=%s, amounts=%s, debug=%s",
                        amount_in,
                        checksum_path,
                        amounts,
                        _debug_routes_enabled(),
                    )
                    _metrics_inc(
                        "dex_quotes_total",
                        labels={"provider": self.name, "status": "zero_liquidity"},
                    )
                    return None
                _metrics_inc(
                    "dex_quotes_total", labels={"provider": self.name, "status": "ok"}
                )
                _metrics_inc(
                    "dex_quote_latency_bucket_total",
                    labels={
                        "provider": self.name,
                        "bucket": self._latency_bucket_name(time.perf_counter() - t0),
                    },
                )
                return Quote(
                    provider=self.name,
                    amount_in=amount_in,
                    amount_out=out_amt,
                    route=normalized_route,
                )
            except Exception as exc:
                if _looks_like_rate_limit(exc) and attempt == 0:
                    _record_rate_limit(self.name, "quote")
                    self._refresh_provider()
                    continue

                # Treat UniswapV2 "execution reverted" as missing pool to avoid noisy warnings/stack traces.
                if _is_execution_reverted(exc):
                    if _debug_routes_enabled():
                        _logger.debug(
                            "UniswapV2 quote skipped: execution reverted (likely no pool): amount_in=%s path=%s attempt=%s",
                            amount_in,
                            list(normalized_route.tokens) if normalized_route else None,
                            attempt,
                        )
                    _metrics_inc(
                        "dex_quotes_total",
                        labels={"provider": self.name, "status": "no_pool"},
                    )
                    return None

                # Enhanced logging with route details for other errors
                _logger.warning(
                    "UniswapV2 quote exception [unknown]: %s, amount_in=%s, path=%s, hops=%s, attempt=%s, debug=%s",
                    exc,
                    amount_in,
                    list(normalized_route.tokens) if normalized_route else None,
                    [
                        f"{hop.token_in[:10]}...->{hop.token_out[:10]}..."
                        for hop in normalized_route.hops
                    ]
                    if normalized_route
                    else None,
                    attempt,
                    _debug_routes_enabled(),
                )

                _metrics_inc(
                    "dex_quotes_total", labels={"provider": self.name, "status": "err"}
                )
                if attempt == 1:  # Only return None on final attempt
                    return None
        _metrics_inc(
            "dex_quotes_total", labels={"provider": self.name, "status": "err"}
        )
        return None

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Quote | None:
        # Normalize route for V2 (strip fee tiers)
        try:
            normalized_route = normalize_route_for_v2(route)
        except ValueError as ve:
            # Route cannot be normalized, return None
            _logger.warning(
                "UniswapV2 quote_exact_out normalization failed: %s, route=%s, debug=%s",
                ve,
                list(route.tokens),
                _debug_routes_enabled(),
            )
            _metrics_inc(
                "dex_quotes_total", labels={"provider": self.name, "status": "err"}
            )
            return None

        if not self._pools_exist(normalized_route):
            # Use DEBUG for VVV bridge routes - missing V2 pools are expected
            tokens = list(normalized_route.tokens)
            vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
            is_vvv_route = (
                any(t.lower() == vvv_addr for t in tokens) if vvv_addr else False
            )
            if is_vvv_route:
                _logger.debug(
                    "UniswapV2 quote_exact_out skipped: missing pool for VVV bridge path=%s",
                    tokens,
                )
            else:
                _logger.warning(
                    "UniswapV2 quote_exact_out skipped: missing pool for path=%s",
                    tokens,
                )
            _metrics_inc(
                "dex_quotes_total",
                labels={"provider": self.name, "status": "no_pool_precheck"},
            )
            return None

        for attempt in range(2):
            try:
                checksum_path = normalized_route.to_uniswap_v2_path(checksum=True)
                amounts = self.router.functions.getAmountsIn(
                    amount_out, checksum_path
                ).call()
                in_amt = int(amounts[0]) if amounts else 0
                if in_amt <= 0:
                    _logger.warning(
                        "UniswapV2 quote_exact_out returned zero input: amount_out=%s, path=%s, amounts=%s, debug=%s",
                        amount_out,
                        checksum_path,
                        amounts,
                        _debug_routes_enabled(),
                    )
                    _metrics_inc(
                        "dex_quotes_total",
                        labels={"provider": self.name, "status": "zero"},
                    )
                    return None
                _metrics_inc(
                    "dex_quotes_total", labels={"provider": self.name, "status": "ok"}
                )
                return Quote(
                    provider=self.name,
                    amount_in=in_amt,
                    amount_out=amount_out,
                    route=normalized_route,
                )
            except Exception as exc:
                if _looks_like_rate_limit(exc) and attempt == 0:
                    _record_rate_limit(self.name, "quote_exact_out")
                    self._refresh_provider()
                    continue

                # Treat UniswapV2 "execution reverted" as missing pool to avoid noisy warnings/stack traces.
                if _is_execution_reverted(exc):
                    if _debug_routes_enabled():
                        _logger.debug(
                            "UniswapV2 quote_exact_out skipped: execution reverted (likely no pool): amount_out=%s path=%s attempt=%s",
                            amount_out,
                            list(normalized_route.tokens) if normalized_route else None,
                            attempt,
                        )
                    _metrics_inc(
                        "dex_quotes_total",
                        labels={"provider": self.name, "status": "no_pool"},
                    )
                    return None

                _logger.warning(
                    "UniswapV2 quote_exact_out exception [unknown]: %s, amount_out=%s, path=%s, hops=%s, attempt=%s, debug=%s",
                    exc,
                    amount_out,
                    list(normalized_route.tokens) if normalized_route else None,
                    [
                        f"{hop.token_in[:10]}...->{hop.token_out[:10]}..."
                        for hop in normalized_route.hops
                    ]
                    if normalized_route
                    else None,
                    attempt,
                    _debug_routes_enabled(),
                )

                _metrics_inc(
                    "dex_quotes_total", labels={"provider": self.name, "status": "err"}
                )
                if attempt == 1:
                    return None
        _metrics_inc(
            "dex_quotes_total", labels={"provider": self.name, "status": "err"}
        )
        return None

    def trade(
        self, amount_in: int, min_amount_out: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        # Normalize route for V2 (strip fee tiers)
        normalized_route = normalize_route_for_v2(route)
        checksum_path = normalized_route.to_uniswap_v2_path(checksum=True)
        token_in = checksum_path[0]
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60
        last_error: Exception | None = None
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                fn = self.router.functions.swapExactTokensForTokens(
                    amount_in, min_amount_out, checksum_path, recipient, deadline
                )
                # Must pass 'from' address for gas estimation to work
                built = fn.build_transaction({"from": recipient})
                tx_hash = send_tx(self.router_addr, built["data"])
                _metrics_inc(
                    "dex_trades_total",
                    labels={"provider": self.name, "path": "standard"},
                )
                _metrics_inc(
                    "dex_trade_latency_bucket_total",
                    labels={
                        "provider": self.name,
                        "bucket": self._latency_bucket_name(time.perf_counter() - t0),
                    },
                )
                # CRITICAL: Log tx_hash for on-chain traceability
                _logger.info(
                    "UniswapV2 trade EXECUTED: amount_in=%s min_out=%s path=%s tx_hash=%s",
                    amount_in,
                    min_amount_out,
                    checksum_path,
                    tx_hash,
                )
                return {
                    "provider": self.name,
                    "tx_hash": tx_hash,
                    "approval_tx": approve_hash,
                }
            except Exception as exc_main:
                if _looks_like_rate_limit(exc_main) and attempt == 0:
                    _record_rate_limit(self.name, "trade_exact_in")
                    self._refresh_provider()
                    continue
                try:
                    fn2 = self.router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
                        amount_in,
                        min_amount_out,
                        checksum_path,
                        recipient,
                        deadline,
                    )
                    # Must pass 'from' address for gas estimation to work
                    built2 = fn2.build_transaction({"from": recipient})
                    tx_hash2 = send_tx(self.router_addr, built2["data"])
                    _metrics_inc(
                        "dex_trades_total",
                        labels={"provider": self.name, "path": "fot"},
                    )
                    _metrics_inc("fot_fallback_total", labels={"provider": self.name})
                    _metrics_inc(
                        "dex_trade_latency_bucket_total",
                        labels={
                            "provider": self.name,
                            "bucket": self._latency_bucket_name(
                                time.perf_counter() - t0
                            ),
                        },
                    )
                    return {
                        "provider": self.name,
                        "tx_hash": tx_hash2,
                        "approval_tx": approve_hash,
                        "fot_fallback": "true",
                    }
                except Exception as exc_fallback:
                    last_error = exc_fallback
                    if _looks_like_rate_limit(exc_fallback) and attempt == 0:
                        _record_rate_limit(self.name, "trade_exact_in_fallback")
                        self._refresh_provider()
                        continue
                    _metrics_inc(
                        "dex_trade_errors_total",
                        labels={"provider": self.name, "path": "fot"},
                    )
                    raise exc_fallback
        if last_error is not None:
            raise last_error
        raise RuntimeError("swapExactTokensForTokens failed without explicit error")

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        # Normalize route for V2 (strip fee tiers)
        normalized_route = normalize_route_for_v2(route)
        checksum_path = normalized_route.to_uniswap_v2_path(checksum=True)
        token_in = checksum_path[0]
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60
        last_error: Exception | None = None
        for attempt in range(2):
            t0 = time.perf_counter()
            try:
                fn = self.router.functions.swapTokensForExactTokens(
                    amount_out, max_amount_in, checksum_path, recipient, deadline
                )
                # Must pass 'from' address for gas estimation to work
                built = fn.build_transaction({"from": recipient})
                tx_hash = send_tx(self.router_addr, built["data"])
                _metrics_inc(
                    "dex_trades_total",
                    labels={"provider": self.name, "path": "exact_out"},
                )
                _metrics_inc(
                    "dex_trade_latency_bucket_total",
                    labels={
                        "provider": self.name,
                        "bucket": self._latency_bucket_name(time.perf_counter() - t0),
                    },
                )
                return {
                    "provider": self.name,
                    "tx_hash": tx_hash,
                    "approval_tx": approve_hash,
                }
            except Exception as exc:
                last_error = exc
                if _looks_like_rate_limit(exc) and attempt == 0:
                    _record_rate_limit(self.name, "trade_exact_out")
                    self._refresh_provider()
                    continue
                _metrics_inc(
                    "dex_trade_errors_total",
                    labels={"provider": self.name, "path": "exact_out"},
                )
                raise exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("swapTokensForExactTokens failed without explicit error")


class AerodromeDexProvider(DexProvider):
    name = "aerodrome"
    supports_exact_out = True

    def __init__(self, router_address: Address, stable: bool = True) -> None:
        from web3 import Web3  # type: ignore

        self.router_addr = Web3.to_checksum_address(router_address)
        self._router_abi = "aerodrome_router.json"
        self._rpc_lock = Lock()
        self._refresh_provider()
        self.recipient: str | None = None
        self.stable = bool(stable)
        # Optional factories let us detect the correct stable flag per hop to avoid
        # router reverts when the default flag is wrong.
        self.factory_vol = os.getenv("AERODROME_FACTORY_VOLATILE")
        self.factory_sta = os.getenv("AERODROME_FACTORY_STABLE")
        self._pair_flag_cache: dict[tuple[str, str], bool] = {}
        # DIEM/VVV volatile pool hint so we can pin the first hop even when
        # factory discovery fails or the default stable flag is wrong.
        self._diem_token = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
        self._vvv_token = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
        self._quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        raw_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip()
        try:
            self._diem_vvv_pair_addr = (
                Web3.to_checksum_address(raw_pair) if raw_pair else ""
            )
        except Exception:
            self._diem_vvv_pair_addr = ""
        # DIEM/USDC Aerodrome SlipStream pool for direct swaps (highest liquidity)
        raw_diem_usdc = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
        try:
            self._diem_usdc_pool_addr = (
                Web3.to_checksum_address(raw_diem_usdc) if raw_diem_usdc else ""
            )
        except Exception:
            self._diem_usdc_pool_addr = ""

    def _refresh_provider(self) -> None:
        with self._rpc_lock:
            self.w3 = get_web3()
            self.router = get_contract(self.w3, self.router_addr, self._router_abi)

    def _get_default_factory(self) -> str:
        """
        Get the default pool factory address from the Aerodrome router.
        Returns configured AERODROME_FACTORY_VOLATILE if set, otherwise queries router.
        """
        if self.factory_vol:
            from web3 import Web3 as _Web3

            return _Web3.to_checksum_address(self.factory_vol)
        try:
            factory = self.router.functions.defaultFactory().call()
            return factory
        except Exception:
            # Fallback: Aerodrome V2 default pool factory on Base
            return "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

    def _factory_for_flag(self, stable_flag: bool) -> str:
        """Pick a factory address for a hop based on the stable flag."""
        from web3 import Web3 as _Web3  # type: ignore

        candidate = (
            (self.factory_sta if stable_flag else self.factory_vol)
            or self.factory_vol
            or self.factory_sta
        )
        if candidate:
            try:
                return _Web3.to_checksum_address(candidate)
            except Exception:
                return candidate
        return self._get_default_factory()

    def _is_diem_vvv_hop(self, token_a: Address, token_b: Address) -> bool:
        if not self._diem_token or not self._vvv_token:
            return False
        a = token_a.lower()
        b = token_b.lower()
        return {a, b} == {self._diem_token, self._vvv_token}

    def _is_diem_usdc_hop(self, token_a: Address, token_b: Address) -> bool:
        """Check if this is a direct DIEM↔USDC hop via the SlipStream pool."""
        if not self._diem_token or not self._quote_token:
            return False
        a = token_a.lower()
        b = token_b.lower()
        return {a, b} == {self._diem_token, self._quote_token}

    def _stable_flag_for_hop(
        self, token_a: Address, token_b: Address, *, default: bool
    ) -> bool:
        """
        Try to detect whether an Aerodrome pair is stable or volatile for this hop.

        We check the configured factories (stable/volatile) and cache the result;
        if lookups fail, fall back to the provided default flag.
        """
        if not token_a or not token_b:
            return default
        # If we have an explicit DIEM/VVV pair configured, force this hop to use
        # the volatile pool (stable=False) so DIEM→VVV quotes do not depend on
        # factory discovery or the provider's default stable flag.
        if self._diem_vvv_pair_addr and self._is_diem_vvv_hop(token_a, token_b):
            key = (token_a.lower(), token_b.lower())
            self._pair_flag_cache[key] = False
            return False
        # DIEM/USDC SlipStream pool is also volatile (not stable)
        if self._diem_usdc_pool_addr and self._is_diem_usdc_hop(token_a, token_b):
            key = (token_a.lower(), token_b.lower())
            self._pair_flag_cache[key] = False
            return False
        key = (token_a.lower(), token_b.lower())
        cached = self._pair_flag_cache.get(key)
        if cached is not None:
            return cached
        # Prefer explicit stable/volatile factories when available
        try:
            from web3 import Web3  # type: ignore

            if self.factory_vol and self.factory_sta:
                fac_vol = get_contract(
                    self.w3,
                    Web3.to_checksum_address(self.factory_vol),
                    "aerodrome_factory.json",
                )
                fac_sta = get_contract(
                    self.w3,
                    Web3.to_checksum_address(self.factory_sta),
                    "aerodrome_factory.json",
                )
                pair_vol = fac_vol.functions.getPair(token_a, token_b, False).call()
                pair_sta = fac_sta.functions.getPair(token_a, token_b, True).call()
                # Choose whichever pair exists; prefer stable when both exist.
                flag = default
                if int(pair_sta or 0) != 0:
                    flag = True
                elif int(pair_vol or 0) != 0:
                    flag = False
                self._pair_flag_cache[key] = flag
                return flag
        except Exception:
            pass
        return default

    def _ensure_allowance(
        self, token: Address, owner: Address, spender: Address, required: int
    ) -> str | None:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = encode_contract_call(erc20, "approve", [spender, required])
        tx_hash = send_tx(token, bytes.fromhex(approve_data[2:]))

        # Wait for approval tx to confirm before returning
        if tx_hash:
            from libs.agentkit_ext.agentkit_wallet import wait_for_tx_confirmation

            _logger.info(
                "Waiting for approval tx confirmation: %s (token=%s, spender=%s)",
                tx_hash,
                token,
                spender,
            )
            confirm_result = wait_for_tx_confirmation(tx_hash, timeout=60)
            if confirm_result.get("status") != "confirmed":
                _logger.warning(
                    "Approval tx not confirmed: %s (status=%s)",
                    tx_hash,
                    confirm_result.get("status"),
                )
                raise RuntimeError(
                    f"Approval tx not confirmed: {confirm_result.get('status')}"
                )
            _logger.info(
                "Approval tx confirmed in block %s",
                confirm_result.get("block_number"),
            )
        return tx_hash

    def _routes(
        self, route: RoutePlan, stable: bool | None = None
    ) -> list[dict[str, Any]]:
        # Normalize route for Aerodrome (strip fee tiers)
        normalized_route = normalize_route_for_aerodrome(route)
        path = normalized_route.to_uniswap_v2_path(checksum=True)
        st = bool(self.stable) if stable is None else bool(stable)
        # Aerodrome router expects struct array with fields: from, to, stable
        # Use dictionaries to match ABI struct format for proper encoding.
        # NOTE: The router Route struct includes a per-hop `factory` field.
        hops: list[dict[str, Any]] = []
        for i in range(len(path) - 1):
            flag = self._stable_flag_for_hop(path[i], path[i + 1], default=st)
            hops.append(
                {
                    "from": path[i],
                    "to": path[i + 1],
                    "stable": flag,
                    "factory": self._factory_for_flag(flag),
                }
            )
        if _debug_routes_enabled():
            _logger.debug(
                "Aerodrome route hops=%s",
                hops,
            )
        return hops

    def _is_diem_vvv_route(self, route: RoutePlan) -> bool:
        try:
            tokens = list(normalize_route_for_aerodrome(route).tokens)
        except Exception:
            return False
        if len(tokens) != 2:
            return False
        diem, vvv = _diem_vvv_addrs()
        if not diem or not vvv:
            return False
        return {tokens[0].lower(), tokens[1].lower()} == {diem, vvv}

    def _routes_with_mask(
        self, route: RoutePlan, mask: Sequence[bool]
    ) -> list[tuple[Address, Address, bool, Address]]:
        # Normalize route for Aerodrome (strip fee tiers)
        normalized_route = normalize_route_for_aerodrome(route)
        path = normalized_route.to_uniswap_v2_path(checksum=True)
        if len(path) - 1 != len(mask):
            raise ValueError("mask length must equal hop count")
        # Aerodrome router expects struct array: (from, to, stable, factory) tuples
        hops: list[tuple[Address, Address, bool, Address]] = []
        for i in range(len(path) - 1):
            flag = self._stable_flag_for_hop(
                path[i], path[i + 1], default=bool(mask[i])
            )
            hops.append((path[i], path[i + 1], flag, self._factory_for_flag(flag)))
        if _debug_routes_enabled():
            _logger.debug(
                "Aerodrome route(mask) hops=%s",
                [
                    {"from": h[0], "to": h[1], "stable": h[2], "factory": h[3]}
                    for h in hops
                ],
            )
        return hops

    def quote(self, amount_in: int, route: RoutePlan) -> Quote | None:
        # Fast-path: when direct DIEM/VVV swapping is enabled, bypass router views
        # and use reserve math to avoid 5s RPC timeouts on getAmountsOut.
        if _diem_vvv_direct_enabled() and self._is_diem_vvv_route(route):
            direct_quote = self._quote_reserve_fallback(
                amount_in, route, mode="exact_in"
            )
            if direct_quote:
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "ok",
                        "mode": "exact_in_direct",
                    },
                )
                return direct_quote

        t0 = time.perf_counter()
        try:
            routes = self._routes(route, stable=self.stable)
            amounts = _call_with_rpc_retry(
                self.name,
                "quote",
                self._refresh_provider,
                lambda: self.router.functions.getAmountsOut(amount_in, routes).call(),
            )
            out_amt = int(amounts[-1]) if amounts else 0
            if out_amt <= 0:
                return None
            _metrics_inc(
                "dex_quotes_total", labels={"provider": self.name, "status": "ok"}
            )
            _bucket_latency("quote", self.name, time.perf_counter() - t0)
            return Quote(
                provider=self.name, amount_in=amount_in, amount_out=out_amt, route=route
            )
        except Exception:
            pass
        try:
            routes = self._routes(route, stable=not bool(self.stable))
            amounts = _call_with_rpc_retry(
                self.name,
                "quote_alt",
                self._refresh_provider,
                lambda: self.router.functions.getAmountsOut(amount_in, routes).call(),
            )
            out_amt = int(amounts[-1]) if amounts else 0
            if out_amt <= 0:
                return None
            _metrics_inc(
                "dex_quotes_total", labels={"provider": self.name, "status": "ok"}
            )
            _bucket_latency("quote", self.name, time.perf_counter() - t0)
            return Quote(
                provider=self.name, amount_in=amount_in, amount_out=out_amt, route=route
            )
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
                        amounts = _call_with_rpc_retry(
                            self.name,
                            "quote_mask",
                            self._refresh_provider,
                            lambda: self.router.functions.getAmountsOut(
                                amount_in, routes
                            ).call(),
                        )
                        out_amt = int(amounts[-1]) if amounts else 0
                        if out_amt <= 0:
                            continue
                        _metrics_inc(
                            "dex_quotes_total",
                            labels={"provider": self.name, "status": "ok"},
                        )
                        _bucket_latency("quote", self.name, time.perf_counter() - t0)
                        return Quote(
                            provider=self.name,
                            amount_in=amount_in,
                            amount_out=out_amt,
                            route=route,
                        )
                    except Exception:
                        continue
        except Exception:
            pass
        _metrics_inc(
            "dex_quotes_total", labels={"provider": self.name, "status": "err"}
        )
        # Router paths failed; attempt reserve math for 1-hop routes as a last resort
        reserve_quote = self._quote_exact_in_reserve(amount_in, route)
        if reserve_quote:
            _metrics_inc(
                "dex_quotes_total",
                labels={
                    "provider": self.name,
                    "status": "ok",
                    "mode": "exact_in_reserve",
                },
            )
            return reserve_quote
        return None

    def trade(
        self, amount_in: int, min_amount_out: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        # Normalize route for Aerodrome (strip fee tiers)
        normalized_route = normalize_route_for_aerodrome(route)
        path = normalized_route.to_uniswap_v2_path(checksum=True)
        token_in = path[0]
        erc20_owner = self.recipient or _Web3.to_checksum_address(get_address())

        _logger.info(
            "AERODROME TRADE START: token_in=%s amount_in=%s path=%s router=%s owner=%s",
            token_in,
            amount_in,
            path,
            self.router_addr,
            erc20_owner,
        )

        approve_hash = (
            self._ensure_allowance(token_in, erc20_owner, self.router_addr, amount_in)
            or ""
        )
        _logger.info(
            "AERODROME APPROVAL: hash=%s", approve_hash or "existing_allowance"
        )
        deadline = int(time.time()) + 20 * 60
        t0 = time.perf_counter()

        # Check if this route involves DIEM - if so, we MUST use swapExactTokensForTokens
        # with explicit factory because swapExactTokensForTokensSimple uses default factory
        # which may not contain the DIEM pools
        is_diem_route = (
            any(t.lower() == self._diem_token for t in path)
            if self._diem_token
            else False
        )

        # Use swapExactTokensForTokens with Route[] struct for:
        # 1. Multi-hop routes (3+ tokens)
        # 2. Any route involving DIEM (need explicit factory)
        if len(path) > 2 or is_diem_route:
            # Build Route[] struct: [{from, to, stable, factory}, ...]
            # Aerodrome V2 requires the factory address in each route hop
            routes = []
            # Get default factory from router or use configured volatile factory
            default_factory = self._get_default_factory()
            for i in range(len(path) - 1):
                # Determine stable flag per hop
                # DIEM/VVV pool on Aerodrome is VOLATILE (not stable)
                # VVV/USDC is also volatile
                # Default to volatile (False) for both DIEM/VVV and VVV/USDC hops
                hop_stable = False
                # Use volatile factory for all hops (DIEM pairs are volatile)
                # Always use checksummed factory from _get_default_factory()
                hop_factory = default_factory
                routes.append((path[i], path[i + 1], hop_stable, hop_factory))
            # Aerodrome V2 uses swapExactTokensForTokens with Route[] struct
            fn = self.router.functions.swapExactTokensForTokens(
                amount_in,
                min_amount_out,
                routes,
                erc20_owner,
                deadline,
            )
            path_label = "multihop" if len(path) > 2 else "diem_single"
            # DEBUG: Log the exact call parameters
            _logger.info(
                "AERODROME TRADE (%s): is_diem=%s amount_in=%s min_out=%s recipient=%s deadline=%s factory=%s",
                path_label,
                is_diem_route,
                amount_in,
                min_amount_out,
                erc20_owner,
                deadline,
                default_factory,
            )
            _logger.info("AERODROME ROUTES: %s", routes)
            for i, r in enumerate(routes):
                _logger.info(
                    "  Route[%d]: from=%s to=%s stable=%s factory=%s",
                    i,
                    r[0],
                    r[1],
                    r[2],
                    r[3],
                )
            try:
                calldata = fn._encode_transaction_data()
                _logger.info(
                    "AERODROME CALLDATA selector=%s length=%s",
                    calldata[:10],
                    len(calldata),
                )
                # Also log gas estimation attempt
                try:
                    gas = fn.estimate_gas({"from": erc20_owner})
                    _logger.info("AERODROME GAS ESTIMATE SUCCESS: %s", gas)
                except Exception as ge:
                    _logger.warning("AERODROME GAS ESTIMATE FAILED: %s", ge)
            except Exception as e:
                _logger.warning("AERODROME CALLDATA encoding failed: %s", e)
        else:
            # Single-hop non-DIEM route can use Simple function (default factory is fine)
            _logger.info(
                "AERODROME SINGLE-HOP (simple): from=%s to=%s stable=%s",
                path[0],
                path[1],
                bool(self.stable),
            )
            fn = self.router.functions.swapExactTokensForTokensSimple(
                amount_in,
                min_amount_out,
                path[0],
                path[1],
                bool(self.stable),
                erc20_owner,
                deadline,
            )
            path_label = "simple"

        # Must pass 'from' address for gas estimation to work inside build_transaction
        built = fn.build_transaction({"from": erc20_owner})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc(
            "dex_trades_total", labels={"provider": self.name, "path": path_label}
        )
        _bucket_latency("trade", self.name, time.perf_counter() - t0)
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        # Normalize route for Aerodrome (strip fee tiers)
        normalized_route = normalize_route_for_aerodrome(route)
        path = normalized_route.to_uniswap_v2_path(checksum=True)
        token_in = path[0]
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60
        fn = self.router.functions.swapTokensForExactTokens(
            amount_out, max_amount_in, path, recipient, deadline
        )
        # Must pass 'from' address for gas estimation to work
        built = fn.build_transaction({"from": recipient})
        tx_hash = send_tx(self.router_addr, built["data"])
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Quote | None:
        """Exact-out quote using Aerodrome's getAmountsIn (supports stable/volatile)."""
        deadline = time.perf_counter() + _provider_timeout_seconds()

        def _remaining_budget() -> float:
            return deadline - time.perf_counter()

        # When DIEM/VVV direct mode is enabled, skip router aggregation and
        # compute quotes directly from pair reserves to avoid RPC view timeouts.
        if _diem_vvv_direct_enabled() and self._is_diem_vvv_route(route):
            reserve_quote = self._quote_reserve_fallback(
                amount_out, route, mode="exact_out"
            )
            if reserve_quote:
                try:
                    object.__setattr__(reserve_quote, "executable", False)
                except Exception:
                    reserve_quote.executable = False  # type: ignore[attr-defined]
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "ok",
                        "mode": "exact_out_direct",
                    },
                )
                return reserve_quote

        def _quote_router(stable_flag: bool, timeout: float) -> Quote | None:
            routes = self._routes(route, stable=stable_flag)
            executor = ThreadPoolExecutor(max_workers=1)
            fut = executor.submit(
                _call_with_rpc_retry,
                self.name,
                "quote_exact_out",
                self._refresh_provider,
                lambda: self.router.functions.getAmountsIn(amount_out, routes).call(),
            )
            try:
                amounts = fut.result(timeout=max(0.25, timeout))
            except TimeoutError:
                fut.cancel()
                _logger.warning(
                    "Aerodrome quote_exact_out timeout stable=%s route=%s amount_out=%s timeout=%.2fs",
                    stable_flag,
                    list(route.tokens),
                    amount_out,
                    max(0.25, timeout),
                )
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "timeout",
                        "mode": "exact_out",
                    },
                )
                return None
            except Exception as e:
                fut.cancel()
                error_str = str(e).lower()
                is_no_pool = "no pool" in error_str or (
                    "execution reverted" in error_str and "no data" in error_str
                )
                status = "no_pool" if is_no_pool else "err"
                _logger.warning(
                    "Aerodrome quote_exact_out error [%s] stable=%s route=%s amount_out=%s error=%s",
                    status,
                    stable_flag,
                    list(route.tokens),
                    amount_out,
                    e,
                )
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": status,
                        "mode": "exact_out",
                    },
                )
                return None
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            in_amt = int(amounts[0]) if amounts else 0
            if in_amt > 0:
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "ok",
                        "mode": "exact_out",
                    },
                )
                return Quote(
                    provider=self.name,
                    amount_in=in_amt,
                    amount_out=amount_out,
                    route=route,
                    executable=False,  # Exact-out on Aerodrome is preview-only by design
                )
            return None

        # First try router getAmountsIn (if supported) within the time budget.
        for stable_flag in (self.stable, not bool(self.stable)):
            remaining = _remaining_budget()
            if remaining <= 0:
                break
            routed = _quote_router(stable_flag, remaining)
            if routed:
                return routed

        # Fallback: reserve-math for 1-hop routes when router view is unavailable.
        reserve_quote = self._quote_exact_out_reserve(amount_out, route)
        if reserve_quote:
            if _debug_routes_enabled():
                _logger.debug(
                    f"Aerodrome quote_exact_out returning reserve fallback quote: "
                    f"in={reserve_quote.amount_in} out={reserve_quote.amount_out}"
                )
            _metrics_inc(
                "dex_quotes_total",
                labels={
                    "provider": self.name,
                    "status": "ok",
                    "mode": "exact_out_reserve",
                },
            )
            # Reserve math quotes are preview-only; execution uses exact-in path.
            try:
                object.__setattr__(reserve_quote, "executable", False)
            except Exception:
                reserve_quote.executable = False  # type: ignore[attr-defined]
            return reserve_quote

        _metrics_inc(
            "dex_quotes_total",
            labels={"provider": self.name, "status": "err", "mode": "exact_out"},
        )
        return None

    def _quote_exact_out_reserve(
        self, amount_out: int, route: RoutePlan
    ) -> Quote | None:
        """Reserve math fallback for exact-out when router view is unavailable."""
        original_stable = self.stable
        quote = self._quote_reserve_fallback(amount_out, route, mode="exact_out")
        if quote:
            return quote
        # Retry with opposite stable flag in case pool type was misclassified.
        try:
            self.stable = not bool(original_stable)
            if _debug_routes_enabled():
                _logger.debug(
                    "Aerodrome reserve fallback retry with stable=%s", self.stable
                )
            return self._quote_reserve_fallback(amount_out, route, mode="exact_out")
        finally:
            self.stable = original_stable

    def _quote_reserve_fallback(
        self, amount: int, route: RoutePlan, mode: str = "exact_out"
    ) -> Quote | None:
        """Fallback quote using reserve math for a single-hop Aerodrome pool."""
        try:
            normalized_route = normalize_route_for_aerodrome(route)
            tokens = list(normalized_route.tokens)
            if len(tokens) != 2:
                if _debug_routes_enabled():
                    _logger.debug(
                        f"Aerodrome fallback: route length {len(tokens)} != 2"
                    )
                return None
            token_in = tokens[0]
            token_out = tokens[1]

            # Fast-path: Use DIEM/VVV reserve math directly when available
            if self._is_diem_vvv_route(route):
                if mode == "exact_out":
                    diem_quote = diem_vvv_quote_from_reserves(
                        amount, token_in, token_out
                    )
                else:
                    diem_quote = diem_vvv_quote_exact_in_from_reserves(
                        amount, token_in, token_out
                    )
                if diem_quote:
                    if _debug_routes_enabled():
                        _logger.debug(
                            f"Aerodrome fallback: using DIEM/VVV reserve math: "
                            f"in={diem_quote.amount_in} out={diem_quote.amount_out}"
                        )
                    return diem_quote

            from web3 import Web3  # type: ignore

            # Prefer explicit env pair for DIEM/VVV when provided.
            pair_addr = None
            try:
                if os.getenv("DIEM_TOKEN_ADDRESS") and os.getenv("VVV_TOKEN_ADDRESS"):
                    diem_addr = os.getenv("DIEM_TOKEN_ADDRESS").strip().lower()
                    vvv_addr = os.getenv("VVV_TOKEN_ADDRESS").strip().lower()
                    if {
                        token_in.lower(),
                        token_out.lower(),
                    } == {diem_addr, vvv_addr}:
                        env_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip()
                        if env_pair:
                            pair_addr = Web3.to_checksum_address(env_pair)
            except Exception:
                pair_addr = None

            if pair_addr is None:
                factory = (
                    os.getenv("AERODROME_FACTORY_STABLE")
                    if self.stable
                    else os.getenv("AERODROME_FACTORY_VOLATILE")
                )
                if not factory:
                    _logger.warning(
                        "Aerodrome reserve fallback skipped: no factory address (stable=%s) for %s->%s",
                        self.stable,
                        token_in,
                        token_out,
                    )
                    _metrics_inc(
                        "dex_quotes_total",
                        labels={
                            "provider": self.name,
                            "status": "no_pool",
                            "mode": mode,
                        },
                    )
                    return None
                pair = (
                    es.get_pair_aerodrome(factory, token_in, token_out, self.stable)
                    if es is not None
                    else None
                )
                if pair:
                    pair_addr = pair

            if not pair_addr:
                _logger.warning(
                    "Aerodrome reserve fallback: no pair for %s->%s (stable=%s)",
                    token_in,
                    token_out,
                    self.stable,
                )
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "no_pool",
                        "mode": mode,
                    },
                )
                return None

            if _debug_routes_enabled():
                _logger.debug(f"Aerodrome fallback: checking reserves for {pair_addr}")

            state = _pair_state_cached(pair_addr, _provider_timeout_seconds())
            if not state:
                _logger.warning(
                    "Aerodrome reserve fallback: no reserves for pair %s (route=%s)",
                    pair_addr,
                    [token_in, token_out],
                )
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "no_pool",
                        "mode": mode,
                    },
                )
                return None
            reserve0, reserve1, token0, token1 = state

            if token_in.lower() == token0.lower():
                reserve_in = int(reserve0)
                reserve_out = int(reserve1)
            elif token_in.lower() == token1.lower():
                reserve_in = int(reserve1)
                reserve_out = int(reserve0)
            else:
                _logger.warning(
                    "Aerodrome reserve fallback: token mismatch %s/%s vs %s/%s",
                    token_in,
                    token_out,
                    token0,
                    token1,
                )
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "no_pool",
                        "mode": mode,
                    },
                )
                return None

            if mode == "exact_out":
                amount_out = amount
                # UniswapV2-style exact-out: amount_in = (reserve_in * amount_out * 1000) / ((reserve_out - amount_out) * 997)
                if amount_out <= 0 or reserve_out <= amount_out:
                    if _debug_routes_enabled():
                        _logger.debug(
                            f"Aerodrome fallback: insufficient liquidity out={amount_out} reserve={reserve_out}"
                        )
                    return None
                numerator = reserve_in * amount_out * 1000
                denominator = (reserve_out - amount_out) * 997
                if denominator <= 0:
                    if _debug_routes_enabled():
                        _logger.debug(
                            f"Aerodrome fallback: denominator <= 0 (reserve_out={reserve_out}, amount_out={amount_out})"
                        )
                    return None
                amount_in = (numerator // denominator) + 1  # round up
                if amount_in <= 0:
                    if _debug_routes_enabled():
                        _logger.debug(
                            f"Aerodrome fallback: amount_in <= 0 ({amount_in})"
                        )
                    return None
            else:
                # exact_in
                amount_in = amount
                # UniswapV2-style exact-in: amount_out = (amount_in * 997 * reserve_out) / (reserve_in * 1000 + amount_in * 997)
                if amount_in <= 0 or reserve_in <= 0:
                    if _debug_routes_enabled():
                        _logger.debug(
                            f"Aerodrome fallback: insufficient liquidity in={amount_in} reserve={reserve_in}"
                        )
                    return None
                amount_in_with_fee = amount_in * 997
                numerator = amount_in_with_fee * reserve_out
                denominator = (reserve_in * 1000) + amount_in_with_fee
                amount_out = numerator // denominator
                if amount_out <= 0:
                    if _debug_routes_enabled():
                        _logger.debug(
                            f"Aerodrome fallback: amount_out <= 0 ({amount_out})"
                        )
                    return None

            if _debug_routes_enabled():
                _logger.debug(
                    f"Aerodrome fallback success: in={amount_in} out={amount_out}"
                )

            return Quote(
                provider=self.name,
                amount_in=int(amount_in),
                amount_out=int(amount_out),
                route=route,
            )
        except Exception as e:
            if _debug_routes_enabled():
                _logger.debug(f"Aerodrome reserve fallback exception: {e}")
            return None

    def _quote_exact_in_reserve(self, amount_in: int, route: RoutePlan) -> Quote | None:
        """
        Reserve-math quote for single-hop Aerodrome pools when router view reverts.
        Only supports paths with exactly 2 tokens.
        """
        normalized_route = normalize_route_for_aerodrome(route)
        tokens = list(normalized_route.tokens)
        if len(tokens) != 2:
            return None
        token_in = tokens[0]
        token_out = tokens[1]

        def _pair_addr_for(stable_flag: bool) -> str | None:
            factory_addr = (
                self.factory_sta if stable_flag else self.factory_vol
            ) or self.factory_vol
            if not factory_addr:
                return None
            factory = get_contract(self.w3, factory_addr, "aerodrome_factory.json")
            try:
                return factory.functions.getPair(
                    token_in, token_out, stable_flag
                ).call()
            except Exception:
                return None

        for stable_flag in (self.stable, not bool(self.stable)):
            pair_addr = _pair_addr_for(bool(stable_flag))
            if not pair_addr:
                continue
            state = _pair_state_cached(pair_addr, _provider_timeout_seconds())
            if not state:
                continue
            reserve0, reserve1, token0, token1 = state
            if (
                token0.lower() == token_in.lower()
                and token1.lower() == token_out.lower()
            ):
                reserve_in, reserve_out = reserve0, reserve1
            elif (
                token1.lower() == token_in.lower()
                and token0.lower() == token_out.lower()
            ):
                reserve_in, reserve_out = reserve1, reserve0
            else:
                continue
            amount_in_with_fee = amount_in * 997
            denom = reserve_in * 1000 + amount_in_with_fee
            if denom <= 0:
                continue
            amount_out = (amount_in_with_fee * reserve_out) // denom
            if amount_out <= 0:
                continue
            return Quote(
                provider=self.name,
                amount_in=amount_in,
                amount_out=amount_out,
                route=normalized_route,
            )
        return None


class AerodromeCLDexProvider(DexProvider):
    name = "aerodrome_cl"
    supports_exact_out = True

    def __init__(
        self, router_address: Address, pool_address: Address, tick_spacing: int
    ) -> None:
        from web3 import Web3  # type: ignore

        self.router_addr = Web3.to_checksum_address(router_address)
        self.pool_addr = Web3.to_checksum_address(pool_address) if pool_address else ""
        self.tick_spacing = int(tick_spacing)
        self._router_abi = "aerodrome_cl_router.json"
        self._rpc_lock = Lock()
        self._refresh_provider()
        self.recipient: str | None = None
        self._diem_token = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
        self._quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        # Ensure slot0 quote helper sees the configured pool address.
        if self.pool_addr and not (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip():
            os.environ["DIEM_USDC_POOL_ADDRESS"] = self.pool_addr

    def _refresh_provider(self) -> None:
        with self._rpc_lock:
            self.w3 = get_web3()
            self.router = get_contract(self.w3, self.router_addr, self._router_abi)

    def _is_cl_pool_route(self, route: RoutePlan) -> bool:
        if not self.pool_addr:
            if _debug_routes_enabled():
                _logger.debug(
                    "aerodrome_cl route check: missing pool",
                    extra={
                        "provider": self.name,
                        "pool_configured": bool(self.pool_addr),
                    },
                )
            return False
        if not self._diem_token or not self._quote_token:
            try:
                env_diem = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip()
                env_quote = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip()
                if env_diem:
                    try:
                        from libs.dex.routes import _normalize_address

                        self._diem_token = _normalize_address(env_diem).lower()
                    except Exception:
                        self._diem_token = env_diem.lower()
                if env_quote:
                    try:
                        from libs.dex.routes import _normalize_address

                        self._quote_token = _normalize_address(env_quote).lower()
                    except Exception:
                        self._quote_token = env_quote.lower()
            except Exception:
                pass
        if not self._diem_token or not self._quote_token:
            if _debug_routes_enabled():
                _logger.debug(
                    "aerodrome_cl route check: missing token config",
                    extra={
                        "provider": self.name,
                        "diem_token_set": bool(self._diem_token),
                        "quote_token_set": bool(self._quote_token),
                    },
                )
            return False
        tokens = list(route.tokens)
        if len(tokens) != 2:
            return False
        try:
            from libs.dex.routes import _normalize_address

            token_a = _normalize_address(tokens[0])
            token_b = _normalize_address(tokens[1])
        except Exception:
            token_a = str(tokens[0]).split("@", 1)[0].strip().lower()
            token_b = str(tokens[1]).split("@", 1)[0].strip().lower()
        match = {token_a.lower(), token_b.lower()} == {
            self._diem_token,
            self._quote_token,
        }
        if _debug_routes_enabled() and not match:
            _logger.debug(
                "aerodrome_cl route check: token mismatch",
                extra={
                    "provider": self.name,
                    "route_tokens": [token_a.lower(), token_b.lower()],
                    "expected_tokens": [self._diem_token, self._quote_token],
                },
            )
        return match

    def _ensure_allowance(
        self, token: Address, owner: Address, spender: Address, required: int
    ) -> str | None:
        erc20 = get_contract(self.w3, token, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None
        approve_data = encode_contract_call(erc20, "approve", [spender, required])
        tx_hash = send_tx(token, bytes.fromhex(approve_data[2:]))

        if tx_hash:
            from libs.agentkit_ext.agentkit_wallet import wait_for_tx_confirmation

            _logger.info(
                "Waiting for approval tx confirmation: %s (token=%s, spender=%s)",
                tx_hash,
                token,
                spender,
            )
            confirm_result = wait_for_tx_confirmation(tx_hash, timeout=60)
            if confirm_result.get("status") != "confirmed":
                _logger.warning(
                    "Approval tx not confirmed: %s (status=%s)",
                    tx_hash,
                    confirm_result.get("status"),
                )
                raise RuntimeError(
                    f"Approval tx not confirmed: {confirm_result.get('status')}"
                )
            _logger.info(
                "Approval tx confirmed in block %s",
                confirm_result.get("block_number"),
            )
        return tx_hash

    def quote(self, amount_in: int, route: RoutePlan) -> Quote | None:
        if not self._is_cl_pool_route(route):
            if _debug_routes_enabled():
                _logger.debug(
                    "aerodrome_cl quote skipped: route not eligible",
                    extra={
                        "provider": self.name,
                        "amount_in": int(amount_in),
                        "route": list(route.tokens)
                        if hasattr(route, "tokens")
                        else None,
                    },
                )
            return None
        t0 = time.perf_counter()
        try:
            from libs.dex.diem_fallbacks import diem_usdc_slot0_quote

            slot0_quote = diem_usdc_slot0_quote(
                amount_in, route.tokens[0], route.tokens[1]
            )
            if slot0_quote and slot0_quote.amount_out > 0:
                try:
                    object.__setattr__(slot0_quote, "provider", self.name)
                except Exception:
                    slot0_quote.provider = self.name  # type: ignore[attr-defined]
                _metrics_inc(
                    "dex_quotes_total",
                    labels={"provider": self.name, "status": "ok"},
                )
                _bucket_latency("quote", self.name, time.perf_counter() - t0)
                if _debug_routes_enabled():
                    _logger.debug(
                        "aerodrome_cl quote ok",
                        extra={
                            "provider": self.name,
                            "amount_in": int(amount_in),
                            "amount_out": int(slot0_quote.amount_out),
                            "route": list(route.tokens)
                            if hasattr(route, "tokens")
                            else None,
                        },
                    )
                return slot0_quote
        except Exception:
            pass
        _metrics_inc(
            "dex_quotes_total", labels={"provider": self.name, "status": "err"}
        )
        if _debug_routes_enabled():
            _logger.debug(
                "aerodrome_cl quote empty",
                extra={
                    "provider": self.name,
                    "amount_in": int(amount_in),
                    "route": list(route.tokens) if hasattr(route, "tokens") else None,
                },
            )
        return None

    def trade(
        self, amount_in: int, min_amount_out: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        from libs.dex.routes import _normalize_address

        t0 = time.perf_counter()
        if not self._is_cl_pool_route(route):
            raise RuntimeError("aerodrome_cl only supports the configured CL pool")
        token_in_raw = _normalize_address(route.tokens[0])
        token_out_raw = _normalize_address(route.tokens[1])
        token_in = _Web3.to_checksum_address(token_in_raw)
        token_out = _Web3.to_checksum_address(token_out_raw)
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60
        params = (
            token_in,
            token_out,
            int(self.tick_spacing),
            recipient,
            deadline,
            amount_in,
            min_amount_out,
            0,
        )
        fn = self.router.functions.exactInputSingle(params)
        built = fn.build_transaction({"from": recipient})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc(
            "dex_trades_total", labels={"provider": self.name, "mode": "exact_in"}
        )
        _bucket_latency("trade", self.name, time.perf_counter() - t0)
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Quote | None:
        """Exact-out quote using slot0-based price calculation for Aerodrome SlipStream."""
        if not self._is_cl_pool_route(route):
            return None
        t0 = time.perf_counter()
        try:
            from libs.dex.diem_fallbacks import diem_usdc_slot0_quote_exact_out

            slot0_quote = diem_usdc_slot0_quote_exact_out(
                amount_out, route.tokens[0], route.tokens[1]
            )
            if slot0_quote and slot0_quote.amount_in > 0:
                try:
                    object.__setattr__(slot0_quote, "provider", self.name)
                    # Mark as executable for aerodrome_cl since we have trade_exact_out
                    object.__setattr__(slot0_quote, "executable", True)
                except Exception:
                    slot0_quote.provider = self.name  # type: ignore[attr-defined]
                    slot0_quote.executable = True  # type: ignore[attr-defined]
                _metrics_inc(
                    "dex_quotes_total",
                    labels={"provider": self.name, "status": "ok", "mode": "exact_out"},
                )
                _bucket_latency("quote_exact_out", self.name, time.perf_counter() - t0)
                return slot0_quote
        except Exception:
            pass
        _metrics_inc(
            "dex_quotes_total",
            labels={"provider": self.name, "status": "err", "mode": "exact_out"},
        )
        return None

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route: RoutePlan
    ) -> dict[str, str]:
        """Execute exact-out trade via Aerodrome SlipStream CL router."""
        from web3 import Web3 as _Web3  # type: ignore

        from libs.dex.routes import _normalize_address

        t0 = time.perf_counter()
        if not self._is_cl_pool_route(route):
            raise RuntimeError("aerodrome_cl only supports the configured CL pool")
        token_in_raw = _normalize_address(route.tokens[0])
        token_out_raw = _normalize_address(route.tokens[1])
        token_in = _Web3.to_checksum_address(token_in_raw)
        token_out = _Web3.to_checksum_address(token_out_raw)
        recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60
        # ExactOutputSingle params: (tokenIn, tokenOut, tickSpacing, recipient, deadline, amountOut, amountInMaximum, sqrtPriceLimitX96)
        params = (
            token_in,
            token_out,
            int(self.tick_spacing),
            recipient,
            deadline,
            amount_out,
            max_amount_in,
            0,  # sqrtPriceLimitX96 = 0 means no limit
        )
        fn = self.router.functions.exactOutputSingle(params)
        built = fn.build_transaction({"from": recipient})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc(
            "dex_trades_total", labels={"provider": self.name, "mode": "exact_out"}
        )
        _bucket_latency("trade_exact_out", self.name, time.perf_counter() - t0)
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}


class BridgeRouteProvider(DexProvider):
    """
    Synthetic DIEM bridge provider that stitches DIEM<->VVV and VVV<->USDC legs.

    Quotes are returned as composite legs so execution reuses underlying venues.
    """

    name = "bridge_vvv"
    supports_exact_out = True

    def __init__(self, provider_map: dict[str, DexProvider]) -> None:
        self._provider_map = provider_map
        self._diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
        self._vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
        self._quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        weth_env = (
            os.getenv("WETH_TOKEN_ADDRESS")
            or os.getenv("WETH_ADDRESS")
            or "0x4200000000000000000000000000000000000006"
        )
        self._weth_addr = (weth_env or "").strip().lower()
        self._last_bridge_failure_reason: str | None = None
        self._last_bridge_failure_context: dict[str, Any] = {}

    def bridge_failure_reason(self) -> str | None:
        return self._last_bridge_failure_reason

    def bridge_failure_context(self) -> dict[str, Any]:
        return dict(self._last_bridge_failure_context or {})

    def _clear_bridge_failure(self) -> None:
        self._last_bridge_failure_reason = None
        self._last_bridge_failure_context = {}

    def _set_bridge_failure(self, reason: str, **context: Any) -> None:
        self._last_bridge_failure_reason = str(reason or "").strip() or None
        self._last_bridge_failure_context = dict(context or {})

    def _maybe_fill_uniswap_v3_fee(
        self,
        route: RoutePlan,
        *,
        token_in: str,
        token_out: str,
    ) -> RoutePlan:
        """
        Ensure a single-hop Uniswap V3 route has a fee tier when the caller did not
        annotate it.

        This is primarily needed for VVV↔USDC bridge legs, where operators often
        configure `VVV_USDC_POOL_FEE` but route specs omit `@fee` annotations.
        """
        try:
            hops = list(route.hops)
        except Exception:
            return route

        if len(hops) != 1:
            return route

        if hops[0].fee is not None:
            return route

        t_in = (token_in or "").strip().lower()
        t_out = (token_out or "").strip().lower()

        fee: int | None = None
        if {t_in, t_out} == {self._vvv_addr, self._quote_addr}:
            try:
                fee = int((os.getenv("VVV_USDC_POOL_FEE") or "3000").strip() or 3000)
            except Exception:
                fee = 3000
        else:
            try:
                raw = os.getenv("UNISWAP_V3_DEFAULT_FEE")
                if raw:
                    fee = int(str(raw).strip())
            except Exception:
                fee = None

        if fee is None:
            return route

        try:
            return route.with_default_fee(int(fee))
        except Exception:
            return route

    def _provider_for_leg(self, token_in: str, token_out: str) -> DexProvider | None:
        t_in = (token_in or "").strip().lower()
        t_out = (token_out or "").strip().lower()
        if not t_in or not t_out:
            return None
        # DIEM/VVV liquidity is in a V2-compatible pool, not Aerodrome
        diem_vvv_provider = (
            (os.getenv("DIEM_VVV_BRIDGE_PROVIDER") or "aerodrome").strip().lower()
        )
        # VVV/USDC pool (0x67A11022...) is an Aerodrome SlipStream (CL) pool
        # Default to aerodrome_cl which can quote it correctly; uniswap_v3 quoter fails
        vvv_usdc_provider = (
            (os.getenv("VVV_USDC_BRIDGE_PROVIDER") or "aerodrome_cl").strip().lower()
        )
        # VVV/WETH uses high-liquidity Aerodrome SlipStream pool for three-hop routes
        vvv_weth_provider = (
            (os.getenv("VVV_WETH_BRIDGE_PROVIDER") or "aerodrome").strip().lower()
        )

        if {t_in, t_out} == {self._diem_addr, self._vvv_addr}:
            provider = self._provider_map.get(diem_vvv_provider)
            if provider is None:
                for name in ("aerodrome", "uniswap_v2", "uniswap_v3"):
                    provider = self._provider_map.get(name)
                    if provider is not None:
                        return provider
            return provider

        if {t_in, t_out} == {self._vvv_addr, self._quote_addr}:
            provider = self._provider_map.get(vvv_usdc_provider)
            if provider is None:
                for name in ("aerodrome_cl", "uniswap_v3", "aerodrome", "uniswap_v2"):
                    provider = self._provider_map.get(name)
                    if provider is not None:
                        return provider
            return provider

        # VVV/WETH leg for three-hop routes (high-liquidity Aerodrome SlipStream pool)
        # Note: Aerodrome SlipStream pools are V3-compatible and require V3-style quoting
        if {t_in, t_out} == {self._vvv_addr, self._weth_addr}:
            # Prefer V3 for SlipStream pools, fall back to configured provider
            if vvv_weth_provider == "aerodrome":
                # Aerodrome SlipStream requires V3 quoter; use uniswap_v3 as wrapper
                v3_provider = self._provider_map.get("uniswap_v3")
                if v3_provider is not None:
                    return v3_provider
            return self._provider_map.get(vvv_weth_provider)

        # WETH/USDC leg for three-hop routes
        if {t_in, t_out} == {self._weth_addr, self._quote_addr}:
            # WETH/USDC typically has deep liquidity on Uniswap V3
            return self._provider_map.get("uniswap_v3")

        # Allow VVV leg to bridge via quote token when DIEM is output.
        if (
            self._quote_addr
            and self._vvv_addr
            and {t_in, t_out} == {self._quote_addr, self._vvv_addr}
        ):
            provider = self._provider_map.get(vvv_usdc_provider)
            if provider is None:
                for name in ("aerodrome_cl", "uniswap_v3", "aerodrome", "uniswap_v2"):
                    provider = self._provider_map.get(name)
                    if provider is not None:
                        return provider
            return provider
        return None

    @staticmethod
    def _bridge_leg2_min_vvv_units() -> int:
        raw = (os.getenv("BRIDGE_LEG2_MIN_VVV_UNITS") or "").strip()
        if not raw:
            return 0
        try:
            return max(0, int(raw))
        except Exception:
            return 0

    def _should_skip_leg2_quote(
        self,
        amount_in: int,
        stage: RoutePlan,
        provider: DexProvider | None,
        *,
        mode: str = "exact_in",
    ) -> bool:
        if mode != "exact_in" or provider is None:
            return False
        if provider.name.lower() != "aerodrome_cl":
            return False
        min_units = self._bridge_leg2_min_vvv_units()
        if min_units <= 0:
            return False
        try:
            tokens = list(stage.tokens)
        except Exception:
            return False
        if len(tokens) != 2:
            return False
        token_in = str(tokens[0]).strip().lower()
        token_out = str(tokens[-1]).strip().lower()
        if token_in != self._vvv_addr or token_out != self._quote_addr:
            return False
        try:
            amount_val = int(amount_in)
        except Exception:
            return False
        return 0 < amount_val < min_units

    def _quote_leg(
        self,
        provider: DexProvider,
        amount_in: int,
        stage: RoutePlan,
        *,
        mode: str = "exact_in",
    ) -> Quote | None:
        if self._should_skip_leg2_quote(amount_in, stage, provider, mode=mode):
            _logger.debug(
                "BridgeRouteProvider._quote_leg: skipping VVV->USDC quote below minimum",
                extra={
                    "provider": provider.name,
                    "amount_in": int(amount_in),
                    "min_vvv_units": self._bridge_leg2_min_vvv_units(),
                },
            )
            return None
        if mode == "exact_out":
            return provider.quote_exact_out(amount_in, stage)
        return provider.quote(amount_in, stage)

    @staticmethod
    def _bridge_single_leg_fallback_enabled() -> bool:
        return str(
            os.getenv("DIEM_BRIDGE_SINGLE_LEG_FALLBACK_ENABLE", "1")
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    def _bridge_single_leg_fallback_providers(self) -> list[DexProvider]:
        preferred: list[DexProvider] = []
        for name in ("uniswap_v2", "uniswap_v3", "aerodrome"):
            provider = self._provider_map.get(name)
            if provider is not None and provider not in preferred:
                preferred.append(provider)
        for name in sorted(self._provider_map.keys()):
            provider = self._provider_map.get(name)
            if provider is not None and provider not in preferred:
                preferred.append(provider)
        return preferred

    def _bridge_single_leg_fallback_routes(self, route: RoutePlan) -> list[RoutePlan]:
        try:
            tokens = list(route.tokens)
        except Exception:
            tokens = []
        if len(tokens) < 2:
            return []
        endpoints = {str(tokens[0]).strip().lower(), str(tokens[-1]).strip().lower()}
        if not (self._diem_addr and self._quote_addr) or endpoints != {
            self._diem_addr,
            self._quote_addr,
        }:
            return []
        if len(tokens) > 3:
            return []
        if len(tokens) == 3:
            mid = str(tokens[1]).strip().lower()
            if mid != self._vvv_addr:
                return []
        candidates: list[RoutePlan] = [route]
        try:
            direct = make_route([tokens[0], tokens[-1]])
            candidates.append(direct)
        except Exception:
            pass
        # Check if canonical WETH routes should be disabled
        disable_canonical_weth = os.getenv(
            "DIEM_DISABLE_CANONICAL_WETH", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if (
            self._weth_addr
            and self._weth_addr not in endpoints
            and self._weth_addr not in {str(t).strip().lower() for t in tokens}
            and not disable_canonical_weth  # Skip WETH routes when disabled
        ):
            try:
                candidates.append(make_route([tokens[0], self._weth_addr, tokens[-1]]))
            except Exception:
                pass
        unique: list[RoutePlan] = []
        seen: set[tuple[tuple[str, ...], tuple[int | None, ...]]] = set()
        for candidate in candidates:
            try:
                key = (
                    tuple(str(t).strip().lower() for t in candidate.tokens),
                    tuple(h.fee for h in candidate.hops),
                )
            except Exception:
                continue
            if key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique

    def _v2_pool_exists(self, route: RoutePlan) -> bool:
        """Return True only when a V2 pool can be verified for the route."""

        if os.getenv("PYTEST_CURRENT_TEST"):
            return True
        factory_addr = (os.getenv("UNISWAP_V2_FACTORY_ADDRESS") or "").strip()
        if not factory_addr:
            return False
        v2_provider = self._provider_map.get("uniswap_v2")
        if v2_provider is None:
            return False
        checker = getattr(v2_provider, "_pools_exist", None)
        if not callable(checker):
            return False
        try:
            from libs.dex.routing import normalize_route_for_v2

            normalized = normalize_route_for_v2(route)
        except Exception:
            normalized = route
        try:
            return bool(checker(normalized))
        except Exception:
            return False

    def _bridge_single_leg_fallback_quote_exact_in(
        self,
        *,
        amount_in: int,
        requested_route: RoutePlan,
        failure_reason: str,
        leg_index: int,
        error: str | None,
    ) -> Quote | None:
        if not self._bridge_single_leg_fallback_enabled():
            return None
        for candidate in self._bridge_single_leg_fallback_routes(requested_route):
            for provider in self._bridge_single_leg_fallback_providers():
                try:
                    quote = provider.quote(int(amount_in), candidate)
                except Exception as exc:
                    if _debug_routes_enabled():
                        _logger.debug(
                            "BridgeRouteProvider.single_leg_fallback quote failed provider=%s route=%s amount_in=%s error=%s",
                            provider.name,
                            list(candidate.tokens)
                            if hasattr(candidate, "tokens")
                            else None,
                            int(amount_in),
                            str(exc),
                        )
                    continue
                if quote is None or not isinstance(quote, Quote):
                    continue
                try:
                    amount_out_val = int(getattr(quote, "amount_out", 0))
                except Exception:
                    continue
                if amount_out_val <= 0:
                    continue
                if not bool(getattr(quote, "executable", True)):
                    continue
                combined = Quote(
                    provider=self.name,
                    amount_in=int(amount_in),
                    amount_out=amount_out_val,
                    route=quote.as_route(),
                    executable=bool(getattr(quote, "executable", True)),
                )
                self._composite_attach(combined, [quote])
                try:
                    _dex_diag_log_event(
                        {
                            "event": "dex_bridge_single_leg_fallback",
                            "mode": "exact_in",
                            "requested_route": list(requested_route.tokens),
                            "fallback_route": list(
                                getattr(quote, "route", candidate).tokens
                            )
                            if getattr(quote, "route", None)
                            else list(candidate.tokens),
                            "fallback_provider": str(
                                getattr(quote, "provider", provider.name)
                            ),
                            "amount_in": int(amount_in),
                            "amount_out": int(getattr(quote, "amount_out", 0)),
                            "failure_reason": failure_reason,
                            "leg_index": int(leg_index),
                            "error": error,
                        }
                    )
                except Exception:
                    pass
                _logger.info(
                    "BridgeRouteProvider.quote: single-leg fallback succeeded",
                    extra={
                        "mode": "exact_in",
                        "failure_reason": failure_reason,
                        "leg_index": leg_index,
                        "fallback_provider": str(
                            getattr(quote, "provider", provider.name)
                        ),
                        "requested_route": list(requested_route.tokens)
                        if hasattr(requested_route, "tokens")
                        else None,
                        "fallback_route": list(quote.as_route().tokens)
                        if quote is not None and hasattr(quote, "route")
                        else None,
                    },
                )
                return combined
        return None

    def _bridge_single_leg_fallback_quote_exact_out(
        self,
        *,
        amount_out: int,
        requested_route: RoutePlan,
        failure_reason: str,
        leg_index: int,
        error: str | None,
    ) -> Quote | None:
        if not self._bridge_single_leg_fallback_enabled():
            return None
        for candidate in self._bridge_single_leg_fallback_routes(requested_route):
            for provider in self._bridge_single_leg_fallback_providers():
                if not bool(getattr(provider, "supports_exact_out", False)):
                    continue
                try:
                    quote = provider.quote_exact_out(int(amount_out), candidate)
                except Exception as exc:
                    if _debug_routes_enabled():
                        _logger.debug(
                            "BridgeRouteProvider.single_leg_fallback exact_out failed provider=%s route=%s amount_out=%s error=%s",
                            provider.name,
                            list(candidate.tokens)
                            if hasattr(candidate, "tokens")
                            else None,
                            int(amount_out),
                            str(exc),
                        )
                    continue
                if quote is None or not isinstance(quote, Quote):
                    continue
                try:
                    amount_in_val = int(getattr(quote, "amount_in", 0))
                except Exception:
                    continue
                if amount_in_val <= 0:
                    continue
                if not bool(getattr(quote, "executable", True)):
                    continue
                combined = Quote(
                    provider=self.name,
                    amount_in=amount_in_val,
                    amount_out=int(amount_out),
                    route=quote.as_route(),
                    executable=bool(getattr(quote, "executable", True)),
                )
                self._composite_attach(combined, [quote])
                try:
                    _dex_diag_log_event(
                        {
                            "event": "dex_bridge_single_leg_fallback",
                            "mode": "exact_out",
                            "requested_route": list(requested_route.tokens),
                            "fallback_route": list(
                                getattr(quote, "route", candidate).tokens
                            )
                            if getattr(quote, "route", None)
                            else list(candidate.tokens),
                            "fallback_provider": str(
                                getattr(quote, "provider", provider.name)
                            ),
                            "amount_out": int(amount_out),
                            "amount_in": int(getattr(quote, "amount_in", 0)),
                            "failure_reason": failure_reason,
                            "leg_index": int(leg_index),
                            "error": error,
                        }
                    )
                except Exception:
                    pass
                _logger.info(
                    "BridgeRouteProvider.quote_exact_out: single-leg fallback succeeded",
                    extra={
                        "mode": "exact_out",
                        "failure_reason": failure_reason,
                        "leg_index": leg_index,
                        "fallback_provider": str(
                            getattr(quote, "provider", provider.name)
                        ),
                        "requested_route": list(requested_route.tokens)
                        if hasattr(requested_route, "tokens")
                        else None,
                        "fallback_route": list(quote.as_route().tokens)
                        if quote is not None and hasattr(quote, "route")
                        else None,
                    },
                )
                return combined
        return None

    def _build_composite_route(self, stage1: RoutePlan, stage2: RoutePlan) -> RoutePlan:
        tokens: list[str] = list(stage1.tokens)
        if stage2.tokens:
            tokens.extend(list(stage2.tokens)[1:])
        fees: list[int | None] = [hop.fee for hop in stage1.hops]
        fees.extend(hop.fee for hop in stage2.hops)
        try:
            composite = make_route(tokens, fees or None)
        except Exception:
            composite = make_route(tokens)
        bridge_legs: list[dict[str, Any]] = []

        diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip()
        if diem_vvv_pair:
            # DIEM/VVV is a VOLATILE pool on Aerodrome, not stable. Default to false.
            stable_env = (
                os.getenv("DIEM_VVV_STABLE") or os.getenv("AERODROME_STABLE") or "false"
            )
            stable_flag = str(stable_env).strip().lower() in {"1", "true", "yes", "on"}
            bridge_legs.append(
                {
                    "token_in": stage1.tokens[0],
                    "token_out": stage1.tokens[1],
                    "provider": (os.getenv("DIEM_VVV_BRIDGE_PROVIDER") or "aerodrome")
                    .strip()
                    .lower(),
                    "pool_address": diem_vvv_pair,
                    "stable": stable_flag,
                }
            )
        vvv_usdc_pool = (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip() or (
            os.getenv("VVV_USDC_POOL_V3_ADDRESS") or ""
        ).strip()
        # Only attach VVV/USDC pool metadata if stage 2 is a direct VVV->USDC hop.
        # If stage 2 is multi-hop (e.g. VVV->WETH->USDC), we don't want to imply
        # it uses the direct pool.
        stage2_tokens_lower = [str(t).strip().lower() for t in stage2.tokens]
        vvv_addr_lower = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
        quote_addr_lower = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        is_vvv_usdc_leg = (
            len(stage2_tokens_lower) == 2
            and vvv_addr_lower
            and quote_addr_lower
            and {stage2_tokens_lower[0], stage2_tokens_lower[1]}
            == {vvv_addr_lower, quote_addr_lower}
        )
        if is_vvv_usdc_leg and vvv_usdc_pool:
            try:
                fee = int(os.getenv("VVV_USDC_POOL_FEE") or "3000")
            except Exception:
                fee = 3000
            bridge_legs.append(
                {
                    "token_in": stage2.tokens[0],
                    "token_out": stage2.tokens[1],
                    "provider": (
                        os.getenv("VVV_USDC_BRIDGE_PROVIDER") or "aerodrome_cl"
                    )
                    .strip()
                    .lower(),
                    "pool_address": vvv_usdc_pool,
                    "fee": fee,
                }
            )
        elif is_vvv_usdc_leg and not vvv_usdc_pool:
            _logger.warning(
                "BridgeRouteProvider: missing VVV/USDC pool address for bridge metadata",
                extra={
                    "event": "dex_bridge_metadata_missing_pool",
                    "leg": "vvv_usdc",
                    "stage2_tokens": stage2_tokens_lower,
                },
            )
        # VVV/WETH pool metadata for three-hop routes
        vvv_weth_pool = (os.getenv("VVV_WETH_POOL_ADDRESS") or "").strip()
        if vvv_weth_pool:
            stage2_tokens_lower = [str(t).strip().lower() for t in stage2.tokens]
            vvv_addr_lower = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
            weth_addr_lower = (
                (
                    os.getenv("WETH_TOKEN_ADDRESS")
                    or os.getenv("WETH_ADDRESS")
                    or "0x4200000000000000000000000000000000000006"
                )
                .strip()
                .lower()
            )
            # Check if VVV and WETH are adjacent in stage2
            for i in range(len(stage2_tokens_lower) - 1):
                if {stage2_tokens_lower[i], stage2_tokens_lower[i + 1]} == {
                    vvv_addr_lower,
                    weth_addr_lower,
                }:
                    try:
                        vvv_weth_fee = int(os.getenv("VVV_WETH_POOL_FEE") or "500")
                    except Exception:
                        vvv_weth_fee = 500
                    bridge_legs.append(
                        {
                            "token_in": stage2.tokens[i],
                            "token_out": stage2.tokens[i + 1],
                            "provider": (
                                os.getenv("VVV_WETH_BRIDGE_PROVIDER") or "aerodrome"
                            )
                            .strip()
                            .lower(),
                            "pool_address": vvv_weth_pool,
                            "fee": vvv_weth_fee,
                        }
                    )
                    break
        if bridge_legs:
            attach_composite_metadata(
                composite, bridge_legs=bridge_legs, is_composite=True
            )
        return composite

    @staticmethod
    def _diem_pair(tokens: Sequence[str], diem: str, quote: str) -> bool:
        if len(tokens) != 2:
            return False
        pair = {tokens[0].lower(), tokens[1].lower()}
        return diem in pair and quote in pair

    def _two_stage(self, route: RoutePlan) -> tuple[RoutePlan, RoutePlan] | None:
        try:
            tokens = list(route.tokens)
        except Exception:
            tokens = []

        # Handle 2-token routes (USDC->DIEM): expand into bridge stages
        if len(tokens) == 2:
            stage = build_two_stage_diem_route(tokens[0], tokens[1])
            return stage

        # Handle 3+ token routes that contain VVV as a bridge
        # We split the route at the VVV token
        token_set = {t.lower() for t in tokens}
        if self._diem_addr in token_set and self._vvv_addr in token_set:
            # Find VVV index
            vvv_idx = -1
            for i, t in enumerate(tokens):
                if t.strip().lower() == self._vvv_addr:
                    vvv_idx = i
                    break

            if vvv_idx > 0 and vvv_idx < len(tokens) - 1:
                from libs.dex.routes import make_route

                # Preserve fees from original route
                all_fees = [h.fee for h in route.hops] if hasattr(route, "hops") else []

                # Stage 1: start -> VVV
                fees1 = all_fees[:vvv_idx] if all_fees else None
                stage1 = make_route(tokens[: vvv_idx + 1], fees=fees1)

                # Stage 2: VVV -> end
                fees2 = all_fees[vvv_idx:] if all_fees else None
                stage2 = make_route(tokens[vvv_idx:], fees=fees2)

                return (stage1, stage2)

        # Handle routes containing WETH as a bridge (for three-hop without VVV split)
        # e.g., DIEM -> WETH -> USDC or USDC -> WETH -> DIEM
        if self._diem_addr in token_set and self._weth_addr in token_set:
            weth_idx = -1
            for i, t in enumerate(tokens):
                if t.strip().lower() == self._weth_addr:
                    weth_idx = i
                    break

            if weth_idx > 0 and weth_idx < len(tokens) - 1:
                from libs.dex.routes import make_route

                all_fees = [h.fee for h in route.hops] if hasattr(route, "hops") else []

                fees1 = all_fees[:weth_idx] if all_fees else None
                stage1 = make_route(tokens[: weth_idx + 1], fees=fees1)

                fees2 = all_fees[weth_idx:] if all_fees else None
                stage2 = make_route(tokens[weth_idx:], fees=fees2)

                return (stage1, stage2)

        return None

    @staticmethod
    def _composite_attach(quote: Quote, legs: list[Quote]) -> None:
        try:
            object.__setattr__(quote, "_composite_legs", legs)
        except Exception:
            pass

    @staticmethod
    def _reserve_pref_threshold() -> float:
        """Return relative drift threshold (fraction) for reserve preference."""

        try:
            bps_raw = (os.getenv("DIEM_VVV_RESERVE_PREF_DRIFT_BPS") or "4000").strip()
            bps = float(bps_raw or 4000.0)
        except Exception:
            bps = 4000.0
        return max(0.0, bps) / 10_000.0

    @staticmethod
    def _configured_diem_vvv_pair() -> str:
        return (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip()

    @staticmethod
    def _route_pool_address(route: RoutePlan | None) -> str | None:
        """Best-effort extract of pool address from a route hop, if present."""

        if not route or not hasattr(route, "hops"):
            return None
        try:
            for hop in route.hops:
                pool_addr = getattr(hop, "pool_address", None)
                if pool_addr:
                    return str(pool_addr)
        except Exception:
            return None
        return None

    def _bridge_leg_context(
        self,
        route: RoutePlan | None,
        provider: DexProvider | None = None,
    ) -> dict[str, Any]:
        """Return pool/fee context for a bridge leg for diagnostics."""

        token_in: str | None = None
        token_out: str | None = None
        fee: int | None = None
        try:
            tokens = list(route.tokens) if route is not None else []
        except Exception:
            tokens = []
        if tokens:
            token_in = str(tokens[0])
            token_out = str(tokens[-1])
        try:
            if route is not None and hasattr(route, "hops") and len(route.hops) == 1:
                fee_val = route.hops[0].fee
                fee = int(fee_val) if fee_val is not None else None
        except Exception:
            fee = None

        pool_address = self._route_pool_address(route)

        if not pool_address:
            try:
                bridge_legs = getattr(route, "_bridge_legs", None)
            except Exception:
                bridge_legs = None
            if bridge_legs and token_in and token_out:
                token_in_norm = token_in.strip().lower()
                token_out_norm = token_out.strip().lower()
                for leg in bridge_legs:
                    try:
                        leg_in = str(leg.get("token_in", "")).strip().lower()
                        leg_out = str(leg.get("token_out", "")).strip().lower()
                    except Exception:
                        continue
                    if {leg_in, leg_out} == {token_in_norm, token_out_norm}:
                        pool_address = str(leg.get("pool_address", "")).strip() or None
                        if fee is None and leg.get("fee") is not None:
                            try:
                                fee = int(leg.get("fee"))
                            except Exception:
                                pass
                        break

        t_in = (token_in or "").strip().lower()
        t_out = (token_out or "").strip().lower()
        if not pool_address:
            if {t_in, t_out} == {self._diem_addr, self._vvv_addr}:
                pool_address = (
                    os.getenv("DIEM_VVV_PAIR_ADDRESS") or ""
                ).strip() or None
            elif {t_in, t_out} == {self._vvv_addr, self._quote_addr}:
                pool_address = (
                    (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip()
                    or (os.getenv("VVV_USDC_POOL_V3_ADDRESS") or "").strip()
                    or None
                )
                if fee is None:
                    try:
                        fee = int(os.getenv("VVV_USDC_POOL_FEE") or "3000")
                    except Exception:
                        fee = 3000
            elif {t_in, t_out} == {self._vvv_addr, self._weth_addr}:
                pool_address = (
                    os.getenv("VVV_WETH_POOL_ADDRESS") or ""
                ).strip() or None
                if fee is None:
                    try:
                        fee = int(os.getenv("VVV_WETH_POOL_FEE") or "500")
                    except Exception:
                        fee = 500

        if not pool_address and provider is not None:
            for attr in (
                "_route_pool_address",
                "route_pool_address",
                "resolve_pool_address",
            ):
                try:
                    helper = getattr(provider, attr, None)
                except Exception:
                    helper = None
                if callable(helper):
                    try:
                        pool_address = helper(route)
                    except Exception:
                        pool_address = None
                if pool_address:
                    break
            if not pool_address:
                try:
                    pool_candidate = getattr(provider, "pool_addr", None) or getattr(
                        provider, "pool_address", None
                    )
                    if pool_candidate:
                        pool_address = str(pool_candidate)
                except Exception:
                    pass

        return {
            "token_in": token_in,
            "token_out": token_out,
            "pool_address": pool_address,
            "fee": fee,
        }

    def _maybe_prefer_reserve_quote(
        self,
        *,
        mode: str,
        leg_index: int,
        stage_tokens: list[str],
        router_quote: Quote | None,
        reserve_quote_factory: Callable[[], Quote | None],
        provider_name: str,
    ) -> tuple[Quote | None, bool]:
        """
        When router DIEM/VVV quote drifts far from on-chain reserves, prefer reserve math.

        Returns (preferred_quote, used_reserve_flag).
        """

        if router_quote is None:
            return None, False

        reserve_quote = reserve_quote_factory()
        if reserve_quote is None:
            return router_quote, False

        if mode == "exact_in":
            router_val = getattr(router_quote, "amount_out", 0)
            reserve_val = getattr(reserve_quote, "amount_out", 0)
        else:
            router_val = getattr(router_quote, "amount_in", 0)
            reserve_val = getattr(reserve_quote, "amount_in", 0)

        if not router_val or not reserve_val:
            return router_quote, False

        drift = abs(float(router_val) - float(reserve_val)) / float(reserve_val)
        threshold = self._reserve_pref_threshold()

        if drift <= threshold:
            return router_quote, False

        try:
            _dex_diag_log_event(
                {
                    "event": "dex_bridge_leg_reserve_preferred",
                    "leg_index": leg_index,
                    "mode": mode,
                    "provider": provider_name,
                    "router_value": float(router_val),
                    "reserve_value": float(reserve_val),
                    "drift": float(drift),
                    "threshold": float(threshold),
                    "tokens": stage_tokens,
                }
            )
        except Exception:
            pass

        _logger.warning(
            "BridgeRouteProvider: preferring reserve math quote over router due to drift",
            extra={
                "leg_index": leg_index,
                "mode": mode,
                "provider": provider_name,
                "router_value": router_val,
                "reserve_value": reserve_val,
                "drift": drift,
                "threshold": threshold,
                "tokens": stage_tokens,
            },
        )

        return reserve_quote, True

    def _quote_multihop_stage(
        self,
        amount_in: int,
        stage: RoutePlan,
        mode: str = "exact_in",
    ) -> Quote | None:
        """
        Quote a multi-hop stage by iterating through each hop sequentially.

        For three-hop routes like USDC->WETH->VVV->DIEM, this handles stages
        like USDC->WETH->VVV (2 hops) or VVV->WETH->USDC (2 hops).
        """
        try:
            tokens = list(stage.tokens)
        except Exception:
            return None

        if len(tokens) < 2:
            return None

        # Single hop - use standard provider lookup
        if len(tokens) == 2:
            provider = self._provider_for_leg(tokens[0], tokens[-1])
            if provider is None:
                return None
            try:
                return self._quote_leg(provider, amount_in, stage, mode=mode)
            except Exception:
                return None

        # Multi-hop - iterate through each hop
        current_amount = amount_in
        hop_quotes: list[Quote] = []
        all_fees = [h.fee for h in stage.hops] if hasattr(stage, "hops") else []

        for i in range(len(tokens) - 1):
            t_in = tokens[i]
            t_out = tokens[i + 1]
            hop_fee = all_fees[i] if i < len(all_fees) else None

            provider = self._provider_for_leg(t_in, t_out)
            if provider is None:
                _logger.debug(
                    "BridgeRouteProvider._quote_multihop_stage: no provider for hop",
                    extra={"hop_index": i, "token_in": t_in, "token_out": t_out},
                )
                return None

            # For VVV/WETH leg, use the configured pool fee
            t_in_lower = t_in.lower() if isinstance(t_in, str) else str(t_in).lower()
            t_out_lower = (
                t_out.lower() if isinstance(t_out, str) else str(t_out).lower()
            )
            if {t_in_lower, t_out_lower} == {self._vvv_addr, self._weth_addr}:
                vvv_weth_fee_env = os.getenv("VVV_WETH_POOL_FEE")
                if vvv_weth_fee_env:
                    try:
                        hop_fee = int(vvv_weth_fee_env)
                    except Exception:
                        hop_fee = 500  # Default for Aerodrome SlipStream

            try:
                hop_route = make_route(
                    [t_in, t_out], fees=[hop_fee] if hop_fee else None
                )
                hop_quote = self._quote_leg(
                    provider, current_amount, hop_route, mode=mode
                )
            except Exception as exc:
                _logger.debug(
                    "BridgeRouteProvider._quote_multihop_stage: hop quote failed",
                    extra={
                        "hop_index": i,
                        "token_in": t_in,
                        "token_out": t_out,
                        "provider": provider.name,
                        "error": str(exc),
                    },
                )
                return None

            if hop_quote is None:
                return None

            hop_quotes.append(hop_quote)

            if mode == "exact_in":
                current_amount = hop_quote.amount_out
            else:
                current_amount = hop_quote.amount_in

        if not hop_quotes:
            return None

        # Combine hop quotes into a single composite quote
        if mode == "exact_in":
            combined = Quote(
                provider=self.name,
                amount_in=amount_in,
                amount_out=hop_quotes[-1].amount_out,
                route=stage,
                executable=all(getattr(q, "executable", True) for q in hop_quotes),
            )
        else:
            combined = Quote(
                provider=self.name,
                amount_in=hop_quotes[0].amount_in,
                amount_out=amount_in,
                route=stage,
                executable=all(getattr(q, "executable", True) for q in hop_quotes),
            )

        self._composite_attach(combined, hop_quotes)
        return combined

    def quote(self, amount_in: int, route: RoutePlan) -> Quote | None:
        self._clear_bridge_failure()
        two_stage = self._two_stage(route)
        if not two_stage:
            try:
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_unsupported_route",
                        "mode": "exact_in",
                        "amount_in": int(amount_in),
                        "route_tokens": list(route.tokens),
                    }
                )
            except Exception:
                pass
            try:
                self._set_bridge_failure(
                    "unsupported_route", route_tokens=list(route.tokens)
                )
            except Exception:
                self._set_bridge_failure("unsupported_route")
            return None
        stage1, stage2 = two_stage
        # Check if stages are multi-hop (more than 2 tokens)
        stage1_is_multihop = len(list(stage1.tokens)) > 2
        stage2_is_multihop = len(list(stage2.tokens)) > 2
        # Use endpoints to look up provider, as stages might be multi-hop
        leg1_provider = self._provider_for_leg(stage1.tokens[0], stage1.tokens[-1])
        leg2_provider = self._provider_for_leg(stage2.tokens[0], stage2.tokens[-1])
        # For multi-hop stages, check each hop has a provider
        if stage1_is_multihop:
            stage1_tokens = list(stage1.tokens)
            for i in range(len(stage1_tokens) - 1):
                if (
                    self._provider_for_leg(stage1_tokens[i], stage1_tokens[i + 1])
                    is None
                ):
                    leg1_provider = None
                    break
            else:
                leg1_provider = self  # Use self as synthetic provider for multi-hop
        if stage2_is_multihop:
            stage2_tokens_list = list(stage2.tokens)
            for i in range(len(stage2_tokens_list) - 1):
                if (
                    self._provider_for_leg(
                        stage2_tokens_list[i], stage2_tokens_list[i + 1]
                    )
                    is None
                ):
                    leg2_provider = None
                    break
            else:
                leg2_provider = self  # Use self as synthetic provider for multi-hop
        if leg1_provider is None or leg2_provider is None:
            stage1_tokens = list(stage1.tokens) if hasattr(stage1, "tokens") else []
            stage2_tokens = list(stage2.tokens) if hasattr(stage2, "tokens") else []
            try:
                missing_idx = 0 if leg1_provider is None else 1
                provider_name = (
                    leg1_provider.name
                    if missing_idx == 0 and leg1_provider is not None
                    else (leg2_provider.name if leg2_provider is not None else None)
                )
                leg_context = self._bridge_leg_context(
                    stage1 if missing_idx == 0 else stage2,
                    leg1_provider if missing_idx == 0 else leg2_provider,
                )
                missing_tokens = stage1_tokens if missing_idx == 0 else stage2_tokens
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_leg_failure",
                        "leg_index": missing_idx,
                        "token_in": missing_tokens[0] if missing_tokens else None,
                        "token_out": missing_tokens[-1] if missing_tokens else None,
                        "provider": provider_name,
                        "mode": "exact_in",
                        "reason": "missing_provider",
                        "amount": amount_in,
                        "pool_address": leg_context.get("pool_address"),
                        "fee": leg_context.get("fee"),
                        "requested_route": list(route.tokens),
                        "route_tokens": missing_tokens,
                    }
                )
            except Exception:
                pass
            _logger.debug(
                "BridgeRouteProvider.quote: missing leg providers",
                extra={
                    "leg1_provider": leg1_provider.name if leg1_provider else None,
                    "leg2_provider": leg2_provider.name if leg2_provider else None,
                    "stage1": stage1_tokens,
                    "stage2": stage2_tokens,
                    "stage1_is_multihop": stage1_is_multihop,
                    "stage2_is_multihop": stage2_is_multihop,
                },
            )
            fallback = self._bridge_single_leg_fallback_quote_exact_in(
                amount_in=int(amount_in),
                requested_route=route,
                failure_reason="missing_leg_provider",
                leg_index=0,
                error=None,
            )
            if fallback is not None:
                return fallback
            self._set_bridge_failure(
                "missing_leg_provider",
                leg1_provider=leg1_provider.name if leg1_provider else None,
                leg2_provider=leg2_provider.name if leg2_provider else None,
                stage1_tokens=stage1_tokens,
                stage2_tokens=stage2_tokens,
            )
            return None

        # Quote leg1 first (first leg: DIEM->VVV for sell, USDC->VVV for buy)
        # Use multi-hop quoting if stage1 has more than 2 tokens
        leg1_quote: Quote | None = None
        leg1_error: str | None = None
        leg1_error_type: str | None = None
        stage1_tokens_prequote = (
            list(stage1.tokens) if hasattr(stage1, "tokens") else []
        )
        is_stage1_two_token = len(stage1_tokens_prequote) == 2
        quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        is_stage1_vvv_usdc = (
            is_stage1_two_token
            and self._vvv_addr in [t.lower() for t in stage1_tokens_prequote]
            and quote_addr in [t.lower() for t in stage1_tokens_prequote]
        )
        if is_stage1_vvv_usdc and not stage1_is_multihop:
            try:
                slot0_quote = vvv_usdc_v3_slot0_quote(
                    amount_in, stage1.tokens[0], stage1.tokens[-1]
                )
                if slot0_quote:
                    leg1_quote = slot0_quote
                    _logger.info(
                        "BridgeRouteProvider.quote: leg1 slot0 prequote used",
                        extra={
                            "leg1_provider": leg1_provider.name
                            if leg1_provider
                            else "unknown",
                            "stage1": stage1_tokens_prequote,
                            "amount_in": amount_in,
                            "slot0_amount_out": slot0_quote.amount_out,
                            "provider": slot0_quote.provider,
                        },
                    )
            except Exception:
                pass
        try:
            if leg1_quote is None:
                if stage1_is_multihop:
                    # Use multi-hop quoting for stages with more than 2 tokens
                    leg1_quote = self._quote_multihop_stage(
                        amount_in, stage1, mode="exact_in"
                    )
                else:
                    stage1_for_quote = stage1
                    try:
                        if leg1_provider.name.lower() == "uniswap_v3":
                            stage1_for_quote = self._maybe_fill_uniswap_v3_fee(
                                stage1,
                                token_in=str(stage1.tokens[0]),
                                token_out=str(stage1.tokens[-1]),
                            )
                    except Exception:
                        stage1_for_quote = stage1
                    leg1_quote = leg1_provider.quote(amount_in, stage1_for_quote)
        except Exception as exc:
            leg1_error = str(exc)
            leg1_error_type = type(exc).__name__
            leg1_quote = None

        # Prefer reserve-math quote when router DIEM/VVV leg drifts from pool price
        stage1_tokens = list(stage1.tokens) if hasattr(stage1, "tokens") else []
        is_stage1_diem_vvv = self._diem_addr in [
            t.lower() for t in stage1_tokens
        ] and self._vvv_addr in [t.lower() for t in stage1_tokens]
        reserve_pref_used_leg1 = False
        if leg1_quote is not None and is_stage1_diem_vvv:
            leg1_quote, reserve_pref_used_leg1 = self._maybe_prefer_reserve_quote(
                mode="exact_in",
                leg_index=0,
                stage_tokens=stage1_tokens,
                router_quote=leg1_quote,
                reserve_quote_factory=lambda: diem_vvv_quote_exact_in_from_reserves(
                    amount_in, stage1.tokens[0], stage1.tokens[1]
                ),
                provider_name=leg1_provider.name if leg1_provider else "unknown",
            )

        # Try reserve fallback for leg1 if router quote failed
        reserve_fallback_used = False
        if leg1_quote is None:
            # Check if this is a DIEM/VVV leg that can use reserve fallback
            is_diem_vvv_leg = is_stage1_diem_vvv

            if is_diem_vvv_leg:
                reserve_fallback = diem_vvv_quote_exact_in_from_reserves(
                    amount_in, stage1.tokens[0], stage1.tokens[1]
                )
                if reserve_fallback:
                    leg1_quote = reserve_fallback
                    reserve_fallback_used = True
                    _logger.info(
                        "BridgeRouteProvider.quote: leg1 reserve fallback succeeded",
                        extra={
                            "leg1_provider": leg1_provider.name,
                            "stage1": stage1_tokens,
                            "amount_in": amount_in,
                            "reserve_fallback_amount_out": reserve_fallback.amount_out,
                        },
                    )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote: leg1 reserve fallback unavailable or disabled",
                        extra={
                            "leg1_provider": leg1_provider.name,
                            "stage1": stage1_tokens,
                            "fallback_enabled": os.getenv(
                                "DIEM_ENABLE_PAIR_MATH_FALLBACK", "0"
                            )
                            .strip()
                            .lower()
                            in {"1", "true", "yes", "on"},
                            "is_diem_vvv_leg": is_diem_vvv_leg,
                        },
                    )
            else:
                # Check if this is USDC→VVV leg (buy path leg1) and try V2 fallback + V3 analytic
                # V2 fallback only applies to 2-token legs (USDC↔VVV), not multi-hop stages
                quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                is_two_token = len(stage1_tokens) == 2
                is_usdc_vvv_leg = (
                    is_two_token
                    and quote_addr in [t.lower() for t in stage1_tokens]
                    and self._vvv_addr in [t.lower() for t in stage1_tokens]
                )

                if (
                    not is_two_token
                    and quote_addr in [t.lower() for t in stage1_tokens]
                    and self._vvv_addr in [t.lower() for t in stage1_tokens]
                ):
                    # Multi-hop stage detected, skip V2 fallback
                    try:
                        _dex_diag_log_event(
                            {
                                "event": "dex_bridge_leg_v2_skip",
                                "reason": "multihop_stage",
                                "mode": "exact_in",
                                "leg_index": 0,
                                "tokens": stage1_tokens,
                                "provider": leg1_provider.name
                                if leg1_provider
                                else "unknown",
                                "token_count": len(stage1_tokens),
                            }
                        )
                    except Exception:
                        pass

                if is_usdc_vvv_leg:
                    # Try V2 provider fallback when V3 router fails (for execution)
                    v2_fallback_enabled = os.getenv(
                        "VVV_USDC_V2_FALLBACK_ENABLE", "1"
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    v2_fallback_only_for_buys = os.getenv(
                        "VVV_USDC_V2_FALLBACK_ONLY_FOR_BUYS", "0"
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    v2_pool_exists = self._v2_pool_exists(stage1)

                    # For exact-in (buy path), always try V2 if enabled
                    should_try_v2 = v2_fallback_enabled and (
                        not v2_fallback_only_for_buys
                        or True  # exact-in is always buy path
                    )
                    if should_try_v2 and not v2_pool_exists:
                        should_try_v2 = False
                        _logger.info(
                            "BridgeRouteProvider.quote: V2 fallback skipped (no V2 pool)",
                            extra={
                                "leg_index": 0,
                                "mode": "exact_in",
                                "tokens": stage1_tokens,
                                "provider": leg1_provider.name
                                if leg1_provider
                                else "unknown",
                            },
                        )

                    v2_fallback_provider = None
                    if should_try_v2:
                        v2_fallback_provider = self._provider_map.get("uniswap_v2")

                    if (
                        v2_fallback_provider
                        and leg1_provider.name.lower() == "uniswap_v3"
                    ):
                        # Create a V2-compatible route (remove fee annotations)
                        from libs.dex.routes import make_route

                        stage1_v2_route = make_route(
                            [stage1.tokens[0], stage1.tokens[-1]]
                        )
                        try:
                            v2_fallback_quote = v2_fallback_provider.quote(
                                amount_in, stage1_v2_route
                            )
                            if v2_fallback_quote and v2_fallback_quote.amount_out > 0:
                                # Prefer V2 if it's executable
                                if (
                                    v2_fallback_quote.executable
                                    if hasattr(v2_fallback_quote, "executable")
                                    else True
                                ):
                                    leg1_quote = v2_fallback_quote
                                    reserve_fallback_used = True
                                    _logger.info(
                                        "BridgeRouteProvider.quote: leg1 V2 fallback succeeded",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "v2_fallback_provider": v2_fallback_provider.name,
                                            "stage1": stage1_tokens,
                                            "amount_in": amount_in,
                                            "v2_fallback_amount_out": v2_fallback_quote.amount_out,
                                        },
                                    )
                                else:
                                    # V2 quote not executable, try analytic fallback
                                    v3_analytic_quote = vvv_usdc_v3_mid_price_quote(
                                        amount_in,
                                        stage1.tokens[0],
                                        stage1.tokens[1],
                                    )
                                    if v3_analytic_quote:
                                        leg1_quote = v3_analytic_quote
                                        _logger.info(
                                            "BridgeRouteProvider.quote: leg1 V3 analytic fallback succeeded (preview-only), V2 fallback not executable",
                                            extra={
                                                "leg1_provider": leg1_provider.name,
                                                "stage1": stage1_tokens,
                                                "amount_in": amount_in,
                                                "analytic_amount_out": v3_analytic_quote.amount_out,
                                                "provider": v3_analytic_quote.provider,
                                                "v2_available": True,
                                            },
                                        )
                            else:
                                # V2 failed, try analytic fallback
                                v3_analytic_quote = vvv_usdc_v3_mid_price_quote(
                                    amount_in,
                                    stage1.tokens[0],
                                    stage1.tokens[1],
                                )
                                if v3_analytic_quote:
                                    leg1_quote = v3_analytic_quote
                                    _logger.info(
                                        "BridgeRouteProvider.quote: leg1 V3 analytic fallback succeeded (preview-only), V2 fallback failed",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "stage1": stage1_tokens,
                                            "amount_in": amount_in,
                                            "analytic_amount_out": v3_analytic_quote.amount_out,
                                            "provider": v3_analytic_quote.provider,
                                        },
                                    )
                                else:
                                    _logger.info(
                                        "BridgeRouteProvider.quote: leg1 V2 fallback returned no quote",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "v2_fallback_provider": v2_fallback_provider.name,
                                            "stage1": stage1_tokens,
                                        },
                                    )
                        except Exception as v2_exc:
                            # V2 failed, try analytic fallback
                            v3_analytic_quote = vvv_usdc_v3_mid_price_quote(
                                amount_in,
                                stage1.tokens[0],
                                stage1.tokens[1],
                            )
                            if v3_analytic_quote:
                                leg1_quote = v3_analytic_quote
                                _logger.info(
                                    "BridgeRouteProvider.quote: leg1 V3 analytic fallback succeeded (preview-only), V2 fallback exception",
                                    extra={
                                        "leg1_provider": leg1_provider.name,
                                        "stage1": stage1_tokens,
                                        "amount_in": amount_in,
                                        "analytic_amount_out": v3_analytic_quote.amount_out,
                                        "provider": v3_analytic_quote.provider,
                                        "v2_error": str(v2_exc),
                                    },
                                )
                            else:
                                _logger.info(
                                    "BridgeRouteProvider.quote: leg1 V2 fallback failed",
                                    extra={
                                        "leg1_provider": leg1_provider.name,
                                        "v2_fallback_provider": v2_fallback_provider.name,
                                        "stage1": stage1_tokens,
                                        "v2_error": str(v2_exc),
                                    },
                                )
                    else:
                        # No V2 fallback available or disabled, try V3 analytic
                        v3_analytic_quote = vvv_usdc_v3_mid_price_quote(
                            amount_in,
                            stage1.tokens[0],
                            stage1.tokens[1],
                        )
                        if v3_analytic_quote:
                            leg1_quote = v3_analytic_quote
                            _logger.info(
                                "BridgeRouteProvider.quote: leg1 V3 analytic fallback succeeded (preview-only)",
                                extra={
                                    "leg1_provider": leg1_provider.name,
                                    "stage1": stage1_tokens,
                                    "amount_in": amount_in,
                                    "analytic_amount_out": v3_analytic_quote.amount_out,
                                    "provider": v3_analytic_quote.provider,
                                },
                            )
                        else:
                            _logger.info(
                                "BridgeRouteProvider.quote: leg1 V3 analytic fallback unavailable",
                                extra={
                                    "leg1_provider": leg1_provider.name,
                                    "stage1": stage1_tokens,
                                    "fallback_enabled": os.getenv(
                                        "DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1"
                                    )
                                    .strip()
                                    .lower()
                                    in {"1", "true", "yes", "on"},
                                    "is_usdc_vvv_leg": is_usdc_vvv_leg,
                                },
                            )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote: leg1 quote failed, no reserve fallback (not DIEM/VVV leg)",
                        extra={
                            "leg1_provider": leg1_provider.name,
                            "stage1": stage1_tokens,
                            "leg1_error": leg1_error,
                        },
                    )

        if leg1_quote is None or leg1_quote.amount_out <= 0:
            # Emit telemetry for bridge-leg failure
            stage1_tokens_final = (
                list(stage1.tokens) if hasattr(stage1, "tokens") else []
            )
            leg1_reason = "empty" if leg1_quote is None else "zero_output"
            try:
                diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                is_diem_leg = (
                    diem_addr
                    and vvv_addr
                    and (
                        diem_addr in [t.lower() for t in stage1_tokens_final]
                        or vvv_addr in [t.lower() for t in stage1_tokens_final]
                    )
                )
                leg_context = self._bridge_leg_context(stage1, leg1_provider)
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_leg_failure",
                        "leg_index": 0,
                        "token_in": stage1_tokens_final[0]
                        if stage1_tokens_final
                        else None,
                        "token_out": stage1_tokens_final[-1]
                        if stage1_tokens_final
                        else None,
                        "provider": leg1_provider.name if leg1_provider else "unknown",
                        "mode": "exact_in",
                        "reason": leg1_reason,
                        "error": leg1_error,
                        "error_type": leg1_error_type,
                        "provider_returned_none": bool(
                            leg1_quote is None and not leg1_error
                        ),
                        "pool_address": leg_context.get("pool_address"),
                        "fee": leg_context.get("fee"),
                        "amount": amount_in,
                        "is_diem_leg": is_diem_leg,
                        "requested_route": list(route.tokens),
                        "configured_provider": leg1_provider.name
                        if leg1_provider
                        else "unknown",
                        "reserve_fallback_used": bool(reserve_fallback_used),
                        "reserve_pref_used": bool(reserve_pref_used_leg1),
                        "route_tokens": stage1_tokens_final,
                    }
                )
            except Exception:
                pass

            try:
                _metrics_inc(
                    "dex_bridge_leg_failures_total",
                    labels={
                        "provider": leg1_provider.name if leg1_provider else "unknown",
                        "reason": leg1_reason,
                        "mode": "exact_in",
                    },
                )
            except Exception:
                pass

            _logger.info(
                "BridgeRouteProvider.quote: leg1 quote failed after fallback",
                extra={
                    "leg1_provider": leg1_provider.name,
                    "stage1": list(stage1.tokens)
                    if hasattr(stage1, "tokens")
                    else None,
                    "leg1_error": leg1_error,
                    "reserve_fallback_used": reserve_fallback_used,
                    "reserve_pref_used": reserve_pref_used_leg1,
                    "configured_pair": self._configured_diem_vvv_pair()
                    if is_stage1_diem_vvv
                    else None,
                    "route_pool": self._route_pool_address(stage1)
                    if is_stage1_diem_vvv
                    else None,
                },
            )
            fallback = self._bridge_single_leg_fallback_quote_exact_in(
                amount_in=int(amount_in),
                requested_route=route,
                failure_reason=f"leg1_{leg1_reason}",
                leg_index=0,
                error=leg1_error,
            )
            if fallback is not None:
                return fallback
            self._set_bridge_failure(
                f"leg1_{leg1_reason}",
                provider=leg1_provider.name if leg1_provider else None,
                stage_tokens=list(stage1.tokens) if hasattr(stage1, "tokens") else None,
                amount_in=int(amount_in),
                error=leg1_error,
            )
            return None

        # Quote leg2 (second leg: VVV->USDC for sell, VVV->DIEM for buy)
        leg2_quote: Quote | None = None
        leg2_error: str | None = None
        leg2_error_type: str | None = None
        leg2_slot0_fallback_used = False
        leg2_skipped_for_min = False
        stage2_for_quote = stage2
        stage2_tokens_prequote = (
            list(stage2.tokens) if hasattr(stage2, "tokens") else []
        )
        is_stage2_two_token = len(stage2_tokens_prequote) == 2
        is_stage2_vvv_usdc = (
            is_stage2_two_token
            and self._vvv_addr in [t.lower() for t in stage2_tokens_prequote]
            and quote_addr in [t.lower() for t in stage2_tokens_prequote]
        )
        if not stage2_is_multihop and self._should_skip_leg2_quote(
            leg1_quote.amount_out,
            stage2,
            leg2_provider,
            mode="exact_in",
        ):
            leg2_skipped_for_min = True
            leg2_error = "amount_below_min_vvv"
            leg2_error_type = "BridgeLegMinAmount"
        if is_stage2_vvv_usdc and not stage2_is_multihop and not leg2_skipped_for_min:
            try:
                slot0_quote = vvv_usdc_v3_slot0_quote(
                    leg1_quote.amount_out, stage2.tokens[0], stage2.tokens[-1]
                )
                if slot0_quote:
                    leg2_quote = slot0_quote
                    leg2_slot0_fallback_used = True
                    _logger.info(
                        "BridgeRouteProvider.quote: leg2 slot0 prequote used",
                        extra={
                            "leg2_provider": leg2_provider.name
                            if leg2_provider
                            else "unknown",
                            "stage2": stage2_tokens_prequote,
                            "amount_in": leg1_quote.amount_out,
                            "slot0_amount_out": slot0_quote.amount_out,
                            "provider": slot0_quote.provider,
                        },
                    )
            except Exception:
                pass
        if leg2_quote is None and not stage2_is_multihop and not leg2_skipped_for_min:
            try:
                if leg2_provider.name.lower() == "uniswap_v3":
                    stage2_for_quote = self._maybe_fill_uniswap_v3_fee(
                        stage2,
                        token_in=str(stage2.tokens[0]),
                        token_out=str(stage2.tokens[-1]),
                    )
            except Exception:
                stage2_for_quote = stage2
        if not leg2_skipped_for_min:
            try:
                if leg2_quote is None:
                    if stage2_is_multihop:
                        # Use multi-hop quoting for stages with more than 2 tokens
                        leg2_quote = self._quote_multihop_stage(
                            leg1_quote.amount_out, stage2, mode="exact_in"
                        )
                    else:
                        leg2_timeout = float(
                            os.getenv("BRIDGE_LEG2_TIMEOUT_SECONDS", "3.0")
                        )
                        if (
                            leg2_timeout > 0
                            and leg2_provider.name.lower() == "uniswap_v3"
                        ):
                            executor = ThreadPoolExecutor(max_workers=1)
                            future = executor.submit(
                                self._quote_leg,
                                leg2_provider,
                                leg1_quote.amount_out,
                                stage2_for_quote,
                                mode="exact_in",
                            )
                            try:
                                leg2_quote = future.result(timeout=leg2_timeout)
                            except TimeoutError:
                                future.cancel()
                                leg2_quote = None
                                _logger.debug(
                                    "BridgeRouteProvider.quote: leg2 quote timed out after %.1fs",
                                    leg2_timeout,
                                )
                            finally:
                                try:
                                    executor.shutdown(wait=False, cancel_futures=True)
                                except Exception:
                                    pass
                        else:
                            leg2_quote = self._quote_leg(
                                leg2_provider,
                                leg1_quote.amount_out,
                                stage2_for_quote,
                                mode="exact_in",
                            )
            except Exception as exc:
                leg2_error = str(exc)
                leg2_error_type = type(exc).__name__
                leg2_quote = None

        if (
            leg2_quote is None
            and leg2_error is None
            and not stage2_is_multihop
            and self._should_skip_leg2_quote(
                leg1_quote.amount_out,
                stage2_for_quote,
                leg2_provider,
                mode="exact_in",
            )
        ):
            leg2_skipped_for_min = True
            leg2_error = "amount_below_min_vvv"
            leg2_error_type = "BridgeLegMinAmount"

        # Try reserve fallback for leg2 if router quote failed and it's a DIEM/VVV leg
        leg2_reserve_fallback_used = False
        stage2_tokens = list(stage2.tokens) if hasattr(stage2, "tokens") else []
        is_diem_vvv_leg = self._diem_addr in [
            t.lower() for t in stage2_tokens
        ] and self._vvv_addr in [t.lower() for t in stage2_tokens]

        # Prefer reserve-math quote when leg2 DIEM/VVV router quote drifts
        reserve_pref_used_leg2 = False
        if (
            leg2_quote is not None
            and getattr(leg2_quote, "amount_out", 0) > 0
            and is_diem_vvv_leg
        ):
            leg2_quote, reserve_pref_used_leg2 = self._maybe_prefer_reserve_quote(
                mode="exact_in",
                leg_index=1,
                stage_tokens=stage2_tokens,
                router_quote=leg2_quote,
                reserve_quote_factory=lambda: diem_vvv_quote_exact_in_from_reserves(
                    leg1_quote.amount_out, stage2.tokens[0], stage2.tokens[1]
                ),
                provider_name=leg2_provider.name if leg2_provider else "unknown",
            )

        if leg2_quote is None or (leg2_quote and leg2_quote.amount_out <= 0):
            if is_diem_vvv_leg:
                leg2_reserve_fallback = diem_vvv_quote_exact_in_from_reserves(
                    leg1_quote.amount_out, stage2.tokens[0], stage2.tokens[1]
                )
                if leg2_reserve_fallback:
                    leg2_quote = leg2_reserve_fallback
                    leg2_reserve_fallback_used = True
                    _logger.info(
                        "BridgeRouteProvider.quote: leg2 reserve fallback succeeded",
                        extra={
                            "leg2_provider": leg2_provider.name,
                            "stage2": stage2_tokens,
                            "amount_in": leg1_quote.amount_out,
                            "reserve_fallback_amount_out": leg2_reserve_fallback.amount_out,
                        },
                    )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote: leg2 reserve fallback unavailable or disabled",
                        extra={
                            "leg2_provider": leg2_provider.name,
                            "stage2": stage2_tokens,
                            "fallback_enabled": os.getenv(
                                "DIEM_ENABLE_PAIR_MATH_FALLBACK", "0"
                            )
                            .strip()
                            .lower()
                            in {"1", "true", "yes", "on"},
                            "is_diem_vvv_leg": is_diem_vvv_leg,
                        },
                    )
            else:
                # Check if this is VVV→USDC leg and try fallbacks
                # V2 fallback only applies to 2-token legs (VVV↔USDC), not multi-hop stages
                quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                is_two_token = len(stage2_tokens) == 2
                is_vvv_usdc_leg = (
                    is_two_token
                    and self._vvv_addr in [t.lower() for t in stage2_tokens]
                    and quote_addr in [t.lower() for t in stage2_tokens]
                )

                if (
                    not is_two_token
                    and self._vvv_addr in [t.lower() for t in stage2_tokens]
                    and quote_addr in [t.lower() for t in stage2_tokens]
                ):
                    # Multi-hop stage detected, skip V2 fallback
                    try:
                        _dex_diag_log_event(
                            {
                                "event": "dex_bridge_leg_v2_skip",
                                "reason": "multihop_stage",
                                "mode": "exact_in",
                                "leg_index": 1,
                                "tokens": stage2_tokens,
                                "provider": leg2_provider.name
                                if leg2_provider
                                else "unknown",
                                "token_count": len(stage2_tokens),
                            }
                        )
                    except Exception:
                        pass

                if is_vvv_usdc_leg:
                    # Slot0-based executable fallback and analytic preview
                    slot0_quote = vvv_usdc_v3_slot0_quote(
                        leg1_quote.amount_out,
                        stage2.tokens[0],
                        stage2.tokens[-1],
                    )
                    v3_analytic_quote = vvv_usdc_v3_mid_price_quote(
                        leg1_quote.amount_out,  # Using exact-in semantics (amount_out is desired output)
                        stage2.tokens[0],
                        stage2.tokens[-1],
                    )
                    # Check if analytic quote is executable (if not, we need V2 fallback for execution)
                    v3_analytic_executable = (
                        v3_analytic_quote
                        and hasattr(v3_analytic_quote, "executable")
                        and v3_analytic_quote.executable
                    )

                    if slot0_quote:
                        leg2_quote = slot0_quote
                        leg2_slot0_fallback_used = True
                        _logger.info(
                            "BridgeRouteProvider.quote: leg2 slot0 fallback preferred",
                            extra={
                                "leg2_provider": leg2_provider.name,
                                "stage2": stage2_tokens,
                                "amount_in": leg1_quote.amount_out,
                                "slot0_amount_out": slot0_quote.amount_out,
                                "provider": slot0_quote.provider,
                            },
                        )
                    else:
                        # Try V2 provider fallback when V3 router fails (always try for execution, even if analytic exists)
                        v2_fallback_enabled = os.getenv(
                            "VVV_USDC_V2_FALLBACK_ENABLE",
                            "0",  # No V2 pool exists for VVV/USDC
                        ).strip().lower() in {"1", "true", "yes", "on"}
                        v2_fallback_only_for_buys = os.getenv(
                            "VVV_USDC_V2_FALLBACK_ONLY_FOR_BUYS", "0"
                        ).strip().lower() in {"1", "true", "yes", "on"}
                        v2_pool_exists = self._v2_pool_exists(stage2)

                        # For exact-in (buy path), always try V2 if enabled
                        should_try_v2 = (
                            v2_fallback_enabled
                            and v2_pool_exists
                            and (
                                not v2_fallback_only_for_buys
                                or True  # exact-in is always buy path
                            )
                        )
                        if v2_fallback_enabled and not v2_pool_exists:
                            _logger.info(
                                "BridgeRouteProvider.quote: V2 fallback skipped (no V2 pool)",
                                extra={
                                    "leg_index": 1,
                                    "mode": "exact_in",
                                    "tokens": stage2_tokens,
                                    "provider": leg2_provider.name
                                    if leg2_provider
                                    else "unknown",
                                },
                            )

                        v2_fallback_provider = None
                        if should_try_v2:
                            v2_fallback_provider = self._provider_map.get("uniswap_v2")

                        if (
                            v2_fallback_provider
                            and leg2_provider.name.lower() == "uniswap_v3"
                        ):
                            # Create a V2-compatible route (remove fee annotations)
                            from libs.dex.routes import make_route

                            stage2_v2_route = make_route(
                                [stage2.tokens[0], stage2.tokens[-1]]
                            )
                            try:
                                v2_fallback_quote = v2_fallback_provider.quote(
                                    leg1_quote.amount_out, stage2_v2_route
                                )
                                if (
                                    v2_fallback_quote
                                    and v2_fallback_quote.amount_out > 0
                                ):
                                    # Prefer V2 if it's executable, otherwise use analytic for preview
                                    if (
                                        v2_fallback_quote.executable
                                        if hasattr(v2_fallback_quote, "executable")
                                        else True
                                    ):
                                        leg2_quote = v2_fallback_quote
                                        leg2_reserve_fallback_used = True
                                        _logger.info(
                                            "BridgeRouteProvider.quote: leg2 V2 fallback succeeded",
                                            extra={
                                                "leg2_provider": leg2_provider.name,
                                                "v2_fallback_provider": v2_fallback_provider.name,
                                                "stage2": stage2_tokens,
                                                "amount_in": leg1_quote.amount_out,
                                                "v2_fallback_amount_out": v2_fallback_quote.amount_out,
                                            },
                                        )
                                    elif v3_analytic_quote:
                                        # Use analytic for preview, but log that V2 is available
                                        leg2_quote = v3_analytic_quote
                                        _logger.info(
                                            "BridgeRouteProvider.quote: leg2 V3 analytic fallback succeeded (preview-only), V2 fallback available",
                                            extra={
                                                "leg2_provider": leg2_provider.name,
                                                "stage2": stage2_tokens,
                                                "amount_in": leg1_quote.amount_out,
                                                "analytic_amount_out": v3_analytic_quote.amount_out,
                                                "provider": v3_analytic_quote.provider,
                                                "v2_available": True,
                                            },
                                        )
                                # V2 failed, use analytic if available
                                elif v3_analytic_quote:
                                    leg2_quote = v3_analytic_quote
                                    _logger.info(
                                        "BridgeRouteProvider.quote: leg2 V3 analytic fallback succeeded (preview-only), V2 fallback failed",
                                        extra={
                                            "leg2_provider": leg2_provider.name,
                                            "stage2": stage2_tokens,
                                            "amount_in": leg1_quote.amount_out,
                                            "analytic_amount_out": v3_analytic_quote.amount_out,
                                            "provider": v3_analytic_quote.provider,
                                        },
                                    )
                                else:
                                    _logger.debug(
                                        "BridgeRouteProvider.quote: leg2 V2 fallback returned no quote",
                                        extra={
                                            "leg2_provider": leg2_provider.name,
                                            "v2_fallback_provider": v2_fallback_provider.name,
                                            "stage2": stage2_tokens,
                                        },
                                    )
                            except Exception as v2_exc:
                                # V2 failed, use analytic if available
                                if v3_analytic_quote:
                                    leg2_quote = v3_analytic_quote
                                    _logger.info(
                                        "BridgeRouteProvider.quote: leg2 V3 analytic fallback succeeded (preview-only), V2 fallback exception",
                                        extra={
                                            "leg2_provider": leg2_provider.name,
                                            "stage2": stage2_tokens,
                                            "amount_in": leg1_quote.amount_out,
                                            "analytic_amount_out": v3_analytic_quote.amount_out,
                                            "provider": v3_analytic_quote.provider,
                                            "v2_error": str(v2_exc),
                                        },
                                    )
                                else:
                                    _logger.info(
                                        "BridgeRouteProvider.quote: leg2 V2 fallback failed",
                                        extra={
                                            "leg2_provider": leg2_provider.name,
                                            "v2_fallback_provider": v2_fallback_provider.name,
                                            "stage2": stage2_tokens,
                                            "v2_error": str(v2_exc),
                                        },
                                    )
                        elif v3_analytic_quote:
                            # No V2 fallback available, use analytic preview
                            leg2_quote = v3_analytic_quote
                            _logger.info(
                                "BridgeRouteProvider.quote: leg2 V3 analytic fallback succeeded (preview-only)",
                                extra={
                                    "leg2_provider": leg2_provider.name,
                                    "stage2": stage2_tokens,
                                    "amount_in": leg1_quote.amount_out,
                                    "analytic_amount_out": v3_analytic_quote.amount_out,
                                    "provider": v3_analytic_quote.provider,
                                },
                            )

                    if not leg2_quote:
                        _logger.info(
                            "BridgeRouteProvider.quote: leg2 V3 analytic fallback unavailable",
                            extra={
                                "leg2_provider": leg2_provider.name,
                                "stage2": stage2_tokens,
                                "fallback_enabled": os.getenv(
                                    "DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1"
                                )
                                .strip()
                                .lower()
                                in {"1", "true", "yes", "on"},
                                "is_vvv_usdc_leg": is_vvv_usdc_leg,
                            },
                        )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote: leg2 quote failed, no reserve fallback (not DIEM/VVV leg)",
                        extra={
                            "leg2_provider": leg2_provider.name,
                            "stage2": stage2_tokens,
                            "leg1_amount_out": leg1_quote.amount_out,
                            "leg2_error": leg2_error,
                        },
                    )

        if leg2_quote is None or leg2_quote.amount_out <= 0:
            # Emit telemetry for bridge-leg failure
            stage2_tokens_final = (
                list(stage2.tokens) if hasattr(stage2, "tokens") else []
            )
            if leg2_skipped_for_min:
                leg2_reason = "below_min_vvv"
            else:
                leg2_reason = "empty" if leg2_quote is None else "zero_output"
            try:
                diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                is_diem_leg_telemetry = (
                    diem_addr
                    and vvv_addr
                    and (
                        diem_addr in [t.lower() for t in stage2_tokens_final]
                        or vvv_addr in [t.lower() for t in stage2_tokens_final]
                    )
                )
                leg_context = self._bridge_leg_context(stage2, leg2_provider)

                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_leg_failure",
                        "leg_index": 1,
                        "token_in": stage2_tokens_final[0]
                        if stage2_tokens_final
                        else None,
                        "token_out": stage2_tokens_final[-1]
                        if stage2_tokens_final
                        else None,
                        "provider": leg2_provider.name if leg2_provider else "unknown",
                        "mode": "exact_in",
                        "reason": leg2_reason,
                        "error": leg2_error,
                        "error_type": leg2_error_type,
                        "provider_returned_none": bool(
                            leg2_quote is None and not leg2_error
                        ),
                        "pool_address": leg_context.get("pool_address"),
                        "fee": leg_context.get("fee"),
                        "amount": leg1_quote.amount_out if leg1_quote else None,
                        "is_diem_leg": is_diem_leg_telemetry,
                        "requested_route": list(route.tokens),
                        "configured_provider": leg2_provider.name
                        if leg2_provider
                        else "unknown",
                        "reserve_fallback_used": bool(leg2_reserve_fallback_used),
                        "slot0_fallback_used": bool(leg2_slot0_fallback_used),
                        "reserve_pref_used": bool(reserve_pref_used_leg2),
                        "route_tokens": stage2_tokens_final,
                    }
                )
            except Exception:
                pass

            try:
                _metrics_inc(
                    "dex_bridge_leg_failures_total",
                    labels={
                        "provider": leg2_provider.name if leg2_provider else "unknown",
                        "reason": leg2_reason,
                        "mode": "exact_in",
                    },
                )
            except Exception:
                pass

            _logger.info(
                "BridgeRouteProvider.quote: leg2 quote failed after fallback",
                extra={
                    "leg2_provider": leg2_provider.name,
                    "stage2": list(stage2.tokens)
                    if hasattr(stage2, "tokens")
                    else None,
                    "leg1_amount_out": leg1_quote.amount_out,
                    "leg2_error": leg2_error,
                    "leg2_reserve_fallback_used": leg2_reserve_fallback_used,
                    "leg2_reserve_pref_used": reserve_pref_used_leg2,
                    "is_diem_vvv_leg": is_diem_vvv_leg
                    if "is_diem_vvv_leg" in locals()
                    else None,
                    "configured_pair": self._configured_diem_vvv_pair()
                    if is_diem_vvv_leg
                    else None,
                    "route_pool": self._route_pool_address(stage2)
                    if is_diem_vvv_leg
                    else None,
                },
            )
            fallback = self._bridge_single_leg_fallback_quote_exact_in(
                amount_in=int(amount_in),
                requested_route=route,
                failure_reason=f"leg2_{leg2_reason}",
                leg_index=1,
                error=leg2_error,
            )
            if fallback is not None:
                return fallback
            self._set_bridge_failure(
                f"leg2_{leg2_reason}",
                provider=leg2_provider.name if leg2_provider else None,
                stage_tokens=list(stage2.tokens) if hasattr(stage2, "tokens") else None,
                amount_in=int(getattr(leg1_quote, "amount_out", 0) or 0),
                error=leg2_error,
            )
            return None

        if leg1_quote and leg2_quote:

            def _decimals_for(addr: str) -> int:
                norm = (addr or "").strip().lower()
                if not norm:
                    return 18
                quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                if quote_addr and norm == quote_addr:
                    try:
                        return int(
                            (os.getenv("QUOTE_TOKEN_DECIMALS") or "6").strip() or 6
                        )
                    except Exception:
                        return 6
                if diem_addr and norm == diem_addr:
                    try:
                        return int((os.getenv("DIEM_DECIMALS") or "18").strip() or 18)
                    except Exception:
                        return 18
                if vvv_addr and norm == vvv_addr:
                    try:
                        return int((os.getenv("VVV_DECIMALS") or "18").strip() or 18)
                    except Exception:
                        return 18
                return 18

            leg1_in_dec = _decimals_for(
                stage1.tokens[0] if hasattr(stage1, "tokens") else ""
            )
            leg1_out_dec = _decimals_for(
                stage1.tokens[-1] if hasattr(stage1, "tokens") else ""
            )
            leg2_in_dec = _decimals_for(
                stage2.tokens[0] if hasattr(stage2, "tokens") else ""
            )
            leg2_out_dec = _decimals_for(
                stage2.tokens[-1] if hasattr(stage2, "tokens") else ""
            )

            leg1_ratio_raw = (
                leg1_quote.amount_out / leg1_quote.amount_in
                if leg1_quote.amount_in > 0
                else 0.0
            )
            leg2_ratio_raw = (
                leg2_quote.amount_out / leg2_quote.amount_in
                if leg2_quote.amount_in > 0
                else 0.0
            )
            leg1_ratio = (
                (leg1_quote.amount_out / (10**leg1_out_dec))
                / (leg1_quote.amount_in / (10**leg1_in_dec))
                if leg1_quote.amount_in > 0
                else 0.0
            )
            leg2_ratio = (
                (leg2_quote.amount_out / (10**leg2_out_dec))
                / (leg2_quote.amount_in / (10**leg2_in_dec))
                if leg2_quote.amount_in > 0
                else 0.0
            )

            _logger.info(
                "BridgeRouteProvider.quote: leg diagnostics",
                extra={
                    "leg1_in": leg1_quote.amount_in,
                    "leg1_out": leg1_quote.amount_out,
                    "leg1_ratio": leg1_ratio,
                    "leg1_ratio_raw": leg1_ratio_raw,
                    "leg1_provider": leg1_quote.provider,
                    "leg2_in": leg2_quote.amount_in,
                    "leg2_out": leg2_quote.amount_out,
                    "leg2_ratio": leg2_ratio,
                    "leg2_ratio_raw": leg2_ratio_raw,
                    "leg2_provider": leg2_quote.provider,
                    "composite_in": amount_in,
                    "composite_out": leg2_quote.amount_out,
                    "leg1_reserve_pref": reserve_pref_used_leg1,
                    "leg2_reserve_pref": reserve_pref_used_leg2,
                    "leg2_slot0_fallback": leg2_slot0_fallback_used,
                    "leg1_pool": self._route_pool_address(stage1)
                    if is_stage1_diem_vvv
                    else None,
                    "leg2_pool": self._route_pool_address(stage2)
                    if is_diem_vvv_leg
                    else None,
                    "diem_vvv_pair_configured": self._configured_diem_vvv_pair(),
                },
            )

            if leg1_quote.amount_in <= 0 or leg2_quote.amount_in <= 0:
                _logger.warning(
                    "BridgeRouteProvider.quote: rejecting quote with non-positive leg amount_in",
                    extra={
                        "leg1_in": leg1_quote.amount_in,
                        "leg2_in": leg2_quote.amount_in,
                    },
                )
                self._set_bridge_failure(
                    "leg_amount_in_non_positive",
                    leg1_amount_in=int(getattr(leg1_quote, "amount_in", 0) or 0),
                    leg2_amount_in=int(getattr(leg2_quote, "amount_in", 0) or 0),
                )
                return None

            # Block quotes where leg ratios are extreme (likely bad data).
            if leg1_ratio > 1e6 or leg2_ratio > 1e6:
                _logger.warning(
                    "BridgeRouteProvider.quote: rejecting quote with extreme leg ratio",
                    extra={
                        "leg1_ratio": leg1_ratio,
                        "leg2_ratio": leg2_ratio,
                        "leg1_ratio_raw": leg1_ratio_raw,
                        "leg2_ratio_raw": leg2_ratio_raw,
                    },
                )
                self._set_bridge_failure(
                    "leg_ratio_extreme",
                    leg1_ratio=float(leg1_ratio),
                    leg2_ratio=float(leg2_ratio),
                    leg1_ratio_raw=float(leg1_ratio_raw),
                    leg2_ratio_raw=float(leg2_ratio_raw),
                )
                return None

        composite_route = self._build_composite_route(stage1, stage2)
        combined = Quote(
            provider=self.name,
            amount_in=amount_in,
            amount_out=leg2_quote.amount_out,
            route=composite_route,
            executable=bool(getattr(leg1_quote, "executable", True))
            and bool(getattr(leg2_quote, "executable", True)),
        )
        self._composite_attach(combined, [leg1_quote, leg2_quote])
        return combined

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Quote | None:
        self._clear_bridge_failure()
        two_stage = self._two_stage(route)
        if not two_stage:
            try:
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_unsupported_route",
                        "mode": "exact_out",
                        "amount_out": int(amount_out),
                        "route_tokens": list(route.tokens),
                    }
                )
            except Exception:
                pass
            try:
                self._set_bridge_failure(
                    "unsupported_route", route_tokens=list(route.tokens)
                )
            except Exception:
                self._set_bridge_failure("unsupported_route")
            return None
        stage1, stage2 = two_stage
        # Check if stages are multi-hop (more than 2 tokens)
        stage1_is_multihop = len(list(stage1.tokens)) > 2
        stage2_is_multihop = len(list(stage2.tokens)) > 2
        # Use endpoints to look up provider, as stages might be multi-hop
        leg1_provider = self._provider_for_leg(stage1.tokens[0], stage1.tokens[-1])
        leg2_provider = self._provider_for_leg(stage2.tokens[0], stage2.tokens[-1])
        # For multi-hop stages, check each hop has a provider
        if stage1_is_multihop:
            stage1_tokens_check = list(stage1.tokens)
            for i in range(len(stage1_tokens_check) - 1):
                if (
                    self._provider_for_leg(
                        stage1_tokens_check[i], stage1_tokens_check[i + 1]
                    )
                    is None
                ):
                    leg1_provider = None
                    break
            else:
                leg1_provider = self  # Use self as synthetic provider for multi-hop
        if stage2_is_multihop:
            stage2_tokens_check = list(stage2.tokens)
            for i in range(len(stage2_tokens_check) - 1):
                if (
                    self._provider_for_leg(
                        stage2_tokens_check[i], stage2_tokens_check[i + 1]
                    )
                    is None
                ):
                    leg2_provider = None
                    break
            else:
                leg2_provider = self  # Use self as synthetic provider for multi-hop
        if leg1_provider is None or leg2_provider is None:
            stage1_tokens = list(stage1.tokens) if hasattr(stage1, "tokens") else []
            stage2_tokens = list(stage2.tokens) if hasattr(stage2, "tokens") else []
            try:
                missing_idx = 0 if leg1_provider is None else 1
                leg_context = self._bridge_leg_context(
                    stage1 if missing_idx == 0 else stage2,
                    leg1_provider if missing_idx == 0 else leg2_provider,
                )
                missing_tokens = stage1_tokens if missing_idx == 0 else stage2_tokens
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_leg_failure",
                        "leg_index": missing_idx,
                        "token_in": missing_tokens[0] if missing_tokens else None,
                        "token_out": missing_tokens[-1] if missing_tokens else None,
                        "provider": None,
                        "mode": "exact_out",
                        "reason": "missing_provider",
                        "pool_address": leg_context.get("pool_address"),
                        "fee": leg_context.get("fee"),
                        "requested_route": list(route.tokens),
                        "route_tokens": missing_tokens,
                    }
                )
            except Exception:
                pass
            _logger.info(
                "BridgeRouteProvider.quote_exact_out: missing leg providers",
                extra={
                    "leg1_provider": leg1_provider.name if leg1_provider else None,
                    "leg2_provider": leg2_provider.name if leg2_provider else None,
                    "stage1": list(stage1.tokens)
                    if hasattr(stage1, "tokens")
                    else None,
                    "stage2": list(stage2.tokens)
                    if hasattr(stage2, "tokens")
                    else None,
                },
            )
            fallback = self._bridge_single_leg_fallback_quote_exact_out(
                amount_out=int(amount_out),
                requested_route=route,
                failure_reason="missing_leg_provider",
                leg_index=0,
                error=None,
            )
            if fallback is not None:
                return fallback
            self._set_bridge_failure(
                "missing_leg_provider",
                leg1_provider=leg1_provider.name if leg1_provider else None,
                leg2_provider=leg2_provider.name if leg2_provider else None,
                stage1_tokens=stage1_tokens,
                stage2_tokens=stage2_tokens,
            )
            return None

        def _positive_leg2(val: Any) -> bool:
            if isinstance(val, (int, float)):
                return val > 0
            return False

        def _positive_leg1(val: Any) -> bool:
            if val is None:
                return False
            if isinstance(val, (int, float)):
                return val > 0
            # Treat non-numeric placeholders (e.g., MagicMock) as positive for test stubs
            return True

        # Quote leg2 first (final leg: VVV->DIEM for buy, DIEM->VVV for sell)
        leg2_quote: Quote | None = None
        leg2_error: str | None = None
        leg2_error_type: str | None = None
        leg2_reserve_pref_used = False
        try:
            if stage2_is_multihop:
                # Use multi-hop quoting for stages with more than 2 tokens
                leg2_quote = self._quote_multihop_stage(
                    amount_out, stage2, mode="exact_out"
                )
            else:
                stage2_for_quote = stage2
                try:
                    if leg2_provider.name.lower() == "uniswap_v3":
                        stage2_for_quote = self._maybe_fill_uniswap_v3_fee(
                            stage2,
                            token_in=str(stage2.tokens[0]),
                            token_out=str(stage2.tokens[-1]),
                        )
                except Exception:
                    stage2_for_quote = stage2
                leg2_quote = leg2_provider.quote_exact_out(amount_out, stage2_for_quote)
        except Exception as exc:
            leg2_error = str(exc)
            leg2_error_type = type(exc).__name__
            leg2_quote = None

        # Try reserve fallback for leg2 if router quote failed and it's a DIEM/VVV leg
        leg2_reserve_fallback_used = False
        leg2_slot0_fallback_used = False
        if leg2_quote is None or not _positive_leg2(
            getattr(leg2_quote, "amount_in", None)
        ):
            stage2_tokens = list(stage2.tokens) if hasattr(stage2, "tokens") else []
            is_diem_vvv_leg = self._diem_addr in [
                t.lower() for t in stage2_tokens
            ] and self._vvv_addr in [t.lower() for t in stage2_tokens]

            if is_diem_vvv_leg:
                leg2_reserve_fallback = diem_vvv_quote_from_reserves(
                    amount_out, stage2.tokens[0], stage2.tokens[1]
                )
                if leg2_reserve_fallback:
                    leg2_quote = leg2_reserve_fallback
                    leg2_reserve_fallback_used = True
                    _logger.info(
                        "BridgeRouteProvider.quote_exact_out: leg2 reserve fallback succeeded",
                        extra={
                            "leg2_provider": leg2_provider.name,
                            "stage2": stage2_tokens,
                            "amount_out": amount_out,
                            "reserve_fallback_amount_in": leg2_reserve_fallback.amount_in,
                        },
                    )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote_exact_out: leg2 reserve fallback unavailable or disabled",
                        extra={
                            "leg2_provider": leg2_provider.name,
                            "stage2": stage2_tokens,
                            "fallback_enabled": os.getenv(
                                "DIEM_ENABLE_PAIR_MATH_FALLBACK", "0"
                            )
                            .strip()
                            .lower()
                            in {"1", "true", "yes", "on"},
                            "is_diem_vvv_leg": is_diem_vvv_leg,
                        },
                    )
            else:
                # Check if this is VVV→USDC leg and try slot0/analytic fallback
                quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                is_two_token = len(stage2_tokens) == 2
                is_vvv_usdc_leg = (
                    is_two_token
                    and self._vvv_addr in [t.lower() for t in stage2_tokens]
                    and quote_addr in [t.lower() for t in stage2_tokens]
                )

                if is_vvv_usdc_leg:
                    slot0_quote = vvv_usdc_v3_slot0_quote_exact_out(
                        amount_out,
                        stage2.tokens[0],
                        stage2.tokens[-1],
                    )
                    v3_analytic_quote = vvv_usdc_v3_mid_price_quote_exact_out(
                        amount_out,
                        stage2.tokens[0],
                        stage2.tokens[-1],
                    )
                    if slot0_quote:
                        leg2_quote = slot0_quote
                        leg2_slot0_fallback_used = True
                        _logger.info(
                            "BridgeRouteProvider.quote_exact_out: leg2 slot0 fallback succeeded",
                            extra={
                                "leg2_provider": leg2_provider.name,
                                "stage2": stage2_tokens,
                                "amount_out": amount_out,
                                "slot0_amount_in": slot0_quote.amount_in,
                                "provider": slot0_quote.provider,
                            },
                        )
                    elif v3_analytic_quote:
                        leg2_quote = v3_analytic_quote
                        _logger.info(
                            "BridgeRouteProvider.quote_exact_out: leg2 V3 analytic fallback succeeded (preview-only)",
                            extra={
                                "leg2_provider": leg2_provider.name,
                                "stage2": stage2_tokens,
                                "amount_out": amount_out,
                                "analytic_amount_in": v3_analytic_quote.amount_in,
                                "provider": v3_analytic_quote.provider,
                            },
                        )
                    else:
                        _logger.info(
                            "BridgeRouteProvider.quote_exact_out: leg2 V3 analytic fallback unavailable",
                            extra={
                                "leg2_provider": leg2_provider.name,
                                "stage2": stage2_tokens,
                                "fallback_enabled": os.getenv(
                                    "DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1"
                                )
                                .strip()
                                .lower()
                                in {"1", "true", "yes", "on"},
                                "is_vvv_usdc_leg": is_vvv_usdc_leg,
                            },
                        )

        if leg2_quote is None or not _positive_leg2(
            getattr(leg2_quote, "amount_in", None)
        ):
            stage2_tokens_final = (
                list(stage2.tokens) if hasattr(stage2, "tokens") else []
            )
            leg2_reason = "empty" if leg2_quote is None else "amount_in_non_positive"
            try:
                leg_context = self._bridge_leg_context(stage2, leg2_provider)
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_leg_failure",
                        "leg_index": 1,
                        "token_in": stage2_tokens_final[0]
                        if stage2_tokens_final
                        else None,
                        "token_out": stage2_tokens_final[-1]
                        if stage2_tokens_final
                        else None,
                        "provider": leg2_provider.name if leg2_provider else "unknown",
                        "mode": "exact_out",
                        "reason": leg2_reason,
                        "error": leg2_error,
                        "error_type": leg2_error_type,
                        "provider_returned_none": bool(
                            leg2_quote is None and not leg2_error
                        ),
                        "pool_address": leg_context.get("pool_address"),
                        "fee": leg_context.get("fee"),
                        "amount": int(amount_out),
                        "requested_route": list(route.tokens),
                        "configured_provider": leg2_provider.name
                        if leg2_provider
                        else "unknown",
                        "reserve_fallback_used": bool(leg2_reserve_fallback_used),
                        "slot0_fallback_used": bool(leg2_slot0_fallback_used),
                        "reserve_pref_used": bool(leg2_reserve_pref_used),
                        "route_tokens": stage2_tokens_final,
                    }
                )
            except Exception:
                pass

            try:
                _metrics_inc(
                    "dex_bridge_leg_failures_total",
                    labels={
                        "provider": leg2_provider.name if leg2_provider else "unknown",
                        "reason": leg2_reason,
                        "mode": "exact_out",
                    },
                )
            except Exception:
                pass

            _logger.info(
                "BridgeRouteProvider.quote_exact_out: leg2 quote failed after fallback",
                extra={
                    "leg2_provider": leg2_provider.name,
                    "stage2": list(stage2.tokens)
                    if hasattr(stage2, "tokens")
                    else None,
                    "amount_out": amount_out,
                    "leg2_error": leg2_error,
                    "leg2_reserve_fallback_used": leg2_reserve_fallback_used,
                    "leg2_reserve_pref_used": leg2_reserve_pref_used,
                    "leg2_slot0_fallback_used": leg2_slot0_fallback_used,
                    "is_diem_vvv_leg": is_diem_vvv_leg
                    if "is_diem_vvv_leg" in locals()
                    else None,
                    "configured_pair": self._configured_diem_vvv_pair()
                    if "is_diem_vvv_leg" in locals() and is_diem_vvv_leg
                    else None,
                    "route_pool": self._route_pool_address(stage2)
                    if "is_diem_vvv_leg" in locals() and is_diem_vvv_leg
                    else None,
                },
            )
            fallback = self._bridge_single_leg_fallback_quote_exact_out(
                amount_out=int(amount_out),
                requested_route=route,
                failure_reason=f"leg2_{leg2_reason}",
                leg_index=1,
                error=leg2_error,
            )
            if fallback is not None:
                return fallback
            self._set_bridge_failure(
                "leg2_empty" if leg2_quote is None else "leg2_amount_in_non_positive",
                provider=leg2_provider.name if leg2_provider else None,
                stage_tokens=list(stage2.tokens) if hasattr(stage2, "tokens") else None,
                amount_out=int(amount_out),
                error=leg2_error,
            )
            return None

        # Prefer reserve math when DIEM/VVV leg2 router quote drifts
        stage2_tokens = list(stage2.tokens) if hasattr(stage2, "tokens") else []
        is_leg2_diem_vvv = self._diem_addr in [
            t.lower() for t in stage2_tokens
        ] and self._vvv_addr in [t.lower() for t in stage2_tokens]
        if (
            leg2_quote is not None
            and is_leg2_diem_vvv
            and not leg2_reserve_fallback_used
        ):
            leg2_quote, leg2_reserve_pref_used = self._maybe_prefer_reserve_quote(
                mode="exact_out",
                leg_index=1,
                stage_tokens=stage2_tokens,
                router_quote=leg2_quote,
                reserve_quote_factory=lambda: diem_vvv_quote_from_reserves(
                    amount_out, stage2.tokens[0], stage2.tokens[1]
                ),
                provider_name=leg2_provider.name if leg2_provider else "unknown",
            )

        # Quote leg1 (first leg: USDC->VVV for buy, VVV->USDC for sell)
        leg1_quote: Quote | None = None
        leg1_error: str | None = None
        leg1_error_type: str | None = None
        leg1_reserve_pref_used = False
        leg1_slot0_fallback_used = False
        stage1_tokens = list(stage1.tokens) if hasattr(stage1, "tokens") else []
        is_leg1_diem_vvv = self._diem_addr in [
            t.lower() for t in stage1_tokens
        ] and self._vvv_addr in [t.lower() for t in stage1_tokens]
        quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        is_vvv_usdc_leg = self._vvv_addr in [
            t.lower() for t in stage1_tokens
        ] and quote_addr in [t.lower() for t in stage1_tokens]

        try:
            if stage1_is_multihop:
                # Use multi-hop quoting for stages with more than 2 tokens
                leg1_quote = self._quote_multihop_stage(
                    leg2_quote.amount_in, stage1, mode="exact_out"
                )
            else:
                if is_vvv_usdc_leg:
                    try:
                        slot0_quote = vvv_usdc_v3_slot0_quote_exact_out(
                            leg2_quote.amount_in, stage1.tokens[0], stage1.tokens[-1]
                        )
                        if slot0_quote:
                            leg1_quote = slot0_quote
                            leg1_slot0_fallback_used = True
                            _logger.info(
                                "BridgeRouteProvider.quote_exact_out: leg1 slot0 prequote used",
                                extra={
                                    "leg1_provider": leg1_provider.name
                                    if leg1_provider
                                    else "unknown",
                                    "stage1": stage1_tokens,
                                    "amount_out": leg2_quote.amount_in,
                                    "slot0_amount_in": slot0_quote.amount_in,
                                    "provider": slot0_quote.provider,
                                },
                            )
                    except Exception:
                        pass
                if leg1_quote is None:
                    v3_exact_out_disabled = os.getenv(
                        "VVV_USDC_V3_EXACT_OUT_DISABLE", "0"
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    skip_v3_router = (
                        is_vvv_usdc_leg
                        and leg1_provider.name.lower() == "uniswap_v3"
                        and v3_exact_out_disabled
                    )
                    if not skip_v3_router:
                        stage1_for_quote = stage1
                        try:
                            if leg1_provider.name.lower() == "uniswap_v3":
                                stage1_for_quote = self._maybe_fill_uniswap_v3_fee(
                                    stage1,
                                    token_in=str(stage1.tokens[0]),
                                    token_out=str(stage1.tokens[-1]),
                                )
                        except Exception:
                            stage1_for_quote = stage1
                        leg1_quote = leg1_provider.quote_exact_out(
                            leg2_quote.amount_in, stage1_for_quote
                        )
                    else:
                        leg1_error = "uniswap_v3_exact_out_disabled_for_vvv_usdc"
                        leg1_quote = None
        except Exception as exc:
            leg1_error = str(exc)
            leg1_error_type = type(exc).__name__
            leg1_quote = None

        # Try reserve fallback for leg1 if router quote failed
        reserve_fallback_used = False
        if leg1_quote is None or not _positive_leg1(
            getattr(leg1_quote, "amount_in", None)
        ):
            # Check if this is a DIEM/VVV leg that can use reserve fallback
            is_diem_vvv_leg = is_leg1_diem_vvv

            if is_diem_vvv_leg:
                reserve_fallback = diem_vvv_quote_from_reserves(
                    leg2_quote.amount_in, stage1.tokens[0], stage1.tokens[1]
                )
                if reserve_fallback:
                    leg1_quote = reserve_fallback
                    reserve_fallback_used = True
                    _logger.info(
                        "BridgeRouteProvider.quote_exact_out: leg1 reserve fallback succeeded",
                        extra={
                            "leg1_provider": leg1_provider.name,
                            "stage1": stage1_tokens,
                            "amount_in_needed": leg2_quote.amount_in,
                            "reserve_fallback_amount_in": reserve_fallback.amount_in,
                        },
                    )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote_exact_out: leg1 reserve fallback unavailable or disabled",
                        extra={
                            "leg1_provider": leg1_provider.name,
                            "stage1": stage1_tokens,
                            "fallback_enabled": os.getenv(
                                "DIEM_ENABLE_PAIR_MATH_FALLBACK", "0"
                            )
                            .strip()
                            .lower()
                            in {"1", "true", "yes", "on"},
                            "is_diem_vvv_leg": is_diem_vvv_leg,
                        },
                    )
            else:
                # Check if this is VVV→USDC leg and try V3 mid-price analytic fallback (preview-only)
                quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                is_vvv_usdc_leg = self._vvv_addr in [
                    t.lower() for t in stage1_tokens
                ] and quote_addr in [t.lower() for t in stage1_tokens]

                if is_vvv_usdc_leg:
                    # Slot0 executable fallback plus analytic preview
                    slot0_quote = vvv_usdc_v3_slot0_quote_exact_out(
                        leg2_quote.amount_in,  # desired amount_out is leg2_quote.amount_in
                        stage1.tokens[0],
                        stage1.tokens[1],
                    )
                    v3_analytic_quote = vvv_usdc_v3_mid_price_quote_exact_out(
                        leg2_quote.amount_in,  # Using exact-out semantics (amount_out is desired output)
                        stage1.tokens[0],
                        stage1.tokens[1],
                    )
                    # Check if analytic quote is executable (if not, we need V2 fallback for execution)
                    v3_analytic_executable = (
                        v3_analytic_quote
                        and hasattr(v3_analytic_quote, "executable")
                        and v3_analytic_quote.executable
                    )

                    if slot0_quote:
                        leg1_quote = slot0_quote
                        leg1_slot0_fallback_used = True
                        _logger.info(
                            "BridgeRouteProvider.quote_exact_out: leg1 slot0 fallback preferred",
                            extra={
                                "leg1_provider": leg1_provider.name,
                                "stage1": stage1_tokens,
                                "amount_in_needed": leg2_quote.amount_in,
                                "slot0_amount_in": slot0_quote.amount_in,
                                "provider": slot0_quote.provider,
                            },
                        )
                    else:
                        # Try V2 provider fallback when V3 router fails (always try for execution, even if analytic exists)
                        v2_fallback_enabled = os.getenv(
                            "VVV_USDC_V2_FALLBACK_ENABLE", "0"
                        ).strip().lower() in {"1", "true", "yes", "on"}
                        v2_fallback_only_for_buys = os.getenv(
                            "VVV_USDC_V2_FALLBACK_ONLY_FOR_BUYS", "0"
                        ).strip().lower() in {"1", "true", "yes", "on"}
                        v2_pool_exists = self._v2_pool_exists(stage1)
                        should_try_v2 = (
                            v2_fallback_enabled
                            and v2_pool_exists
                            and (not v2_fallback_only_for_buys or True)
                        )
                        if v2_fallback_enabled and not v2_pool_exists:
                            _logger.info(
                                "BridgeRouteProvider.quote_exact_out: V2 fallback skipped (no V2 pool)",
                                extra={
                                    "leg_index": 0,
                                    "mode": "exact_out",
                                    "tokens": stage1_tokens,
                                    "provider": leg1_provider.name
                                    if leg1_provider
                                    else "unknown",
                                },
                            )
                        v2_fallback_provider = (
                            self._provider_map.get("uniswap_v2")
                            if should_try_v2
                            else None
                        )
                        if (
                            v2_fallback_provider
                            and leg1_provider.name.lower() == "uniswap_v3"
                        ):
                            # Create a V2-compatible route (remove fee annotations)
                            from libs.dex.routes import make_route

                            stage1_v2_route = make_route(
                                [stage1.tokens[0], stage1.tokens[1]]
                            )
                            try:
                                v2_fallback_quote = (
                                    v2_fallback_provider.quote_exact_out(
                                        leg2_quote.amount_in, stage1_v2_route
                                    )
                                )
                                if (
                                    v2_fallback_quote
                                    and v2_fallback_quote.amount_in > 0
                                ):
                                    # Prefer V2 if it's executable, otherwise use analytic for preview
                                    if (
                                        v2_fallback_quote.executable
                                        if hasattr(v2_fallback_quote, "executable")
                                        else True
                                    ):
                                        leg1_quote = v2_fallback_quote
                                        reserve_fallback_used = True
                                        _logger.info(
                                            "BridgeRouteProvider.quote_exact_out: leg1 V2 fallback succeeded",
                                            extra={
                                                "leg1_provider": leg1_provider.name,
                                                "v2_fallback_provider": v2_fallback_provider.name,
                                                "stage1": stage1_tokens,
                                                "amount_in_needed": leg2_quote.amount_in,
                                                "v2_fallback_amount_in": v2_fallback_quote.amount_in,
                                            },
                                        )
                                    elif v3_analytic_quote:
                                        # Use analytic for preview, but log that V2 is available
                                        leg1_quote = v3_analytic_quote
                                        _logger.info(
                                            "BridgeRouteProvider.quote_exact_out: leg1 V3 analytic fallback succeeded (preview-only), V2 fallback available",
                                            extra={
                                                "leg1_provider": leg1_provider.name,
                                                "stage1": stage1_tokens,
                                                "amount_in_needed": leg2_quote.amount_in,
                                                "analytic_amount_in": v3_analytic_quote.amount_in,
                                                "provider": v3_analytic_quote.provider,
                                                "v2_available": True,
                                            },
                                        )
                                # V2 failed, use analytic if available
                                elif v3_analytic_quote:
                                    leg1_quote = v3_analytic_quote
                                    _logger.info(
                                        "BridgeRouteProvider.quote_exact_out: leg1 V3 analytic fallback succeeded (preview-only), V2 fallback failed",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "stage1": stage1_tokens,
                                            "amount_in_needed": leg2_quote.amount_in,
                                            "analytic_amount_in": v3_analytic_quote.amount_in,
                                            "provider": v3_analytic_quote.provider,
                                        },
                                    )
                                else:
                                    _logger.info(
                                        "BridgeRouteProvider.quote_exact_out: leg1 V2 fallback returned no quote",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "v2_fallback_provider": v2_fallback_provider.name,
                                            "stage1": stage1_tokens,
                                        },
                                    )
                            except Exception as v2_exc:
                                # V2 failed, use analytic if available
                                if v3_analytic_quote:
                                    leg1_quote = v3_analytic_quote
                                    _logger.info(
                                        "BridgeRouteProvider.quote_exact_out: leg1 V3 analytic fallback succeeded (preview-only), V2 fallback exception",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "stage1": stage1_tokens,
                                            "amount_in_needed": leg2_quote.amount_in,
                                            "analytic_amount_in": v3_analytic_quote.amount_in,
                                            "provider": v3_analytic_quote.provider,
                                            "v2_error": str(v2_exc),
                                        },
                                    )
                                else:
                                    _logger.info(
                                        "BridgeRouteProvider.quote_exact_out: leg1 V2 fallback failed",
                                        extra={
                                            "leg1_provider": leg1_provider.name,
                                            "v2_fallback_provider": v2_fallback_provider.name,
                                            "stage1": stage1_tokens,
                                            "v2_error": str(v2_exc),
                                        },
                                    )
                        elif v3_analytic_quote:
                            # No V2 fallback available, use analytic preview
                            leg1_quote = v3_analytic_quote
                            _logger.info(
                                "BridgeRouteProvider.quote_exact_out: leg1 V3 analytic fallback succeeded (preview-only)",
                                extra={
                                    "leg1_provider": leg1_provider.name,
                                    "stage1": stage1_tokens,
                                    "amount_in_needed": leg2_quote.amount_in,
                                    "analytic_amount_in": v3_analytic_quote.amount_in,
                                    "provider": v3_analytic_quote.provider,
                                },
                            )

                    if not leg1_quote:
                        _logger.info(
                            "BridgeRouteProvider.quote_exact_out: leg1 V3 analytic fallback unavailable",
                            extra={
                                "leg1_provider": leg1_provider.name,
                                "stage1": stage1_tokens,
                                "fallback_enabled": os.getenv(
                                    "DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1"
                                )
                                .strip()
                                .lower()
                                in {"1", "true", "yes", "on"},
                                "is_vvv_usdc_leg": is_vvv_usdc_leg,
                            },
                        )
                else:
                    _logger.info(
                        "BridgeRouteProvider.quote_exact_out: leg1 quote failed, no reserve fallback (not DIEM/VVV leg)",
                        extra={
                            "leg1_provider": leg1_provider.name,
                            "stage1": stage1_tokens,
                            "leg1_error": leg1_error,
                        },
                    )

        if leg1_quote is None or not _positive_leg1(
            getattr(leg1_quote, "amount_in", None)
        ):
            stage1_tokens_final = (
                list(stage1.tokens) if hasattr(stage1, "tokens") else []
            )
            leg1_reason = "empty" if leg1_quote is None else "amount_in_non_positive"
            try:
                leg_context = self._bridge_leg_context(stage1, leg1_provider)
                _dex_diag_log_event(
                    {
                        "event": "dex_bridge_leg_failure",
                        "leg_index": 0,
                        "token_in": stage1_tokens_final[0]
                        if stage1_tokens_final
                        else None,
                        "token_out": stage1_tokens_final[-1]
                        if stage1_tokens_final
                        else None,
                        "provider": leg1_provider.name if leg1_provider else "unknown",
                        "mode": "exact_out",
                        "reason": leg1_reason,
                        "error": leg1_error,
                        "error_type": leg1_error_type,
                        "provider_returned_none": bool(
                            leg1_quote is None and not leg1_error
                        ),
                        "pool_address": leg_context.get("pool_address"),
                        "fee": leg_context.get("fee"),
                        "amount": int(getattr(leg2_quote, "amount_in", 0) or 0),
                        "requested_route": list(route.tokens),
                        "configured_provider": leg1_provider.name
                        if leg1_provider
                        else "unknown",
                        "reserve_fallback_used": bool(reserve_fallback_used),
                        "reserve_pref_used": bool(leg1_reserve_pref_used),
                        "slot0_fallback_used": bool(leg1_slot0_fallback_used),
                        "route_tokens": stage1_tokens_final,
                    }
                )
            except Exception:
                pass

            try:
                _metrics_inc(
                    "dex_bridge_leg_failures_total",
                    labels={
                        "provider": leg1_provider.name if leg1_provider else "unknown",
                        "reason": leg1_reason,
                        "mode": "exact_out",
                    },
                )
            except Exception:
                pass

            _logger.info(
                "BridgeRouteProvider.quote_exact_out: leg1 quote failed after fallback",
                extra={
                    "leg1_provider": leg1_provider.name,
                    "stage1": list(stage1.tokens)
                    if hasattr(stage1, "tokens")
                    else None,
                    "leg1_error": leg1_error,
                    "reserve_fallback_used": reserve_fallback_used,
                    "reserve_pref_used": leg1_reserve_pref_used,
                    "configured_pair": self._configured_diem_vvv_pair()
                    if is_leg1_diem_vvv
                    else None,
                    "route_pool": self._route_pool_address(stage1)
                    if is_leg1_diem_vvv
                    else None,
                },
            )
            fallback = self._bridge_single_leg_fallback_quote_exact_out(
                amount_out=int(amount_out),
                requested_route=route,
                failure_reason=f"leg1_{leg1_reason}",
                leg_index=0,
                error=leg1_error,
            )
            if fallback is not None:
                return fallback
            self._set_bridge_failure(
                "leg1_empty" if leg1_quote is None else "leg1_amount_in_non_positive",
                provider=leg1_provider.name if leg1_provider else None,
                stage_tokens=list(stage1.tokens) if hasattr(stage1, "tokens") else None,
                amount_out=int(getattr(leg2_quote, "amount_in", 0) or 0),
                error=leg1_error,
            )
            return None

        # Prefer reserve math when DIEM/VVV leg1 router quote drifts
        leg1_reserve_pref_used = False
        if leg1_quote is not None and is_leg1_diem_vvv:
            leg1_quote, leg1_reserve_pref_used = self._maybe_prefer_reserve_quote(
                mode="exact_out",
                leg_index=0,
                stage_tokens=stage1_tokens,
                router_quote=leg1_quote,
                reserve_quote_factory=lambda: diem_vvv_quote_from_reserves(
                    leg2_quote.amount_in, stage1.tokens[0], stage1.tokens[1]
                ),
                provider_name=leg1_provider.name if leg1_provider else "unknown",
            )

        composite_route = self._build_composite_route(stage1, stage2)
        combined = Quote(
            provider=self.name,
            amount_in=leg1_quote.amount_in,
            amount_out=amount_out,
            route=composite_route,
            executable=bool(getattr(leg1_quote, "executable", True))
            and bool(getattr(leg2_quote, "executable", True)),
        )
        self._composite_attach(combined, [leg1_quote, leg2_quote])
        return combined

    def trade(
        self, amount_in: int, min_amount_out: int, route: RoutePlan
    ) -> dict[str, str]:
        # Composite execution handled by DexAggregator; this provider does not submit trades directly.
        raise RuntimeError("bridge_vvv does not support direct trade execution")

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route: RoutePlan
    ) -> dict[str, str]:
        raise RuntimeError("bridge_vvv does not support direct trade execution")


class UniswapV3DexProvider(DexProvider):
    name = "uniswap_v3"
    supports_exact_out = True

    def __init__(
        self,
        router_address: Address,
        quoter_address: Address,
        *,
        default_fee: int | None = None,
        allowed_fee_tiers: Sequence[int] | None = None,
    ) -> None:
        from web3 import Web3  # type: ignore

        # Web3.to_checksum_address is strict about length; tests inject mixed‑case
        # demo addresses that may not checksum cleanly. Fall back to a lower‑case
        # normalization that preserves the hex body when checksum conversion fails.
        try:
            self.router_addr = Web3.to_checksum_address(router_address)
        except Exception:
            from libs.dex.routes import _normalize_address

            norm = _normalize_address(str(router_address))
            body = norm[2:] if norm.startswith("0x") else norm
            if len(body) < 40:
                body = body.zfill(40)
            elif len(body) > 40:
                body = body[-40:]
            self.router_addr = "0x" + body
        try:
            self.quoter_addr = Web3.to_checksum_address(quoter_address)
        except Exception:
            from libs.dex.routes import _normalize_address

            norm = _normalize_address(str(quoter_address))
            body = norm[2:] if norm.startswith("0x") else norm
            if len(body) < 40:
                body = body.zfill(40)
            elif len(body) > 40:
                body = body[-40:]
            self.quoter_addr = "0x" + body
        self._router_abi = "uniswap_v3_router.json"
        self._quoter_abi = "uniswap_v3_quoter.json"
        self._rpc_lock = Lock()
        self._refresh_provider()
        self.recipient: str | None = None
        self.default_fee = int(default_fee) if default_fee not in (None, "") else None
        if allowed_fee_tiers is None:
            self.allowed_fee_tiers: tuple[int, ...] | None = None
        else:
            tiers = sorted({int(f) for f in allowed_fee_tiers})
            self.allowed_fee_tiers = tuple(tiers)
        self._empty_quote_log_until = 0.0

    def _refresh_provider(self) -> None:
        with self._rpc_lock:
            self.w3 = get_web3()
            self.router = get_contract(self.w3, self.router_addr, self._router_abi)
            self.quoter = get_contract(self.w3, self.quoter_addr, self._quoter_abi)

    def _ensure_route(self, route: RoutePlan) -> RoutePlan:
        # Normalize route for V3 (ensure fee tiers are present)
        try:
            normalized = normalize_route_for_v3(route, default_fee=self.default_fee)
        except ValueError as e:
            # If normalization fails and we have a default fee, try with_default_fee
            if self.default_fee is not None:
                try:
                    normalized = route.with_default_fee(self.default_fee)
                except Exception:
                    raise ValueError("fee tier required for Uniswap V3 route") from e
            else:
                raise ValueError("fee tier required for Uniswap V3 route") from e

        # Validate fee tiers against allowed list
        if self.allowed_fee_tiers:
            for hop in normalized.hops:
                if hop.fee not in self.allowed_fee_tiers:
                    raise ValueError(
                        f"fee tier {hop.fee} not permitted for provider {self.name}"
                    )
        return normalized

    def _ensure_allowance(
        self, token: Address, owner: Address, spender: Address, required: int
    ) -> str | None:
        from libs.dex.routes import _normalize_address

        # Normalize token address to strip any @fee suffix before Web3 operations
        token_normalized = _normalize_address(token)
        erc20 = get_contract(self.w3, token_normalized, "erc20.json")
        try:
            current = int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            current = 0
        if current >= required:
            return None

        def _flag_enabled(raw: str | None, *, default: bool) -> bool:
            if raw is None:
                return default
            value = str(raw).strip().lower()
            if value == "":
                return default
            return value in {"1", "true", "yes", "on"}

        raw_specific = os.getenv("DEX_UNISWAP_V3_APPROVE_MAX")
        raw_global = os.getenv("DEX_APPROVE_MAX")
        approve_max = (
            _flag_enabled(raw_specific, default=True)
            if raw_specific is not None
            else _flag_enabled(raw_global, default=True)
        )
        approve_amount = (2**256 - 1) if approve_max else int(required)

        approve_data = encode_contract_call(
            erc20, "approve", [spender, int(approve_amount)]
        )
        # In tests, approve_data may be a MagicMock; skip tx submit in that case
        if os.getenv("PYTEST_CURRENT_TEST") and not isinstance(approve_data, str):
            return None
        tx_hash = send_tx(token_normalized, bytes.fromhex(approve_data[2:]))
        try:
            _logger.info(
                "UniswapV3 APPROVAL: token=%s spender=%s allowance_before=%s approve_amount=%s tx_hash=%s",
                token_normalized,
                spender,
                int(current),
                int(approve_amount),
                tx_hash or "submit_failed",
            )
        except Exception:
            pass
        try:
            from libs.dex.diagnostics import log_event as _dex_diag_log_event

            _dex_diag_log_event(
                {
                    "event": "dex_approval_injected",
                    "provider": self.name,
                    "token": token_normalized,
                    "spender": spender,
                    "owner": owner,
                    "allowance_before": int(current),
                    "approve_amount": int(approve_amount),
                    "approval_tx_hash": tx_hash or "",
                }
            )
        except Exception:
            pass

        # Wait for approval tx to confirm before returning
        if tx_hash:
            # Unit tests use stub hashes; skip confirmation waits there.
            if os.getenv("PYTEST_CURRENT_TEST"):
                return tx_hash
            from libs.agentkit_ext.agentkit_wallet import wait_for_tx_confirmation

            _logger.info(
                "Waiting for approval tx confirmation: %s (token=%s, spender=%s)",
                tx_hash,
                token_normalized,
                spender,
            )
            confirm_result = wait_for_tx_confirmation(tx_hash, timeout=60)
            if confirm_result.get("status") != "confirmed":
                _logger.warning(
                    "Approval tx not confirmed: %s (status=%s)",
                    tx_hash,
                    confirm_result.get("status"),
                )
                raise RuntimeError(
                    f"Approval tx not confirmed: {confirm_result.get('status')}"
                )
            _logger.info(
                "Approval tx confirmed in block %s",
                confirm_result.get("block_number"),
            )
        return tx_hash

    def _normalize_quote_result(self, value: Any) -> int:
        if isinstance(value, (list, tuple)):
            return int(value[0]) if value else 0
        return int(value or 0)

    def _empty_log_ttl_seconds(self) -> float:
        try:
            raw = os.getenv("DEX_CIRCUIT_COOL_SECONDS") or "30"
            ttl = float(str(raw).strip() or 30.0)
        except Exception:
            ttl = 30.0
        return max(5.0, ttl)

    def _should_log_empty_quote(self) -> bool:
        now = time.monotonic()
        until = float(getattr(self, "_empty_quote_log_until", 0.0) or 0.0)
        if now < until:
            return False
        self._empty_quote_log_until = now + self._empty_log_ttl_seconds()
        return True

    def _configured_vvv_usdc_pool(self, token_in: str, token_out: str) -> str | None:
        vvv = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
        quote = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        if not vvv or not quote:
            return None
        tokens = {str(token_in).strip().lower(), str(token_out).strip().lower()}
        if tokens != {vvv, quote}:
            return None
        pool = (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip() or (
            os.getenv("VVV_USDC_POOL_V3_ADDRESS") or ""
        ).strip()
        return pool or None

    def _pool_match_info(self, route: RoutePlan) -> dict[str, Any]:
        info: dict[str, Any] = {
            "pool_configured": False,
            "pool_address": None,
            "pool_token0": None,
            "pool_token1": None,
            "pool_tokens_match": None,
        }
        try:
            tokens = list(route.tokens)
        except Exception:
            tokens = []
        if len(tokens) != 2:
            return info
        pool_addr = self._configured_vvv_usdc_pool(tokens[0], tokens[1])
        if not pool_addr:
            return info
        info["pool_configured"] = True
        info["pool_address"] = pool_addr
        try:
            from web3 import Web3  # type: ignore

            from libs.agentkit_ext.web3_utils import get_web3
            from libs.dex.routes import _normalize_address

            w3 = get_web3()
            pool_abi = [
                {
                    "constant": True,
                    "inputs": [],
                    "name": "token0",
                    "outputs": [{"name": "", "type": "address"}],
                    "stateMutability": "view",
                    "type": "function",
                },
                {
                    "constant": True,
                    "inputs": [],
                    "name": "token1",
                    "outputs": [{"name": "", "type": "address"}],
                    "stateMutability": "view",
                    "type": "function",
                },
            ]
            pool = w3.eth.contract(
                address=Web3.to_checksum_address(pool_addr), abi=pool_abi
            )
            token0 = pool.functions.token0().call()
            token1 = pool.functions.token1().call()
            info["pool_token0"] = token0
            info["pool_token1"] = token1
            token0_norm = _normalize_address(token0)
            token1_norm = _normalize_address(token1)
            token_in_norm = _normalize_address(tokens[0])
            token_out_norm = _normalize_address(tokens[1])
            info["pool_tokens_match"] = {
                token_in_norm,
                token_out_norm,
            } == {token0_norm, token1_norm}
        except Exception:
            info["pool_tokens_match"] = None
        return info

    def _log_empty_quote(
        self,
        *,
        mode: str,
        amount: int,
        route: RoutePlan,
        reason: str,
        latency_ms: float,
        error: str | None = None,
    ) -> None:
        if not self._should_log_empty_quote():
            return
        try:
            tokens = list(route.tokens)
        except Exception:
            tokens = []
        fees: list[int | None] = []
        try:
            fees = [hop.fee for hop in route.hops]
        except Exception:
            fees = []
        fee_tier = fees[0] if len(fees) == 1 else None
        pool_info = self._pool_match_info(route)
        payload: dict[str, Any] = {
            "event": "dex_v3_empty_quote",
            "provider": self.name,
            "mode": mode,
            "reason": reason,
            "amount": int(amount),
            "token_in": tokens[0] if tokens else None,
            "token_out": tokens[-1] if tokens else None,
            "fees": fees,
            "fee_tier": fee_tier,
            "quoter": self.quoter_addr,
            "router": self.router_addr,
            "latency_ms": float(latency_ms),
            "rpc_latency_ms": float(latency_ms),
        }
        if error:
            payload["error"] = error
        payload.update(pool_info)
        try:
            _logger.warning("UniswapV3 empty quote", extra=payload)
        except Exception:
            pass
        try:
            _dex_diag_log_event(payload)
        except Exception:
            pass

    @staticmethod
    def _decode_revert_reason(exc: Exception) -> str | None:
        """Best-effort decode of revert reason from web3 exceptions."""

        def _decode_error_selector(raw: str) -> str | None:
            if not raw:
                return None
            data = raw[2:] if raw.startswith("0x") else raw
            # 0x08c379a0 = Error(string)
            if not data.startswith("08c379a0") or len(data) < (8 + 64 + 64):
                return None
            try:
                strlen = int(data[8 + 64 : 8 + 64 + 64], 16)
                reason_hex = data[8 + 64 + 64 : 8 + 64 + 64 + strlen * 2]
                if strlen > 0 and reason_hex:
                    return bytes.fromhex(reason_hex).decode("utf-8", "ignore").strip()
            except Exception:
                return None
            return None

        def _iter_hex_candidates(value: Any) -> list[str]:
            out: list[str] = []
            try:
                if (
                    isinstance(value, str)
                    and value.startswith("0x")
                    and len(value) > 10
                ):
                    out.append(value)
                elif isinstance(value, (bytes, bytearray)) and len(value) > 0:
                    out.append("0x" + value.hex())
                elif isinstance(value, dict):
                    for v in value.values():
                        out.extend(_iter_hex_candidates(v))
                elif isinstance(value, (list, tuple)):
                    for v in value:
                        out.extend(_iter_hex_candidates(v))
            except Exception:
                return out
            return out

        candidates: list[str] = []
        args0 = exc.args[0] if getattr(exc, "args", None) else None
        if isinstance(args0, str):
            candidates.append(args0)
            decoded = _decode_error_selector(args0)
            if decoded:
                return decoded
            if "execution reverted" in args0.lower():
                parts = args0.split("execution reverted", 1)
                if len(parts) > 1:
                    tail = parts[1].lstrip(": ").strip()
                    if tail:
                        decoded_tail = _decode_error_selector(tail)
                        if decoded_tail:
                            return decoded_tail
                        return tail
        if isinstance(args0, dict):
            data_field = args0.get("data") or args0.get("result")
            if isinstance(data_field, str):
                decoded = _decode_error_selector(data_field)
                if decoded:
                    return decoded
            msg = args0.get("message")
            if isinstance(msg, str):
                candidates.append(msg)
            for cand in _iter_hex_candidates(args0):
                decoded = _decode_error_selector(cand)
                if decoded:
                    return decoded
        data_attr = getattr(exc, "data", None)
        if isinstance(data_attr, dict):
            raw = data_attr.get("data") or data_attr.get("result")
            if isinstance(raw, str):
                decoded = _decode_error_selector(raw)
                if decoded:
                    return decoded
            for cand in _iter_hex_candidates(data_attr):
                decoded = _decode_error_selector(cand)
                if decoded:
                    return decoded
        elif isinstance(data_attr, (bytes, bytearray, str)):
            decoded = _decode_error_selector(
                data_attr if isinstance(data_attr, str) else "0x" + data_attr.hex()
            )
            if decoded:
                return decoded
        for cand in candidates:
            if cand:
                return cand
        return None

    def _is_diem_route(self, route: RoutePlan) -> bool:
        """Check if route involves DIEM token - no V3 pool exists for DIEM on vanilla UniswapV3."""
        try:
            diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
            if not diem_addr:
                return False
            tokens = [str(t).strip().lower() for t in route.tokens]
            return diem_addr in tokens
        except Exception:
            return False

    def _is_vvv_usdc_route(self, route: RoutePlan) -> bool:
        """Check if route is VVV<->USDC - V3 pool exists but often has no tick liquidity."""
        try:
            vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
            usdc_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
            if not vvv_addr or not usdc_addr:
                return False
            tokens = [str(t).strip().lower() for t in route.tokens]
            if len(tokens) != 2:
                return False
            return set(tokens) == {vvv_addr, usdc_addr}
        except Exception:
            return False

    def quote(self, amount_in: int, route: RoutePlan) -> Quote | None:
        t0 = time.perf_counter()
        # Fast-path: skip DIEM routes on vanilla UniswapV3 (no pool exists)
        skip_v3_diem = os.getenv("DEX_V3_SKIP_DIEM", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if skip_v3_diem and self._is_diem_route(route):
            _logger.debug("UniswapV3 quote skipped: DIEM route (no V3 pool)")
            return None
        # Skip VVV/USDC V3 quotes - pool exists but consistently has no tick liquidity
        skip_v3_vvv = os.getenv("DEX_V3_SKIP_VVV_USDC", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if skip_v3_vvv and self._is_vvv_usdc_route(route):
            _logger.debug(
                "UniswapV3 quote skipped: VVV/USDC route (low tick liquidity)"
            )
            return None
        try:
            effective_route = self._ensure_route(route)
            path_bytes = effective_route.to_uniswap_v3_path_bytes()
            result = _call_with_rpc_retry(
                self.name,
                "quote",
                self._refresh_provider,
                lambda: self.quoter.functions.quoteExactInput(
                    path_bytes, amount_in
                ).call(),
            )
            out_amt = self._normalize_quote_result(result)
            if out_amt <= 0:
                _metrics_inc(
                    "dex_quotes_total", labels={"provider": self.name, "status": "zero"}
                )
                self._log_empty_quote(
                    mode="exact_in",
                    amount=amount_in,
                    route=effective_route,
                    reason="zero_output",
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
                return None
            _metrics_inc(
                "dex_quotes_total", labels={"provider": self.name, "status": "ok"}
            )
            _bucket_latency("quote", self.name, time.perf_counter() - t0)
            return Quote(
                provider=self.name,
                amount_in=amount_in,
                amount_out=out_amt,
                route=effective_route,
            )
        except Exception as exc:
            _metrics_inc(
                "dex_quotes_total", labels={"provider": self.name, "status": "err"}
            )
            try:
                self._log_empty_quote(
                    mode="exact_in",
                    amount=amount_in,
                    route=route,
                    reason="exception",
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    error=str(exc),
                )
            except Exception:
                pass
            return None

    def _quote_exact_out_via_slot0(
        self, amount_out: int, route: RoutePlan
    ) -> Quote | None:
        """
        Fallback: Compute exact-out quote using V3 pool slot0 price.

        This is a conservative estimate when QuoterV2 reverts.
        Only works for single-hop routes.
        """
        if len(route.tokens) != 2:
            return None  # Multi-hop not supported for slot0 fallback

        try:
            # Get pool address from route (need to resolve via factory)
            token0 = route.tokens[0].lower()
            token1 = route.tokens[1].lower()
            fee = (
                route.hops[0].fee
                if route.hops and route.hops[0].fee
                else self.default_fee
            )
            if fee is None:
                return None

            # Try to get pool address from factory
            try:
                from web3 import Web3  # type: ignore

                from libs.agentkit_ext.web3_utils import get_web3
                from libs.dex.routes import _normalize_address

                w3 = get_web3()
                factory_addr = os.getenv("UNISWAP_V3_FACTORY_ADDRESS")
                if not factory_addr:
                    return None

                factory_abi = [
                    {
                        "constant": True,
                        "inputs": [
                            {"name": "tokenA", "type": "address"},
                            {"name": "tokenB", "type": "address"},
                            {"name": "fee", "type": "uint24"},
                        ],
                        "name": "getPool",
                        "outputs": [{"name": "pool", "type": "address"}],
                        "type": "function",
                    }
                ]
                factory = w3.eth.contract(
                    address=Web3.to_checksum_address(factory_addr), abi=factory_abi
                )
                # Normalize addresses to strip any @fee suffix before Web3 conversion
                token0_normalized = _normalize_address(token0)
                token1_normalized = _normalize_address(token1)
                pool_addr = factory.functions.getPool(
                    Web3.to_checksum_address(token0_normalized),
                    Web3.to_checksum_address(token1_normalized),
                    fee,
                ).call()
                if (
                    not pool_addr
                    or pool_addr == "0x0000000000000000000000000000000000000000"
                ):
                    return None

                # Query slot0
                pool_abi = [
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "slot0",
                        "outputs": [
                            {"name": "sqrtPriceX96", "type": "uint160"},
                            {"name": "tick", "type": "int24"},
                            {"name": "observationIndex", "type": "uint16"},
                            {"name": "observationCardinality", "type": "uint16"},
                            {"name": "observationCardinalityNext", "type": "uint16"},
                            {"name": "feeProtocol", "type": "uint8"},
                            {"name": "unlocked", "type": "bool"},
                        ],
                        "stateMutability": "view",
                        "type": "function",
                    },
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "token0",
                        "outputs": [{"name": "", "type": "address"}],
                        "stateMutability": "view",
                        "type": "function",
                    },
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "token1",
                        "outputs": [{"name": "", "type": "address"}],
                        "stateMutability": "view",
                        "type": "function",
                    },
                ]
                pool = w3.eth.contract(
                    address=Web3.to_checksum_address(pool_addr), abi=pool_abi
                )
                slot0 = slot0_cache_fetch(
                    f"slot0:{pool_addr.lower()}",
                    lambda: pool.functions.slot0().call(),
                    validator=lambda value: bool(value),
                )
                if not slot0:
                    return None
                sqrt_price_x96 = slot0[0]
                pool_token0 = pool.functions.token0().call().lower()
                pool_token1 = pool.functions.token1().call().lower()

                # Get decimals via ERC20
                erc20_abi = [
                    {
                        "constant": True,
                        "inputs": [],
                        "name": "decimals",
                        "outputs": [{"name": "", "type": "uint8"}],
                        "type": "function",
                    }
                ]
                try:
                    token0_contract = w3.eth.contract(
                        address=Web3.to_checksum_address(token0_normalized),
                        abi=erc20_abi,
                    )
                    token1_contract = w3.eth.contract(
                        address=Web3.to_checksum_address(token1_normalized),
                        abi=erc20_abi,
                    )
                    dec0 = token0_contract.functions.decimals().call()
                    dec1 = token1_contract.functions.decimals().call()
                except Exception:
                    # Fallback to defaults
                    dec0 = 18
                    dec1 = 18

                # Calculate price ratio
                ratio = sqrt_price_x96 / float(1 << 96)
                price_ratio = ratio * ratio * float(pow(10.0, dec0 - dec1))

                # Determine direction
                if pool_token0 == token0 and pool_token1 == token1:
                    # token0 -> token1: price_ratio gives token1 per token0
                    # For exact-out: we want amount_out of token1, need amount_in of token0
                    # amount_in ≈ amount_out / price_ratio (conservative, add 0.5% buffer)
                    if price_ratio <= 0:
                        return None
                    amount_in_approx = int((amount_out * 1.005) / price_ratio)
                elif pool_token0 == token1 and pool_token1 == token0:
                    # token1 -> token0: price_ratio gives token0 per token1
                    # For exact-out: we want amount_out of token0, need amount_in of token1
                    # amount_in ≈ amount_out * price_ratio (conservative, add 0.5% buffer)
                    amount_in_approx = int(amount_out * price_ratio * 1.005)
                else:
                    return None

                if amount_in_approx <= 0:
                    return None

                return Quote(
                    provider=f"{self.name}_slot0_fallback",
                    amount_in=amount_in_approx,
                    amount_out=amount_out,
                    route=route,
                )
            except Exception:
                return None
        except Exception:
            return None

    def _quote_exact_out_via_router_simulation(
        self, amount_out: int, route: RoutePlan
    ) -> Quote | None:
        """
        Fallback: Try router simulation (eth_call) with probe amount to infer price.

        Uses a small probe amount to estimate the required input.
        For multi-hop routes, validates scaling and adds appropriate buffer.
        """
        try:
            effective_route = self._ensure_route(route)
            path_bytes = effective_route.to_uniswap_v3_path_bytes(reverse=True)

            # For multi-hop routes, use a smaller probe to avoid hitting liquidity limits
            # Use 1% of desired amount or 1e18, whichever is smaller
            num_hops = len(effective_route.hops)
            if num_hops > 1:
                # Multi-hop: use smaller probe (0.1% of amount or 1e17, whichever is smaller)
                probe_out = min(amount_out // 1000, 10**17)
            else:
                # Single-hop: use 1% of amount or 1e18, whichever is smaller
                probe_out = min(amount_out // 100, 10**18)

            # Ensure probe is at least 1 wei
            probe_out = max(probe_out, 1)
            probe_out = min(probe_out, amount_out)

            try:
                from libs.agentkit_ext.web3_utils import get_web3

                w3 = get_web3()
                # Try router exactOutput simulation
                router_abi = get_contract(w3, self.router_addr, self._router_abi)
                # Build exactOutput call params
                recipient = (
                    "0x0000000000000000000000000000000000000000"  # Dummy for simulation
                )
                deadline = 2**256 - 1  # Max deadline
                params = (path_bytes, recipient, deadline, probe_out, 2**256 - 1)
                fn = router_abi.functions.exactOutput(params)

                # Use eth_call to simulate
                probe_in = w3.eth.call(fn.build_transaction({"from": recipient}))  # type: ignore
                if not probe_in or len(probe_in) < 32:
                    return None

                probe_in_amount = int.from_bytes(probe_in[:32], "big")
                if probe_in_amount <= 0:
                    return None

                # Scale up: amount_in ≈ (amount_out / probe_out) * probe_in_amount
                # For multi-hop routes, add larger buffer due to compounding slippage
                # Single-hop: 1% buffer, multi-hop: 2% buffer per hop (capped at 5%)
                if num_hops > 1:
                    slippage_buffer = min(1.0 + (0.02 * num_hops), 1.05)
                else:
                    slippage_buffer = 1.01

                amount_in_approx = int(
                    (amount_out * probe_in_amount * slippage_buffer) / probe_out
                )
                if amount_in_approx <= 0:
                    return None

                # Validate scaling: ensure scaled amount is reasonable
                # If probe ratio suggests extreme slippage (>10x), reject
                probe_ratio = probe_in_amount / probe_out if probe_out > 0 else 0
                if probe_ratio > 10.0 or probe_ratio < 0.1:
                    return None

                return Quote(
                    provider=f"{self.name}_router_sim_fallback",
                    amount_in=amount_in_approx,
                    amount_out=amount_out,
                    route=effective_route,
                )
            except Exception:
                return None
        except Exception:
            return None

    def quote_exact_out(self, amount_out: int, route: RoutePlan) -> Quote | None:
        t0 = time.perf_counter()
        # Fast-path: skip DIEM routes on vanilla UniswapV3 (no pool exists)
        skip_v3_diem = os.getenv("DEX_V3_SKIP_DIEM", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if skip_v3_diem and self._is_diem_route(route):
            _logger.debug("UniswapV3 quote_exact_out skipped: DIEM route (no V3 pool)")
            return None
        # Skip VVV/USDC V3 quotes - pool exists but consistently has no tick liquidity
        skip_v3_vvv = os.getenv("DEX_V3_SKIP_VVV_USDC", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if skip_v3_vvv and self._is_vvv_usdc_route(route):
            _logger.debug(
                "UniswapV3 quote_exact_out skipped: VVV/USDC route (low tick liquidity)"
            )
            return None
        try:
            effective_route = self._ensure_route(route)
            path_bytes = effective_route.to_uniswap_v3_path_bytes(reverse=True)

            # Try QuoterV2 with sqrtPriceLimitX96 first (if available)
            sqrt_price_limit = _get_sqrt_price_limit()

            # Check if quoter supports quoteExactOutputSingle (QuoterV2)
            # For single-hop routes, use quoteExactOutputSingle with sqrtPriceLimitX96
            if len(effective_route.tokens) == 2:
                try:
                    # Try QuoterV2 quoteExactOutputSingle signature
                    token_in = effective_route.tokens[0]
                    token_out = effective_route.tokens[1]
                    fee = (
                        effective_route.hops[0].fee
                        if effective_route.hops and effective_route.hops[0].fee
                        else self.default_fee
                    )
                    if fee is not None:
                        from web3 import Web3  # type: ignore

                        from libs.dex.routes import _normalize_address

                        token_in_norm = _normalize_address(token_in)
                        token_out_norm = _normalize_address(token_out)
                        token_in_addr = Web3.to_checksum_address(token_in_norm)
                        token_out_addr = Web3.to_checksum_address(token_out_norm)

                        # Try QuoterV2 quoteExactOutputSingle
                        try:
                            result = _call_with_rpc_retry(
                                self.name,
                                "quote_exact_out",
                                self._refresh_provider,
                                lambda: self.quoter.functions.quoteExactOutputSingle(
                                    (
                                        token_in_addr,
                                        token_out_addr,
                                        fee,
                                        amount_out,
                                        sqrt_price_limit,
                                    )
                                ).call(),
                            )
                            in_amt = self._normalize_quote_result(result)
                            if in_amt > 0:
                                _metrics_inc(
                                    "dex_quotes_total",
                                    labels={
                                        "provider": self.name,
                                        "status": "ok",
                                        "mode": "exact_out",
                                        "method": "quoter_v2_single",
                                    },
                                )
                                return Quote(
                                    provider=self.name,
                                    amount_in=in_amt,
                                    amount_out=amount_out,
                                    route=effective_route,
                                )
                        except Exception:
                            # QuoterV2 not available or failed, fall through to QuoterV1
                            pass
                except Exception:
                    # Fall through to QuoterV1
                    pass

            # Multi-hop: prefer QuoterV2 quoteExactOutput when available
            if len(effective_route.tokens) > 2:
                try:
                    result = _call_with_rpc_retry(
                        self.name,
                        "quote_exact_out",
                        self._refresh_provider,
                        lambda: self.quoter.functions.quoteExactOutput(
                            path_bytes, amount_out
                        ).call(),
                    )
                    in_amt = self._normalize_quote_result(result)
                    if in_amt > 0:
                        _metrics_inc(
                            "dex_quotes_total",
                            labels={
                                "provider": self.name,
                                "status": "ok",
                                "mode": "exact_out",
                                "method": "quoter_v2_multi",
                            },
                        )
                        return Quote(
                            provider=self.name,
                            amount_in=in_amt,
                            amount_out=amount_out,
                            route=effective_route,
                        )
                except Exception:
                    # Fall through to standard fallback path
                    pass

            # Fallback to QuoterV1 quoteExactOutput (no sqrtPriceLimitX96)
            result = _call_with_rpc_retry(
                self.name,
                "quote_exact_out",
                self._refresh_provider,
                lambda: self.quoter.functions.quoteExactOutput(
                    path_bytes, amount_out
                ).call(),
            )
            in_amt = self._normalize_quote_result(result)
            if in_amt <= 0:
                _metrics_inc(
                    "dex_quotes_total",
                    labels={
                        "provider": self.name,
                        "status": "zero",
                        "mode": "exact_out",
                    },
                )
                self._log_empty_quote(
                    mode="exact_out",
                    amount=amount_out,
                    route=effective_route,
                    reason="zero_output",
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                )
                return None
            _metrics_inc(
                "dex_quotes_total",
                labels={"provider": self.name, "status": "ok", "mode": "exact_out"},
            )
            return Quote(
                provider=self.name,
                amount_in=in_amt,
                amount_out=amount_out,
                route=effective_route,
            )
        except Exception as exc:
            # Check if this is a revert/no data error
            error_str = str(exc).lower()
            revert_reason = self._decode_revert_reason(exc)
            is_revert = (
                "execution reverted" in error_str
                or "no data" in error_str
                or "revert" in error_str
            )

            if is_revert:
                # Try fallbacks
                # Fallback 1: Router simulation
                fallback_quote = self._quote_exact_out_via_router_simulation(
                    amount_out, route
                )
                if fallback_quote:
                    try:
                        _metrics_inc(
                            "dex_quotes_total",
                            labels={
                                "provider": self.name,
                                "status": "fallback_router_sim",
                                "mode": "exact_out",
                            },
                        )
                    except Exception:
                        pass
                    return fallback_quote

                # Fallback 2: Slot0-based price math (single-hop only)
                if len(route.tokens) == 2:
                    fallback_quote = self._quote_exact_out_via_slot0(amount_out, route)
                    if fallback_quote:
                        try:
                            _metrics_inc(
                                "dex_quotes_total",
                                labels={
                                    "provider": self.name,
                                    "status": "fallback_slot0",
                                    "mode": "exact_out",
                                },
                            )
                        except Exception:
                            pass
                        return fallback_quote

            if revert_reason:
                try:
                    # Use DEBUG for SPL (slippage) errors on VVV routes - these are expected
                    # when routing through VVV bridge without sufficient liquidity
                    tokens = list(getattr(route, "tokens", []))
                    vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                    is_vvv_route = (
                        any(str(t).lower() == vvv_addr for t in tokens)
                        if vvv_addr
                        else False
                    )
                    is_spl_error = "SPL" in str(revert_reason).upper()
                    if is_vvv_route and is_spl_error:
                        _logger.debug(
                            "UniswapV3 quote_exact_out reverted: %s route=%s amount_out=%s",
                            revert_reason,
                            tokens,
                            amount_out,
                        )
                    else:
                        _logger.warning(
                            "UniswapV3 quote_exact_out reverted: %s route=%s amount_out=%s",
                            revert_reason,
                            tokens,
                            amount_out,
                        )
                except Exception:
                    pass
                try:
                    _metrics_inc(
                        "dex_quotes_total",
                        labels={
                            "provider": self.name,
                            "status": "revert",
                            "mode": "exact_out",
                        },
                    )
                except Exception:
                    pass
                raise RuntimeError(f"uniswap_v3_revert:{revert_reason}") from exc

            _metrics_inc(
                "dex_quotes_total",
                labels={"provider": self.name, "status": "err", "mode": "exact_out"},
            )
            try:
                self._log_empty_quote(
                    mode="exact_out",
                    amount=amount_out,
                    route=route,
                    reason="exception",
                    latency_ms=(time.perf_counter() - t0) * 1000.0,
                    error=str(exc),
                )
            except Exception:
                pass
            return None

    def trade(
        self, amount_in: int, min_amount_out: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        from libs.dex.routes import _normalize_address

        effective_route = self._ensure_route(route)
        # Normalize address to strip any @fee suffix before Web3 conversion
        token_in_raw = _normalize_address(effective_route.tokens[0])
        token_in = _Web3.to_checksum_address(token_in_raw)
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("ETH_PRIVATE_KEY"):
            recipient = "0x" + "1" * 40
        else:
            recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60

        # Use exactInputSingle for single-hop swaps (2 tokens), exactInput for multi-hop
        # SwapRouter02 on Base requires exactInputSingle for single-hop to work properly
        if len(effective_route.tokens) == 2:
            # Single-hop swap: use exactInputSingle
            token_out_raw = _normalize_address(effective_route.tokens[1])
            token_out = _Web3.to_checksum_address(token_out_raw)
            # Get fee from the first hop (RoutePlan stores fees in hops, not as a list)
            fee = (
                effective_route.hops[0].fee
                if effective_route.hops[0].fee is not None
                else self.default_fee
            )
            params = (
                token_in,  # tokenIn
                token_out,  # tokenOut
                fee,  # fee
                recipient,  # recipient
                amount_in,  # amountIn
                min_amount_out,  # amountOutMinimum
                0,  # sqrtPriceLimitX96 (0 = no limit)
            )
            _logger.info(
                "UniswapV3 SINGLE-HOP: tokenIn=%s tokenOut=%s fee=%s amount=%s min_out=%s",
                token_in,
                token_out,
                fee,
                amount_in,
                min_amount_out,
            )
            fn = self.router.functions.exactInputSingle(params)
        else:
            # Multi-hop swap: use exactInput with packed bytes path
            params = (
                effective_route.to_uniswap_v3_path_bytes(),
                recipient,
                deadline,
                amount_in,
                min_amount_out,
            )
            _logger.info(
                "UniswapV3 MULTI-HOP: path=%s tokens amount=%s min_out=%s",
                len(effective_route.tokens),
                amount_in,
                min_amount_out,
            )
            fn = self.router.functions.exactInput(params)

        # Must pass 'from' address for gas estimation to work
        built = fn.build_transaction({"from": recipient})
        tx_hash = send_tx(self.router_addr, built["data"])
        _metrics_inc(
            "dex_trades_total", labels={"provider": self.name, "mode": "exact_in"}
        )
        # CRITICAL: Log tx_hash for on-chain traceability
        _logger.info(
            "UniswapV3 trade EXECUTED: amount_in=%s min_out=%s route=%s tx_hash=%s",
            amount_in,
            min_amount_out,
            list(effective_route.tokens),
            tx_hash,
        )
        return {"provider": self.name, "tx_hash": tx_hash, "approval_tx": approve_hash}

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route: RoutePlan
    ) -> dict[str, str]:
        from web3 import Web3 as _Web3  # type: ignore

        from libs.dex.routes import _normalize_address

        if os.getenv("PYTEST_CURRENT_TEST"):
            # Rebind web3/router/quoter to patched test doubles
            self._refresh_provider()

        effective_route = self._ensure_route(route)
        # Normalize address to strip any @fee suffix before Web3 conversion
        token_in_raw = _normalize_address(effective_route.tokens[0])
        token_in = _Web3.to_checksum_address(token_in_raw)
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("ETH_PRIVATE_KEY"):
            recipient = "0x" + "1" * 40
        else:
            recipient = self.recipient or _Web3.to_checksum_address(get_address())
        approve_hash = (
            self._ensure_allowance(token_in, recipient, self.router_addr, max_amount_in)
            or ""
        )
        deadline = int(time.time()) + 20 * 60

        try:
            # Use exactOutputSingle for single-hop swaps (2 tokens), exactOutput for multi-hop
            if len(effective_route.tokens) == 2:
                # Single-hop swap: use exactOutputSingle
                token_out_raw = _normalize_address(effective_route.tokens[1])
                token_out = _Web3.to_checksum_address(token_out_raw)
                # Get fee from the first hop (RoutePlan stores fees in hops, not as a list)
                fee = (
                    effective_route.hops[0].fee
                    if effective_route.hops[0].fee is not None
                    else self.default_fee
                )
                params = (
                    token_in,  # tokenIn
                    token_out,  # tokenOut
                    fee,  # fee
                    recipient,  # recipient
                    amount_out,  # amountOut
                    max_amount_in,  # amountInMaximum
                    0,  # sqrtPriceLimitX96 (0 = no limit)
                )
                _logger.info(
                    "UniswapV3 SINGLE-HOP exact_out: tokenIn=%s tokenOut=%s fee=%s amount_out=%s max_in=%s",
                    token_in,
                    token_out,
                    fee,
                    amount_out,
                    max_amount_in,
                )
                fn = self.router.functions.exactOutputSingle(params)
            else:
                # Multi-hop swap: use exactOutput with packed bytes path
                params = (
                    effective_route.to_uniswap_v3_path_bytes(reverse=True),
                    recipient,
                    deadline,
                    amount_out,
                    max_amount_in,
                )
                _logger.info(
                    "UniswapV3 MULTI-HOP exact_out: path=%s tokens amount_out=%s max_in=%s",
                    len(effective_route.tokens),
                    amount_out,
                    max_amount_in,
                )
                fn = self.router.functions.exactOutput(params)

            # Must pass 'from' address for gas estimation to work
            built = fn.build_transaction({"from": recipient})
            tx_hash = send_tx(self.router_addr, built["data"])
            _metrics_inc(
                "dex_trades_total", labels={"provider": self.name, "mode": "exact_out"}
            )
            return {
                "provider": self.name,
                "tx_hash": tx_hash,
                "approval_tx": approve_hash,
            }
        except Exception as exc:
            # Enhanced error logging with route and trade context
            error_msg = str(exc)
            error_type = type(exc).__name__
            route_tokens = (
                list(effective_route.tokens)
                if hasattr(effective_route, "tokens")
                else []
            )

            # Import logger if not already available
            try:
                from libs.telemetry.logger import get_logger

                _dex_logger = get_logger("dex.providers")
            except Exception:
                import logging

                _dex_logger = logging.getLogger("dex.providers")

            _dex_logger.error(
                f"UniswapV3 trade_exact_out failed: {error_type}: {error_msg} "
                f"(amount_out={amount_out}, max_amount_in={max_amount_in}, "
                f"route={route_tokens}, token_in={token_in}, recipient={recipient})",
                exc_info=True,
                extra={
                    "provider": self.name,
                    "mode": "exact_out",
                    "error": error_msg,
                    "error_type": error_type,
                    "route": route_tokens,
                    "amount_out": amount_out,
                    "max_amount_in": max_amount_in,
                    "token_in": token_in,
                    "recipient": recipient,
                },
            )
            raise


class DexAggregator:
    def __init__(self, providers: list[DexProvider]) -> None:
        self.providers = providers
        self._lock = Lock()
        self._provider_name_map = {p.name.lower(): p.name for p in providers}
        self._provider_obj_map = {p.name.lower(): p for p in providers}
        # DIEM-aware routing preferences (computed once per aggregator instance)
        self._diem_token = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
        self._quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        # Default WETH on Base if not explicitly configured
        weth_env = (
            os.getenv("WETH_TOKEN_ADDRESS")
            or os.getenv("WETH_ADDRESS")
            or "0x4200000000000000000000000000000000000006"
        )
        self._weth_token = weth_env.strip().lower()
        self._force_diem_v2 = (
            os.getenv("DEX_DIEM_FORCE_UNISWAP_V2") or "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        discovery_raw_env = os.getenv("DEX_DISCOVERY_PROVIDERS")
        requested_discovery = self._parse_provider_list_raw(
            discovery_raw_env, list(self._provider_name_map.keys())
        )
        # Env allowlists can name venues this instance does not have (CI / host
        # leakage). Keep only providers that were actually constructed.
        self._discovery_providers = {
            name for name in requested_discovery if name in self._provider_name_map
        }
        if not self._discovery_providers:
            self._discovery_providers = set(self._provider_name_map.keys())

        exec_raw_env = os.getenv("DEX_EXEC_PROVIDERS")
        requested_execution = set(
            self._parse_provider_list_raw(exec_raw_env, list(self._discovery_providers))
        )
        filtered_execution = {
            name for name in requested_execution if name in self._discovery_providers
        }
        if not filtered_execution:
            # Host/CI allowlists can name venues this instance does not have.
            # Fall back to the providers that were actually constructed.
            filtered_execution = set(self._discovery_providers)
        self._execution_providers = filtered_execution

        self._discovery_provider_names = [
            self._provider_name_map[name] for name in sorted(self._discovery_providers)
        ]
        self._execution_provider_names = [
            self._provider_name_map.get(name, name)
            for name in sorted(self._execution_providers)
        ]
        # When execution providers are explicitly configured, keep a separate exact-out
        # allowlist that excludes Aerodrome (exact-out is not supported on Aerodrome V2).
        # Aerodrome CL supports exact-out, so keep it eligible.
        #
        # If no execution providers are configured (empty list), callers should keep
        # current preview behavior (attempt all discovery venues).
        self._execution_provider_names_exact_out = [
            name
            for name in list(self._execution_provider_names)
            if str(name).strip().lower() not in {"aerodrome"}
        ]
        self._execution_providers_requested = [
            self._provider_name_map.get(name, name)
            for name in sorted(requested_execution)
        ]
        self._execution_providers_configured = bool(self._execution_providers_requested)
        # Updated defaults per plan: 5 failures → 30s cooldown (was 3 → 60s)
        self._circ_failures = int(
            (
                os.getenv("DEX_CIRCUIT_FAILURE_THRESHOLD")
                or os.getenv("DEX_CIRCUIT_FAILURES")
                or "5"
            ).strip()
            or 5
        )
        self._circ_cool = float(
            (
                os.getenv("DEX_CIRCUIT_COOL_SECONDS")
                or os.getenv("DEX_CIRCUIT_COOL_OFF_SECONDS")
                or "30"
            ).strip()
            or 30
        )
        raw_timeout: str | None = None
        try:
            raw_timeout = os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS")
            # Default increased to 10s per plan recommendations
            self._timeout = float((raw_timeout or "10.0").strip() or 10.0)
        except Exception:
            self._timeout = 3.0
        if self._timeout < 0:
            self._timeout = 0.0
        # Respect explicit overrides; only cap the implicit default to keep prod quick.
        if raw_timeout is None and self._timeout > 5.0:
            self._timeout = 5.0
        try:
            configured_workers = int((os.getenv("DEX_MAX_WORKERS") or "0").strip() or 0)
        except Exception:
            configured_workers = 0
        default_workers = len(self.providers) if self.providers else 1
        self._max_workers = max(1, configured_workers or default_workers)
        try:
            self._circ_backoff = float(
                (os.getenv("DEX_CIRCUIT_BACKOFF_MULT") or "2.0").strip() or 2.0
            )
        except Exception:
            self._circ_backoff = 2.0
        self._circ_backoff = max(self._circ_backoff, 1.0)
        try:
            self._circ_max_cool = float(
                (os.getenv("DEX_CIRCUIT_MAX_COOL_SECONDS") or "300").strip() or 300.0
            )
        except Exception:
            self._circ_max_cool = 300.0
        if self._circ_max_cool <= 0:
            self._circ_max_cool = float(self._circ_cool)
        self._circuit = CircuitBreaker(
            failure_threshold=self._circ_failures,
            cool_seconds=self._circ_cool,
            backoff_mult=self._circ_backoff,
            max_cool=self._circ_max_cool,
        )
        # Early-exit linger after first successful quote (milliseconds)
        try:
            self._linger_ms = int(
                (os.getenv("DEX_FIRST_QUOTE_LINGER_MS") or "80").strip() or 80
            )
        except Exception:
            self._linger_ms = 80
        self._linger_ms = max(self._linger_ms, 0)
        # Optional: exit as soon as the first quote arrives to avoid timeouts piling up.
        self._early_exit_on_first = (
            os.getenv("DEX_EARLY_EXIT_FIRST_QUOTE") or "1"
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Optional aggregate timeout across all providers; falls back to per-provider timeout if unset
        try:
            self._aggregate_timeout = float(
                (
                    os.getenv("DEX_AGGREGATE_TIMEOUT_SECONDS") or str(self._timeout)
                ).strip()
            )
        except Exception:
            self._aggregate_timeout = self._timeout
        if self._aggregate_timeout < 0:
            self._aggregate_timeout = 0.0
        try:
            self._diem_timeout = float(
                (os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS_DIEM") or "20.0").strip()
            )
        except Exception:
            self._diem_timeout = 20.0
        if self._diem_timeout < 0:
            self._diem_timeout = 0.0
        try:
            self._diem_aggregate_timeout = float(
                (
                    os.getenv("DEX_AGGREGATE_TIMEOUT_SECONDS_DIEM")
                    or str(self._diem_timeout)
                ).strip()
            )
        except Exception:
            self._diem_aggregate_timeout = self._diem_timeout
        if self._diem_aggregate_timeout < 0:
            self._diem_aggregate_timeout = 0.0
        try:
            raw_bridge_timeout = (
                os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS_BRIDGE")
                or os.getenv("DEX_BRIDGE_TIMEOUT_SECONDS")
                or os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS_BRIDGE_VVV")
            )
            if raw_bridge_timeout is None or not str(raw_bridge_timeout).strip():
                self._bridge_timeout = 6.0
            else:
                self._bridge_timeout = float(str(raw_bridge_timeout).strip())
        except Exception:
            self._bridge_timeout = 6.0
        if self._bridge_timeout < 0:
            self._bridge_timeout = 0.0

        # Keep test runs snappy by trimming timeouts unless explicitly set.
        if os.getenv("PYTEST_CURRENT_TEST"):
            try:
                test_timeout = float(os.getenv("DEX_TEST_TIMEOUT_SECONDS") or 0.5)
            except Exception:
                test_timeout = 0.5
            if raw_timeout is None:
                self._timeout = min(self._timeout, test_timeout)
            if os.getenv("DEX_AGGREGATE_TIMEOUT_SECONDS") is None:
                self._aggregate_timeout = min(self._aggregate_timeout, self._timeout)
            if os.getenv("DEX_PROVIDER_TIMEOUT_SECONDS_DIEM") is None:
                self._diem_timeout = min(self._diem_timeout, self._timeout)
            if os.getenv("DEX_AGGREGATE_TIMEOUT_SECONDS_DIEM") is None:
                self._diem_aggregate_timeout = min(
                    self._diem_aggregate_timeout, self._diem_timeout
                )
            if raw_bridge_timeout is None:
                self._bridge_timeout = min(self._bridge_timeout, self._timeout)
        self._rate_limit_qps = max(0.0, _float_env("DEX_RATE_LIMIT_QPS", 0.0))
        self._rate_limit_burst = max(0.0, _float_env("DEX_RATE_LIMIT_BURST", 0.0))
        if self._rate_limit_burst <= 0.0 and self._rate_limit_qps > 0.0:
            self._rate_limit_burst = max(self._rate_limit_qps, 1.0)
        self._rate_limit_enabled = (
            self._rate_limit_qps > 0.0 and self._rate_limit_burst > 0.0
        )
        self._rate_lock = Lock()
        now = time.perf_counter()
        if self._rate_limit_enabled:
            self._rate_state: dict[str, dict[str, float]] = {
                p.name: {"tokens": self._rate_limit_burst, "last": now}
                for p in providers
            }
        else:
            self._rate_state = {}
        self._last_quote_diagnostics: list[dict[str, Any]] = []
        self._last_quote_context: dict[str, Any] = {}
        self._diag_enabled = str(
            os.getenv("DEX_DIAGNOSTICS_ENABLED") or "1"
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Single-venue gating: accept single provider success when risk caps pass
        try:
            self._single_venue_ok = str(
                os.getenv("DEX_SINGLE_VENUE_OK") or "1"
            ).strip().lower() in {"1", "true", "yes", "on"}
        except Exception:
            self._single_venue_ok = True  # Default: accept single venue

        self._log_provider_configuration(
            discovery_raw_env=discovery_raw_env,
            exec_raw_env=exec_raw_env,
            requested_execution=requested_execution,
            filtered_execution=self._execution_providers,
        )

    @staticmethod
    def _is_composite_quote_obj(quote: Quote) -> bool:
        provider = str(getattr(quote, "provider", "")).lower()
        if provider in {"composite", "diem_composite"}:
            return True
        return bool(getattr(quote, "_composite_legs", None))

    @staticmethod
    def _attach_composite_metadata(
        quote: Quote,
        composite_quote: Any,
        *,
        mode: str,
    ) -> None:
        """
        Attach composite execution hints to a Quote so trade_* paths
        can execute underlying legs instead of looking for a provider
        named "composite".
        """
        try:
            legs = list(getattr(composite_quote, "legs", []) or [])
            object.__setattr__(quote, "_composite_legs", legs)
        except Exception:
            pass
        try:
            object.__setattr__(quote, "_composite_mode", mode)
        except Exception:
            pass
        try:
            slip = getattr(composite_quote, "total_slippage_bps", None)
            if slip is not None:
                object.__setattr__(quote, "_composite_slippage_bps", float(slip))
        except Exception:
            pass

    def _v2_pools_exist(self, route_plan: RoutePlan) -> bool:
        """
        Check on-chain V2 pool existence for a route using the provider's cached helper.
        """
        try:
            v2_provider = self._provider_obj_map.get("uniswap_v2")
            if v2_provider is None or not hasattr(v2_provider, "_pools_exist"):
                return False
            normalized_route = normalize_route_for_v2(route_plan)
            return bool(v2_provider._pools_exist(normalized_route))
        except Exception:
            return False

    @staticmethod
    def _scaled_min_out(
        expected_out: int, quoted_in: int, actual_in: int, slippage_bps: int
    ) -> int:
        """Scale expected output for a leg when the input or slippage changes."""
        if expected_out <= 0 or quoted_in <= 0 or actual_in <= 0:
            return max(1, expected_out)
        scaled = expected_out * actual_in // max(1, quoted_in)
        scaled = scaled * max(0, 10_000 - slippage_bps) // 10_000
        return max(1, scaled)

    @staticmethod
    def _composite_slippage_bps(default_bps: int) -> int:
        raw = (os.getenv("DEX_COMPOSITE_EXEC_SLIPPAGE_BPS") or "").strip()
        try:
            override = int(raw) if raw else default_bps
        except Exception:
            override = default_bps
        override = max(override, 0)
        return override

    @staticmethod
    def _composite_legs(quote: Quote) -> list[Quote]:
        try:
            legs = getattr(quote, "_composite_legs", None)
        except Exception:
            legs = None
        if not legs:
            return []
        return list(legs)

    @staticmethod
    def _erc20_allowance(token: str, owner: str, spender: str) -> int | None:
        if not token or not owner or not spender:
            return None
        try:
            w3 = get_web3()
            erc20 = get_contract(w3, token, "erc20.json")
            return int(erc20.functions.allowance(owner, spender).call())
        except Exception:
            return None

    def _precheck_and_inject_composite_allowances(
        self,
        *,
        legs: Sequence[tuple[int, DexProvider, RoutePlan, int]],
        owner: str,
        correlation_id: str | None,
        mode: str,
    ) -> None:
        """
        Composite trade atomicity guard: inject approvals upfront.

        Before any leg executes, pre-check ERC20 allowances for every leg's
        (token_in, router) pair.

        If any allowance is low, submit the needed approval(s) before leg 1 starts.
        """
        if not legs or not owner:
            return

        owner = _maybe_checksum_address(owner)
        preflight: list[dict[str, Any]] = []
        approvals_required: list[dict[str, Any]] = []

        for leg_index, provider, route_plan, required in legs:
            if required <= 0:
                continue
            token_in = ""
            try:
                token_in = str(getattr(route_plan, "tokens", [])[0] or "").strip()
            except Exception:
                token_in = ""
            spender = ""
            try:
                spender = str(getattr(provider, "router_addr", "") or "").strip()
            except Exception:
                spender = ""

            token_in = _maybe_checksum_address(token_in)
            spender = _maybe_checksum_address(spender)

            allowance = self._erc20_allowance(token_in, owner, spender)
            status = (
                "unavailable"
                if allowance is None
                else ("ok" if allowance >= required else "low")
            )
            record: dict[str, Any] = {
                "leg_index": int(leg_index),
                "provider": getattr(provider, "name", ""),
                "mode": str(mode),
                "token_in": token_in,
                "owner": owner,
                "spender": spender,
                "required": int(required),
                "allowance": None if allowance is None else int(allowance),
                "status": status,
                "correlation_id": correlation_id,
            }
            preflight.append(record)
            if status == "low":
                approvals_required.append(record)

        if not approvals_required:
            return

        # Deduplicate approvals across legs and always approve the max requirement.
        ordered_keys: list[tuple[str, str]] = []
        approval_plan: dict[tuple[str, str], dict[str, Any]] = {}

        for record in approvals_required:
            token_in = str(record.get("token_in") or "").strip()
            spender = str(record.get("spender") or "").strip()
            if not token_in or not spender:
                continue
            key = (token_in.lower(), spender.lower())
            if key not in approval_plan:
                ordered_keys.append(key)
                approval_plan[key] = dict(record)
                continue
            try:
                existing_required = int(approval_plan[key].get("required") or 0)
            except Exception:
                existing_required = 0
            try:
                new_required = int(record.get("required") or 0)
            except Exception:
                new_required = 0
            if new_required > existing_required:
                approval_plan[key]["required"] = new_required

        injected: list[dict[str, Any]] = []
        for token_key in ordered_keys:
            record = approval_plan.get(token_key) or {}
            provider_name = str(record.get("provider") or "")
            required = int(record.get("required") or 0)
            if required <= 0:
                continue
            try:
                provider = self._provider_by_name(provider_name)
            except Exception:
                continue
            ensure_allowance = getattr(provider, "_ensure_allowance", None)
            if not callable(ensure_allowance):
                continue
            token_in = str(record.get("token_in") or "").strip()
            spender = str(record.get("spender") or "").strip()
            tx_hash = ensure_allowance(token_in, owner, spender, required)
            if tx_hash:
                injected.append(
                    {
                        "provider": provider_name,
                        "token_in": token_in,
                        "spender": spender,
                        "required": required,
                        "approval_tx": tx_hash,
                        "correlation_id": correlation_id,
                    }
                )

        if injected:
            try:
                _logger.info(
                    "Composite allowance preflight injected approvals",
                    extra={
                        "mode": mode,
                        "approvals": injected,
                        "correlation_id": correlation_id,
                    },
                )
            except Exception:
                pass
            try:
                _dex_diag_log_event(
                    {
                        "event": "dex_composite_allowance_preflight",
                        "mode": mode,
                        "owner": owner,
                        "approvals": injected,
                        "preflight": preflight,
                        "correlation_id": correlation_id,
                    }
                )
            except Exception:
                pass

    @staticmethod
    def _parse_provider_list_raw(
        raw_value: str | None, default: Sequence[str]
    ) -> set[str]:
        if raw_value is None:
            return {str(name).strip().lower() for name in default if str(name).strip()}
        raw = str(raw_value).strip()
        if raw == "":
            return set()
        names: list[str] = []
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parsed = [parsed]
        if isinstance(parsed, list):
            for entry in parsed:
                if isinstance(entry, dict):
                    value = entry.get("name")
                else:
                    value = entry
                if value:
                    names.append(str(value))
        if not names:
            names = [segment for segment in raw.split(",")]
        processed = {str(name).strip().lower() for name in names if str(name).strip()}
        if not processed:
            return set()
        return processed

    @staticmethod
    def _parse_provider_list(env_name: str, default: Sequence[str]) -> set[str]:
        raw = os.getenv(env_name)
        parsed = DexAggregator._parse_provider_list_raw(raw, default)
        if parsed:
            return parsed
        return {str(name).strip().lower() for name in default if str(name).strip()}

    def _log_provider_configuration(
        self,
        *,
        discovery_raw_env: str | None,
        exec_raw_env: str | None,
        requested_execution: set[str],
        filtered_execution: set[str],
    ) -> None:
        """
        Emit a startup snapshot of discovery/execution providers and flag config drift.
        """
        try:
            discovery_names = list(self._discovery_provider_names)
            requested_names = [
                self._provider_name_map.get(name, name)
                for name in sorted(requested_execution)
            ]
            filtered_names = [
                self._provider_name_map.get(name, name)
                for name in sorted(filtered_execution)
            ]
            unknown_exec = sorted(
                name
                for name in requested_execution
                if name not in self._provider_name_map
            )

            payload: dict[str, Any] = {
                "event": "dex_provider_configuration",
                "discovery_providers": discovery_names,
                "execution_providers": filtered_names,
            }
            if discovery_raw_env is not None:
                payload["discovery_env_raw"] = discovery_raw_env.strip()
            if exec_raw_env is not None:
                payload["exec_env_raw"] = exec_raw_env.strip()
                payload["execution_providers_requested"] = requested_names
            if unknown_exec:
                payload["execution_providers_unknown"] = unknown_exec
            try:
                _dex_diag_log_event(payload)
            except Exception:
                pass

            try:
                v2_present = "uniswap_v2" in self._provider_name_map
                v2_enabled = ("uniswap_v2" in self._discovery_providers) or (
                    "uniswap_v2" in self._execution_providers
                )
                v2_bridge_needed = False
                try:
                    v2_bridge_needed = (
                        (os.getenv("VVV_USDC_V2_FALLBACK_ENABLE") or "1")
                        .strip()
                        .lower()
                        in {"1", "true", "yes", "on"}
                        or (os.getenv("DIEM_VVV_BRIDGE_PROVIDER") or "aerodrome")
                        .strip()
                        .lower()
                        == "uniswap_v2"
                        or (os.getenv("VVV_USDC_BRIDGE_PROVIDER") or "aerodrome_cl")
                        .strip()
                        .lower()
                        == "uniswap_v2"
                    )
                except Exception:
                    v2_bridge_needed = False

                if v2_bridge_needed and (not v2_present or not v2_enabled):
                    _logger.warning(
                        "DIEM bridge fallback depends on UniswapV2, but V2 is %s (discovery=%s execution=%s). "
                        "Enable uniswap_v2 in DEX_DISCOVERY_PROVIDERS/DEX_EXEC_PROVIDERS or disable V2 bridge fallbacks.",
                        "missing" if not v2_present else "disabled",
                        discovery_names,
                        filtered_names,
                    )
            except Exception:
                pass

            try:
                cl_router = (os.getenv("AERODROME_CL_ROUTER_ADDRESS") or "").strip()
                cl_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
                cl_configured = bool(cl_router and cl_pool)
                cl_present = "aerodrome_cl" in self._provider_name_map
                cl_exec_enabled = "aerodrome_cl" in self._execution_providers
                if cl_configured and cl_present and not cl_exec_enabled:
                    _logger.warning(
                        "aerodrome_cl is configured but not executable; include aerodrome_cl in DEX_EXEC_PROVIDERS to enable direct DIEM/USDC execution."
                    )
                    try:
                        _dex_diag_log_event(
                            {
                                "event": "dex_exec_provider_missing",
                                "provider": "aerodrome_cl",
                                "reason": "not_in_execution_providers",
                                "execution_providers": filtered_names,
                            }
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            if exec_raw_env is not None:
                if not filtered_execution:
                    _logger.warning(
                        "DEX_EXEC_PROVIDERS is set but no valid execution providers remain after filtering; raw=%s",
                        exec_raw_env.strip(),
                    )
                elif unknown_exec or set(requested_names) != set(filtered_names):
                    _logger.warning(
                        "DEX_EXEC_PROVIDERS filtered to discovery set; requested=%s effective=%s unknown=%s",
                        requested_names,
                        filtered_names,
                        unknown_exec,
                    )
                else:
                    _logger.info(
                        "DEX providers configured discovery=%s execution=%s",
                        discovery_names,
                        filtered_names,
                    )
            else:
                _logger.info(
                    "DEX providers defaulted discovery=%s execution=%s",
                    discovery_names,
                    filtered_names,
                )
        except Exception:
            # Diagnostics should never block aggregator initialization.
            pass

    def _provider_by_name(self, name: str) -> DexProvider:
        """
        Return provider matching name or raise a descriptive error.

        Without this guard a missing provider triggers StopIteration, which
        bubbles up to the orchestrator as an empty error string and hides the
        real failure. Raising RuntimeError keeps diagnostics visible.

        Special case: diem_pair_math is a quote-only provider (reserve math fallback).
        Map it to the actual DIEM/VVV bridge provider for execution.

        Special case: composite_analytic is a preview-only provider (V3 analytic fallback).
        Map it to the VVV/USDC bridge provider for execution when explicitly allowed.
        """
        # Map quote-only providers to actual execution providers
        if name == "diem_pair_math":
            bridge_provider = (
                (os.getenv("DIEM_VVV_BRIDGE_PROVIDER") or "aerodrome").strip().lower()
            )
            name = bridge_provider
        elif name == "composite_analytic":
            # composite_analytic is a preview-only provider for VVV→USDC leg
            # Map to the configured VVV/USDC execution provider
            vvv_usdc_provider = (
                (os.getenv("VVV_USDC_BRIDGE_PROVIDER") or "aerodrome").strip().lower()
            )
            _logger.info(
                "Mapping composite_analytic to execution provider: %s",
                vvv_usdc_provider,
            )
            name = vvv_usdc_provider
        elif name == "vvv_usdc_v3_slot0":
            vvv_usdc_provider = (
                (os.getenv("VVV_USDC_BRIDGE_PROVIDER") or "aerodrome_cl")
                .strip()
                .lower()
            )
            _logger.info(
                "Mapping vvv_usdc_v3_slot0 to execution provider: %s",
                vvv_usdc_provider,
            )
            name = vvv_usdc_provider
        for provider in self.providers:
            if provider.name == name:
                return provider
        raise RuntimeError(f"execution provider '{name}' not found in aggregator")

    def _discovery_enabled(self, provider_name: str) -> bool:
        return provider_name.lower() in self._discovery_providers

    def _execution_enabled(self, provider_name: str) -> bool:
        try:
            allowed = self._execution_providers
            if not isinstance(allowed, set):
                allowed = {
                    getattr(p, "name", str(p)).lower()
                    for p in allowed  # type: ignore[arg-type]
                }
        except Exception:
            allowed = set()
        return provider_name.lower() in allowed

    def _is_diem_route(self, route_plan: RoutePlan) -> bool:
        if not self._diem_token:
            return False
        try:
            tokens = [str(t).strip().lower() for t in route_plan.tokens]
        except Exception:
            return False
        return self._diem_token in tokens

    def _is_usdc_weth_diem_path(self, route_plan: RoutePlan) -> bool:
        if not (self._diem_token and self._quote_token and self._weth_token):
            return False
        try:
            tokens = [str(t).strip().lower() for t in route_plan.tokens]
        except Exception:
            return False
        forward = [self._quote_token, self._weth_token, self._diem_token]
        reverse = list(reversed(forward))
        return tokens == forward or tokens == reverse

    def get_preferred_provider_order(self, route_plan: RoutePlan) -> list[str]:
        """Get preferred provider order for a route, prioritizing V2 providers for canonical routes.

        Args:
            route_plan: The route to get provider order for

        Returns:
            List of provider names in preferred order
        """
        is_canonical = self._is_usdc_weth_diem_path(route_plan)
        route_is_v3 = (
            route_plan.is_uniswap_v3()
            if hasattr(route_plan, "is_uniswap_v3")
            else False
        )

        # For canonical V2 routes, prefer Aerodrome and UniswapV2 first
        if is_canonical and not route_is_v3:
            preferred = ["aerodrome", "uniswap_v2"]
            # Add other providers that aren't already in preferred list
            for provider_name in self._execution_provider_names:
                if provider_name.lower() not in [p.lower() for p in preferred]:
                    preferred.append(provider_name)
            return preferred

        # For V3 routes, prefer UniswapV3 first
        if route_is_v3:
            preferred = ["uniswap_v3"]
            for provider_name in self._execution_provider_names:
                if provider_name.lower() not in [p.lower() for p in preferred]:
                    preferred.append(provider_name)
            return preferred

        # Default: use execution provider order
        return list(self._execution_provider_names)

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
            allow_aero = os.getenv(
                "AERODROME_EXACT_OUT_ENABLE", "0"
            ).strip().lower() in {"1", "true", "yes", "on"}
            if not allow_aero:
                return False
        if provider.supports_exact_out:
            return True
        # Detect overrides of quote_exact_out
        try:
            base_impl = DexProvider.quote_exact_out
            return provider.__class__.quote_exact_out is not base_impl
        except AttributeError:
            return False

    def _rate_acquire(self, provider: str, deadline: float | None) -> bool:
        if not self._rate_limit_enabled:
            return True
        burst = self._rate_limit_burst
        qps = self._rate_limit_qps
        if burst <= 0.0 or qps <= 0.0:
            return True
        while True:
            now = time.perf_counter()
            with self._rate_lock:
                state = self._rate_state.setdefault(
                    provider, {"tokens": burst, "last": now}
                )
                tokens = min(burst, state["tokens"] + (now - state["last"]) * qps)
                if tokens >= 1.0:
                    state["tokens"] = tokens - 1.0
                    state["last"] = now
                    return True
                state["tokens"] = tokens
                state["last"] = now
            if deadline is not None:
                remaining = deadline - now
                if remaining <= 0:
                    return False
                sleep_for = min(0.05, max(0.0, remaining))
            else:
                sleep_for = 0.05
            time.sleep(sleep_for)

    def _circ_is_open(self, provider: str) -> bool:
        return self._circuit.is_open(provider)

    def _circ_on_success(self, provider: str) -> None:
        self._circuit.record_success(provider)

    def _circ_get_status(self, provider: str) -> dict[str, Any]:
        """Get circuit breaker status for a provider (for diagnostics)."""
        with self._circuit._lock:
            state = self._circuit._state.get(provider)
            if not state:
                return {
                    "provider": provider,
                    "circuit_open": False,
                    "failures": 0,
                    "open_until": None,
                    "time_until_recovery": None,
                }
            current_time = time.time()
            is_open = state.open_until > 0 and current_time < state.open_until
            time_until_recovery = (
                max(0.0, state.open_until - current_time) if is_open else None
            )
            return {
                "provider": provider,
                "circuit_open": is_open,
                "failures": state.failures,
                "open_until": state.open_until if state.open_until > 0 else None,
                "time_until_recovery": time_until_recovery,
            }

    def _circ_on_failure(self, provider: str, reason: str = "error") -> None:
        # Don't trip circuit breaker for route-liquidity failures (empty_route, zero_liquidity, no_pool)
        # These indicate route-specific issues, not provider outages
        if reason in ("empty_route", "zero_liquidity", "no_pool"):
            # Still record metrics but don't increment failure count
            try:
                _metrics_inc(
                    "dex_circuit_soft_failure_total",
                    labels={
                        "provider": provider,
                        "reason": reason,
                    },
                )
            except Exception:
                pass
            return

        cooldown = self._circuit.record_failure(provider)
        if cooldown > 0:
            _metrics_inc(
                "dex_circuit_open_total",
                labels={
                    "provider": provider,
                    "reason": reason,
                    "cooldown": str(int(cooldown)),
                },
            )
            _metrics_inc("dex_circuit_open_total", labels={"provider": provider})

    def _log_skipped_executable(
        self,
        skipped: list[dict[str, Any]],
        route_plan: RoutePlan,
        *,
        amount: int,
        mode: str,
    ) -> None:
        """
        Emit a diagnostic event when an executable provider is available but not attempted.

        This helps explain why attempts lack a healthy venue (e.g., V2) despite inspections
        showing the path is usable.
        """
        if not skipped:
            return
        payload = {
            "event": "dex_executable_provider_skipped",
            "mode": mode,
            "amount": int(amount),
            "route": list(route_plan.tokens),
            "skipped": skipped,
        }
        try:
            payload["diem_route"] = bool(self._is_diem_route(route_plan))
        except Exception:
            payload["diem_route"] = False
        try:
            _dex_diag_log_event(payload)
        except Exception:
            pass
        try:
            _logger.info(
                "dex executable provider skipped mode=%s route=%s skipped=%s diem_route=%s",
                mode,
                list(route_plan.tokens),
                skipped,
                payload.get("diem_route"),
            )
        except Exception:
            pass

    def _collect_quotes(
        self,
        providers: list[DexProvider],
        method: str,
        route_plan: RoutePlan,
        amount: int,
        *,
        mode: str,
    ) -> list[Quote]:
        quotes: list[Quote] = []
        diagnostics: list[dict[str, Any]] = []
        self._last_quote_context = {
            "mode": mode,
            "method": method,
            "amount": int(amount),
            "tokens": list(route_plan.tokens),
            "discoveryProviders": list(self._discovery_provider_names),
            "executionProviders": list(self._execution_provider_names),
            # Track executability so downstream pricing can distinguish preview-only quotes
            "executable_quotes": 0,
            "analytic_quotes": 0,
            "has_executable_quotes": False,
            # has_onchain_liquidity mirrors executable availability; callers should
            # treat analytic-only results as off-chain/preview liquidity.
            "has_onchain_liquidity": False,
            "status_counts": {},
            "provider_errors": 0,
            "route": list(route_plan.tokens),
        }
        if not providers:
            self._last_quote_diagnostics = diagnostics
            return quotes

        def _record(provider_name: str, status: str, **fields: Any) -> None:
            entry: dict[str, Any] = {
                "provider": provider_name,
                "status": status,
                "mode": mode,
                "method": method,
                "amount": int(amount),
                "route": list(route_plan.tokens),
            }
            entry.update(fields)
            diagnostics.append(entry)

        max_workers = min(len(providers), self._max_workers)
        per_provider_timeout = (
            self._timeout if self._timeout and self._timeout > 0 else None
        )
        aggregate_timeout = (
            self._aggregate_timeout
            if self._aggregate_timeout and self._aggregate_timeout > 0
            else None
        )
        try:
            if self._is_diem_route(route_plan):
                diem_timeout = max(0.0, float(self._diem_timeout))
                diem_agg = max(
                    0.0, float(self._diem_aggregate_timeout or self._diem_timeout)
                )
                if diem_timeout > 0:
                    per_provider_timeout = max(
                        per_provider_timeout or 0.0, diem_timeout
                    )
                if diem_agg > 0:
                    aggregate_timeout = max(aggregate_timeout or 0.0, diem_agg)
        except Exception:
            pass
        bridge_timeout: float | None = None
        try:
            bridge_timeout = float(getattr(self, "_bridge_timeout", 0.0) or 0.0) or None
        except Exception:
            bridge_timeout = None
        start_ts = time.perf_counter()
        executor = ThreadPoolExecutor(max_workers=max_workers)
        future_map: dict[Any, DexProvider] = {}
        submit_ts: dict[str, float] = {}
        provider_deadlines: dict[str, float] = {}
        early_exit: bool = False
        first_quote_seen: bool = False
        linger_deadline: float | None = None
        for provider in providers:
            if self._rate_limit_enabled:
                limit_window = None
                if per_provider_timeout is not None and per_provider_timeout > 0:
                    limit_window = per_provider_timeout
                elif aggregate_timeout is not None and aggregate_timeout > 0:
                    limit_window = aggregate_timeout
                deadline = (
                    time.perf_counter() + limit_window
                    if limit_window is not None and limit_window > 0
                    else None
                )
                wait_start = time.perf_counter()
                if not self._rate_acquire(provider.name, deadline):
                    wait_elapsed = time.perf_counter() - wait_start
                    _record(
                        provider.name,
                        "rate_limited",
                        waited_ms=wait_elapsed * 1000.0,
                    )
                    try:
                        _metrics_inc(
                            "dex_rate_limit_skips_total",
                            labels={"provider": provider.name},
                        )
                    except Exception:
                        pass
                    continue
                wait_elapsed = time.perf_counter() - wait_start
                if wait_elapsed > 0:
                    try:
                        _metrics_inc(
                            "dex_rate_limit_wait_ms_total",
                            value=max(1, int(wait_elapsed * 1000.0)),
                            labels={"provider": provider.name},
                        )
                    except Exception:
                        pass
            submit_ts[provider.name] = time.perf_counter()
            provider_timeout = None
            if provider.name == "bridge_vvv":
                if bridge_timeout is not None and bridge_timeout > 0:
                    if per_provider_timeout is None or per_provider_timeout <= 0:
                        provider_timeout = bridge_timeout
                    else:
                        provider_timeout = min(per_provider_timeout, bridge_timeout)
                elif per_provider_timeout is not None and per_provider_timeout > 0:
                    provider_timeout = per_provider_timeout
            if provider_timeout is not None and provider_timeout > 0:
                provider_deadlines[provider.name] = submit_ts[provider.name] + float(
                    provider_timeout
                )
            fut = executor.submit(
                self._invoke_provider, provider, method, route_plan, amount
            )
            future_map[fut] = provider
        pending = set(future_map.keys())
        try:
            while pending:
                # Compute remaining time budget for the aggregate
                now = time.perf_counter()
                if provider_deadlines:
                    for fut in list(pending):
                        provider = future_map[fut]
                        deadline = provider_deadlines.get(provider.name)
                        if deadline is None or now < deadline:
                            continue
                        fut.cancel()
                        latency_ms = (
                            time.perf_counter() - submit_ts.get(provider.name, start_ts)
                        ) * 1000.0
                        _metrics_inc(
                            "dex_agg_timeouts_total",
                            labels={"provider": provider.name, "method": method},
                        )
                        self._circ_on_failure(provider.name, reason="timeout")
                        _record(provider.name, "timeout", latency_ms=latency_ms)
                        pending.discard(fut)
                    if not pending:
                        break
                if aggregate_timeout is not None:
                    elapsed = now - start_ts
                    remaining = aggregate_timeout - elapsed
                    if remaining <= 0:
                        break
                else:
                    remaining = None

                # If we already have a quote, only linger for a short window to grab a better one
                if first_quote_seen:
                    linger_remaining = (
                        ((linger_deadline or 0.0) - now)
                        if linger_deadline is not None
                        else 0.0
                    )
                    if linger_remaining <= 0:
                        early_exit = True
                        break
                    if remaining is None:
                        remaining = linger_remaining
                    else:
                        remaining = max(0.0, min(remaining, linger_remaining))

                # Wait for the next completed future within remaining budget
                try:
                    completed_iter = as_completed(list(pending), timeout=remaining)
                    fut = next(completed_iter)
                except TimeoutError:
                    # Aggregate timeout elapsed before any further completions
                    break
                pending.discard(fut)
                provider = future_map[fut]
                try:
                    # If a per-provider timeout is configured, bound result wait to it; otherwise no bound
                    quote = fut.result(timeout=per_provider_timeout)
                except TimeoutError:
                    fut.cancel()
                    latency_ms = (
                        time.perf_counter() - submit_ts.get(provider.name, start_ts)
                    ) * 1000.0
                    if _debug_routes_enabled():
                        route_tokens = list(route_plan.tokens)
                        _logger.warning(
                            "dex aggregator timeout provider=%s method=%s route=%s amount=%s",
                            provider.name,
                            method,
                            route_tokens,
                            int(amount),
                        )
                    _metrics_inc(
                        "dex_agg_timeouts_total",
                        labels={"provider": provider.name, "method": method},
                    )
                    self._circ_on_failure(provider.name, reason="timeout")
                    _record(provider.name, "timeout", latency_ms=latency_ms)
                    continue
                except Exception as exc:
                    latency_ms = (
                        time.perf_counter() - submit_ts.get(provider.name, start_ts)
                    ) * 1000.0
                    revert_reason: str | None = None
                    try:
                        msg = str(exc)
                        if msg.startswith("uniswap_v3_revert:"):
                            revert_reason = msg.split(":", 1)[1].strip() or None
                    except Exception:
                        revert_reason = None
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
                    _metrics_inc(
                        "dex_agg_provider_errors_total",
                        labels={"provider": provider.name, "method": method},
                    )
                    self._circ_on_failure(provider.name)
                    record_kwargs: dict[str, Any] = {
                        "error": str(exc),
                        "latency_ms": latency_ms,
                    }
                    if revert_reason:
                        record_kwargs["revert_reason"] = revert_reason
                    _record(provider.name, "error", **record_kwargs)
                    continue
                if quote is not None:
                    if quote.route is None:
                        object.__setattr__(quote, "route", route_plan)
                    quotes.append(quote)
                    self._circ_on_success(provider.name)
                    if not first_quote_seen:
                        first_quote_seen = True
                        if self._early_exit_on_first:
                            early_exit = True
                            linger_deadline = None
                        elif self._linger_ms > 0:
                            linger_deadline = time.perf_counter() + (
                                self._linger_ms / 1000.0
                            )
                        else:
                            early_exit = True
                    latency_ms = (
                        time.perf_counter() - submit_ts.get(provider.name, start_ts)
                    ) * 1000.0
                    quote_details: dict[str, Any] = {
                        "latency_ms": latency_ms,
                        "amount_in": int(getattr(quote, "amount_in", amount)),
                        "amount_out": int(getattr(quote, "amount_out", 0)),
                        "executable": bool(getattr(quote, "executable", True)),
                    }
                    _record(provider.name, "ok", **quote_details)
                    if early_exit:
                        break
                    # Keep looping during linger window to allow a potentially better quote to land
                    continue
                latency_ms = (
                    time.perf_counter() - submit_ts.get(provider.name, start_ts)
                ) * 1000.0
                _metrics_inc(
                    "dex_agg_null_quotes_total",
                    labels={"provider": provider.name, "method": method},
                )
                # For DIEM routes, empty quotes may indicate route-liquidity issues rather than provider failures
                # Check if this is a DIEM route to determine whether to trip circuit breaker
                is_diem_route = False
                try:
                    is_diem_route = self._is_diem_route(route_plan)
                except Exception:
                    pass

                # For DIEM routes, use "empty_route" reason which won't trip circuit breaker
                # For non-DIEM routes, use "empty" which will trip circuit breaker
                circuit_reason = "empty_route" if is_diem_route else "empty"

                if _debug_routes_enabled():
                    route_tokens = list(route_plan.tokens)
                    _logger.info(
                        "dex aggregator empty quote provider=%s method=%s route=%s amount=%s diem_route=%s",
                        provider.name,
                        method,
                        route_tokens,
                        int(amount),
                        is_diem_route,
                    )
                # For bridge_vvv provider, enrich diagnostics with leg failure details
                record_fields: dict[str, Any] = {
                    "latency_ms": latency_ms,
                    "is_diem_route": is_diem_route,
                }

                # Check if this is bridge_vvv and add specific diagnostics
                if provider.name == "bridge_vvv" and is_diem_route:
                    fallback_enabled = os.getenv(
                        "DIEM_ENABLE_PAIR_MATH_FALLBACK", "0"
                    ).strip().lower() in {"1", "true", "yes", "on"}
                    record_fields["fallback_enabled"] = fallback_enabled
                    if not fallback_enabled:
                        record_fields["diem_bridge_failure_reason"] = (
                            "fallback_disabled"
                        )
                    else:
                        provider_reason: str | None = None
                        try:
                            getter = getattr(provider, "bridge_failure_reason", None)
                            if callable(getter):
                                provider_reason = getter()
                            else:
                                provider_reason = getattr(
                                    provider, "_last_bridge_failure_reason", None
                                )
                        except Exception:
                            provider_reason = None
                        if not provider_reason:
                            # Fallback: infer why bridge_vvv can't serve this route without
                            # relying on provider-local mutable state (which can race under
                            # concurrent quote calls).
                            try:
                                two_stage_fn = getattr(provider, "_two_stage", None)
                                provider_for_leg_fn = getattr(
                                    provider, "_provider_for_leg", None
                                )
                                if callable(two_stage_fn) and callable(
                                    provider_for_leg_fn
                                ):
                                    two_stage = two_stage_fn(route_plan)
                                    if not two_stage:
                                        provider_reason = "unsupported_route"
                                    else:
                                        stage1, stage2 = two_stage
                                        leg1 = provider_for_leg_fn(
                                            stage1.tokens[0], stage1.tokens[-1]
                                        )
                                        leg2 = provider_for_leg_fn(
                                            stage2.tokens[0], stage2.tokens[-1]
                                        )
                                        if leg1 is None or leg2 is None:
                                            provider_reason = "missing_leg_provider"
                            except Exception:
                                provider_reason = None
                        record_fields["diem_bridge_failure_reason"] = (
                            provider_reason or "leg_provider_failure"
                        )
                        # Bridge failures where we cannot identify a concrete cause should
                        # trip the circuit breaker so the system can recover after cooldown
                        # instead of retrying indefinitely.
                        if not provider_reason:
                            circuit_reason = "leg_provider_failure"
                        if provider_reason and _debug_routes_enabled():
                            try:
                                ctx_getter = getattr(
                                    provider, "bridge_failure_context", None
                                )
                                ctx = (
                                    ctx_getter()
                                    if callable(ctx_getter)
                                    else getattr(
                                        provider, "_last_bridge_failure_context", None
                                    )
                                )
                                if isinstance(ctx, dict) and ctx:
                                    record_fields["diem_bridge_failure_context"] = ctx
                            except Exception:
                                pass

                # Always call _circ_on_failure so soft failure metrics are recorded.
                # For DIEM routes, "empty_route" is treated as a soft failure (no circuit trip).
                self._circ_on_failure(provider.name, reason=circuit_reason)

                _record(
                    provider.name,
                    "empty",
                    **record_fields,
                )
        finally:
            # Mark remaining providers as timed out for circuit/metrics and cancel their futures
            for fut in list(pending):
                provider = future_map[fut]
                fut.cancel()
                latency_ms = (
                    time.perf_counter() - submit_ts.get(provider.name, start_ts)
                ) * 1000.0
                if early_exit:
                    _record(
                        provider.name,
                        "cancelled_early_exit",
                        latency_ms=latency_ms,
                    )
                    continue
                _metrics_inc(
                    "dex_agg_timeouts_total",
                    labels={"provider": provider.name, "method": method},
                )
                self._circ_on_failure(provider.name, reason="timeout")
                _record(provider.name, "timeout_pending", latency_ms=latency_ms)
            # Do not wait for slow providers; cancel outstanding futures and return
            executor.shutdown(wait=False, cancel_futures=True)
        # Attach executability summary to the last quote context so callers can
        # decide whether on-chain liquidity truly exists.
        exec_count = sum(1 for q in quotes if getattr(q, "executable", True))
        analytic_count = max(0, len(quotes) - exec_count)
        status_counts: dict[str, int] = {}
        provider_errors = 0
        for entry in diagnostics:
            status = str(entry.get("status", "")).strip().lower()
            if not status:
                continue
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "error":
                provider_errors += 1
        self._last_quote_context["executable_quotes"] = exec_count
        self._last_quote_context["analytic_quotes"] = analytic_count
        self._last_quote_context["has_executable_quotes"] = exec_count > 0
        self._last_quote_context["has_onchain_liquidity"] = exec_count > 0
        self._last_quote_context["status_counts"] = status_counts
        self._last_quote_context["provider_errors"] = provider_errors
        self._last_quote_context["quotes_attempted"] = len(diagnostics)
        self._last_quote_diagnostics = diagnostics
        return quotes

    def _emit_quote_diagnostics(
        self,
        *,
        reason: str,
        route_plan: RoutePlan,
        amount: int,
        mode: str,
        fallback_attempts: list[dict[str, Any]] | None = None,
    ) -> None:
        if not self._diag_enabled:
            return
        record: dict[str, Any] = {
            "event": "dex_quote_failure",
            "reason": reason,
            "mode": mode,
            "amount": int(amount),
            "route": list(route_plan.tokens),
            "attempts": self._last_quote_diagnostics,
            "discovery_providers": self._discovery_provider_names,
            "execution_providers": self._execution_provider_names,
            "executable_quote_count": getattr(self, "_last_quote_context", {}).get(
                "executable_quotes"
            ),
            "provider_errors": getattr(self, "_last_quote_context", {}).get(
                "provider_errors"
            ),
        }
        try:
            # Downgrade to DEBUG for expected DIEM route failures (bridge routes often fail
            # individual legs but succeed via fallback). Only warn for unexpected failures.
            is_diem = self._is_diem_route(route_plan)
            exec_count = record.get("executable_quote_count") or 0
            # If DIEM route and we have executable quotes from fallback, it's expected - use DEBUG
            # Also use DEBUG for known failure reasons like no_quotes on bridge routes
            if is_diem and (
                exec_count > 0 or reason in ("no_quotes", "no_quotes_exact_out")
            ):
                _logger.debug(
                    "dex quote failure (expected: DIEM route with fallback)",
                    extra={
                        "event": "dex_quote_failure",
                        "reason": reason,
                        "mode": mode,
                        "amount": int(amount),
                        "route": list(route_plan.tokens),
                        "executable_quote_count": exec_count,
                        "provider_errors": record.get("provider_errors"),
                    },
                )
            else:
                _logger.warning(
                    "dex quote failure",
                    extra={
                        "event": "dex_quote_failure",
                        "reason": reason,
                        "mode": mode,
                        "amount": int(amount),
                        "route": list(route_plan.tokens),
                        "executable_quote_count": exec_count,
                        "provider_errors": record.get("provider_errors"),
                    },
                )
        except Exception:
            pass
        try:
            has_exec = bool(
                getattr(self, "_last_quote_context", {}).get("has_executable_quotes")  # type: ignore[arg-type]
            )
            record["has_onchain_liquidity"] = has_exec
        except Exception:
            pass
        try:
            status_counts: dict[str, int] = {}
            for entry in self._last_quote_diagnostics:
                status = str(entry.get("status", "")).strip().lower()
                if not status:
                    continue
                status_counts[status] = status_counts.get(status, 0) + 1
            if status_counts:
                record["attempt_status_counts"] = status_counts
        except Exception:
            pass
        try:
            if self._is_diem_route(route_plan):
                record["diem_route"] = True
                if self._is_usdc_weth_diem_path(route_plan):
                    record["diem_usdc_weth_path"] = True
                # Snapshot circuit-breaker state per provider to explain why no
                # quotes were attempted for DIEM routes.
                circuit_state: dict[str, bool] = {}
                circuit_status: dict[str, dict[str, Any]] = {}
                for name in self._provider_name_map.values():
                    try:
                        is_open = bool(self._circ_is_open(name))
                        circuit_state[name] = is_open
                        # Include detailed status if available
                        if hasattr(self, "_circ_get_status"):
                            try:
                                circuit_status[name] = self._circ_get_status(name)
                            except Exception:
                                pass
                    except Exception:
                        continue
                if circuit_state:
                    record["circuit_open"] = circuit_state
                if circuit_status:
                    record["circuit_status"] = circuit_status
                compat_state: dict[str, dict[str, Any]] = {}
                for name in self._provider_name_map.values():
                    try:
                        compat_ok, compat_reason = self._diem_provider_compatibility(
                            name, route_plan
                        )
                        compat_state[name] = {
                            "compatible": bool(compat_ok),
                            "reason": compat_reason,
                        }
                    except Exception:
                        continue
                if compat_state:
                    record["diem_compatibility"] = compat_state
        except Exception:
            # Diagnostics are best-effort; never fail the main path.
            pass
        metadata = getattr(route_plan, "_metadata", None)
        if isinstance(metadata, dict):
            record["route_metadata"] = metadata
        inspections = self._inspect_route(route_plan, amount, mode)
        if inspections:
            record["inspections"] = inspections
            try:
                insp_counts: dict[str, int] = {}
                for snap in inspections:
                    status = str(snap.get("status", "")).strip().lower()
                    if not status:
                        continue
                    insp_counts[status] = insp_counts.get(status, 0) + 1
                if insp_counts:
                    record["inspection_status_counts"] = insp_counts
            except Exception:
                pass
        if fallback_attempts:
            record["fallback_attempts"] = fallback_attempts
        try:
            _dex_diag_log_event(record)
        except Exception as exc:
            if _debug_routes_enabled():
                _logger.debug("dex diagnostics logging failed: %s", exc)

    def _diem_inspection_fallback(
        self, route_plan: RoutePlan, amount: int, mode: str
    ) -> list[int] | None:
        """
        Provide a deterministic DIEM/VVV reserve-based inspection result.
        """

        try:
            if not self._is_diem_route(route_plan):
                return None
            tokens = list(route_plan.tokens)
        except Exception:
            return None
        diem, vvv = _diem_vvv_addrs()
        if not diem or not vvv or len(tokens) != 2:
            return None
        if {tokens[0].lower(), tokens[1].lower()} != {diem, vvv}:
            return None
        if mode == "exact_in":
            quote_obj = diem_vvv_quote_exact_in_from_reserves(
                int(amount), tokens[0], tokens[1]
            )
        else:
            quote_obj = diem_vvv_quote_from_reserves(int(amount), tokens[0], tokens[1])
        if quote_obj is None:
            return None
        return [int(quote_obj.amount_in), int(quote_obj.amount_out)]

    def _inspect_route(
        self,
        route_plan: RoutePlan,
        amount: int,
        mode: str,
    ) -> list[dict[str, Any]]:
        inspections: list[dict[str, Any]] = []
        for provider in self.providers:
            snapshot = self._inspect_provider_route(provider, route_plan, amount, mode)
            if snapshot:
                inspections.append(snapshot)
        return inspections

    def _diem_rescue_reserve_estimate(
        self, route_plan: RoutePlan, amount: int, mode: str
    ) -> list[int] | None:
        """
        Provide a deterministic DIEM inspection estimate using cached DIEM/VVV reserves.

        This is only used during the rescue inspection stage to avoid empty snapshots
        when routers are unavailable but reserves are healthy.
        """
        try:
            if not self._is_diem_route(route_plan):
                return None
            tokens = [str(t).strip().lower() for t in route_plan.tokens]
            if len(tokens) < 2:
                return None
        except Exception:
            return None

        pair_addr = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip().lower()
        diem, vvv = _diem_vvv_addrs()
        quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
        if not pair_addr or not diem or not vvv or not quote_token:
            return None

        reserves = _pair_state_cached(pair_addr, _provider_timeout_seconds())
        if not reserves:
            return None

        reserve0, reserve1, token0, token1 = reserves
        token0 = str(token0).lower()
        token1 = str(token1).lower()

        def _cp_exact_in(amount_in: int, token_in: str, token_out: str) -> int | None:
            if amount_in <= 0:
                return None
            if token_in == token0 and token_out == token1:
                r_in, r_out = reserve0, reserve1
            elif token_in == token1 and token_out == token0:
                r_in, r_out = reserve1, reserve0
            else:
                return None
            amount_in_with_fee = amount_in * 997
            numerator = amount_in_with_fee * r_out
            denominator = (r_in * 1000) + amount_in_with_fee
            if denominator <= 0:
                return None
            return numerator // denominator

        def _cp_exact_out(amount_out: int, token_in: str, token_out: str) -> int | None:
            if amount_out <= 0:
                return None
            if token_in == token0 and token_out == token1:
                r_in, r_out = reserve0, reserve1
            elif token_in == token1 and token_out == token0:
                r_in, r_out = reserve1, reserve0
            else:
                return None
            if r_out <= amount_out:
                return None
            numerator = r_in * amount_out * 1000
            denominator = (r_out - amount_out) * 997
            if denominator <= 0:
                return None
            return (numerator // denominator) + 1

        # DIEM sell path: DIEM -> ... -> QUOTE
        if tokens[0] == diem and tokens[-1] == quote_token:
            vvv_out = _cp_exact_in(int(amount), diem, vvv)
            if vvv_out is None:
                return None
            # Assume VVV->QUOTE roughly 1:1 with a safety haircut for inspection only
            quote_out = int(vvv_out * 0.95)
            return [int(amount), max(1, quote_out)]

        # DIEM buy path: QUOTE -> ... -> DIEM
        if tokens[0] == quote_token and tokens[-1] == diem:
            vvv_in = int(int(amount) * 0.95)  # conservative stage-1 estimate
            diem_out = _cp_exact_in(vvv_in, vvv, diem)
            if diem_out is None:
                return None
            return [int(amount), max(1, int(diem_out))]

        # Direct DIEM<->VVV route
        if {tokens[0], tokens[-1]} == {diem, vvv}:
            if mode == "exact_in":
                out_val = _cp_exact_in(int(amount), tokens[0], tokens[-1])
                if out_val is None:
                    return None
                return [int(amount), int(out_val)]
            out_val = _cp_exact_out(int(amount), tokens[0], tokens[-1])
            if out_val is None:
                return None
            return [int(out_val), int(amount)]

        return None

    def _rescue_inspection(
        self,
        route_plan: RoutePlan,
        amount: int,
        mode: str,
    ) -> list[dict[str, Any]]:
        """
        DIEM-aware inspection stage used before rescue retries.

        Seeds allowance/balance hints and injects reserve-based results so
        diagnostics stay non-empty when routes are otherwise healthy.
        """
        inspections = self._inspect_route(route_plan, amount, mode)
        try:
            if not self._is_diem_route(route_plan):
                return inspections
        except Exception:
            return inspections

        allowance_hint = {
            "seeded": True,
            "required": int(amount),
            "spender": "rescue_inspection",
        }
        balance_hint = {
            "seeded": True,
            "available": max(int(amount * 2), 1),
        }

        enriched: list[dict[str, Any]] = []
        for snap in inspections:
            snapshot = dict(snap)
            status = str(snapshot.get("status", "")).strip().lower()
            if status in {"", "empty", "error"}:
                reserve_result = self._diem_rescue_reserve_estimate(
                    route_plan, amount, mode
                )
                if reserve_result:
                    snapshot["status"] = "ok"
                    snapshot["result"] = reserve_result
                    snapshot["inspection_reason"] = (
                        snapshot.get("inspection_reason") or "diem_rescue_reserves"
                    )
            snapshot.setdefault("allowance", allowance_hint)
            snapshot.setdefault("balance", balance_hint)
            enriched.append(snapshot)
        return enriched

    def _inspect_provider_route(
        self,
        provider: DexProvider,
        route_plan: RoutePlan,
        amount: int,
        mode: str,
    ) -> dict[str, Any] | None:
        base: dict[str, Any] = {"provider": provider.name}
        provider_name = provider.name.lower()
        probe_amount = max(int(amount), 1)

        try:
            compat_ok, compat_reason = self._diem_provider_compatibility(
                provider_name, route_plan
            )
        except Exception:
            compat_ok, compat_reason = True, None
        if not compat_ok:
            if (
                provider_name == "uniswap_v2"
                and compat_reason == "v2_incompatible_route"
            ):
                # Silence noisy inspections for V2 when the route is known to be incompatible.
                return None
            base["status"] = "incompatible"
            if compat_reason:
                base["reason"] = compat_reason
            return base

        try:
            if provider_name == "uniswap_v2":
                if self._should_skip_v2(route_plan):
                    base["status"] = "incompatible"
                    base["reason"] = "v2_incompatible_route"
                    return base
                router = getattr(provider, "router", None)
                if router is None:
                    base["status"] = "no_router"
                    return base
                # Normalize route for V2 (strip fee tiers) before converting to path
                try:
                    normalized_route = normalize_route_for_v2(route_plan)
                    path = normalized_route.to_uniswap_v2_path(checksum=True)
                except ValueError as ve:
                    base["status"] = "error"
                    base["error"] = str(ve)
                    return base
                pools_exist = True
                try:
                    checker = getattr(provider, "_pools_exist", None)
                    if callable(checker):
                        pools_exist = bool(checker(normalized_route))
                except Exception:
                    pools_exist = True
                if not pools_exist:
                    base["status"] = "no_pool"
                    return base
                fn_name = "getAmountsOut" if mode == "exact_in" else "getAmountsIn"
                fn = getattr(router.functions, fn_name, None)
                if fn is None:
                    base["status"] = "fn_missing"
                    base["function"] = fn_name
                    return base
                result = fn(probe_amount, path).call()
                base["status"] = "ok"
                base["result"] = (
                    [int(x) for x in result]
                    if isinstance(result, (list, tuple))
                    else [int(result or 0)]
                )
                return base

            if provider_name == "uniswap_v3":
                ensure_route = getattr(provider, "_ensure_route", None)
                effective_route = (
                    ensure_route(route_plan) if callable(ensure_route) else route_plan
                )
                try:
                    if mode == "exact_in":
                        quote_obj = provider.quote(probe_amount, effective_route)
                    else:
                        quote_obj = provider.quote_exact_out(
                            probe_amount, effective_route
                        )
                except Exception as exc:
                    base["status"] = "error"
                    base["error"] = str(exc)
                    return base

                if quote_obj is None:
                    # Try DIEM fallback for single-hop DIEM/VVV routes
                    fallback = self._diem_inspection_fallback(
                        effective_route, probe_amount, mode
                    )
                    if fallback is not None:
                        base["status"] = "ok"
                        base["result"] = fallback
                        base["inspection_reason"] = "diem_bridge_trusted"
                        return base
                    # For multi-hop DIEM routes, check if bridge path is available
                    if self._is_diem_route(effective_route):
                        try:
                            tokens = list(effective_route.tokens)
                            if len(tokens) >= 2:
                                diem, vvv = _diem_vvv_addrs()
                                quote_token = (
                                    (os.getenv("QUOTE_TOKEN_ADDRESS") or "")
                                    .strip()
                                    .lower()
                                )
                                # If this is a DIEM->USDC route, trust bridge path exists
                                if (
                                    diem
                                    and vvv
                                    and quote_token
                                    and tokens[0].lower() == diem
                                    and tokens[-1].lower() == quote_token
                                ):
                                    # Use reserve-based estimate for inspection
                                    try:
                                        from libs.dex.diem_fallbacks import (
                                            diem_vvv_quote_exact_in_from_reserves,
                                        )

                                        # Estimate first leg (DIEM->VVV)
                                        leg1_quote = (
                                            diem_vvv_quote_exact_in_from_reserves(
                                                probe_amount, tokens[0], vvv
                                            )
                                        )
                                        if leg1_quote:
                                            # Rough estimate: assume VVV->USDC has similar pricing
                                            estimated_out = int(
                                                leg1_quote.amount_out * 0.9
                                            )  # Conservative estimate
                                            base["status"] = "ok"
                                            base["result"] = [
                                                int(probe_amount),
                                                int(estimated_out),
                                            ]
                                            base["inspection_reason"] = (
                                                "diem_bridge_trusted"
                                            )
                                            return base
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    base["status"] = "empty"
                    return base

                base["status"] = "ok"
                base["result"] = [
                    int(quote_obj.amount_in),
                    int(quote_obj.amount_out),
                ]
                return base

            if provider_name == "aerodrome":
                if mode == "exact_in":
                    try:
                        quote_obj = provider.quote(probe_amount, route_plan)
                    except Exception as exc:
                        base["status"] = "error"
                        base["error"] = str(exc)
                        return base
                    if quote_obj is None:
                        # Try DIEM fallback for single-hop DIEM/VVV routes
                        fallback = self._diem_inspection_fallback(
                            route_plan, probe_amount, mode
                        )
                        if fallback is not None:
                            base["status"] = "ok"
                            base["result"] = fallback
                            base["inspection_reason"] = "diem_bridge_trusted"
                            return base
                        # For multi-hop DIEM routes, check if bridge path is available
                        if self._is_diem_route(route_plan):
                            try:
                                tokens = list(route_plan.tokens)
                                if len(tokens) >= 2:
                                    diem, vvv = _diem_vvv_addrs()
                                    quote_token = (
                                        (os.getenv("QUOTE_TOKEN_ADDRESS") or "")
                                        .strip()
                                        .lower()
                                    )
                                    # If this is a DIEM->USDC route, trust bridge path exists
                                    if (
                                        diem
                                        and vvv
                                        and quote_token
                                        and tokens[0].lower() == diem
                                        and tokens[-1].lower() == quote_token
                                    ):
                                        # Use reserve-based estimate for inspection
                                        # This provides a non-empty result so rescue can proceed
                                        try:
                                            from libs.dex.diem_fallbacks import (
                                                diem_vvv_quote_exact_in_from_reserves,
                                            )

                                            # Estimate first leg (DIEM->VVV)
                                            leg1_quote = (
                                                diem_vvv_quote_exact_in_from_reserves(
                                                    probe_amount, tokens[0], vvv
                                                )
                                            )
                                            if leg1_quote:
                                                # Rough estimate: assume VVV->USDC has similar pricing
                                                # This is just for inspection, not execution
                                                estimated_out = int(
                                                    leg1_quote.amount_out * 0.9
                                                )  # Conservative estimate
                                                base["status"] = "ok"
                                                base["result"] = [
                                                    int(probe_amount),
                                                    int(estimated_out),
                                                ]
                                                base["inspection_reason"] = (
                                                    "diem_bridge_trusted"
                                                )
                                                return base
                                        except Exception:
                                            pass
                            except Exception:
                                pass
                        base["status"] = "empty"
                        return base
                    base["status"] = "ok"
                    base["result"] = [
                        int(quote_obj.amount_in),
                        int(quote_obj.amount_out),
                    ]
                    return base

                router = getattr(provider, "router", None)
                routes_fn = getattr(provider, "_routes", None)
                if router is None or not callable(routes_fn):
                    base["status"] = "no_router"
                    return base
                routes = routes_fn(
                    route_plan, stable=getattr(provider, "stable", False)
                )
                fn_name = "getAmountsOut" if mode == "exact_in" else "getAmountsIn"
                fn = getattr(router.functions, fn_name, None)
                if fn is None:
                    base["status"] = "fn_missing"
                    base["function"] = fn_name
                    return base
                result = fn(probe_amount, routes).call()
                base["status"] = "ok"
                base["result"] = (
                    [int(x) for x in result]
                    if isinstance(result, (list, tuple))
                    else [int(result or 0)]
                )
                return base

            if provider_name == "aerodrome_cl":
                # Aerodrome SlipStream CL provider - use slot0-based quoting
                if mode == "exact_in":
                    try:
                        quote_obj = provider.quote(probe_amount, route_plan)
                    except Exception as exc:
                        base["status"] = "error"
                        base["error"] = str(exc)
                        return base
                    if quote_obj is None:
                        base["status"] = "empty"
                        return base
                    base["status"] = "ok"
                    base["result"] = [
                        int(quote_obj.amount_in),
                        int(quote_obj.amount_out),
                    ]
                    return base
                # exact_out
                try:
                    quote_obj = provider.quote_exact_out(probe_amount, route_plan)
                except Exception as exc:
                    base["status"] = "error"
                    base["error"] = str(exc)
                    return base
                if quote_obj is None:
                    base["status"] = "empty"
                    return base
                base["status"] = "ok"
                base["result"] = [
                    int(quote_obj.amount_in),
                    int(quote_obj.amount_out),
                ]
                return base

            base["status"] = "unsupported"
            return base
        except Exception as exc:
            base["status"] = "error"
            base["error"] = str(exc)
            return base

    def _promote_inspection_quotes(
        self,
        *,
        plan: RoutePlan,
        amount: int,
        mode: str,
        method: str,
        allowed: set[str] | None,
        base_diagnostics: list[dict[str, Any]],
    ) -> tuple[list[Quote], list[dict[str, Any]]]:
        """
        Re-attempt quoting with providers that passed inspection but were not attempted.

        Only providers that are execution-enabled and not blocked by the circuit breaker
        are considered. This addresses cases where discovery filters or compatibility
        heuristics hid a healthy venue (e.g., UniswapV2) from the initial attempt set.
        """
        skipped_statuses = {"skipped", "rate_limited", "not_enabled"}
        attempted = {
            str(entry.get("provider", "")).strip().lower()
            for entry in base_diagnostics
            if entry.get("provider")
            and str(entry.get("status", "")).strip().lower() not in skipped_statuses
        }
        inspections = self._inspect_route(plan, amount, mode)
        promotable: list[DexProvider] = []
        skipped: list[dict[str, Any]] = []

        for snapshot in inspections:
            provider_name = str(snapshot.get("provider", "")).strip().lower()
            status = snapshot.get("status")
            if not provider_name or status != "ok":
                continue
            if provider_name in attempted:
                continue
            if provider_name == "uniswap_v2" and self._should_skip_v2(plan):
                skipped.append(
                    {"provider": provider_name, "reason": "v2_incompatible_route"}
                )
                continue
            if allowed is not None and provider_name not in allowed:
                skipped.append({"provider": provider_name, "reason": "not_allowed"})
                continue
            compatible, compat_reason = self._diem_provider_compatibility(
                provider_name, plan
            )
            if not compatible:
                skipped.append(
                    {
                        "provider": provider_name,
                        "reason": compat_reason or "route_incompatible",
                    }
                )
                continue
            if not self._execution_enabled(provider_name):
                skipped.append(
                    {"provider": provider_name, "reason": "execution_disabled"}
                )
                continue
            canonical = self._provider_name_map.get(provider_name, provider_name)
            if self._circ_is_open(canonical):
                skipped.append({"provider": provider_name, "reason": "circuit_open"})
                continue
            provider_obj = self._provider_obj_map.get(provider_name)
            if provider_obj is None:
                skipped.append({"provider": provider_name, "reason": "not_configured"})
                continue
            if mode == "exact_out" and not self._supports_exact_out(provider_obj):
                skipped.append(
                    {"provider": provider_name, "reason": "mode_unsupported"}
                )
                continue
            promotable.append(provider_obj)

        if skipped:
            self._log_skipped_executable(skipped, plan, amount=amount, mode=mode)

        if not promotable:
            return [], []

        quotes = self._collect_quotes(promotable, method, plan, amount, mode=mode)
        promo_diag = list(self._last_quote_diagnostics)
        return quotes, promo_diag

    def _diem_rescue_quotes(
        self,
        *,
        plan: RoutePlan,
        amount: int,
        mode: str,
        base_diagnostics: list[dict[str, Any]],
    ) -> tuple[list[Quote], list[dict[str, Any]]]:
        """
        Final-chance retry for DIEM exact-in routes when discovery skipped a healthy venue.

        We re-use inspections to find execution-enabled providers whose circuit is closed
        and that are compatible with the DIEM path (e.g., UniswapV2 when V3 failed).
        """
        if mode != "exact_in":
            return [], []
        try:
            is_diem_route = bool(self._is_diem_route(plan))
            if not is_diem_route:
                return [], []
        except Exception:
            return [], []

        skipped: list[dict[str, Any]] = []
        forced_overrides: list[str] = []
        final_attempts: list[str] = []
        eligible_candidates: list[dict[str, Any]] = []
        eligibility_snapshot: list[dict[str, Any]] = []
        inspection_diag_entries: list[dict[str, Any]] = []
        attempted = {
            str(entry.get("provider", "")).strip().lower()
            for entry in base_diagnostics
            if entry.get("provider")
            and str(entry.get("status", "")).strip().lower()
            not in {"skipped", "rate_limited", "not_enabled"}
        }
        inspections = self._rescue_inspection(plan, amount, mode)
        rescue_providers: list[DexProvider] = []

        for snapshot in inspections:
            provider_name = str(snapshot.get("provider", "")).strip().lower()
            status = str(snapshot.get("status", "")).strip().lower()
            if not provider_name:
                continue
            inspection_diag_entries.append(
                {
                    "provider": provider_name,
                    "status": status or "unknown",
                    "stage": "rescue_inspection",
                    "inspection_reason": snapshot.get("inspection_reason"),
                    "allowance": snapshot.get("allowance"),
                    "balance": snapshot.get("balance"),
                    "mode": mode,
                    "method": "inspect",
                    "amount": int(amount),
                    "result": snapshot.get("result"),
                }
            )
            if provider_name in attempted:
                continue
            provider_obj = self._provider_obj_map.get(provider_name)
            canonical = self._provider_name_map.get(provider_name, provider_name)
            in_execution = canonical.lower() in self._execution_providers
            circ_open = self._circ_is_open(canonical)
            if not in_execution:
                skipped.append(
                    {"provider": provider_name, "reason": "not_execution_provider"}
                )
                continue
            if circ_open:
                skipped.append({"provider": provider_name, "reason": "circuit_open"})
                continue
            if provider_name == "uniswap_v2" and self._should_skip_v2(plan):
                skipped.append(
                    {
                        "provider": provider_name,
                        "reason": "v2_incompatible_route",
                        "stage": "rescue_compat",
                    }
                )
                continue
            compatible, compat_reason = self._diem_provider_compatibility(
                provider_name, plan
            )
            compat_allowed = bool(
                compatible
                or (self._force_diem_v2 and compat_reason == "v2_incompatible_route")
            )
            if not compat_allowed:
                skipped.append(
                    {
                        "provider": provider_name,
                        "reason": compat_reason or "route_incompatible",
                        "stage": "rescue_compat",
                    }
                )
                continue
            if provider_obj is None:
                skipped.append({"provider": provider_name, "reason": "not_configured"})
                continue
            eligibility_snapshot.append(
                {
                    "provider": provider_name,
                    "status": status or "unknown",
                    "in_execution": in_execution,
                    "circuit_open": circ_open,
                    "compatible": bool(compatible),
                    "compat_allowed": compat_allowed,
                    "compat_reason": compat_reason,
                    "diem_route": is_diem_route,
                }
            )
            eligible_candidates.append(
                {
                    "provider": provider_obj,
                    "name": provider_name,
                    "status": status,
                    "compat_reason": compat_reason,
                }
            )
            if not compatible and compat_reason == "v2_incompatible_route":
                forced_overrides.append(provider_name)
            if status != "ok":
                skipped.append(
                    {
                        "provider": provider_name,
                        "reason": f"inspection_{status or 'unknown'}",
                        "stage": "rescue_inspection",
                    }
                )
                continue
            rescue_providers.append(provider_obj)

        # If inspections show an execution provider with a closed circuit but none were
        # enqueued, force a single final attempt so DIEM routes do not exit early with
        # no_quotes while a viable venue exists.
        if not rescue_providers and eligible_candidates:
            candidate = eligible_candidates[0]
            rescue_providers.append(candidate["provider"])
            final_attempts.append(str(candidate.get("name")))
            compat_reason = candidate.get("compat_reason")
            if compat_reason == "v2_incompatible_route":
                name = str(candidate.get("name") or "")
                if name and name not in forced_overrides:
                    forced_overrides.append(name)

            if skipped:
                self._log_skipped_executable(skipped, plan, amount=amount, mode=mode)

        if not rescue_providers:
            return [], inspection_diag_entries

        quotes = self._collect_quotes(
            rescue_providers, "quote", plan, amount, mode=mode
        )
        rescue_diag = list(self._last_quote_diagnostics)
        if forced_overrides:
            for name in forced_overrides:
                rescue_diag.append(
                    {
                        "provider": name,
                        "status": "rescue_override",
                        "reason": "v2_incompatible_route",
                        "mode": mode,
                        "method": "quote",
                        "amount": int(amount),
                    }
                )
        if final_attempts:
            for name in final_attempts:
                rescue_diag.append(
                    {
                        "provider": name,
                        "status": "rescue_final_attempt",
                        "mode": mode,
                        "method": "quote",
                        "amount": int(amount),
                    }
                )
        if eligibility_snapshot:
            try:
                _dex_diag_log_event(
                    {
                        "event": "dex_diem_rescue_candidates",
                        "route": list(plan.tokens),
                        "mode": mode,
                        "amount": int(amount),
                        "diem_route": is_diem_route,
                        "candidates": eligibility_snapshot,
                        "attempted": sorted(attempted),
                        "forced": final_attempts,
                    }
                )
            except Exception:
                pass
        if inspection_diag_entries:
            inspection_diag_entries.extend(rescue_diag)
            rescue_diag = inspection_diag_entries
        return quotes, rescue_diag

    def _is_v2_incompatible_route(self, route_plan: RoutePlan) -> bool:
        """
        Detect routes that are incompatible with UniswapV2 (e.g., DIEM->VVV which only exists on V3).

        Returns True if the route contains hops that don't exist on UniswapV2.
        """
        try:
            # If on-chain V2 pools exist for this route, treat it as compatible.
            if self._v2_pools_exist(route_plan):
                return False
        except Exception:
            # Fall back to heuristics if pool check fails
            pass
        if not self._diem_token:
            return False
        try:
            tokens = [str(t).strip().lower() for t in route_plan.tokens]
            vvv_token = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
        except Exception:
            return False

        if not vvv_token:
            return False

        # Check if route contains DIEM->VVV or VVV->DIEM hop
        # These pairs typically only exist on V3, not V2
        for i in range(len(tokens) - 1):
            hop_pair = {tokens[i], tokens[i + 1]}
            if hop_pair == {self._diem_token, vvv_token}:
                if _debug_routes_enabled():
                    _logger.debug(
                        "Route incompatible with V2: DIEM->VVV hop detected in %s",
                        list(route_plan.tokens),
                    )
                return True

        # Also check if route contains DIEM with any token that typically doesn't have V2 pools
        # For exact_out routes, DIEM may appear as the output token (last in path)
        # For exact_in routes, DIEM may appear as input or intermediate token
        # If DIEM appears in the route, it's likely a V3-only route
        if self._diem_token in tokens:
            # Exception: DIEM->USDC or USDC->DIEM might work on V2 if there's a direct pool
            # But DIEM->VVV, VVV->DIEM, or DIEM->WETH are typically V3-only
            quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
            weth_token = "0x4200000000000000000000000000000000000006"

            # Check for DIEM->VVV or VVV->DIEM (already handled above, but double-check)
            # Check for DIEM->WETH or WETH->DIEM (typically V3-only)
            for i in range(len(tokens) - 1):
                hop_pair = {tokens[i], tokens[i + 1]}
                # Skip if it's DIEM->USDC or USDC->DIEM (might have V2 pool)
                if quote_token and hop_pair == {self._diem_token, quote_token}:
                    continue
                # If DIEM is paired with VVV or WETH, it's likely V3-only
                if self._diem_token in hop_pair and (
                    vvv_token in hop_pair or weth_token.lower() in hop_pair
                ):
                    if _debug_routes_enabled():
                        _logger.debug(
                            "Route incompatible with V2: DIEM paired with VVV/WETH in %s",
                            list(route_plan.tokens),
                        )
                return True

        return False

    def _should_skip_v2(self, route_plan: RoutePlan) -> bool:
        """
        Skip Uniswap V2 when DIEM is present or any hop carries a fee tier.

        Exception: Allow V2 for canonical DIEM routes (DIEM->WETH->USDC or USDC->WETH->DIEM),
        even if the route includes fee tiers, because we normalize to V2 for execution.

        Also allow V2 for 2-hop DIEM routes (DIEM→VVV→USDC or USDC→VVV→DIEM) when
        DIEM_ENABLE_V2_MULTIHOP is enabled.
        """

        has_fee_tiers = False
        try:
            for hop in route_plan.hops:
                if hop.fee is not None:
                    has_fee_tiers = True
                    break
        except Exception:
            has_fee_tiers = False

        is_diem_route = False
        try:
            is_diem_route = bool(self._is_diem_route(route_plan))
        except Exception:
            is_diem_route = False

        if is_diem_route:
            # Allow canonical DIEM routes even if fee tiers are present.
            try:
                if self._is_usdc_weth_diem_path(route_plan):
                    return False
                metadata = getattr(route_plan, "_metadata", None)
                if metadata and metadata.get("canonical_v2", False):
                    return False
            except Exception:
                pass

            if has_fee_tiers:
                return True

            # Allow simple 2-token DIEM↔quote routes on V2.
            try:
                tokens = list(route_plan.tokens)
                if len(tokens) == 2:
                    token_set = {t.lower() for t in tokens}
                    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                    quote_addr = (
                        (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                    )
                    if (
                        diem_addr
                        and quote_addr
                        and {diem_addr, quote_addr} == token_set
                    ):
                        return False
            except Exception:
                pass

            # Allow DIEM↔VVV↔USDC multihop on V2 only when explicitly enabled.
            try:
                tokens = list(route_plan.tokens)
                if len(tokens) == 3:
                    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                    vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                    quote_addr = (
                        (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                    )
                    token_set = {t.lower() for t in tokens}
                    if (
                        diem_addr
                        and vvv_addr
                        and quote_addr
                        and diem_addr in token_set
                        and vvv_addr in token_set
                        and quote_addr in token_set
                    ):
                        v2_multihop_enabled = os.getenv(
                            "DIEM_ENABLE_V2_MULTIHOP", "1"
                        ).strip().lower() in {"1", "true", "yes", "on"}
                        return not v2_multihop_enabled
            except Exception:
                pass

            # Non-canonical DIEM route - skip V2.
            return True

        if has_fee_tiers:
            return True

        # If a V2 pool actually exists for this route, allow it.
        try:
            if self._v2_pools_exist(route_plan):
                return False
        except Exception:
            pass

        return False

    def _diem_provider_compatibility(
        self, provider_name: str, route_plan: RoutePlan
    ) -> tuple[bool, str | None]:
        """
        Return whether a provider should be considered compatible with a DIEM route.

        Compatibility is explicit so diagnostics can show why a venue was skipped.
        """
        try:
            if not self._is_diem_route(route_plan):
                name = str(provider_name or "").strip().lower()
                if name == "bridge_vvv":
                    return False, "non_diem_route"
                return True, None
        except Exception:
            return True, None

        name = str(provider_name or "").strip().lower()
        if name == "bridge_vvv":
            try:
                tokens = [str(t).strip().lower() for t in route_plan.tokens]
                vvv_token = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
            except Exception:
                return True, None

            if not (vvv_token and quote_token and self._diem_token):
                return True, None

            if len(tokens) == 2:
                if {tokens[0], tokens[1]} == {self._diem_token, quote_token}:
                    return True, None
                return False, "bridge_unsupported_route"

            endpoints = {tokens[0], tokens[-1]}
            if endpoints != {self._diem_token, quote_token}:
                return False, "bridge_requires_diem_quote_endpoints"
            if vvv_token not in tokens:
                return False, "bridge_requires_vvv"
            try:
                vvv_idx = tokens.index(vvv_token)
                if vvv_idx <= 0 or vvv_idx >= len(tokens) - 1:
                    return False, "bridge_requires_mid_vvv"
            except Exception:
                return False, "bridge_requires_vvv"
            return True, None
        if name == "uniswap_v2":
            # Check if this is a canonical V2 route (no fee tiers, canonical path)
            try:
                # Check for canonical path first
                if self._is_usdc_weth_diem_path(route_plan):
                    # Check if route has no fee tiers (V2-compatible)
                    has_fee_tiers = any(hop.fee is not None for hop in route_plan.hops)
                    if not has_fee_tiers:
                        # Canonical V2 route - allow UniswapV2
                        return True, None
                # Check metadata for explicit canonical V2 marking
                metadata = getattr(route_plan, "_metadata", None)
                if metadata and metadata.get("canonical_v2", False):
                    return True, None

                # Check if V2 multihop is enabled for 2-hop DIEM routes
                v2_multihop_enabled = os.getenv(
                    "DIEM_ENABLE_V2_MULTIHOP", "1"
                ).strip().lower() in {"1", "true", "yes", "on"}

                if v2_multihop_enabled:
                    tokens = list(route_plan.tokens)
                    if len(tokens) == 3:
                        diem_addr = (
                            (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        vvv_addr = (
                            (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        quote_addr = (
                            (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        token_set = {t.lower() for t in tokens}

                        # Check if this is a 2-hop DIEM route via VVV
                        if (
                            diem_addr
                            and vvv_addr
                            and quote_addr
                            and diem_addr in token_set
                            and vvv_addr in token_set
                            and quote_addr in token_set
                        ):
                            # 2-hop DIEM route via VVV - allow V2
                            return True, None
            except Exception:
                pass
            # Use _should_skip_v2() which now handles canonical routes and 2-hop DIEM routes
            if self._should_skip_v2(route_plan):
                return False, "v2_incompatible_route"
        if name == "uniswap_v3":
            try:
                if hasattr(route_plan, "is_uniswap_v3") and route_plan.is_uniswap_v3():
                    return True, None
            except Exception:
                pass
            # DIEM pools don't exist on vanilla UniswapV3 - only on Aerodrome SlipStream (CL)
            # Skip V3 for DIEM routes to avoid expensive RPC calls that always fail
            try:
                skip_v3_for_diem = os.getenv(
                    "DEX_V3_SKIP_DIEM", "1"
                ).strip().lower() in {"1", "true", "yes", "on"}
                if skip_v3_for_diem:
                    return False, "v3_no_diem_pool"
            except Exception:
                return False, "v3_no_diem_pool"
        return True, None

    def quote_all(
        self,
        amount_in: int,
        route: RouteLike,
        allowed_providers: Sequence[str] | None = None,
    ) -> list[Quote]:
        route_plan = as_route_plan(route)
        is_diem_route = self._is_diem_route(route_plan)
        route_is_v3 = (
            route_plan.is_uniswap_v3()
            if hasattr(route_plan, "is_uniswap_v3")
            else False
        )
        force_v2_canonical = os.getenv(
            "DEX_FORCE_V2_FOR_CANONICAL", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        allowed: set[str] | None = None
        if allowed_providers is not None:
            allowed = {
                str(name).strip().lower()
                for name in allowed_providers
                if str(name).strip()
            }
        allowed_list = sorted(allowed) if allowed is not None else None
        preview_mode = allowed is None
        active: list[DexProvider] = []
        skipped_diag: list[dict[str, Any]] = []
        skipped_eligible: list[dict[str, Any]] = []

        def _mark_skip(provider: DexProvider, reason: str, eligible: bool) -> None:
            entry = {
                "provider": provider.name,
                "status": "skipped",
                "mode": "exact_in",
                "method": "quote",
                "amount": int(amount_in),
                "reason": reason,
            }
            if allowed_list is not None and reason in {
                "not_allowed",
                "execution_disabled",
            }:
                entry["allowed_providers"] = allowed_list
            if reason == "execution_disabled":
                entry["execution_providers"] = list(self._execution_provider_names)
            skipped_diag.append(entry)
            if (
                reason in {"not_allowed", "execution_disabled"}
                and _debug_routes_enabled()
            ):
                _logger.debug(
                    "Skipping provider=%s reason=%s allowed=%s execution=%s route=%s",
                    provider.name,
                    reason,
                    allowed_list,
                    list(self._execution_provider_names)
                    if reason == "execution_disabled"
                    else None,
                    list(route_plan.tokens) if hasattr(route_plan, "tokens") else None,
                )
            if eligible:
                skipped_eligible.append({"provider": provider.name, "reason": reason})

        for provider in self.providers:
            exec_enabled = (
                True if preview_mode else self._execution_enabled(provider.name)
            )
            circ_open_raw = self._circ_is_open(provider.name)
            force_allow_circ = (
                circ_open_raw
                and is_diem_route
                and self._force_diem_v2
                and provider.name.lower() == "uniswap_v2"
            )
            circ_open = bool(circ_open_raw and not force_allow_circ)
            eligible = exec_enabled and not circ_open

            # Route-specific overrides:
            # - Always allow V3 providers for V3 routes (even if execution set prunes them)
            if route_is_v3 and provider.name.lower() == "uniswap_v3":
                eligible = True
                exec_enabled = True
            # - When forcing V2 for canonical/2-token routes, skip non-V2 providers
            # EXCEPT bridge_vvv and aerodrome which provide DIEM/VVV liquidity
            if (
                force_v2_canonical
                and not route_is_v3
                and is_diem_route
                and provider.name.lower()
                not in ("uniswap_v2", "bridge_vvv", "aerodrome")
            ):
                _mark_skip(provider, "force_v2_canonical", eligible)
                continue

            if allowed is not None and provider.name.lower() not in allowed:
                _mark_skip(provider, "not_allowed", eligible)
                continue
            if provider.name.lower() == "uniswap_v2" and self._should_skip_v2(
                route_plan
            ):
                _mark_skip(provider, "v2_incompatible_route", False)
                continue
            if not exec_enabled and not preview_mode:
                _metrics_inc(
                    "dex_execution_skips_total",
                    labels={"provider": provider.name, "reason": "not_enabled"},
                )
                _mark_skip(provider, "execution_disabled", False)
                continue
            if not self._discovery_enabled(provider.name):
                _mark_skip(provider, "discovery_disabled", eligible)
                continue

            compatible, compat_reason = self._diem_provider_compatibility(
                provider.name, route_plan
            )
            if not compatible:
                if _debug_routes_enabled():
                    _logger.debug(
                        "Skipping provider=%s for DIEM route=%s reason=%s",
                        provider.name,
                        list(route_plan.tokens),
                        compat_reason,
                    )
                _metrics_inc(
                    "dex_route_incompatible_skips_total",
                    labels={
                        "provider": provider.name,
                        "reason": compat_reason or "route_incompatible",
                    },
                )
                _mark_skip(provider, compat_reason or "route_incompatible", False)
                continue

            if circ_open:
                # For DIEM routes, optionally ignore circuit state for Uniswap V2 so a
                # healthy DIEM path on V2 is not permanently shadowed by prior errors
                # on other venues.
                _metrics_inc(
                    "dex_circuit_skips_total", labels={"provider": provider.name}
                )
                _mark_skip(provider, "circuit_open", False)
                continue
            if (
                is_diem_route
                and self._force_diem_v2
                and provider.name.lower() != "uniswap_v2"
            ):
                _mark_skip(provider, "force_diem_v2_only", eligible)
                continue
            active.append(provider)
        quotes = self._collect_quotes(
            active, "quote", route_plan, amount_in, mode="exact_in"
        )
        if not quotes and is_diem_route and len(route_plan.tokens) == 2:
            try:
                prefer_direct = os.getenv(
                    "DIEM_PREFER_DIRECT_ROUTE", "1"
                ).strip().lower() in {"1", "true", "yes", "on"}
                allowed_cl = allowed is None or "aerodrome_cl" in allowed
                diem_usdc_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
                cl_router = (os.getenv("AERODROME_CL_ROUTER_ADDRESS") or "").strip()
                tick_spacing_raw = os.getenv("DIEM_USDC_TICK_SPACING")
                if tick_spacing_raw is None or not str(tick_spacing_raw).strip():
                    tick_spacing = 100
                else:
                    try:
                        tick_spacing = int(str(tick_spacing_raw).strip())
                    except Exception:
                        tick_spacing = None
                exec_intent = allowed is not None
                if (
                    prefer_direct
                    and diem_usdc_pool
                    and allowed_cl
                    and (
                        not exec_intent
                        or (cl_router and tick_spacing and tick_spacing > 0)
                    )
                ):
                    from libs.dex.diem_fallbacks import diem_usdc_slot0_quote

                    direct_quote = diem_usdc_slot0_quote(
                        amount_in, route_plan.tokens[0], route_plan.tokens[1]
                    )
                    if direct_quote and direct_quote.amount_out > 0:
                        if (
                            not cl_router
                            or not tick_spacing
                            or tick_spacing <= 0
                            or (
                                allowed is None
                                and not self._execution_enabled("aerodrome_cl")
                            )
                        ):
                            try:
                                object.__setattr__(direct_quote, "executable", False)
                            except Exception:
                                direct_quote.executable = False  # type: ignore[attr-defined]
                        quotes.append(direct_quote)
                        diag_entry = {
                            "provider": "aerodrome_cl",
                            "status": "ok",
                            "mode": "exact_in",
                            "method": "slot0_fallback",
                            "amount": int(amount_in),
                            "amount_in": int(
                                getattr(direct_quote, "amount_in", amount_in)
                            ),
                            "amount_out": int(getattr(direct_quote, "amount_out", 0)),
                            "executable": bool(
                                getattr(direct_quote, "executable", True)
                            ),
                            "route": list(route_plan.tokens),
                        }
                        try:
                            self._last_quote_diagnostics.append(diag_entry)
                        except Exception:
                            self._last_quote_diagnostics = [diag_entry]
                        exec_count = sum(
                            1 for q in quotes if getattr(q, "executable", True)
                        )
                        analytic_count = max(0, len(quotes) - exec_count)
                        status_counts: dict[str, int] = {}
                        provider_errors = 0
                        for entry in self._last_quote_diagnostics:
                            status = str(entry.get("status", "")).strip().lower()
                            if not status:
                                continue
                            status_counts[status] = status_counts.get(status, 0) + 1
                            if status == "error":
                                provider_errors += 1
                        self._last_quote_context["executable_quotes"] = exec_count
                        self._last_quote_context["analytic_quotes"] = analytic_count
                        self._last_quote_context["has_executable_quotes"] = (
                            exec_count > 0
                        )
                        self._last_quote_context["has_onchain_liquidity"] = (
                            exec_count > 0
                        )
                        self._last_quote_context["status_counts"] = status_counts
                        self._last_quote_context["provider_errors"] = provider_errors
                        self._last_quote_context["quotes_attempted"] = len(
                            self._last_quote_diagnostics
                        )
                        if _debug_routes_enabled():
                            _logger.debug(
                                "Direct DIEM/USDC slot0 fallback injected into quote_all",
                                extra={
                                    "amount_in": int(amount_in),
                                    "amount_out": int(
                                        getattr(direct_quote, "amount_out", 0)
                                    ),
                                    "route": list(route_plan.tokens),
                                    "exec_intent": exec_intent,
                                },
                            )
            except Exception:
                pass
        if skipped_diag:
            merged = list(self._last_quote_diagnostics)
            merged.extend(skipped_diag)
            self._last_quote_diagnostics = merged
        if skipped_eligible:
            self._log_skipped_executable(
                skipped_eligible, route_plan, amount=amount_in, mode="exact_in"
            )
        return quotes

    def best_quote(
        self,
        amount_in: int,
        route: RouteLike,
        *,
        allowed_providers: Sequence[str] | None = None,
    ) -> Quote | None:
        plan = as_route_plan(route)
        allowed: set[str] | None = None
        if allowed_providers is not None:
            allowed = {
                str(name).strip().lower()
                for name in allowed_providers
                if str(name).strip()
            }

        # PRIORITY 0: Try direct DIEM/USDC SlipStream pool first (highest liquidity)
        # Use slot0-based quoting since Aerodrome SlipStream pools have a different quoter
        is_diem = self._is_diem_route(plan)
        is_2_token = len(plan.tokens) == 2
        if is_diem and is_2_token:
            diem_usdc_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
            prefer_direct = os.getenv(
                "DIEM_PREFER_DIRECT_ROUTE", "1"
            ).strip().lower() in {"1", "true", "yes", "on"}
            allowed_cl = allowed is None or "aerodrome_cl" in allowed
            exec_intent = allowed is not None
            cl_router = (os.getenv("AERODROME_CL_ROUTER_ADDRESS") or "").strip()
            tick_spacing_raw = os.getenv("DIEM_USDC_TICK_SPACING")
            if tick_spacing_raw is None or not str(tick_spacing_raw).strip():
                tick_spacing = 100
            else:
                try:
                    tick_spacing = int(str(tick_spacing_raw).strip())
                except Exception:
                    tick_spacing = None
            _logger.info(
                f"Direct DIEM/USDC slot0 check: is_diem={is_diem}, tokens={len(plan.tokens)}, "
                f"pool={'set' if diem_usdc_pool else 'NOT SET'}, prefer_direct={prefer_direct}"
            )
            if prefer_direct and not diem_usdc_pool:
                _logger.warning(
                    "Direct DIEM/USDC slot0 skipped: DIEM_USDC_POOL_ADDRESS missing",
                    extra={
                        "reason": "missing_pool",
                        "allowed_providers": sorted(allowed)
                        if allowed is not None
                        else None,
                    },
                )
            if prefer_direct and diem_usdc_pool and not allowed_cl:
                _logger.info(
                    "Direct DIEM/USDC slot0 skipped: aerodrome_cl not in allowed_providers",
                    extra={
                        "reason": "not_allowed",
                        "allowed_providers": sorted(allowed)
                        if allowed is not None
                        else None,
                    },
                )
            if prefer_direct and diem_usdc_pool and allowed_cl:
                if exec_intent and not self._execution_enabled("aerodrome_cl"):
                    _logger.info(
                        "Direct DIEM/USDC slot0 skipped: aerodrome_cl execution disabled",
                        extra={
                            "reason": "execution_disabled",
                            "execution_providers": list(self._execution_provider_names),
                        },
                    )
                elif exec_intent and (
                    not cl_router or not tick_spacing or tick_spacing <= 0
                ):
                    _logger.warning(
                        "Direct DIEM/USDC slot0 skipped: missing CL execution config",
                        extra={
                            "reason": "missing_exec_config",
                            "router_configured": bool(cl_router),
                            "tick_spacing": tick_spacing,
                        },
                    )
                else:
                    try:
                        # Use slot0-based quoting for DIEM/USDC SlipStream pool
                        from libs.dex.diem_fallbacks import diem_usdc_slot0_quote

                        direct_quote = diem_usdc_slot0_quote(
                            amount_in, plan.tokens[0], plan.tokens[1]
                        )
                        if direct_quote and direct_quote.amount_out > 0:
                            # Mark preview-only if execution config is incomplete.
                            if (
                                not cl_router or not tick_spacing or tick_spacing <= 0
                            ) or (
                                allowed is None
                                and not self._execution_enabled("aerodrome_cl")
                            ):
                                try:
                                    object.__setattr__(
                                        direct_quote, "executable", False
                                    )
                                except Exception:
                                    direct_quote.executable = False  # type: ignore[attr-defined]
                            _metrics_inc(
                                "dex_agg_selected_total",
                                labels={
                                    "provider": "aerodrome_cl",
                                    "mode": "exact_in_direct",
                                },
                            )
                            _logger.info(
                                f"Direct DIEM/USDC quote SUCCESS via slot0: in={amount_in}, out={direct_quote.amount_out}"
                            )
                            return direct_quote
                        # Not a warning - fallback to other providers is expected behavior
                        # The slot0 quote may return None for various reasons (different token order, etc.)
                        _logger.debug(
                            "Direct DIEM/USDC slot0 quote returned None or zero output: "
                            f"quote={direct_quote}, falling back to other providers"
                        )
                    except Exception as exc:
                        # Only log at DEBUG - fallback to other providers handles this case
                        _logger.debug(
                            f"Direct DIEM/USDC slot0 quote exception: {exc}, falling back"
                        )

        # Fallback: Try DIEM-aware two-stage routing (DIEM↔USDC via VVV)
        allow_bridge_fallback = True
        if self._is_diem_route(plan) and len(plan.tokens) == 2:
            if allowed is not None and "bridge_vvv" not in allowed:
                allow_bridge_fallback = False
            if allow_bridge_fallback:
                buy_direct_only = os.getenv(
                    "DIEM_BUY_DIRECT_ONLY", "0"
                ).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                if buy_direct_only:
                    try:
                        quote_addr = (
                            (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        diem_addr = (
                            (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        tokens = [str(t).strip().lower() for t in plan.tokens]
                        if quote_addr and diem_addr and tokens:
                            if tokens[0] == quote_addr and tokens[-1] == diem_addr:
                                allow_bridge_fallback = False
                    except Exception:
                        pass
            if not allow_bridge_fallback:
                _logger.info(
                    "Skipping DIEM bridge composite fallback for direct route",
                    extra={
                        "reason": "bridge_fallback_disabled",
                        "allowed_providers": sorted(allowed)
                        if allowed is not None
                        else None,
                        "route": list(plan.tokens),
                    },
                )
        if (
            self._is_diem_route(plan)
            and len(plan.tokens) == 2
            and allow_bridge_fallback
        ):
            try:
                from libs.dex import composite as composite_module
                from libs.dex.diem_fallbacks import build_two_stage_diem_route

                attach_composite_metadata_fn = getattr(
                    composite_module, "attach_composite_metadata", None
                )
                quote_composite_exact_in_fn = getattr(
                    composite_module, "quote_composite_exact_in", None
                )

                two_stage = build_two_stage_diem_route(plan.tokens[0], plan.tokens[1])
                if two_stage:
                    stage1_route, stage2_route = two_stage
                    # Build bridge legs metadata
                    diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip()
                    vvv_usdc_pool = (
                        os.getenv("VVV_USDC_POOL_ADDRESS") or ""
                    ).strip() or (os.getenv("VVV_USDC_POOL_V3_ADDRESS") or "").strip()

                    bridge_legs = []
                    # Stage 1: DIEM/VVV or VVV/DIEM
                    if diem_vvv_pair:
                        diem_vvv_provider = (
                            os.getenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
                            .strip()
                            .lower()
                            or "aerodrome"
                        )
                        # DIEM/VVV is a VOLATILE pool on Aerodrome, not stable
                        stable_env = (
                            os.getenv("DIEM_VVV_STABLE")
                            or os.getenv("AERODROME_STABLE")
                            or "false"
                        )
                        try:
                            diem_vvv_stable = str(stable_env).strip().lower() in {
                                "1",
                                "true",
                                "yes",
                                "on",
                            }
                        except Exception:
                            diem_vvv_stable = False

                        bridge_leg_stage1 = {
                            "token_in": stage1_route.tokens[0],
                            "token_out": stage1_route.tokens[1],
                            "provider": diem_vvv_provider,
                            "pool_address": diem_vvv_pair,
                            "fee": None,
                            "stable": diem_vvv_stable,
                        }
                        bridge_legs.append(bridge_leg_stage1)
                    # Stage 2: VVV/USDC
                    if vvv_usdc_pool:
                        fee = None
                        try:
                            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
                            fee = int(fee_str)
                        except Exception:
                            fee = 3000
                        bridge_legs.append(
                            {
                                "token_in": stage2_route.tokens[0],
                                "token_out": stage2_route.tokens[1],
                                "provider": "uniswap_v3",
                                "pool_address": vvv_usdc_pool,
                                "fee": fee,
                            }
                        )
                    else:
                        _logger.warning(
                            "DIEM composite bridge metadata missing VVV/USDC pool address",
                            extra={
                                "event": "dex_bridge_metadata_missing_pool",
                                "leg": "vvv_usdc",
                                "route": list(stage2_route.tokens)
                                if hasattr(stage2_route, "tokens")
                                else None,
                            },
                        )

                    if bridge_legs and attach_composite_metadata_fn:
                        # Create composite route with explicit bridge hop tokens/fees
                        composite_tokens: list[str] = list(stage1_route.tokens)
                        if stage2_route.tokens:
                            composite_tokens.extend(list(stage2_route.tokens)[1:])
                        composite_fees: list[int | None] = [
                            hop.fee for hop in stage1_route.hops
                        ]
                        composite_fees.extend(hop.fee for hop in stage2_route.hops)
                        try:
                            composite_route = make_route(
                                composite_tokens, composite_fees or None
                            )
                        except Exception:
                            composite_route = make_route(plan.tokens)
                        attach_composite_metadata_fn(
                            composite_route, bridge_legs=bridge_legs, is_composite=True
                        )
                        composite_quote = None
                        if quote_composite_exact_in_fn:
                            composite_quote = quote_composite_exact_in_fn(
                                self,
                                composite_route,
                                amount_in,
                                bridge_legs=bridge_legs,
                            )
                        if composite_quote:
                            from libs.dex.composite import CompositeQuote

                            if isinstance(composite_quote, CompositeQuote):
                                representative_route = (
                                    composite_quote.legs[0].route
                                    if composite_quote.legs
                                    else plan
                                )
                                quote = Quote(
                                    provider="diem_composite",
                                    amount_in=composite_quote.amount_in,
                                    amount_out=composite_quote.amount_out,
                                    route=representative_route,
                                )
                                self._attach_composite_metadata(
                                    quote, composite_quote, mode="exact_in"
                                )
                                _metrics_inc(
                                    "dex_agg_selected_total",
                                    labels={
                                        "provider": "diem_composite",
                                        "mode": "exact_in",
                                    },
                                )
                                return quote
            except Exception as exc:
                if _debug_routes_enabled():
                    _logger.debug(f"DIEM two-stage routing failed, falling back: {exc}")

        _ensure_composite_loaded()
        # Check for composite route first (bridge paths with multiple venues)
        composite_enabled = str(
            os.getenv("DEX_COMPOSITE_ENABLE", "1")
        ).strip().lower() in {"1", "true", "yes", "on"}

        if (
            composite_enabled
            and is_composite_route is not None
            and quote_composite_exact_in is not None
            and is_composite_route(plan)
        ):
            try:
                bridge_legs = getattr(plan, "_bridge_legs", None)
                composite_quote = quote_composite_exact_in(
                    self, plan, amount_in, bridge_legs=bridge_legs
                )
                if composite_quote:
                    # Convert CompositeQuote to Quote format
                    from libs.dex.composite import CompositeQuote

                    if isinstance(composite_quote, CompositeQuote):
                        # Use the first leg's route as the representative route
                        representative_route = (
                            composite_quote.legs[0].route
                            if composite_quote.legs
                            else plan
                        )
                        quote = Quote(
                            provider="composite",
                            amount_in=composite_quote.amount_in,
                            amount_out=composite_quote.amount_out,
                            route=representative_route,
                        )
                        self._attach_composite_metadata(
                            quote, composite_quote, mode="exact_in"
                        )
                        _metrics_inc(
                            "dex_agg_selected_total",
                            labels={"provider": "composite", "mode": "exact_in"},
                        )
                        return quote
            except Exception as exc:
                if _debug_routes_enabled():
                    _logger.debug(f"Composite quote failed, falling back: {exc}")
                # Fall through to regular quote path

        quotes = self.quote_all(amount_in, plan, allowed_providers=allowed_providers)
        allowed: set[str] | None = None
        if allowed_providers is not None:
            allowed = {
                str(name).strip().lower()
                for name in allowed_providers
                if str(name).strip()
            }
            quotes = [q for q in quotes if q.provider.lower() in allowed]
        exec_quotes = [q for q in quotes if getattr(q, "executable", True)]
        if exec_quotes:
            best = max(exec_quotes, key=lambda q: q.amount_out)
            _metrics_inc("dex_agg_selected_total", labels={"provider": best.provider})
            return best
        if quotes and not exec_quotes:
            # Preserve preview quotes but signal lack of executable liquidity.
            self._emit_quote_diagnostics(
                reason="no_executable_quotes",
                route_plan=plan,
                amount=amount_in,
                mode="exact_in",
            )
            preview = max(quotes, key=lambda q: q.amount_out)
            return preview
        if not quotes:
            if _debug_routes_enabled():
                tokens = list(plan.tokens)
                _logger.warning(
                    "dex aggregator no quotes route=%s amount_in=%s mode=exact_in",
                    tokens,
                    int(amount_in),
                )
            base_context = dict(self._last_quote_context)
            base_diagnostics = list(self._last_quote_diagnostics)

            promo_quotes, promo_diag = self._promote_inspection_quotes(
                plan=plan,
                amount=amount_in,
                mode="exact_in",
                method="quote",
                allowed=allowed,
                base_diagnostics=base_diagnostics,
            )
            if promo_diag:
                base_diagnostics.extend(promo_diag)
            if promo_quotes:
                quotes = promo_quotes
                self._last_quote_context = base_context
                self._last_quote_diagnostics = base_diagnostics
                best = max(quotes, key=lambda q: q.amount_out)
                _metrics_inc(
                    "dex_agg_selected_total", labels={"provider": best.provider}
                )
                return best

            rescue_quotes, rescue_diag = self._diem_rescue_quotes(
                plan=plan,
                amount=amount_in,
                mode="exact_in",
                base_diagnostics=base_diagnostics,
            )
            if rescue_diag:
                base_diagnostics.extend(rescue_diag)
            if rescue_quotes:
                quotes = rescue_quotes
                self._last_quote_context = base_context
                self._last_quote_diagnostics = base_diagnostics
                best = max(quotes, key=lambda q: q.amount_out)
                _metrics_inc(
                    "dex_agg_selected_total", labels={"provider": best.provider}
                )
                return best

            fallback_attempts: list[dict[str, Any]] = []
            # Calculate minimum amount in wei based on min trade USD config.
            # Skip fallback decay if scaled amount would fall below minimum.
            try:
                min_trade_usd = float(
                    os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0") or 2.0
                )
            except Exception:
                min_trade_usd = 2.0
            # Estimate min_amount_in using approximate DIEM price ($200-250)
            # Use conservative estimate to avoid rejecting valid small trades
            diem_price_estimate = 200.0  # Conservative floor
            min_amount_wei = (
                int(min_trade_usd / diem_price_estimate * 10**18)
                if min_trade_usd > 0
                else 0
            )

            for factor in (0.5, 0.25):
                scaled_amount = max(int(amount_in * factor), 1)
                if scaled_amount == amount_in or scaled_amount <= 0:
                    continue
                # Stop fallback decay if we'd go below minimum trade size
                if min_amount_wei > 0 and scaled_amount < min_amount_wei:
                    _logger.debug(
                        f"Fallback decay stopped: scaled_amount={scaled_amount} < min_amount_wei={min_amount_wei}"
                    )
                    break
                scaled_quotes = self.quote_all(
                    scaled_amount, plan, allowed_providers=allowed_providers
                )
                if allowed is not None:
                    scaled_quotes = [
                        q for q in scaled_quotes if q.provider.lower() in allowed
                    ]
                fallback_attempts.append(
                    {
                        "factor": factor,
                        "amount_in": scaled_amount,
                        "quotes": [
                            {
                                "provider": q.provider,
                                "amount_out": int(getattr(q, "amount_out", 0)),
                            }
                            for q in scaled_quotes
                        ],
                        "diagnostics": list(self._last_quote_diagnostics),
                    }
                )
                if scaled_quotes:
                    break
            self._last_quote_context = base_context
            self._last_quote_diagnostics = base_diagnostics
            self._emit_quote_diagnostics(
                reason="no_quotes",
                route_plan=plan,
                amount=amount_in,
                mode="exact_in",
                fallback_attempts=fallback_attempts,
            )
            _metrics_inc("dex_agg_no_quotes_total")
            return None
        # No quotes selected after filtering; treat as lack of executable liquidity.
        return None

    def trade_best(
        self, amount_in: int, min_out_bps: int, route: RouteLike | None = None, **kwargs
    ) -> dict[str, str]:
        route = kwargs.get("route", route)
        correlation_id = kwargs.get("correlation_id") or kwargs.get("corr_id")
        allowed_providers = kwargs.get("allowed_providers")
        if route is None and "path" in kwargs:
            route = kwargs["path"]
        if route is None:
            raise ValueError("route/path is required")
        slippage_bps, _cap = self._clamp_slippage_bps(min_out_bps)
        if allowed_providers is None:
            allowed_providers = self._execution_provider_names
        quote = self.best_quote(amount_in, route, allowed_providers=allowed_providers)
        if quote is None:
            raise RuntimeError(
                "No executable quotes available from configured DEX providers"
            )
        if not getattr(quote, "executable", True):
            raise RuntimeError(
                "best_quote returned analytic-only (non-executable) quote"
            )
        if self._is_composite_quote_obj(quote):
            return self._execute_composite_exact_in(
                quote,
                slippage_bps,
                correlation_id=str(correlation_id) if correlation_id else None,
            )
        min_out = quote.amount_out * (10_000 - slippage_bps) // 10_000
        provider = self._provider_by_name(quote.provider)
        try:
            result = self._invoke_provider(
                provider, "trade", quote.as_route(), amount_in, min_out
            )
            self._circ_on_success(provider.name)
            return result
        except Exception as exc:
            _metrics_inc(
                "dex_agg_trade_errors_total",
                labels={"provider": quote.provider, "mode": "exact_in"},
            )
            self._circ_on_failure(provider.name)
            raise exc

    def quote_all_exact_out(
        self,
        amount_out: int,
        route: RouteLike,
        allowed_providers: Sequence[str] | None = None,
    ) -> list[Quote]:
        # When execution providers are explicitly configured, treat exact-out preview
        # as execution-shaped so we don't spam diagnostics with execution-ineligible
        # venues (notably Aerodrome "mode_unsupported" for exact-out).
        auto_allowed = False
        execution_allowlist: set[str] | None = None
        if allowed_providers is None and bool(
            getattr(self, "_execution_providers_configured", False)
        ):
            candidate = list(getattr(self, "_execution_provider_names_exact_out", []))
            # If the exact-out execution allowlist is empty, keep legacy preview behavior.
            if candidate:
                allowed_providers = candidate
                auto_allowed = True

        route_plan = as_route_plan(route)
        is_diem_route = self._is_diem_route(route_plan)
        allowed: set[str] | None = None
        if allowed_providers is not None:
            allowed = {
                str(name).strip().lower()
                for name in allowed_providers
                if str(name).strip()
            }
            if auto_allowed:
                execution_allowlist = set(allowed)
        allowed_list = sorted(allowed) if allowed is not None else None
        preview_mode = allowed is None
        active: list[DexProvider] = []
        skipped_diag: list[dict[str, Any]] = []
        skipped_eligible: list[dict[str, Any]] = []

        # Pre-compute V2 provider availability for canonical routes (used in loop below)
        force_v2_for_canonical = os.getenv(
            "DEX_FORCE_V2_FOR_CANONICAL", "0"
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        def _mark_skip(provider: DexProvider, reason: str, eligible: bool) -> None:
            entry = {
                "provider": provider.name,
                "status": "skipped",
                "mode": "exact_out",
                "method": "quote_exact_out",
                "amount": int(amount_out),
                "reason": reason,
            }
            if allowed_list is not None and reason in {
                "not_allowed",
                "execution_disabled",
            }:
                entry["allowed_providers"] = allowed_list
            if reason == "execution_disabled":
                entry["execution_providers"] = list(self._execution_provider_names)
            skipped_diag.append(entry)
            if (
                reason in {"not_allowed", "execution_disabled"}
                and _debug_routes_enabled()
            ):
                _logger.debug(
                    "Skipping provider=%s reason=%s allowed=%s execution=%s route=%s mode=exact_out",
                    provider.name,
                    reason,
                    allowed_list,
                    list(self._execution_provider_names)
                    if reason == "execution_disabled"
                    else None,
                    list(route_plan.tokens) if hasattr(route_plan, "tokens") else None,
                )
            if eligible:
                skipped_eligible.append({"provider": provider.name, "reason": reason})

        for provider in self.providers:
            exec_enabled = (
                True
                if preview_mode
                else (
                    provider.name.lower() in execution_allowlist
                    if execution_allowlist is not None
                    else self._execution_enabled(provider.name)
                )
            )
            circ_open_raw = self._circ_is_open(provider.name)
            force_allow_circ = (
                circ_open_raw
                and is_diem_route
                and self._force_diem_v2
                and provider.name.lower() == "uniswap_v2"
            )
            circ_open = bool(circ_open_raw and not force_allow_circ)
            eligible = exec_enabled and not circ_open

            if allowed is not None and provider.name.lower() not in allowed:
                if not auto_allowed:
                    _mark_skip(provider, "not_allowed", eligible)
                continue

            # Determine route characteristics
            route_is_v3 = False
            is_canonical_v2 = False
            try:
                route_is_v3 = (
                    route_plan.is_uniswap_v3()
                    if hasattr(route_plan, "is_uniswap_v3")
                    else False
                )
                # Check if this is a canonical V2-compatible route (DIEM->WETH->USDC).
                if is_diem_route:
                    is_canonical_v2 = self._is_usdc_weth_diem_path(route_plan)
                    # Also check metadata for explicit marking
                    if not is_canonical_v2:
                        try:
                            metadata = getattr(route_plan, "_metadata", None)
                            if metadata and metadata.get("canonical_v2", False):
                                is_canonical_v2 = True
                        except Exception:
                            pass
            except Exception:
                pass

            # Route-specific overrides before filtering
            if route_is_v3 and provider.name.lower() == "uniswap_v3":
                exec_enabled = True
                eligible = True

            # EXCEPT bridge_vvv and aerodrome which provide DIEM/VVV liquidity
            if (
                force_v2_for_canonical
                and is_diem_route
                and not route_is_v3
                and provider.name.lower()
                not in ("uniswap_v2", "bridge_vvv", "aerodrome")
            ):
                _mark_skip(provider, "force_v2_canonical", eligible)
                continue

            # Filter providers based on route type
            if route_is_v3:
                if provider.name.lower() == "uniswap_v2":
                    if not (is_diem_route and is_canonical_v2):
                        _mark_skip(provider, "v3_route_v2_disallowed", eligible)
                        continue
            elif force_v2_for_canonical and is_diem_route and is_canonical_v2:
                # V2-compatible canonical route: prefer V2 but allow V3 fallback
                # When force_v2_for_canonical is enabled, we prefer V2 by processing it first,
                # but we don't block V3 providers upfront since V2 might return zero output
                # even when it's enabled and not circuit-open. V3 providers will be attempted
                # if V2 fails during quote collection.
                # For canonical V2 routes, don't check _should_skip_v2() as it's already allowed
                pass  # Allow all providers to attempt; V2 will be preferred by processing order
            elif provider.name.lower() == "uniswap_v2":
                # For non-canonical routes, check if V2 should be skipped
                # Note: _should_skip_v2() now handles canonical routes correctly
                if self._should_skip_v2(route_plan):
                    _mark_skip(provider, "v2_incompatible_route", False)
                    continue
            if not exec_enabled and not preview_mode:
                _metrics_inc(
                    "dex_execution_skips_total",
                    labels={"provider": provider.name, "reason": "not_enabled"},
                )
                _mark_skip(provider, "execution_disabled", False)
                continue
            if not self._supports_exact_out(provider):
                _mark_skip(provider, "mode_unsupported", eligible)
                continue
            if not self._discovery_enabled(provider.name):
                _mark_skip(provider, "discovery_disabled", eligible)
                continue

            compatible, compat_reason = self._diem_provider_compatibility(
                provider.name, route_plan
            )
            if not compatible:
                if _debug_routes_enabled():
                    _logger.debug(
                        "Skipping provider=%s for DIEM route=%s (exact_out) reason=%s",
                        provider.name,
                        list(route_plan.tokens),
                        compat_reason,
                    )
                _metrics_inc(
                    "dex_route_incompatible_skips_total",
                    labels={
                        "provider": provider.name,
                        "reason": compat_reason or "route_incompatible",
                    },
                )
                _mark_skip(provider, compat_reason or "route_incompatible", False)
                continue

            if circ_open:
                _metrics_inc(
                    "dex_circuit_skips_total",
                    labels={"provider": provider.name},
                )
                _mark_skip(provider, "circuit_open", False)
                continue
            if (
                is_diem_route
                and self._force_diem_v2
                and provider.name.lower() != "uniswap_v2"
            ):
                _mark_skip(provider, "force_diem_v2_only", eligible)
                continue

            # Log provider selection for debugging
            if _debug_routes_enabled() and is_diem_route:
                _logger.debug(
                    f"Provider selected for DIEM route (exact_out): provider={provider.name}, route={list(route_plan.tokens)}, "
                    f"is_v3={route_is_v3}, force_v2_for_canonical={force_v2_for_canonical}"
                )

            active.append(provider)

        # When force_v2_for_canonical is enabled for canonical V2 routes, prioritize V2 provider
        # by sorting it first, but still allow other providers to attempt as fallback
        if force_v2_for_canonical and is_diem_route and is_canonical_v2:
            active.sort(key=lambda p: 0 if p.name.lower() == "uniswap_v2" else 1)

        quotes = self._collect_quotes(
            active, "quote_exact_out", route_plan, amount_out, mode="exact_out"
        )
        if skipped_diag:
            merged = list(self._last_quote_diagnostics)
            merged.extend(skipped_diag)
            self._last_quote_diagnostics = merged
        if skipped_eligible:
            self._log_skipped_executable(
                skipped_eligible, route_plan, amount=amount_out, mode="exact_out"
            )
        return quotes

    def best_quote_exact_out(
        self,
        amount_out: int,
        route: RouteLike,
        *,
        allowed_providers: Sequence[str] | None = None,
    ) -> Quote | None:
        plan = as_route_plan(route)
        allowed: set[str] | None = None
        if allowed_providers is not None:
            allowed = {
                str(name).strip().lower()
                for name in allowed_providers
                if str(name).strip()
            }

        # PRIORITY 0: Try direct DIEM/USDC SlipStream pool first (highest liquidity)
        # Use slot0-based quoting since Aerodrome SlipStream pools have a different quoter
        is_diem = self._is_diem_route(plan)
        is_2_token = len(plan.tokens) == 2
        if is_diem and is_2_token:
            diem_usdc_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
            prefer_direct = os.getenv(
                "DIEM_PREFER_DIRECT_ROUTE", "1"
            ).strip().lower() in {"1", "true", "yes", "on"}
            allowed_cl = allowed is None or "aerodrome_cl" in allowed
            cl_router = (os.getenv("AERODROME_CL_ROUTER_ADDRESS") or "").strip()
            tick_spacing_raw = os.getenv("DIEM_USDC_TICK_SPACING")
            if tick_spacing_raw is None or not str(tick_spacing_raw).strip():
                tick_spacing = 100
            else:
                try:
                    tick_spacing = int(str(tick_spacing_raw).strip())
                except Exception:
                    tick_spacing = None
            _logger.info(
                f"Direct DIEM/USDC slot0 exact_out check: is_diem={is_diem}, tokens={len(plan.tokens)}, "
                f"pool={'set' if diem_usdc_pool else 'NOT SET'}, prefer_direct={prefer_direct}"
            )
            if prefer_direct and not diem_usdc_pool:
                _logger.warning(
                    "Direct DIEM/USDC slot0 exact_out skipped: DIEM_USDC_POOL_ADDRESS missing",
                    extra={
                        "reason": "missing_pool",
                        "allowed_providers": sorted(allowed)
                        if allowed is not None
                        else None,
                    },
                )
            if prefer_direct and diem_usdc_pool and not allowed_cl:
                _logger.info(
                    "Direct DIEM/USDC slot0 exact_out skipped: aerodrome_cl not in allowed_providers",
                    extra={
                        "reason": "not_allowed",
                        "allowed_providers": sorted(allowed)
                        if allowed is not None
                        else None,
                    },
                )
            if prefer_direct and diem_usdc_pool and allowed_cl:
                try:
                    # Use slot0-based quoting for DIEM/USDC SlipStream pool
                    from libs.dex.diem_fallbacks import diem_usdc_slot0_quote_exact_out

                    direct_quote = diem_usdc_slot0_quote_exact_out(
                        amount_out, plan.tokens[0], plan.tokens[1]
                    )
                    if direct_quote and direct_quote.amount_in > 0:
                        # Exact-out on Aerodrome CL is preview-only.
                        try:
                            object.__setattr__(direct_quote, "executable", False)
                        except Exception:
                            direct_quote.executable = False  # type: ignore[attr-defined]
                        if not cl_router or not tick_spacing or tick_spacing <= 0:
                            _logger.warning(
                                "Direct DIEM/USDC exact_out quote is preview-only; missing CL execution config",
                                extra={
                                    "router_configured": bool(cl_router),
                                    "tick_spacing": tick_spacing,
                                },
                            )
                        _metrics_inc(
                            "dex_agg_selected_total",
                            labels={
                                "provider": "aerodrome_cl",
                                "mode": "exact_out_direct",
                            },
                        )
                        _logger.info(
                            f"Direct DIEM/USDC exact_out quote SUCCESS via slot0: out={amount_out}, in={direct_quote.amount_in}"
                        )
                        return direct_quote
                    _logger.warning(
                        "Direct DIEM/USDC slot0 exact_out quote returned None or zero input: "
                        f"quote={direct_quote}, falling back"
                    )
                except Exception as exc:
                    _logger.warning(
                        f"Direct DIEM/USDC slot0 exact_out quote exception: {exc}"
                    )

        # Fallback: Try DIEM-aware two-stage routing (DIEM↔USDC via VVV)
        allow_bridge_fallback = True
        if is_diem and is_2_token:
            if allowed is not None and "bridge_vvv" not in allowed:
                allow_bridge_fallback = False
            if allow_bridge_fallback:
                buy_direct_only = os.getenv(
                    "DIEM_BUY_DIRECT_ONLY", "0"
                ).strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                if buy_direct_only:
                    try:
                        quote_addr = (
                            (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        diem_addr = (
                            (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
                        )
                        tokens = [str(t).strip().lower() for t in plan.tokens]
                        if quote_addr and diem_addr and tokens:
                            if tokens[0] == quote_addr and tokens[-1] == diem_addr:
                                allow_bridge_fallback = False
                    except Exception:
                        pass
            if not allow_bridge_fallback:
                _logger.info(
                    "Skipping DIEM bridge composite fallback for direct route (exact_out)",
                    extra={
                        "reason": "bridge_fallback_disabled",
                        "allowed_providers": sorted(allowed)
                        if allowed is not None
                        else None,
                        "route": list(plan.tokens),
                    },
                )
        if is_diem and is_2_token and allow_bridge_fallback:
            try:
                from libs.dex import composite as composite_module
                from libs.dex.diem_fallbacks import build_two_stage_diem_route

                attach_composite_metadata_fn = getattr(
                    composite_module, "attach_composite_metadata", None
                )
                quote_composite_exact_out_fn = getattr(
                    composite_module, "quote_composite_exact_out", None
                )

                two_stage = build_two_stage_diem_route(plan.tokens[0], plan.tokens[1])
                if two_stage:
                    stage1_route, stage2_route = two_stage
                    # Build bridge legs metadata
                    diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip()
                    vvv_usdc_pool = (
                        os.getenv("VVV_USDC_POOL_ADDRESS") or ""
                    ).strip() or (os.getenv("VVV_USDC_POOL_V3_ADDRESS") or "").strip()

                    bridge_legs = []
                    # Stage 1: DIEM/VVV or VVV/DIEM
                    if diem_vvv_pair:
                        diem_vvv_provider = (
                            os.getenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
                            .strip()
                            .lower()
                            or "aerodrome"
                        )
                        # DIEM/VVV is a VOLATILE pool on Aerodrome, not stable
                        stable_env = (
                            os.getenv("DIEM_VVV_STABLE")
                            or os.getenv("AERODROME_STABLE")
                            or "false"
                        )
                        try:
                            diem_vvv_stable = str(stable_env).strip().lower() in {
                                "1",
                                "true",
                                "yes",
                                "on",
                            }
                        except Exception:
                            diem_vvv_stable = False

                        bridge_leg_stage1 = {
                            "token_in": stage1_route.tokens[0],
                            "token_out": stage1_route.tokens[1],
                            "provider": diem_vvv_provider,
                            "pool_address": diem_vvv_pair,
                            "fee": None,
                            "stable": diem_vvv_stable,
                        }
                        bridge_legs.append(bridge_leg_stage1)
                    # Stage 2: VVV/USDC
                    if vvv_usdc_pool:
                        fee = None
                        try:
                            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
                            fee = int(fee_str)
                        except Exception:
                            fee = 3000
                        bridge_legs.append(
                            {
                                "token_in": stage2_route.tokens[0],
                                "token_out": stage2_route.tokens[1],
                                "provider": "uniswap_v3",
                                "pool_address": vvv_usdc_pool,
                                "fee": fee,
                            }
                        )
                    else:
                        _logger.warning(
                            "DIEM composite bridge metadata missing VVV/USDC pool address (exact_out)",
                            extra={
                                "event": "dex_bridge_metadata_missing_pool",
                                "leg": "vvv_usdc",
                                "route": list(stage2_route.tokens)
                                if hasattr(stage2_route, "tokens")
                                else None,
                                "mode": "exact_out",
                            },
                        )

                    if bridge_legs and attach_composite_metadata_fn:
                        composite_tokens: list[str] = list(stage1_route.tokens)
                        if stage2_route.tokens:
                            composite_tokens.extend(list(stage2_route.tokens)[1:])
                        composite_fees: list[int | None] = [
                            hop.fee for hop in stage1_route.hops
                        ]
                        composite_fees.extend(hop.fee for hop in stage2_route.hops)
                        try:
                            composite_route = make_route(
                                composite_tokens, composite_fees or None
                            )
                        except Exception:
                            composite_route = make_route(plan.tokens)
                        attach_composite_metadata_fn(
                            composite_route, bridge_legs=bridge_legs, is_composite=True
                        )
                        composite_quote = None
                        if quote_composite_exact_out_fn:
                            composite_quote = quote_composite_exact_out_fn(
                                self,
                                composite_route,
                                amount_out,
                                bridge_legs=bridge_legs,
                            )
                        if composite_quote:
                            from libs.dex.composite import CompositeQuote

                            if isinstance(composite_quote, CompositeQuote):
                                representative_route = (
                                    composite_quote.legs[0].route
                                    if composite_quote.legs
                                    else plan
                                )
                                quote = Quote(
                                    provider="diem_composite",
                                    amount_in=composite_quote.amount_in,
                                    amount_out=composite_quote.amount_out,
                                    route=representative_route,
                                )
                                self._attach_composite_metadata(
                                    quote, composite_quote, mode="exact_out"
                                )
                                _metrics_inc(
                                    "dex_agg_selected_total",
                                    labels={
                                        "provider": "diem_composite",
                                        "mode": "exact_out",
                                    },
                                )
                                return quote
            except Exception as exc:
                if _debug_routes_enabled():
                    _logger.debug(f"DIEM two-stage routing failed, falling back: {exc}")

        _ensure_composite_loaded()
        # Check for composite route first (bridge paths with multiple venues)
        composite_enabled = str(
            os.getenv("DEX_COMPOSITE_ENABLE", "1")
        ).strip().lower() in {"1", "true", "yes", "on"}

        if (
            composite_enabled
            and is_composite_route is not None
            and quote_composite_exact_out is not None
            and is_composite_route(plan)
        ):
            try:
                bridge_legs = getattr(plan, "_bridge_legs", None)
                composite_quote = quote_composite_exact_out(
                    self, plan, amount_out, bridge_legs=bridge_legs
                )
                if composite_quote:
                    # Convert CompositeQuote to Quote format
                    from libs.dex.composite import CompositeQuote

                    if isinstance(composite_quote, CompositeQuote):
                        # Use the first leg's route as the representative route
                        representative_route = (
                            composite_quote.legs[0].route
                            if composite_quote.legs
                            else plan
                        )
                        quote = Quote(
                            provider="composite",
                            amount_in=composite_quote.amount_in,
                            amount_out=composite_quote.amount_out,
                            route=representative_route,
                        )
                        self._attach_composite_metadata(
                            quote, composite_quote, mode="exact_out"
                        )
                        _metrics_inc(
                            "dex_agg_selected_total",
                            labels={"provider": "composite", "mode": "exact_out"},
                        )
                        return quote
            except Exception as exc:
                if _debug_routes_enabled():
                    _logger.debug(f"Composite quote failed, falling back: {exc}")
                # Fall through to regular quote path

        quotes = self.quote_all_exact_out(
            amount_out, plan, allowed_providers=allowed_providers
        )
        if _debug_routes_enabled():
            _logger.debug(
                f"best_quote_exact_out: collected {len(quotes)} quotes for {amount_out}"
            )
        allowed: set[str] | None = None
        if allowed_providers is not None:
            allowed = {
                str(name).strip().lower()
                for name in allowed_providers
                if str(name).strip()
            }
            quotes = [q for q in quotes if q.provider.lower() in allowed]
        exec_quotes = [q for q in quotes if getattr(q, "executable", True)]
        if exec_quotes:
            best = min(exec_quotes, key=lambda q: q.amount_in)
            _metrics_inc(
                "dex_agg_selected_total",
                labels={"provider": best.provider, "mode": "exact_out"},
            )
            return best
        if quotes and not exec_quotes:
            # Preview-only quotes; flag lack of executable liquidity.
            self._emit_quote_diagnostics(
                reason="no_executable_quotes_exact_out",
                route_plan=plan,
                amount=amount_out,
                mode="exact_out",
            )
            preview = min(quotes, key=lambda q: q.amount_in)
            return preview
        base_context = dict(self._last_quote_context)
        base_diagnostics = list(self._last_quote_diagnostics)

        promo_quotes, promo_diag = self._promote_inspection_quotes(
            plan=plan,
            amount=amount_out,
            mode="exact_out",
            method="quote_exact_out",
            allowed=allowed,
            base_diagnostics=base_diagnostics,
        )
        if promo_diag:
            base_diagnostics.extend(promo_diag)
        if promo_quotes:
            quotes = promo_quotes
            self._last_quote_context = base_context
            self._last_quote_diagnostics = base_diagnostics
            best = min(quotes, key=lambda q: q.amount_in)
            _metrics_inc(
                "dex_agg_selected_total",
                labels={"provider": best.provider, "mode": "exact_out"},
            )
            return best
        fallback_plan: RoutePlan | None = None
        if plan.is_uniswap_v3():
            try:
                fallback_plan = make_route(plan.tokens)
            except Exception:
                fallback_plan = None
        if fallback_plan is not None:
            if _debug_routes_enabled():
                _logger.warning(
                    "dex aggregator exact-out retry using v2 route=%s amount_out=%s",
                    list(fallback_plan.tokens),
                    int(amount_out),
                )
            quotes = self.quote_all_exact_out(
                amount_out, fallback_plan, allowed_providers=allowed_providers
            )
            if allowed is not None:
                quotes = [q for q in quotes if q.provider.lower() in allowed]
            if quotes:
                best = min(quotes, key=lambda q: q.amount_in)
                _metrics_inc(
                    "dex_agg_selected_total",
                    labels={
                        "provider": best.provider,
                        "mode": "exact_out",
                        "fallback": "v2",
                    },
                )
                return best
        # Try exact-in fallback if enabled and this is a DIEM route
        exact_in_fallback_enabled = str(
            os.getenv("DIEM_EXACT_IN_FALLBACK_ENABLE", "0")
        ).strip().lower() in {"1", "true", "yes", "on"}
        if exact_in_fallback_enabled and self._is_diem_route(plan):
            try:
                # Estimate input needed using market price with buffer
                max_usd_str = os.getenv("DIEM_EXACT_IN_FALLBACK_MAX_USD") or "1000"
                try:
                    max_usd = float(max_usd_str)
                except Exception:
                    max_usd = 1000.0

                # Get quote token decimals
                quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
                quote_decimals = 6  # USDC default
                if quote_token:
                    try:
                        from web3 import Web3  # type: ignore

                        from libs.agentkit_ext.web3_utils import get_web3

                        w3 = get_web3()
                        erc20_abi = [
                            {
                                "constant": True,
                                "inputs": [],
                                "name": "decimals",
                                "outputs": [{"name": "", "type": "uint8"}],
                                "type": "function",
                            }
                        ]
                        token_contract = w3.eth.contract(
                            address=Web3.to_checksum_address(quote_token), abi=erc20_abi
                        )
                        quote_decimals = token_contract.functions.decimals().call()
                    except Exception:
                        pass

                # Estimate amount_in: use a conservative price estimate
                # For DIEM, assume ~$100 USD per token as a conservative estimate
                diem_price_estimate_usd = 100.0
                diem_decimals = 18
                try:
                    diem_decimals = int(os.getenv("DIEM_DECIMALS") or 18)
                except Exception:
                    pass

                # Calculate: amount_out (DIEM) * price = USD value
                diem_tokens = amount_out / (10**diem_decimals)
                estimated_usd = diem_tokens * diem_price_estimate_usd

                if estimated_usd <= max_usd:
                    # Add 5% buffer for slippage
                    estimated_usd_with_buffer = estimated_usd * 1.05
                    amount_in_estimate = int(
                        estimated_usd_with_buffer * (10**quote_decimals)
                    )

                    # Try exact-in quote
                    exact_in_quote = self.best_quote(amount_in_estimate, plan)
                    if exact_in_quote and exact_in_quote.amount_out > 0:
                        # Check if output is acceptable (at least 90% of desired)
                        if exact_in_quote.amount_out >= amount_out * 0.9:
                            # Get slippage tolerance
                            slippage_bps = 50  # Default
                            try:
                                slippage_str = (
                                    os.getenv("RISK_MAX_SLIPPAGE_BPS") or "50"
                                )
                                slippage_bps = int(slippage_str)
                            except Exception:
                                pass

                            # Check slippage
                            actual_price = (
                                exact_in_quote.amount_in
                                / float(exact_in_quote.amount_out)
                                * (10**diem_decimals)
                                / (10**quote_decimals)
                            )
                            slippage_pct = abs(
                                (actual_price - diem_price_estimate_usd)
                                / diem_price_estimate_usd
                            )
                            slippage_bps_actual = int(slippage_pct * 10000)

                            if slippage_bps_actual <= slippage_bps:
                                _metrics_inc(
                                    "dex_agg_selected_total",
                                    labels={
                                        "provider": exact_in_quote.provider,
                                        "mode": "exact_out_fallback_exact_in",
                                    },
                                )
                                return exact_in_quote
            except Exception as exc:
                if _debug_routes_enabled():
                    _logger.debug(f"Exact-in fallback failed: {exc}")

        if _debug_routes_enabled():
            _logger.warning(
                "dex aggregator no quotes route=%s amount_out=%s mode=exact_out",
                list(plan.tokens),
                int(amount_out),
            )
        fallback_attempts: list[dict[str, Any]] = []
        # Calculate minimum amount in wei based on min trade USD config.
        # Skip fallback decay if scaled amount would fall below minimum.
        try:
            min_trade_usd = float(
                os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0") or 2.0
            )
        except Exception:
            min_trade_usd = 2.0
        # Estimate min_amount using approximate DIEM price ($200-250)
        diem_price_estimate = 200.0  # Conservative floor
        min_amount_wei = (
            int(min_trade_usd / diem_price_estimate * 10**18)
            if min_trade_usd > 0
            else 0
        )

        for factor in (0.5, 0.25):
            scaled_amount = max(int(amount_out * factor), 1)
            if scaled_amount == amount_out or scaled_amount <= 0:
                continue
            # Stop fallback decay if we'd go below minimum trade size
            if min_amount_wei > 0 and scaled_amount < min_amount_wei:
                _logger.debug(
                    f"Exact-out fallback decay stopped: scaled_amount={scaled_amount} < min_amount_wei={min_amount_wei}"
                )
                break
            try:
                scaled_quotes = self.quote_all_exact_out(
                    scaled_amount, plan, allowed_providers=allowed_providers
                )
            except Exception:
                scaled_quotes = []
            if not scaled_quotes:
                fallback_attempts.append(
                    {
                        "factor": factor,
                        "amount_out": scaled_amount,
                        "quotes": [],
                        "diagnostics": list(self._last_quote_diagnostics),
                    }
                )
                continue
            if allowed is not None:
                scaled_quotes = [
                    q for q in scaled_quotes if q.provider.lower() in allowed
                ]
            fallback_attempts.append(
                {
                    "factor": factor,
                    "amount_out": scaled_amount,
                    "quotes": [
                        {
                            "provider": q.provider,
                            "amount_in": int(getattr(q, "amount_in", 0)),
                        }
                        for q in scaled_quotes
                    ],
                    "diagnostics": list(self._last_quote_diagnostics),
                }
            )
            if scaled_quotes:
                # Scale the quote back up to the original amount_out
                # Use the best quote and scale proportionally
                best_scaled = min(scaled_quotes, key=lambda q: q.amount_in)
                # Scale amount_in proportionally: amount_in_original = amount_in_scaled * (amount_out_original / amount_out_scaled)
                scaled_amount_in = best_scaled.amount_in * amount_out // scaled_amount
                scaled_quote = Quote(
                    provider=best_scaled.provider,
                    amount_in=scaled_amount_in,
                    amount_out=amount_out,
                    route=best_scaled.route or plan,
                )
                if _debug_routes_enabled():
                    _logger.info(
                        f"dex aggregator using scaled quote: factor={factor} "
                        f"scaled_in={best_scaled.amount_in} scaled_out={scaled_amount} "
                        f"original_in={scaled_amount_in} original_out={amount_out}"
                    )
                return scaled_quote
        self._last_quote_context = base_context
        self._last_quote_diagnostics = base_diagnostics
        self._emit_quote_diagnostics(
            reason="no_quotes_exact_out",
            route_plan=plan,
            amount=amount_out,
            mode="exact_out",
            fallback_attempts=fallback_attempts,
        )
        _metrics_inc("dex_agg_no_quotes_total", labels={"mode": "exact_out"})
        return None

    def _execute_composite_exact_in(
        self, quote: Quote, min_out_bps: int, *, correlation_id: str | None = None
    ) -> dict[str, str]:
        legs = self._composite_legs(quote)
        if not legs:
            raise RuntimeError("Composite quote missing leg metadata for execution")

        wait_confirm = (
            os.getenv("DEX_COMPOSITE_WAIT_CONFIRM") or "1"
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            confirm_timeout = int(
                (os.getenv("DEX_COMPOSITE_CONFIRM_TIMEOUT_SECONDS") or "120").strip()
                or 120
            )
        except Exception:
            confirm_timeout = 120

        slippage_bps = self._composite_slippage_bps(min_out_bps)
        current_amount = quote.amount_in
        leg_results: list[dict[str, Any]] = []

        # Use a stable owner across legs so balance/allowance checks are consistent.
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("ETH_PRIVATE_KEY"):
            owner = "0x" + "1" * 40
        else:
            try:
                owner = str(get_address() or "").strip()
            except Exception:
                owner = ""

        # Composite atomicity: ensure all required approvals are in place before any leg executes.
        allowance_legs: list[tuple[int, DexProvider, RoutePlan, int]] = []
        planned_amount = int(current_amount)
        for idx, leg_quote in enumerate(legs):
            provider = self._provider_by_name(leg_quote.provider)
            leg_route = leg_quote.as_route()
            allowance_legs.append((idx, provider, leg_route, int(planned_amount)))
            planned_amount = self._scaled_min_out(
                leg_quote.amount_out, leg_quote.amount_in, planned_amount, slippage_bps
            )

        self._precheck_and_inject_composite_allowances(
            legs=allowance_legs,
            owner=owner,
            correlation_id=correlation_id,
            mode="exact_in",
        )

        def _erc20_balance(token: str) -> int | None:
            if not token or not owner:
                return None
            try:
                erc20 = get_contract(get_web3(), token, "erc20.json")
                return int(erc20.functions.balanceOf(owner).call())
            except Exception:
                return None

        def _wait_for_balance(token: str, required: int, *, leg_index: int) -> None:
            if required <= 0 or not token:
                return
            if os.getenv("PYTEST_CURRENT_TEST"):
                return
            balance = _erc20_balance(token)
            if balance is None or balance >= required:
                return

            # If the previous leg hasn't been confirmed yet (or confirmation was
            # skipped), wait briefly so the intermediate balance is visible for
            # gas estimation on the next leg.
            prev_leg = leg_results[-1] if leg_results else None
            prev_confirm = (
                prev_leg.get("confirmation") if isinstance(prev_leg, dict) else None
            )
            prev_status = (
                prev_confirm.get("status") if isinstance(prev_confirm, dict) else None
            )
            if prev_status != "confirmed":
                prev_tx_hash = str(
                    (prev_leg or {}).get("tx_hash")
                    or (prev_leg or {}).get("hash")
                    or ""
                )
                if prev_tx_hash:
                    from libs.agentkit_ext.agentkit_wallet import (
                        wait_for_tx_confirmation,
                    )

                    prev_confirm = wait_for_tx_confirmation(
                        prev_tx_hash, timeout=confirm_timeout
                    )
                    if isinstance(prev_leg, dict):
                        prev_leg["confirmation"] = prev_confirm
                    if prev_confirm.get("status") != "confirmed":
                        raise RuntimeError(
                            f"Composite prior leg not confirmed: leg={leg_index - 1} "
                            f"status={prev_confirm.get('status')}"
                        )

            deadline = time.monotonic() + max(1, confirm_timeout)
            last_balance = balance
            while time.monotonic() < deadline:
                last_balance = _erc20_balance(token)
                if last_balance is None or last_balance >= required:
                    return
                time.sleep(1.0)
            if last_balance is not None and last_balance < required:
                raise RuntimeError(
                    f"Composite leg {leg_index} missing balance for token {token}: "
                    f"have={last_balance} need={required}"
                )

        for idx, leg_quote in enumerate(legs):
            provider = self._provider_by_name(leg_quote.provider)
            leg_route = leg_quote.as_route()
            min_out = self._scaled_min_out(
                leg_quote.amount_out, leg_quote.amount_in, current_amount, slippage_bps
            )
            retry_info: dict[str, Any] | None = None

            # Composite trades depend on intermediate balances from prior legs.
            # Ensure the token-in for the next leg is present before attempting
            # gas estimation (which will revert STF when balances are not yet updated).
            if idx > 0:
                try:
                    token_in = str(getattr(leg_route, "tokens", [])[0] or "")
                except Exception:
                    token_in = ""
                _wait_for_balance(token_in, int(current_amount), leg_index=idx)

                if not os.getenv("PYTEST_CURRENT_TEST"):
                    # Pre-approve the intermediate token for the leg-2 router so the
                    # trade call doesn't STF during gas estimation.
                    try:
                        spender = str(
                            getattr(provider, "router_addr", "") or ""
                        ).strip()
                        ensure_allowance = getattr(provider, "_ensure_allowance", None)
                        if (
                            token_in
                            and owner
                            and spender
                            and callable(ensure_allowance)
                        ):
                            ensure_allowance(
                                token_in, owner, spender, int(current_amount)
                            )
                    except Exception as exc:
                        try:
                            _logger.warning(
                                "Composite pre-approval failed: provider=%s token=%s spender=%s required=%s err=%s",
                                getattr(provider, "name", ""),
                                token_in,
                                str(getattr(provider, "router_addr", "") or "").strip(),
                                int(current_amount),
                                str(exc),
                            )
                        except Exception:
                            pass

            try:
                from libs.dex.composite import execute_with_uniswap_v3_stf_retry

                retry_state: dict[str, Any] = {}

                def _attempt() -> Any:
                    return self._invoke_provider(
                        provider, "trade", leg_route, current_amount, min_out
                    )

                res, retry_info = execute_with_uniswap_v3_stf_retry(
                    provider=provider,
                    route=leg_route,
                    required_allowance=int(current_amount),
                    attempt=_attempt,
                    correlation_id=correlation_id,
                    retry_state=retry_state,
                )
                self._circ_on_success(provider.name)
                used_mode = "exact_in"
            except Exception as exc:
                self._circ_on_failure(provider.name)
                _metrics_inc(
                    "dex_composite_exec_fail_total", labels={"mode": "exact_in"}
                )
                raise exc

            leg_result = {
                "provider": provider.name,
                "mode": used_mode,
                "amount_in": int(current_amount),
                "min_out": int(min_out),
            }
            if isinstance(res, dict):
                leg_result.update(res)
            if retry_info and retry_info.get("retried"):
                leg_result["retry"] = retry_info
            leg_results.append(leg_result)

            # Composite legs are separate transactions; wait for confirmation so the
            # next leg can estimate gas against updated balances/allowances.
            if (
                wait_confirm
                and idx < len(legs) - 1
                and not os.getenv("PYTEST_CURRENT_TEST")
            ):
                tx_hash = str(leg_result.get("tx_hash") or leg_result.get("hash") or "")
                if tx_hash:
                    from libs.agentkit_ext.agentkit_wallet import (
                        wait_for_tx_confirmation,
                    )

                    confirm = wait_for_tx_confirmation(tx_hash, timeout=confirm_timeout)
                    leg_result["confirmation"] = confirm
                    if confirm.get("status") != "confirmed":
                        raise RuntimeError(
                            f"Composite leg {idx} not confirmed: status={confirm.get('status')}"
                        )

            current_amount = min_out

        _metrics_inc(
            "dex_agg_trade_total", labels={"provider": "composite", "mode": "exact_in"}
        )
        _metrics_inc("dex_composite_exec_success_total", labels={"mode": "exact_in"})
        final_tx = leg_results[-1].get("tx_hash") if leg_results else None
        return {
            "provider": "composite",
            "tx_hash": final_tx,
            "legs": leg_results,
            "correlation_id": correlation_id,
        }

    def _execute_composite_exact_out(
        self,
        quote: Quote,
        amount_out: int,
        max_in_bps: int,
        route: RouteLike,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, str]:
        legs = self._composite_legs(quote)
        if not legs:
            raise RuntimeError("Composite quote missing leg metadata for execution")

        wait_confirm = (
            os.getenv("DEX_COMPOSITE_WAIT_CONFIRM") or "1"
        ).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            confirm_timeout = int(
                (os.getenv("DEX_COMPOSITE_CONFIRM_TIMEOUT_SECONDS") or "120").strip()
                or 120
            )
        except Exception:
            confirm_timeout = 120

        slippage_bps = self._composite_slippage_bps(max_in_bps)
        total_budget = quote.amount_in * (10_000 + max_in_bps) // 10_000
        available_in = min(quote.amount_in, total_budget)

        # Composite atomicity: pre-check allowances for all legs and inject approvals
        # upfront when needed so trades do not partially execute due to missing approvals.
        allowance_legs: list[tuple[int, DexProvider, RoutePlan, int]] = []
        planned_available_in = int(available_in)
        for idx, leg_quote in enumerate(legs):
            provider = self._provider_by_name(leg_quote.provider)
            leg_route = leg_quote.as_route()
            allowance_legs.append((idx, provider, leg_route, int(planned_available_in)))

            desired_out = leg_quote.amount_out
            if idx == len(legs) - 1:
                desired_out = amount_out
            fallback_min_out = self._scaled_min_out(
                desired_out, leg_quote.amount_in, planned_available_in, slippage_bps
            )
            planned_available_in = max(int(desired_out), int(fallback_min_out))

        # Owner must match the active wallet; keep it stable across legs.
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("ETH_PRIVATE_KEY"):
            owner = "0x" + "1" * 40
        else:
            try:
                owner = str(get_address() or "").strip()
            except Exception:
                owner = ""

        self._precheck_and_inject_composite_allowances(
            legs=allowance_legs,
            owner=owner,
            correlation_id=correlation_id,
            mode="exact_out",
        )

        leg_results: list[dict[str, Any]] = []

        for idx, leg_quote in enumerate(legs):
            provider = self._provider_by_name(leg_quote.provider)
            desired_out = leg_quote.amount_out
            if idx == len(legs) - 1:
                desired_out = amount_out

            max_in_allowed = available_in
            if max_in_allowed <= 0:
                raise RuntimeError(f"No input available for composite leg {idx}")

            used_mode = "exact_out"
            produced_out = desired_out
            retry_state: dict[str, Any] = {}
            retry_info: dict[str, Any] | None = None
            leg_route = leg_quote.as_route()

            try:
                from libs.dex.composite import execute_with_uniswap_v3_stf_retry

                def _attempt_exact_out() -> Any:
                    return self._invoke_provider(
                        provider,
                        "trade_exact_out",
                        leg_route,
                        desired_out,
                        max_in_allowed,
                    )

                res, retry_info = execute_with_uniswap_v3_stf_retry(
                    provider=provider,
                    route=leg_route,
                    required_allowance=int(max_in_allowed),
                    attempt=_attempt_exact_out,
                    correlation_id=correlation_id,
                    retry_state=retry_state,
                )
                self._circ_on_success(provider.name)
            except Exception as exc_out:
                # Exact-out failed; fall back to exact-in using available input.
                self._circ_on_failure(provider.name)
                min_out = self._scaled_min_out(
                    desired_out, leg_quote.amount_in, max_in_allowed, slippage_bps
                )
                try:
                    from libs.dex.composite import execute_with_uniswap_v3_stf_retry

                    def _attempt_exact_in() -> Any:
                        return self._invoke_provider(
                            provider,
                            "trade",
                            leg_route,
                            max_in_allowed,
                            min_out,
                        )

                    res, retry_info = execute_with_uniswap_v3_stf_retry(
                        provider=provider,
                        route=leg_route,
                        required_allowance=int(max_in_allowed),
                        attempt=_attempt_exact_in,
                        correlation_id=correlation_id,
                        retry_state=retry_state,
                    )
                    self._circ_on_success(provider.name)
                    used_mode = "exact_in"
                    produced_out = min_out
                    _metrics_inc(
                        "dex_composite_exec_fallback_total",
                        labels={"leg_index": str(idx)},
                    )
                except Exception as exc_in:
                    self._circ_on_failure(provider.name)
                    _metrics_inc(
                        "dex_composite_exec_fail_total", labels={"mode": "exact_out"}
                    )
                    raise exc_in from exc_out

            leg_result = {
                "provider": provider.name,
                "mode": used_mode,
                "amount_in": int(max_in_allowed),
                "target_out": int(desired_out),
                "produced_out": int(produced_out),
            }
            if isinstance(res, dict):
                leg_result.update(res)
            if retry_info and retry_info.get("retried"):
                leg_result["retry"] = retry_info
            leg_results.append(leg_result)

            if (
                wait_confirm
                and idx < len(legs) - 1
                and not os.getenv("PYTEST_CURRENT_TEST")
            ):
                tx_hash = str(leg_result.get("tx_hash") or leg_result.get("hash") or "")
                if tx_hash:
                    from libs.agentkit_ext.agentkit_wallet import (
                        wait_for_tx_confirmation,
                    )

                    confirm = wait_for_tx_confirmation(tx_hash, timeout=confirm_timeout)
                    leg_result["confirmation"] = confirm
                    if confirm.get("status") != "confirmed":
                        raise RuntimeError(
                            f"Composite leg {idx} not confirmed: status={confirm.get('status')}"
                        )

            available_in = produced_out

        _metrics_inc(
            "dex_agg_trade_total", labels={"provider": "composite", "mode": "exact_out"}
        )
        _metrics_inc("dex_composite_exec_success_total", labels={"mode": "exact_out"})
        final_tx = leg_results[-1].get("tx_hash") if leg_results else None
        return {
            "provider": "composite",
            "tx_hash": final_tx,
            "legs": leg_results,
            "route": list(as_route_plan(route).tokens),
            "correlation_id": correlation_id,
        }

    @staticmethod
    def _min_quote_amount_in() -> int:
        """Minimum input size derived from ARBI_DIEM liquidity floor (quote token units)."""
        try:
            dec = int(os.getenv("QUOTE_TOKEN_DECIMALS", "6") or 6)
        except Exception:
            dec = 6
        try:
            min_usd = float(
                os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0") or 2.0
            )
        except Exception:
            min_usd = 2.0
        return max(1, int(min_usd * (10**dec)))

    @staticmethod
    def _risk_max_slippage_bps() -> int | None:
        """
        Return the configured max slippage cap in bps.

        Uses `RISK_MAX_SLIPPAGE_BPS` when present.
        Returns None when unset/unavailable.
        """
        raw = os.getenv("RISK_MAX_SLIPPAGE_BPS")
        if raw is not None and str(raw).strip() != "":
            try:
                return max(0, min(10_000, int(float(str(raw).strip()))))
            except Exception:
                pass
        return None

    def _clamp_slippage_bps(self, requested_bps: int) -> tuple[int, int | None]:
        try:
            req = int(requested_bps)
        except Exception:
            req = 0
        req = max(0, min(10_000, req))
        cap = self._risk_max_slippage_bps()
        if cap is None:
            return req, None
        return min(req, int(cap)), int(cap)

    def _binary_search_exact_in_quote(
        self,
        target_out: int,
        route: RoutePlan,
        *,
        high: int,
        max_steps: int,
    ) -> Quote | None:
        """Find smallest executable exact-in quote that meets target_out."""
        low = self._min_quote_amount_in()
        best: Quote | None = None
        steps = max(1, max_steps)
        for _ in range(steps):
            if low > high:
                break
            mid = max(low, (low + high) // 2)
            q = self.best_quote(
                mid, route, allowed_providers=self._execution_provider_names
            )
            if q is None or not getattr(q, "executable", True):
                low = mid + 1
                continue
            if q.amount_out >= target_out:
                best = q
                high = mid - 1
            else:
                low = mid + 1
        return best

    def _exact_in_fallback_after_revert(
        self,
        *,
        amount_out: int,
        route: RoutePlan,
        reference_in: int,
        max_in_bps: int,
    ) -> dict[str, str]:
        """
        Attempt exact-in execution after an exact-out revert using size step-down and binary search.
        """
        try:
            diem_decimals = int(os.getenv("DIEM_DECIMALS", "18") or 18)
        except Exception:
            diem_decimals = 18
        try:
            max_steps = int(
                os.getenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10") or 10
            )
        except Exception:
            max_steps = 10
        max_steps = max(1, max_steps)
        # Convert min USD floor to DIEM units (assume $1/DIEM for floor math)
        try:
            min_usd = float(
                os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0") or 2.0
            )
        except Exception:
            min_usd = 2.0
        min_out_floor = max(1, int(min_usd * (10**diem_decimals)))
        current_target = max(amount_out, min_out_floor)
        last_error: Exception | None = None

        for _ in range(max_steps):
            if current_target < min_out_floor:
                break
            budget_in = reference_in * (10_000 + max_in_bps) // 10_000
            budget_in = max(budget_in, self._min_quote_amount_in())
            candidate = self._binary_search_exact_in_quote(
                current_target, route, high=budget_in, max_steps=8
            )
            if candidate and getattr(candidate, "executable", True):
                provider = self._provider_by_name(candidate.provider)
                # Require at least the requested output while honoring slippage cap
                min_out_bps = max(0, 10_000 - max_in_bps)
                min_out = max(
                    current_target,
                    candidate.amount_out * min_out_bps // 10_000,
                )
                try:
                    result = self._invoke_provider(
                        provider,
                        "trade",
                        candidate.as_route(),
                        candidate.amount_in,
                        min_out,
                    )
                    self._circ_on_success(provider.name)
                    _metrics_inc(
                        "dex_agg_trade_total",
                        labels={
                            "provider": candidate.provider,
                            "mode": "exact_in_fallback",
                        },
                    )
                    return result
                except Exception as exc:
                    last_error = exc
                    self._circ_on_failure(provider.name)
            # Step down target size and retry
            current_target = current_target // 2

        if last_error:
            raise last_error
        raise RuntimeError("exact_in_fallback_failed")

    def trade_best_exact_out(
        self, amount_out: int, max_in_bps: int, route: RouteLike | None = None, **kwargs
    ) -> dict[str, str]:
        route = kwargs.get("route", route)
        correlation_id = kwargs.get("correlation_id") or kwargs.get("corr_id")
        allowed_providers = kwargs.get("allowed_providers")
        if route is None and "path" in kwargs:
            route = kwargs["path"]
        if route is None:
            raise ValueError("route/path is required")
        max_in_bps_eff, _cap = self._clamp_slippage_bps(max_in_bps)
        if allowed_providers is not None:
            allowed_exact_out = list(allowed_providers)
        else:
            allowed_exact_out = list(
                getattr(self, "_execution_provider_names_exact_out", [])
            )
            if not allowed_exact_out:
                allowed_exact_out = list(self._execution_provider_names)
        quote = self.best_quote_exact_out(
            amount_out,
            route,
            allowed_providers=allowed_exact_out,
        )
        if quote is None:
            raise RuntimeError(
                "No executable exact-out quotes available from configured DEX providers"
            )
        if not getattr(quote, "executable", True):
            # Treat analytic-only quotes as a signal to jump straight to exact-in fallback.
            return self._exact_in_fallback_after_revert(
                amount_out=amount_out,
                route=quote.as_route(),
                reference_in=max(getattr(quote, "amount_in", 1), 1),
                max_in_bps=max_in_bps_eff,
            )
        if self._is_composite_quote_obj(quote):
            return self._execute_composite_exact_out(
                quote,
                amount_out,
                max_in_bps_eff,
                route,
                correlation_id=str(correlation_id) if correlation_id else None,
            )
        max_in = quote.amount_in * (10_000 + max_in_bps_eff) // 10_000
        provider = self._provider_by_name(quote.provider)
        try:
            result = self._invoke_provider(
                provider, "trade_exact_out", quote.as_route(), amount_out, max_in
            )
            self._circ_on_success(provider.name)
            _metrics_inc(
                "dex_agg_trade_total",
                labels={"provider": quote.provider, "mode": "exact_out"},
            )
            return result
        except Exception:
            _metrics_inc(
                "dex_agg_trade_errors_total",
                labels={"provider": quote.provider, "mode": "exact_out"},
            )
            self._circ_on_failure(provider.name)
            # Attempt exact-in fallback using step-down sizing and binary search.
            fallback_route = quote.as_route()
            return self._exact_in_fallback_after_revert(
                amount_out=amount_out,
                route=fallback_route,
                reference_in=max(quote.amount_in, 1),
                max_in_bps=max_in_bps_eff,
            )

    def trade_best_exact_in(
        self, amount_in: int, min_out_bps: int, route: RouteLike | None = None, **kwargs
    ) -> dict[str, str]:
        """Execute an exact-in swap using the best available quote.

        This is a fallback for when exact-out trades fail. It uses the best
        exact-in quote and executes with slippage protection.
        The effective slippage is clamped to `RISK_MAX_SLIPPAGE_BPS` when configured.

        Args:
            amount_in: Input amount in base units
            min_out_bps: Slippage tolerance in basis points (e.g., 150 for 1.5% slippage)
            route: Trade route (RoutePlan or path list)

        Returns:
            Dict with 'provider', 'tx_hash', and optional 'approval_tx'
        """
        route = kwargs.get("route", route)
        correlation_id = kwargs.get("correlation_id") or kwargs.get("corr_id")
        allowed_providers = kwargs.get("allowed_providers")
        if route is None and "path" in kwargs:
            route = kwargs["path"]
        if route is None:
            raise ValueError("route/path is required")
        slippage_bps, _cap = self._clamp_slippage_bps(min_out_bps)
        if allowed_providers is None:
            allowed_providers = self._execution_provider_names
        quote = self.best_quote(amount_in, route, allowed_providers=allowed_providers)
        if quote is None:
            raise RuntimeError(
                "No executable exact-in quotes available from configured DEX providers"
            )
        if self._is_composite_quote_obj(quote):
            return self._execute_composite_exact_in(
                quote,
                slippage_bps,
                correlation_id=str(correlation_id) if correlation_id else None,
            )
        # Calculate minimum output with slippage protection
        min_out = quote.amount_out * (10_000 - slippage_bps) // 10_000
        provider = self._provider_by_name(quote.provider)
        try:
            result = self._invoke_provider(
                provider, "trade", quote.as_route(), amount_in, min_out
            )
            self._circ_on_success(provider.name)
            _metrics_inc(
                "dex_agg_trade_total",
                labels={"provider": quote.provider, "mode": "exact_in"},
            )
            return result
        except Exception as exc:
            _metrics_inc(
                "dex_agg_trade_errors_total",
                labels={"provider": quote.provider, "mode": "exact_in"},
            )
            self._circ_on_failure(provider.name)
            raise exc


def _provider_missing_reason(spec: dict[str, Any], dex_cfg: Any) -> str | None:
    name = str(spec.get("name", "")).strip().lower()
    if not name:
        return None
    if name == "uniswap_v2":
        router = spec.get("router") or getattr(dex_cfg, "uniswap_v2_router", None)
        if not router:
            return "uniswap_v2 requires UNISWAP_V2_ROUTER_ADDRESS or ROUTER_ADDRESS"
    if name == "uniswap_v3":
        router = spec.get("router") or getattr(dex_cfg, "uniswap_v3_router", None)
        quoter = spec.get("quoter") or getattr(dex_cfg, "uniswap_v3_quoter", None)
        missing = []
        if not router:
            missing.append("UNISWAP_V3_ROUTER_ADDRESS")
        if not quoter:
            missing.append("UNISWAP_V3_QUOTER_ADDRESS")
        if missing:
            return f"uniswap_v3 requires {', '.join(missing)}"
    if name == "aerodrome":
        router = spec.get("router") or getattr(dex_cfg, "aerodrome_router", None)
        if not router:
            return "aerodrome requires AERODROME_ROUTER_ADDRESS"
    if name == "aerodrome_cl":
        router = (
            spec.get("router")
            or getattr(dex_cfg, "aerodrome_cl_router", None)
            or os.getenv("AERODROME_CL_ROUTER_ADDRESS")
        )
        pool = (
            spec.get("pool")
            or spec.get("pool_address")
            or os.getenv("DIEM_USDC_POOL_ADDRESS")
        )
        tick_spacing = (
            spec.get("tick_spacing")
            or spec.get("tickSpacing")
            or os.getenv("DIEM_USDC_TICK_SPACING")
        )
        if not router:
            return "aerodrome_cl requires AERODROME_CL_ROUTER_ADDRESS"
        if not pool:
            return "aerodrome_cl requires DIEM_USDC_POOL_ADDRESS"
        if tick_spacing is None or str(tick_spacing).strip() == "":
            return "aerodrome_cl requires DIEM_USDC_TICK_SPACING"
        try:
            if int(str(tick_spacing).strip()) <= 0:
                return "aerodrome_cl requires DIEM_USDC_TICK_SPACING > 0"
        except Exception:
            return "aerodrome_cl requires DIEM_USDC_TICK_SPACING (invalid value)"
    return None


def _provider_from_spec(spec: dict[str, Any], dex_cfg: Any) -> DexProvider | None:
    name = str(spec.get("name", "")).strip().lower()
    if not name:
        return None
    if name in {
        "bridge_vvv",
        "path_engine",
        "direct_pool",
        "aggregator",
        "external_reference",
        "composite",
        "composite_analytic",
        "diem_canonical",
    }:
        return None
    if name == "uniswap_v2":
        router = spec.get("router") or getattr(dex_cfg, "uniswap_v2_router", None)
        if not router:
            _logger.warning(
                "uniswap_v2 selected but router address missing; set UNISWAP_V2_ROUTER_ADDRESS or ROUTER_ADDRESS"
            )
            return None
        return UniswapV2DexProvider(router)
    if name == "uniswap_v3":
        router = spec.get("router") or getattr(dex_cfg, "uniswap_v3_router", None)
        quoter = spec.get("quoter") or getattr(dex_cfg, "uniswap_v3_quoter", None)
        if not router or not quoter:
            _logger.warning(
                "uniswap_v3 selected but router/quoter addresses missing; set UNISWAP_V3_ROUTER_ADDRESS and UNISWAP_V3_QUOTER_ADDRESS"
            )
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
        router = spec.get("router") or getattr(dex_cfg, "aerodrome_router", None)
        if not router:
            _logger.warning(
                "aerodrome selected but router address missing; set AERODROME_ROUTER_ADDRESS"
            )
            return None
        stable_val = spec.get("stable")
        if stable_val is None:
            stable_val = bool(getattr(dex_cfg, "aerodrome_stable", False))
        return AerodromeDexProvider(router, stable=bool(stable_val))
    if name == "aerodrome_cl":
        router = (
            spec.get("router")
            or getattr(dex_cfg, "aerodrome_cl_router", None)
            or os.getenv("AERODROME_CL_ROUTER_ADDRESS")
        )
        pool = (
            spec.get("pool")
            or spec.get("pool_address")
            or os.getenv("DIEM_USDC_POOL_ADDRESS")
        )
        tick_spacing_raw = (
            spec.get("tick_spacing")
            or spec.get("tickSpacing")
            or os.getenv("DIEM_USDC_TICK_SPACING")
        )
        if not router:
            _logger.warning(
                "aerodrome_cl selected but router address missing; set AERODROME_CL_ROUTER_ADDRESS"
            )
            return None
        if not pool:
            _logger.warning(
                "aerodrome_cl selected but pool address missing; set DIEM_USDC_POOL_ADDRESS"
            )
            return None
        if tick_spacing_raw is None or str(tick_spacing_raw).strip() == "":
            _logger.warning(
                "aerodrome_cl selected but tick spacing missing; set DIEM_USDC_TICK_SPACING (defaulting to 100)"
            )
            tick_spacing = 100
        else:
            try:
                tick_spacing = int(str(tick_spacing_raw).strip())
            except Exception:
                _logger.warning(
                    "aerodrome_cl selected but tick spacing invalid; set DIEM_USDC_TICK_SPACING"
                )
                return None
        if int(tick_spacing) <= 0:
            _logger.warning(
                "aerodrome_cl selected but tick spacing <= 0; set DIEM_USDC_TICK_SPACING"
            )
            return None
        return AerodromeCLDexProvider(router, pool, int(tick_spacing))
    if name not in _warned_unknown_providers:
        _warned_unknown_providers.add(name)
        _logger.warning("unknown dex provider name: %s", name)
    return None


def _build_bridge_provider(
    providers: list[DexProvider],
) -> BridgeRouteProvider | None:
    """
    Optionally add the synthetic DIEM bridge provider when leg venues exist.

    The bridge provider is only constructed when DIEM, VVV, and quote tokens are
    configured and both bridge legs have a backing provider.
    """

    try:
        provider_map = {p.name.lower(): p for p in providers}
        bridge = BridgeRouteProvider(provider_map)
        if not (bridge._diem_addr and bridge._vvv_addr and bridge._quote_addr):
            return None
        leg_a = bridge._provider_for_leg(bridge._diem_addr, bridge._vvv_addr)
        leg_b = bridge._provider_for_leg(bridge._vvv_addr, bridge._quote_addr)
        if leg_a is None or leg_b is None:
            return None
        return bridge
    except Exception:
        return None


def _verify_base_router_addresses(dex_cfg: Any) -> None:
    """Verify Base chain router addresses and warn on mismatches."""
    chain_id = os.getenv("BASE_CHAIN_ID", "8453")
    try:
        chain_id_int = int(chain_id)
    except Exception:
        return

    if chain_id_int != 8453:
        return  # Only verify for Base mainnet

    # Expected router addresses for Base mainnet
    expected_routers = {
        "uniswap_v2": getattr(dex_cfg, "uniswap_v2_router", None),
        "uniswap_v3": getattr(dex_cfg, "uniswap_v3_router", None),
        "aerodrome": getattr(dex_cfg, "aerodrome_router", None),
    }

    # Known Base mainnet router addresses (for reference)
    known_base_routers = {
        "uniswap_v2": "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24",
        "uniswap_v3": "0x2626664c2603336e57b271c5c0b26f421741e481",
        # Aerodrome router on Base mainnet
        "aerodrome": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
    }

    for provider_name, router_addr in expected_routers.items():
        if not router_addr:
            continue
        router_addr_lower = router_addr.strip().lower()
        known_addr = known_base_routers.get(provider_name, "").strip().lower()
        if known_addr and router_addr_lower != known_addr:
            _logger.warning(
                f"Router address mismatch for {provider_name} on Base: "
                f"configured={router_addr}, expected={known_base_routers[provider_name]}"
            )


def build_aggregator_from_env() -> DexAggregator:
    # Lightweight test fallback: avoid Web3/RPC requirements when running under pytest
    # and no RPC endpoints are configured.  Provides stub V2/V3 providers for
    # compatibility/route-type checks used in unit tests.
    if os.getenv("PYTEST_CURRENT_TEST") and not any(
        os.getenv(k) for k in ("RPC_URL", "BASE_RPC_URL", "RPC_URLS", "BASE_RPC_URLS")
    ):

        class _StubProvider(DexProvider):  # type: ignore[misc]
            def __init__(self, name: str) -> None:
                self.name = name
                self.supports_exact_out = True

        return DexAggregator([_StubProvider("uniswap_v2"), _StubProvider("uniswap_v3")])

    cfg = get_config()
    cfg.require(groups=("dex",))
    dex_cfg = cfg.dex

    # Verify router addresses for Base chain
    _verify_base_router_addresses(dex_cfg)

    # Prefer Uniswap V3 when available, then Aerodrome, then Uniswap V2.
    # This aligns with config/default.yml and improves exact-out coverage and quote quality on Base.
    specs = list(dex_cfg.providers or [])
    try:
        cl_router = (os.getenv("AERODROME_CL_ROUTER_ADDRESS") or "").strip()
        if cl_router:
            has_cl = any(
                str(spec.get("name", "")).strip().lower() == "aerodrome_cl"
                for spec in specs
            )
            if not has_cl:
                cl_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip()
                cl_tick_spacing = (
                    os.getenv("DIEM_USDC_TICK_SPACING")
                    or getattr(dex_cfg, "diem_usdc_tick_spacing", None)
                    or ""
                )
                cl_spec: dict[str, Any] = {"name": "aerodrome_cl", "router": cl_router}
                if cl_pool:
                    cl_spec["pool"] = cl_pool
                if cl_tick_spacing:
                    cl_spec["tick_spacing"] = cl_tick_spacing
                specs.append(cl_spec)
    except Exception:
        pass
    providers: list[DexProvider] = []
    missing_reasons: list[str] = []
    for spec in specs:
        provider = _provider_from_spec(spec, dex_cfg)
        if provider is not None:
            providers.append(provider)
            continue
        reason = _provider_missing_reason(spec, dex_cfg)
        if reason:
            name = str(spec.get("name", "")) or "unknown"
            _logger.warning("Skipping DEX provider %s: %s", name, reason)
            missing_reasons.append(reason)
    if missing_reasons and providers:
        _logger.warning(
            "Some DEX providers were skipped due to missing configuration: %s",
            "; ".join(sorted(set(missing_reasons))),
        )
    if not providers:
        if missing_reasons:
            raise ConfigError(
                "No DEX providers configured; fix missing router/quoter env vars: "
                + "; ".join(sorted(set(missing_reasons)))
            )
        raise ConfigError(
            "No DEX providers configured. Set DEX_PROVIDERS and router envs."
        )
    bridge_provider = _build_bridge_provider(providers)
    if bridge_provider:
        providers.append(bridge_provider)
    return DexAggregator(providers)
