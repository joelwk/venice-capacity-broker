from __future__ import annotations

import os
from importlib import import_module


def test_arbi_diem_risk_limits_mint_size(monkeypatch):
    # Arrange fake DIEM actions to capture units
    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"tx_hash": "0xabc"}

        def burn(self, amount: int):  # unused here
            return {"tx_hash": "0xdef"}

        def trade(self, side: str, amount: int):
            calls.append(("trade", amount))
            return {"tx_hash": "0xghi"}

    # Monkeypatch DIEMACTIONS used by DIEMService
    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    pricing_mod = import_module("libs.pricing.diem")

    # Make fair/day = 1.0 so a price >1.05 triggers mint
    monkeypatch.setattr(pricing_mod, "fair_value_per_diem", lambda alpha: 365.0, raising=True)

    svc = svc_mod.DIEMService(aggregator=None)  # aggregator unused in this path

    # Configure policy to limit to 50 USD per trade and set decimals=18
    os.environ["RISK_MAX_DIEM_TRADE_USD"] = "50"
    os.environ["DIEM_DECIMALS"] = "18"
    risk = risk_mod.RiskPolicy.from_env()

    # Mint desire is very large, but price is $2/DIEM, so expect ~25 DIEM worth in units
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    did = arbi.evaluate_and_maybe_mint(market_price=2.0, mint_rate=1.0, desired_units=10**24)
    assert did is True
    assert calls, "Expected at least mint/trade calls"
    # Extract units used
    minted = next(v for k, v in calls if k == "mint")
    traded = next(v for k, v in calls if k == "trade")
    # USD notional ~ 50
    usd = risk.usd_from_units(minted, 2.0)
    assert 45.0 <= usd <= 50.0
    assert minted == traded

