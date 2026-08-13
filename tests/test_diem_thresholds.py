from __future__ import annotations

import os
from importlib import import_module


def setup_module(module):
    # Ensure defaults do not interfere
    os.environ.pop("DIEM_FAIR_ALPHA", None)
    os.environ.pop("DIEM_PREMIUM_THRESHOLD", None)


def _constant_fair_value(**_: object) -> float:
    return 1.0


def test_diem_controller_thresholds_mint_sell(monkeypatch):
    # Configure threshold so that premium triggers mint_sell
    os.environ["DIEM_FAIR_ALPHA"] = "0.2"  # ignored by stubbed fair_value_per_diem
    os.environ["DIEM_PREMIUM_THRESHOLD"] = "1.10"

    # Stub MarketDataProvider.prices to return DIEM price
    md_mod = import_module("services.marketdata.provider")

    class FakeMD:
        def prices(self, symbols):
            return {"DIEM": 1.2, "VVV": 1.0}

    monkeypatch.setattr(md_mod, "MarketDataProvider", FakeMD, raising=True)

    # Stub fair_value_per_diem to return constant fair value
    diem_mod = import_module("libs.pricing.diem")

    monkeypatch.setattr(
        diem_mod, "fair_value_per_diem", _constant_fair_value, raising=True
    )

    nodes = import_module("graph.langgraph.nodes")
    out = nodes.diem_controller_node({})
    assert out["decision"]["action"] == "mint_sell"
    assert out["decision"]["premium"] >= 1.10


def test_diem_controller_thresholds_hold(monkeypatch):
    os.environ["DIEM_FAIR_ALPHA"] = "0.2"
    os.environ["DIEM_PREMIUM_THRESHOLD"] = "1.10"

    md_mod = import_module("services.marketdata.provider")

    class FakeMD:
        def prices(self, symbols):
            return {"DIEM": 1.05, "VVV": 1.0}

    monkeypatch.setattr(md_mod, "MarketDataProvider", FakeMD, raising=True)

    diem_mod = import_module("libs.pricing.diem")
    monkeypatch.setattr(
        diem_mod, "fair_value_per_diem", _constant_fair_value, raising=True
    )

    nodes = import_module("graph.langgraph.nodes")
    out = nodes.diem_controller_node({})
    assert out["decision"]["action"] == "hold"
