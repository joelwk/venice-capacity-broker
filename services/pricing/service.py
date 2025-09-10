from __future__ import annotations

import time
from typing import Dict

import os
from libs.pricing.engine import StaticPricingEngine, MarketPricingEngine
from db.session import get_session, create_db_and_tables
from db.models import Quote


class PricingService:
    def __init__(self) -> None:
        # Choose engine: market or static
        if (os.getenv("PRICE_ENGINE") or "static").strip().lower() == "market":
            self.engine = MarketPricingEngine()
        else:
            self.engine = StaticPricingEngine()
        try:
            create_db_and_tables()
        except Exception:
            pass

    def get_quote(self, units: float, asset: str) -> Dict[str, object]:
        draft = self.engine.price(float(units), asset)
        # Utilization-aware adjustment
        util = self._utilization_ratio()
        alpha = float(os.getenv("PRICE_UTIL_ALPHA", "0.5") or 0.5)
        mult = 1.0 + max(0.0, min(1.0, util)) * alpha
        unit_price = int(round(draft.unit_price * mult))
        total_price = int(round(unit_price * float(draft.units)))
        from datetime import datetime
        with next(get_session()) as s:  # type: ignore[call-arg]
            q = Quote(
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
