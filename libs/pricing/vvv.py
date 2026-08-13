from __future__ import annotations

import os
from typing import Any


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_float(value: float | None) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _safe_int(value: int | None) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _pv_annuity(daily_cashflow: float, daily_rate: float, days: int) -> float:
    if days <= 0:
        return 0.0
    c = float(daily_cashflow)
    r = float(daily_rate)
    n = int(days)
    if c <= 0:
        return 0.0
    if r <= 0:
        return c * float(n)
    try:
        discount_factor = (1.0 + r) ** (-n)
        return c * (1.0 - discount_factor) / r
    except Exception:
        return c * float(n)


def fair_value_per_vvv(
    *,
    vvv_price_usd: float,
    horizon_days: int | None = None,
    discount_rate_apy: float | None = None,
    emissions_vvv_per_day_per_staked_vvv: float | None = None,
    diem_per_day_per_staked_vvv: float | None = None,
    diem_utility_usd_per_diem_day: float | None = None,
    locked_ratio: float | None = None,
    locked_emissions_mult: float | None = None,
) -> dict[str, Any]:
    """
    Intrinsic VVV fair value model (finite-horizon PV).

    VVV stakers earn:
      - VVV emissions (in VVV/day per staked VVV)
      - DIEM allocation (in DIEM/day per staked VVV), valued at $/DIEM-day (defaults to 1.0)

    Locked sVVV earns reduced emissions (default 0.8×) per tokenomics.

    Returns a dict containing `vvv_fair_value_usd` and a component breakdown.
    """

    price = max(_safe_float(vvv_price_usd) or 0.0, 0.0)

    h_days = _safe_int(horizon_days)
    if h_days is None:
        try:
            h_days = int(os.getenv("VVV_FV_HORIZON_DAYS", "365"))
        except Exception:
            h_days = 365
    h_days = max(1, min(int(h_days), 3650))

    r_apy = _safe_float(discount_rate_apy)
    if r_apy is None:
        try:
            r_apy = float(os.getenv("VVV_FV_DISCOUNT_APY", "0.20"))
        except Exception:
            r_apy = 0.20
    r_apy = max(float(r_apy), 0.0)
    daily_r = (1.0 + r_apy) ** (1.0 / 365.0) - 1.0 if r_apy > 0 else 0.0

    emissions = _safe_float(emissions_vvv_per_day_per_staked_vvv)
    if emissions is None:
        raw = os.getenv("VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV")
        if raw not in (None, ""):
            try:
                emissions = float(raw)
            except Exception:
                emissions = 0.0
        else:
            emissions = 0.0
    emissions = max(float(emissions), 0.0)

    diem_per_day = _safe_float(diem_per_day_per_staked_vvv)
    if diem_per_day is None:
        raw = os.getenv("VVV_FV_DIEM_PER_DAY_PER_STAKED_VVV")
        if raw not in (None, ""):
            try:
                diem_per_day = float(raw)
            except Exception:
                diem_per_day = 0.0
        else:
            diem_per_day = 0.0
    diem_per_day = max(float(diem_per_day), 0.0)

    diem_utility = _safe_float(diem_utility_usd_per_diem_day)
    if diem_utility is None:
        try:
            diem_utility = float(
                os.getenv("VVV_FV_DIEM_UTILITY_USD_PER_DIEM_DAY", "1.0")
            )
        except Exception:
            diem_utility = 1.0
    diem_utility = max(float(diem_utility), 0.0)

    locked_mult = _safe_float(locked_emissions_mult)
    if locked_mult is None:
        try:
            locked_mult = float(os.getenv("VVV_FV_LOCKED_EMISSIONS_MULT", "0.8"))
        except Exception:
            locked_mult = 0.8
    locked_mult = _clamp(float(locked_mult), 0.0, 1.0)

    lock_ratio = _safe_float(locked_ratio)
    if lock_ratio is None:
        lock_ratio = 0.0
    lock_ratio = _clamp(float(lock_ratio), 0.0, 1.0)

    emissions_mult = (1.0 - lock_ratio) + lock_ratio * locked_mult

    emissions_daily_usd = emissions * price * emissions_mult
    diem_daily_usd = diem_per_day * diem_utility

    emissions_pv_usd = _pv_annuity(emissions_daily_usd, daily_r, h_days)
    diem_utility_pv_usd = _pv_annuity(diem_daily_usd, daily_r, h_days)

    fair_value = emissions_pv_usd + diem_utility_pv_usd

    return {
        "vvv_fair_value_usd": float(fair_value),
        "components": {
            "emissions_pv_usd": float(emissions_pv_usd),
            "diem_utility_pv_usd": float(diem_utility_pv_usd),
            "horizon_days": int(h_days),
            "discount_rate_apy": float(r_apy),
            "daily_discount_rate": float(daily_r),
            "vvv_price_usd": float(price),
            "emissions_vvv_per_day_per_staked_vvv": float(emissions),
            "diem_per_day_per_staked_vvv": float(diem_per_day),
            "diem_utility_usd_per_diem_day": float(diem_utility),
            "locked_ratio": float(lock_ratio),
            "locked_emissions_mult": float(locked_mult),
            "effective_emissions_mult": float(emissions_mult),
            "emissions_daily_usd": float(emissions_daily_usd),
            "diem_daily_usd": float(diem_daily_usd),
        },
    }
