from __future__ import annotations

import pytest

from services.broker.inventory import (
    IntakePaused,
    assert_intake_open,
    broker_inventory_utilization,
    clear_inventory_policy_cache,
    inventory_utilization_ratio,
    save_inventory_policy,
)


def test_broker_inventory_utilization_from_usage_limits() -> None:
    usage = {"dailyAverageDiem": 90.0}
    limits = {"data": [{"consumptionLimit": {"diem": 100.0}}]}
    assert broker_inventory_utilization(usage, limits) == pytest.approx(0.9)


def test_quote_markup_rises_with_inventory_utilization(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_USDC", "1000000")
    monkeypatch.setenv("PRICE_UTIL_ALPHA", "1.0")
    monkeypatch.setenv("PRICE_DISCOUNT_USDC_BPS", "0")
    monkeypatch.setenv("PRICE_DISCOUNT_DEFAULT_BPS", "0")
    monkeypatch.setenv("BROKER_INVENTORY_POLICY_PATH", str(tmp_path / "policy.json"))

    from services.pricing.service import PricingService

    clear_inventory_policy_cache()
    save_inventory_policy(utilization=0.0, status="calm")
    low = PricingService().get_quote(units=1.0, asset="USDC")

    clear_inventory_policy_cache()
    save_inventory_policy(utilization=1.0, status="hot")
    high = PricingService().get_quote(units=1.0, asset="USDC")

    assert int(high["unitPriceBeforeDiscount"]) > int(low["unitPriceBeforeDiscount"])
    assert inventory_utilization_ratio() == pytest.approx(1.0)


def test_hot_failsafe_pauses_intake(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("BROKER_INVENTORY_POLICY_PATH", str(tmp_path / "policy.json"))
    clear_inventory_policy_cache()
    save_inventory_policy(utilization=0.92, status="hot")
    with pytest.raises(IntakePaused):
        assert_intake_open()

    clear_inventory_policy_cache()
    save_inventory_policy(utilization=0.2, status="calm")
    assert_intake_open()
