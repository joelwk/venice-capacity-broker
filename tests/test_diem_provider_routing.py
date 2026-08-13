"""Tests for DIEM provider routing correctness.

Verifies that V2-compatible routes only attempt UniswapV2,
and V3-only routes only attempt UniswapV3.
"""

from __future__ import annotations

from libs.dex.providers import DexAggregator, Quote
from libs.dex.routes import make_route


def test_dex_provider_routing_respects_route_type(monkeypatch):
    """V2-compatible route only attempts UniswapV2; V3-only route only attempts UniswapV3."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DEX_FORCE_V2_FOR_CANONICAL", "1")

    class FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.supports_exact_out = True

    providers = [FakeProvider("uniswap_v2"), FakeProvider("uniswap_v3")]
    agg = DexAggregator(providers)

    captured: list[tuple[str, list[str]]] = []

    def fake_collect_quotes(
        self,
        active_providers,
        method,
        route_plan,
        amount,
        mode="exact_in",
    ):
        captured.append((mode, [p.name for p in active_providers]))
        if not active_providers:
            return []
        return [
            Quote(
                provider=active_providers[0].name,
                amount_in=amount,
                amount_out=amount,
                route=route_plan,
            )
        ]

    monkeypatch.setattr(
        DexAggregator,
        "_collect_quotes",
        fake_collect_quotes,
        raising=False,
    )

    v2_route = make_route(["0xusdc", "0xdiem"])
    agg.quote_all(100, v2_route)
    assert captured[-1] == ("exact_in", ["uniswap_v2"])

    v3_route = make_route(["0xusdc", "0xvvv", "0xdiem"], fees=[3000, 3000])
    agg.quote_all(150, v3_route)
    assert captured[-1] == ("exact_in", ["uniswap_v3"])

    captured.clear()
    agg.quote_all_exact_out(50, v2_route)
    assert captured[-1] == ("exact_out", ["uniswap_v2"])

    captured.clear()
    agg.quote_all_exact_out(60, v3_route)
    assert captured[-1] == ("exact_out", ["uniswap_v3"])


def test_canonical_v2_route_allows_uniswap_v2(monkeypatch):
    """Canonical DIEM→WETH→USDC route should allow UniswapV2 even though DIEM is present."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("WETH_ADDRESS", "0xweth")
    monkeypatch.setenv("DEX_FORCE_V2_FOR_CANONICAL", "1")

    class FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.supports_exact_out = True

    providers = [FakeProvider("uniswap_v2"), FakeProvider("uniswap_v3")]
    agg = DexAggregator(providers)

    captured: list[tuple[str, list[str]]] = []

    def fake_collect_quotes(
        self,
        active_providers,
        method,
        route_plan,
        amount,
        mode="exact_in",
    ):
        captured.append((mode, [p.name for p in active_providers]))
        if not active_providers:
            return []
        return [
            Quote(
                provider=active_providers[0].name,
                amount_in=amount,
                amount_out=amount,
                route=route_plan,
            )
        ]

    monkeypatch.setattr(
        DexAggregator,
        "_collect_quotes",
        fake_collect_quotes,
        raising=False,
    )

    # Canonical V2 route: USDC→WETH→DIEM (no fee tiers)
    canonical_v2_route = make_route(["0xusdc", "0xweth", "0xdiem"])
    # Mark as canonical V2 route
    object.__setattr__(canonical_v2_route, "_metadata", {"canonical_v2": True})

    agg.quote_all_exact_out(100, canonical_v2_route)
    # Should allow UniswapV2 for canonical route
    assert "uniswap_v2" in captured[-1][1]

    # Reverse canonical route: DIEM→WETH→USDC
    canonical_v2_reverse = make_route(["0xdiem", "0xweth", "0xusdc"])
    object.__setattr__(canonical_v2_reverse, "_metadata", {"canonical_v2": True})

    captured.clear()
    agg.quote_all_exact_out(100, canonical_v2_reverse)
    # Should allow UniswapV2 for canonical route
    assert "uniswap_v2" in captured[-1][1]


def test_should_skip_v2_allows_canonical_routes(monkeypatch):
    """_should_skip_v2() should return False for canonical V2 routes."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("WETH_ADDRESS", "0xweth")

    class FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name

    providers = [FakeProvider("uniswap_v2")]
    agg = DexAggregator(providers)

    # Canonical V2 route (no fee tiers)
    canonical_route = make_route(["0xusdc", "0xweth", "0xdiem"])
    assert not agg._should_skip_v2(canonical_route), (
        "Canonical V2 route should not skip V2"
    )

    # Non-canonical DIEM route (no fee tiers but not canonical path)
    non_canonical_route = make_route(["0xdiem", "0xother", "0xusdc"])
    assert agg._should_skip_v2(non_canonical_route), (
        "Non-canonical DIEM route should skip V2"
    )

    # Canonical USDC→WETH→DIEM stays V2-eligible even when hops carry fee tiers.
    v3_tagged_canonical = make_route(["0xusdc", "0xweth", "0xdiem"], fees=[3000, 3000])
    assert not agg._should_skip_v2(v3_tagged_canonical), (
        "Canonical path should allow V2 even with fee tiers"
    )


def test_diem_provider_compatibility_allows_v2_for_canonical(monkeypatch):
    """_diem_provider_compatibility() should return True for UniswapV2 with canonical routes."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("WETH_ADDRESS", "0xweth")

    class FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name

    providers = [FakeProvider("uniswap_v2")]
    agg = DexAggregator(providers)

    # Canonical V2 route
    canonical_route = make_route(["0xusdc", "0xweth", "0xdiem"])
    compatible, reason = agg._diem_provider_compatibility("uniswap_v2", canonical_route)
    assert compatible, (
        f"UniswapV2 should be compatible with canonical route, reason: {reason}"
    )

    # Non-canonical DIEM route
    non_canonical_route = make_route(["0xdiem", "0xother", "0xusdc"])
    compatible, reason = agg._diem_provider_compatibility(
        "uniswap_v2", non_canonical_route
    )
    assert not compatible, (
        f"UniswapV2 should not be compatible with non-canonical route, reason: {reason}"
    )
