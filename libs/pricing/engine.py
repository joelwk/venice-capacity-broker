from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass
class QuoteDraft:
    quote_id: str
    units: float
    asset: str
    unit_price: int
    total_price: int
    accepted_min: Optional[float]
    accepted_max: Optional[float]
    expires_at_epoch: int


class StaticPricingEngine:
    """Simple deterministic pricing without external feeds.

    Env vars (all optional):
    - PRICE_UNIT_ETH_WEI: price per unit in wei (default 0)
    - PRICE_UNIT_USDC: price per unit in USDC minor units (default 0)
    - PRICE_ACCEPTED_MIN_UNITS, PRICE_ACCEPTED_MAX_UNITS
    - PRICE_QUOTE_TTL_SECONDS (default 120)
    """

    def __init__(self) -> None:
        self._unit_eth = int(os.getenv("PRICE_UNIT_ETH_WEI", "0") or 0)
        self._unit_usdc = int(os.getenv("PRICE_UNIT_USDC", "0") or 0)
        # Allow fractional unit bounds
        try:
            self._min_u = float(os.getenv("PRICE_ACCEPTED_MIN_UNITS", "0.01") or 0.01)
        except Exception:
            self._min_u = 0.01
        try:
            self._max_u = float(os.getenv("PRICE_ACCEPTED_MAX_UNITS", "1000000") or 1_000_000)
        except Exception:
            self._max_u = 1_000_000.0
        self._ttl = int(os.getenv("PRICE_QUOTE_TTL_SECONDS", "120") or 120)

    def price(self, units: float, asset: str) -> QuoteDraft:
        asset_u = asset.strip().upper()
        if asset_u == "ETH":
            unit_p = self._unit_eth
        elif asset_u == "USDC":
            unit_p = self._unit_usdc
        else:
            raise ValueError("unsupported asset; use ETH or USDC")
        if unit_p <= 0:
            raise ValueError("unit price not configured; set PRICE_UNIT_ETH_WEI or PRICE_UNIT_USDC")
        try:
            uf = float(units)
        except Exception:
            uf = 0.0
        uf = max(float(self._min_u), min(float(self._max_u), uf))
        total = int(round(unit_p * uf))
        now = int(time.time())
        qid = f"q-{asset_u}-{now}-{uf}"
        return QuoteDraft(
            quote_id=qid,
            units=uf,
            asset=asset_u,
            unit_price=unit_p,
            total_price=total,
            accepted_min=self._min_u,
            accepted_max=self._max_u,
            expires_at_epoch=now + self._ttl,
        )


class MarketPricingEngine:
    """Market-driven pricing with cached updates.

    Inputs via env (simple, dependency-light):
    - DIEM_PRICE_USD, VVV_PRICE_USD (optional, floats as strings)
    - BASE_UNIT_USD: price per unit in USD baseline (default 0.1)
    - PRICE_QUOTE_TTL_SECONDS: TTL for quote (default 120)
    Notes: For now, we avoid external feeds and allow ops to inject prices.
    """

    def __init__(self) -> None:
        import os

        self._ttl = int(os.getenv("PRICE_QUOTE_TTL_SECONDS", "120") or 120)
        self._base_unit_usd = float(os.getenv("BASE_UNIT_USD", "0.1") or 0.1)
        # Allow ops to set DIEM/VVV prices for signaling; unused directly in v1
        self._diem_usd = float(os.getenv("DIEM_PRICE_USD", "0") or 0)
        self._vvv_usd = float(os.getenv("VVV_PRICE_USD", "0") or 0)
        # Asset conversion rates
        self._usdc_1 = 1.0  # 1 USDC ~ 1 USD
        self._eth_usd = float(os.getenv("ETH_PRICE_USD", "0") or 0)
        # Unit pricing per asset computed from USD baseline
        self._unit_usdc_minor = int(round(self._base_unit_usd * 1_000_000))
        self._unit_eth_wei = int(round(self._base_unit_usd / self._eth_usd * 1e18)) if self._eth_usd > 0 else 0
        # Fractional unit bounds
        try:
            self._min_u = float(os.getenv("PRICE_ACCEPTED_MIN_UNITS", os.getenv("PRICE_MIN_UNITS", "0.01")) or 0.01)
        except Exception:
            self._min_u = 0.01
        try:
            self._max_u = float(os.getenv("PRICE_ACCEPTED_MAX_UNITS", "1000000") or 1_000_000)
        except Exception:
            self._max_u = 1_000_000.0

    def price(self, units: float, asset: str) -> QuoteDraft:
        import time

        asset_u = asset.strip().upper()
        if asset_u == "USDC":
            unit_p = int(self._unit_usdc_minor)
        elif asset_u == "ETH":
            if self._eth_usd <= 0:
                raise ValueError("ETH_PRICE_USD not configured for market pricing")
            unit_p = int(self._unit_eth_wei)
        else:
            raise ValueError("unsupported asset; use ETH or USDC")
        if unit_p <= 0:
            raise ValueError("unit price not configured for selected asset")
        try:
            uf = float(units)
        except Exception:
            uf = 0.0
        uf = max(float(self._min_u), min(float(self._max_u), uf))
        total = int(round(unit_p * uf))
        now = int(time.time())
        qid = f"qM-{asset_u}-{now}-{uf}"
        return QuoteDraft(
            quote_id=qid,
            units=uf,
            asset=asset_u,
            unit_price=unit_p,
            total_price=total,
            accepted_min=self._min_u,
            accepted_max=self._max_u,
            expires_at_epoch=now + self._ttl,
        )
