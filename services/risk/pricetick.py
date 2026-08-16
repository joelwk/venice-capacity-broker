from __future__ import annotations

import os
from datetime import datetime, timezone

from libs.telemetry.logger import get_logger

logger = get_logger("risk.pricetick")

PRICE_TICK_WINDOW = 16


def vol_persist_enabled() -> bool:
    raw = os.getenv("RISK_VOL_PERSIST")
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}
    return bool(
        (os.getenv("SQL_DATABASE_URL") or "").strip()
        or (os.getenv("DATABASE_URL") or "").strip()
    )


def persist_price_tick(symbol: str, price_usd: float) -> None:
    if not vol_persist_enabled():
        return
    from sqlmodel import Session

    from db.models import PriceTick
    from db.session import get_engine

    engine = get_engine()
    with Session(engine) as session:  # type: ignore[call-arg]
        session.add(
            PriceTick(
                symbol=str(symbol or "DIEM"),
                price_usd=float(price_usd),
                ts=datetime.now(timezone.utc),
            )
        )
        session.commit()


def load_recent_prices(symbol: str = "DIEM", limit: int = PRICE_TICK_WINDOW) -> list[float]:
    if not vol_persist_enabled():
        return []
    from sqlmodel import desc, select

    from db.models import PriceTick
    from db.session import get_session

    cap = max(1, int(limit))
    with next(get_session()) as session:  # type: ignore[call-arg]
        rows = session.exec(
            select(PriceTick)
            .where(PriceTick.symbol == str(symbol or "DIEM"))
            .order_by(desc(PriceTick.ts))
            .limit(cap)
        ).all()
    prices = [float(row.price_usd) for row in reversed(list(rows))]
    return prices
