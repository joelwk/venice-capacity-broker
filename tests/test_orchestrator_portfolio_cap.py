from __future__ import annotations

import os
from importlib import import_module


def test_orchestrator_passes_portfolio_inventory_to_arbi(monkeypatch):
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    # Market returns DIEM price for exposure calc
    class FakeMarket:
        def prices(self, symbols):  # noqa: ANN001
            return {"DIEM": 2.0, "USDC": 1.0, "VVV": 1.0}

    seen = {}

    class FakeArbi:
        def __init__(self):  # noqa: D401
            self.risk = risk_mod.RiskPolicy.from_env()

        def evaluate_and_maybe_mint(self, price, mint_rate=1.0, desired_units=None, current_inventory_usd=None):  # noqa: ANN001
            seen["inventory_usd"] = current_inventory_usd
            # Return True to mark a decision
            return True

    os.environ["RISK_ENABLE_PORTFOLIO_CAP"] = "true"
    os.environ["DIEM_INVENTORY_UNITS"] = str(10 ** 18)  # 1 DIEM
    os.environ["USDC_INVENTORY_UNITS"] = str(1_000_000)  # 1 USDC
    os.environ["VVV_INVENTORY_UNITS"] = "0"

    orch = orch_mod.Orchestrator(market=FakeMarket(), arbi=FakeArbi())
    rec = orch.run_once(dry_run=False)
    assert rec["action"] == "mint_sell"
    # Exposure = 1*2.0 + 1*1.0 = 3.0 USD
    assert seen["inventory_usd"] and abs(float(seen["inventory_usd"]) - 3.0) < 1e-6

