import pytest

from libs.pricing.diem import fair_value_per_diem
from libs.pricing.diem_metrics import (
    build_diem_premium_snapshot,
    compute_diem_premium_attribution,
)


def test_external_reference_untrusted_price_sets_trusted_false() -> None:
    snap = build_diem_premium_snapshot(
        price_usd=1.25,
        vvv_price_usd=1.0,
        mint_rate=1.0,
        fair_value_usd=100.0,
        fair_value_components={"base_cost": 1.1, "horizon_days": 365},
        price_health={
            "source": "external_reference",
            "fallback_reason": "no_onchain_liquidity",
            "valid": True,
            "clamped": False,
            "trusted_external": False,
        },
    )
    assert snap["trustedPrice"] is False
    assert snap["premiumFair"] == pytest.approx(0.0125)
    assert snap["premiumMint"] == pytest.approx(1.25 / 1.1)


def test_external_reference_trusted_price_sets_trusted_true() -> None:
    snap = build_diem_premium_snapshot(
        price_usd=120.0,
        vvv_price_usd=1.25,
        mint_rate=1.0,
        fair_value_usd=100.0,
        fair_value_components={"base_cost": 1.375, "horizon_days": 365},
        price_health={
            "source": "external_reference",
            "fallback_reason": "no_onchain_liquidity",
            "valid": True,
            "clamped": False,
            "trusted_external": True,
        },
    )
    assert snap["trustedPrice"] is True


def test_premium_attribution_detects_fair_value_driver_changes() -> None:
    prev = build_diem_premium_snapshot(
        price_usd=100.0,
        vvv_price_usd=1.0,
        mint_rate=1.0,
        fair_value_usd=80.0,
        fair_value_components={
            "adoption": 0.60,
            "horizon_days": 365,
            "illiquidity_discount_applied": 1.0,
            "scarcity_multiplier": 1.0,
            "sentiment_adjustment": 1.0,
            "base_cost": 1.1,
        },
        price_health={"source": "bridge_vvv", "valid": True, "clamped": False},
        computed_at_ts=1.0,
    )
    cur = build_diem_premium_snapshot(
        price_usd=100.0,
        vvv_price_usd=1.0,
        mint_rate=1.0,
        fair_value_usd=120.0,
        fair_value_components={
            "adoption": 0.75,
            "horizon_days": 730,
            "illiquidity_discount_applied": 1.0,
            "scarcity_multiplier": 1.0,
            "sentiment_adjustment": 1.0,
            "base_cost": 1.1,
        },
        price_health={"source": "bridge_vvv", "valid": True, "clamped": False},
        computed_at_ts=2.0,
    )
    attr = compute_diem_premium_attribution(current=cur, previous=prev)
    assert attr["status"] == "ok"
    changes = attr["fairValueDriverChanges"]
    assert "horizon_days" in changes
    assert "adoption" in changes


def test_fair_value_horizon_env_increases_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Same inputs; longer horizon should increase PV component and thus fair value.
    monkeypatch.setenv("DIEM_FAIR_VALUE_HORIZON_DAYS", "365")
    fv_1y = fair_value_per_diem(vvv_price=1.0, mint_rate=1.0)
    monkeypatch.setenv("DIEM_FAIR_VALUE_HORIZON_DAYS", "730")
    fv_2y = fair_value_per_diem(vvv_price=1.0, mint_rate=1.0)
    assert float(fv_2y["fair_value"]) > float(fv_1y["fair_value"])
