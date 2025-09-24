from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


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
    usd_per_unit: Optional[float] = None
    market_prices: Optional[Dict[str, float]] = None


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
        usd_hint = None
        if asset_u == "USDC":
            usd_hint = unit_p / 1_000_000
        return QuoteDraft(
            quote_id=qid,
            units=uf,
            asset=asset_u,
            unit_price=unit_p,
            total_price=total,
            accepted_min=self._min_u,
            accepted_max=self._max_u,
            expires_at_epoch=now + self._ttl,
            usd_per_unit=usd_hint,
            market_prices=None,
        )

    def price_from_budget(self, budget_usd: float, asset: str) -> QuoteDraft:
        """Budget-aware pricing is unsupported in static mode.

        Static pricing lacks USD context for ETH quotes, so we surface a clear
        error for callers; the API layer translates this into a 400 response.
        """
        raise ValueError("budget-based quotes require PRICE_ENGINE=market")


class MarketPricingEngine:
    """Market-driven pricing with cached updates and DIEM-aware units.

    Behavior:
    - If `PURCHASE_UNITS_KIND=diem`, 1 unit == 1 DIEM. Unit price follows the live DIEM→USD price.
      Fallbacks: on-chain AMM mid-price and aggregator WETH bridge only.
    - Otherwise, units are generic and priced off `BASE_UNIT_USD` (default 0.1 USD per unit).

    ETH pricing derives ETH/USD via MarketDataProvider using the configured `QUOTE_TOKEN_ADDRESS`
    path (WETH->QUOTE) with AMM fallbacks. No static price overrides.
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
        # No manual price overrides in market mode
        self._diem_usd_hint = 0.0
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

        self._prefetched_prices: Optional[Dict[str, float]] = None
        self._prefetched_provider: Optional[Any] = None

    @staticmethod
    def _valid_price(value: Optional[float]) -> bool:
        try:
            v = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return 1e-6 < v < 1e6

    def set_prefetched_prices(self, provider: Any, prices: Dict[str, float]) -> None:
        cleaned: Dict[str, float] = {}
        if prices:
            for key, value in prices.items():
                try:
                    cleaned[str(key).upper()] = float(value)
                except Exception:
                    continue
        self._prefetched_prices = cleaned if cleaned else None
        self._prefetched_provider = provider

    def price_from_budget(self, budget_amount: float, asset: str) -> QuoteDraft:
        try:
            raw_budget = float(budget_amount)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid budget") from exc
        if not self._valid_price(raw_budget):
            raise ValueError("budget must be greater than zero")
        asset_u = asset.strip().upper()
        base_unit_usd, price_map = self._resolve_prices()
        eth_usd = float(price_map.get("ETH") or price_map.get("WETH") or 0.0)
        wbtc_usd = float(price_map.get("WBTC") or 0.0)
        if asset_u in {"ETH", "WETH"}:
            if not self._valid_price(eth_usd):
                raise ValueError("ETH pricing unavailable for budget sizing")
            budget = raw_budget * float(eth_usd)
        elif asset_u == "WBTC":
            if not self._valid_price(wbtc_usd):
                raise ValueError("WBTC pricing unavailable for budget sizing")
            budget = raw_budget * float(wbtc_usd)
        elif asset_u == "USDC":
            budget = raw_budget  # USDC budgets already denominated in USD
        else:
            raise ValueError("unsupported asset; use ETH, WBTC, or USDC")
        if not self._valid_price(base_unit_usd):
            raise ValueError("DIEM pricing unavailable for budget sizing")
        target_units = budget / float(base_unit_usd)
        if target_units <= 0:
            raise ValueError("budget too small for minimum unit size")
        min_units = float(self._min_u)
        if target_units < min_units:
            raise ValueError(f"budget must cover at least {min_units} DIEM (current min quote size)")
        return self.price(target_units, asset)

    def _resolve_prices(self) -> Tuple[float, Dict[str, float]]:
        """Return per-unit USD baseline alongside a market price map.

        The price map always includes at least USDC (1.0) and best-effort
        entries for DIEM, ETH, WETH, and WBTC when available from the
        MarketDataProvider.
        """

        def _valid(v: Optional[float]) -> bool:
            try:
                if v is None:
                    return False
                return self._valid_price(float(v))
            except Exception:
                return False

        base_unit_usd = float(self._base_unit_usd_default)
        prices: Dict[str, float] = {"USDC": 1.0}
        diem_usd = 0.0
        eth_usd = 0.0
        wbtc_usd = 0.0

        prefetched = self._prefetched_prices or None
        pref_provider = self._prefetched_provider
        self._prefetched_prices = None
        self._prefetched_provider = None

        try:
            from services.marketdata.provider import MarketDataProvider  # lazy import

            if pref_provider is not None and hasattr(pref_provider, "prices"):
                mdp = pref_provider  # type: ignore[assignment]
            else:
                mdp = MarketDataProvider()
            px_source = prefetched if prefetched is not None else (mdp.prices(["DIEM", "ETH", "USDC", "WBTC"]) or {})
            px = dict(px_source) if isinstance(px_source, dict) else {}
            if _valid(px.get("DIEM")):
                diem_usd = float(px["DIEM"])  # type: ignore[index]
            if _valid(px.get("ETH")):
                eth_usd = float(px["ETH"])  # type: ignore[index]
            if _valid(px.get("WBTC")):
                wbtc_usd = float(px["WBTC"])  # type: ignore[index]
            if not self._valid_price(diem_usd):
                try:
                    alt = mdp.diem_price_with_fallback()
                    if self._valid_price(alt):
                        diem_usd = float(alt)
                    else:
                        diem_usd = 0.0
                except Exception:
                    diem_usd = 0.0
        except Exception:
            diem_usd = 0.0
            eth_usd = 0.0
            wbtc_usd = 0.0

        if self._valid_price(diem_usd):
            prices["DIEM"] = float(diem_usd)
        if self._valid_price(eth_usd):
            prices["ETH"] = float(eth_usd)
            prices["WETH"] = float(eth_usd)
        if self._valid_price(wbtc_usd):
            prices["WBTC"] = float(wbtc_usd)

        uk = str(self._units_kind or "").lower()
        if uk == "diem":
            if self._valid_price(diem_usd):
                base_unit_usd = float(diem_usd)
        elif uk == "vvv":
            try:
                from services.marketdata.provider import MarketDataProvider  # lazy import

                px_vvv = MarketDataProvider().prices(["VVV"]) or {}
                vvv = float(px_vvv.get("VVV") or 0.0)
                if self._valid_price(vvv):
                    base_unit_usd = vvv
                    prices["VVV"] = vvv
            except Exception:
                pass
        else:
            base_unit_usd = float(self._base_unit_usd_default)

        return float(base_unit_usd), prices

    def price(self, units: float, asset: str) -> QuoteDraft:
        import time

        asset_u = asset.strip().upper()
        base_unit_usd, price_map = self._resolve_prices()
        eth_usd = float(price_map.get("ETH") or price_map.get("WETH") or 0.0)
        wbtc_usd = float(price_map.get("WBTC") or 0.0)

        if asset_u == "USDC":
            unit_p = int(round(float(base_unit_usd) * 1_000_000))
        elif asset_u in {"ETH", "WETH"}:
            if not (eth_usd and eth_usd > 0):
                raise ValueError("ETH pricing unavailable (missing ETH/USD from DEX); check QUOTE_TOKEN_ADDRESS and WETH route")
            unit_p = int(round((float(base_unit_usd) / float(eth_usd)) * 1e18))
        elif asset_u == "WBTC":
            if not (wbtc_usd and wbtc_usd > 0):
                raise ValueError("WBTC pricing unavailable (missing WBTC/USD liquidity); configure WBTC_TOKEN_ADDRESS and trade paths")
            unit_p = int(round((float(base_unit_usd) / float(wbtc_usd)) * 1e8))
        else:
            raise ValueError("unsupported asset; use ETH, WBTC, or USDC")
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
            usd_per_unit=float(base_unit_usd),
            market_prices=dict(price_map),
        )
