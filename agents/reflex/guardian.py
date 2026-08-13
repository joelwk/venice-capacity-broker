from __future__ import annotations

import os
from typing import Any

from libs.telemetry.logger import get_logger

try:
    from libs.telemetry.events import emit as _emit_event
except Exception:  # pragma: no cover - optional telemetry

    def _emit_event(kind: str, payload: dict[str, Any]) -> None:  # type: ignore
        return


logger = get_logger("agent.reflex")


class ReflexGuardian:
    """Best-effort anomaly detector that can veto live trades."""

    def __init__(
        self,
        *,
        max_vol_bps: float | None = None,
        max_utilization: float | None = None,
        max_drawdown: float | None = None,
        apply_in_dry_run: bool | None = None,
        require_active_stake: bool | None = None,
    ) -> None:
        self.max_vol_bps = self._resolve_float(max_vol_bps, "REFLEX_MAX_VOL_BPS", 450.0)
        self.max_utilization = self._resolve_float(
            max_utilization, "REFLEX_MAX_UTILIZATION", 0.92
        )
        self.max_drawdown = self._resolve_float(
            max_drawdown, "REFLEX_MAX_PRICE_DRAWDOWN", 0.12
        )
        if self.max_drawdown and self.max_drawdown > 1.0:
            self.max_drawdown = self.max_drawdown / 100.0
        self.apply_in_dry_run = self._resolve_flag(
            apply_in_dry_run, "REFLEX_APPLY_DRY_RUN", False
        )
        self.require_active_stake = self._resolve_flag(
            require_active_stake, "REFLEX_REQUIRE_ACTIVE_STAKE", True
        )
        self._consecutive_inactive = 0
        self._provider_error_streak = 0
        try:
            self._provider_error_threshold = max(
                1, int(os.getenv("REFLEX_PROVIDER_ERROR_THRESHOLD", "2") or 2)
            )
        except Exception:
            self._provider_error_threshold = 2

        # Log effective configuration at startup for observability
        logger.info(
            "ReflexGuardian initialized: max_vol_bps=%.1f, max_utilization=%.2f, "
            "max_drawdown=%.2f, apply_in_dry_run=%s, require_active_stake=%s",
            self.max_vol_bps or 0.0,
            self.max_utilization or 0.0,
            self.max_drawdown or 0.0,
            self.apply_in_dry_run,
            self.require_active_stake,
        )

    # ------------------------------------------------------------------
    def evaluate(
        self,
        *,
        price: float | None,
        utilization: float | None,
        vol_bps: float | None,
        stake: dict[str, Any] | None,
        dry_run: bool,
        enable_live: bool,
        last_cycle: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return decision payload: halt bool + telemetry."""

        reasons: list[str] = []
        warnings: list[str] = []

        if dry_run and not self.apply_in_dry_run:
            return self._result(False, reasons, warnings, price, utilization, vol_bps)
        if not enable_live and not dry_run:
            return self._result(False, reasons, warnings, price, utilization, vol_bps)

        if price is None or price <= 0:
            reasons.append("price_unavailable")
        else:
            last_price = self._extract_last_price(last_cycle)
            if last_price is not None and last_price > 0:
                drawdown = (last_price - price) / last_price
                if self.max_drawdown is not None and drawdown > self.max_drawdown:
                    reasons.append("price_drawdown")

        if (
            vol_bps is not None
            and self.max_vol_bps is not None
            and vol_bps > self.max_vol_bps
        ):
            reasons.append("volatility_exceeded")

        if (
            utilization is not None
            and self.max_utilization is not None
            and utilization > self.max_utilization
        ):
            warnings.append("utilization_hot")

        stake_payload = stake if isinstance(stake, dict) else {}
        stake_status_raw = stake_payload.get("status")
        stake_status = str(stake_status_raw or "").lower()
        if stake_status not in {"", "ok", "unknown"}:
            reasons.append("stake_error")
        if self.require_active_stake:
            snapshot = (
                stake_payload.get("snapshot")
                if isinstance(stake_payload, dict)
                else None
            )
            active_flag: bool | None
            if isinstance(snapshot, dict) and "active_staker" in snapshot:
                active_flag = bool(snapshot.get("active_staker"))
            else:
                active_flag = None
            threshold = self._stake_inactive_threshold()
            if stake_status == "unknown" or active_flag is None:
                self._consecutive_inactive = 0
            elif active_flag is False:
                self._consecutive_inactive += 1
            else:
                self._consecutive_inactive = 0
            if self._consecutive_inactive >= threshold:
                reasons.append("stake_inactive")

        # Raise warnings/halts when recent cycles show provider reverts or errors
        provider_errors = self._extract_provider_errors(last_cycle)
        if provider_errors is not None:
            if provider_errors > 0:
                self._provider_error_streak += 1
                warnings.append("dex_provider_errors")
                if self._provider_error_streak >= self._provider_error_threshold:
                    reasons.append("dex_provider_errors")
            else:
                self._provider_error_streak = 0

        halt = bool(reasons)
        result = self._result(halt, reasons, warnings, price, utilization, vol_bps)
        observed = result.get("observed", {})
        observed["stake_inactive_consecutive"] = self._consecutive_inactive
        observed["stake_status"] = stake_status
        observed["provider_error_streak"] = self._provider_error_streak
        observed["provider_errors_last_cycle"] = provider_errors
        result["observed"] = observed
        if last_cycle is not None:
            result["lastCycleTs"] = last_cycle.get("ts")
        if halt or warnings:
            logger.warning(
                "Reflex guardian %s reasons=%s warnings=%s",
                "halted" if halt else "warned",
                reasons,
                warnings,
            )
            try:
                _emit_event(
                    "agent.reflex",
                    {
                        "halt": halt,
                        "reasons": reasons,
                        "warnings": warnings,
                        "price": price,
                        "utilization": utilization,
                        "vol_bps": vol_bps,
                    },
                )
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    def _stake_inactive_threshold(self) -> int:
        try:
            value = int(os.getenv("REFLEX_STAKE_INACTIVE_CONSEC") or 3)
        except Exception:
            value = 3
        return max(1, value)

    def _result(
        self,
        halt: bool,
        reasons: list[str],
        warnings: list[str],
        price: float | None,
        utilization: float | None,
        vol_bps: float | None,
    ) -> dict[str, Any]:
        return {
            "halt": bool(halt),
            "reasons": reasons,
            "warnings": warnings,
            "observed": {
                "price": price,
                "utilization": utilization,
                "vol_bps": vol_bps,
            },
            "limits": {
                "max_vol_bps": self.max_vol_bps,
                "max_utilization": self.max_utilization,
                "max_drawdown": self.max_drawdown,
            },
        }

    def _extract_last_price(self, last_cycle: dict[str, Any] | None) -> float | None:
        if not isinstance(last_cycle, dict):
            return None
        cycle = last_cycle.get("cycle") if "cycle" in last_cycle else last_cycle
        if not isinstance(cycle, dict):
            return None
        arbi = cycle.get("arbi")
        if not isinstance(arbi, dict):
            return None
        try:
            price = arbi.get("price")
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    def _extract_provider_errors(self, last_cycle: dict[str, Any] | None) -> int | None:
        if not isinstance(last_cycle, dict):
            return None
        cycle = last_cycle.get("cycle") if "cycle" in last_cycle else last_cycle
        if not isinstance(cycle, dict):
            return None
        arbi = cycle.get("arbi") or cycle.get("arbi_diem") or {}
        if not isinstance(arbi, dict):
            return None
        # Prefer explicit quote_summary on arbi record, then rationale
        candidates = []
        direct = arbi.get("quote_summary") or arbi.get("quoteSummary")
        if direct:
            candidates.append(direct)
        why = arbi.get("why")
        if isinstance(why, dict):
            qs = why.get("quote_summary") or why.get("quoteSummary")
            if qs:
                candidates.append(qs)
        for cand in candidates:
            try:
                if isinstance(cand, dict) and "provider_errors" in cand:
                    return int(cand.get("provider_errors") or 0)
            except Exception:
                continue
        return None

    def _resolve_flag(
        self, override: bool | None, env_name: str, default: bool
    ) -> bool:
        if override is not None:
            return bool(override)
        raw = os.getenv(env_name)
        if raw is None:
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _resolve_float(
        self, override: float | None, env_name: str, default: float | None
    ) -> float | None:
        if override is not None:
            return float(override)
        raw = os.getenv(env_name)
        if raw is None:
            return float(default) if default is not None else None
        try:
            return float(raw)
        except ValueError:
            return float(default) if default is not None else None


__all__ = ["ReflexGuardian"]
