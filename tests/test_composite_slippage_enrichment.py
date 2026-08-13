from __future__ import annotations

import pytest

from libs.dex.composite import attach_composite_metadata
from libs.dex.providers import DexAggregator
from libs.dex.routes import make_route
from services.marketdata.pathing.enrichment import enrich_route
from services.marketdata.pathing.models import QuoteRequest, RouteCandidate
from tests.test_dex_composite import ImpactProvider


def test_enrichment_sets_slippage_for_composite_route(monkeypatch: pytest.MonkeyPatch):
    provider = ImpactProvider()
    aggregator = DexAggregator([provider])

    import services.marketdata.pathing.enrichment as enrich_mod

    monkeypatch.setattr(enrich_mod, "_erc20_decimals", lambda addr: 18)
    monkeypatch.setattr(enrich_mod, "_ensure_route_verified", lambda route: None)

    route = make_route(["0xaaa", "0xbbb", "0xccc"])
    bridge_legs = [
        {"token_in": "0xaaa", "token_out": "0xbbb", "provider": provider.name},
        {"token_in": "0xbbb", "token_out": "0xccc", "provider": provider.name},
    ]
    attach_composite_metadata(route, bridge_legs=bridge_legs, is_composite=True)

    request = QuoteRequest(
        token_in="0xaaa",
        token_out="0xccc",
        amount_in_wei=20_000_000,
    )
    candidate = RouteCandidate(route=route, source="test")

    price_map = {"0xaaa": 1.0, "0xbbb": 1.0, "0xccc": 1.0}

    evaluation = enrich_route(
        request,
        candidate,
        aggregator=aggregator,
        price_map=price_map,
    )

    assert evaluation.quote is not None
    assert "slippage_bps" in evaluation.quote
    assert evaluation.quote["slippage_bps"] > 0
    # With heavy impact the slippage should be well above the 50 bps cap
    assert evaluation.quote["slippage_bps"] > 1000
