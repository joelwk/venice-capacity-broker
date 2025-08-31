from __future__ import annotations

def decide_mint_or_sell(market_px: float, fair_daily: float) -> str:
    """Return 'mint_sell' or 'hold' based on threshold policy."""
    return "mint_sell" if market_px > fair_daily * 1.05 else "hold"

