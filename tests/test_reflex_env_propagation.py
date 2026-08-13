"""Test that ReflexGuardian correctly reads and applies REFLEX_* environment variables."""

from __future__ import annotations

import os

from agents.reflex.guardian import ReflexGuardian


def test_reflex_guardian_reads_env_vars(monkeypatch):
    """Test that ReflexGuardian reads REFLEX_MAX_VOL_BPS from environment."""
    monkeypatch.setenv("REFLEX_MAX_VOL_BPS", "100000")
    monkeypatch.setenv("REFLEX_MAX_UTILIZATION", "0.99")
    monkeypatch.setenv("REFLEX_MAX_PRICE_DRAWDOWN", "1.0")
    monkeypatch.setenv("REFLEX_REQUIRE_ACTIVE_STAKE", "true")

    guard = ReflexGuardian()

    assert guard.max_vol_bps == 100000.0
    assert guard.max_utilization == 0.99
    assert guard.max_drawdown == 1.0
    assert guard.require_active_stake is True


def test_reflex_guardian_uses_defaults_when_env_not_set(monkeypatch):
    """Test that ReflexGuardian uses defaults when env vars are not set."""
    # Clear all REFLEX_* env vars
    for key in list(os.environ.keys()):
        if key.startswith("REFLEX_"):
            monkeypatch.delenv(key, raising=False)

    guard = ReflexGuardian()

    assert guard.max_vol_bps == 450.0  # default from code
    assert guard.max_utilization == 0.92  # default from code
    assert guard.max_drawdown == 0.12  # default from code
    assert guard.require_active_stake is True  # default from code


def test_reflex_guardian_constructor_override_takes_precedence(monkeypatch):
    """Test that constructor parameters override env vars."""
    monkeypatch.setenv("REFLEX_MAX_VOL_BPS", "100000")

    guard = ReflexGuardian(max_vol_bps=50000.0)

    assert guard.max_vol_bps == 50000.0  # constructor override wins


def test_reflex_guardian_evaluate_includes_limits_in_result():
    """Test that evaluate() includes limits in the result dict."""
    guard = ReflexGuardian(max_vol_bps=100000.0, max_utilization=0.99, max_drawdown=1.0)

    result = guard.evaluate(
        price=1.0,
        utilization=0.5,
        vol_bps=100.0,
        stake={"status": "ok", "snapshot": {"active_staker": True}},
        dry_run=False,
        enable_live=True,
    )

    assert "limits" in result
    limits = result["limits"]
    assert limits["max_vol_bps"] == 100000.0
    assert limits["max_utilization"] == 0.99
    assert limits["max_drawdown"] == 1.0


def test_reflex_guardian_startup_logging(caplog, monkeypatch):
    """Test that ReflexGuardian logs effective configuration at startup."""
    monkeypatch.setenv("REFLEX_MAX_VOL_BPS", "100000")
    monkeypatch.setenv("REFLEX_MAX_UTILIZATION", "0.99")

    ReflexGuardian()

    # Check that initialization log was emitted
    log_records = [
        r for r in caplog.records if "ReflexGuardian initialized" in r.message
    ]
    assert len(log_records) > 0
    log_msg = log_records[0].message
    assert "max_vol_bps=100000" in log_msg or "max_vol_bps=100000.0" in log_msg
    assert "max_utilization=0.99" in log_msg
