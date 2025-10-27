from __future__ import annotations

import time
from typing import Dict, Any, Optional, Type
from collections.abc import Iterable

import importlib
import os
from libs.pricing.engine import StaticPricingEngine, MarketPricingEngine
from db.session import create_db_and_tables, get_session
from libs.telemetry.events import emit as emit_event
try:
    from libs.telemetry.metrics import inc as _metrics_inc  # type: ignore
except Exception:
    _metrics_inc = None  # type: ignore


def parse_discount_fraction(value: Optional[str]) -> Optional[float]:
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


def resolve_discount_fraction(asset_u: str) -> tuple[float, str]:
    asset_norm = str(asset_u or "USDC").strip().upper() or "USDC"
    keys = [
        f"PRICE_DISCOUNT_{asset_norm}_BPS",
        f"PRICE_DISCOUNT_{asset_norm}",
        f"PRICE_DISCOUNT_{asset_norm}_PCT",
    ]
    for key in keys:
        fraction = parse_discount_fraction(os.getenv(key))
        if fraction is not None:
            return fraction, key
    fallback_keys = [
        "PRICE_DISCOUNT_BPS",
        "PRICE_DISCOUNT_DEFAULT_BPS",
        "PRICE_DISCOUNT_DEFAULT",
    ]
    for key in fallback_keys:
        fraction = parse_discount_fraction(os.getenv(key))
        if fraction is not None:
            if asset_norm == "WBTC" and abs(fraction - 0.05) < 1e-9:
                return 0.10, f"{key}+wbtc"
            return fraction, key
    if asset_norm == "WBTC":
        return 0.10, "default_wbtc"
    return 0.05, "default"


def _latency_bucket(seconds: float) -> str:
    try:
        s = float(seconds)
    except Exception:
        s = 0.0
    if s < 0.05:
        return "lt_50ms"
    if s < 0.1:
        return "lt_100ms"
    if s < 0.2:
        return "lt_200ms"
    if s < 0.5:
        return "lt_500ms"
    if s < 1.0:
        return "lt_1s"
    if s < 2.0:
        return "lt_2s"
    return "ge_2s"


