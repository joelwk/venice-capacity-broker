"""Tests for composite multi-venue route quoting."""

from __future__ import annotations

import math
import os
from unittest.mock import Mock, patch

import pytest

from libs.dex.composite import (
    CompositeQuote,
    attach_composite_metadata,
    is_composite_route,
    quote_composite_exact_in,
    quote_composite_exact_out,
)
from libs.dex.providers import DexAggregator, DexProvider, Quote
from libs.dex.routes import make_route


class MockV2Provider(DexProvider):
    """Mock Uniswap V2 provider for testing."""

    name = "uniswap_v2"
    supports_exact_out = True

    def quote(self, amount_in: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        # Simple 2:1 ratio for testing
        return Quote(
            provider=self.name,
            amount_in=amount_in,
            amount_out=amount_in // 2,
            route=plan,
        )

    def quote_exact_out(self, amount_out: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        # Simple 2:1 ratio for testing
        return Quote(
            provider=self.name,
            amount_in=amount_out * 2,
            amount_out=amount_out,
            route=plan,
        )

    def trade(self, amount_in: int, min_amount_out: int, route):
        raise NotImplementedError

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route):
        raise NotImplementedError


class MockV3Provider(DexProvider):
    """Mock Uniswap V3 provider for testing."""

    name = "uniswap_v3"
    supports_exact_out = True

    def quote(self, amount_in: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        # Simple 1.5:1 ratio for testing
        return Quote(
            provider=self.name,
            amount_in=amount_in,
            amount_out=int(amount_in * 1.5),
            route=plan,
        )

    def quote_exact_out(self, amount_out: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        # Simple 1.5:1 ratio for testing
        return Quote(
            provider=self.name,
            amount_in=int(amount_out * 1.5),
            amount_out=amount_out,
            route=plan,
        )

    def trade(self, amount_in: int, min_amount_out: int, route):
        raise NotImplementedError

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route):
        raise NotImplementedError


class ImpactProvider(DexProvider):
    """Provider that introduces predictable price impact to test slippage."""

    name = "impact"
    supports_exact_out = True
    _scale = 10_000_000  # higher = gentler impact

    def _impact_ratio(self, amount: int) -> float:
        if amount <= 0:
            return 0.0
        return min(0.5, float(amount) / float(self._scale))

    def quote(self, amount_in: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        impact = self._impact_ratio(amount_in)
        out = int(max(1.0, amount_in * (1.0 - impact)))
        return Quote(
            provider=self.name,
            amount_in=amount_in,
            amount_out=out,
            route=plan,
        )

    def quote_exact_out(self, amount_out: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        impact = self._impact_ratio(amount_out)
        effective = max(1e-9, 1.0 - impact)
        amt_in = int(math.ceil(amount_out / effective))
        return Quote(
            provider=self.name,
            amount_in=amt_in,
            amount_out=amount_out,
            route=plan,
        )

    def trade(self, amount_in: int, min_amount_out: int, route):
        raise NotImplementedError

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route):
        raise NotImplementedError


def test_is_composite_route():
    """Test composite route detection."""
    route = make_route(["DIEM", "VVV", "USDC"])
    assert not is_composite_route(route)

    # Mark as composite
    attach_composite_metadata(route, is_composite=True)
    assert is_composite_route(route)

    # With bridge legs
    attach_composite_metadata(
        route,
        bridge_legs=[
            {
                "token_in": "DIEM",
                "token_out": "VVV",
                "provider": "uniswap_v2",
            },
            {
                "token_in": "VVV",
                "token_out": "USDC",
                "provider": "uniswap_v3",
            },
        ],
    )
    assert is_composite_route(route)


def test_quote_composite_exact_in():
    """Test composite exact-in quoting."""
    v2_provider = MockV2Provider()
    v3_provider = MockV3Provider()
    aggregator = DexAggregator([v2_provider, v3_provider])

    route = make_route(["DIEM", "VVV", "USDC"])
    bridge_legs = [
        {
            "token_in": "DIEM",
            "token_out": "VVV",
            "provider": "uniswap_v2",
            "pool_address": "0xpair1",
        },
        {
            "token_in": "VVV",
            "token_out": "USDC",
            "provider": "uniswap_v3",
            "pool_address": "0xpool1",
        },
    ]

    quote = quote_composite_exact_in(aggregator, route, 1000, bridge_legs=bridge_legs)

    assert quote is not None
    assert isinstance(quote, CompositeQuote)
    assert quote.amount_in == 1000
    assert len(quote.legs) == 2
    assert quote.legs[0].provider == "uniswap_v2"
    assert quote.legs[1].provider == "uniswap_v3"
    # First leg: 1000 -> 500 (V2 2:1 ratio)
    # Second leg: 500 -> 750 (V3 1.5:1 ratio)
    assert quote.amount_out == 750


def test_quote_composite_exact_out():
    """Test composite exact-out quoting."""
    v2_provider = MockV2Provider()
    v3_provider = MockV3Provider()
    aggregator = DexAggregator([v2_provider, v3_provider])

    route = make_route(["USDC", "VVV", "DIEM"])  # Reversed for buy
    bridge_legs = [
        {
            "token_in": "USDC",
            "token_out": "VVV",
            "provider": "uniswap_v3",
            "pool_address": "0xpool1",
        },
        {
            "token_in": "VVV",
            "token_out": "DIEM",
            "provider": "uniswap_v2",
            "pool_address": "0xpair1",
        },
    ]

    quote = quote_composite_exact_out(aggregator, route, 1000, bridge_legs=bridge_legs)

    assert quote is not None
    assert isinstance(quote, CompositeQuote)
    assert quote.amount_out == 1000
    assert len(quote.legs) == 2
    # Work backwards: need 1000 DIEM
    # Second leg (VVV->DIEM): need 2000 VVV (V2 2:1)
    # First leg (USDC->VVV): need 3000 USDC (V3 1.5:1)
    assert quote.amount_in == 3000


def test_quote_composite_missing_provider():
    """Test composite quote fails when provider is missing."""
    v2_provider = MockV2Provider()
    aggregator = DexAggregator([v2_provider])  # Missing V3

    route = make_route(["DIEM", "VVV", "USDC"])
    bridge_legs = [
        {
            "token_in": "DIEM",
            "token_out": "VVV",
            "provider": "uniswap_v2",
        },
        {
            "token_in": "VVV",
            "token_out": "USDC",
            "provider": "uniswap_v3",  # Not in aggregator
        },
    ]

    quote = quote_composite_exact_in(aggregator, route, 1000, bridge_legs=bridge_legs)
    assert quote is None


def test_quote_composite_leg_quote_failure():
    """Test composite quote fails when a leg quote fails."""
    failing_provider = Mock()
    failing_provider.name = "uniswap_v2"
    failing_provider.quote = Mock(return_value=None)

    aggregator = DexAggregator([failing_provider])

    route = make_route(["DIEM", "VVV", "USDC"])
    bridge_legs = [
        {
            "token_in": "DIEM",
            "token_out": "VVV",
            "provider": "uniswap_v2",
        },
    ]

    quote = quote_composite_exact_in(aggregator, route, 1000, bridge_legs=bridge_legs)
    assert quote is None


def test_aggregator_uses_composite_for_bridge_route():
    """Test that aggregator automatically uses composite quoter for bridge routes."""
    v2_provider = MockV2Provider()
    v3_provider = MockV3Provider()
    aggregator = DexAggregator([v2_provider, v3_provider])

    route = make_route(["DIEM", "VVV", "USDC"])
    bridge_legs = [
        {
            "token_in": "DIEM",
            "token_out": "VVV",
            "provider": "uniswap_v2",
        },
        {
            "token_in": "VVV",
            "token_out": "USDC",
            "provider": "uniswap_v3",
        },
    ]
    attach_composite_metadata(route, bridge_legs=bridge_legs, is_composite=True)

    # Test exact-out (buy DIEM)
    rev_route = route.reversed()
    # Preserve metadata
    attach_composite_metadata(
        rev_route,
        bridge_legs=list(reversed(bridge_legs)),
        is_composite=True,
    )

    quote = aggregator.best_quote_exact_out(1000, rev_route)
    assert quote is not None
    assert quote.provider == "composite"


def _set_diem_env(monkeypatch):
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    )
    monkeypatch.setenv(
        "VVV_USDC_POOL_ADDRESS", "0x67a11022b7b6ed66f81233f6c8ed6e48f7826530"
    )
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "uniswap_v2")


def test_best_quote_uses_three_token_composite(monkeypatch):
    """Ensure best_quote builds a three-token DIEM bridge route."""
    _set_diem_env(monkeypatch)

    v2_provider = MockV2Provider()
    v3_provider = MockV3Provider()
    aggregator = DexAggregator([v2_provider, v3_provider])

    diem = os.getenv("DIEM_TOKEN_ADDRESS")
    usdc = os.getenv("QUOTE_TOKEN_ADDRESS")
    assert diem and usdc

    route = make_route([diem, usdc])
    from libs.dex import composite as composite_module

    captured_tokens = []
    original_fn = composite_module.quote_composite_exact_in

    def spy(agg, route_plan, amount_in, *, bridge_legs=None):
        captured_tokens.append(tuple(route_plan.tokens))
        return original_fn(agg, route_plan, amount_in, bridge_legs=bridge_legs)

    with patch.object(composite_module, "quote_composite_exact_in", side_effect=spy):
        quote = aggregator.best_quote(1_000, route)

    assert quote is not None
    assert quote.provider == "diem_composite"
    assert captured_tokens and len(captured_tokens[0]) == 3


def test_best_quote_exact_out_uses_three_token_composite(monkeypatch):
    """Ensure best_quote_exact_out builds a three-token DIEM bridge route."""
    _set_diem_env(monkeypatch)

    v2_provider = MockV2Provider()
    v3_provider = MockV3Provider()
    aggregator = DexAggregator([v2_provider, v3_provider])

    diem = os.getenv("DIEM_TOKEN_ADDRESS")
    usdc = os.getenv("QUOTE_TOKEN_ADDRESS")
    assert diem and usdc

    route = make_route([diem, usdc])
    from libs.dex import composite as composite_module

    captured_tokens = []
    original_fn = composite_module.quote_composite_exact_out

    def spy(agg, route_plan, amount_out, *, bridge_legs=None):
        captured_tokens.append(tuple(route_plan.tokens))
        return original_fn(agg, route_plan, amount_out, bridge_legs=bridge_legs)

    with patch.object(composite_module, "quote_composite_exact_out", side_effect=spy):
        quote = aggregator.best_quote_exact_out(1_000, route)

    assert quote is not None
    assert quote.provider == "diem_composite"
    assert captured_tokens and len(captured_tokens[0]) == 3


def test_aggregator_composite_disabled():
    """Test that composite routing can be disabled via env var."""
    v2_provider = MockV2Provider()
    aggregator = DexAggregator([v2_provider])

    route = make_route(["DIEM", "VVV", "USDC"])
    attach_composite_metadata(
        route,
        bridge_legs=[{"provider": "uniswap_v2"}],
        is_composite=True,
    )

    with patch.dict(os.environ, {"DEX_COMPOSITE_ENABLE": "0"}):
        quote = aggregator.best_quote_exact_out(1000, route.reversed())
        # Should fall back to regular quoting (which may fail)
        # We just verify it doesn't use composite
        if quote:
            assert quote.provider != "composite"


def _build_composite_quote():
    route = make_route(["A", "B", "C"])
    leg1 = Quote(
        provider="uniswap_v2",
        amount_in=1_000,
        amount_out=900,
        route=make_route(["A", "B"]),
    )
    leg2 = Quote(
        provider="uniswap_v3",
        amount_in=900,
        amount_out=800,
        route=make_route(["B", "C"]),
    )
    composite = Quote(
        provider="composite",
        amount_in=1_000,
        amount_out=800,
        route=route,
    )
    object.__setattr__(composite, "_composite_legs", [leg1, leg2])
    object.__setattr__(composite, "_composite_mode", "exact_out")
    return composite, route


def test_trade_best_exact_out_executes_composite_legs():
    """trade_best_exact_out should execute underlying composite legs."""
    p1 = Mock()
    p1.name = "uniswap_v2"
    p1.supports_exact_out = True
    p1.trade_exact_out = Mock(return_value={"tx_hash": "0xleg1"})

    p2 = Mock()
    p2.name = "uniswap_v3"
    p2.supports_exact_out = True
    p2.trade_exact_out = Mock(return_value={"tx_hash": "0xleg2"})

    aggregator = DexAggregator([p1, p2])
    composite_quote, route = _build_composite_quote()

    with patch.object(aggregator, "best_quote_exact_out", return_value=composite_quote):
        res = aggregator.trade_best_exact_out(800, max_in_bps=50, route=route)

    assert res["provider"] == "composite"
    assert p1.trade_exact_out.called
    assert p2.trade_exact_out.called


def test_trade_best_exact_out_fallbacks_when_leg_reverts():
    """Composite exact-out execution should fall back to exact-in on leg failure."""
    p1 = Mock()
    p1.name = "uniswap_v2"
    p1.supports_exact_out = True
    p1.trade_exact_out = Mock(return_value={"tx_hash": "0xleg1"})

    p2 = Mock()
    p2.name = "uniswap_v3"
    p2.supports_exact_out = True
    p2.trade_exact_out = Mock(side_effect=RuntimeError("revert"))
    p2.trade = Mock(return_value={"tx_hash": "0xleg2_fallback"})

    aggregator = DexAggregator([p1, p2])
    composite_quote, route = _build_composite_quote()

    with patch.object(aggregator, "best_quote_exact_out", return_value=composite_quote):
        res = aggregator.trade_best_exact_out(800, max_in_bps=50, route=route)

    assert res["provider"] == "composite"
    assert p1.trade_exact_out.called
    assert p2.trade_exact_out.called
    assert p2.trade.called


def test_composite_exact_out_preflights_allowances_and_injects_before_leg1():
    events: list[str] = []

    p1 = Mock()
    p1.name = "uniswap_v2"
    p1.supports_exact_out = True
    p1.router_addr = "0x" + "1" * 40
    p1.trade_exact_out = Mock(
        side_effect=lambda *args, **kwargs: events.append("trade1")
        or {"tx_hash": "0xleg1"}
    )

    p2 = Mock()
    p2.name = "uniswap_v3"
    p2.supports_exact_out = True
    p2.router_addr = "0x" + "2" * 40
    p2._ensure_allowance = Mock(
        side_effect=lambda *args, **kwargs: events.append("approve2")
        or ("0x" + "a" * 64)
    )
    p2.trade_exact_out = Mock(
        side_effect=lambda *args, **kwargs: events.append("trade2")
        or {"tx_hash": "0xleg2"}
    )

    aggregator = DexAggregator([p1, p2])
    composite_quote, route = _build_composite_quote()

    token_b = composite_quote._composite_legs[1].route.tokens[0]

    def _allowance(token: str, owner: str, spender: str) -> int | None:
        # Force only the second leg to fail the allowance check.
        if token == token_b and spender == p2.router_addr:
            return 0
        return 10**18

    with (
        patch.object(aggregator, "best_quote_exact_out", return_value=composite_quote),
        patch.object(aggregator, "_erc20_allowance", side_effect=_allowance),
    ):
        res = aggregator.trade_best_exact_out(800, max_in_bps=50, route=route)

    assert res["provider"] == "composite"
    assert "approve2" in events
    assert events.index("approve2") < events.index("trade1")


def test_composite_exact_in_preflights_allowances_and_injects_before_leg1():
    events: list[str] = []

    p1 = Mock()
    p1.name = "uniswap_v2"
    p1.router_addr = "0x" + "1" * 40
    p1.trade = Mock(
        side_effect=lambda *args, **kwargs: events.append("trade1")
        or {"tx_hash": "0xleg1"}
    )

    p2 = Mock()
    p2.name = "uniswap_v3"
    p2.router_addr = "0x" + "2" * 40
    p2._ensure_allowance = Mock(
        side_effect=lambda *args, **kwargs: events.append("approve2")
        or ("0x" + "a" * 64)
    )
    p2.trade = Mock(
        side_effect=lambda *args, **kwargs: events.append("trade2")
        or {"tx_hash": "0xleg2"}
    )

    aggregator = DexAggregator([p1, p2])
    composite_quote, route = _build_composite_quote()

    token_b = composite_quote._composite_legs[1].route.tokens[0]

    def _allowance(token: str, owner: str, spender: str) -> int | None:
        if token == token_b and spender == p2.router_addr:
            return 0
        return 10**18

    with (
        patch.object(aggregator, "best_quote", return_value=composite_quote),
        patch.object(aggregator, "_erc20_allowance", side_effect=_allowance),
    ):
        res = aggregator.trade_best(1_000, min_out_bps=50, route=route)

    assert res["provider"] == "composite"
    assert "approve2" in events
    assert events.index("approve2") < events.index("trade1")


def test_composite_allowance_preflight_checksums_token_in():
    """Composite preflight should checksum token addresses before approvals."""
    from web3 import Web3  # type: ignore

    events: list[str] = []

    token_a = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    token_b = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    token_c = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"

    leg1 = Quote(
        provider="uniswap_v2",
        amount_in=1_000,
        amount_out=900,
        route=make_route([token_a, token_b]),
    )
    leg2 = Quote(
        provider="uniswap_v3",
        amount_in=900,
        amount_out=800,
        route=make_route([token_b, token_c]),
    )
    route = make_route([token_a, token_b, token_c])
    composite = Quote(
        provider="composite", amount_in=1_000, amount_out=800, route=route
    )
    object.__setattr__(composite, "_composite_legs", [leg1, leg2])
    object.__setattr__(composite, "_composite_mode", "exact_out")

    p1 = Mock()
    p1.name = "uniswap_v2"
    p1.supports_exact_out = True
    p1.router_addr = "0x" + "1" * 40
    p1.trade_exact_out = Mock(
        side_effect=lambda *args, **kwargs: events.append("trade1")
        or {"tx_hash": "0xleg1"}
    )

    p2 = Mock()
    p2.name = "uniswap_v3"
    p2.supports_exact_out = True
    p2.router_addr = "0x" + "2" * 40
    p2._ensure_allowance = Mock(
        side_effect=lambda *args, **kwargs: events.append("approve2")
        or ("0x" + "a" * 64)
    )
    p2.trade_exact_out = Mock(
        side_effect=lambda *args, **kwargs: events.append("trade2")
        or {"tx_hash": "0xleg2"}
    )

    aggregator = DexAggregator([p1, p2])

    token_b_checksum = Web3.to_checksum_address(token_b)

    def _allowance(token: str, owner: str, spender: str) -> int | None:
        if token == token_b_checksum and spender == p2.router_addr:
            return 0
        return 10**18

    with (
        patch.object(aggregator, "best_quote_exact_out", return_value=composite),
        patch.object(aggregator, "_erc20_allowance", side_effect=_allowance),
    ):
        res = aggregator.trade_best_exact_out(800, max_in_bps=50, route=route)

    assert res["provider"] == "composite"
    assert "approve2" in events
    assert p2._ensure_allowance.called
    assert p2._ensure_allowance.call_args[0][0] == token_b_checksum


def test_composite_slippage_exact_in_uses_leg_impact():
    """Composite exact-in slippage should reflect price impact across legs."""
    provider = ImpactProvider()
    aggregator = DexAggregator([provider])

    route = make_route(["0xaaa", "0xbbb", "0xccc"])
    bridge_legs = [
        {"token_in": "0xaaa", "token_out": "0xbbb", "provider": provider.name},
        {"token_in": "0xbbb", "token_out": "0xccc", "provider": provider.name},
    ]

    quote = quote_composite_exact_in(
        aggregator, route, 20_000_000, bridge_legs=bridge_legs
    )

    assert quote is not None
    assert quote.total_slippage_bps > 0
    # With two impacted legs, slippage should be material (> 1000 bps)
    assert quote.total_slippage_bps > 1_000


def test_composite_slippage_exact_out_uses_leg_impact():
    """Composite exact-out slippage should reflect input increase from impact."""
    provider = ImpactProvider()
    aggregator = DexAggregator([provider])

    route = make_route(["0xccc", "0xbbb", "0xaaa"])
    bridge_legs = [
        {"token_in": "0xccc", "token_out": "0xbbb", "provider": provider.name},
        {"token_in": "0xbbb", "token_out": "0xaaa", "provider": provider.name},
    ]

    quote = quote_composite_exact_out(
        aggregator, route, 5_000_000, bridge_legs=bridge_legs
    )

    assert quote is not None
    assert quote.total_slippage_bps > 0
    assert quote.total_slippage_bps > 5_000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
