from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from agents.quorum.core import QuorumVote


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_str_set(name: str) -> set[str]:
    raw = os.getenv(name)
    if raw is None:
        return set()
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


@dataclass
class QuorumContext:
    """Shared signal payload pushed into quorum models each cycle."""

    price: float | None = None
    mint_rate: float | None = None
    premium: float | None = None
    suggested_units: int | None = None
    utilization_ratio: float | None = None
    vol_bps: float | None = None
    inventory_usd: float | None = None
    stake: dict[str, Any] | None = None
    rationale: dict[str, Any] | None = None
    reflex: dict[str, Any] | None = None
    price_guard: dict[str, Any] | None = None
    capacity_usage: dict[str, Any] | None = None
    execution_preview: dict[str, Any] | None = (
        None  # ExecutionResult.as_dict() from preview_trade
    )
    dry_run: bool = True
    live_mode: bool = False
    simulate_decision: bool = False


@dataclass
class BaseModel:
    """Base quorum model with shared helpers."""

    name: str
    weight: float = 1.0
    _ctx: QuorumContext | None = field(default=None, init=False, repr=False)

    def update(self, context: QuorumContext) -> None:
        self._ctx = context

    # Expose for convenience in subclasses
    @property
    def context(self) -> QuorumContext:
        if self._ctx is None:
            raise RuntimeError(f"{self.name} model has no context; call update() first")
        return self._ctx

    def vote(self) -> QuorumVote:  # pragma: no cover - subclasses must implement
        raise NotImplementedError

    # Helpers ---------------------------------------------------------
    def _clamp_conf(self, value: float, *, default: float = 0.5) -> float:
        try:
            return max(0.0, min(1.0, float(value)))
        except Exception:
            return default


@dataclass
class YieldModel(BaseModel):
    """Prefers action when staking is healthy and heartbeats are passing."""

    min_conf_active: float = 0.55
    inactive_reasons_hold: set[str] = field(
        default_factory=lambda: {"inactive_staker", "heartbeat_failed"}
    )

    def vote(self) -> QuorumVote:
        ctx = self.context
        stake = ctx.stake if isinstance(ctx.stake, dict) else {}
        status = stake.get("status")
        snapshot = stake.get("snapshot") if isinstance(stake, dict) else None
        heartbeat = stake.get("heartbeat") if isinstance(stake, dict) else None
        if status not in (None, "ok"):
            return QuorumVote(
                approve=False, confidence=0.9, reason=f"stake_status:{status}"
            )

        active = (
            bool(snapshot.get("active_staker")) if isinstance(snapshot, dict) else False
        )
        heartbeat_err = (
            (heartbeat or {}).get("error") if isinstance(heartbeat, dict) else None
        )
        if not active:
            return QuorumVote(approve=False, confidence=0.8, reason="inactive-staker")
        if heartbeat_err:
            return QuorumVote(
                approve=False, confidence=0.6, reason=f"heartbeat:{heartbeat_err}"
            )
        rewards = (
            snapshot.get("unclaimed_rewards") if isinstance(snapshot, dict) else None
        )
        has_rewards = (
            bool(rewards) and float(rewards) > 0.0 if rewards is not None else False
        )
        reason = "stake_healthy_with_rewards" if has_rewards else "stake_healthy"
        confidence = self._clamp_conf(
            self.min_conf_active + (0.1 if has_rewards else 0.0)
        )
        return QuorumVote(approve=True, confidence=confidence, reason=reason)


@dataclass
class ArbModel(BaseModel):
    """Gates on DIEM premium and suggested trade sizing."""

    premium_threshold: float = 1.03
    min_units: int = 1

    def __post_init__(self) -> None:
        thresh = _env_float("QUORUM_ARB_MIN_PREMIUM", None)
        if thresh is not None:
            self.premium_threshold = thresh
        min_units = _env_float("QUORUM_ARB_MIN_UNITS", None)
        if min_units is not None:
            try:
                self.min_units = int(min_units)
            except Exception:
                pass

    def vote(self) -> QuorumVote:
        ctx = self.context
        if not ctx.simulate_decision:
            return QuorumVote(
                approve=False, confidence=0.4, reason="no-simulated-decision"
            )

        premium = ctx.premium
        suggested = ctx.suggested_units or 0
        if premium is None or premium <= 0:
            return QuorumVote(approve=False, confidence=0.5, reason="missing-premium")
        if suggested < self.min_units:
            return QuorumVote(approve=False, confidence=0.6, reason="insufficient-size")

        if premium >= self.premium_threshold:
            headroom = max(0.0, premium - self.premium_threshold)
            conf = self._clamp_conf(0.6 + headroom * 4.0, default=0.6)
            return QuorumVote(
                approve=True, confidence=conf, reason=f"premium:{premium:.3f}"
            )
        return QuorumVote(
            approve=False, confidence=0.6, reason=f"premium:{premium:.3f}"
        )


