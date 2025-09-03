from __future__ import annotations

import time
from typing import Dict

from libs.pricing.engine import StaticPricingEngine
from db.session import get_session, create_db_and_tables
from db.models import Quote


class PricingService:
    def __init__(self) -> None:
        self.engine = StaticPricingEngine()
        try:
            create_db_and_tables()
        except Exception:
            pass

    def get_quote(self, units: int, asset: str) -> Dict[str, object]:
        draft = self.engine.price(units, asset)
        from datetime import datetime
        with next(get_session()) as s:  # type: ignore[call-arg]
            q = Quote(
                quote_id=draft.quote_id,
                units=draft.units,
                asset=draft.asset,
                unit_price=draft.unit_price,
                total_price=draft.total_price,
                accepted_min=draft.accepted_min,
                accepted_max=draft.accepted_max,
                expires_at=datetime.utcfromtimestamp(draft.expires_at_epoch),
                status="open",
            )
            s.add(q)
            s.commit()
        return {
            "quoteId": draft.quote_id,
            "units": draft.units,
            "asset": draft.asset,
            "unitPrice": draft.unit_price,
            "totalPrice": draft.total_price,
            "acceptedMin": draft.accepted_min,
            "acceptedMax": draft.accepted_max,
            "expiresAt": draft.expires_at_epoch,
        }