def configured_discount_map(assets: Optional[Iterable[str]] = None) -> Dict[str, Dict[str, Any]]:
    seen = []
    if assets:
        for asset in assets:
            candidate = str(asset or "").strip().upper()
            if candidate and candidate not in seen:
                seen.append(candidate)
    else:
        seen = ["USDC", "ETH", "WETH", "USDT", "WBTC"]
    result: Dict[str, Dict[str, Any]] = {}
    for asset in seen:
        fraction, source = resolve_discount_fraction(asset)
        fraction = max(0.0, min(0.95, float(fraction)))
        base_bps = int(round(fraction * 10_000))
        result[asset] = {
            "baseFraction": fraction,
            "baseBps": base_bps,
            "basePercent": round(fraction * 100.0, 6),
            "source": source,
        }
    return result


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
        return parse_discount_fraction(value)

    def _base_discount_fraction(self, asset_u: str) -> float:
        fraction, _ = resolve_discount_fraction(asset_u)
        return fraction

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
        price_health: Dict[str, Any] | None = None
        price_guard: Dict[str, Any] | None = None
        market_data_start: float = 0.0
        engine_start: float = 0.0
        persist_start: float = 0.0
        try:
            if isinstance(self.engine, MarketPricingEngine):
                try:
                    from services.marketdata.provider import MarketDataProvider  # lazy import

                    market_data_start = time.perf_counter()
                    mdp = MarketDataProvider()
                    symbols = ["DIEM", "ETH", "USDC", "WBTC", "VVV"]
                    prefetched = mdp.prices(symbols) or {}
                    prefetched_prices = dict(prefetched) if isinstance(prefetched, dict) else {}
                    mdp_stats = mdp.last_prices_stats()
                    if hasattr(self.engine, "set_prefetched_prices"):
                        self.engine.set_prefetched_prices(mdp, prefetched_prices)
                    price_health_candidate = None
                    if hasattr(mdp, "price_health"):
                        try:
                            price_health_candidate = mdp.price_health("DIEM", max_age=180.0)
                        except Exception:
                            price_health_candidate = None
                    if isinstance(price_health_candidate, dict) and price_health_candidate:
                        price_health = dict(price_health_candidate)
                        source_label = str(price_health.get("source") or "")
                        clamped = bool(price_health.get("clamped"))
                        valid_flag = price_health.get("valid")
                        source_ok = source_label.startswith("aggregator")
                        if clamped or valid_flag is False or not source_ok:
                            price_guard = {
                                "status": "unhealthy",
                                "reason": "price_guard",
                                "details": dict(price_health),
                            }
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
                    def _resolve_positive_price(symbols: list[str]) -> float:
                        for sym in symbols:
                            try:
                                value = float(price_map.get(sym) or 0.0)
                            except Exception:
                                value = 0.0
                            if value > 0:
                                return value
                        try:
                            _, fallback_map = self.engine._resolve_prices()  # type: ignore[attr-defined]
                        except Exception:  # noqa: BLE001
                            fallback_map = None
                        if isinstance(fallback_map, dict):
                            for key, value in fallback_map.items():
                                if key not in price_map or price_map.get(key) in (None, 0, 0.0):
                                    price_map[key] = value
                        for sym in symbols:
                            try:
                                value = float(price_map.get(sym) or 0.0)
                            except Exception:
                                value = 0.0
                            if value > 0:
                                return value
                        return 0.0
                    if asset_u in {"ETH", "WETH"}:
                        eth_usd = _resolve_positive_price(["ETH", "WETH"])
                        if not (eth_usd and eth_usd > 0):
                            raise ValueError("ETH pricing unavailable for budget sizing")
                        budget_value = budget_value / float(eth_usd)
                    elif asset_u == "WBTC":
                        wbtc_usd = _resolve_positive_price(["WBTC"])
                        if not (wbtc_usd and wbtc_usd > 0):
                            raise ValueError("WBTC pricing unavailable for budget sizing")
                        budget_value = budget_value / float(wbtc_usd)
                draft = self.engine.price_from_budget(budget_value, asset)  # type: ignore[attr-defined]
            else:
                if units is None:
                    raise ValueError("units must be greater than zero")
                draft = self.engine.price(float(units), asset)
            engine_start = time.perf_counter()
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
            engine_end = time.perf_counter()
            
            # Build timing metadata
            market_data_duration = 0.0
            if market_data_start > 0:
                market_data_duration = (time.perf_counter() - market_data_start) * 1000.0
            
            engine_duration = 0.0
            if engine_start > 0:
                engine_duration = (engine_end - engine_start) * 1000.0
            
            total_duration = (time.perf_counter() - start_time) * 1000.0
            
            meta: Dict[str, Any] = {
                "totalMs": round(total_duration, 3),
                "engineMs": round(engine_duration, 3),
            }
            if market_data_duration > 0:
                meta["marketDataMs"] = round(market_data_duration, 3)
            
            # Add cache stats if available
            if mdp_stats:
                cache_hits = mdp_stats.get("cache_hits")
                cache_misses = mdp_stats.get("cache_misses")
                cache_hit_rate = mdp_stats.get("cache_hit_rate")
                if cache_hits is not None:
                    meta["cacheHits"] = int(cache_hits)
                if cache_misses is not None:
                    meta["cacheMisses"] = int(cache_misses)
                if cache_hit_rate is not None:
                    meta["cacheHitRate"] = round(float(cache_hit_rate), 3)
            
            response: Dict[str, object] = {
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
                "meta": meta,
            }
            if price_health is not None:
                response["priceHealth"] = price_health
            if price_guard is not None:
                response["priceGuard"] = price_guard
            # emit latency metrics (counters with buckets)
            if _metrics_inc is not None:
                try:
                    _metrics_inc(
                        "quote_latency_bucket_total",
                        labels={"bucket": _latency_bucket(total_duration / 1000.0)},
                    )
                    if market_data_duration > 0:
                        _metrics_inc(
                            "quote_marketdata_latency_bucket_total",
                            labels={"bucket": _latency_bucket(market_data_duration / 1000.0)},
                        )
                    _metrics_inc(
                        "quote_engine_latency_bucket_total",
                        labels={"bucket": _latency_bucket(engine_duration / 1000.0)},
                    )
                except Exception:
                    pass
            return response

    def persist_quote(self, payload: Dict[str, Any]) -> None:
        self._persist_quote_from_payload(payload)

    def _persist_quote_from_payload(self, payload: Dict[str, Any]) -> None:
        from datetime import datetime, timezone

        quote_id = str(payload.get("quoteId")) if payload.get("quoteId") else None
        if not quote_id:
            raise ValueError("quote payload missing quoteId")

        units_val = float(payload.get("units") or 0.0)
        if units_val <= 0:
            raise ValueError("quote payload units invalid")

        asset = str(payload.get("asset") or "").strip().upper()
        if not asset:
            raise ValueError("quote payload asset missing")

        unit_price = int(payload.get("unitPrice") or payload.get("unit_price") or 0)
        total_price = int(payload.get("totalPrice") or payload.get("total_price") or 0)
        if unit_price <= 0 or total_price <= 0:
            raise ValueError("quote payload pricing invalid")

        accepted_min = payload.get("acceptedMin")
        accepted_max = payload.get("acceptedMax")

        expires_at_raw = payload.get("expiresAt") or payload.get("expires_at")
        if not expires_at_raw:
            raise ValueError("quote payload expiresAt missing")
        expires_at = datetime.fromtimestamp(int(expires_at_raw), tz=timezone.utc)

        quote_model = self._get_quote_model()
        with next(get_session()) as session:  # type: ignore[call-arg]
            try:
                from sqlmodel import select  # type: ignore
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("sqlmodel unavailable for quote persistence") from exc

            stmt = select(quote_model).where(quote_model.quote_id == quote_id)
            existing = session.exec(stmt).first()
            if existing is None:
                q = quote_model(
                    quote_id=quote_id,
                    units=units_val,
                    asset=asset,
                    unit_price=unit_price,
                    total_price=total_price,
                    accepted_min=accepted_min,
                    accepted_max=accepted_max,
                    expires_at=expires_at,
                    status="open",
                )
                session.add(q)
            else:
                existing.units = units_val
                existing.asset = asset
                existing.unit_price = unit_price
                existing.total_price = total_price
                existing.accepted_min = accepted_min
                existing.accepted_max = accepted_max
                existing.expires_at = expires_at
            session.commit()

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
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
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
