from __future__ import annotations

from typing import Optional

import pytest

from libs.dex.routes import make_route

from services.marketdata.pathing.discovery import DiscoveryContext, discover_routes
from services.marketdata.pathing.env import EnvConfig
from services.marketdata.pathing.models import (
    GuardrailContext,
    HopTelemetry,
    PolicyContext,
    QuoteMode,
    QuoteRequest,
    RouteCandidate,
    RouteEvaluation,
)
from services.marketdata.pathing.orchestrator import PathQuoteEngine
from services.marketdata.pathing.scoring import multi_objective_score


class _NullAggregator:
    def best_quote(self, amount_in: int, route) -> Optional[object]:
        return None


def test_path_quote_engine_external_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    quote_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    monkeypatch.setattr(
        "services.marketdata.pathing.orchestrator.build_aggregator_from_env",
        lambda: _NullAggregator(),
    )
    monkeypatch.setattr(
        "services.marketdata.pools.suggest_routes_for_tokens",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.delenv("DIEM_VVV_PAIR_ADDRESS", raising=False)
    monkeypatch.delenv("VVV_USDC_POOL_ADDRESS", raising=False)
    def fetcher(symbol: str) -> float | None:
        return 1.25 if symbol == "DIEM" else None
    engine = PathQuoteEngine(external_price_fetcher=fetcher)
    request = QuoteRequest(
        token_in=diem_addr,
        token_out=quote_addr,
        amount_in_wei=10**18,
        mode=QuoteMode.DRY_RUN,
    )
    result = engine.quote(request)
    assert result is not None
    assert result.source == "external_reference"
    assert pytest.approx(result.price) == 1.25


def test_discover_routes_deduplicates_manual_and_db() -> None:
    config = EnvConfig(
        quote_token="0x1",
        bridge_token="0x2",
        diem_token="0x3",
        vvv_token="0x4",
        trade_paths=[make_route(["0xa", "0xb", "0xc"])],
        progressive_live=False,
        progressive_min_cycles=None,
        diem_vvv_pair=None,
        vvv_usdc_pool=None,
    )
    db_route = make_route(["0xa", "0x2", "0xc"])
    routes = discover_routes("0xa", "0xc", config, discovery=DiscoveryContext(routes_from_db=[db_route]))
    dedup = {tuple(candidate.route.tokens) for candidate in routes}
    assert len(dedup) == len(routes)
    assert any(candidate.source == "env" for candidate in routes)
    assert any(candidate.source == "heuristic" for candidate in routes)


def test_multi_objective_score_penalties() -> None:
    candidate = RouteCandidate(route=make_route(["0xa", "0xb"]), source="env")
    evaluation = RouteEvaluation(candidate=candidate)
    evaluation.quote = {
        "provider": "test",
        "amount_in": 10**18,
        "amount_out": 2 * 10**18,
        "decimals": {"in": 18, "out": 18},
        "price": 2.0,
    }
    hop = HopTelemetry(token_in="0xa", token_out="0xb", pool="0xpool", status="ok")
    hop.metrics["pool_take_bps"] = 750.0
    hop.metrics["reserve_in_usd"] = 100.0
    evaluation.hops.append(hop)
    guardrails = GuardrailContext(max_pool_take_bps=100.0)
    policy = PolicyContext(
        liquidity_floor_usd=500.0,
        progressive_mode=True,
        progressive_cycle=1,
        progressive_min_cycles=4,
    )
    score, guard_penalty, policy_penalty, breakdown = multi_objective_score(
        evaluation,
        guardrails,
        policy,
        slippage_limit_bps=50.0,
    )
    assert score > -2.0  # penalties move score upward
    assert guard_penalty > 0
    assert policy_penalty > 0
    assert "expected_out" in breakdown
