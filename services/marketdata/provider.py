from __future__ import annotations

import json
import math
import os
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from threading import Lock, Thread
from typing import Any, Deque, Dict, List, Optional, Sequence, Tuple

import requests

from libs.dex.providers import build_aggregator_from_env
from libs.dex.routes import RouteLike, RoutePlan, as_route_plan, make_route

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # pragma: no cover - metrics optional
    def _metrics_inc(name: str, value: int = 1, labels: Dict[str, str] | None = None) -> None:
        return

try:
    from libs.telemetry.logger import get_logger
    _logger = get_logger('marketdata.provider')
except Exception:  # pragma: no cover - logging optional
    class _NullLogger:
        def info(self, *args, **kwargs):
            return

        def warning(self, *args, **kwargs):
            return

        def error(self, *args, **kwargs):
            return

        def debug(self, *args, **kwargs):
            return

    _logger = _NullLogger()


def _debug_sanity_enabled() -> bool:
    flag = os.getenv("MARKETDATA_DEBUG_SANITY")
    if flag is None:
        flag = os.getenv("DIEM_DEBUG_ROUTES")
    if flag is None:
        return False
    return str(flag).strip().lower() in {"1", "true", "yes", "on"}


def _latency_bucket(seconds: float) -> str:
    try:
        s = float(seconds)
    except Exception:
        s = 0.0
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


DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
DEFAULT_TOKEN_ADDRESSES = {
    "VVV": "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
    "DIEM": "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
}


