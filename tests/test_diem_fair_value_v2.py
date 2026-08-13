from __future__ import annotations

from libs.pricing.diem import fair_value_per_diem


def _result(**kwargs):
    defaults = {
        "vvv_price": 1.3,
        "mint_rate": 1.0,
        "emissions_penalty": 0.2,
    }
    defaults.update(kwargs)
    return fair_value_per_diem(**defaults)


def test_components_present() -> None:
    out = _result(
        utilization_current=0.4, circulating_supply=20000, historical_ratio=1.0
    )
    assert "components" in out
    comps = out["components"]
    assert comps["scarcity_multiplier"] >= 0.4
    assert comps["demand_multiplier"] >= 1.0
    assert out["fair_value"] >= out["components"]["mint_cost"]


def test_scarcity_multiplier_increases_with_low_supply() -> None:
    balanced = _result(utilization_current=0.4, circulating_supply=38000)
    scarce = _result(utilization_current=0.4, circulating_supply=19000)
    assert (
        scarce["components"]["scarcity_multiplier"]
        > balanced["components"]["scarcity_multiplier"]
    )
    assert scarce["fair_value"] > balanced["fair_value"]


def test_oversupply_reduces_multiplier() -> None:
    balanced = _result(utilization_current=0.4, circulating_supply=38000)
    oversupply = _result(utilization_current=0.4, circulating_supply=60000)
    assert (
        oversupply["components"]["scarcity_multiplier"]
        < balanced["components"]["scarcity_multiplier"]
    )
    assert oversupply["fair_value"] < balanced["fair_value"]


def test_demand_multiplier_tracks_utilization() -> None:
    low = _result(utilization_current=0.1)
    high = _result(utilization_current=0.9)
    assert (
        high["components"]["demand_multiplier"] > low["components"]["demand_multiplier"]
    )
    assert high["fair_value"] > low["fair_value"]


def test_sentiment_adjustment_affects_value() -> None:
    neutral = _result(utilization_current=0.5, historical_ratio=1.0)
    bullish = _result(utilization_current=0.5, historical_ratio=1.2)
    bearish = _result(utilization_current=0.5, historical_ratio=0.8)
    assert bullish["fair_value"] > neutral["fair_value"]
    assert bearish["fair_value"] < neutral["fair_value"]


def test_confidence_reduces_when_inputs_missing() -> None:
    full = _result(
        utilization_current=0.4,
        utilization_trend=0.4,
        circulating_supply=20000,
        historical_ratio=1.0,
    )
    missing = _result()
    assert full["confidence"] > missing["confidence"]
