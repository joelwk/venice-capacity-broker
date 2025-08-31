from __future__ import annotations

import time
from typing import Dict, Tuple

from libs.kv import KVStore


class KVSlidingWindowLimiter:
    """
    Fixed-window rate limiter backed by KV atomic counters.

    Behavior
    - Uses a per-window counter key: `rl:{key}:{window_start_epoch}`.
    - Increments atomically via KV and sets TTL to the end of the window.
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
        window_seconds = int(window_seconds)
        limit = int(limit)
        window_start = int(now // window_seconds) * window_seconds
        window_end = window_start + window_seconds
        bucket_key = f"rl:{key}:{window_start}"
        ttl_remaining = max(1, int(window_end - now))

        count = int(self.kv.incrby(bucket_key, 1, ttl_s=ttl_remaining))
        allowed = count <= limit
        remaining = max(0, limit - count)
        headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(window_end)),
        }
        return allowed, headers
