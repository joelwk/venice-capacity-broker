from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Any

from libs.telemetry.logger import get_logger
from libs.telemetry.tracing import annotate_span


logger = get_logger("workflow.orchestrator")


@dataclass
class Orchestrator:
    market: Any
    arbi: Any

    def run_once(self, mint_rate: float = 1.0, dry_run: bool = True) -> Dict[str, Any]:
        prices = self.market.prices(["DIEM"]) or {}
        px = float(prices.get("DIEM", 1.0))
        decision = self.arbi.evaluate_and_maybe_mint(px, mint_rate=mint_rate) if not dry_run else (px > 0)
        record = {"agent": "arbi_diem", "action": "mint_sell" if decision else "hold", "price": px, "dry_run": dry_run}
        try:
            annotate_span({"orchestrator": record}, name="vvv.orchestrator.decision")
        except Exception:
            pass
        # Persist decision if SQL is available
        try:
            from sqlmodel import Session
            from db.session import get_engine, create_db_and_tables
            from db.models import Decision

            create_db_and_tables()
            eng = get_engine()
            with Session(eng) as s:  # type: ignore[call-arg]
                s.add(Decision(agent=str(record["agent"]), action=str(record["action"]), details=json.dumps(record)))
                s.commit()
        except Exception:
            pass
        logger.info(f"orchestrator decision: {record}")
        return record

