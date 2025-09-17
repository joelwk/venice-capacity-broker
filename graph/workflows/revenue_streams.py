from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from libs.telemetry.logger import get_logger
from libs.pricing.diem import fair_value_per_diem
from services.marketdata.provider import MarketDataProvider
from agents.arbi_diem.agent import ArbiDiem


logger = get_logger("workflow.revenue")


@dataclass
class DiemMintSellWorkflow:
    market: MarketDataProvider
    arbi: ArbiDiem
    discount_rate_apy: float = 0.2

    def run_once(self, mint_rate: float = 1.0, dry_run: bool = True) -> bool:
        px = self.market.prices(["DIEM"]).get("DIEM", 1.0)
        effective_mint_rate = float(mint_rate)
        try:
            info = self.market.diem_mint_rate(ttl_s=60)
            if isinstance(info, dict):
                candidate = info.get("tokens_per_diem")
                if candidate not in (None, 0):
                    effective_mint_rate = float(candidate)  # type: ignore[arg-type]
        except Exception:
            pass
        fair_day = fair_value_per_diem(self.discount_rate_apy) * effective_mint_rate / 365.0
        decision = "mint_sell" if px > fair_day * 1.05 else "hold"
        logger.info(
            f"Decision={decision} (market={px:.4f}, fair/day={fair_day:.4f}, mint_rate={effective_mint_rate:.4f})"
        )
        if decision == "mint_sell" and not dry_run:
            return self.arbi.evaluate_and_maybe_mint(px, effective_mint_rate)
        return decision == "mint_sell"

