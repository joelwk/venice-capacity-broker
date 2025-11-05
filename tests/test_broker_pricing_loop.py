"""Tests for broker pricing loop with hysteresis."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from agents.capacity_broker.agent import CapacityBroker
from services.venice_keys.manager import KeyManager


def test_broker_pricing_hysteresis():
    """Test that broker pricing uses hysteresis to prevent oscillation."""
    os.environ["BROKER_UTIL_TARGET"] = "0.65"
    os.environ["BROKER_HYSTERESIS_WINDOW"] = "0.05"
    os.environ["BROKER_PRICE_STEP_BPS"] = "50"
    
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


def test_broker_pricing_stepwise_adjustment():
    """Test that broker pricing adjusts stepwise."""
    os.environ["BROKER_UTIL_TARGET"] = "0.65"
    os.environ["BROKER_PRICE_STEP_BPS"] = "50"
    os.environ["BROKER_BASE_PRICE_USD"] = "1.0"
    
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


def test_broker_tracks_price_history():
    """Test that broker tracks price history for rollback."""
    mock_keys = MagicMock(spec=KeyManager)
    broker = CapacityBroker(keys=mock_keys)
    
    # Run multiple cycles
    for util in [0.70, 0.75, 0.65, 0.60]:
        summary = broker.run_once(parent_key="test_key", enforce_limits=False)
        if summary.get("pricing"):
            assert "lastApplied" in summary["pricing"]
            assert "historyLength" in summary["pricing"]
            assert summary["pricing"]["historyLength"] <= 10

