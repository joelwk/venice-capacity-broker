"""
Rate limiting configuration for Venice Broker API.

Builds KV-backed rate limiter and admin KV store from environment.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from libs.kv import KVStore
    from libs.ratelimit import KVSlidingWindowLimiter

logger = logging.getLogger("broker.api.rate_limit")


def build_rate_limiter() -> tuple[
    KVSlidingWindowLimiter | None,
    KVStore | None,
    bool,
    int,
    int,
]:
    """
    Build rate limiter and KV admin from environment configuration.
    
    Returns:
        Tuple of (limiter, kv_admin, enabled, window_seconds, max_requests)
        
    Environment variables:
        RATE_LIMITS_ENABLED: Enable rate limiting (default: false)
        RATE_LIMIT_WINDOW_SECONDS: Sliding window in seconds (default: 60)
        RATE_LIMIT_MAX_REQUESTS: Max requests per window (default: 60)
    """
    enabled = (os.getenv("RATE_LIMITS_ENABLED") or "false").strip().lower() == "true"
    window_seconds = int((os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60)
    max_requests = int((os.getenv("RATE_LIMIT_MAX_REQUESTS") or "60").strip() or 60)
    
    limiter: KVSlidingWindowLimiter | None = None
    kv_admin: KVStore | None = None
    
    if enabled:
        try:
            from libs.kv import KVStore
            from libs.ratelimit import KVSlidingWindowLimiter
            
            kv = KVStore()
            limiter = KVSlidingWindowLimiter(kv)
            kv_admin = kv
            logger.info(
                "rate-limiter: enabled (window=%ss, max=%s)",
                window_seconds,
                max_requests,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("rate-limiter: failed to initialize; continuing without (%s)", e)
            limiter = None
            kv_admin = None
    else:
        # KV admin available even without rate limiting (for idempotency, broker limits, etc.)
        try:
            from libs.kv import KVStore
            kv_admin = KVStore()
        except Exception:
            kv_admin = None
    
    return limiter, kv_admin, enabled, window_seconds, max_requests

