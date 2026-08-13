from __future__ import annotations

from importlib import import_module


def test_vvv_fair_value_components(monkeypatch):
    mod = import_module("libs.pricing.vvv")

    monkeypatch.setenv("VVV_FV_HORIZON_DAYS", "10")
    monkeypatch.setenv("VVV_FV_DISCOUNT_APY", "0.0")
    monkeypatch.setenv("VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV", "1.0")
    monkeypatch.setenv("VVV_FV_DIEM_PER_DAY_PER_STAKED_VVV", "0.5")
    monkeypatch.setenv("VVV_FV_DIEM_UTILITY_USD_PER_DIEM_DAY", "1.0")

    res = mod.fair_value_per_vvv(vvv_price_usd=2.0, locked_ratio=0.0)
    comps = res["components"]

    # 10 days, no discounting:
    # emissions: 1.0 VVV/day * $2.0 = $2.0/day => PV $20.0
    # diem: 0.5 DIEM-day/day * $1.0 = $0.5/day => PV $5.0
    assert res["vvv_fair_value_usd"] == 25.0
    assert comps["emissions_pv_usd"] == 20.0
    assert comps["diem_utility_pv_usd"] == 5.0


def test_vvv_fair_value_respects_locked_emissions_mult(monkeypatch):
    mod = import_module("libs.pricing.vvv")

    monkeypatch.setenv("VVV_FV_HORIZON_DAYS", "1")
    monkeypatch.setenv("VVV_FV_DISCOUNT_APY", "0.0")
    monkeypatch.setenv("VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV", "1.0")
    monkeypatch.setenv("VVV_FV_LOCKED_EMISSIONS_MULT", "0.8")

    unlocked = mod.fair_value_per_vvv(vvv_price_usd=2.0, locked_ratio=0.0)
    locked = mod.fair_value_per_vvv(vvv_price_usd=2.0, locked_ratio=1.0)

    assert unlocked["components"]["emissions_daily_usd"] == 2.0
    assert locked["components"]["emissions_daily_usd"] == 1.6
