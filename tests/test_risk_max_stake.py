from __future__ import annotations

from importlib import import_module


def test_risk_max_stake_from_prices(monkeypatch):
    risk_mod = import_module("services.risk.policy")
    # Cap stake at $1000
    monkeypatch.setenv("RISK_MAX_STAKE_USD", "1000")
    pol = risk_mod.RiskPolicy.from_env()
    prices = {"VVV": 2.5}
    # Already staked 100 VVV (base units)
    current_units = 100 * 10**18
    # Current USD = 250; remaining budget = 750 => 300 VVV
    max_units = pol.max_stake_from_prices(
        prices, current_staked_units=current_units, vvv_decimals=18
    )
    # Expect approximately 300 VVV in base units
    assert int(max_units) == 300 * 10**18
