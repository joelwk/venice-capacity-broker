from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, List

from libs.telemetry.logger import get_logger


logger = get_logger("agent.quorum")


@dataclass
class QuorumMember:
    name: str
    vote: Callable[[], bool]
    weight: float = 1.0


@dataclass
class Quorum:
    members: List[QuorumMember]
    threshold: float = 0.5  # simple majority by weight

    def decide(self) -> bool:
        total = sum(m.weight for m in self.members)
        yes = sum(m.weight for m in self.members if m.vote())
        ratio = yes / total if total else 0
        logger.info(f"Quorum vote ratio={ratio:.2f} (thr={self.threshold:.2f})")
        return ratio >= self.threshold

