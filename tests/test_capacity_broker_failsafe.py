from __future__ import annotations

from agents.capacity_broker.agent import CapacityBroker
from services.venice_keys.manager import KeyManager


class DummyClient:
    def __init__(self, usage, limits):
        self._usage = usage
        self._limits = limits
        self.config = type("cfg", (), {"api_key": "PARENT"})()

    def get_usage(self):
        return self._usage

    def get_rate_limits(self):
        return self._limits


def test_capacity_broker_triggers_failsafe(monkeypatch):
    monkeypatch.setenv("BROKER_UTIL_SURGE_THRESHOLD", "0.80")
    monkeypatch.setenv("BROKER_UTIL_RELAX_THRESHOLD", "0.30")
    monkeypatch.setenv("BROKER_BASE_PRICE_USD", "1.00")
    monkeypatch.setenv("BROKER_SURGE_MULTIPLIER", "1.5")

    usage = {"dailyAverageDiem": 90.0}
    limits = {"data": [{"consumptionLimit": {"diem": 100.0}}]}

    broker = CapacityBroker(keys=KeyManager(client=DummyClient(usage, limits)))
    res = broker.run_once(parent_key="PARENT")

    assert res["status"] == "ok"
    assert res["utilization"] > 0.8
    assert res["pricing"]["mode"] == "surge"
    assert res["inventoryFailsafe"]["status"] == "hot"
    assert "pause_low_tier" in res["inventoryFailsafe"]["actions"]


def test_capacity_broker_relaxed_mode(monkeypatch):
    monkeypatch.setenv("BROKER_UTIL_SURGE_THRESHOLD", "0.80")
    monkeypatch.setenv("BROKER_UTIL_RELAX_THRESHOLD", "0.30")
    usage = {"dailyAverageDiem": 10.0}
    limits = {"data": [{"consumptionLimit": {"diem": 100.0}}]}

    broker = CapacityBroker(keys=KeyManager(client=DummyClient(usage, limits)))
    res = broker.run_once(parent_key="PARENT")

    assert res["pricing"]["mode"] == "discount"
    assert res["inventoryFailsafe"]["status"] == "calm"
