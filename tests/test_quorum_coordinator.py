from __future__ import annotations

from agents.quorum import QuorumCoordinator, build_default_models
from agents.quorum.models import QuorumContext


def _make_base_context(**overrides):
    ctx = QuorumContext(
        price=1.2,
        mint_rate=1.0,
        premium=1.05,
        suggested_units=250,
        utilization_ratio=0.9,
        vol_bps=18.0,
        inventory_usd=45.0,
        stake={"status": "ok", "snapshot": {"active_staker": True}},
        simulate_decision=True,
        dry_run=True,
        live_mode=False,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


def test_quorum_coordinator_approves_high_premium():
    models = build_default_models(include_treasury=False)
    coordinator = QuorumCoordinator(models=models, threshold=0.55)
    ctx = _make_base_context()
    coordinator.update(ctx)
    decision, details = coordinator.decide_with_details()
    assert decision is True
    assert isinstance(details, dict)
    assert details["totalWeight"] > 0
    assert any(entry["name"] == "arbitrage" and entry["approve"] for entry in details["breakdown"])


def test_quorum_coordinator_blocks_on_reflex_halt():
    models = build_default_models(include_treasury=False)
    coordinator = QuorumCoordinator(models=models, threshold=0.55)
    ctx = _make_base_context(reflex={"halt": True})
    coordinator.update(ctx)
    decision, details = coordinator.decide_with_details()
    assert decision is False
    risk_vote = next(entry for entry in details["breakdown"] if entry["name"] == "risk")
    assert risk_vote["approve"] is False
    assert risk_vote["confidence"] == 1.0
