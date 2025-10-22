from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Dict, Optional

from agents.quorum.core import Quorum, QuorumMember
from agents.quorum.models import (
    ArbModel,
    BaseModel,
    DemandModel,
    QuorumContext,
    RiskModel,
    TreasuryModel,
    YieldModel,
)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class QuorumCoordinator:
    """Wraps quorum voting models with shared context updates."""

    models: Dict[str, BaseModel]
    threshold: float = 0.55
    _quorum: Quorum = field(init=False, repr=False)
    _last_context: Optional[QuorumContext] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        members = [
            QuorumMember(name=name, vote=model.vote, weight=model.weight)
            for name, model in self.models.items()
        ]
        self._quorum = Quorum(members=members, threshold=self.threshold)

    def update(self, context: QuorumContext) -> None:
        self._last_context = context
        for model in self.models.values():
            model.update(context)

    def decide(self) -> bool:
        return self._quorum.decide()

    def decide_with_details(self):
        return self._quorum.decide_with_details()

    def last_info(self):
        return self._quorum.last_info()

    def models_snapshot(self) -> Dict[str, dict]:
        """Return last context snapshot for observability."""
        snapshot: Dict[str, dict] = {}
        for name, model in self.models.items():
            try:
                ctx = model.context  # type: ignore[attr-defined]
            except Exception:
                continue
            snapshot[name] = {
                "weight": model.weight,
                "context": ctx.__dict__,
            }
        return snapshot

    @property
    def last_context(self) -> Optional[QuorumContext]:
        return self._last_context


def build_default_models(include_treasury: bool = True) -> Dict[str, BaseModel]:
    weights = {
        "yield": _env_float("QUORUM_WEIGHT_YIELD", 1.0),
        "arbitrage": _env_float("QUORUM_WEIGHT_ARB", 1.2),
        "risk": _env_float("QUORUM_WEIGHT_RISK", 2.0),
        "demand": _env_float("QUORUM_WEIGHT_DEMAND", 0.8),
        "treasury": _env_float("QUORUM_WEIGHT_TREASURY", 0.6),
    }
    models: "OrderedDict[str, BaseModel]" = OrderedDict()
    models["yield"] = YieldModel(name="yield", weight=weights["yield"])
    models["arbitrage"] = ArbModel(name="arbitrage", weight=weights["arbitrage"])
    models["risk"] = RiskModel(name="risk", weight=weights["risk"])
    models["demand"] = DemandModel(name="demand", weight=weights["demand"])
    if include_treasury:
        models["treasury"] = TreasuryModel(name="treasury", weight=weights["treasury"])
    return models


def build_default_coordinator(
    *,
    threshold: float | None = None,
    include_treasury: bool | None = None,
) -> QuorumCoordinator:
    thr = threshold if threshold is not None else _env_float("QUORUM_THRESHOLD", 0.55)
    include_flag = include_treasury if include_treasury is not None else _env_bool("QUORUM_INCLUDE_TREASURY", False)
    models = build_default_models(include_treasury=include_flag)
    return QuorumCoordinator(models=models, threshold=thr)


__all__ = [
    "QuorumCoordinator",
    "QuorumContext",
    "BaseModel",
    "build_default_coordinator",
    "build_default_models",
]
