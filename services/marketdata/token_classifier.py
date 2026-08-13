"""Token characteristic detection for dynamic routing thresholds."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TokenCharacteristics:
    """Detected characteristics for a token relevant to routing decisions."""

    requires_high_liquidity: bool
    min_liquidity_usd: float
    prefer_multi_hop: bool
    max_drift_from_bridge: float


DEFAULT_MIN_LIQUIDITY_USD = 5_000.0
HIGH_VALUE_MIN_LIQUIDITY_USD = 50_000.0
DEFAULT_MAX_DRIFT = 0.50  # 50%
HIGH_VALUE_MAX_DRIFT = 0.30  # 30%


def classify_token(
    address: str,
    price_usd: float | None,
    liquidity_usd: float | None,
) -> TokenCharacteristics:
    """Return token characteristics for dynamic thresholds.

    Args:
        address: ERC-20 token address (unused for now, but reserved for future heuristics).
        price_usd: Best-effort token price estimate in USD.
        liquidity_usd: Observed liquidity for token routes in USD.

    Returns:
        TokenCharacteristics describing routing preferences.
    """

    # Fallback defaults when data unavailable
    price = float(price_usd) if price_usd is not None else 0.0
    liquidity = float(liquidity_usd) if liquidity_usd is not None else 0.0

    is_high_value = price > 10.0
    is_low_liquidity = liquidity > 0 and liquidity < HIGH_VALUE_MIN_LIQUIDITY_USD

    requires_high_liquidity = is_high_value
    prefer_multi_hop = is_high_value and is_low_liquidity

    min_liquidity = (
        HIGH_VALUE_MIN_LIQUIDITY_USD
        if requires_high_liquidity
        else DEFAULT_MIN_LIQUIDITY_USD
    )
    max_drift = HIGH_VALUE_MAX_DRIFT if prefer_multi_hop else DEFAULT_MAX_DRIFT

    return TokenCharacteristics(
        requires_high_liquidity=requires_high_liquidity,
        min_liquidity_usd=min_liquidity,
        prefer_multi_hop=prefer_multi_hop,
        max_drift_from_bridge=max_drift,
    )


def characteristics_as_dict(
    characteristics: TokenCharacteristics,
) -> dict[str, float | bool]:
    """Utility helper for logging/debugging."""

    return {
        "requires_high_liquidity": characteristics.requires_high_liquidity,
        "min_liquidity_usd": characteristics.min_liquidity_usd,
        "prefer_multi_hop": characteristics.prefer_multi_hop,
        "max_drift_from_bridge": characteristics.max_drift_from_bridge,
    }
