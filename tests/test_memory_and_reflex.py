from __future__ import annotations

from agents.reflex.guardian import ReflexGuardian
from graph.workflows.orchestrator import SingleLoopOrchestrator
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


def test_reflection_engine_burn_gas_error_downgrades_severity():
    reflection = ReflectionEngine()
    cycle = {
        "ts": 4,
        "stake": {"status": "ok"},
        "capacity": {"status": "ok"},
        "arbi": {
            "action": "burn",
            "execution": {
                "status": "error",
                "error": "insufficient funds for gas * price + value",
                "error_type": "InsufficientFunds",
                "burn": {
                    "status": "error",
                    "error": "max fee per gas less than block base fee",
                },
            },
        },
    }

    summary = reflection.reflect(cycle, history=None)

    assert summary["severity"] == "medium"
    assert "burn_gas_error" in summary.get("labels", [])
    assert "arbi_execution_error_gas" in summary.get("severity_reasons", [])


def test_reflection_halt_skips_gas_burn_labels():
    dummy = object()
    orch = SingleLoopOrchestrator(
        stake_master=dummy, arbi=dummy, capacity_broker=dummy, market=dummy
    )
    reflection = {
        "severity": "high",
        "notes": ["ArbiDiem execution error"],
        "labels": ["burn_gas_error"],
        "severity_reasons": ["arbi_execution_error_gas"],
    }

    orch._maybe_arm_reflection_halt(reflection)

    assert getattr(orch, "_reflection_halt_until", 0.0) == 0.0


def test_reflection_engine_post_buy_balance_sync_error():
    """Reflection engine should classify post-buy balance sync errors as medium severity."""
    reflection = ReflectionEngine()
    cycle = {
        "ts": 5,
        "stake": {"status": "ok"},
        "capacity": {"status": "ok"},
        "arbi": {
            "action": "buy_burn",
            "execution": {
                "status": "error",
                "buy": {
                    "status": "submitted",
                    "tx_hash": "0xabc123",
                },
                "burn": {
                    "status": "error",
                    "steps": [
                        {
                            "status": "error",
                            "error": "insufficient_diem_balance",
                            "reason": "insufficient_diem_balance",
                        }
                    ],
                },
            },
        },
    }

    summary = reflection.reflect(cycle, history=None)

    assert summary["severity"] == "medium"
    assert "balance_sync_delay" in summary.get("labels", [])
    assert "post_buy_balance_sync" in summary.get("severity_reasons", [])
    assert any("stale balance" in note.lower() for note in summary["notes"])


def test_reflection_halt_skips_balance_sync_labels():
    """Orchestrator should skip halt for balance sync delay labels."""
    dummy = object()
    orch = SingleLoopOrchestrator(
        stake_master=dummy, arbi=dummy, capacity_broker=dummy, market=dummy
    )
    reflection = {
        "severity": "high",
        "notes": ["Burn failed due to stale balance"],
        "labels": ["balance_sync_delay"],
        "severity_reasons": ["post_buy_balance_sync"],
    }

    orch._maybe_arm_reflection_halt(reflection)

    assert getattr(orch, "_reflection_halt_until", 0.0) == 0.0


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


def test_reflex_guardian_requires_active_stake(monkeypatch):
    monkeypatch.setenv("REFLEX_STAKE_INACTIVE_CONSEC", "1")
    guardian = ReflexGuardian(
        max_drawdown=None,
        max_vol_bps=None,
        max_utilization=None,
        require_active_stake=True,
    )
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
    guardian_skip = ReflexGuardian(
        apply_in_dry_run=False,
        max_drawdown=None,
        max_vol_bps=None,
        max_utilization=None,
    )
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
    guardian_apply = ReflexGuardian(
        apply_in_dry_run=True, max_drawdown=None, max_vol_bps=None, max_utilization=None
    )
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


def test_reflex_guardian_consecutive_inactive_threshold():
    guardian = ReflexGuardian(
        max_drawdown=None,
        max_vol_bps=None,
        max_utilization=None,
        require_active_stake=True,
    )
    kwargs = {
        "price": 1.0,
        "utilization": 0.5,
        "vol_bps": 10.0,
        "dry_run": False,
        "enable_live": True,
        "last_cycle": None,
    }
    stake_payload = {"status": "ok", "snapshot": {"active_staker": False}}
    first = guardian.evaluate(stake=stake_payload, **kwargs)
    assert first["halt"] is False
    second = guardian.evaluate(stake=stake_payload, **kwargs)
    assert second["halt"] is False
    third = guardian.evaluate(stake=stake_payload, **kwargs)
    assert third["halt"] is True
    assert third["observed"]["stake_inactive_consecutive"] >= 3

    unknown = guardian.evaluate(
        stake={"status": "unknown", "snapshot": {"active_staker": True}},
        **kwargs,
    )
    assert unknown["halt"] is False
    assert unknown["observed"]["stake_inactive_consecutive"] == 0
