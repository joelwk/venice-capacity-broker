from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence


class ReflectionEngine:
    """Generate short critiques after each orchestrator cycle."""

    def __init__(
        self,
        *,
        lookback: int = 10,
        vol_bps_threshold: float | None = None,
        hold_streak_threshold: int | None = None,
    ) -> None:
        self.lookback = max(1, int(lookback))
        self.vol_bps_threshold = self._resolve_float(vol_bps_threshold, "REFLECTION_VOL_BPS_THRESHOLD", 600.0)
        self.hold_streak_threshold = self._resolve_int(hold_streak_threshold, "REFLECTION_HOLD_STREAK", 3)

    # ------------------------------------------------------------------
    def reflect(
        self,
        cycle: Dict[str, Any],
        *,
        history: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        notes: List[str] = []
        severity = "info"

        arbi = self._extract(cycle, "arbi")
        stake = self._extract(cycle, "stake")
        capacity = self._extract(cycle, "capacity")

        execution_state = (arbi.get("execution") or {}).get("status")
        if execution_state == "error":
            severity = "high"
            notes.append("ArbiDiem execution error detected; require manual review before resuming live trades.")
        elif execution_state == "reflex_halt":
            severity = "medium"
            notes.append("Reflex guardian halted the cycle; confirm anomaly cleared before reenabling.")

        if stake.get("status") not in (None, "ok"):
            severity = "high"
            notes.append("StakeMaster returned a non-ok status; confirm staking heartbeat and contract calls.")

        if capacity.get("status") not in (None, "ok"):
            if severity == "info":
                severity = "medium"
            notes.append("Capacity broker degraded; verify tenant limits and Venice proxy availability.")

        vol_bps = self._to_float(self._extract(arbi, "signals").get("vol_bps"))
        if vol_bps is not None and self.vol_bps_threshold is not None and vol_bps > self.vol_bps_threshold:
            notes.append(
                f"Realized volatility at {vol_bps:.1f} bps exceeds the {self.vol_bps_threshold:.1f} bps guard; consider throttling size."
            )
            if severity == "info":
                severity = "medium"

        hold_streak = self._hold_streak(history, include_current=self._is_hold(arbi))
        if (
            self.hold_streak_threshold is not None
            and hold_streak >= self.hold_streak_threshold
            and self._is_hold(arbi)
        ):
            notes.append(
                f"ArbiDiem has held for {hold_streak} consecutive cycles; revisit thresholds or supply assumptions."
            )

        premium = self._to_float((arbi.get("why") or {}).get("premium"))
        action = arbi.get("action")
        if premium is not None and action == "mint_sell" and premium < 1.0:
            notes.append("Premium fell below parity while still signalling mint/sell; double-check fair value inputs.")
            severity = "high"

        if not notes:
            notes.append("Cycle completed without critical findings.")

        summary = {
            "severity": severity,
            "notes": notes,
            "streaks": {"hold": hold_streak},
        }
        if premium is not None:
            summary.setdefault("metrics", {})["premium"] = premium
        if vol_bps is not None:
            summary.setdefault("metrics", {})["vol_bps"] = vol_bps
        return summary

    # ------------------------------------------------------------------
    def _resolve_float(self, override: float | None, env_name: str, default: float) -> Optional[float]:
        if override is not None:
            return float(override)
        raw = os.getenv(env_name)
        if raw is None:
            return float(default) if default is not None else None
        try:
            return float(raw)
        except ValueError:
            return float(default)

    def _resolve_int(self, override: int | None, env_name: str, default: int) -> Optional[int]:
        if override is not None:
            return int(override)
        raw = os.getenv(env_name)
        if raw is None:
            return int(default) if default is not None else None
        try:
            return int(raw)
        except ValueError:
            return int(default)

    # ------------------------------------------------------------------
    def _extract(self, source: Dict[str, Any], key: str) -> Dict[str, Any]:
        value = source.get(key, {}) if isinstance(source, dict) else {}
        if isinstance(value, dict):
            return value
        return {}

    def _is_hold(self, arbi: Dict[str, Any]) -> bool:
        return (arbi.get("action") or "").lower() == "hold"

    def _hold_streak(self, history: Optional[Sequence[Dict[str, Any]]], *, include_current: bool) -> int:
        streak = 1 if include_current else 0
        if not history:
            return streak
        for entry in reversed(history):
            cycle = entry.get("cycle") if isinstance(entry, dict) else None
            if not isinstance(cycle, dict):
                continue
            arbi = cycle.get("arbi")
            if not isinstance(arbi, dict) or (arbi.get("action") or "").lower() != "hold":
                break
            streak += 1
        return streak

    def _to_float(self, value: Any) -> Optional[float]:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None


__all__ = ["ReflectionEngine"]
