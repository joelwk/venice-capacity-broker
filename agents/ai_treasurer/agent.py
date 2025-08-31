from __future__ import annotations

from dataclasses import dataclass

from libs.telemetry.logger import get_logger


logger = get_logger("agent.ai_treasurer")


@dataclass
class AITreasurer:
    buffer_target_days: float = 1.5  # keep 150% of avg daily need

    def rebalance(self, avg_daily_diem: float, current_diem: float) -> float:
        target = avg_daily_diem * self.buffer_target_days
        delta = target - current_diem
        logger.info(f"Rebalance delta={delta:.2f} (target={target:.2f})")
        return delta

