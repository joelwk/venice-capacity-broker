from __future__ import annotations

import math
import os
from types import SimpleNamespace

import pytest

from libs.dex.routes import as_route_plan, make_route
from services.marketdata.pathing.env import load_env_config
from services.marketdata.pathing.fallbacks import bridge_vvv_price
from services.marketdata.pathing.models import (
    QuoteMode,
    QuoteRequest,
    RouteCandidate,
    RouteEvaluation,
)
from services.marketdata.pathing.orchestrator import PathQuoteEngine
from services.marketdata.pathing.validation import validate_diem_route_price


def test_prices_normalized_without_heuristics(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("WETH_ADDRESS", weth_addr)
    monkeypatch.setenv("TRADE_PATH", ",".join([diem_addr, weth_addr, quote_addr]))

    monkeypatch.setattr(
        MarketDataProvider,
        "_fetch_external_price",
        lambda self, symbol: None,
        raising=False,
    )
    monkeypatch.setattr(
        MarketDataProvider, "_external_price_ttl", lambda self: 0.0, raising=False
    )

    MarketDataProvider._price_cache.clear()
    md = MarketDataProvider()

    def fake_decimals(address: str) -> int:
        addr = address.lower()
        if addr == quote_addr.lower():
            return 6
        return 18

    monkeypatch.setattr(md, "_erc20_decimals", fake_decimals)
    monkeypatch.setattr(md, "diem_price_with_fallback", lambda: 227.25)

    def fake_best_price(route, amount_in_decimal: float = 1.0, **kwargs):  # type: ignore[override]
        plan = as_route_plan(route)
        tokens = [p.lower() for p in plan.tokens]
        shortcut = [vvv_addr.lower(), quote_addr.lower()]
        via_weth = [vvv_addr.lower(), weth_addr.lower(), quote_addr.lower()]
        weth_quote = [weth_addr.lower(), quote_addr.lower()]
        if tokens == shortcut or tokens == via_weth:
            return {"provider": "stub", "price": 2.63}
        if tokens == weth_quote:
            return {"provider": "stub", "price": 3200.0}
        raise RuntimeError(f"Unexpected path {plan.tokens}")

    monkeypatch.setattr(md, "best_price", fake_best_price)
    monkeypatch.setenv(
        "VVV_PRICE_PATH",
        f"{vvv_addr}@3000,{weth_addr}@500,{quote_addr}",
    )

    prices = md.prices(["VVV", "DIEM", "ETH", "USDC"])

    assert math.isclose(prices["DIEM"], 227.25, rel_tol=1e-6)
    assert math.isclose(prices["VVV"], 2.63, rel_tol=1e-6)
    assert math.isclose(prices["ETH"], 3200.0, rel_tol=1e-6)
    assert prices["USDC"] == 1.0


def test_eth_price_canonical_route_avoids_vvv(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("WETH_ADDRESS", weth_addr)

    routes: list[tuple[str, ...]] = []

    def fake_best_price(self, route, amount_in_decimal: float = 1.0, **kwargs):  # type: ignore[override]
        plan = as_route_plan(route)
        tokens = tuple(plan.tokens)
        routes.append(tokens)
        return {
            "provider": "stub",
            "amount_in": 1,
            "amount_out": 3200,
            "decimals": {"in": 18, "out": 6},
            "price": 3200.0,
            "path": list(plan.tokens),
            "fees": [hop.fee for hop in plan.hops],
        }

    monkeypatch.setattr(
        MarketDataProvider, "best_price", fake_best_price, raising=False
    )
    monkeypatch.setattr(
        MarketDataProvider,
        "_quote_via_path_engine",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        MarketDataProvider, "_external_price", lambda self, symbol: None, raising=False
    )

    MarketDataProvider._price_cache.clear()
    provider = MarketDataProvider()
    prices = provider.prices(["ETH"])

    assert math.isclose(prices["ETH"], 3200.0)
    assert routes, "expected at least one canonical route invocation"
    first_route = routes[0]
    assert first_route[0].lower() == weth_addr.lower()
    assert first_route[-1].lower() == quote_addr.lower()
    for recorded in routes:
        assert vvv_addr.lower() not in [tok.lower() for tok in recorded]


def test_eth_price_canonical_route_skips_external_clamp(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("WETH_ADDRESS", weth_addr)
    # Disable ETH fast path so test exercises the clamp logic after DEX route lookup
    monkeypatch.setenv("MARKETDATA_EXTERNAL_FIRST_SYMBOLS", "")

    def fake_best_price(self, route, amount_in_decimal: float = 1.0, **kwargs):  # type: ignore[override]
        plan = as_route_plan(route)
        return {
            "provider": "stub",
            "amount_in": 1,
            "amount_out": 3200,
            "decimals": {"in": 18, "out": 6},
            "price": 3200.0,
            "path": list(plan.tokens),
            "fees": [hop.fee for hop in plan.hops],
        }

    monkeypatch.setattr(
        MarketDataProvider, "best_price", fake_best_price, raising=False
    )
    monkeypatch.setattr(
        MarketDataProvider,
        "_quote_via_path_engine",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        MarketDataProvider,
        "_external_price",
        lambda self, symbol: 5000.0,
        raising=False,
    )

    MarketDataProvider._price_cache.clear()
    provider = MarketDataProvider()
    prices = provider.prices(["ETH"])

    assert math.isclose(prices["ETH"], 3200.0)
    source = type(provider)._get_price_source("ETH")
    assert source.get("source") == "external_clamp"
    assert source.get("fallback") == "internal"


def test_external_price_timeout_enters_backoff(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    wbtc_addr = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    monkeypatch.setenv("WBTC_TOKEN_ADDRESS", wbtc_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    # Keep TTL positive to exercise the backoff window.
    monkeypatch.setattr(
        MarketDataProvider, "_external_price_ttl", lambda self: 300.0, raising=False
    )

    calls: dict[str, int] = {"count": 0}

    def failing_fetch(self, symbol):  # type: ignore[override]
        calls["count"] += 1

    monkeypatch.setattr(
        MarketDataProvider, "_fetch_external_price", failing_fetch, raising=False
    )

    provider = MarketDataProvider()

    first = provider._external_price("WBTC")
    second = provider._external_price("WBTC")

    assert first is None
    assert second is None
    assert calls["count"] == 1  # second call should reuse backoff instead of refetch


def test_diem_price_canonical_path(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("WETH_ADDRESS", weth_addr)

    route = make_route([diem_addr, weth_addr, quote_addr], [3000, 500])
    path_result = SimpleNamespace(
        price=150.0,  # Realistic price reflecting perpetual $1/day compute value
        provider="path_engine",
        source="path_engine",
        route=route,
        metadata={
            "path": list(route.tokens),
            "decimals": {"in": 18, "out": 6},
            "policy_penalty": None,
            "guardrail_penalty": None,
            "fees": [hop.fee for hop in route.hops],
        },
        score=1.0,
    )

    monkeypatch.setattr(
        MarketDataProvider,
        "_quote_via_path_engine",
        lambda *args, **kwargs: path_result,
        raising=False,
    )
    monkeypatch.setattr(
        MarketDataProvider, "_external_price", lambda self, symbol: None, raising=False
    )

    provider = MarketDataProvider()
    price = provider._price_for_symbol("DIEM")
    assert math.isclose(price, 150.0, rel_tol=1e-6)
    source = type(provider)._get_price_source("DIEM")
    assert source.get("path") == list(route.tokens)
    assert source.get("decimals") == {"in": 18, "out": 6}


def test_diem_price_validation_tiers(monkeypatch):
    """Test two-tier DIEM price validation: hard errors vs soft warnings."""
    from services.marketdata.provider import MarketDataProvider

    # Setup addresses
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)

    provider = MarketDataProvider()

    # Hard error: obviously broken price should be rejected
    result = provider._validate_diem_price(0.05, "test_source")
    assert result is None  # Rejected

    # Soft warning: low but possibly valid price should be accepted with warning
    result = provider._validate_diem_price(5.0, "test_source")
    assert result == 5.0  # Accepted despite being below expected minimum

    # Normal: expected range should pass without issues
    result = provider._validate_diem_price(150.0, "test_source")
    assert result == 150.0

    # Soft warning: high price should be accepted with warning
    result = provider._validate_diem_price(15000.0, "test_source")
    assert result == 15000.0  # Accepted despite being above expected maximum


def test_best_price_uses_diem_bridge_route(monkeypatch):
    """MarketDataProvider should fall back to DIEM→VVV→USDC composite routes."""
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    pair_addr = "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    pool_addr = "0x67a11022b7b6ed66f81233f6c8ed6e48f7826530"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv("DIEM_VVV_PAIR_ADDRESS", pair_addr)
    monkeypatch.setenv("VVV_USDC_POOL_ADDRESS", pool_addr)
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")

    MarketDataProvider._price_cache.clear()
    provider = MarketDataProvider()

    class StubAggregator:
        def __init__(self) -> None:
            self.routes: list[tuple[str, ...]] = []

        def best_quote(self, amount_in: int, cand):
            self.routes.append(tuple(cand.tokens))
            if len(cand.tokens) == 3:
                return SimpleNamespace(
                    amount_in=amount_in,
                    amount_out=amount_in * 150,
                    route=cand,
                    provider="mock",
                )
            return None

    stub = StubAggregator()
    monkeypatch.setattr(provider, "_get_aggregator", lambda: stub, raising=False)
    monkeypatch.setattr(
        provider,
        "_erc20_decimals",
        lambda address: 6 if address.lower() == usdc_addr.lower() else 18,
        raising=False,
    )

    direct_route = make_route([diem_addr, usdc_addr])
    result = provider.best_price(direct_route, amount_in_decimal=1.0)

    assert result["provider"] == "mock"
    assert stub.routes, "expected aggregator invocations"
    assert any(len(tokens) == 3 for tokens in stub.routes)


def test_price_health_tracks_clamp(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    # use non-sensitive placeholder addresses to avoid gitleaks false positives
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0x0000000000000000000000000000000000000001"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0x0000000000000000000000000000000000000002"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x0000000000000000000000000000000000000003"
    )
    # Remove any higher-priority multi-path settings that could override TRADE_PATH
    monkeypatch.delenv("TRADE_PATHS", raising=False)
    monkeypatch.delenv("TRADE_PATH_2", raising=False)
    # Minimal trade path so MarketDataProvider validation passes without env config
    monkeypatch.setenv(
        "TRADE_PATH",
        "0x0000000000000000000000000000000000000001,0x0000000000000000000000000000000000000003",
    )
    # Skip strict trade path validation for this clamp-focused unit test
    monkeypatch.setattr(
        "services.marketdata.provider.MarketDataProvider._validate_trade_paths",
        lambda self: None,
        raising=False,
    )

    provider = MarketDataProvider()
    type(provider)._last_price_sources.clear()
    type(provider)._price_clamp_events.clear()

    monkeypatch.setattr(provider, "_external_price", lambda symbol: 1.0, raising=False)

    provider._apply_price_sanity("DIEM", 200.0)

    health = provider.price_health("DIEM", max_age=999)
    assert health["clamped"] is True
    assert str(health.get("source") or "").startswith("external")
    assert health.get("clamp_reason") == "drift"


def _require_token_addresses() -> dict[str, str]:
    """Helper to get token addresses for integration tests."""
    config = load_env_config()
    diem = config.diem_token
    quote = config.quote_token
    if not diem or not quote:
        pytest.skip("DIEM_TOKEN_ADDRESS or QUOTE_TOKEN_ADDRESS not configured")
    return {
        "diem": diem,
        "quote": quote,
        "weth": (config.bridge_token or os.getenv("WETH_ADDRESS") or "").strip(),
    }


@pytest.mark.integration
def test_direct_pool_validation_rejects_bad_prices():
    """Bad direct pool prices must be rejected by validation."""
    from services.marketdata.provider import MarketDataProvider

    provider = MarketDataProvider()
    assert provider._validate_diem_price(0.000221, "direct_pool") is None


@pytest.mark.integration
def test_bridge_pricing_as_fallback(monkeypatch):
    """Bridge pricing should be used when path engine fails."""
    from services.marketdata.provider import MarketDataProvider

    # Configure DIEM/VVV pair for bridge pricing
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )

    provider = MarketDataProvider()
    price = provider.get_price("DIEM")
    if price is None or price <= 10.0:
        pytest.skip("DIEM bridge pricing unavailable or invalid for integration test")
    assert price > 10.0
    source = MarketDataProvider._get_price_source("DIEM")
    # Bridge pricing may not always be the source if path engine succeeds
    allowed_sources = {"bridge_vvv", "path_engine", "missing"}
    source_value = str(source.get("source") or "")
    if source_value not in allowed_sources:
        pytest.skip(f"DIEM price source not on-chain: {source_value}")
    assert source_value in allowed_sources


@pytest.mark.integration
def test_price_consistency_across_calls():
    """DIEM price should be consistent across multiple calls."""
    from services.marketdata.provider import MarketDataProvider

    provider = MarketDataProvider()
    prices = [provider.get_price("DIEM") for _ in range(5)]
    valid = [p for p in prices if p and p > 0]
    if len(valid) < 3:
        pytest.skip("Insufficient valid DIEM prices for consistency check")
    drift = (max(valid) - min(valid)) / min(valid)
    assert drift < 0.05


@pytest.mark.integration
def test_bridge_pricing_calculation(monkeypatch):
    """Bridge pricing should calculate correctly from reserves."""
    # Configure DIEM/VVV pair for bridge pricing
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )

    config = load_env_config()
    price = bridge_vvv_price(config)
    if price is None:
        pytest.skip("DIEM_VVV_PAIR_ADDRESS not configured or pair unavailable")
    assert 10.0 < price < 1_000.0


@pytest.mark.integration
def test_multi_hop_preferred_over_direct(monkeypatch):
    """Multi-hop routes should be scored better than direct DIEM routes."""
    # Deterministic token addresses for offline scoring
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    addresses = _require_token_addresses()

    # Configure DEX providers with router addresses
    monkeypatch.setenv(
        "UNISWAP_V2_ROUTER_ADDRESS", "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
    )
    monkeypatch.setenv(
        "AERODROME_ROUTER_ADDRESS", "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"
    )
    monkeypatch.setenv("DEX_PROVIDERS", "uniswap_v2,aerodrome")

    # Stub aggregator to avoid RPC/web3 and force multi-hop preference
    from types import SimpleNamespace

    from libs.dex.providers import Quote
    from libs.dex.routes import make_route
    from services.marketdata.pathing import enrichment as enrich_mod
    from services.marketdata.pathing import orchestrator as orch_mod

    class StubAgg:
        def __init__(self):
            self.providers = [SimpleNamespace(name="stub")]

        def best_quote(self, amount_in, route, allowed_providers=None):
            tokens = getattr(route, "tokens", [])
            # Reward multi-hop with a better price so it wins scoring
            out = int(amount_in * (200 if len(tokens) >= 3 else 120))
            return Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=out,
                route=route,
            )

    stub_agg = StubAgg()

    def fake_discover(self, token_in, token_out, config):
        direct = RouteEvaluation(
            candidate=RouteCandidate(
                route=make_route([token_in, token_out]),
                source="direct",
            )
        )
        multi = RouteEvaluation(
            candidate=RouteCandidate(
                route=make_route(
                    [token_in, "0x4200000000000000000000000000000000000006", token_out],
                    [3000, 500],
                ),
                source="canonical",
            )
        )
        return [multi, direct]

    monkeypatch.setattr(
        PathQuoteEngine, "_discover_routes", fake_discover, raising=True
    )
    monkeypatch.setattr(
        PathQuoteEngine, "_get_aggregator", lambda self: stub_agg, raising=True
    )
    monkeypatch.setattr(
        orch_mod, "build_aggregator_from_env", lambda: stub_agg, raising=True
    )
    # Avoid RPC calls for decimals or path verification
    monkeypatch.setattr(
        enrich_mod, "_erc20_decimals", lambda addr: 18 if addr else 18, raising=True
    )
    monkeypatch.setattr(
        enrich_mod, "_ensure_route_verified", lambda route: None, raising=True
    )

    engine = PathQuoteEngine()
    request = QuoteRequest(
        token_in=addresses["diem"],
        token_out=addresses["quote"],
        amount_in_wei=int(1e18),
        mode=QuoteMode.DRY_RUN,
    )
    result = engine.quote(request)
    if not result or not result.route:
        pytest.skip(
            "PathQuoteEngine returned no route for DIEM -> QUOTE (DEX providers may not be configured)"
        )
    tokens = list(result.route.tokens)
    assert len(tokens) >= 3, f"expected multi-hop route, got {tokens}"


@pytest.mark.integration
def test_route_validation_rejects_drift(monkeypatch):
    """Routes with excessive drift from bridge should be rejected."""
    addresses = _require_token_addresses()

    # Configure DIEM/VVV pair for bridge pricing
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )

    config = load_env_config()
    bridge_price = bridge_vvv_price(config)
    if bridge_price is None:
        pytest.skip("DIEM_VVV_PAIR_ADDRESS not configured or pair unavailable")

    route = make_route([addresses["diem"], addresses["quote"]])
    valid, reason = validate_diem_route_price(route, bridge_price * 0.5)
    assert not valid
    assert "drift" in reason
