from __future__ import annotations

from importlib import import_module

import pytest


def test_risk_policy_units_from_usd_monkeypatched_decimals(monkeypatch):
    policy_mod = import_module("services.risk.policy")
    pol = policy_mod.RiskPolicy.from_env()
    # Force decimals to 18 without web3
    monkeypatch.setattr(pol, "_diem_decimals", lambda: 18, raising=True)
    # price = 1.2 USD/DIEM, 100 USD cap => ~83.333 DIEM => 83.333e18 units
    units = pol.units_from_usd(100.0, 1.2)
    assert units > 0
    # round trip
    usd = pol.usd_from_units(units, 1.2)
    assert 95.0 <= usd <= 100.0


def test_risk_policy_suggest_trade_units(monkeypatch):
    policy_mod = import_module("services.risk.policy")
    pol = policy_mod.RiskPolicy.from_env()
    monkeypatch.setattr(pol, "_diem_decimals", lambda: 18, raising=True)
    # Desired very large, but USD cap limits
    price = 2.0
    desired = 10**24  # far above cap
    suggested = pol.suggest_trade_units(desired, price)
    assert 0 < suggested < desired


def test_dynamic_slippage_cap_tracks_premium(monkeypatch):
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "75")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_PREMIUM_MULT", "2.0")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_HARD_CAP_BPS", "300")
    policy_mod = import_module("services.risk.policy")
    pol = policy_mod.RiskPolicy.from_env()

    # Small premium widens slightly above static cap
    flat_cap = pol._compute_dynamic_slippage_cap(price_ratio=1.005)
    assert flat_cap == pytest.approx(100.0)

    wide_cap = pol._compute_dynamic_slippage_cap(price_ratio=1.70)
    assert 299.0 <= wide_cap <= 300.0


def test_dynamic_slippage_cap_respects_liquidity_signal(monkeypatch):
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "75")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_PREMIUM_MULT", "2.0")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_HARD_CAP_BPS", "300")
    policy_mod = import_module("services.risk.policy")
    pol = policy_mod.RiskPolicy.from_env()

    cap = pol._compute_dynamic_slippage_cap(
        price_ratio=None, liquidity_slippage_bps=120.0
    )
    assert cap == pytest.approx(240.0)
