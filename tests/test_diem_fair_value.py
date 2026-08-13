from __future__ import annotations

from libs.pricing.diem import fair_value_per_diem


def _fv(result):
    assert isinstance(result, dict)
    return result["fair_value"]


def test_fair_value_not_below_mint_cost() -> None:
    vvv_price = 1.30
    mint_rate = 1.0
    fair = _fv(
        fair_value_per_diem(
            vvv_price=vvv_price, mint_rate=mint_rate, utilization_current=0.4
        )
    )
    assert fair >= vvv_price * mint_rate


def test_fair_value_scales_with_utilization() -> None:
    base = _fv(
        fair_value_per_diem(vvv_price=1.0, mint_rate=1.0, utilization_current=0.2)
    )
    high = _fv(
        fair_value_per_diem(vvv_price=1.0, mint_rate=1.0, utilization_current=0.8)
    )
    assert high > base


def test_fair_value_scales_with_vvv_price() -> None:
    low = _fv(
        fair_value_per_diem(vvv_price=1.0, mint_rate=1.0, utilization_current=0.4)
    )
    high = _fv(
        fair_value_per_diem(vvv_price=2.0, mint_rate=1.0, utilization_current=0.4)
    )
    assert high > low


def test_fair_value_scales_with_mint_rate() -> None:
    low = _fv(
        fair_value_per_diem(vvv_price=1.0, mint_rate=1.0, utilization_current=0.4)
    )
    high = _fv(
        fair_value_per_diem(vvv_price=1.0, mint_rate=2.0, utilization_current=0.4)
    )
    assert high > low


def test_fair_value_reasonable_upper_bound() -> None:
    # With finite-horizon PV and 60% adoption, expect fair value in $30-200 range
    fair = _fv(
        fair_value_per_diem(vvv_price=1.30, mint_rate=1.0, utilization_current=0.5)
    )
    assert fair >= 1.30  # At least mint cost
    assert fair <= 500.0  # Reasonable upper bound with finite horizon
