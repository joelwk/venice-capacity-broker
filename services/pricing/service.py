from __future__ import annotations

import time
from typing import Dict, Any, Optional, Type

import importlib
import os
from libs.pricing.engine import StaticPricingEngine, MarketPricingEngine
from db.session import get_session, create_db_and_tables
from libs.telemetry.events import emit as emit_event

def _refresh_session_bindings() -> None:
    """Ensure db.session helpers point to the real module (tests may stub earlier)."""
    global get_session, create_db_and_tables
    module = importlib.import_module("db.session")
    current_engine = getattr(get_session.__globals__, "get", lambda *a, **k: None)("get_engine") if hasattr(get_session, "__globals__") else None
    expected = module.__name__
    needs_reload = False
    if getattr(get_session, "__module__", None) != expected:
        needs_reload = True
    if hasattr(current_engine, "__module__") and current_engine.__module__ != expected:
        needs_reload = True
    if needs_reload:
        module = importlib.reload(module)
        get_session = module.get_session  # type: ignore[attr-defined, assignment]
        create_db_and_tables = module.create_db_and_tables  # type: ignore[attr-defined, assignment]


class PricingService:
    def __init__(self) -> None:
        # Choose engine: market or static
        if (os.getenv("PRICE_ENGINE") or "static").strip().lower() == "market":
            self.engine = MarketPricingEngine()
        else:
            self.engine = StaticPricingEngine()
        self._quote_model: Optional[Type[Any]] = None
        self._asset_decimals_map: Dict[str, int] = {
            "USDC": 6,
            "USDT": 6,
            "ETH": 18,
            "WETH": 18,
            "WBTC": 8,
        }
        _refresh_session_bindings()
        try:
            create_db_and_tables()
        except Exception:
            pass

    def _get_quote_model(self):
        """Load or reload the Quote SQLModel after optional sqlmodel stubbing."""
        if self._quote_model is not None and self._is_sqlmodel(self._quote_model):
            return self._quote_model
        models = importlib.import_module("db.models")
        quote_cls = getattr(models, "Quote", None)
        if not self._is_sqlmodel(quote_cls):
            models = importlib.reload(models)
            quote_cls = getattr(models, "Quote", None)
        if not self._is_sqlmodel(quote_cls):
            raise RuntimeError("Quote model unavailable")
        self._quote_model = quote_cls
        return quote_cls

    @staticmethod
    def _is_sqlmodel(model):
        return model is not None and hasattr(model, "model_fields")

    @staticmethod
    def _parse_discount_fraction(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        raw = str(value).strip()
        if not raw:
            return None
        try:
            num = float(raw)
        except Exception:
            return None
        if num < 0:
            return None
        if num <= 1.0:
            fraction = num
        elif num <= 100.0:
            fraction = num / 100.0
        else:
            fraction = num / 10_000.0
        return max(0.0, min(0.95, fraction))

    def _base_discount_fraction(self, asset_u: str) -> float:
        keys = [
            f"PRICE_DISCOUNT_{asset_u}_BPS",
            f"PRICE_DISCOUNT_{asset_u}",
            f"PRICE_DISCOUNT_{asset_u}_PCT",
        ]
        for key in keys:
            fraction = self._parse_discount_fraction(os.getenv(key))
            if fraction is not None:
                return fraction
        fallback = self._parse_discount_fraction(os.getenv("PRICE_DISCOUNT_BPS"))
        if fallback is None:
            fallback = self._parse_discount_fraction(os.getenv("PRICE_DISCOUNT_DEFAULT_BPS"))
        if fallback is None:
            fallback = self._parse_discount_fraction(os.getenv("PRICE_DISCOUNT_DEFAULT"))
        if fallback is None:
            fallback = 0.05
        if asset_u == "WBTC" and fallback == 0.05:
            return 0.10
        return fallback

    def _asset_decimals(self, asset_u: str) -> int:
        return int(self._asset_decimals_map.get(asset_u, 18))

    @staticmethod
    def _clean_prices(data: Optional[Dict[str, float]]) -> Dict[str, float]:
        if not data:
            return {}
        out: Dict[str, float] = {}
        for key, value in data.items():
            try:
                val = float(value)
            except Exception:
                continue
            out[str(key).upper()] = val
        return out

    def _resolve_asset_usd(self, asset_u: str, market_prices: Dict[str, float]) -> float:
        prices = self._clean_prices(market_prices)
        if asset_u in {"ETH", "WETH"}:
            return float(prices.get("ETH") or prices.get("WETH") or 0.0)
        if asset_u == "USDC":
            return float(prices.get("USDC") or 1.0)
        if asset_u == "WBTC":
            return float(prices.get("WBTC") or 0.0)
        if asset_u == "USDT":
            return float(prices.get("USDT") or prices.get("USDC") or 1.0)
        return float(prices.get(asset_u) or 0.0)

    def _apply_discount(
        self,
        *,
        asset: str,
        markup_multiplier: float,
        draft,
        unit_price_with_markup: int,
    ) -> tuple[int, Dict[str, Any]]:
        asset_u = str(asset or "USDC").upper()
        base_fraction = self._base_discount_fraction(asset_u)
        decimals = self._asset_decimals(asset_u)
        prices = self._clean_prices(getattr(draft, "market_prices", None))
        asset_usd = self._resolve_asset_usd(asset_u, prices)
        market_ref = None
        try:
            market_ref = float(draft.usd_per_unit)
        except Exception:
            market_ref = None
        if not market_ref or market_ref <= 0:
            market_ref = prices.get("DIEM")
            if market_ref is not None:
                try:
                    market_ref = float(market_ref)
                except Exception:
                    market_ref = None
        unit_multiplier = max(0.0, float(markup_multiplier))
        asset_units_pre = unit_price_with_markup / (10 ** decimals)
        pre_usd = asset_units_pre * asset_usd if asset_usd and asset_usd > 0 else None
        if market_ref and market_ref > 0 and pre_usd and pre_usd > 0:
            effective_ratio = pre_usd / float(market_ref)
        else:
            effective_ratio = unit_multiplier if unit_multiplier > 0 else 1.0
        if effective_ratio <= 0:
            effective_ratio = 1.0
        desired_ratio = max(0.0, 1.0 - base_fraction)
        ratio_fraction = desired_ratio / effective_ratio
        if ratio_fraction >= 1.0:
            total_fraction = base_fraction
        else:
            total_fraction = max(base_fraction, min(0.95, 1.0 - ratio_fraction))
        discount_multiplier = max(0.0, 1.0 - total_fraction)
        discounted_unit_price = int(round(unit_price_with_markup * discount_multiplier))
        if discounted_unit_price <= 0:
            discounted_unit_price = 1
        post_units = discounted_unit_price / (10 ** decimals)
        post_usd = post_units * asset_usd if asset_usd and asset_usd > 0 else None
        total_bps = int(round(total_fraction * 10_000))
        base_bps = int(round(base_fraction * 10_000))
        relief_bps = max(0, total_bps - base_bps)
        details = {
            "totalBps": total_bps,
            "baseBps": base_bps,
            "markupReliefBps": relief_bps,
            "markupMultiplier": round(effective_ratio, 6),
            "targetDiscountRatio": round(desired_ratio, 6),
            "discountMultiplier": round(discount_multiplier, 6),
            "preDiscountUsdPerUnit": float(pre_usd) if pre_usd else None,
            "postDiscountUsdPerUnit": float(post_usd) if post_usd else None,
            "marketUsdPerUnit": float(market_ref) if market_ref else None,
            "assetUsd": float(asset_usd) if asset_usd else None,
            "decimals": decimals,
            "unitPriceBeforeDiscount": unit_price_with_markup,
            "unitPriceAfterDiscount": discounted_unit_price,
        }
        details = {k: v for k, v in details.items() if v is not None}
        return discounted_unit_price, details

    def get_quote(
        self,
        units: Optional[float],
        asset: str,
        budget_usd: Optional[float] = None,
    ) -> Dict[str, object]:
        start_time = time.perf_counter()
        outcome = "ok"
        prefetched_prices: Dict[str, float] = {}
        mdp_stats: Dict[str, Any] = {}
        discount_meta: Dict[str, Any] = {}
        try:
            if isinstance(self.engine, MarketPricingEngine):
                try:
                    from services.marketdata.provider import MarketDataProvider  # lazy import

                    mdp = MarketDataProvider()
                    symbols = ["DIEM", "ETH", "USDC", "WBTC", "VVV"]
                    prefetched = mdp.prices(symbols) or {}
                    prefetched_prices = dict(prefetched) if isinstance(prefetched, dict) else {}
                    mdp_stats = mdp.last_prices_stats()
                    if hasattr(self.engine, "set_prefetched_prices"):
                        self.engine.set_prefetched_prices(mdp, prefetched_prices)
                except Exception:
                    prefetched_prices = {}
                    mdp_stats = {}
            if units is not None and budget_usd is not None:
                raise ValueError("provide either units or budget, not both")
            if budget_usd is not None:
                budget_value = float(budget_usd)
                asset_u = asset.strip().upper()
                price_map: Dict[str, float] = {}
                if isinstance(self.engine, MarketPricingEngine):
                    price_map = dict(prefetched_prices)
                    if not price_map:
                        try:
                            _, price_map = self.engine._resolve_prices()  # type: ignore[attr-defined]
                        except Exception:  # noqa: BLE001
                            price_map = {}
                    if asset_u in {"ETH", "WETH"}:
                        eth_usd = float(price_map.get("ETH") or price_map.get("WETH") or 0.0)
                        if not (eth_usd and eth_usd > 0):
                            raise ValueError("ETH pricing unavailable for budget sizing")
                        budget_value = budget_value / float(eth_usd)
                    elif asset_u == "WBTC":
                        wbtc_usd = float(price_map.get("WBTC") or 0.0)
                        if not (wbtc_usd and wbtc_usd > 0):
                            raise ValueError("WBTC pricing unavailable for budget sizing")
                        budget_value = budget_value / float(wbtc_usd)
                draft = self.engine.price_from_budget(budget_value, asset)  # type: ignore[attr-defined]
            else:
                if units is None:
                    raise ValueError("units must be greater than zero")
                draft = self.engine.price(float(units), asset)
            util = self._utilization_ratio()
            alpha = float(os.getenv("PRICE_UTIL_ALPHA", "0.5") or 0.5)
            mult = 1.0 + max(0.0, min(1.0, util)) * alpha
            unit_price_markup = int(round(draft.unit_price * mult))
            unit_price, discount_meta = self._apply_discount(
                asset=draft.asset,
                markup_multiplier=mult,
                draft=draft,
                unit_price_with_markup=unit_price_markup,
            )
            total_price = int(round(unit_price * float(draft.units)))
            from datetime import datetime
            quote_model = self._get_quote_model()
            with next(get_session()) as s:  # type: ignore[call-arg]
                q = quote_model(
                    quote_id=draft.quote_id,
                    units=float(draft.units),
                    asset=draft.asset,
                    unit_price=unit_price,
                    total_price=total_price,
                    accepted_min=draft.accepted_min,
                    accepted_max=draft.accepted_max,
                    expires_at=datetime.utcfromtimestamp(draft.expires_at_epoch),
                    status="open",
                )
                s.add(q)
                s.commit()
            return {
                "quoteId": draft.quote_id,
                "units": float(draft.units),
                "asset": draft.asset,
                "unitPrice": unit_price,
                "totalPrice": total_price,
                "acceptedMin": draft.accepted_min,
                "acceptedMax": draft.accepted_max,
                "expiresAt": draft.expires_at_epoch,
                "discountBps": discount_meta.get("totalBps"),
                "discount": discount_meta,
                "unitPriceBeforeDiscount": unit_price_markup,
            }
        except Exception:
            outcome = "error"
            raise
        finally:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            try:
                payload: Dict[str, Any] = {
                    "outcome": outcome,
                    "latency_ms": round(duration_ms, 3),
                    "asset": asset.strip().upper(),
                    "engine": "market" if isinstance(self.engine, MarketPricingEngine) else "static",
                }
                payload["quoteLatencyMs"] = payload["latency_ms"]
                if units is not None:
                    payload["units"] = float(units)
                if budget_usd is not None:
                    payload["budgetUsd"] = float(budget_usd)
                if discount_meta:
                    total_bps = discount_meta.get("totalBps")
                    if total_bps is not None:
                        payload["discountBps"] = total_bps
                if prefetched_prices:
                    payload["prefetch"] = True
                if mdp_stats:
                    cache_hits = mdp_stats.get("cache_hits")
                    cache_misses = mdp_stats.get("cache_misses")
                    hit_rate = mdp_stats.get("cache_hit_rate")
                    dex_calls = mdp_stats.get("dex_calls")
                    if cache_hits is not None:
                        payload["cacheHits"] = cache_hits
                    if cache_misses is not None:
                        payload["cacheMisses"] = cache_misses
                    if hit_rate is not None:
                        payload["cacheHitRate"] = hit_rate
                    if dex_calls is not None:
                        payload["dexCalls"] = dex_calls
                    if "duration_seconds" in mdp_stats:
                        payload["pricesDurationSec"] = mdp_stats.get("duration_seconds")
                emit_event("pricing.quote", payload)
            except Exception:
                pass

    def _utilization_ratio(self) -> float:
        """Compute recent utilization ratio as used/capacity in lookback window.

        Env:
        - PRICE_UTIL_LOOKBACK_MIN (default 60)
        - CAPACITY_UNITS_PER_MIN (default 100)
        """
        lookback = int(os.getenv("PRICE_UTIL_LOOKBACK_MIN", "60") or 60)
        per_min = int(os.getenv("CAPACITY_UNITS_PER_MIN", "100") or 100)
        if per_min <= 0 or lookback <= 0:
            return 0.0
        try:
            from db.models import Counter
            from sqlmodel import select
            from datetime import datetime, timedelta
            now = datetime.utcnow()
            start = now - timedelta(minutes=lookback)
            used = 0
            with next(get_session()) as s:  # type: ignore[call-arg]
                rows = s.exec(
                    select(Counter).where(Counter.bucket_start >= start)
                ).all()
                used = sum(int(r.count or 0) for r in rows)
            cap = per_min * lookback
            return max(0.0, min(1.0, (used / cap) if cap > 0 else 0.0))
        except Exception:
            return 0.0
