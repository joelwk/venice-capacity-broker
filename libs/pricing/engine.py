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
    """Market-driven pricing with cached updates and DIEM-aware units.

    Behavior:
    - If `PURCHASE_UNITS_KIND=diem`, 1 unit == 1 DIEM. Unit price follows the live DIEM→USD price.
      Fallbacks: DIEM_PRICE_USD env, then BASE_UNIT_USD.
    - Otherwise, units are generic and priced off `BASE_UNIT_USD` (default 0.1 USD per unit).

    ETH pricing no longer requires `ETH_PRICE_USD`; we derive ETH/USD via MarketDataProvider
    using the configured `QUOTE_TOKEN_ADDRESS` path (WETH->QUOTE) with AMM fallbacks.
    """

    def __init__(self) -> None:
        import os

        self._ttl = int(os.getenv("PRICE_QUOTE_TTL_SECONDS", "120") or 120)
        # Units kind: diem|vvv|usd (default diem to match buyer flow labelling)
        self._units_kind = (os.getenv("PURCHASE_UNITS_KIND") or os.getenv("PRICE_UNITS_KIND") or "diem").strip().lower()
        # Baseline when units are generic (usd)
        try:
            self._base_unit_usd_default = float(os.getenv("BASE_UNIT_USD", "0.1") or 0.1)
        except Exception:
            self._base_unit_usd_default = 0.1
        # Optional manual hints
        try:
            self._diem_usd_hint = float(os.getenv("DIEM_PRICE_USD", "0") or 0)
        except Exception:
            self._diem_usd_hint = 0.0
        try:
            self._vvv_usd_hint = float(os.getenv("VVV_PRICE_USD", "0") or 0)
        except Exception:
            self._vvv_usd_hint = 0.0
        # Fractional unit bounds
        try:
            self._min_u = float(os.getenv("PRICE_ACCEPTED_MIN_UNITS", os.getenv("PRICE_MIN_UNITS", "0.01")) or 0.01)
        except Exception:
            self._min_u = 0.01
        try:
            self._max_u = float(os.getenv("PRICE_ACCEPTED_MAX_UNITS", "1000000") or 1_000_000)
        except Exception:
            self._max_u = 1_000_000.0

    def _resolve_prices(self) -> Tuple[float, float, float]:
        """Return tuple (base_unit_usd, diem_usd, eth_usd).

        - base_unit_usd reflects per-unit USD cost based on units kind.
        - Fetches DIEM/ETH prices from MarketDataProvider with safe fallbacks.
        """
        # Default fallbacks
        base_unit_usd = float(self._base_unit_usd_default)
        diem_usd = float(self._diem_usd_hint or 0.0)
        eth_usd = 0.0
        # Resolve via market provider (best-effort)
        try:
            from services.marketdata.provider import MarketDataProvider  # lazy import

            mdp = MarketDataProvider()
            px = mdp.prices(["DIEM", "ETH", "USDC"]) or {}
            # Prices are in QUOTE token (USDC); USDC≈1 USD
            if isinstance(px, dict):
                diem_usd = float(px.get("DIEM") or diem_usd or 0.0)
                eth_usd = float(px.get("ETH") or 0.0)
        except Exception:
            # Provider unavailable; rely on hints/env
            pass
        # Units kind mapping
        uk = str(self._units_kind or "").lower()
        if uk == "diem":
            # 1 unit == 1 DIEM
            if diem_usd and diem_usd > 0:
                base_unit_usd = float(diem_usd)
            elif self._diem_usd_hint and self._diem_usd_hint > 0:
                base_unit_usd = float(self._diem_usd_hint)
        elif uk == "vvv":
            # 1 unit == 1 VVV (rare); use hint if provided, otherwise fall back to default base
            if self._vvv_usd_hint and self._vvv_usd_hint > 0:
                base_unit_usd = float(self._vvv_usd_hint)
        else:
            # Generic USD-priced units
            base_unit_usd = float(self._base_unit_usd_default)
        return float(base_unit_usd), float(diem_usd or 0.0), float(eth_usd or 0.0)

    def price(self, units: float, asset: str) -> QuoteDraft:
        import time

        asset_u = asset.strip().upper()
        base_unit_usd, _diem_usd, eth_usd = self._resolve_prices()
        # Convert base USD price per unit into selected asset's minor units
        if asset_u == "USDC":
            unit_p = int(round(float(base_unit_usd) * 1_000_000))
        elif asset_u == "ETH":
            # Derive wei per unit using ETH/USD
            if not (eth_usd and eth_usd > 0):
                # Last-resort: try env hint
                try:
                    eth_usd = float((__import__("os").getenv("ETH_PRICE_USD") or "0").strip() or 0)
                except Exception:
                    eth_usd = 0.0
            if not (eth_usd and eth_usd > 0):
                raise ValueError("ETH pricing unavailable (missing ETH/USD); check DEX config or ETH_PRICE_USD")
            unit_p = int(round((float(base_unit_usd) / float(eth_usd)) * 1e18))
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
