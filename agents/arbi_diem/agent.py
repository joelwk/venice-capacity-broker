from __future__ import annotations

from dataclasses import dataclass

from libs.telemetry.logger import get_logger
from libs.pricing.diem import fair_value_per_diem
from services.diem.client import DIEMService


logger = get_logger("agent.arbi_diem")


@dataclass
class ArbiDiem:
    diem: DIEMService
    discount_rate_apy: float = 0.2

    def evaluate_and_maybe_mint(self, market_price: float, mint_rate: float = 1.0) -> bool:
        fair = fair_value_per_diem(self.discount_rate_apy) * mint_rate / 365.0
        logger.info(f"Market px={market_price:.4f}, fair/day={fair:.4f}")
        if market_price > fair * 1.05:  # 5% threshold
            logger.info("Signal: Mint and sell DIEM (stub)")
            self.diem.mint(1_000)
            self.diem.trade("sell", 1_000)
            return True
        logger.info("No-op: market not favorable")
        return False

