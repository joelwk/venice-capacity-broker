"""Regression test for Broker API lifespan warmup cache import fix."""

from __future__ import annotations

import pytest


def test_lifespan_warmup_cache_import() -> None:
    """Verify that lifespan warmup can import cache module without errors."""
    # This test ensures the fix for apps.cache import mismatch works
    try:
        from apps.broker_api import cache as cache_module
        from apps.broker_api import lifespan as lifespan_module

        # Verify cache module has provider_fingerprint function
        assert hasattr(cache_module, "provider_fingerprint")
        assert callable(cache_module.provider_fingerprint)

        # Verify lifespan module can be imported without ImportError
        assert lifespan_module is not None
    except ImportError as exc:
        pytest.fail(f"Broker lifespan warmup cache import failed: {exc}")


def test_lifespan_warmup_env_prices_no_crash() -> None:
    """Verify warm_env_prices_async doesn't crash when apps.cache is refactored."""
    from apps.broker_api.lifespan import warm_env_prices_async

    # Mock dependencies
    def mock_get_marketdata_provider():
        class MockProvider:
            def prices(self, symbols):
                return {"DIEM": 100.0, "VVV": 1.0, "USDC": 1.0}

            def last_prices_stats(self):
                return {}

        return MockProvider()

    def mock_env_status_fn():
        return {"venice": {"ready": True}}

    def mock_env_prices_cache_set(key, result, source_fingerprint=None):
        pass

    # Should not raise ImportError even if apps/__init__.py is empty
    try:
        warm_env_prices_async(
            symbols=("DIEM", "ETH", "USDC"),
            get_marketdata_provider=mock_get_marketdata_provider,
            env_status_fn=mock_env_status_fn,
            env_prices_cache_set=mock_env_prices_cache_set,
        )
        # Give background thread a moment
        import time

        time.sleep(0.1)
    except ImportError as exc:
        if "cache" in str(exc).lower() and "apps" in str(exc).lower():
            pytest.fail(f"Broker warmup still has apps.cache import issue: {exc}")
        raise
