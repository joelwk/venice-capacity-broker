"""Tests for enhanced DIEM fair value model with finite-horizon PV, adoption, and illiquidity discount."""

from __future__ import annotations

from libs.pricing.diem import fair_value_per_diem


def _result(**kwargs):
    defaults = {
        "vvv_price": 1.36,
        "mint_rate": 1.0,
        "emissions_penalty": 0.2,
    }
    defaults.update(kwargs)
    return fair_value_per_diem(**defaults)


def test_adoption_baseline_used_when_util_missing() -> None:
    """When utilization is unknown, should use adoption_base (default 0.60)."""
    result = _result(utilization_current=None, adoption_base=0.60)
    assert result["components"]["adoption"] == 0.60

    # With explicit adoption_base
    result2 = _result(utilization_current=None, adoption_base=0.75)
    assert result2["components"]["adoption"] == 0.75

    # Higher adoption should yield higher fair value
    assert result2["fair_value"] > result["fair_value"]


def test_finite_horizon_pv_calculation() -> None:
    """Verify finite-horizon PV produces reasonable values."""
    # 365-day horizon should produce fair value in $100-200 range
    result = _result(horizon_days=365, adoption_base=0.60, has_onchain_liquidity=True)
    fair = result["fair_value"]
    pv = result["components"]["pv_horizon"]

    # PV should be in reasonable range (not $7,300 perpetuity)
    assert 150 <= pv <= 300  # ~200-250 expected for 365 days at 10% net discount
    assert 80 <= fair <= 250  # After blending and multipliers

    # Longer horizon should yield higher PV
    result_long = _result(
        horizon_days=730, adoption_base=0.60, has_onchain_liquidity=True
    )
    assert result_long["components"]["pv_horizon"] > pv


def test_illiquidity_discount_applied() -> None:
    """When no on-chain liquidity exists, apply 20% discount."""
    with_liq = _result(has_onchain_liquidity=True, illiquidity_discount=0.80)
    without_liq = _result(has_onchain_liquidity=False, illiquidity_discount=0.80)

    # Without liquidity should be ~80% of with liquidity
    ratio = without_liq["fair_value"] / with_liq["fair_value"]
    assert 0.75 <= ratio <= 0.85  # Should be close to 0.80

    # Check discount was applied
    assert without_liq["components"]["illiquidity_discount_applied"] == 0.80
    assert with_liq["components"]["illiquidity_discount_applied"] == 1.0


def test_horizon_sensitivity() -> None:
    """Fair value should increase with longer horizons."""
    h30 = _result(horizon_days=30)
    h365 = _result(horizon_days=365)
    h730 = _result(horizon_days=730)

    assert h30["fair_value"] < h365["fair_value"]
    assert h365["fair_value"] < h730["fair_value"]

    # But not unbounded (should plateau due to discounting)
    assert h730["fair_value"] < h365["fair_value"] * 2.0


def test_market_alignment() -> None:
    """With current market conditions, fair value should be in $100-180 range."""
    # Simulate current market: VVV=$1.36, no on-chain liquidity, 60% adoption
    result = _result(
        vvv_price=1.36,
        mint_rate=1.0,
        utilization_current=None,  # Will use adoption_base
        adoption_base=0.60,
        horizon_days=365,
        has_onchain_liquidity=False,  # No DIEM DEX pools
        illiquidity_discount=0.80,
        discount_rate_apy=0.15,
        growth_rate_apy=0.05,
    )

    fair = result["fair_value"]
    # Should be in range that makes $123 market price reasonable (slight premium or discount)
    assert 100 <= fair <= 180

    # Premium calculation
    market_px = 123.49
    premium = market_px / fair
    # Should show modest premium (0.7-1.3×) not extreme (17×)
    assert 0.7 <= premium <= 1.5


def test_adoption_affects_fair_value() -> None:
    """Higher adoption should increase fair value."""
    low_adoption = _result(adoption_base=0.30)
    mid_adoption = _result(adoption_base=0.60)
    high_adoption = _result(adoption_base=0.85)

    assert low_adoption["fair_value"] < mid_adoption["fair_value"]
    assert mid_adoption["fair_value"] < high_adoption["fair_value"]


def test_mint_cost_floor_respected() -> None:
    """Fair value should never go below base_cost."""
    # Even with illiquidity discount and low adoption
    result = _result(
        vvv_price=2.0,
        mint_rate=1.5,
        adoption_base=0.25,
        has_onchain_liquidity=False,
        illiquidity_discount=0.80,
    )

    base_cost = result["components"]["base_cost"]
    fair = result["fair_value"]

    assert fair >= base_cost


def test_discount_rate_affects_pv() -> None:
    """Higher discount rate should reduce PV and fair value."""
    low_discount = _result(discount_rate_apy=0.10, growth_rate_apy=0.05)  # 5% net
    high_discount = _result(discount_rate_apy=0.25, growth_rate_apy=0.05)  # 20% net

    assert (
        low_discount["components"]["pv_horizon"]
        > high_discount["components"]["pv_horizon"]
    )
    assert low_discount["fair_value"] > high_discount["fair_value"]
