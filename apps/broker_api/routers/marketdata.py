"""
Market data and environment endpoints for Venice Broker API.

Provides endpoints for:
- /v1/env - Environment configuration
- /v1/market/prices - Market prices for symbols
- /v1/env-and-prices - Combined environment and prices
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query

from .. import cache

router = APIRouter(prefix="/v1", tags=["marketdata"])

_get_marketdata_provider: Callable
_env_status_fn: Callable[[], dict[str, Any]]
_logger: logging.Logger


def init_router(
    *,
    get_marketdata_provider: Callable,
    env_status_fn: Callable[[], dict[str, Any]],
    logger: logging.Logger,
) -> APIRouter:
    """
    Initialize marketdata router with dependencies.
    
    Args:
        get_marketdata_provider: Function to get MarketDataProvider instance
        env_status_fn: Function that returns environment status dict
        logger: Logger instance
    """
    global _get_marketdata_provider, _env_status_fn, _logger
    _get_marketdata_provider = get_marketdata_provider
    _env_status_fn = env_status_fn
    _logger = logger
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
    symbols: str = Query(..., description="Comma-separated list of symbols (e.g., DIEM,ETH,USDC)"),
) -> dict[str, Any]:
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
        
        # Check cache first
        cache_key = ",".join(sorted(symbol_list))
        provider = _get_marketdata_provider()
        provider_fp = cache.provider_fingerprint(provider)

        bypass_cache = False
        calls_attr = getattr(provider, "calls", None)
        if isinstance(calls_attr, list) and not calls_attr:
            bypass_cache = True

        cached = None if bypass_cache else cache.prices_cache_get(cache_key, expected_fingerprint=provider_fp)
        if cached is not None:
            return cached
        
        # Fetch fresh prices
        prices_payload = provider.prices(symbol_list)
        
        # Get stats if available
        stats = {}
        if hasattr(provider, "last_prices_stats"):
            try:
                stats = provider.last_prices_stats() or {}
            except Exception:
                pass
        # Build response
        meta = {"symbols": symbol_list, "cacheHit": False, "refreshedAt": int(time.time() * 1000)}
        if isinstance(stats, dict):
            for key in ("cache_hits", "cache_misses", "cache_hit_rate", "dex_calls", "duration_seconds"):
                if key in stats:
                    meta[key] = stats[key]
        
        response = {"prices": prices_payload, "meta": meta}
        
        # Cache the response
        cache.prices_cache_set(cache_key, response, source_fingerprint=provider_fp)
        
        return response
    except HTTPException:
        raise
    except Exception as e:
        _logger.exception("market prices fetch failed")
        raise HTTPException(status_code=500, detail=f"failed to fetch prices: {e}") from e


@router.get("/env-and-prices")
def get_env_and_prices(
    symbols: str = Query(..., description="Comma-separated list of symbols (e.g., DIEM,ETH,USDC)"),
) -> dict[str, Any]:
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

        cached = None if bypass_cache else cache.env_prices_cache_get(cache_key, expected_fingerprint=provider_fp)
        if cached is not None:
            return cached
        
        # Fetch fresh data
        env_payload = _build_env_status()
        prices_payload = provider.prices(symbol_list)
        
        # Get stats if available
        stats = {}
        if hasattr(provider, "last_prices_stats"):
            try:
                stats = provider.last_prices_stats() or {}
            except Exception:
                pass
        
        # Build response
        meta = {"symbols": symbol_list, "cacheHit": False, "refreshedAt": int(time.time() * 1000)}
        if isinstance(stats, dict):
            for key in ("cache_hits", "cache_misses", "cache_hit_rate", "dex_calls", "duration_seconds"):
                if key in stats:
                    meta[key] = stats[key]
        
        response = {"env": env_payload, "prices": prices_payload, "meta": meta}
        
        # Cache the response
        cache.env_prices_cache_set(cache_key, response, source_fingerprint=provider_fp)
        
        return response
    except HTTPException:
        raise
    except Exception as e:
        _logger.exception("env-and-prices fetch failed")
        raise HTTPException(status_code=500, detail=f"failed to fetch env-and-prices: {e}") from e

