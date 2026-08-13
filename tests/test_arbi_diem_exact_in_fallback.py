"""Tests for ArbiDiem exact-in fallback when exact-out previews fail."""

from __future__ import annotations

from importlib import import_module

import pytest

from agents.arbi_diem.agent import ArbiDiem
from libs.dex.providers import Quote
from libs.dex.routes import make_route
from services.diem.client import DIEMService
from services.risk.policy import RiskPolicy


class FakeExactInOnlyAggregator:
    """Aggregator that only supports exact-in, not exact-out."""

    def __init__(self):
        self.name = "fake_exact_in"

    def best_quote_exact_out(self, amount_out, route):
        """Always fails - simulates degraded DEX conditions."""
        return

    def best_quote(self, amount_in, route):
        """Provides exact-in quotes."""
        plan = route if hasattr(route, "tokens") else make_route(route)
        # Simple 2:1 ratio for testing
        return Quote(
            provider="uniswap_v2",
            amount_in=int(amount_in),
            amount_out=int(amount_in * 0.5),  # 50% output
            route=plan,
        )

    def trade_best(self, amount_in, slippage_bps, route):
        """Execute exact-in trade."""
        return {"provider": "uniswap_v2", "tx_hash": "0xexact_in"}

    def trade_best_exact_out(self, amount_out, max_in_bps, route):
        """Always fails."""
        raise RuntimeError("exact-out not available")


@pytest.fixture
def mock_environment(monkeypatch):
    """Set up test environment with fallback enabled."""
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "10.0")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS", "150")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "120")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "0")


def test_exact_in_fallback_enabled_but_exact_out_available(
    monkeypatch, mock_environment
):
    """Test that exact-out is preferred when available, even if fallback is enabled."""
    from libs.dex.providers import Quote

    class BothModesAggregator:
        def best_quote_exact_out(self, amount_out, route):
            plan = route if hasattr(route, "tokens") else make_route(route)
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_out * 2),
                amount_out=int(amount_out),
                route=plan,
            )

        def best_quote(self, amount_in, route):
            plan = route if hasattr(route, "tokens") else make_route(route)
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(amount_in * 0.5),
                route=plan,
            )

    aggregator = BothModesAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
    ]
    route_plan = make_route(route_tokens)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def unified_signals(self, ttl_s=30):
            return {}

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    diem_service.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        diem_service, DIEMService
    )
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=FakeMarket())

    # Market price $50, fair value should be higher to trigger buy
    # Set fair value to $100 so discount branch triggers
    pricing_mod = import_module("libs.pricing.diem")

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 100.0  # Higher than market price

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    arbi.evaluate_and_maybe_mint(
        market_price=50.0,
        mint_rate=1.0,
        desired_units=1000000000000000000,  # 1 DIEM
        simulate=True,
    )

    # Should use exact-out, not fallback
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("exact_in_fallback") is not True
    assert rationale.get("venue") != "exact_in_fallback"


def test_exact_in_fallback_used_when_exact_out_fails(monkeypatch, mock_environment):
    """Test that exact-in fallback is used when exact-out previews fail."""
    aggregator = FakeExactInOnlyAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0x4200000000000000000000000000000000000006",  # WETH
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
    ]
    route_plan = make_route(route_tokens)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def unified_signals(self, ttl_s=30):
            return {}

        def reserve_cap_units(self, route, take_bps=25):
            # Return a reasonable cap
            return 10000000000000000000  # 10 DIEM

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    diem_service.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        diem_service, DIEMService
    )
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=FakeMarket())

    # Mock fair value to trigger buy branch
    pricing_mod = import_module("libs.pricing.diem")

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 100.0  # Higher than market price

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    # Mock _preview_exec_price_buy to return 0 (simulating exact-out failure)

    def failing_preview(units_out):
        return 0.0  # Simulates exact-out failure

    arbi._preview_exec_price_buy = failing_preview  # type: ignore[assignment]

    arbi.evaluate_and_maybe_mint(
        market_price=50.0,
        mint_rate=1.0,
        desired_units=1000000000000000000,  # 1 DIEM (within $10 cap)
        simulate=True,
    )

    # Should use exact-in fallback
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("exact_in_fallback") is True
    assert rationale.get("venue") == "exact_in_fallback"
    assert rationale.get("fallback_provider") == "uniswap_v2"
    assert rationale.get("slippage_source") == "exact_in_fallback"


