from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from libs.telemetry.logger import get_logger

logger = get_logger("agent.quorum")

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # pragma: no cover - optional metrics backend

    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return


@dataclass
class QuorumMember:
    name: str
    vote: Callable[[], bool]
    weight: float = 1.0


@dataclass
class QuorumVote:
    approve: bool
    weight: float | None = None
    confidence: float = 1.0
    reason: str | None = None


@dataclass
class Quorum:
    members: list[QuorumMember]
    threshold: float = 0.5  # simple majority by weight
    _last_info: dict | None = field(default=None, init=False, repr=False)
    _last_decision: bool | None = field(default=None, init=False, repr=False)

    def decide(self) -> bool:
        decision, info = self.decide_with_details()
        self._last_decision = decision
        self._last_info = info
        return decision

    def decide_with_details(self) -> tuple[bool, dict]:
        breakdown = []
        approved = 0.0
        total = 0.0

        for member in self.members:
            raw_vote = member.vote()
            normalized = self._normalize_vote(raw_vote, member.weight)
            weight = (
                normalized.weight if normalized.weight is not None else member.weight
            )
            confidence = self._clamp_confidence(normalized.confidence)
            effective_weight = float(weight) * confidence
            total += effective_weight
            if normalized.approve:
                approved += effective_weight
            try:
                _metrics_inc(
                    "quorum_vote_events_total",
                    labels={
                        "member": str(member.name),
                        "approve": "true" if normalized.approve else "false",
                    },
                )
            except Exception:
                pass
            breakdown.append(
                {
                    "name": member.name,
                    "approve": bool(normalized.approve),
                    "weight": float(weight),
                    "confidence": confidence,
                    "effectiveWeight": effective_weight,
                    "reason": normalized.reason,
                }
            )

        ratio = approved / total if total else 0.0
        decision = ratio >= float(self.threshold)
        info = {
            "ratio": ratio,
            "approvedWeight": approved,
            "totalWeight": total,
            "threshold": float(self.threshold),
            "breakdown": breakdown,
            "confidence": max(
                (entry["confidence"] for entry in breakdown), default=0.0
            ),
        }
        logger.info(
            "Quorum vote ratio=%.2f thr=%.2f",
            ratio,
            float(self.threshold),
        )
        try:
            _metrics_inc(
                "quorum_decisions_total",
                labels={"decision": "approved" if decision else "blocked"},
            )
        except Exception:
            pass
        self._last_info = info
        self._last_decision = decision
        return decision, info

    def last_info(self) -> dict | None:
        return self._last_info

    def _normalize_vote(
        self, raw: bool | QuorumVote | dict, default_weight: float
    ) -> QuorumVote:
        if isinstance(raw, QuorumVote):
            return raw
        if isinstance(raw, dict):
            return QuorumVote(
                approve=bool(raw.get("approve", raw.get("vote", False))),
                weight=raw.get("weight", default_weight),
                confidence=float(raw.get("confidence", 1.0)),
                reason=raw.get("reason"),
            )
        return QuorumVote(approve=bool(raw), weight=default_weight, confidence=1.0)

    def _clamp_confidence(self, value: float) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return 1.0
