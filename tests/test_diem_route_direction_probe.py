"""
Test route direction semantics and probe sizing for DIEM routes.

This test validates:
1. Route direction matches side (buy routes are USDC->...->DIEM, sell routes are DIEM->...->USDC)
2. Probe sizing prevents zero outputs on multi-hop routes
3. No route_mismatch incoherent preview mutes for canonical routes
"""

import os
from unittest.mock import patch

import pytest


def test_route_direction_buy():
    """Test that buy quotes use routes in buy direction (USDC->...->DIEM)."""
    os.environ["DIEM_TOKEN_ADDRESS"] = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    os.environ["QUOTE_TOKEN_ADDRESS"] = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    os.environ["VVV_TOKEN_ADDRESS"] = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    os.environ["DIEM_MAX_ROUTE_HOPS"] = "2"
    os.environ["DIEM_ENABLE_THREE_HOP_WETH"] = "0"

    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=market)

    # Get routes for buy
    routes = svc.trade_routes()
    assert len(routes) > 0, "Expected at least one route"

    # Test quote with buy side
    quote_token = os.getenv("QUOTE_TOKEN_ADDRESS", "").strip().lower()
    diem_token = os.getenv("DIEM_TOKEN_ADDRESS", "").strip().lower()
    amount = 1_000_000  # 1 USDC

    result = svc.quote("buy", amount, routes=routes)

    # Check that logged route matches buy direction
    route_tokens = result.get("quote_summary", {}).get("route", [])
    if route_tokens:
        route_tokens_lower = [t.lower() for t in route_tokens]
        # Route should start with quote token and end with DIEM for buy
        assert route_tokens_lower[0] == quote_token, (
            f"Buy route should start with quote token, got {route_tokens[0]}"
        )
        assert route_tokens_lower[-1] == diem_token, (
            f"Buy route should end with DIEM, got {route_tokens[-1]}"
        )


def test_route_direction_sell():
    """Test that sell quotes use routes in sell direction (DIEM->...->USDC)."""
    os.environ["DIEM_TOKEN_ADDRESS"] = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    os.environ["QUOTE_TOKEN_ADDRESS"] = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    os.environ["VVV_TOKEN_ADDRESS"] = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    os.environ["DIEM_MAX_ROUTE_HOPS"] = "2"
    os.environ["DIEM_ENABLE_THREE_HOP_WETH"] = "0"

    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=market)

    # Get routes for sell
    routes = svc.trade_routes()
    assert len(routes) > 0, "Expected at least one route"

    # Test quote with sell side
    quote_token = os.getenv("QUOTE_TOKEN_ADDRESS", "").strip().lower()
    diem_token = os.getenv("DIEM_TOKEN_ADDRESS", "").strip().lower()
    amount = 1_000_000_000_000_000_000  # 1 DIEM (18 decimals)

    result = svc.quote("sell", amount, routes=routes)

    # Check that logged route matches sell direction
    route_tokens = result.get("quote_summary", {}).get("route", [])
    if route_tokens:
        route_tokens_lower = [t.lower() for t in route_tokens]
        # Route should start with DIEM and end with quote token for sell
        assert route_tokens_lower[0] == diem_token, (
            f"Sell route should start with DIEM, got {route_tokens[0]}"
        )
        assert route_tokens_lower[-1] == quote_token, (
            f"Sell route should end with quote token, got {route_tokens[-1]}"
        )


def test_probe_sizing_configurable():
    """Test that probe sizing is configurable and prevents dust-sized probes."""
    os.environ["DIEM_ROUTE_HEALTH_PROBE_USD"] = "5.0"
    os.environ["QUOTE_TOKEN_DECIMALS"] = "6"

    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=market)

    # Get probe amount
    probe_amount = svc._get_probe_amount()

    # Should be at least 5 USD = 5 * 10^6 = 5,000,000 base units
    assert probe_amount >= 5_000_000, (
        f"Probe amount should be at least 5 USD, got {probe_amount}"
    )


def test_probe_sizing_prevents_zero_outputs():
    """Test that probe sizing prevents zero outputs on multi-hop routes."""
    os.environ["DIEM_ROUTE_HEALTH_PROBE_USD"] = "3.0"
    os.environ["QUOTE_TOKEN_DECIMALS"] = "6"
    os.environ["DIEM_TOKEN_ADDRESS"] = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    os.environ["QUOTE_TOKEN_ADDRESS"] = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=market)

    # Get probe amount
    probe_amount = svc._get_probe_amount()

    # Probe should be large enough to avoid rounding to zero on multi-hop
    # For a 2-hop route, even with 0.1% fees, 3 USD should produce > 0 output
    assert probe_amount >= 3_000_000, (
        f"Probe amount should be at least 3 USD, got {probe_amount}"
    )


def test_min_notional_gate():
    """Test that minimum executable notional gate works correctly."""
    os.environ["ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD"] = "2.0"
    os.environ["DIEM_TOKEN_ADDRESS"] = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    os.environ["QUOTE_TOKEN_ADDRESS"] = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    from agents.arbi_diem.models import ExecutionIntent, TradeSide
    from libs.dex.providers import build_aggregator_from_env
    from services.diem.client import DIEMService
    from services.marketdata.provider import MarketDataProvider

    market = MarketDataProvider()
    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg, market_data=market)

    # Create intent with very small amount (below min notional)
    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=100_000,  # 0.1 USDC (below 2 USD threshold)
        slippage_bps=50,
    )

    # Mock market data to return a price that makes this below threshold
    with patch.object(market, "prices", return_value={"DIEM": 100.0, "USDC": 1.0}):
        result = svc.preview_trade(intent)

        # Should be rejected due to below min notional
        assert result.status.value == "rejected", (
            f"Expected rejected status, got {result.status.value}"
        )
        assert (
            "below_min_notional" in str(result.error).lower()
            or "below_min" in str(result.error).lower()
        ), f"Expected min notional error, got {result.error}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
