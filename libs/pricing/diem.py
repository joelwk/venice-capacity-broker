from __future__ import annotations

from typing import Any

SCARCITY_WEIGHT = 0.30
SCARCITY_MIN = 0.70
SCARCITY_MAX = 1.50
DEMAND_WEIGHT = 0.20
DEMAND_MAX = 1.20
SENTIMENT_WEIGHT = 0.10
COST_WEIGHT = 0.20
UTILITY_WEIGHT = 0.80
EMISSIONS_APY = 0.10
MIN_UTILIZATION_FLOOR = 0.05
ADOPTION_BASE = 0.60
MIN_ADOPTION = 0.25
MAX_ADOPTION = 0.90


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_float(value: float | None) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def fair_value_per_diem(
    vvv_price: float,
    mint_rate: float = 1.0,
    emissions_penalty: float = 0.20,
    utilization_current: float | None = None,
    utilization_trend: float | None = None,
    circulating_supply: float | None = None,
    target_supply: int = 38_000,
    discount_rate_apy: float = 0.15,
    growth_rate_apy: float = 0.05,
    historical_ratio: float | None = None,
    horizon_days: int | None = None,
    adoption_base: float | None = None,
    has_onchain_liquidity: bool = True,
    illiquidity_discount: float | None = None,
    market_price: float | None = None,
) -> dict[str, Any]:
    """
    Estimate the fair value of a DIEM token using a finite-horizon PV model.

    The result blends production cost, emissions drag, and the finite-horizon PV
    of $1/day compute utility (with adoption-based scaling), then applies scarcity,
    demand, sentiment, and illiquidity multipliers. Returns a dictionary containing
    the final value, intermediate components, and a confidence score.

    Args:
        vvv_price: Current VVV market price in USD
        mint_rate: sVVV tokens required per DIEM (normalized to token units)
        emissions_penalty: Opportunity cost rate (default 0.20 = 20%)
        utilization_current: Current utilization ratio (0..1)
        utilization_trend: Utilization trend signal (0..1)
        circulating_supply: Current DIEM circulating supply
        target_supply: Target DIEM supply for scarcity calculation (default 38,000)
        discount_rate_apy: Annual discount rate (default 0.15 = 15%)
        growth_rate_apy: Expected growth rate (default 0.05 = 5%)
        historical_ratio: Historical DIEM/VVV price ratio for sentiment
        horizon_days: PV calculation horizon (default 365, env: DIEM_FAIR_VALUE_HORIZON_DAYS)
        adoption_base: Baseline adoption when util unknown (default 0.60, env: DIEM_ADOPTION_BASE)
        has_onchain_liquidity: Whether on-chain DEX liquidity exists
        illiquidity_discount: Discount multiplier when no liquidity (default 0.80, env: DIEM_ILLIQUIDITY_DISCOUNT)
        market_price: Observed DIEM market price for optional blending (env: DIEM_FAIR_VALUE_BLEND_MARKET)

    Returns:
        Dict with 'fair_value', 'components', and 'confidence'
    """

    import os

    price = max(_safe_float(vvv_price) or 0.0, 0.0)
    rate = max(_safe_float(mint_rate) or 0.0, 0.0)

    mint_cost = price * rate
    emissions_cost = mint_cost * max(emissions_penalty, 0.0) * EMISSIONS_APY
    base_cost = mint_cost + emissions_cost

    # Determine effective adoption/utilization
    util_inputs = [
        u
        for u in (utilization_current, utilization_trend)
        if _safe_float(u) is not None
    ]
    if util_inputs:
        util_effective = sum(
            _safe_float(u) for u in util_inputs if _safe_float(u) is not None
        ) / len(util_inputs)
    else:
        # Use adoption baseline when utilization is unknown.
        env_adoption_base: float | None = None
        if adoption_base is None:
            for name in ("DIEM_FV_ADOPTION_BASE", "DIEM_ADOPTION_BASE"):
                raw = os.getenv(name)
                if raw is None or str(raw).strip() == "":
                    continue
                try:
                    val = float(raw)
                except Exception:
                    continue
                if val >= 0 and val <= 1:
                    env_adoption_base = val
                    break
        util_effective = (
            adoption_base
            if adoption_base is not None
            else (env_adoption_base if env_adoption_base is not None else ADOPTION_BASE)
        )

    # Clamp adoption to reasonable bounds
    adoption = _clamp(util_effective, MIN_ADOPTION, MAX_ADOPTION)

    scarcity_multiplier = 1.0
    circ = _safe_float(circulating_supply)
    if circ is not None and target_supply > 0:
        supply_ratio = circ / float(target_supply)
        if supply_ratio < 1.0:
            scarcity_multiplier = 1.0 + (1.0 - supply_ratio) * SCARCITY_WEIGHT
        else:
            scarcity_multiplier = 1.0 - (supply_ratio - 1.0) * (SCARCITY_WEIGHT / 2.0)
        scarcity_multiplier = _clamp(scarcity_multiplier, SCARCITY_MIN, SCARCITY_MAX)

    demand_multiplier = 1.0 + adoption * DEMAND_WEIGHT
    demand_multiplier = _clamp(demand_multiplier, 1.0, DEMAND_MAX)

    # Finite-horizon PV calculation with adoption-based scaling
    # Get horizon from env or use default
    h_days = horizon_days
    if h_days is None:
        try:
            h_days = int(os.getenv("DIEM_FAIR_VALUE_HORIZON_DAYS", "365"))
        except Exception:
            h_days = 365
    h_days = max(30, min(h_days, 1825))  # Clamp to 30 days - 5 years

    # Calculate net discount rate
    r_apy = max(discount_rate_apy, 0.0001)
    g_apy = max(growth_rate_apy, 0.0)
    net_discount_apy = max(r_apy - g_apy, 0.05)  # Minimum 5% net discount

    # Convert APY to daily rate: (1 + r_apy)^(1/365) - 1
    d_daily = (1.0 + net_discount_apy) ** (1.0 / 365.0) - 1.0

    # DIEM provides $1/day of compute when staked (per tokenomics)
    daily_value = 1.0

    # Finite-horizon annuity PV formula: PV = C × (1 - (1 + r)^(-n)) / r
    # Scale by adoption to reflect expected utilization
    if d_daily > 0:
        discount_factor = (1.0 + d_daily) ** (-h_days)
        pv_horizon = adoption * daily_value * (1.0 - discount_factor) / d_daily
    else:
        # Fallback if daily rate is zero
        pv_horizon = adoption * daily_value * h_days

    utility_component = pv_horizon

    sentiment_adjustment = 1.0
    ratio = _safe_float(historical_ratio)
    if ratio is not None and ratio > 0:
        deviation = ratio - 1.0
        sentiment_adjustment += _clamp(deviation * SENTIMENT_WEIGHT, -0.5, 0.5)
        sentiment_adjustment = max(0.5, sentiment_adjustment)

    blended_base = COST_WEIGHT * base_cost + UTILITY_WEIGHT * utility_component
    fair_value_pre = (
        blended_base * scarcity_multiplier * demand_multiplier * sentiment_adjustment
    )

    # Apply illiquidity discount when no on-chain DEX liquidity exists
    illiq_discount = illiquidity_discount
    if illiq_discount is None:
        try:
            illiq_discount = float(os.getenv("DIEM_ILLIQUIDITY_DISCOUNT", "0.80"))
        except Exception:
            illiq_discount = 0.80
    illiq_discount = _clamp(illiq_discount, 0.5, 1.0)

    illiq_discount_applied = 1.0
    if not has_onchain_liquidity:
        illiq_discount_applied = illiq_discount

    fair_value = fair_value_pre * illiq_discount_applied

    blend_ratio = 0.0
    try:
        blend_ratio = float(os.getenv("DIEM_FAIR_VALUE_BLEND_MARKET", "0.0"))
    except Exception:
        blend_ratio = 0.0
    blend_ratio = _clamp(blend_ratio, 0.0, 1.0)
    market_px = _safe_float(market_price)
    if blend_ratio > 0 and market_px is not None and market_px > 0:
        fair_value = fair_value * (1.0 - blend_ratio) + market_px * blend_ratio

    # Always respect mint cost floor
    fair_value = max(fair_value, base_cost)

    confidence = 1.0
    if circ is None:
        confidence -= 0.15
    if not util_inputs:
        confidence -= 0.2
    if ratio is None:
        confidence -= 0.1
    if fair_value <= 0:
        confidence = 0.0
    confidence = _clamp(confidence, 0.0, 1.0)

    components: dict[str, Any] = {
        "mint_cost": mint_cost,
        "emissions_cost": emissions_cost,
        "base_cost": base_cost,
        "pv_horizon": pv_horizon,
        "utility_component": utility_component,
        "scarcity_multiplier": scarcity_multiplier,
        "demand_multiplier": demand_multiplier,
        "sentiment_adjustment": sentiment_adjustment,
        "adoption": adoption,
        "horizon_days": h_days,
        "net_discount_apy": net_discount_apy,
        "blended_base": blended_base,
        "illiquidity_discount_applied": illiq_discount_applied,
        "has_onchain_liquidity": has_onchain_liquidity,
        "blend_ratio": blend_ratio,
        "market_price": market_px,
    }

    return {
        "fair_value": float(fair_value),
        "components": components,
        "confidence": confidence,
    }
