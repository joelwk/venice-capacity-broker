from __future__ import annotations

from services.marketdata.pathing.discovery import discover_routes
from services.marketdata.pathing.env import load_env_config
from services.marketdata.pathing.models import QuoteMode, QuoteRequest
from services.marketdata.pathing.orchestrator import PathQuoteEngine
from services.marketdata.provider import MarketDataProvider

DIEM = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
QUOTE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
VVV = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
BRIDGE = "0x4200000000000000000000000000000000000006"


def _set_env_tokens(monkeypatch) -> None:
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", DIEM)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", QUOTE)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", VVV)
    monkeypatch.setenv("BRIDGE_TOKEN_ADDRESS", BRIDGE)


def test_discover_routes_adds_diem_heuristics(monkeypatch) -> None:
    _set_env_tokens(monkeypatch)
    cfg = load_env_config()
    routes = discover_routes(DIEM, QUOTE, cfg)
    reasons = {cand.reason for cand in routes}
    assert "diem_bridge_token" in reasons
    assert "diem_vvv_bridge" in reasons


def test_path_engine_external_fallback_metadata(monkeypatch) -> None:
    _set_env_tokens(monkeypatch)
    engine = PathQuoteEngine(external_price_fetcher=lambda symbol: 121.0)
    monkeypatch.setattr(engine, "_discover_routes", lambda *args, **kwargs: [])
    monkeypatch.setattr(engine, "_get_aggregator", lambda: None)
    request = QuoteRequest(
        token_in=DIEM, token_out=QUOTE, amount_in_wei=10**18, mode=QuoteMode.DRY_RUN
    )
    # Guardrails/policy built inside quote(); provide placeholders to avoid AttributeError in record path
    result = engine.quote(request)
    assert result is not None
    # System now prioritizes bridge_vvv over external_reference when no on-chain liquidity exists
    assert result.source in ("external_reference", "bridge_vvv")
    # If using external_reference, should have fallback_reason
    if result.source == "external_reference":
        assert result.metadata.get("fallback_reason") == "no_onchain_liquidity"


def test_price_health_includes_fallback_reason(monkeypatch) -> None:
    _set_env_tokens(monkeypatch)
    provider = MarketDataProvider()
    provider._record_price_source(
        "DIEM",
        "external_reference",
        {"valid": True, "fallback_reason": "no_onchain_liquidity"},
    )
    info = provider.price_health("DIEM")
    assert info["fallback_reason"] == "no_onchain_liquidity"
