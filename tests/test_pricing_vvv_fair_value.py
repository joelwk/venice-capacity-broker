from __future__ import annotations

from importlib import import_module

import pytest


def test_pricing_vvv_fair_value_discounted_pv_matches_annuity():
    mod = import_module("libs.pricing.vvv")

    res = mod.fair_value_per_vvv(
        vvv_price_usd=2.0,
        horizon_days=10,
        discount_rate_apy=0.365,
        emissions_vvv_per_day_per_staked_vvv=1.0,
        diem_per_day_per_staked_vvv=0.0,
        locked_ratio=0.0,
    )
    comps = res["components"]

    daily_r = (1.0 + 0.365) ** (1.0 / 365.0) - 1.0
    expected_emissions_pv = 2.0 * (1.0 - (1.0 + daily_r) ** (-10)) / daily_r

    assert comps["daily_discount_rate"] == pytest.approx(daily_r)
    assert comps["emissions_pv_usd"] == pytest.approx(expected_emissions_pv)
    assert comps["diem_utility_pv_usd"] == 0.0
    assert res["vvv_fair_value_usd"] == pytest.approx(expected_emissions_pv)


def test_pricing_vvv_fair_value_component_breakdown():
    mod = import_module("libs.pricing.vvv")

    res = mod.fair_value_per_vvv(
        vvv_price_usd=2.0,
        horizon_days=1,
        discount_rate_apy=0.0,
        emissions_vvv_per_day_per_staked_vvv=1.0,
        diem_per_day_per_staked_vvv=0.5,
        diem_utility_usd_per_diem_day=1.0,
        locked_ratio=0.5,
        locked_emissions_mult=0.8,
    )
    comps = res["components"]

    assert comps["effective_emissions_mult"] == pytest.approx(0.9)
    assert comps["emissions_daily_usd"] == pytest.approx(1.8)
    assert comps["diem_daily_usd"] == pytest.approx(0.5)
    assert comps["emissions_pv_usd"] == pytest.approx(1.8)
    assert comps["diem_utility_pv_usd"] == pytest.approx(0.5)
    assert res["vvv_fair_value_usd"] == pytest.approx(2.3)
