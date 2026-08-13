"""Tests for ArbiDiem buy/burn path with composite routes and fallback behavior."""

from __future__ import annotations

import pytest

from agents.arbi_diem.agent import ArbiDiem
from libs.dex.composite import attach_composite_metadata
from libs.dex.providers import Quote
from libs.dex.routes import make_route
from services.diem.client import DIEMService
from services.risk.policy import RiskPolicy


class CompositeExactOutFailingAggregator:
    """Aggregator that fails on composite exact-out but succeeds on exact-in."""

    def __init__(self):
        self.name = "composite_test"

    def best_quote_exact_out(self, amount_out, route):
        """Fail for composite routes, succeed for simple routes."""
        from libs.dex.composite import is_composite_route

        if is_composite_route(route):
            return None  # Simulate composite exact-out failure
        # Simple route fallback
        plan = route if hasattr(route, "tokens") else make_route(route)
        return Quote(
            provider="uniswap_v2",
            amount_in=int(amount_out * 2),
            amount_out=int(amount_out),
            route=plan,
        )

    def best_quote(self, amount_in, route):
        """Provides exact-in quotes."""
        plan = route if hasattr(route, "tokens") else make_route(route)
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
        """Execute exact-out trade."""
        return {"provider": "uniswap_v2", "tx_hash": "0xexact_out"}


@pytest.fixture
def mock_environment_with_fallback(monkeypatch):
    """Set up test environment with fallback enabled."""
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "25.0")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS", "150")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "120")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "0")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # gitleaks:allow Base mainnet contract
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS",
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # gitleaks:allow Base mainnet contract
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # gitleaks:allow Base mainnet contract
    )


@pytest.fixture
def mock_environment_no_fallback(monkeypatch):
    """Set up test environment without fallback."""
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_ENABLE", "0")
    monkeypatch.setenv("ARBI_DIEM_EXACT_IN_FALLBACK", "0")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "120")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VVV_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "0")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # gitleaks:allow Base mainnet contract
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS",
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # gitleaks:allow Base mainnet contract
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # gitleaks:allow Base mainnet contract
    )


def test_buy_burn_with_composite_exact_out_failure_no_fallback(
    monkeypatch, mock_environment_no_fallback
):
    """Test that buy/burn is skipped when composite exact-out fails and fallback is disabled."""
    aggregator = CompositeExactOutFailingAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    # Create composite route
    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    ]
    route_plan = make_route(route_tokens)
    bridge_legs = [
        {
            "token_in": route_tokens[0],
            "token_out": route_tokens[1],
            "provider": "uniswap_v2",
            "pool_address": "0x1234567890123456789012345678901234567890",
        },
        {
            "token_in": route_tokens[1],
            "token_out": route_tokens[2],
            "provider": "uniswap_v3",
            "fee": 3000,
        },
    ]
    attach_composite_metadata(route_plan, bridge_legs=bridge_legs, is_composite=True)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def price_health(self, symbol):
            if symbol == "DIEM":
                return {
                    "source": "bridge_vvv",
                    "valid": True,
                    "price": 50.0,
                    "provider": "bridge_vvv",
                }
            return {"source": "unknown", "valid": False}

        def reserve_cap_units(self, path, take_bps=None):
            return None

    market = FakeMarket()
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=market)

    # Set trade routes
    diem_service._trade_routes = lambda: [route_plan]

    # Market price is below fair value (discount scenario)
    market_price = 45.0  # Below fair value of ~50.0
    result = arbi.evaluate_and_maybe_mint(
        market_price=market_price,
        mint_rate=1.0,
        desired_units=1000000000000000000,  # 1 DIEM
        simulate=True,
    )

    assert result is False, (
        "Should skip buy/burn when exact-out fails and fallback disabled"
    )
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("decision") == "hold"
    assert rationale.get("reason") == "no_exact_out_preview"
    assert rationale.get("exact_in_fallback") is False
    assert rationale.get("is_composite") is True


def test_buy_burn_with_composite_exact_out_failure_with_fallback(
    monkeypatch, mock_environment_with_fallback
):
    """Test that buy/burn succeeds using exact-in fallback when composite exact-out fails."""
    aggregator = CompositeExactOutFailingAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    # Create composite route
    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    ]
    route_plan = make_route(route_tokens)
    bridge_legs = [
        {
            "token_in": route_tokens[0],
            "token_out": route_tokens[1],
            "provider": "uniswap_v2",
            "pool_address": "0x1234567890123456789012345678901234567890",
        },
        {
            "token_in": route_tokens[1],
            "token_out": route_tokens[2],
            "provider": "uniswap_v3",
            "fee": 3000,
        },
    ]
    attach_composite_metadata(route_plan, bridge_legs=bridge_legs, is_composite=True)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def price_health(self, symbol):
            if symbol == "DIEM":
                return {
                    "source": "bridge_vvv",
                    "valid": True,
                    "price": 50.0,
                    "provider": "bridge_vvv",
                }
            return {"source": "unknown", "valid": False}

        def reserve_cap_units(self, path, take_bps=None):
            return None

    market = FakeMarket()
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=market)

    # Set trade routes
    diem_service._trade_routes = lambda: [route_plan]

    # Market price is below fair value (discount scenario)
    market_price = 45.0  # Below fair value of ~50.0

    result = arbi.evaluate_and_maybe_mint(
        market_price=market_price,
        mint_rate=1.0,
        desired_units=1000000000000000000,  # 1 DIEM
        simulate=True,
    )

    assert result is True, "Should proceed with buy/burn using exact-in fallback"
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("decision") == "buy_burn"
    assert rationale.get("exact_in_fallback") is True
    assert rationale.get("venue") == "exact_in_fallback"