class MarketDataProvider:
    """Market data via DEX aggregator + Venice endpoints.

    - Quotes/prices: uses configured DEX providers (UniswapV2, Aerodrome)
      via `libs.dex.providers` with decimals-aware conversions.
    - VVV metrics: fetched via explicit endpoints (circulating supply, utilization, staking_yield).
    - DIEM balance/quotas: fetched via rate-limits endpoint (`/api_keys/rate_limits`).
    """

    _price_cache: Dict[str, Tuple[float, float, float]] = {}
    _price_cache_lock = Lock()
    _warm_thread_lock = Lock()
    _warm_thread_started: bool = False
    _warm_thread_logged: bool = False
    _warm_symbols: Tuple[str, ...] = ()
    _warm_interval_seconds: float = 0.0
    _last_prices_latency: float = 0.0
    _last_prices_latency_ts: float = 0.0
    _external_price_cache: Dict[str, Tuple[float, float]] = {}
    _external_price_lock: Lock = Lock()
    _price_source_lock: Lock = Lock()
    _last_price_sources: Dict[str, Dict[str, Any]] = {}
    _price_clamp_lock: Lock = Lock()
    _price_clamp_events: Dict[str, Dict[str, Any]] = {}
    _util_samples_lock: Lock = Lock()
    _util_samples: Deque[Tuple[float, float]] = deque(maxlen=64)


    def _price_cache_key(self, symbol: str) -> str:
        return (symbol or "").upper()

    def _price_cache_capacity(self) -> int:
        try:
            return max(0, int(os.getenv("MARKETDATA_PRICE_CACHE_MAX_SYMBOLS") or "32"))
        except Exception:
            return 32

    def _price_cache_ttl(self) -> int:
        try:
            return max(0, int(os.getenv("MARKETDATA_PRICE_CACHE_TTL_SECONDS") or "60"))
        except Exception:
            return 60

    def _price_cache_ttl_for_symbol(self, symbol: str) -> int:
        base = self._price_cache_ttl()
        sym = (symbol or "").upper()
        if sym in {"DIEM", "VVV"}:
            try:
                return max(1, int(os.getenv("MARKETDATA_PRICE_CACHE_TTL_DIEM_SECONDS") or "30"))
            except Exception:
                return 30
        if sym == "USDC":
            try:
                return max(1, int(os.getenv("MARKETDATA_PRICE_CACHE_TTL_USDC_SECONDS") or "300"))
            except Exception:
                return 300
        return base

    def _price_cache_failure_ttl(self) -> int:
        try:
            return max(0, int(os.getenv("MARKETDATA_PRICE_CACHE_FAILURE_TTL_SECONDS") or "5"))
        except Exception:
            return 5

    def _cache_price_get(self, symbol: str) -> Optional[float]:
        key = self._price_cache_key(symbol)
        with self._price_cache_lock:
            entry = self._price_cache.get(key)
            if not entry:
                return None
            ts, value, ttl = entry
            if ttl <= 0 or (time.time() - ts) >= ttl:
                self._price_cache.pop(key, None)
                return None
            return float(value)

    def _cache_price_set(self, symbol: str, value: float) -> None:
        if self._valid_price(value):
            ttl = self._price_cache_ttl_for_symbol(symbol)
        else:
            ttl = self._price_cache_failure_ttl()
        if ttl <= 0:
            return
        key = self._price_cache_key(symbol)
        with self._price_cache_lock:
            capacity = self._price_cache_capacity()
            if capacity > 0 and len(self._price_cache) >= capacity:
                try:
                    oldest = min(self._price_cache.items(), key=lambda item: item[1][0])[0]
                    self._price_cache.pop(oldest, None)
                except ValueError:
                    self._price_cache.clear()
            self._price_cache[key] = (time.time(), float(value), float(ttl))

    def _stat_increment(self, key: str, delta: int = 1) -> None:
        stats = getattr(self, '_active_stats', None)
        if not stats:
            return
        with self._stats_lock:
            stats[key] = stats.get(key, 0) + delta



    def _external_price_ttl(self) -> float:
        try:
            raw = os.getenv("MARKETDATA_EXTERNAL_PRICE_TTL_SECONDS") or "30"
            ttl = float(raw)
            return ttl if ttl > 0 else 0.0
        except Exception:
            return 30.0

    def _price_sanity_threshold(self) -> float:
        try:
            raw_candidates = (
                os.getenv("MARKETDATA_SANITY_THRESHOLD"),
                os.getenv("MARKETDATA_PRICE_SANITY_MAX_DRIFT"),
                os.getenv("MARKETDATA_PRICE_SANITY_MAX_DRIFT_PCT"),
            )
            default_threshold = 0.15
            val: Optional[float] = None
            for raw in raw_candidates:
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    candidate = float(raw)
                except Exception:
                    continue
                val = candidate
                break
            if val is None:
                val = default_threshold
            if val > 1.0:
                val = val / 100.0
            if val < 0.0:
                return 0.0
            if val > 1.0:
                return 1.0
            return val
        except Exception:
            return 0.15

    def _external_price(self, symbol: str) -> Optional[float]:
        ttl = self._external_price_ttl()
        if ttl <= 0:
            return None
        key = self._price_cache_key(symbol)
        now = time.time()
        with self._external_price_lock:
            cached = self._external_price_cache.get(key)
            if cached and (now - cached[0]) < ttl:
                return cached[1]
        price = self._fetch_external_price(symbol)
        if self._valid_price(price):
            with self._external_price_lock:
                self._external_price_cache[key] = (now, float(price))
        return float(price) if self._valid_price(price) else None

    def _fetch_external_price(self, symbol: str) -> Optional[float]:
        address = self._address_for_symbol(symbol)
        if not address:
            return None
        try:
            response = requests.get(DEXSCREENER_TOKEN_URL.format(address=address), timeout=5)
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None
        return self._select_external_price(payload, address)

    def _select_external_price(self, payload: Dict[str, Any], address: str) -> Optional[float]:
        try:
            pairs = payload.get("pairs") or []
        except Exception:
            return None
        if not isinstance(pairs, list):
            return None
        try:
            quote_pref = (self._quote_token_address() or "").lower()
        except Exception:
            quote_pref = ""
        best_price: Optional[float] = None
        best_score: Optional[Tuple[int, float]] = None
        address_norm = address.lower()
        for pair in pairs:
            try:
                base = str(((pair or {}).get("baseToken") or {}).get("address") or "").lower()
                if base != address_norm:
                    continue
                price_raw = (pair or {}).get("priceUsd")
                if price_raw is None:
                    continue
                price = float(price_raw)
                if price <= 0:
                    continue
                liquidity = float(((pair or {}).get("liquidity") or {}).get("usd") or 0.0)
                quote = str(((pair or {}).get("quoteToken") or {}).get("address") or "").lower()
                priority = 1 if quote_pref and quote == quote_pref else 0
                score = (priority, liquidity)
                if best_score is None or score > best_score:
                    best_score = score
                    best_price = price
            except Exception:
                continue
        return best_price

    @staticmethod
    def _format_path(path: Any) -> Optional[str]:  # noqa: ANN401
        if isinstance(path, (list, tuple)):
            tokens = [str(p).strip() for p in path if p]
            if tokens:
                return "->".join(tokens)
        if isinstance(path, str) and path.strip():
            return path.strip()
        return None

    def _price_breakdown(self, symbol: str) -> Optional[Dict[str, Any]]:
        detail = type(self)._get_price_source(symbol)
        if not detail:
            return None
        provider = detail.get("provider")
        path = detail.get("path")
        if provider is None and path is None:
            return None
        breakdown = {
            "provider": provider,
            "path": path,
            "source": detail.get("source"),
        }
        return breakdown

    def _apply_price_sanity(self, symbol: str, price: Optional[float]) -> float:
        label = self._norm_symbol_label(symbol)
        stats = getattr(self, '_active_stats', None)

        def _store_event(reason: str, diff_val: Optional[float], threshold_val: Optional[float]) -> dict[str, float | str | None]:
            event = {
                "symbol": label,
                "reason": reason,
                "internal_price": float(price) if price is not None else None,
                "external_price": float(ext_price),
                "diff": diff_val,
                "threshold": threshold_val,
                "ts": time.time(),
            }
            if isinstance(stats, dict):
                stats.setdefault("price_sanity_events", []).append(event)
            return event

        try:
            ext_price = self._external_price(symbol)
        except Exception:
            ext_price = None
        if ext_price is None or ext_price <= 0:
            return float(price or 0.0)
        if not self._valid_price(price):
            self._record_counter("marketdata_price_sanity_total", {"symbol": label, "outcome": "external_replace", "reason": "invalid_internal"})
            evt = _store_event("invalid_internal", None, None)
            _logger.warning("price sanity: replacing invalid internal price symbol=%s internal=%s external=%s", label, price, ext_price)
            breakdown = self._price_breakdown(label)
            if breakdown:
                path_str = self._format_path(breakdown.get("path"))
                try:
                    _logger.info(
                        "price sanity breakdown symbol=%s provider=%s path=%s source=%s",
                        label,
                        breakdown.get("provider"),
                        path_str,
                        breakdown.get("source"),
                    )
                except Exception:
                    pass
                evt["price_source"] = breakdown
            if _debug_sanity_enabled():
                _logger.info("price sanity debug replace invalid symbol=%s event=%s", label, evt)
            type(self)._record_price_clamp(
                label,
                "invalid_internal",
                {
                    "external_price": float(ext_price),
                    "internal_price": float(price) if price is not None else None,
                },
            )
            type(self)._record_price_source(label, "external_invalid", {"valid": True})
            return float(ext_price)
        threshold = self._price_sanity_threshold()
        diff = abs(float(price) - float(ext_price)) / float(ext_price)
        if diff > threshold:
            self._record_counter("marketdata_price_sanity_total", {"symbol": label, "outcome": "clamped", "reason": "drift"})
            evt = _store_event("drift", diff, threshold)
            _logger.warning("price sanity: clamp applied symbol=%s internal=%s external=%s diff=%.6f threshold=%.6f", label, price, ext_price, diff, threshold)
            breakdown = self._price_breakdown(label)
            if breakdown:
                path_str = self._format_path(breakdown.get("path"))
                try:
                    _logger.info(
                        "price sanity breakdown symbol=%s provider=%s path=%s source=%s diff=%.6f",
                        label,
                        breakdown.get("provider"),
                        path_str,
                        breakdown.get("source"),
                        diff,
                    )
                except Exception:
                    pass
                evt["price_source"] = breakdown
            if _debug_sanity_enabled():
                _logger.info("price sanity debug clamp symbol=%s event=%s", label, evt)
            type(self)._record_price_clamp(
                label,
                "drift",
                {
                    "diff": float(diff),
                    "threshold": float(threshold),
                    "external_price": float(ext_price),
                    "internal_price": float(price),
                },
            )
            type(self)._record_price_source(label, "external_clamp", {"valid": True})
            return float(ext_price)
        return float(price)

    def _validate_trade_paths(self) -> None:
        try:
            routes = self._collect_trade_paths()
        except Exception:
            _logger.warning("failed to load TRADE_PATH for validation", exc_info=True)
            return
        if not routes:
            return
        for route in routes:
            try:
                adjusted_plan = route if isinstance(route, RoutePlan) else as_route_plan(route)
            except Exception:
                tokens = getattr(route, "tokens", route)
                _logger.warning("trade path invalid", extra={"path": list(tokens) if isinstance(tokens, (list, tuple)) else tokens}, exc_info=True)
                continue
            try:
                if adjusted_plan.is_uniswap_v3():
                    adjusted_plan.ensure_v3()
            except Exception:
                _logger.error("trade path missing fee tiers for Uniswap V3", extra={"path": list(adjusted_plan.tokens)}, exc_info=True)
            try:
                self._warm_route_liquidity(adjusted_plan.tokens)
            except Exception:
                _logger.debug("trade path warm attempt failed", exc_info=True)
            try:
                report = self.discover_trade_path(list(adjusted_plan.tokens))
            except Exception:
                _logger.debug("trade path verification failed", extra={"path": list(adjusted_plan.tokens)}, exc_info=True)
            else:
                if isinstance(report, dict):
                    hops = report.get("hops") or []
                    pairs = []
                    for hop in hops:
                        uv2 = (hop or {}).get("uniswap_v2") or {}
                        pair_addr = uv2.get("pair")
                        if pair_addr:
                            pairs.append(str(pair_addr))
                    if pairs:
                        _logger.info("trade path verified", extra={"path": list(adjusted_plan.tokens), "pairs": pairs})
                    else:
                        _logger.warning("trade path verification empty", extra={"path": list(adjusted_plan.tokens)})
                else:
                    _logger.warning("trade path verification empty", extra={"path": list(adjusted_plan.tokens)})

    def _check_wbtc_configuration(self) -> None:
        try:
            token = (self._address_for_symbol("WBTC") or "").strip()
        except Exception:
            token = ""
        if not token:
            return
        override = self._route_optional_from_env("WBTC_PRICE_PATH") or self._route_optional_from_env("WBTC_TRADE_PATH")
        if override:
            try:
                self._warm_route_liquidity(override.tokens)
            except Exception:
                _logger.debug("failed to warm WBTC override path", exc_info=True)
            return
        _logger.warning("WBTC_TOKEN_ADDRESS configured without WBTC_PRICE_PATH; defaulting to WBTC->WETH->QUOTE routing")
        try:
            tokens = [token, self._weth_address(), self._quote_token_address()]
            self._warm_route_liquidity(tokens)
        except Exception:
            _logger.debug("failed to warm default WBTC route", exc_info=True)

    def _warm_route_liquidity(self, tokens: Sequence[str]) -> None:
        if not tokens or len(tokens) < 2:
            return
        from services.marketdata.etherscan_verify import warm_cache_for_path

        warm_cache_for_path(list(tokens))

    def __init__(self) -> None:
        self._stats_lock = Lock()
        self._active_stats: Optional[Dict[str, Any]] = None
        self._last_prices_stats: Dict[str, Any] = {}
        self._ensure_warm_thread()
        self._validate_trade_paths()
        self._check_wbtc_configuration()

    def _norm_symbol_label(self, symbol: object) -> str:
        try:
            raw = str(symbol).strip()
        except Exception:
            raw = ""
        if not raw:
            return "UNKNOWN"
        sym = raw.upper()
        if sym.startswith("0X") and len(sym) == 42:
            mapping: Dict[str, str] = {}
            try:
                diem = self._address_for_symbol("DIEM")
                if diem:
                    mapping[str(diem).strip().upper()] = "DIEM"
            except Exception:
                pass
            try:
                vvv = self._address_for_symbol("VVV")
                if vvv:
                    mapping[str(vvv).strip().upper()] = "VVV"
            except Exception:
                pass
            try:
                quote_addr = self._quote_token_address()
                if quote_addr:
                    quote_symbol = (os.getenv("QUOTE_TOKEN_SYMBOL") or "QUOTE").upper()
                    mapping[str(quote_addr).strip().upper()] = quote_symbol
            except Exception:
                pass
            try:
                mapping[self._weth_address().strip().upper()] = "WETH"
            except Exception:
                pass
            mapped = mapping.get(sym)
            if mapped:
                return mapped
        return sym or "UNKNOWN"

    def _record_counter(self, name: str, labels: Dict[str, str]) -> None:
        try:
            clean = {str(k): str(v) for k, v in (labels or {}).items()}
            _metrics_inc(name, labels=clean)
        except Exception:
            return

    def _record_latency(self, symbol: object, stage: str, seconds: float, outcome: str) -> None:
        try:
            sym = self._norm_symbol_label(symbol)
            bucket = _latency_bucket(seconds)
            labels = {
                "symbol": sym,
                "stage": str(stage or "").lower(),
                "bucket": bucket,
                "outcome": str(outcome or "").lower() or "unknown",
            }
            _metrics_inc("marketdata_latency_bucket_total", labels=labels)
        except Exception:
            return


    @classmethod
    def _record_price_source(cls, symbol: str, source: str, detail: Optional[Dict[str, Any]] = None) -> None:
        sym = str(symbol or "").strip()
        if not sym:
            return
        key = sym.upper()
        payload: Dict[str, Any] = {"source": str(source or "").strip(), "ts": time.time()}
        if detail:
            for k, v in detail.items():
                if v is not None:
                    if isinstance(v, (list, tuple)):
                        payload[k] = [str(item) for item in v]
                    else:
                        payload[k] = v
        with cls._price_source_lock:
            cls._last_price_sources[key] = payload

    @classmethod
    def _get_price_source(cls, symbol: str) -> Dict[str, Any]:
        sym = str(symbol or "").strip()
        if not sym:
            return {}
        key = sym.upper()
        with cls._price_source_lock:
            data = dict(cls._last_price_sources.get(key) or {})
        return data

    @classmethod
    def _record_price_clamp(cls, symbol: str, reason: str, detail: Optional[Dict[str, Any]] = None) -> None:
        sym = str(symbol or "").strip()
        if not sym:
            return
        key = sym.upper()
        with cls._price_clamp_lock:
            payload: Dict[str, Any] = {"ts": time.time(), "reason": str(reason or "")}
            if detail:
                for k, v in detail.items():
                    if v is not None:
                        payload[k] = v
            cls._price_clamp_events[key] = payload

    def _util_sample_interval(self) -> float:
        try:
            raw = os.getenv("MARKETDATA_WATCHER_INTERVAL") or os.getenv("MARKETDATA_UTIL_SAMPLE_INTERVAL_SECONDS") or "180"
            interval = float(raw)
            if interval <= 0:
                return 0.0
            return interval
        except Exception:
            return 180.0

    def _util_sample_ttl(self) -> float:
        try:
            raw = os.getenv("MARKETDATA_UTIL_SAMPLE_TTL")
            if raw is None or str(raw).strip() == "":
                return max(1800.0, self._util_sample_interval() * 6.0)
            ttl = float(raw)
            return ttl if ttl > 0 else max(1800.0, self._util_sample_interval() * 6.0)
        except Exception:
            return max(1800.0, self._util_sample_interval() * 6.0)

    def _record_utilization_sample(self, value: Optional[float]) -> None:
        try:
            if value is None:
                return
            val = float(value)
            if not math.isfinite(val) or val < 0:
                return
        except Exception:
            return
        now = time.time()
        interval = self._util_sample_interval()
        ttl = self._util_sample_ttl()
        with type(self)._util_samples_lock:
            samples = type(self)._util_samples
            if samples and interval > 0 and (now - samples[-1][0]) < interval:
                samples[-1] = (now, val)
            else:
                samples.append((now, val))
            if ttl > 0:
                while samples and (now - samples[0][0]) > ttl:
                    samples.popleft()

    def utilization_volatility_bps(self, window: int = 3) -> Optional[float]:
        if window <= 1:
            window = 2
        with type(self)._util_samples_lock:
            data = list(type(self)._util_samples)
        if len(data) < window or len(data) < 2:
            return None
        subset = [float(v) for _, v in data[-window:]]
        try:
            mean = sum(subset) / float(len(subset))
            if mean <= 0:
                return None
            variance = 0.0
            if len(subset) > 1:
                variance = sum((x - mean) ** 2 for x in subset) / float(len(subset) - 1)
            if variance <= 0:
                return 0.0
            stddev = variance ** 0.5
            return float((stddev / mean) * 10_000.0)
        except Exception:
            return None

    def price_health(self, symbol: str, max_age: float = 120.0) -> Dict[str, Any]:
        sym = str(symbol or "").strip()
        key = sym.upper()
        now = time.time()
        with type(self)._price_source_lock:
            source_info = dict(type(self)._last_price_sources.get(key) or {})
        with type(self)._price_clamp_lock:
            clamp_info = dict(type(self)._price_clamp_events.get(key) or {})
        ts = source_info.get("ts")
        age = None
        if isinstance(ts, (int, float)):
            age = max(0.0, now - float(ts))
        source_label = source_info.get("source")
        if not isinstance(source_label, str) or not source_label.strip():
            source_label = "unknown"
        else:
            source_label = source_label.strip()
        provider = source_info.get("provider")
        path = source_info.get("path")
        valid = source_info.get("valid")
        stale = age is not None and age > max_age
        clamp_ts = clamp_info.get("ts")
        clamp_age = None
        clamped = False
        if isinstance(clamp_ts, (int, float)):
            clamp_age = max(0.0, now - float(clamp_ts))
            clamped = clamp_age <= max_age
        clamp_reason = clamp_info.get("reason")
        valid_flag: bool
        if isinstance(valid, bool):
            valid_flag = valid
        elif valid is not None:
            try:
                valid_flag = bool(valid)
            except Exception:
                valid_flag = False
        else:
            valid_flag = False
        diff_val = clamp_info.get("diff")
        threshold_val = clamp_info.get("threshold")
        external_px = clamp_info.get("external_price")
        internal_px = clamp_info.get("internal_price")
        return {
            "symbol": key or sym,
            "source": source_label,
            "age": age,
            "stale": stale,
            "valid": valid_flag,
            "clamped": clamped,
            "clamp_reason": clamp_reason,
            "clamp_age": clamp_age,
            "provider": provider,
            "path": path,
            "diff": diff_val,
            "threshold": threshold_val,
            "external_price": external_px,
            "internal_price": internal_px,
        }
    def _mark_last_latency(self, latency: float) -> None:
        cls = type(self)
        try:
            cls._last_prices_latency = float(latency)
        except Exception:
            cls._last_prices_latency = 0.0
        cls._last_prices_latency_ts = time.time()

    def last_prices_latency(self) -> Tuple[float, float]:
        cls = type(self)
        return float(cls._last_prices_latency), float(cls._last_prices_latency_ts)

    def last_prices_stats(self) -> Dict[str, Any]:
        return dict(getattr(self, '_last_prices_stats', {}))

    def _ensure_warm_thread(self) -> None:
        cls = type(self)
        env_symbols = os.getenv("MARKETDATA_WARM_SYMBOLS", "")
        symbols = tuple(sorted({s.strip().upper() for s in env_symbols.split(",") if s.strip()}))
        if not symbols:
            return
        interval_val = os.getenv("MARKETDATA_WARM_INTERVAL_SECONDS")
        try:
            interval = float(interval_val) if interval_val not in (None, "") else 30.0
        except Exception:
            interval = 30.0
        if interval < 0:
            interval = 0.0
        with cls._warm_thread_lock:
            if cls._warm_thread_started:
                return
            cls._warm_thread_started = True
            cls._warm_symbols = symbols
            cls._warm_interval_seconds = interval
            weak_self = weakref.ref(self)
            def _runner() -> None:
                inst = weak_self()
                if inst is None:
                    try:
                        inst = MarketDataProvider()
                    except Exception:
                        return
                try:
                    inst._warm_loop()
                except Exception:
                    _logger.warning("marketdata warm loop failed", exc_info=True)
            thread = Thread(target=_runner, name="marketdata-warm-cache", daemon=True)
            thread.start()
            if not cls._warm_thread_logged:
                try:
                    _logger.info("marketdata warm cache thread started", extra={"symbols": list(symbols), "interval": interval})
                except Exception:
                    pass
                cls._warm_thread_logged = True

    def _warm_loop(self) -> None:
        cls = type(self)
        symbols = cls._warm_symbols
        if not symbols:
            return
        interval = float(cls._warm_interval_seconds or 0.0)
        while True:
            for sym in symbols:
                start = time.perf_counter()
                outcome = "ok"
                try:
                    price = self._price_for_symbol(sym)
                    self._cache_price_set(sym, price)
                    if not self._valid_price(price):
                        outcome = "invalid"
                except Exception:
                    outcome = "error"
                finally:
                    elapsed = time.perf_counter() - start
                    self._record_counter("marketdata_warm_total", {"symbol": self._norm_symbol_label(sym), "outcome": outcome})
                    self._record_latency(sym, "warm", elapsed, outcome)
            if interval <= 0:
                break
            time.sleep(interval)

    def _erc20_decimals(self, address: str) -> int:
        from web3 import Web3  # lazy load
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        erc20 = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
        return int(erc20.functions.decimals().call())

    @staticmethod
    def _parse_route_spec(raw: str) -> RoutePlan:
        spec = raw.strip()
        if not spec:
            raise ValueError("route specification must be non-empty")
        if spec[0] in "[{":
            data = json.loads(spec)
            if isinstance(data, dict):
                tokens = data.get("tokens") or data.get("path")
                fees = data.get("fees") or data.get("fee_tiers")
                if not isinstance(tokens, Sequence):
                    raise ValueError("route JSON must include 'tokens' array")
                if fees is not None and not isinstance(fees, Sequence):
                    raise ValueError("route JSON 'fees' must be an array when provided")
                return make_route(list(tokens), list(fees) if fees is not None else None)
            if isinstance(data, list):
                tokens = []
                fees: List[Optional[int]] = []
                for idx, item in enumerate(data):
                    if isinstance(item, dict):
                        addr = item.get("token") or item.get("address")
                        fee = item.get("fee")
                        if not addr:
                            raise ValueError("route hop missing token address")
                        tokens.append(str(addr))
                        if idx < len(data) - 1:
                            fees.append(int(fee) if fee is not None else None)
                    else:
                        tokens.append(str(item))
                        if idx < len(data) - 1:
                            fees.append(None)
                return make_route(tokens, fees)
            raise ValueError("unsupported route JSON format")
        parts = [p.strip() for p in spec.split(",") if p.strip()]
        tokens: List[str] = []
        fees: List[Optional[int]] = []
        for idx, part in enumerate(parts):
            if "@" in part:
                addr, fee_str = part.split("@", 1)
                tokens.append(addr.strip())
                if idx < len(parts) - 1:
                    fees.append(int(fee_str.strip()))
            else:
                tokens.append(part)
                if idx < len(parts) - 1:
                    fees.append(None)
        if len(tokens) < 2:
            raise ValueError("route must include at least two addresses")
        return make_route(tokens, fees)

    @staticmethod
    def _coerce_route_entry(entry: Any) -> Optional[str]:  # noqa: ANN401
        if entry is None:
            return None
        if isinstance(entry, str):
            val = entry.strip()
            return val or None
        if isinstance(entry, dict):
            for key in ("value", "path", "route", "trade_path"):
                val = entry.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return None

    def _route_from_env(self, key: str = "TRADE_PATH") -> RoutePlan:
        if key == "TRADE_PATH" and os.getenv("TRADE_PATHS"):
            paths = self._collect_trade_paths()
            if paths:
                return paths[0]
        path_env = os.getenv(key)
        if not path_env:
            raise EnvironmentError(f"{key} must be set for pricing routes")
        return self._parse_route_spec(path_env)

    def _route_optional_from_env(self, key: str) -> Optional[RoutePlan]:
        raw = os.getenv(key)
        if not raw:
            return None
        try:
            return self._parse_route_spec(raw)
        except Exception:
            return None

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
            value = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip() or None
            return value or DEFAULT_TOKEN_ADDRESSES.get("DIEM")
        if s == "VVV":
            value = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip() or None
            return value or DEFAULT_TOKEN_ADDRESSES.get("VVV")
        if s in {"ETH", "WETH"}:
            try:
                return self._weth_address()
            except Exception:
                return None
        if s == "USDC":
            value = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip() or None
            return value or DEFAULT_TOKEN_ADDRESSES.get("USDC")
        if s == "WBTC":
            return (
                os.getenv("WBTC_TOKEN_ADDRESS")
                or os.getenv("BTC_TOKEN_ADDRESS")
                or os.getenv("CBBTC_TOKEN_ADDRESS")
                or ""
            ).strip() or None
        return None

    @staticmethod
    def _valid_price(value: Optional[float]) -> bool:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        return 1e-6 < v < 1e6

    def _collect_trade_paths(self) -> List[RoutePlan]:
        paths: List[RoutePlan] = []

        raw_paths = os.getenv("TRADE_PATHS")
        if raw_paths:
            try:
                parsed = json.loads(raw_paths)
                if isinstance(parsed, list):
                    for entry in parsed:
                        spec = self._coerce_route_entry(entry)
                        if not spec:
                            continue
                        try:
                            route = self._parse_route_spec(spec)
                            paths.append(route)
                        except Exception:
                            _logger.warning("trade path entry invalid", exc_info=True)
                else:
                    _logger.warning("TRADE_PATHS must be a JSON array")
            except Exception:
                _logger.warning("failed to parse TRADE_PATHS JSON", exc_info=True)

        if not paths:
            for key in ("TRADE_PATH", "TRADE_PATH_2"):
                raw = os.getenv(key)
                if not raw:
                    continue
                try:
                    paths.append(self._parse_route_spec(raw))
                except Exception:
                    _logger.warning("invalid %s env route", key, exc_info=True)
                    continue
        return paths

    def _try_path_direct(self, route: RoutePlan) -> Optional[Dict[str, Any]]:
        try:
            self._stat_increment("dex_calls")
            bp = self.best_price(route, amount_in_decimal=1.0)
            if not bp:
                return None
            price = float(bp.get("price") or 0.0)
            return bp if self._valid_price(price) else None
        except Exception:
            return None

    def _hop_price(self, token_in: str, token_out: str, *, allow_inverse: bool = True) -> Optional[float]:
        token_in = (token_in or "").strip()
        token_out = (token_out or "").strip()
        if not token_in or not token_out:
            return None
        if token_in.lower() == token_out.lower():
            return 1.0

        attempts: List[Optional[float]] = []
        # Direct aggregator quote (best effort)
        try:
            self._stat_increment("dex_calls")
            bp = self.best_price(make_route([token_in, token_out]), amount_in_decimal=1.0)
            attempts.append(float(bp.get("price") or 0.0))
        except Exception:
            attempts.append(None)

        # Scan smaller sizes when pools are thin
        try:
            attempts.append(self._best_price_scan(make_route([token_in, token_out]), start=1.0, min_amount=1e-12, factor=10.0))
        except Exception:
            attempts.append(None)

        # Mid-price from reserves
        attempts.append(self._mid_price_from_reserves(token_in, token_out))

        for price in attempts:
            if self._valid_price(price):
                return float(price)

        # Bridge via WETH when direct liquidity is missing
        bridge = self._weth_address()
        if bridge and bridge.lower() not in {token_in.lower(), token_out.lower()}:
            try:
                self._stat_increment("dex_calls")
                bp_bridge = self.best_price(make_route([token_in, bridge, token_out]), amount_in_decimal=1.0)
                price_bridge = float(bp_bridge.get("price") or 0.0)
                if self._valid_price(price_bridge):
                    return price_bridge
            except Exception:
                pass
            first = self._hop_price(token_in, bridge, allow_inverse=False)
            second = self._hop_price(bridge, token_out, allow_inverse=False) if self._valid_price(first) else None
            if self._valid_price(first) and self._valid_price(second):
                return float(first) * float(second)

        if allow_inverse:
            inv = self._hop_price(token_out, token_in, allow_inverse=False)
            if self._valid_price(inv):
                try:
                    return 1.0 / float(inv)
                except ZeroDivisionError:
                    return None
        return None

    def _price_via_segments(self, route: RoutePlan) -> Optional[float]:
        tokens = route.tokens
        if len(tokens) < 2:
            return None
        total = 1.0
        for a, b in zip(tokens, tokens[1:]):
            hop = self._hop_price(a, b)
            if not self._valid_price(hop):
                return None
            total *= float(hop)
        return total

    def quote_all(self, amount_in: int, path: List[str]) -> List[Any]:
        """Return quotes across all configured providers for path.

        amount_in is in smallest units of the input token.
        """
        from libs.dex.providers import build_aggregator_from_env

        agg = build_aggregator_from_env()
        return agg.quote_all(amount_in, path)

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:  # noqa: ANN401
        try:
            if isinstance(value, (int, float)):
                return float(value)
            if isinstance(value, str) and value.strip():
                return float(value.strip())
        except Exception:
            return None
        return None

    def _deep_find(self, obj: Any, keys: List[str]) -> Optional[Any]:
        stack: List[Any] = [obj]
        seen = set()
        while stack:
            item = stack.pop()
            if id(item) in seen:
                continue
            seen.add(id(item))
            if isinstance(item, dict):
                for k, v in item.items():
                    if k in keys:
                        return v
                    stack.append(v)
            elif isinstance(item, list):
                stack.extend(item)
        return None

    def _diem_svvv_decimals(self) -> tuple[int, int]:
        try:
            diem_dec = int(os.getenv("DIEM_DECIMALS") or 0)
        except Exception:
            diem_dec = 0
        try:
            svvv_dec = int(os.getenv("SVVV_DECIMALS") or os.getenv("VVV_DECIMALS") or 0)
        except Exception:
            svvv_dec = 0
        if diem_dec <= 0:
            try:
                diem_addr = self._address_for_symbol("DIEM")
                if diem_addr:
                    diem_dec = self._erc20_decimals(diem_addr)
            except Exception:
                diem_dec = 18
        if diem_dec <= 0:
            diem_dec = 18
        if svvv_dec <= 0:
            try:
                vvv_addr = self._address_for_symbol("VVV")
                if vvv_addr:
                    svvv_dec = self._erc20_decimals(vvv_addr)
            except Exception:
                svvv_dec = 18
        if svvv_dec <= 0:
            svvv_dec = 18
        return int(diem_dec), int(svvv_dec)

    def _ratio_units_to_tokens(self, units: int, diem_decimals: int, svvv_decimals: int) -> float:
        return float(units) * (10 ** diem_decimals) / float(10 ** svvv_decimals)

    def best_price(self, route: RouteLike, amount_in_decimal: float = 1.0, *, label_symbol: Optional[str] = None) -> Dict[str, Any]:
        """Compute best price for the supplied route with telemetry instrumentation."""

        plan = as_route_plan(route)
        tokens = plan.tokens
        if len(tokens) < 2:
            raise ValueError("route must include at least [token_in, token_out]")
        dec_in = self._erc20_decimals(tokens[0])
        dec_out = self._erc20_decimals(tokens[-1])
        amount_in_units = int(amount_in_decimal * (10 ** dec_in))
        label = self._norm_symbol_label(label_symbol or (tokens[0] if tokens else None))

        agg = build_aggregator_from_env()
        supports_reserve = any(getattr(p, "supports_reserve_math", False) for p in getattr(agg, "providers", []))

        def _record(stage: str, elapsed: float, outcome: str) -> None:
            self._record_latency(label, stage, elapsed, outcome)
            self._record_counter("marketdata_price_source_total", {"symbol": label, "source": stage, "outcome": outcome})

        quote = None
        used_route = plan

        start = time.perf_counter()
        try:
            quote = agg.best_quote(amount_in_units, plan)
        except Exception:
            elapsed = time.perf_counter() - start
            _record("dex_primary", elapsed, "error")
            raise
        else:
            elapsed = time.perf_counter() - start
            outcome = "ok" if quote is not None else "empty"
            _record("dex_primary", elapsed, outcome)
            if quote is not None:
                used_route = quote.route
                self._record_counter("marketdata_price_provider_total", {"symbol": label, "provider": str(quote.provider)})

        if quote is None and len(tokens) == 2:
            bridge = self._bridge_token_address()
            if bridge and bridge.lower() not in {tokens[0].lower(), tokens[-1].lower()}:
                alt_route = make_route([tokens[0], bridge, tokens[-1]])
                start_bridge = time.perf_counter()
                try:
                    quote = agg.best_quote(amount_in_units, alt_route)
                except Exception:
                    elapsed = time.perf_counter() - start_bridge
                    _record("dex_bridge", elapsed, "error")
                    quote = None
                else:
                    elapsed = time.perf_counter() - start_bridge
                    outcome = "ok" if quote is not None else "empty"
                    _record("dex_bridge", elapsed, outcome)
                    if quote is not None:
                        used_route = quote.route
                        self._record_counter("marketdata_price_provider_total", {"symbol": label, "provider": str(quote.provider)})

        if quote is not None:
            used_route = quote.route
            price = (quote.amount_out / (10 ** dec_out)) / (quote.amount_in / (10 ** dec_in))
            return {
                "provider": quote.provider,
                "amount_in": quote.amount_in,
                "amount_out": quote.amount_out,
                "decimals": {"in": dec_in, "out": dec_out},
                "price": price,
                "path": used_route.tokens,
            }

        if supports_reserve:
            start_approx = time.perf_counter()
            approx = self.approx_exec_price(amount_in_units, plan)
            elapsed = time.perf_counter() - start_approx
            outcome = "ok" if approx and approx > 0 else "empty"
            _record("approx", elapsed, outcome)
            if approx and approx > 0:
                price = float(approx)
                if dec_out >= dec_in:
                    amount_out_units = int(price * amount_in_units * (10 ** (dec_out - dec_in)))
                else:
                    amount_out_units = int(price * amount_in_units / (10 ** (dec_in - dec_out)))
                return {
                    "provider": "approx",
                    "amount_in": amount_in_units,
                    "amount_out": amount_out_units,
                    "decimals": {"in": dec_in, "out": dec_out},
                    "price": price,
                    "path": plan.tokens,
                }

        # As a final fallback, try composable hop pricing for multi-hop routes
        seg_price = self._price_via_segments(plan)
        if self._valid_price(seg_price):
            price = float(seg_price)
            if dec_out >= dec_in:
                amount_out_units = int(price * amount_in_units * (10 ** (dec_out - dec_in)))
            else:
                amount_out_units = int(price * amount_in_units / (10 ** (dec_in - dec_out)))
            _record("segments", 0.0, "ok")
            return {
                "provider": "segments",
                "amount_in": amount_in_units,
                "amount_out": amount_out_units,
                "decimals": {"in": dec_in, "out": dec_out},
                "price": price,
                "path": plan.tokens,
            }

        _record("dex_final", 0.0, "error")
        raise RuntimeError("No quotes available for provided route")

    def _best_price_scan(
        self,
        route: RouteLike,
        start: float = 1.0,
        min_amount: float = 1e-12,
        factor: float = 10.0,
    ) -> Optional[float]:
        """Scan progressively smaller input amounts until a quote is found; return price.

        Uses router quotes only (no reserve math) to avoid dependence on external explorers.
        """
        plan = as_route_plan(route)
        tokens = plan.tokens
        if len(tokens) < 2:
            return None
        try:
            dec_in = self._erc20_decimals(tokens[0])
            dec_out = self._erc20_decimals(tokens[-1])
        except Exception:
            return None
        amt = float(start)
        if factor <= 1.0:
            factor = 10.0
        agg = build_aggregator_from_env()
        while amt >= float(min_amount):
            try:
                amount_in_units = int(amt * (10 ** dec_in))
                q = agg.best_quote(amount_in_units, plan)
                if q is not None and q.amount_in > 0 and q.amount_out > 0:
                    price = (q.amount_out / (10 ** dec_out)) / (q.amount_in / (10 ** dec_in))
                    if price > 0:
                        return float(price)
            except Exception:
                pass
            amt = amt / factor
        return None

    # --- Constant-product AMM fallback (UniswapV2-style) ---
    def _uni_v2_out(self, amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
        try:
            if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
                return 0
            fee_n = 10_000 - int(fee_bps)
            amount_in_with_fee = int(amount_in) * fee_n // 10_000
            num = int(reserve_out) * int(amount_in_with_fee)
            den = int(reserve_in) + int(amount_in_with_fee)
            if den <= 0:
                return 0
            return int(num // den)
        except Exception:
            return 0

    def approx_quote_exact_in(self, amount_in: int, route: RoutePlan, fee_bps_per_hop: int = 30) -> Optional[int]:
        """Approximate multi-hop exact-in output using UniswapV2 constant-product math.

        Uses Etherscan v2 discovery/cache for UniswapV2 reserves and token mapping.
        Returns None when reserves are unavailable for any hop.
        """
        try:
            route.ensure_v2()
            tokens = route.tokens
            if len(tokens) < 2 or amount_in <= 0:
                return None
            try:
                from services.marketdata.etherscan_verify import (
                    get_cached_pair_info_for_tokens,
                    verify_trade_path,
                )
            except Exception:
                return None
            amt = int(amount_in)
            for i in range(len(tokens) - 1):
                a = str(tokens[i])
                b = str(tokens[i + 1])
                info = get_cached_pair_info_for_tokens(a, b)
                if not info:
                    # Try to warm cache for this hop
                    try:
                        _ = verify_trade_path([a, b])
                        info = get_cached_pair_info_for_tokens(a, b)
                    except Exception:
                        info = None
                if not info:
                    return None
                reserves = info.get("reserves") if isinstance(info, dict) else None
                t0 = (info.get("token0") or "") if isinstance(info, dict) else ""
                t1 = (info.get("token1") or "") if isinstance(info, dict) else ""
                if not isinstance(reserves, tuple) or len(reserves) < 2 or not t0 or not t1:
                    return None
                def _n(x: str) -> str:
                    return ("0x" + str(x).lower().removeprefix("0x"))
                t0n, t1n = _n(t0), _n(t1)
                ain, aout = _n(a), _n(b)
                # Map reserves in path direction
                if ain == t0n and aout == t1n:
                    rin, rout = int(reserves[0]), int(reserves[1])
                elif ain == t1n and aout == t0n:
                    rin, rout = int(reserves[1]), int(reserves[0])
                else:
                    return None
                out_i = self._uni_v2_out(amt, rin, rout, fee_bps=int(fee_bps_per_hop))
                if out_i <= 0:
                    return None
                amt = int(out_i)
            return int(amt)
        except Exception:
            return None

    def approx_exec_price(self, amount_in: int, route: RoutePlan, fee_bps_per_hop: int = 30) -> Optional[float]:
        """Return approximate execution price (out/in) using AMM fallback for given input units.

        Price is normalized to token decimals along the path ends (path[0] -> path[-1]).
        """
        try:
            route.ensure_v2()
            tokens = route.tokens
            if len(tokens) < 2 or amount_in <= 0:
                return None
            out_units = self.approx_quote_exact_in(amount_in, route, fee_bps_per_hop=fee_bps_per_hop)
            if out_units is None or out_units <= 0:
                return None
            dec_in = self._erc20_decimals(tokens[0])
            dec_out = self._erc20_decimals(tokens[-1])
            amt_in = float(amount_in) / float(10 ** int(dec_in))
            amt_out = float(out_units) / float(10 ** int(dec_out))
            if amt_in <= 0:
                return None
            return float(amt_out / amt_in)
        except Exception:
            return None

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
        """Return DIEM price quoted in the configured QUOTE asset."""
        symbol = "DIEM"
        source = "missing"
        selected: Optional[float] = None

        try:
            diem = (self._address_for_symbol("DIEM") or "").strip()
            quote = self._quote_token_address().strip()
        except Exception:
            type(self)._record_price_source(symbol, source, {"valid": False, "reason": "lookup_failed"})
            return None
        if not diem or not quote:
            type(self)._record_price_source(symbol, source, {"valid": False, "reason": "missing_env"})
            return None

        routes = self._collect_trade_paths()
        if not routes:
            routes = [make_route([diem, quote])]
        else:
            has_diem_path = any(r.tokens and r.tokens[0].lower() == diem.lower() for r in routes)
            if not has_diem_path:
                routes.insert(0, make_route([diem, quote]))

        detail_payload: Optional[Dict[str, Any]] = None
        seen: set[tuple[str, ...]] = set()
        for route in routes:
            tokens = [t.strip() for t in route.tokens if t and t.strip()]
            if not tokens or tokens[0].lower() != diem.lower():
                continue
            key = tuple(t.lower() for t in tokens)
            if key in seen:
                continue
            seen.add(key)

            total: Optional[float] = None
            local_source = None
            source_detail: Optional[Dict[str, Any]] = None
            direct_quote = self._try_path_direct(route)
            if isinstance(direct_quote, dict):
                local_source = "aggregator_direct"
                total = float(direct_quote.get("price") or 0.0)
                source_detail = {
                    "provider": direct_quote.get("provider"),
                    "path": direct_quote.get("path"),
                }
                if direct_quote.get("source"):
                    source_detail["source"] = direct_quote.get("source")
            else:
                seg_price = self._price_via_segments(route)
                if self._valid_price(seg_price):
                    local_source = "aggregator_segments"
                    total = float(seg_price)
                    source_detail = {"provider": "segments", "path": tokens}
            if total is None:
                continue

            final_token = tokens[-1]
            if final_token.lower() != quote.lower():
                tail = self._hop_price(final_token, quote)
                if not self._valid_price(tail):
                    continue
                total *= float(tail)
                if local_source:
                    local_source = f"{local_source}_tail"
                else:
                    local_source = "aggregator_tail"
                if source_detail is not None:
                    detail_path = list(source_detail.get("path") or [])
                    if detail_path and detail_path[-1].lower() != quote.lower():
                        detail_path = list(detail_path) + [quote]
                        source_detail["path"] = detail_path
                else:
                    source_detail = {"provider": "tail_hop", "path": tokens + [quote]}

            if not self._valid_price(total):
                continue
            selected = float(total)
            source = local_source or "aggregator"
            detail_payload = source_detail
            break

        if not self._valid_price(selected):
            direct = self._hop_price(diem, quote)
            if self._valid_price(direct):
                selected = float(direct)
                source = "hop"
                detail_payload = {"provider": "hop", "path": [diem, quote]}

        if self._valid_price(selected):
            detail_payload = detail_payload or {}
            detail_payload["valid"] = True
            type(self)._record_price_source(symbol, source, detail_payload)
            return float(selected)

        type(self)._record_price_source(symbol, "missing", {"valid": False})
        return None


    def prices(self, symbols: List[str]) -> Dict[str, float]:
        """Return prices for requested symbols with caching and telemetry."""
        symbols = list(symbols or [])
        stats: Dict[str, Any] = {
            "symbols": [self._norm_symbol_label(sym) for sym in symbols],
            "cache_hits": 0,
            "cache_misses": 0,
            "dex_calls": 0,
        }
        self._active_stats = stats
        raw: Dict[str, float] = {}
        normalized_out: Dict[str, float] = {}
        overall_outcome = "ok"
        total_elapsed = 0.0
        start_total = time.perf_counter()
        label_map: Dict[str, str] = {}
        start_map: Dict[str, float] = {}
        try:
            misses: List[str] = []
            for sym in symbols:
                sym_label = self._norm_symbol_label(sym)
                label_map[sym] = sym_label
                sym_start = time.perf_counter()
                cached = self._cache_price_get(sym)
                if cached is not None:
                    stats["cache_hits"] += 1
                    raw[sym] = cached
                    self._record_counter("marketdata_price_cache_hits_total", {"symbol": sym_label})
                    elapsed = time.perf_counter() - sym_start
                    self._record_latency(sym_label, "symbol", elapsed, "cache")
                    continue
                stats["cache_misses"] += 1
                self._record_counter("marketdata_price_cache_misses_total", {"symbol": sym_label})
                misses.append(sym)
                start_map[sym] = sym_start
            worker_env = os.getenv("MARKETDATA_PRICE_FETCH_WORKERS")
            try:
                max_workers = int(worker_env) if worker_env else 4
            except Exception:
                max_workers = 4
            max_workers = max(1, max_workers)
            if misses:
                with ThreadPoolExecutor(max_workers=min(len(misses), max_workers)) as executor:
                    future_map = {executor.submit(self._price_for_symbol, sym): sym for sym in misses}
                    for future, sym in future_map.items():
                        sym_label = label_map.get(sym, self._norm_symbol_label(sym))
                        sym_start = start_map.get(sym, start_total)
                        try:
                            price = future.result()
                        except Exception:
                            elapsed = time.perf_counter() - sym_start
                            self._record_counter("marketdata_price_lookup_total", {"symbol": sym_label, "outcome": "error"})
                            self._record_latency(sym_label, "symbol", elapsed, "error")
                            overall_outcome = "error"
                            raise
                        else:
                            raw[sym] = price
                            outcome = "ok" if self._valid_price(price) else "invalid"
                            elapsed = time.perf_counter() - sym_start
                            self._record_counter("marketdata_price_lookup_total", {"symbol": sym_label, "outcome": outcome})
                            self._record_latency(sym_label, "symbol", elapsed, outcome)
                            self._cache_price_set(sym, price)
            for sym, price in raw.items():
                try:
                    val = float(price)
                except Exception:
                    val = 0.0
                if str(sym).upper() == "USDC":
                    val = 1.0
                elif not self._valid_price(val):
                    val = 0.0
                normalized_out[sym] = val
            total_elapsed = time.perf_counter() - start_total
            try:
                from libs.telemetry.events import emit as _emit
                payload = {
                    "symbols": [str(s) for s in symbols],
                    "prices": dict(normalized_out),
                    "latency_ms": round(total_elapsed * 1000.0, 3),
                    "cache_hits": stats["cache_hits"],
                    "cache_misses": stats["cache_misses"],
                    "dex_calls": stats.get("dex_calls", 0),
                }
                _emit("signal.market.prices", payload)
            except Exception:
                pass
            return normalized_out
        except Exception:
            overall_outcome = "error"
            raise
        finally:
            if total_elapsed <= 0.0:
                total_elapsed = time.perf_counter() - start_total
            self._mark_last_latency(total_elapsed)
            self._record_latency("batch", "request", total_elapsed, overall_outcome)
            request_labels = {"outcome": overall_outcome, "count": str(len(symbols))}
            self._record_counter("marketdata_prices_requests_total", request_labels)
            self._active_stats = None
            stats["duration_seconds"] = total_elapsed
            total_requests = stats["cache_hits"] + stats["cache_misses"]
            stats["cache_hit_rate"] = (stats["cache_hits"] / total_requests) if total_requests else 0.0
            stats["timestamp"] = time.time()
            self._last_prices_stats = dict(stats)

    def _price_for_symbol(self, symbol: str) -> float:
        sym_str = "" if symbol is None else str(symbol)
        su = sym_str.upper()
        if su == "USDC":
            return 1.0
        if su == "DIEM":
            try:
                px_fb = self.diem_price_with_fallback()
                if self._valid_price(px_fb):
                    return self._apply_price_sanity("DIEM", float(px_fb))
            except Exception:
                pass
            try:
                route = self._route_from_env()
                self._stat_increment("dex_calls")
                bp = self.best_price(route, amount_in_decimal=1.0, label_symbol=su)
                price = float(bp.get("price") or 0.0)
                price_valid = self._valid_price(price)
                detail = {
                    "valid": price_valid,
                    "provider": bp.get("provider") if isinstance(bp, dict) else None,
                    "path": bp.get("path") if isinstance(bp, dict) else None,
                }
                type(self)._record_price_source("DIEM", "route_direct", detail)
                return self._apply_price_sanity("DIEM", price)
            except Exception:
                type(self)._record_price_source("DIEM", "missing", {"valid": False, "reason": "route_error"})
                return self._apply_price_sanity("DIEM", 0.0)
        if su == "VVV":
            try:
                token = self._address_for_symbol("VVV")
                quote = self._quote_token_address()
                if not token or not quote:
                    raise ValueError("VVV or QUOTE token address missing")
                route_override = self._route_optional_from_env("VVV_PRICE_PATH") or self._route_optional_from_env("VVV_TRADE_PATH")
                if route_override:
                    try:
                        self._stat_increment("dex_calls")
                        bp = self.best_price(route_override, amount_in_decimal=1.0, label_symbol=su)
                        price = float(bp.get("price") or 0.0)
                        if self._valid_price(price):
                            return price
                    except Exception:
                        pass
                try:
                    self._stat_increment("dex_calls")
                    bp = self.best_price(make_route([token, quote]), amount_in_decimal=1.0)
                    price = float(bp.get("price") or 0.0)
                    if self._valid_price(price):
                        return price
                except Exception:
                    pass
                try:
                    weth = self._weth_address()
                    self._stat_increment("dex_calls")
                    bp2 = self.best_price(make_route([token, weth, quote]), amount_in_decimal=1.0)
                    price = float(bp2.get("price") or 0.0)
                    if self._valid_price(price):
                        return price
                except Exception:
                    pass
                try:
                    weth = self._weth_address()
                    px_tw = self._mid_price_from_reserves(token, weth) or 0.0
                    px_wq = self._mid_price_from_reserves(weth, quote) or 0.0
                    if px_tw > 0 and px_wq > 0:
                        combo = float(px_tw) * float(px_wq)
                        if self._valid_price(combo):
                            return combo
                except Exception:
                    pass
                external = self._external_price('VVV')
                if self._valid_price(external):
                    return self._apply_price_sanity('VVV', float(external))
                return 0.0
            except Exception:
                external = self._external_price('VVV')
                if self._valid_price(external):
                    return self._apply_price_sanity('VVV', float(external))
                return 0.0
        if su == "WBTC":
            try:
                token = self._address_for_symbol("WBTC")
                quote = self._quote_token_address()
                if not token or not quote:
                    raise ValueError("WBTC or QUOTE token address missing")
                override = self._route_optional_from_env("WBTC_PRICE_PATH") or self._route_optional_from_env("WBTC_TRADE_PATH")
                weth = self._weth_address()
                routes: List[RoutePlan] = []
                if override:
                    routes.append(override)
                if weth and weth.lower() not in {token.lower(), quote.lower()}:
                    routes.append(make_route([token, weth, quote]))
                routes.append(make_route([token, quote]))
                for route in routes:
                    priced = None
                    candidates: list[float] = []
                    probe_sizes = (1e-6, 1e-5, 1e-4, 1e-3, 0.01, 0.1, 1.0)
                    for amt in probe_sizes:
                        try:
                            self._stat_increment("dex_calls")
                            bp = self.best_price(route, amount_in_decimal=float(amt), label_symbol=su)
                        except Exception:
                            continue
                        if not bp:
                            continue
                        price = float(bp.get("price") or 0.0)
                        if self._valid_price(price):
                            candidates.append(price)
                    if candidates:
                        priced = max(candidates)
                    if priced is None:
                        try:
                            scan_price = self._best_price_scan(route, start=1.0, min_amount=1e-6, factor=10.0)
                        except Exception:
                            scan_price = None
                        if self._valid_price(scan_price):
                            priced = float(scan_price)
                    if priced is not None and self._valid_price(priced):
                        return self._apply_price_sanity("WBTC", float(priced))
                # Reserve-based fallback(s)
                px_direct = self._mid_price_from_reserves(token, quote)
                if self._valid_price(px_direct):
                    return self._apply_price_sanity("WBTC", float(px_direct))
                try:
                    px_tw = self._mid_price_from_reserves(token, weth) or 0.0
                    px_wq = self._mid_price_from_reserves(weth, quote) or 0.0
                    if self._valid_price(px_tw) and self._valid_price(px_wq):
                        combo = float(px_tw) * float(px_wq)
                        if self._valid_price(combo):
                            return self._apply_price_sanity("WBTC", combo)
                except Exception:
                    pass
                return self._apply_price_sanity("WBTC", 0.0)
            except Exception:
                return self._apply_price_sanity("WBTC", 0.0)
        if su == "ETH":
            try:
                weth = self._weth_address()
                quote = self._quote_token_address()
                self._stat_increment("dex_calls")
                bp = self.best_price(make_route([weth, quote]), amount_in_decimal=1.0, label_symbol=su)
                price = float(bp.get("price") or 0.0)
                if self._valid_price(price):
                    return price
            except Exception:
                pass
            try:
                weth = self._weth_address()
                quote = self._quote_token_address()
                px = self._mid_price_from_reserves(weth, quote)
                if self._valid_price(px):
                    return float(px)
            except Exception:
                pass
            return 0.0
        return 0.0


    _vvv_metrics_cache: Optional[Dict[str, Any]] = None
    _vvv_metrics_cache_t: float = 0.0
    _diem_balance_cache: Optional[Dict[str, Any]] = None
    _diem_balance_cache_t: float = 0.0
    _mint_rate_cache: Optional[Dict[str, Any]] = None
    _mint_rate_cache_t: float = 0.0

    def vvv_metrics(self, ttl_s: int = 30, retries: int = 2, backoff_s: float = 0.5) -> Dict[str, Any]:
        """Fetch VVV metrics (circulating supply, utilization, staking_yield) with cache and retry."""
        now = time.time()
        if self._vvv_metrics_cache and (now - self._vvv_metrics_cache_t) < ttl_s:
            cached = self._vvv_metrics_cache
            if isinstance(cached, dict):
                self._record_utilization_sample(cached.get("utilization"))
                return dict(cached)
            return cached
        from libs.venice_sdk.client import VeniceClient

        client = VeniceClient()
        last_err: Optional[Exception] = None
        for i in range(retries + 1):
            try:
                res = client.get_vvv_metrics()
                if isinstance(res, dict):
                    self._record_utilization_sample(res.get("utilization"))
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

    def _compute_mint_rate(self) -> Dict[str, Any]:
        diem_dec, svvv_dec = self._diem_svvv_decimals()
        env_rate = os.getenv("DIEM_MINT_RATE")
        if env_rate:
            try:
                rate = float(env_rate)
                return {"tokens_per_diem": rate, "svvv_units_per_diem": None, "source": "env_float"}
            except Exception:
                pass
        env_units = os.getenv("DIEM_MINT_RATE_SVVV_PER_DIEM")
        if env_units:
            try:
                units_val = int(env_units)
                tokens = self._ratio_units_to_tokens(units_val, diem_dec, svvv_dec)
                return {
                    "tokens_per_diem": tokens,
                    "svvv_units_per_diem": units_val,
                    "source": "env_units",
                }
            except Exception:
                pass

        metrics: Optional[Dict[str, Any]] = None
        try:
            metrics = self.vvv_metrics(ttl_s=60)
        except Exception:
            metrics = None

        if isinstance(metrics, dict):
            rate_candidate = self._deep_find(metrics, [
                "diemMintRate",
                "diem_mint_rate",
                "mintRate",
                "mint_rate",
                "mintRateTokens",
            ])
            rate_float = self._to_float(rate_candidate)
            units_candidate = self._deep_find(metrics, [
                "mintRateSvvvPerDiem",
                "mint_rate_svvv_per_diem",
                "mintRateUnits",
            ])
            units_val = None
            try:
                if units_candidate is not None:
                    units_val = int(units_candidate)
            except Exception:
                units_val = None

            if units_val is not None and units_val > 0:
                tokens = self._ratio_units_to_tokens(units_val, diem_dec, svvv_dec)
                return {
                    "tokens_per_diem": tokens,
                    "svvv_units_per_diem": units_val,
                    "source": "vvv_metrics",
                }
            if rate_float is not None and rate_float > 0:
                return {
                    "tokens_per_diem": rate_float,
                    "svvv_units_per_diem": None,
                    "source": "vvv_metrics",
                }

        venice_fallback = self._fetch_venice_mint_rate(diem_decimals=diem_dec, svvv_decimals=svvv_dec)
        if venice_fallback is not None:
            return venice_fallback

        return {"tokens_per_diem": None, "svvv_units_per_diem": None, "source": "unknown"}

    def diem_mint_rate(self, ttl_s: int = 120) -> Dict[str, Any]:
        now = time.time()
        if self._mint_rate_cache and (now - self._mint_rate_cache_t) < ttl_s:
            return dict(self._mint_rate_cache)
        info = self._compute_mint_rate()
        self._mint_rate_cache = dict(info)
        self._mint_rate_cache_t = now
        try:
            from libs.telemetry.events import emit as _emit

            _emit("signal.market.diem_mint_rate", {**info, "ts": int(now)})
        except Exception:
            pass
        return info

    def _fetch_venice_mint_rate(self, *, diem_decimals: int, svvv_decimals: int) -> Optional[Dict[str, Any]]:
        try:
            from libs.venice_sdk.client import VeniceClient

            client = VeniceClient()
            payload = client.get_vvv_staking_yield()
        except Exception:
            return None

        if not isinstance(payload, dict):
            return None

        rate_candidate = self._deep_find(
            payload,
            [
                "mintRateTokens",
                "mint_rate_tokens",
                "diemMintRate",
                "diem_mint_rate",
            ],
        )
        units_candidate = self._deep_find(
            payload,
            [
                "mintRateSvvvPerDiem",
                "mint_rate_svvv_per_diem",
                "mintRateUnits",
            ],
        )

        tokens_val: Optional[float] = None
        units_val: Optional[int] = None
        try:
            if units_candidate is not None:
                units_val = int(units_candidate)
        except Exception:
            units_val = None
        if units_val is not None and units_val > 0:
            tokens_val = self._ratio_units_to_tokens(units_val, diem_decimals, svvv_decimals)
        else:
            tokens_val = self._to_float(rate_candidate)

        if tokens_val is None or tokens_val <= 0:
            return None
        return {
            "tokens_per_diem": float(tokens_val),
            "svvv_units_per_diem": int(units_val) if units_val is not None and units_val > 0 else None,
            "source": "venice_api",
        }

    # --- Etherscan v2 discovery helpers ---
    def discover_trade_path(self, path: List[str]) -> Dict[str, Any]:
        """Return discovery report for the path using Etherscan v2 helpers.

        Wraps services.marketdata.etherscan_verify.verify_trade_path.
        """
        from services.marketdata.etherscan_verify import verify_trade_path  # lazy import

        return verify_trade_path(path)

    def reserve_cap_units(self, path: RouteLike, take_bps: Optional[int] = None) -> Optional[int]:
        """Estimate a conservative max input units based on pool reserves.

        - Only applies for direct 2-token path (path[0] -> path[1]) on UniswapV2-like pools.
        - Caps input to a fraction of the input-side reserve: take_bps/10_000.
        - Env override RISK_MAX_POOL_TAKE_BPS if take_bps not provided (default 100 = 1%).
        Returns None when discovery or reserves unavailable.
        """
        route = as_route_plan(path)
        tokens = route.tokens
        if len(tokens) < 2:
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
            cached = get_cached_pair_info_for_tokens(tokens[0], tokens[1])
            if isinstance(cached, dict):
                pair = cached.get("pair")
                rez = cached.get("reserves")
                t0 = cached.get("token0")
                t1 = cached.get("token1")
        except Exception:
            pass
        if not pair:
            disc = verify_trade_path(tokens)
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
            inp_n = _norm(tokens[0])
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
