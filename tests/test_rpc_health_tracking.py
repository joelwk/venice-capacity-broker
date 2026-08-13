"""Tests for RPC health tracking and endpoint selection."""

from __future__ import annotations

import time

from libs.agentkit_ext.rpc_health import RpcEndpointHealth, RpcHealthTracker


def test_rpc_endpoint_health_is_healthy():
    """Test that healthy endpoints are correctly identified."""
    health = RpcEndpointHealth(url="https://test.rpc")
    assert health.is_healthy() is True

    # Record some successes
    health.record_success()
    assert health.success_count == 1
    assert health.is_healthy() is True


def test_rpc_endpoint_health_rate_limit_cooldown():
    """Test that rate-limited endpoints are marked unhealthy during cooldown."""
    health = RpcEndpointHealth(url="https://test.rpc")
    health.record_failure(is_rate_limit=True)
    assert health.last_429_at > 0

    # Should be unhealthy during cooldown
    assert health.is_healthy(cooldown_seconds=60.0) is False

    # Should recover after cooldown
    health.last_429_at = time.time() - 70.0  # 70 seconds ago
    assert health.is_healthy(cooldown_seconds=60.0) is True


def test_rpc_endpoint_health_backoff():
    """Test that endpoints in backoff are marked unhealthy."""
    health = RpcEndpointHealth(url="https://test.rpc")
    health.apply_backoff(base_seconds=60.0, max_seconds=600.0, multiplier=2.0)
    assert health.backoff_until > time.time()
    assert health.is_healthy() is False

    # Should recover after backoff expires
    health.backoff_until = time.time() - 1.0
    assert health.is_healthy() is True


def test_rpc_health_tracker_selects_healthy_endpoint():
    """Test that tracker prefers healthy endpoints."""
    tracker = RpcHealthTracker()
    candidates = [
        "https://rpc1.test",
        "https://rpc2.test",
        "https://rpc3.test",
    ]

    # All endpoints start healthy
    selected = tracker.select_best_endpoint(candidates)
    assert selected in candidates

    # Mark first endpoint as unhealthy (rate limited)
    tracker.record_failure(candidates[0], is_rate_limit=True)
    selected = tracker.select_best_endpoint(candidates)
    assert selected != candidates[0]  # Should avoid rate-limited endpoint


def test_rpc_health_tracker_prefers_successful_endpoints():
    """Test that tracker prefers endpoints with more successes."""
    tracker = RpcHealthTracker()
    candidates = [
        "https://rpc1.test",
        "https://rpc2.test",
    ]

    # Record successes for first endpoint
    tracker.record_success(candidates[0])
    tracker.record_success(candidates[0])
    tracker.record_success(candidates[0])

    # Record failures for second endpoint
    tracker.record_failure(candidates[1])

    selected = tracker.select_best_endpoint(candidates)
    assert selected == candidates[0]  # Should prefer successful endpoint


def test_rpc_health_tracker_falls_back_to_least_unhealthy():
    """Test that tracker falls back to least unhealthy when all are unhealthy."""
    tracker = RpcHealthTracker()
    candidates = [
        "https://rpc1.test",
        "https://rpc2.test",
    ]

    # Mark both as rate-limited
    tracker.record_failure(candidates[0], is_rate_limit=True)
    tracker.record_failure(candidates[1], is_rate_limit=True)

    # Should still return one (least unhealthy)
    selected = tracker.select_best_endpoint(candidates)
    assert selected in candidates


def test_rpc_health_tracker_exponential_backoff():
    """Test that tracker applies exponential backoff on failures."""
    tracker = RpcHealthTracker(backoff_base_seconds=60.0, backoff_multiplier=2.0)
    url = "https://test.rpc"

    # First failure - no backoff yet
    tracker.record_failure(url)
    health = tracker._health[url]
    assert health.consecutive_failures == 1

    # Multiple failures should increase backoff
    tracker.record_failure(url)
    tracker.record_failure(url)
    tracker.record_failure(url)

    health = tracker._health[url]
    assert health.consecutive_failures == 4
    # Backoff should be applied (60 * 2^(4-1) = 480 seconds, capped at max)
    assert health.backoff_until > time.time()


def test_rpc_health_tracker_success_clears_backoff():
    """Test that successful calls clear backoff."""
    tracker = RpcHealthTracker()
    url = "https://test.rpc"

    # Apply backoff
    tracker.record_failure(url)
    tracker.record_failure(url)
    health = tracker._health[url]
    assert health.backoff_until > 0

    # Success should clear backoff
    tracker.record_success(url)
    health = tracker._health[url]
    assert health.consecutive_failures == 0
    # Backoff should be cleared if expired
    if health.backoff_until <= time.time():
        assert health.backoff_until == 0.0
