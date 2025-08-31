from __future__ import annotations

from dataclasses import dataclass

from libs.telemetry.logger import get_logger
from services.staking.client import StakingService


logger = get_logger("agent.stake_master")


@dataclass
class StakeMaster:
    staking: StakingService

    def run_once(self, live: bool = False) -> None:
        """Single heartbeat.

        Reads status; if live is True and rewards>0, attempts a claim.
        """
        status = self.staking.status()
        logger.info(f"Status: {status}")
        if live:
            if int(status.get("rewards", 0)) > 0:
                res = self.staking.claim()
                logger.info(f"Claim result: {res}")
            else:
                logger.info("No rewards to claim (live mode)")
        else:
            logger.info("Dry-run: would claim if rewards > 0")