def test_buy_burn_rationale_includes_price_health_and_composite_info(
    monkeypatch, mock_environment_no_fallback
):
    """Test that rationale includes price_health and composite routing info when exact-out fails."""
    aggregator = CompositeExactOutFailingAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
    ]
    route_plan = make_route(route_tokens)
    bridge_legs = [
        {
            "token_in": route_tokens[0],
            "token_out": route_tokens[1],
            "provider": "uniswap_v2",
            "pool_address": "0x1234567890123456789012345678901234567890",
        },
    ]
    attach_composite_metadata(route_plan, bridge_legs=bridge_legs, is_composite=True)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 50.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def price_health(self, symbol):
            if symbol == "DIEM":
                return {
                    "source": "bridge_vvv",
                    "valid": True,
                    "price": 50.0,
                    "provider": "bridge_vvv",
                }
            return {"source": "unknown", "valid": False}

        def reserve_cap_units(self, path, take_bps=None):
            return None

    market = FakeMarket()
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=market)
    diem_service._trade_routes = lambda: [route_plan]

    market_price = 45.0
    arbi.evaluate_and_maybe_mint(
        market_price=market_price,
        mint_rate=1.0,
        desired_units=1000000000000000000,
        simulate=True,
    )

    rationale = getattr(arbi, "_last_rationale", {})
    assert "price_health" in rationale
    assert rationale["price_health"]["source"] == "bridge_vvv"
    assert rationale.get("is_composite") is True
    assert "trade_route_meta" in rationale or rationale.get("tradeRoute") is not None


def test_composite_exact_in_fallback_blocks_on_slippage(
    monkeypatch, mock_environment_with_fallback
):
    """Composite exact-in fallback should respect slippage cap and hold in live mode."""

    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("DIEM_COMPOSITE_MAX_SLIPPAGE_BPS", "50")

    class HighSlippageCompositeAggregator:
        def __init__(self):
            self.trade_calls = 0

        def best_quote_exact_out(self, amount_out, route):
            from libs.dex.composite import is_composite_route

            if is_composite_route(route):
                return None  # Force fallback path
            plan = route if hasattr(route, "tokens") else make_route(route)
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_out),
                amount_out=int(amount_out),
                route=plan,
            )

        def best_quote(self, amount_in, route):
            plan = route if hasattr(route, "tokens") else make_route(route)
            target_price = 1.3  # Force >50 bps slippage vs $1 ref
            amount_out = int(amount_in * (10**12) / target_price)
            return Quote(
                provider="uniswap_v2",
                amount_in=int(amount_in),
                amount_out=int(amount_out),
                route=plan,
            )

        def trade_best(self, amount_in, slippage_bps, route):
            self.trade_calls += 1
            return {"provider": "uniswap_v2", "tx_hash": "0xdead"}

        def trade_best_exact_out(self, amount_out, max_in_bps, route):
            self.trade_calls += 1
            return {"provider": "uniswap_v2", "tx_hash": "0xbeef"}

    aggregator = HighSlippageCompositeAggregator()
    diem_service = DIEMService(aggregator=aggregator)
    risk = RiskPolicy.from_env()

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = make_route(route_tokens)
    bridge_legs = [
        {
            "token_in": route_tokens[0],
            "token_out": route_tokens[1],
            "provider": "uniswap_v2",
            "pool_address": "0x1234567890123456789012345678901234567890",
        },
        {
            "token_in": route_tokens[1],
            "token_out": route_tokens[2],
            "provider": "uniswap_v3",
            "fee": 3000,
        },
    ]
    attach_composite_metadata(route_plan, bridge_legs=bridge_legs, is_composite=True)

    class FakeMarket:
        def prices(self, symbols):
            return {"DIEM": 1.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def price_health(self, symbol):
            if symbol == "DIEM":
                return {
                    "source": "aggregator",
                    "valid": True,
                    "price": 1.0,
                    "provider": "dex",
                }
            return {"source": "unknown", "valid": False}

        def reserve_cap_units(self, path, take_bps=None):
            return None

    market = FakeMarket()
    arbi = ArbiDiem(diem=diem_service, risk=risk, market=market)
    diem_service._trade_routes = lambda: [route_plan]

    result = arbi.evaluate_and_maybe_mint(
        market_price=1.0,
        mint_rate=1.0,
        desired_units=1000000000000000000,  # 1 DIEM
        simulate=False,
    )

    assert result is False, "High-slippage composite fallback should hold in live mode"
    rationale = getattr(arbi, "_last_rationale", {})
    assert rationale.get("decision") == "hold"
    assert getattr(arbi, "_last_exact_in_fallback_reason", None) == "slippage_exceeded"
    assert aggregator.trade_calls == 0
