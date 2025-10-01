from __future__ import annotations

import math
import time
from typing import Dict, Tuple

from libs.kv import KVStore


class KVSlidingWindowLimiter:
    """
    Sliding-window rate limiter backed by KV atomic counters.

    Behavior
    - Uses a per-window counter key: `rl:{key}:{window_start_epoch}`.
    - Increments atomically via KV and sets TTL to cover the current window
      plus the next window so boundary-spanning requests still see the
      previous bucket.
    - Combines the current window with the previous window using a weighted
      contribution to approximate a continuous sliding window.
    - Returns standard X-RateLimit headers and a reset epoch.

    Notes
    - With Redis (`REDIS_URL`) this is atomic across processes.
    - With Replit DB HTTP or in-memory fallback, atomicity is best-effort or
      process-local only.
    """

    def __init__(self, kv: KVStore) -> None:
        self.kv = kv

    def _now(self) -> float:
        return time.time()

    def allow(self, key: str, limit: int, window_seconds: int) -> Tuple[bool, Dict[str, str]]:
        now = self._now()
        window_seconds = max(1, int(window_seconds))
        limit = int(limit)
        if limit <= 0:
            headers = {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(int(now) + window_seconds),
            }
            return False, headers

        window_start = int(now // window_seconds) * window_seconds
        window_end = window_start + window_seconds
        bucket_key = f"rl:{key}:{window_start}"
        ttl_span = (window_end - now) + window_seconds
        ttl_remaining = max(1, int(math.ceil(ttl_span)))

        count = int(self.kv.incrby(bucket_key, 1, ttl_s=ttl_remaining))

        prev_key = f"rl:{key}:{window_start - window_seconds}"
        prev_raw = self.kv.get(prev_key)
        try:
            prev_count = int(prev_raw) if prev_raw is not None else 0
        except Exception:
            prev_count = 0

        elapsed = max(0.0, min(float(window_seconds), now - window_start))
        prev_weight = max(0.0, min(1.0, 1.0 - (elapsed / float(window_seconds))))
        effective = float(count) + float(prev_count) * prev_weight
        allowed = effective <= float(limit)
        remaining = max(0.0, float(limit) - effective)

        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(int(remaining)),
            "X-RateLimit-Reset": str(int(window_end)),
        }
        return allowed, headers
