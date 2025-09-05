from __future__ import annotations

from importlib import import_module


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