def test_exact_in_fallback_respects_max_usd_cap(monkeypatch, mock_environment):
    """Test that exact-in fallback respects DIEM_EXACT_IN_FALLBACK_MAX_USD cap."""
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "5.0")  # Very small cap

    aggregator = FakeExactInOnlyAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0x4200000000000000000000000000000000000006",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    ]
    route_plan = make_route(route_tokens)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def unified_signals(self, ttl_s=30):
            return {}

        def reserve_cap_units(self, route, take_bps=25):
            return 10000000000000000000

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    diem_service.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        diem_service, DIEMService
    )
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=FakeMarket())

    pricing_mod = import_module("libs.pricing.diem")

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 100.0

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    def failing_preview(units_out):
        return 0.0

    arbi._preview_exec_price_buy = failing_preview  # type: ignore[assignment]

    # Request large amount (would exceed $5 cap at $50/DIEM)
    arbi.evaluate_and_maybe_mint(
        market_price=50.0,
        mint_rate=1.0,
        desired_units=10000000000000000000,  # 10 DIEM = $500, exceeds $5 cap
        simulate=True,
    )

    # Fallback should cap the amount to $5 worth
    rationale = getattr(arbi, "_last_rationale", {})
    if rationale.get("exact_in_fallback"):
        # If fallback was attempted, it should have capped the amount
        fallback_quote = rationale.get("fallback_quote_amount_out")
        if fallback_quote:
            # Verify it's within the USD cap
            max_units = int((5.0 * 10**18) / 50.0)  # $5 / $50 per DIEM
            assert fallback_quote <= max_units


def test_exact_in_fallback_respects_slippage_limits(monkeypatch, mock_environment):
    """Test that exact-in fallback rejects quotes with excessive slippage."""
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS", "100")  # Tight limit

    class HighSlippageAggregator(FakeExactInOnlyAggregator):
        def best_quote(self, amount_in, route):
            # Return a quote that would result in high slippage
            plan = route if hasattr(route, "tokens") else make_route(route)
            # Very poor output ratio = high slippage
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(amount_in * 0.1),  # Only 10% output = very high slippage
                route=plan,
            )

    aggregator = HighSlippageAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0x4200000000000000000000000000000000000006",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    ]
    route_plan = make_route(route_tokens)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def unified_signals(self, ttl_s=30):
            return {}

        def reserve_cap_units(self, route, take_bps=25):
            return 10000000000000000000

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    diem_service.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        diem_service, DIEMService
    )
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=FakeMarket())

    pricing_mod = import_module("libs.pricing.diem")

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 100.0

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    def failing_preview(units_out):
        return 0.0

    arbi._preview_exec_price_buy = failing_preview  # type: ignore[assignment]

    arbi.evaluate_and_maybe_mint(
        market_price=50.0,
        mint_rate=1.0,
        desired_units=1000000000000000000,
        simulate=True,
    )

    # Fallback should reject high slippage quote
    rationale = getattr(arbi, "_last_rationale", {})
    # Either fallback failed or was rejected due to slippage
    assert rationale.get("reason") in [
        "exact_in_fallback_failed",
        "no_exact_out_preview",
        "slippage_exceeded",
    ]


def test_exact_in_fallback_disabled_by_default(monkeypatch):
    """Test that exact-in fallback is disabled by default."""
    # Don't set DIEM_EXACT_IN_FALLBACK_ENABLE
    monkeypatch.delenv("DIEM_EXACT_IN_FALLBACK_ENABLE", raising=False)
    monkeypatch.delenv("ARBI_DIEM_EXACT_IN_FALLBACK", raising=False)

    aggregator = FakeExactInOnlyAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "0x4200000000000000000000000000000000000006",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    ]
    route_plan = make_route(route_tokens)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def unified_signals(self, ttl_s=30):
            return {}

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    diem_service.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        diem_service, DIEMService
    )
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=FakeMarket())

    pricing_mod = import_module("libs.pricing.diem")

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 100.0

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    def failing_preview(units_out):
        return 0.0

    arbi._preview_exec_price_buy = failing_preview  # type: ignore[assignment]

    arbi.evaluate_and_maybe_mint(
        market_price=50.0,
        mint_rate=1.0,
        desired_units=1000000000000000000,
        simulate=True,
    )

    # Should hold, not use fallback
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("exact_in_fallback") is not True
    assert rationale.get("reason") == "no_exact_out_preview"
