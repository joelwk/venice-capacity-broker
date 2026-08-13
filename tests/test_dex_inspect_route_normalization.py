"""Tests for route normalization in DEX aggregator inspection methods."""

from __future__ import annotations

import pytest

from libs.dex.providers import DexAggregator, DexProvider
from libs.dex.routes import make_route


class FakeUniswapV2Provider(DexProvider):
    """Fake Uniswap V2 provider for testing."""

    name = "uniswap_v2"
    supports_exact_out = True

    def __init__(self):
        # Mock router for inspection
        self.router = type("Router", (), {"functions": type("Functions", (), {})()})()

    def quote(self, amount_in: int, route):
        return None

    def trade(self, amount_in: int, min_amount_out: int, route):
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, route):
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route):
        raise NotImplementedError


def test_inspect_provider_route_normalizes_v2_path():
    """Test that _inspect_provider_route normalizes routes before calling to_uniswap_v2_path."""
    # Create a route with fee tiers (V3-style)
    route_with_fees = make_route(["0xtoken1", "0xtoken2", "0xtoken3"], fees=[3000, 500])

    # Create aggregator with fake V2 provider
    provider = FakeUniswapV2Provider()

    # Mock router functions
    def mock_get_amounts_out(amount, path):
        # Verify path doesn't have fee tiers
        assert isinstance(path, list)
        return [amount, amount * 2, amount * 3]

    provider.router.functions.getAmountsOut = type(
        "Function", (), {"call": lambda: mock_get_amounts_out(100, [])}
    )()

    aggregator = DexAggregator([provider])

    # This should not raise "route contains fee tiers" error
    # because _inspect_provider_route should normalize the route first
    try:
        inspections = aggregator._inspect_route(route_with_fees, 100, "exact_in")
        # Should succeed without ValueError about fee tiers
        assert isinstance(inspections, list)
    except ValueError as e:
        if "fee tiers" in str(e).lower():
            pytest.fail(
                f"_inspect_provider_route should normalize routes before calling to_uniswap_v2_path: {e}"
            )
        raise


def test_inspect_provider_route_handles_v3_route_for_v2_provider():
    """Test that inspection gracefully handles V3 routes for V2 providers."""
    # Create V3-style route
    v3_route = make_route(["0xtoken1", "0xtoken2"], fees=[3000])

    provider = FakeUniswapV2Provider()

    # Mock router to return None (simulating failure)
    provider.router.functions.getAmountsOut = type(
        "Function", (), {"call": lambda: None}
    )()

    aggregator = DexAggregator([provider])

    # Should handle gracefully without raising ValueError
    inspections = aggregator._inspect_provider_route(
        provider, v3_route, 100, "exact_in"
    )
    # Should return a dict with status indicating the issue
    assert isinstance(inspections, dict)
    # Status might be "error" or "no_router" but shouldn't be ValueError about fee tiers
    assert "status" in inspections
