from __future__ import annotations

import math
import threading
import time

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
        # Store sliding window state: {cache_key: (window_start, count)}
        # Previous window is derived by subtracting window_seconds from window_start
        self._sliding_window_state: dict[str, dict[int, int]] = {}
        self._local_lock = threading.Lock()
        checker = getattr(kv, "has_atomic_counters", None)
        if callable(checker):
            try:
                self._strict_atomic = bool(checker())
            except Exception:
                self._strict_atomic = False
        else:
            self._strict_atomic = False

    def _now(self) -> float:
        return time.time()

    def allow(
        self, key: str, limit: int, window_seconds: int
    ) -> tuple[bool, dict[str, str]]:
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

        if not getattr(self, "_strict_atomic", False):
            with self._local_lock:
                # Include window_seconds and limit in cache key to avoid stale state
                # when rate limits are dynamically updated
                cache_key = f"{key}:{window_seconds}:{limit}"

                # Get or initialize sliding window state for this cache key
                window_buckets = self._sliding_window_state.get(cache_key, {})

                # Get current window count
                count = window_buckets.get(window_start, 0)

                # Get previous window count
                prev_window_start = window_start - window_seconds
                prev_count = window_buckets.get(prev_window_start, 0)

                # Calculate weighted contribution from previous window (same as atomic version)
                elapsed = max(0.0, min(float(window_seconds), now - window_start))
                prev_weight = max(
                    0.0, min(1.0, 1.0 - (elapsed / float(window_seconds)))
                )
                effective = float(count) + float(prev_count) * prev_weight

                # Check if request should be allowed (before incrementing)
                # effective < limit means we have capacity remaining
                if effective >= float(limit):
                    remaining = max(0.0, float(limit) - effective)
                    reset_epoch = window_end
                    headers = {
                        "X-RateLimit-Limit": str(limit),
                        "X-RateLimit-Remaining": str(int(remaining)),
                        "X-RateLimit-Reset": str(int(reset_epoch)),
                    }
                    return False, headers

                # Increment current window count
                count += 1
                window_buckets[window_start] = count

                # Clean up old windows (keep only current and previous)
                # This prevents unbounded memory growth
                keys_to_remove = [
                    k for k in window_buckets.keys() if k < prev_window_start
                ]
                for k in keys_to_remove:
                    del window_buckets[k]

                self._sliding_window_state[cache_key] = window_buckets

                # Recalculate effective count after increment
                effective = float(count) + float(prev_count) * prev_weight
                remaining = max(0.0, float(limit) - effective)
                reset_epoch = window_end

            headers = {
                "X-RateLimit-Limit": str(limit),
                "X-RateLimit-Remaining": str(int(remaining)),
                "X-RateLimit-Reset": str(int(reset_epoch)),
            }
            return True, headers

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
