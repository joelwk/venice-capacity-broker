"""
Lifespan management for Venice Broker API.

Handles startup warming of market data and env-prices cache.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

try:
    from libs.telemetry.logger import get_logger

    logger = get_logger("broker.api.lifespan")
except Exception:  # pragma: no cover - fallback if telemetry not available
    logger = logging.getLogger("broker.api.lifespan")

_marketdata_warm_logged = False

_DEFAULT_WARMUP_MARKETDATA_TIMEOUT_SECONDS = 30.0


def _warmup_marketdata_timeout_seconds() -> float:
    raw = os.getenv("BROKER_WARMUP_MARKETDATA_TIMEOUT_SECONDS") or ""
    if not raw.strip():
        return _DEFAULT_WARMUP_MARKETDATA_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except Exception:
        return _DEFAULT_WARMUP_MARKETDATA_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_WARMUP_MARKETDATA_TIMEOUT_SECONDS
    return value


def warm_marketdata_async(
    symbols: tuple[str, ...],
    get_marketdata_provider: Callable,
) -> None:
    """
    Warm market data provider cache in background thread.

    Args:
        symbols: Symbols to warm (e.g., ("DIEM", "ETH", "USDC"))
        get_marketdata_provider: Function to get MarketDataProvider instance
    """
    if not symbols:
        return

    def _runner() -> None:
        global _marketdata_warm_logged
        try:
            provider = get_marketdata_provider()
            provider.prices(
                list(symbols),
                timeout_s=_warmup_marketdata_timeout_seconds(),
            )
        except Exception as exc:
            logger.warning("marketdata warmup failed: %s", exc)
        else:
            if not _marketdata_warm_logged:
                _marketdata_warm_logged = True
                try:
                    logger.info(
                        "marketdata warmup complete for symbols=%s", ",".join(symbols)
                    )
                except Exception:
                    pass

    threading.Thread(target=_runner, name="broker-marketdata-warm", daemon=True).start()


def warm_env_prices_async(
    symbols: tuple[str, ...],
    get_marketdata_provider: Callable,
    env_status_fn: Callable,
    env_prices_cache_set: Callable[[str, dict], None],
) -> None:
    """
    Warm env-and-prices cache in background thread.

    Args:
        symbols: Symbols to warm
        get_marketdata_provider: Function to get MarketDataProvider instance
        env_status_fn: Function that returns env status dict
        env_prices_cache_set: Function to set cache entry
    """
    warm_symbols_list = list(symbols)
    if "VVV" not in {s.upper() for s in warm_symbols_list}:
        warm_symbols_list.append("VVV")

    def _warm_env_prices():
        try:
            # Build env payload first (fast, no network calls)
            env_payload = env_status_fn()

            # Get provider and warm prices (this populates provider's internal cache too)
            mdp = get_marketdata_provider()
            try:
                prices_payload = mdp.prices(
                    warm_symbols_list,
                    timeout_s=_warmup_marketdata_timeout_seconds(),
                )
            except Exception as price_exc:
                logger.warning("price warmup failed, using empty prices: %s", price_exc)
                # Still cache env payload with empty prices so frontend can at least get config
                prices_payload = {}

            stats = mdp.last_prices_stats() if hasattr(mdp, "last_prices_stats") else {}
            meta = {"symbols": warm_symbols_list, "warmup_completed": True}
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
            key_parts = sorted({s.upper() for s in warm_symbols_list})
            cache_key = ",".join(key_parts) if key_parts else "__empty__"
            result = {"env": env_payload, "prices": prices_payload, "meta": meta}
            # Use same fingerprint function as router to ensure cache matches
            try:
                from . import cache as cache_module
            except ImportError:
                from apps.broker_api import cache as cache_module

            provider_fp = cache_module.provider_fingerprint(mdp)
            env_prices_cache_set(cache_key, result, source_fingerprint=provider_fp)
            logger.info(
                "env-and-prices cache warmed for symbols=%s (env_keys=%d, price_keys=%d)",
                ",".join(warm_symbols_list),
                len(env_payload) if isinstance(env_payload, dict) else 0,
                len(prices_payload) if isinstance(prices_payload, dict) else 0,
            )
        except Exception as exc:
            logger.warning("env-and-prices warmup failed: %s", exc)

    threading.Thread(
        target=_warm_env_prices, name="broker-env-prices-warm", daemon=True
    ).start()


def warm_env_prices_http_ping(symbols: tuple[str, ...]) -> None:
    """Ping env-and-prices after startup so the cache is warm for first UI load."""
    if os.getenv("PYTEST_CURRENT_TEST"):
        return
    disabled = os.getenv("BROKER_WARMUP_PING_DISABLE", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return

    sym_list = [s.upper() for s in symbols if s]
    if "VVV" not in sym_list:
        sym_list.append("VVV")
    if not sym_list:
        return

    host = (
        os.getenv("AUTOSTART_BROKER_HOST") or os.getenv("HOST") or "127.0.0.1"
    ).strip()
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"
    port = (os.getenv("AUTOSTART_BROKER_PORT") or os.getenv("PORT") or "8000").strip()
    scheme = (os.getenv("BROKER_WARMUP_PING_SCHEME") or "http").strip() or "http"
    delay_raw = os.getenv("BROKER_WARMUP_PING_DELAY_SECONDS") or "1.5"
    timeout_raw = os.getenv("BROKER_WARMUP_PING_TIMEOUT_SECONDS") or "15"
    attempts_raw = os.getenv("BROKER_WARMUP_PING_ATTEMPTS") or "3"
    try:
        delay = max(0.0, float(delay_raw))
    except Exception:
        delay = 1.5
    try:
        timeout = max(1.0, float(timeout_raw))
    except Exception:
        timeout = 15.0
    try:
        attempts = max(1, int(attempts_raw))
    except Exception:
        attempts = 3

    url = f"{scheme}://{host}:{port}/v1/env-and-prices?symbols={','.join(sym_list)}"

    def _runner() -> None:
        try:
            import requests
        except Exception as exc:
            logger.warning("warmup ping skipped (requests unavailable): %s", exc)
            return
        for attempt in range(1, attempts + 1):
            try:
                if delay > 0:
                    time.sleep(delay if attempt == 1 else min(2.0 * attempt, 5.0))
                response = requests.get(url, timeout=timeout)
                if response.ok:
                    logger.info("startup warmup ping ok: %s", url)
                    return
                logger.warning(
                    "startup warmup ping non-200 (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    response.status_code,
                )
            except Exception as exc:
                logger.warning(
                    "startup warmup ping failed (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    exc,
                )

    threading.Thread(target=_runner, name="broker-env-prices-ping", daemon=True).start()


def _seed_pools_async() -> None:
    """Seed DIEM pools in background thread to avoid blocking port binding."""
    try:
        from services.marketdata.pools import seed_diem_pools_from_env

        seeded, updated = seed_diem_pools_from_env()
        if seeded > 0 or updated > 0:
            logger.info(
                "pool.diem | Startup seeding complete: %d seeded, %d already existed",
                seeded,
                updated,
            )
    except Exception as exc:
        logger.warning("pool.diem | Startup seeding failed: %s", exc)


@asynccontextmanager
async def lifespan(
    app: FastAPI,
    warm_symbols: tuple[str, ...],
    get_marketdata_provider: Callable,
    env_status_fn: Callable,
    env_prices_cache_set: Callable[[str, dict], None],
):
    """
    Async context manager for FastAPI lifespan events.

    Warms market data and env-prices cache on startup.
    Seeds pool registry with DIEM pools from env (in background).

    Args:
        app: FastAPI application instance
        warm_symbols: Symbols to warm (e.g., ("DIEM", "ETH", "USDC"))
        get_marketdata_provider: Function to get MarketDataProvider instance
        env_status_fn: Function that returns env status dict
        env_prices_cache_set: Function to set env-prices cache entry
    """
    # Seed pool registry in background to avoid blocking port binding
    # This calls get_web3() which can be slow on first connection
    threading.Thread(
        target=_seed_pools_async, name="broker-pool-seed", daemon=True
    ).start()

    try:
        warm_marketdata_async(warm_symbols, get_marketdata_provider)
        warm_env_prices_async(
            warm_symbols, get_marketdata_provider, env_status_fn, env_prices_cache_set
        )
        warm_env_prices_http_ping(warm_symbols)
    except Exception as exc:
        logger.debug("startup warmup scheduling failed: %s", exc)
    yield
