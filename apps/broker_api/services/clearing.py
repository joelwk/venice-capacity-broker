"""
Clearing price computation for Venice Broker API.

Computes DIEM clearing price and band from market data.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.marketdata.provider import MarketDataProvider

logger = logging.getLogger("broker.api.services.clearing")


def compute_clearing_price(
    market_data_provider: MarketDataProvider,
    clearing_band_bps: int | None = None,
) -> dict:
    """
    Compute clearing price from DIEM/VVV market prices.
    
    Calculates a price band around the DIEM market price based on
    a configurable basis point spread.
    
    Args:
        market_data_provider: MarketDataProvider instance
        clearing_band_bps: Band width in basis points (default from env or 200)
        
    Returns:
        dict with keys:
            - price: DIEM market price (float)
            - bandMin: Lower band limit (float)
            - bandMax: Upper band limit (float)
            - bandBps: Band width in basis points (int)
            
    Raises:
        RuntimeError: If DIEM price is unavailable or invalid
    """
    if clearing_band_bps is None:
        try:
            raw = os.getenv("CLEARING_BAND_BPS", "200")
            clearing_band_bps = int(raw) if raw and raw.strip() else 200
        except Exception:
            clearing_band_bps = 200
    
    if clearing_band_bps <= 0:
        clearing_band_bps = 200
    
    try:
        px = market_data_provider.prices(["DIEM", "VVV"]) or {}
        diem = float(px.get("DIEM") or 0.0)
        
        if not diem or diem <= 0:
            raise RuntimeError("DIEM price unavailable")
        
        span = float(clearing_band_bps) / 10_000.0
        band_min = float(diem * (1.0 - span))
        band_max = float(diem * (1.0 + span))
        
        return {
            "price": diem,
            "bandMin": band_min,
            "bandMax": band_max,
            "bandBps": clearing_band_bps,
        }
    except RuntimeError:
        raise
    except Exception as e:
        logger.error("Failed to compute clearing price: %s", e)
        raise RuntimeError(f"clearing price computation failed: {e}") from e

