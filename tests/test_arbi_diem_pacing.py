from __future__ import annotations

from importlib import import_module


def test_pacing_enforces_min_action_interval(monkeypatch) -> None:
    monkeypatch.setenv("ARBI_DIEM_MIN_ACTION_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("ARBI_DIEM_MAX_MINT_SELL_PER_HOUR", "0")

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        pass

    agent = arbi_mod.ArbiDiem(
        diem=FakeDiemService(),
        risk=risk_mod.RiskPolicy.from_env(),
    )

    now = [1000.0]
    monkeypatch.setattr(arbi_mod.time, "time", lambda: now[0], raising=True)

    agent._pacing_record_action(action="mint_sell")

    now[0] = 1100.0
    blocked = agent._pacing_check(action="mint_sell")
    assert blocked.get("ok") is False
    assert blocked.get("reason") == "min_action_interval"

    now[0] = 1300.0
    allowed = agent._pacing_check(action="mint_sell")
    assert allowed.get("ok") is True


def test_pacing_caps_mint_sell_per_hour(monkeypatch) -> None:
    monkeypatch.setenv("ARBI_DIEM_MIN_ACTION_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("ARBI_DIEM_MAX_MINT_SELL_PER_HOUR", "2")

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        pass

    agent = arbi_mod.ArbiDiem(
        diem=FakeDiemService(),
        risk=risk_mod.RiskPolicy.from_env(),
    )

    now = [1000.0]
    monkeypatch.setattr(arbi_mod.time, "time", lambda: now[0], raising=True)

    agent._pacing_record_action(action="mint_sell")
    now[0] = 1100.0
    agent._pacing_record_action(action="mint_sell")

    now[0] = 1200.0
    blocked = agent._pacing_check(action="mint_sell")
    assert blocked.get("ok") is False
    assert blocked.get("reason") == "max_mint_sell_per_hour"

    now[0] = 5000.0
    allowed = agent._pacing_check(action="mint_sell")
    assert allowed.get("ok") is True


def test_pacing_allows_recovery_bypass_interval(monkeypatch) -> None:
    monkeypatch.setenv("ARBI_DIEM_MIN_ACTION_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("ARBI_DIEM_MAX_MINT_SELL_PER_HOUR", "0")

    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")

    class FakeDiemService:
        pass

    agent = arbi_mod.ArbiDiem(
        diem=FakeDiemService(),
        risk=risk_mod.RiskPolicy.from_env(),
    )

    now = [1000.0]
    monkeypatch.setattr(arbi_mod.time, "time", lambda: now[0], raising=True)

    agent._pacing_record_action(action="mint_sell")

    now[0] = 1100.0
    blocked = agent._pacing_check(action="capacity_recovery")
    assert blocked.get("ok") is False
    assert blocked.get("reason") == "min_action_interval"

    allowed = agent._pacing_check(action="capacity_recovery", bypass_interval=True)
    assert allowed.get("ok") is True
