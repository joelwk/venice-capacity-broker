"""Tests for broker pricing loop with hysteresis."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agents.capacity_broker.agent import CapacityBroker
from services.venice_keys.manager import KeyManager


def test_broker_pricing_hysteresis(monkeypatch):
    """Test that broker pricing uses hysteresis to prevent oscillation."""
    monkeypatch.setenv("BROKER_UTIL_TARGET", "0.65")
    monkeypatch.setenv("BROKER_HYSTERESIS_WINDOW", "0.05")
    monkeypatch.setenv("BROKER_PRICE_STEP_BPS", "50")
    monkeypatch.delenv("BROKER_UTIL_SURGE_THRESHOLD", raising=False)

    mock_keys = MagicMock(spec=KeyManager)
    broker = CapacityBroker(keys=mock_keys)

    # First call with utilization above target
    pricing1, _ = broker._derive_inventory_policy(0.70)
    assert pricing1 is not None
    assert "proposed" in pricing1
    price1 = pricing1["proposed"]

    # Update last price
    broker._last_price = price1

    # Second call with utilization just slightly above target (within hysteresis)
    pricing2, _ = broker._derive_inventory_policy(0.68)
    assert pricing2 is not None
    assert "proposed" in pricing2
    price2 = pricing2["proposed"]

    # Price should not change drastically within hysteresis window
    assert abs(price2 - price1) < price1 * 0.1  # Within 10%


def test_broker_pricing_stepwise_adjustment(monkeypatch):
    """Test that broker pricing adjusts stepwise."""
    monkeypatch.setenv("BROKER_UTIL_TARGET", "0.65")
    monkeypatch.setenv("BROKER_PRICE_STEP_BPS", "50")
    monkeypatch.setenv("BROKER_BASE_PRICE_USD", "1.0")
    monkeypatch.setenv("BROKER_UTIL_SURGE_THRESHOLD", "0.75")

    mock_keys = MagicMock(spec=KeyManager)
    broker = CapacityBroker(keys=mock_keys)
    broker._last_price = 1.0

    # High utilization should increase price
    pricing, _ = broker._derive_inventory_policy(0.80)
    assert pricing is not None
    assert pricing["mode"] == "surge"
    assert pricing["proposed"] > broker._last_price

    # Low utilization should decrease price
    broker._last_price = 1.0
    pricing, _ = broker._derive_inventory_policy(0.30)
    assert pricing is not None
    assert pricing["mode"] == "discount"
    assert pricing["proposed"] < broker._last_price


def test_broker_tracks_price_history(monkeypatch):
    """Test that broker tracks price history for rollback."""
    monkeypatch.setenv("BROKER_BASE_PRICE_USD", "1.0")
    monkeypatch.setenv("BROKER_PRICE_STEP_BPS", "50")
    monkeypatch.setenv("BROKER_UTIL_TARGET", "0.65")
    monkeypatch.setenv("BROKER_UTIL_SURGE_THRESHOLD", "0.80")
    monkeypatch.setenv("BROKER_UTIL_RELAX_THRESHOLD", "0.40")
    monkeypatch.setenv("BROKER_HYSTERESIS_WINDOW", "0.05")

    mock_keys = MagicMock(spec=KeyManager)
    client = MagicMock()
    client.config = SimpleNamespace(api_key="parent-key")

    usage_iter = iter([70.0, 75.0, 65.0, 60.0])

    def _usage():
        try:
            val = next(usage_iter)
        except StopIteration:
            val = 60.0
        return {"dailyAverageDiem": val}

    client.get_usage.side_effect = _usage
    client.get_rate_limits.return_value = {
        "data": [
            {"consumptionLimit": {"diem": 100.0}, "expiresAt": "2099-01-01T00:00:00Z"}
        ]
    }
    mock_keys.client = client

    broker = CapacityBroker(keys=mock_keys)

    # Run multiple cycles
    for util in [0.70, 0.75, 0.65, 0.60]:
        summary = broker.run_once(parent_key="test_key", enforce_limits=False)
        if summary.get("pricing"):
            assert "lastApplied" in summary["pricing"]
            assert "historyLength" in summary["pricing"]
            assert summary["pricing"]["historyLength"] <= 10
