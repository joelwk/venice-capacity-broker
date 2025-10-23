from __future__ import annotations

from agents.reflex.guardian import ReflexGuardian
from services.memory import MemoryStore, ReflectionEngine


def test_memory_store_record_and_recent(tmp_path):
    store = MemoryStore(path=tmp_path / "memory.jsonl")
    cycle = {
        "ts": 123.0,
        "stake": {"status": "ok"},
        "arbi": {"action": "hold"},
        "capacity": {"status": "ok"},
    }
    store.record_cycle(cycle)

    recent = store.recent(1)
    assert len(recent) == 1
    assert recent[0]["cycle"]["arbi"]["action"] == "hold"
    assert store.most_recent()["ts"] == 123.0


def test_reflection_engine_hold_streak(tmp_path):
    reflection = ReflectionEngine(lookback=5, hold_streak_threshold=2)
    history = [
        {"ts": 1, "cycle": {"arbi": {"action": "hold"}}},
        {"ts": 2, "cycle": {"arbi": {"action": "hold"}}},
    ]
    current = {
        "ts": 3,
        "stake": {"status": "ok"},
        "arbi": {"action": "hold", "execution": {"status": "dry_run"}},
        "capacity": {"status": "ok"},
    }
    summary = reflection.reflect(current, history=history)
    assert any("held" in note for note in summary["notes"])
    assert summary["streaks"]["hold"] >= 3


def test_reflex_guardian_detects_drawdown():
    guardian = ReflexGuardian(max_drawdown=0.05, max_vol_bps=None, max_utilization=None)
    last_cycle = {"cycle": {"arbi": {"price": 1.0}}, "ts": 1}
    result = guardian.evaluate(
        price=0.9,
        utilization=0.5,
        vol_bps=50.0,
        stake={"status": "ok", "snapshot": {"active_staker": True}},
        dry_run=False,
        enable_live=True,
        last_cycle=last_cycle,
    )
    assert result["halt"] is True
    assert "price_drawdown" in result["reasons"]


def test_reflex_guardian_warns_high_utilization():
    guardian = ReflexGuardian(max_drawdown=None, max_vol_bps=None, max_utilization=0.8)
    result = guardian.evaluate(
        price=1.0,
        utilization=0.9,
        vol_bps=None,
        stake={"status": "ok", "snapshot": {"active_staker": True}},
        dry_run=False,
        enable_live=True,
        last_cycle=None,
    )
    assert result["halt"] is False
    assert "utilization_hot" in result["warnings"]


def test_reflex_guardian_requires_active_stake():
    guardian = ReflexGuardian(max_drawdown=None, max_vol_bps=None, max_utilization=None, require_active_stake=True)
    result = guardian.evaluate(
        price=1.0,
        utilization=0.5,
        vol_bps=10.0,
        stake={"status": "ok", "snapshot": {"active_staker": False}},
        dry_run=False,
        enable_live=True,
        last_cycle=None,
    )
    assert result["halt"] is True
    assert "stake_inactive" in result["reasons"]


def test_reflex_guardian_apply_in_dry_run_flag():
    guardian_skip = ReflexGuardian(apply_in_dry_run=False, max_drawdown=None, max_vol_bps=None, max_utilization=None)
    result_skip = guardian_skip.evaluate(
        price=None,
        utilization=None,
        vol_bps=None,
        stake={"status": "ok", "snapshot": {"active_staker": True}},
        dry_run=True,
        enable_live=False,
        last_cycle=None,
    )
    assert result_skip["halt"] is False
    guardian_apply = ReflexGuardian(apply_in_dry_run=True, max_drawdown=None, max_vol_bps=None, max_utilization=None)
    result_apply = guardian_apply.evaluate(
        price=None,
        utilization=None,
        vol_bps=None,
        stake={"status": "ok", "snapshot": {"active_staker": True}},
        dry_run=True,
        enable_live=True,
        last_cycle=None,
    )
    assert result_apply["halt"] is True
    assert "price_unavailable" in result_apply["reasons"]
