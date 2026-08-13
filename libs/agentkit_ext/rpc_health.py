"""Health-aware RPC endpoint selection and rotation for Base chain.

This module provides intelligent RPC endpoint selection that tracks health,
handles rate limits (429 errors), and rotates away from failing endpoints.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from threading import Lock

try:
    from libs.telemetry.logger import get_logger

    _logger = get_logger("rpc.health")
except Exception:

    class _NullLogger:
        def info(self, *args, **kwargs):
            return

        def warning(self, *args, **kwargs):
            return

        def debug(self, *args, **kwargs):
            return

    _logger = _NullLogger()


@dataclass
class RpcEndpointHealth:
    """Health metadata for a single RPC endpoint."""

    url: str
    success_count: int = 0
    failure_count: int = 0
    last_429_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    backoff_until: float = 0.0
    consecutive_failures: int = 0

    def is_healthy(self, *, cooldown_seconds: float = 60.0) -> bool:
        """Check if endpoint is currently healthy and not in backoff."""
        if self.backoff_until > 0 and time.time() < self.backoff_until:
            return False
        if self.last_429_at > 0:
            elapsed = time.time() - self.last_429_at
            if elapsed < cooldown_seconds:
                return False
        return True

    def record_success(self) -> None:
        """Record a successful call."""
        self.success_count += 1
        self.last_success_at = time.time()
        self.consecutive_failures = 0
        # Clear backoff on success
        if self.backoff_until > 0 and time.time() >= self.backoff_until:
            self.backoff_until = 0.0

    def record_failure(self, *, is_rate_limit: bool = False) -> None:
        """Record a failed call."""
        self.failure_count += 1
        self.last_failure_at = time.time()
        self.consecutive_failures += 1
        if is_rate_limit:
            self.last_429_at = time.time()

    def apply_backoff(
        self,
        *,
        base_seconds: float = 60.0,
        max_seconds: float = 600.0,
        multiplier: float = 2.0,
    ) -> None:
        """Apply exponential backoff based on consecutive failures."""
        if self.consecutive_failures <= 0:
            # Allow manual backoff even if no failures were recorded yet
            self.consecutive_failures = 1
        failures = max(1, self.consecutive_failures)
        backoff = min(base_seconds * (multiplier ** (failures - 1)), max_seconds)
        self.backoff_until = time.time() + backoff


class RpcHealthTracker:
    """Tracks health of multiple RPC endpoints and selects the best one."""

    def __init__(
        self,
        *,
        rate_limit_cooldown_seconds: float = 60.0,
        backoff_base_seconds: float = 60.0,
        backoff_max_seconds: float = 600.0,
        backoff_multiplier: float = 2.0,
    ) -> None:
        self._health: dict[str, RpcEndpointHealth] = {}
        self._lock = Lock()
        self.rate_limit_cooldown_seconds = rate_limit_cooldown_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.backoff_multiplier = backoff_multiplier

    def register_endpoint(self, url: str) -> None:
        """Register an RPC endpoint for health tracking."""
        with self._lock:
            if url not in self._health:
                self._health[url] = RpcEndpointHealth(url=url)

    def record_success(self, url: str) -> None:
        """Record a successful call to an endpoint."""
        with self._lock:
            if url not in self._health:
                self._health[url] = RpcEndpointHealth(url=url)
            self._health[url].record_success()

    def record_failure(
        self, url: str, *, is_rate_limit: bool = False, apply_backoff: bool = True
    ) -> None:
        """Record a failed call to an endpoint."""
        with self._lock:
            if url not in self._health:
                self._health[url] = RpcEndpointHealth(url=url)
            health = self._health[url]
            health.record_failure(is_rate_limit=is_rate_limit)
            if apply_backoff:
                health.apply_backoff(
                    base_seconds=self.backoff_base_seconds,
                    max_seconds=self.backoff_max_seconds,
                    multiplier=self.backoff_multiplier,
                )

    def select_best_endpoint(self, candidates: list[str]) -> str | None:
        """Select the best currently healthy endpoint from candidates.

        Returns the first healthy endpoint, preferring ones with:
        1. No active backoff
        2. Recent success
        3. Lower failure count

        If no healthy endpoint is found, returns the least unhealthy one.
        """
        with self._lock:
            # Register any new candidates
            for url in candidates:
                if url not in self._health:
                    self._health[url] = RpcEndpointHealth(url=url)

            # Filter to healthy endpoints
            healthy: list[tuple[RpcEndpointHealth, str]] = []
            unhealthy: list[tuple[RpcEndpointHealth, str]] = []

            for url in candidates:
                if url not in self._health:
                    continue
                health = self._health[url]
                if health.is_healthy(cooldown_seconds=self.rate_limit_cooldown_seconds):
                    healthy.append((health, url))
                else:
                    unhealthy.append((health, url))

            # Prefer healthy endpoints, sorted by success rate and recency
            if healthy:
                healthy.sort(
                    key=lambda x: (
                        -x[0].success_count,  # More successes = better
                        x[0].last_success_at,  # More recent = better
                        -x[0].failure_count,  # Fewer failures = better
                    )
                )
                selected = healthy[0][1]
                _logger.debug(
                    "Selected healthy RPC endpoint: %s (success=%d, failures=%d)",
                    selected,
                    healthy[0][0].success_count,
                    healthy[0][0].failure_count,
                )
                return selected

            # Fall back to least unhealthy if no healthy endpoints
            if unhealthy:
                unhealthy.sort(
                    key=lambda x: (
                        x[0].backoff_until,  # Soonest to recover = better
                        -x[0].success_count,  # More successes = better
                        x[0].last_failure_at,  # Older failure = better
                    )
                )
                selected = unhealthy[0][1]
                _logger.warning(
                    "No healthy RPC endpoints available, using least unhealthy: %s",
                    selected,
                )
                return selected

            # No candidates registered
            return None

    def get_health_summary(self) -> dict[str, dict[str, any]]:
        """Get a summary of all endpoint health for debugging."""
        with self._lock:
            summary: dict[str, dict[str, any]] = {}
            for url, health in self._health.items():
                summary[url] = {
                    "success_count": health.success_count,
                    "failure_count": health.failure_count,
                    "consecutive_failures": health.consecutive_failures,
                    "is_healthy": health.is_healthy(
                        cooldown_seconds=self.rate_limit_cooldown_seconds
                    ),
                    "backoff_until": health.backoff_until,
                    "last_429_at": health.last_429_at,
                    "last_success_at": health.last_success_at,
                    "last_failure_at": health.last_failure_at,
                }
            return summary


# Global singleton instance
_global_tracker: RpcHealthTracker | None = None
_tracker_lock = Lock()


def get_rpc_health_tracker() -> RpcHealthTracker:
    """Get or create the global RPC health tracker."""
    global _global_tracker
    with _tracker_lock:
        if _global_tracker is None:
            rate_limit_cooldown = float(
                os.getenv("RPC_RATE_LIMIT_COOLDOWN_SECONDS", "60") or 60
            )
            backoff_base = float(os.getenv("RPC_BACKOFF_BASE_SECONDS", "60") or 60)
            backoff_max = float(os.getenv("RPC_BACKOFF_MAX_SECONDS", "600") or 600)
            backoff_mult = float(os.getenv("RPC_BACKOFF_MULTIPLIER", "2.0") or 2.0)
            _global_tracker = RpcHealthTracker(
                rate_limit_cooldown_seconds=rate_limit_cooldown,
                backoff_base_seconds=backoff_base,
                backoff_max_seconds=backoff_max,
                backoff_multiplier=backoff_mult,
            )
        return _global_tracker
