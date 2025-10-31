"""
Lifespan management for Venice Broker API.

Handles startup warming of market data and env-prices cache.
"""
from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger("broker.api.lifespan")

_marketdata_warm_logged = False


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
            provider.prices(list(symbols))
        except Exception as exc:  # noqa: BLE001
            logger.warning("marketdata warmup failed: %s", exc)
        else:
            if not _marketdata_warm_logged:
                _marketdata_warm_logged = True
                try:
                    logger.info("marketdata warmup complete for symbols=%s", ",".join(symbols))
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
    warm_symbols_list = list(symbols) + ["VVV"]

    def _warm_env_prices():
        try:
            env_payload = env_status_fn()
            mdp = get_marketdata_provider()
            prices_payload = mdp.prices(warm_symbols_list)
            stats = mdp.last_prices_stats() if hasattr(mdp, "last_prices_stats") else {}
            meta = {"symbols": warm_symbols_list}
            if isinstance(stats, dict):
                for key in ("cache_hits", "cache_misses", "cache_hit_rate", "dex_calls", "duration_seconds"):
                    if key in stats:
                        meta[key] = stats[key]
            key_parts = sorted({s.upper() for s in warm_symbols_list})
            cache_key = ",".join(key_parts) if key_parts else "__empty__"
            result = {"env": env_payload, "prices": prices_payload, "meta": meta}
            env_prices_cache_set(cache_key, result, source_fingerprint=id(mdp))
            logger.info("env-and-prices cache warmed for symbols=%s", ",".join(warm_symbols_list))
        except Exception as exc:  # noqa: BLE001
            logger.warning("env-and-prices warmup failed: %s", exc)

    threading.Thread(target=_warm_env_prices, name="broker-env-prices-warm", daemon=True).start()


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
    
    Args:
        app: FastAPI application instance
        warm_symbols: Symbols to warm (e.g., ("DIEM", "ETH", "USDC"))
        get_marketdata_provider: Function to get MarketDataProvider instance
        env_status_fn: Function that returns env status dict
        env_prices_cache_set: Function to set env-prices cache entry
    """
    try:
        warm_marketdata_async(warm_symbols, get_marketdata_provider)
        warm_env_prices_async(warm_symbols, get_marketdata_provider, env_status_fn, env_prices_cache_set)
    except Exception as exc:  # noqa: BLE001
        logger.debug("startup warmup scheduling failed: %s", exc)
    yield

