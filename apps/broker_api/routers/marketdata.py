"""
Market data and environment endpoints for Venice Broker API.

Provides endpoints for:
- /v1/env - Environment configuration
- /v1/market/prices - Market prices for symbols
- /v1/env-and-prices - Combined environment and prices
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import deque
from collections.abc import Callable
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from services.marketdata.provider import MarketDataProvider
from services.memory.store import MemoryStore

from .. import cache

router = APIRouter(prefix="/v1", tags=["marketdata"])

_get_marketdata_provider: Callable
_env_status_fn: Callable[[], dict[str, Any]]
_logger: logging.Logger
_require_admin: Callable[[str | None], None]

_MARKET_CACHE_MAX_AGE = 10
_MARKET_CACHE_STALE_SECONDS = 30
_MARKET_REFRESH_STATS: dict[str, tuple[int, float]] = {}
_MARKET_REFRESH_LOCK = Lock()

try:
    from libs.telemetry.metrics import inc as _metrics_inc  # type: ignore
    from libs.telemetry.metrics import set_gauge as _metrics_gauge  # type: ignore
except Exception:  # pragma: no cover

    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return

    def _metrics_gauge(name: str, value: float, labels: dict | None = None) -> None:  # type: ignore
        return


def init_router(
    *,
    get_marketdata_provider: Callable,
    env_status_fn: Callable[[], dict[str, Any]],
    logger: logging.Logger,
    require_admin: Callable[[str | None], None],
) -> APIRouter:
    """
    Initialize marketdata router with dependencies.

    Args:
        get_marketdata_provider: Function to get MarketDataProvider instance
        env_status_fn: Function that returns environment status dict
        logger: Logger instance
    """
    global _get_marketdata_provider, _env_status_fn, _logger, _require_admin
    _get_marketdata_provider = get_marketdata_provider
    _env_status_fn = env_status_fn
    _logger = logger
    _require_admin = require_admin
    return router


def _build_env_status() -> dict[str, Any]:
    """Build environment status response."""
    try:
        return _env_status_fn()
    except Exception as e:
        _logger.warning("env_status_fn failed: %s", e)
        return {
            "version": "0.2.0",
            "features": {},
            "pricing": {"discounts": {}},
        }


@router.get("/env")
def get_env() -> dict[str, Any]:
    """
    Get environment configuration and status.

    Returns:
        Environment status dict with features, pricing, payments, etc.
    """
    return _build_env_status()


@router.get("/market/prices")
def get_market_prices(
    symbols: str = Query(
        ..., description="Comma-separated list of symbols (e.g., DIEM,ETH,USDC)"
    ),
    ttl_s: int | None = Query(
        default=None,
        ge=1,
        le=600,
        description="Optional cache TTL override in seconds",
    ),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    hidden_suppressions: int | None = Header(
        default=None, alias="X-Broker-Hidden-Suppressed"
    ),
) -> JSONResponse:
    """
    Get current market prices for symbols.

    Args:
        symbols: Comma-separated list of symbols to fetch prices for

    Returns:
        Dict with 'prices' and 'meta' keys
    """
    try:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            raise HTTPException(status_code=400, detail="at least one symbol required")
        with _otel_span(
            "broker.market.prices",
            {"symbols": ",".join(symbol_list), "ttl_s": ttl_s or 0},
        ) as span:
            # Check cache first
            cache_key = ",".join(sorted(symbol_list))
            provider = _get_marketdata_provider()
            provider_fp = cache.provider_fingerprint(provider)

            bypass_cache = False
            calls_attr = getattr(provider, "calls", None)
            if isinstance(calls_attr, list) and not calls_attr:
                bypass_cache = True

            cached = (
                None
                if bypass_cache
                else cache.prices_cache_get(cache_key, expected_fingerprint=provider_fp)
            )
            if cached is not None:
                if span is not None:
                    try:
                        span.set_attribute("cache_hit", True)
                    except Exception:
                        pass
                if hidden_suppressions:
                    _metrics_inc(
                        "market_hidden_tab_suppressions_total",
                        value=int(hidden_suppressions),
                    )
                meta = cached.get("meta") or {}
                _record_market_metrics("market.prices", meta, cached=True)
                return _maybe_cache_response(
                    cached,
                    symbols=symbol_list,
                    prices=cached.get("prices") or {},
                    if_none_match=if_none_match,
                )

            # Fetch fresh prices
            prices_payload = provider.prices(symbol_list, ttl_s=ttl_s)

            # Get stats if available
            stats = {}
            if hasattr(provider, "last_prices_stats"):
                try:
                    stats = provider.last_prices_stats() or {}
                except Exception:
                    pass
            # Build response
            meta = {
                "symbols": symbol_list,
                "cacheHit": False,
                "refreshedAt": int(time.time() * 1000),
            }
            if isinstance(stats, dict):
                for key in (
                    "cache_hits",
                    "cache_misses",
                    "cache_hit_rate",
                    "dex_calls",
                    "duration_seconds",
                ):
                    if key in stats:
                        meta[key] = stats[key]

            response = {"prices": prices_payload, "meta": meta}

            # Cache the response
            cache.prices_cache_set(cache_key, response, source_fingerprint=provider_fp)

            if span is not None:
                try:
                    span.set_attribute("cache_hit", False)
                except Exception:
                    pass
            if hidden_suppressions:
                _metrics_inc(
                    "market_hidden_tab_suppressions_total",
                    value=int(hidden_suppressions),
                )
            _record_market_metrics("market.prices", meta, cached=False)
            return _maybe_cache_response(
                response,
                symbols=symbol_list,
                prices=prices_payload,
                if_none_match=if_none_match,
            )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Failed to fetch market prices")
        raise HTTPException(
            status_code=500, detail=f"prices unavailable: {exc}"
        ) from exc


@router.get("/env-and-prices")
def get_env_and_prices(
    symbols: str = Query(
        ..., description="Comma-separated list of symbols (e.g., DIEM,ETH,USDC)"
    ),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
    hidden_suppressions: int | None = Header(
        default=None, alias="X-Broker-Hidden-Suppressed"
    ),
) -> JSONResponse:
    """
    Get combined environment status and market prices.

    This endpoint combines /v1/env and /v1/market/prices into a single response,
    which is useful for frontend initialization.

    Args:
        symbols: Comma-separated list of symbols to fetch prices for

    Returns:
        Dict with 'env', 'prices', and 'meta' keys
    """
    try:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            raise HTTPException(status_code=400, detail="at least one symbol required")

        # Check cache first
        cache_key = ",".join(sorted(symbol_list))
        provider = _get_marketdata_provider()
        provider_fp = cache.provider_fingerprint(provider)

        bypass_cache = False
        calls_attr = getattr(provider, "calls", None)
        if isinstance(calls_attr, list) and not calls_attr:
            bypass_cache = True

        cached = (
            None
            if bypass_cache
            else cache.env_prices_cache_get(cache_key, expected_fingerprint=provider_fp)
        )
        if cached is not None:
            if hidden_suppressions:
                _metrics_inc(
                    "market_hidden_tab_suppressions_total",
                    value=int(hidden_suppressions),
                )
            meta = cached.get("meta") or {}
            _record_market_metrics("market.env_and_prices", meta, cached=True)
            return _maybe_cache_response(
                cached,
                symbols=symbol_list,
                prices=cached.get("prices") or {},
                if_none_match=if_none_match,
            )

        # Fetch fresh data
        # Build env payload first (fast, no network calls)
        env_payload = _build_env_status()

        # Fetch prices (may be slow on first request if provider cache is cold)
        # Provider's internal cache will be populated here, so subsequent requests will be faster
        try:
            prices_payload = provider.prices(symbol_list)
        except Exception as price_exc:
            _logger.warning("price fetch failed in env-and-prices: %s", price_exc)
            # Return env payload with empty prices rather than failing completely
            # Frontend can still initialize and retry prices separately
            prices_payload = {}

        # Get stats if available
        stats = {}
        if hasattr(provider, "last_prices_stats"):
            try:
                stats = provider.last_prices_stats() or {}
            except Exception:
                pass

        # Build response
        meta = {
            "symbols": symbol_list,
            "cacheHit": False,
            "refreshedAt": int(time.time() * 1000),
        }
        if isinstance(stats, dict):
            for key in (
                "cache_hits",
                "cache_misses",
                "cache_hit_rate",
                "dex_calls",
                "duration_seconds",
            ):
                if key in stats:
                    meta[key] = stats[key]

        response = {"env": env_payload, "prices": prices_payload, "meta": meta}

        # Cache the response
        cache.env_prices_cache_set(cache_key, response, source_fingerprint=provider_fp)

        if hidden_suppressions:
            _metrics_inc(
                "market_hidden_tab_suppressions_total",
                value=int(hidden_suppressions),
            )
        _record_market_metrics("market.env_and_prices", meta, cached=False)
        return _maybe_cache_response(
            response,
            symbols=symbol_list,
            prices=prices_payload,
            if_none_match=if_none_match,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Failed to fetch env and prices")
        raise HTTPException(
            status_code=500, detail=f"env/prices unavailable: {exc}"
        ) from exc


def _light_diem_snapshot(prices: dict[str, Any]) -> dict[str, Any]:
    try:
        price = float(prices.get("DIEM", 0.0) or 0.0) if prices else 0.0
    except Exception:
        price = 0.0
    return {
        "symbol": "DIEM",
        "priceUsd": price,
        "timestampMs": int(time.time() * 1000),
    }


@router.get("/market/snapshot")
def get_market_snapshot(
    symbols: str = Query(
        ..., description="Comma-separated list of symbols (e.g., DIEM,ETH,USDC)"
    ),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> JSONResponse:
    """
    Return { env, prices, diem } in one call.

    DIEM is a lightweight view derived from the cached env-and-prices feed.
    """
    try:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            raise HTTPException(status_code=400, detail="at least one symbol required")

        cache_key = ",".join(sorted(symbol_list))
        provider = _get_marketdata_provider()
        provider_fp = cache.provider_fingerprint(provider)

        bypass_cache = False
        calls_attr = getattr(provider, "calls", None)
        if isinstance(calls_attr, list) and not calls_attr:
            bypass_cache = True

        cached = (
            None
            if bypass_cache
            else cache.env_prices_cache_get(cache_key, expected_fingerprint=provider_fp)
        )
        if cached is not None:
            prices_payload = cached.get("prices") or {}
            response = {
                "env": cached.get("env") or {},
                "prices": prices_payload,
                "diem": _light_diem_snapshot(prices_payload),
            }
            meta = cached.get("meta") or {}
            _record_market_metrics("market.snapshot", meta, cached=True)
            return _maybe_cache_response(
                response,
                symbols=symbol_list,
                prices=prices_payload,
                if_none_match=if_none_match,
            )

        env_payload = _build_env_status()
        try:
            prices_payload = provider.prices(symbol_list)
        except Exception as price_exc:
            _logger.warning("price fetch failed in market snapshot: %s", price_exc)
            prices_payload = {}

        meta = {
            "symbols": symbol_list,
            "cacheHit": False,
            "refreshedAt": int(time.time() * 1000),
        }
        cache.env_prices_cache_set(
            cache_key,
            {"env": env_payload, "prices": prices_payload, "meta": meta},
            source_fingerprint=provider_fp,
        )

        response = {
            "env": env_payload,
            "prices": prices_payload,
            "diem": _light_diem_snapshot(prices_payload),
        }
        _record_market_metrics("market.snapshot", meta, cached=False)
        return _maybe_cache_response(
            response,
            symbols=symbol_list,
            prices=prices_payload,
            if_none_match=if_none_match,
        )
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Failed to fetch market snapshot")
        raise HTTPException(
            status_code=500, detail=f"market snapshot unavailable: {exc}"
        ) from exc


def _cache_control_value(max_age: int = _MARKET_CACHE_MAX_AGE) -> str:
    return f"public, max-age={max_age}, stale-while-revalidate={_MARKET_CACHE_STALE_SECONDS}"


def _public_cache_enabled() -> bool:
    raw = os.getenv("MARKETDATA_PUBLIC_CACHE_ENABLED") or ""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _etag_for_prices(symbols: list[str], prices: dict[str, Any]) -> str:
    payload = {"symbols": sorted(symbols), "prices": prices or {}}
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    digest = hashlib.blake2s(encoded, digest_size=8).hexdigest()
    return f'W/"{digest}"'


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    raw = if_none_match.strip()
    if raw == "*":
        return True
    candidates = [tag.strip() for tag in raw.split(",") if tag.strip()]
    return etag in candidates


def _not_modified_response(
    etag: str, *, max_age: int = _MARKET_CACHE_MAX_AGE
) -> Response:
    headers = {"Cache-Control": _cache_control_value(max_age), "ETag": etag}
    return Response(status_code=304, headers=headers)


def _json_cacheable(
    content: dict, *, max_age: int = _MARKET_CACHE_MAX_AGE, etag: str | None = None
) -> JSONResponse:
    headers = {"Cache-Control": _cache_control_value(max_age)}
    if etag:
        headers["ETag"] = etag
    return JSONResponse(content=content, headers=headers)


def _maybe_cache_response(
    content: dict,
    *,
    symbols: list[str],
    prices: dict[str, Any],
    if_none_match: str | None,
) -> Response:
    if not _public_cache_enabled():
        return _create_no_cache_response(content)
    etag = _etag_for_prices(symbols, prices)
    if _etag_matches(if_none_match, etag):
        return _not_modified_response(etag)
    return _json_cacheable(content, etag=etag)


def _record_market_metrics(
    endpoint: str, meta: dict[str, Any], *, cached: bool
) -> None:
    cache_hit = meta.get("cacheHit")
    if cache_hit is None:
        cache_hit = cached
    if cache_hit:
        _metrics_inc("market_cache_hits_total", labels={"endpoint": endpoint})
    else:
        _metrics_inc("market_cache_misses_total", labels={"endpoint": endpoint})

    hit_rate = meta.get("cache_hit_rate")
    if hit_rate is None:
        hit_rate = 1.0 if cache_hit else 0.0
    try:
        _metrics_gauge(
            "market_cache_hit_ratio",
            float(hit_rate),
            labels={"endpoint": endpoint},
        )
    except Exception:
        pass

    duration_seconds = meta.get("duration_seconds")
    if duration_seconds is None:
        return
    try:
        latency_ms = float(duration_seconds) * 1000.0
    except Exception:
        return
    with _MARKET_REFRESH_LOCK:
        count, avg = _MARKET_REFRESH_STATS.get(endpoint, (0, 0.0))
        count += 1
        avg = avg + (latency_ms - avg) / count
        _MARKET_REFRESH_STATS[endpoint] = (count, avg)
    _metrics_gauge("market_refresh_latency_ms", avg, labels={"endpoint": endpoint})


@contextmanager
def _otel_span(name: str, attrs: dict[str, Any] | None = None):
    """
    Best-effort OpenTelemetry span wrapper.

    Important: never swallow exceptions raised inside the `with _otel_span(...):`
    block. Only telemetry wiring failures should degrade to a no-op span.
    """
    try:
        from opentelemetry import trace  # type: ignore

        tracer = trace.get_tracer(__name__)
        span_cm = tracer.start_as_current_span(name)
    except Exception:
        with nullcontext():
            yield None
        return

    with span_cm as span:
        if attrs:
            for key, value in attrs.items():
                try:
                    span.set_attribute(key, value)
                except Exception:
                    pass
        yield span


def _create_no_cache_response(content: dict) -> JSONResponse:
    """Create a JSONResponse with aggressive no-cache headers to prevent stale data."""
    return JSONResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


def _premium_diagnostics_enabled() -> bool:
    try:
        flag = (os.getenv("DIEM_PREMIUM_DIAGNOSTICS_ENABLE") or "").strip().lower()
        return flag in {"1", "true", "yes", "on"}
    except Exception:
        return False


def _extract_diem_premium_history(
    entries: list[dict[str, Any]] | None, *, lookback: int
) -> list[dict[str, Any]]:
    if not entries or lookback <= 0:
        return []
    out: list[dict[str, Any]] = []
    for entry in entries:
        cycle = entry.get("cycle") if isinstance(entry, dict) else None
        if not isinstance(cycle, dict):
            continue
        arbi = cycle.get("arbi")
        if not isinstance(arbi, dict):
            continue
        snap = arbi.get("diemPremium")
        if not isinstance(snap, dict):
            continue
        out.append(
            {
                "ts": cycle.get("ts"),
                "snapshot": snap,
                "attribution": arbi.get("diemPremiumAttribution"),
            }
        )
    # Ensure oldest -> newest and clamp to lookback
    return out[-lookback:]


_DEX_DIAGNOSTICS_PATH = Path(
    os.getenv("DEX_DIAGNOSTICS_LOG") or "logs/dex_diagnostics.jsonl"
)


def _recent_dex_diagnostics(limit: int = 3) -> list[dict[str, Any]]:
    if not _DEX_DIAGNOSTICS_PATH.exists():
        return []
    recent: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
    try:
        with _DEX_DIAGNOSTICS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                recent.append(entry)
    except Exception:
        return []
    return list(recent)


def _current_diem_payload(provider: MarketDataProvider) -> dict[str, Any]:
    """
    Build DIEM price snapshot payload with cache-first lookup.

    Checks env+prices cache before calling provider.prices() to avoid
    redundant DEX lookups when prices are already available from recent
    /v1/env-and-prices calls.
    """
    symbol = "DIEM"
    price = 0.0
    source = "fresh"

    # Try to get DIEM price from env+prices cache first (fast path)
    # Common cache key for buy page symbols (sorted): DIEM,ETH,USDC,VVV,WBTC
    common_symbols = ["DIEM", "ETH", "USDC", "VVV", "WBTC"]
    cache_key = ",".join(sorted(common_symbols))
    # Use same fingerprint logic as warmup to ensure cache matches
    provider_fp = cache.provider_fingerprint(provider)
    cached_env_prices = cache.env_prices_cache_get(
        cache_key, expected_fingerprint=provider_fp
    )

    if cached_env_prices and isinstance(cached_env_prices.get("prices"), dict):
        cached_price = cached_env_prices["prices"].get(symbol)
        if cached_price is not None:
            try:
                price = float(cached_price)
                if price > 0:
                    source = "cache"
            except (ValueError, TypeError):
                pass

    # Fallback to provider.prices() if cache miss or invalid price
    if price <= 0:
        prices = provider.prices([symbol])
        price = float(prices.get(symbol, 0.0) or 0.0)

    health: dict[str, Any] = {}
    try:
        health = provider.price_health(symbol, max_age=180.0)
    except Exception:
        health = {}
    diagnostics = _recent_dex_diagnostics()

    # Add fallback reason and pool registry status
    fallback_info: dict[str, Any] = {}
    try:
        # Extract fallback reason from health info if available
        if isinstance(health, dict):
            fallback_reason = health.get("fallback_reason")
            fallback_source = health.get("source")
            if fallback_reason or fallback_source:
                fallback_info["reason"] = fallback_reason or "unknown"
                fallback_info["source"] = fallback_source
    except Exception:
        pass

    # Check pool registry status
    pool_registry_status: dict[str, Any] = {}
    try:
        from db.models import DexPool
        from db.session import get_session

        diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip().lower()
        vvv_usdc_pool = (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip().lower()

        # Normalize addresses for lookup
        def _normalize_hex(value: str) -> str:
            raw = value.lower().strip()
            if raw.startswith("0x"):
                raw = raw[2:]
            padded = raw.rjust(40, "0")
            return "0x" + padded[-40:]

        if diem_vvv_pair:
            pool_addr = _normalize_hex(diem_vvv_pair)
            with next(get_session()) as session:
                pool = session.get(DexPool, pool_addr)
                pool_registry_status["diem_vvv_pair_registered"] = pool is not None
                pool_registry_status["diem_vvv_pair_address"] = diem_vvv_pair

        if vvv_usdc_pool:
            pool_addr = _normalize_hex(vvv_usdc_pool)
            with next(get_session()) as session:
                pool = session.get(DexPool, pool_addr)
                pool_registry_status["vvv_usdc_pool_registered"] = pool is not None
                pool_registry_status["vvv_usdc_pool_address"] = vvv_usdc_pool
    except Exception:
        pool_registry_status["error"] = "unable_to_check"

    return {
        "symbol": symbol,
        "priceUsd": price,
        "health": health,
        "diagnostics": diagnostics,
        "timestampMs": int(time.time() * 1000),
        # Internal metadata for debugging (not part of public API contract)
        "_meta": {
            "source": source,
            "fallback_info": fallback_info if fallback_info else None,
            "pool_registry_status": pool_registry_status
            if pool_registry_status
            else None,
        },
    }


@router.get("/market/diem")
def get_diem_price() -> JSONResponse:
    try:
        _metrics_inc("market_diem_snapshot_total", labels={"endpoint": "market.diem"})
        provider = _get_marketdata_provider()
        payload = _current_diem_payload(provider)
        return _create_no_cache_response(
            {"diem": payload, "refreshedAt": int(time.time() * 1000)}
        )
    except Exception as exc:
        _logger.exception("Failed to fetch DIEM price snapshot")
        raise HTTPException(
            status_code=500,
            detail={"message": "DIEM price unavailable", "error": str(exc)},
        ) from exc


@router.get("/market/diem/premium")
def get_diem_premium(
    lookback: int = Query(
        10, ge=0, le=200, description="Number of prior premium snapshots to return"
    ),
) -> JSONResponse:
    """
    Get DIEM premium snapshot and attribution.

    Returns both premium ratios:
    - premiumFair = priceUsd / fairValueUsd
    - premiumMint = priceUsd / mintCostFloorUsd

    This endpoint is gated by DIEM_PREMIUM_DIAGNOSTICS_ENABLE=1.
    """
    if not _premium_diagnostics_enabled():
        raise HTTPException(status_code=404, detail="DIEM premium diagnostics disabled")
    try:
        from libs.pricing.diem import fair_value_per_diem
        from libs.pricing.diem_metrics import (
            build_diem_premium_snapshot,
            compute_diem_premium_attribution,
        )

        provider = _get_marketdata_provider()
        prices = provider.prices(["DIEM", "VVV"]) or {}
        diem_price = float(prices.get("DIEM", 0.0) or 0.0)
        vvv_price = float(prices.get("VVV", 0.0) or 0.0)

        price_health: dict[str, Any] = {}
        try:
            price_health = provider.price_health("DIEM", max_age=180.0) or {}
        except Exception:
            price_health = {}

        has_onchain_liquidity = True
        try:
            src = str(price_health.get("source") or "")
            if src in ("bridge_vvv", "external_reference"):
                has_onchain_liquidity = False
        except Exception:
            has_onchain_liquidity = True

        # Mint rate (best-effort): provider may expose it depending on deployment.
        mint_rate = 1.0
        mint_rate_source = "default"
        try:
            mint_fn = getattr(provider, "diem_mint_rate", None)
            if callable(mint_fn):
                info = mint_fn(ttl_s=60)
                if isinstance(info, dict):
                    candidate = info.get("tokens_per_diem")
                    if candidate not in (None, 0):
                        mint_rate = float(candidate)  # type: ignore[arg-type]
                        mint_rate_source = str(info.get("source") or "market")
        except Exception:
            mint_rate = 1.0
            mint_rate_source = "default"

        fair_data = fair_value_per_diem(
            vvv_price=vvv_price,
            mint_rate=mint_rate,
            emissions_penalty=0.20,
            utilization_current=None,
            utilization_trend=None,
            circulating_supply=None,
            target_supply=38_000,
            discount_rate_apy=0.15,
            growth_rate_apy=0.05,
            historical_ratio=None,
            has_onchain_liquidity=has_onchain_liquidity,
            market_price=diem_price,
        )
        fair_value = (
            float(fair_data.get("fair_value", 0.0))
            if isinstance(fair_data, dict)
            else float(fair_data)
        )
        fv_components = (
            fair_data.get("components", {}) if isinstance(fair_data, dict) else {}
        )

        snapshot = build_diem_premium_snapshot(
            price_usd=diem_price,
            vvv_price_usd=vvv_price,
            mint_rate=mint_rate,
            fair_value_usd=fair_value,
            fair_value_components=fv_components
            if isinstance(fv_components, dict)
            else None,
            price_health=price_health if isinstance(price_health, dict) else None,
            computed_at_ts=time.time(),
        )

        # History from agent memory (best-effort; may be empty if orchestrator isn't writing JSONL here).
        store = MemoryStore()
        raw_history = []
        try:
            raw_history = store.recent(max(1, min(500, lookback * 10)))
        except Exception:
            raw_history = []
        history = _extract_diem_premium_history(raw_history, lookback=lookback)
        prev = history[-1]["snapshot"] if history else None
        attribution = compute_diem_premium_attribution(current=snapshot, previous=prev)

        payload = {
            "current": snapshot,
            "attribution": attribution,
            "history": history,
            "meta": {
                "lookback": int(lookback),
                "mintRateSource": mint_rate_source,
                "memoryPath": str(store.path),
            },
            "refreshedAt": int(time.time() * 1000),
        }
        return _create_no_cache_response(payload)
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Failed to fetch DIEM premium snapshot")
        raise HTTPException(
            status_code=500,
            detail={"message": "DIEM premium unavailable", "error": str(exc)},
        ) from exc


@router.get("/market/diagnostics")
def get_market_diagnostics(
    symbols: str = Query(
        "DIEM,VVV,ETH,USDC,WBTC",
        description="Comma-separated list of symbols to probe (defaults to buy page set).",
    ),
    timeout_s: float | None = Query(
        default=None,
        ge=0.1,
        le=120.0,
        description="Optional batch timeout override for this diagnostics probe.",
    ),
    authorization: str | None = Header(default=None),
) -> JSONResponse:
    """
    Admin-only market diagnostics.

    Returns:
    - The probe prices payload (may be partial on timeout)
    - Which symbols timed out / errored in the last batch
    - Per-symbol last known price source metadata (path/provider/etc.)
    """
    _require_admin(authorization)
    try:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        if not symbol_list:
            raise HTTPException(status_code=400, detail="at least one symbol required")
        provider: MarketDataProvider = _get_marketdata_provider()
        prices = provider.prices(symbol_list, timeout_s=timeout_s)
        # Treat missing keys or non-positive values as "unavailable" for the UI.
        missing_or_zero: list[str] = []
        for sym in symbol_list:
            try:
                value = float(prices.get(sym, 0.0) or 0.0)
            except Exception:
                value = 0.0
            if value <= 0.0 and sym != "USDC":
                missing_or_zero.append(sym)

        stats = (
            provider.last_prices_stats()
            if hasattr(provider, "last_prices_stats")
            else {}
        )
        sources = {}
        for sym in symbol_list:
            try:
                sources[sym] = provider._get_price_source(sym)  # type: ignore[attr-defined]
            except Exception:
                sources[sym] = {}

        latency = None
        try:
            latency = provider.last_prices_latency()
        except Exception:
            latency = None

        payload = {
            "probe": {
                "symbols": symbol_list,
                "timeout_s": timeout_s,
                "prices": prices,
                "missing_or_zero": missing_or_zero,
            },
            "last_batch": stats if isinstance(stats, dict) else {},
            "sources": sources,
            "latency": latency,
            "env": {
                "MARKETDATA_PRICES_TIMEOUT_SECONDS": os.getenv(
                    "MARKETDATA_PRICES_TIMEOUT_SECONDS"
                ),
                "BROKER_WARMUP_MARKETDATA_TIMEOUT_SECONDS": os.getenv(
                    "BROKER_WARMUP_MARKETDATA_TIMEOUT_SECONDS"
                ),
                "MARKETDATA_PRICE_FETCH_WORKERS": os.getenv(
                    "MARKETDATA_PRICE_FETCH_WORKERS"
                ),
            },
            "refreshedAt": int(time.time() * 1000),
        }
        return JSONResponse(content=payload)
    except HTTPException:
        raise
    except Exception as exc:
        _logger.exception("Failed to build market diagnostics")
        raise HTTPException(
            status_code=500, detail=f"market diagnostics unavailable: {exc}"
        ) from exc
