from __future__ import annotations

def fair_value_per_diem(discount_rate_apy: float) -> float:
    """Perpetuity value of $1/day at the given APY.

    Value assumes $1 per day forever, discounted at APY converted to daily.
    """
    if discount_rate_apy <= 0:
        return float("inf")
    daily_rate = (1 + discount_rate_apy) ** (1 / 365.0) - 1
    return 1.0 / daily_rate

