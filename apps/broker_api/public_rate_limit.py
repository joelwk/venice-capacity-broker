"""Per-IP rate limiting for unauthenticated buyer endpoints.

The tenant chat limiter is keyed by API key; the public buy flow (quotes,
payment verification, recovery, status) has no credential, so we key on the
client IP per route instead. Backed by the shared sliding-window limiter,
which degrades to process-local counting when no Redis/KV is configured.
"""

from __future__ import annotations

import os
import threading

from fastapi import HTTPException, Request

from libs.env import env_flag

_DEFAULT_WINDOW_SECONDS = 60
_DEFAULT_MAX_REQUESTS = 30

_limiter = None
_limiter_lock = threading.Lock()


def _get_limiter():
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                from libs.kv import KVStore
                from libs.ratelimit import KVSlidingWindowLimiter

                _limiter = KVSlidingWindowLimiter(KVStore())
    return _limiter


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for") or ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def enforce_public_rate_limit(request: Request) -> None:
    """FastAPI dependency: reject with 429 once the per-IP budget is spent."""
    if not env_flag("BUY_RATE_LIMITS_ENABLED", True):
        return

    window_seconds = _env_int("BUY_RATE_LIMIT_WINDOW_SECONDS", _DEFAULT_WINDOW_SECONDS)
    max_requests = _env_int("BUY_RATE_LIMIT_MAX_REQUESTS", _DEFAULT_MAX_REQUESTS)

    key = f"pub:{_client_ip(request)}:{request.url.path}"
    try:
        allowed, headers = _get_limiter().allow(key, max_requests, window_seconds)
    except Exception:
        # Rate limiting must never take the buy flow down with it.
        return
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded; retry shortly",
            headers=headers,
        )


__all__ = ["enforce_public_rate_limit"]
