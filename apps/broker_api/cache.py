"""
Response caching for the Venice Broker API.

Provides TTL-based caching for price responses and env+prices combined responses.
"""
from __future__ import annotations

import copy
import os
import time
from threading import Lock
from typing import Any, Optional, Tuple

# Cache storage
# Exported for test access (tests/test_env_and_prices_cache.py)
_prices_resp_cache: dict[str, tuple[float, dict, Optional[int]]] = {}
_prices_resp_cache_lock = Lock()
_env_prices_resp_cache: dict[str, tuple[float, dict, Optional[int]]] = {}
_env_prices_resp_cache_lock = Lock()


def _coerce_entry(entry: Tuple[Any, ...]) -> tuple[float, dict, Optional[int]]:
    """Normalize cache entries created before fingerprint support."""
    if len(entry) == 3:
        ts, payload, fingerprint = entry
        return float(ts), dict(payload), fingerprint if fingerprint is None else int(fingerprint)
    if len(entry) == 2:
        ts, payload = entry
        return float(ts), dict(payload), None
    raise ValueError("invalid cache entry")


def provider_fingerprint(source: object) -> int:
    """Compute a stable fingerprint for the current marketdata provider instance."""
    override = getattr(source, "cache_fingerprint", None)
    if isinstance(override, int):
        return override
    explicit = getattr(source, "fingerprint", None)
    if isinstance(explicit, int):
        return explicit
    hint = getattr(source, "cache_identity", None)
    if isinstance(hint, int):
        return hint
    return id(source)


def prices_cache_ttl_seconds() -> float:
    """Get TTL for price cache from environment.
    
    Returns:
        TTL in seconds (default 60.0)
    """
    try:
        raw = os.getenv("BROKER_PRICES_TTL_SECONDS")
        if raw is None or str(raw).strip() == "":
            return 60.0  # Increased to 60s to align with frontend watchdog and reduce refresh loops
        ttl = float(raw)
        return ttl if ttl > 0 else 0.0
    except Exception:
        return 60.0


def prices_cache_capacity() -> int:
    """Get maximum cache capacity from environment.
    
    Returns:
        Max number of cached entries (default 128)
    """
    try:
        raw = os.getenv("BROKER_PRICES_CACHE_MAX")
        if raw is None or str(raw).strip() == "":
            return 128
        return max(0, int(raw))
    except Exception:
        return 128


def prices_cache_get(key: str, expected_fingerprint: Optional[int] = None) -> Optional[dict]:
    """Get cached price response by key.
    
    Args:
        key: Cache key
        expected_fingerprint: Optional fingerprint for the active provider
        
    Returns:
        Cached response dict with updated metadata, or None if not found/expired
    """
    ttl = prices_cache_ttl_seconds()
    if ttl <= 0:
        return None
    now = time.time()
    with _prices_resp_cache_lock:
        entry = _prices_resp_cache.get(key)
        if entry is None:
            return None
        ts, payload, fingerprint = _coerce_entry(entry)
        expired = (now - ts) > ttl
        fingerprint_mismatch = expected_fingerprint is not None and fingerprint != expected_fingerprint
        if fingerprint is None and expected_fingerprint is not None:
            fingerprint_mismatch = True
        if expired or fingerprint_mismatch:
            _prices_resp_cache.pop(key, None)
            return None
        snapshot = copy.deepcopy(payload)
        meta = dict(snapshot.get("meta") or {})
        meta["cacheHit"] = True
        meta["cacheAgeMs"] = round((now - ts) * 1000.0, 3)
        meta["refreshedAt"] = int(now * 1000)
        snapshot["meta"] = meta
        return snapshot


def prices_cache_set(key: str, payload: dict, *, source_fingerprint: Optional[int] = None) -> None:
    """Store price response in cache with TTL.
    
    Args:
        key: Cache key
        payload: Response dict to cache
        source_fingerprint: Fingerprint for the provider that produced the payload
    """
    ttl = prices_cache_ttl_seconds()
    if ttl <= 0:
        return
    snapshot = copy.deepcopy(payload)
    now_ms = time.time()
    meta = dict(snapshot.get("meta") or {})
    meta["cacheHit"] = False
    meta["refreshedAt"] = int(now_ms * 1000)
    snapshot["meta"] = meta
    with _prices_resp_cache_lock:
        capacity = prices_cache_capacity()
        if capacity > 0 and len(_prices_resp_cache) >= capacity:
            try:
                oldest_key = min(_prices_resp_cache.items(), key=lambda item: item[1][0])[0]
                _prices_resp_cache.pop(oldest_key, None)
            except ValueError:
                _prices_resp_cache.clear()
        _prices_resp_cache[key] = (now_ms, snapshot, source_fingerprint)


def env_prices_cache_get(key: str, expected_fingerprint: Optional[int] = None) -> Optional[dict]:
    """Get cached env-and-prices response. Uses same TTL as prices cache.
    
    Args:
        key: Cache key
        expected_fingerprint: Optional fingerprint for the active provider
        
    Returns:
        Cached response dict, or None if not found/expired
    """
    ttl = prices_cache_ttl_seconds()
    if ttl <= 0:
        return None
    now = time.time()
    with _env_prices_resp_cache_lock:
        entry = _env_prices_resp_cache.get(key)
        if entry is None:
            return None
        ts, payload, fingerprint = _coerce_entry(entry)
        expired = (now - ts) > ttl
        fingerprint_mismatch = expected_fingerprint is not None and fingerprint != expected_fingerprint
        if fingerprint is None and expected_fingerprint is not None:
            fingerprint_mismatch = True
        if expired or fingerprint_mismatch:
            _env_prices_resp_cache.pop(key, None)
            return None
        snapshot = copy.deepcopy(payload)
        meta = dict(snapshot.get("meta") or {})
        meta["cacheHit"] = True
        meta["cacheAgeMs"] = round((now - ts) * 1000.0, 3)
        meta["refreshedAt"] = int(now * 1000)
        snapshot["meta"] = meta
        return snapshot


def env_prices_cache_set(
    key: str,
    payload: dict,
    *,
    source_fingerprint: Optional[int] = None,
) -> None:
    """Store env-and-prices response in cache.
    
    Args:
        key: Cache key
        payload: Response dict to cache
        source_fingerprint: Fingerprint for the provider that produced the payload
    """
    ttl = prices_cache_ttl_seconds()
    if ttl <= 0:
        return
    snapshot = copy.deepcopy(payload)
    now_ms = time.time()
    meta = dict(snapshot.get("meta") or {})
    meta["cacheHit"] = False
    meta["refreshedAt"] = int(now_ms * 1000)
    snapshot["meta"] = meta
    with _env_prices_resp_cache_lock:
        capacity = prices_cache_capacity()
        if capacity > 0 and len(_env_prices_resp_cache) >= capacity:
            try:
                oldest_key = min(_env_prices_resp_cache.items(), key=lambda item: item[1][0])[0]
                _env_prices_resp_cache.pop(oldest_key, None)
            except ValueError:
                _env_prices_resp_cache.clear()
        _env_prices_resp_cache[key] = (now_ms, snapshot, source_fingerprint)


__all__ = [
    "provider_fingerprint",
    "prices_cache_ttl_seconds",
    "prices_cache_capacity",
    "prices_cache_get",
    "prices_cache_set",
    "env_prices_cache_get",
    "env_prices_cache_set",
]