@dataclass
class RiskModel(BaseModel):
    """Vetoes actions when volatility or guardrails flag hazards."""

    max_vol_bps: float | None = None
    halt_reasons_block: set[str] = field(
        default_factory=lambda: {"price_guard", "reflex_halt", "stake_error"}
    )

    def __post_init__(self) -> None:
        if self.max_vol_bps is None:
            self.max_vol_bps = _env_float("QUORUM_RISK_MAX_VOL_BPS", None)
        block_env = _env_str_set("QUORUM_RISK_BLOCK_REASONS")
        if block_env:
            self.halt_reasons_block |= block_env

    def vote(self) -> QuorumVote:
        ctx = self.context
        reflex = ctx.reflex if isinstance(ctx.reflex, dict) else {}
        if reflex.get("halt"):
            return QuorumVote(approve=False, confidence=1.0, reason="reflex-halt")

        price_guard = ctx.price_guard if isinstance(ctx.price_guard, dict) else {}
        guard_status = price_guard.get("status")
        if guard_status in self.halt_reasons_block:
            return QuorumVote(
                approve=False, confidence=0.9, reason=f"price-guard:{guard_status}"
            )

        vol = ctx.vol_bps
        if vol is not None and self.max_vol_bps is not None and vol > self.max_vol_bps:
            return QuorumVote(
                approve=False, confidence=0.85, reason=f"vol-bps:{vol:.1f}"
            )

        warnings = reflex.get("warnings")
        if isinstance(warnings, list) and warnings:
            return QuorumVote(
                approve=False, confidence=0.7, reason=f"reflex-warning:{warnings[0]}"
            )

        return QuorumVote(approve=True, confidence=0.55, reason="risk-clear")


@dataclass
class DemandModel(BaseModel):
    """Promotes execution when utilisation is hot, holds otherwise."""

    surge_threshold: float = 0.82
    relax_threshold: float = 0.45

    def __post_init__(self) -> None:
        surge = _env_float("QUORUM_DEMAND_SURGE_THRESHOLD", None)
        relax = _env_float("QUORUM_DEMAND_RELAX_THRESHOLD", None)
        if surge is not None:
            self.surge_threshold = surge
        if relax is not None:
            self.relax_threshold = relax

    def vote(self) -> QuorumVote:
        ctx = self.context
        util = ctx.utilization_ratio
        if util is None:
            return QuorumVote(approve=False, confidence=0.4, reason="util-unknown")

        util = max(0.0, min(1.0, float(util)))
        if util >= self.surge_threshold:
            intensity = (util - self.surge_threshold) / max(
                1e-6, 1.0 - self.surge_threshold
            )
            conf = self._clamp_conf(0.6 + intensity * 0.4, default=0.6)
            return QuorumVote(
                approve=True, confidence=conf, reason=f"util-hot:{util:.3f}"
            )
        if util <= self.relax_threshold:
            slack = (self.relax_threshold - util) / max(1e-6, self.relax_threshold)
            conf = self._clamp_conf(0.6 + slack * 0.3, default=0.6)
            return QuorumVote(
                approve=False, confidence=conf, reason=f"util-slack:{util:.3f}"
            )
        return QuorumVote(
            approve=False, confidence=0.45, reason=f"util-neutral:{util:.3f}"
        )


@dataclass
class TreasuryModel(BaseModel):
    """Lightweight treasury heuristic: ensure DIEM buffer stays above target."""

    buffer_days: float = 1.5
    min_conf: float = 0.4

    def __post_init__(self) -> None:
        buf = _env_float("QUORUM_TREASURY_BUFFER_DAYS", None)
        if buf is not None and buf > 0:
            self.buffer_days = buf

    def vote(self) -> QuorumVote:
        ctx = self.context
        usage = ctx.capacity_usage if isinstance(ctx.capacity_usage, dict) else {}
        avg_diem = usage.get("dailyAverageDiem") or usage.get("daily_average_diem")
        if avg_diem is None:
            return QuorumVote(
                approve=False, confidence=self.min_conf, reason="usage-unknown"
            )
        try:
            avg = float(avg_diem)
        except (TypeError, ValueError):
            return QuorumVote(
                approve=False, confidence=self.min_conf, reason="usage-invalid"
            )

        target = avg * max(0.0, float(self.buffer_days))
        inventory = ctx.inventory_usd or 0.0
        # Treat USD inventory as 1:1 DIEM coverage in early versions
        delta = target - inventory
        if delta <= 0:
            conf = self._clamp_conf(
                self.min_conf + min(0.2, abs(delta) / max(1.0, target)),
                default=self.min_conf,
            )
            return QuorumVote(approve=False, confidence=conf, reason="buffer-satisfied")
        conf = self._clamp_conf(0.5 + min(0.3, delta / max(1.0, target)), default=0.5)
        return QuorumVote(approve=True, confidence=conf, reason="buffer-deficit")


__all__ = [
    "ArbModel",
    "BaseModel",
    "DemandModel",
    "QuorumContext",
    "RiskModel",
    "TreasuryModel",
    "YieldModel",
]
