from __future__ import annotations

import time
from typing import Dict, Any, Optional, Type

import importlib
import os
from libs.pricing.engine import StaticPricingEngine, MarketPricingEngine
from db.session import get_session, create_db_and_tables


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

    def get_quote(
        self,
        units: Optional[float],
        asset: str,
        budget_usd: Optional[float] = None,
    ) -> Dict[str, object]:
        if units is not None and budget_usd is not None:
            raise ValueError("provide either units or budget, not both")
        if budget_usd is not None:
            budget_value = float(budget_usd)
            asset_u = asset.strip().upper()
            if isinstance(self.engine, MarketPricingEngine):
                try:
                    _, _, eth_usd = self.engine._resolve_prices()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    eth_usd = 0.0
                if asset_u == "ETH":
                    if not (eth_usd and eth_usd > 0):
                        raise ValueError("ETH pricing unavailable for budget sizing")
                    budget_value = budget_value / float(eth_usd)
            draft = self.engine.price_from_budget(budget_value, asset)  # type: ignore[attr-defined]
        else:
            if units is None:
                raise ValueError("units must be greater than zero")
            draft = self.engine.price(float(units), asset)
        # Utilization-aware adjustment
        util = self._utilization_ratio()
        alpha = float(os.getenv("PRICE_UTIL_ALPHA", "0.5") or 0.5)
        mult = 1.0 + max(0.0, min(1.0, util)) * alpha
        unit_price = int(round(draft.unit_price * mult))
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
        }

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
