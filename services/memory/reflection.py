from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any


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
        self.vol_bps_threshold = self._resolve_float(
            vol_bps_threshold, "REFLECTION_VOL_BPS_THRESHOLD", 450.0
        )
        self.hold_streak_threshold = self._resolve_int(
            hold_streak_threshold, "REFLECTION_HOLD_STREAK", 4
        )
        self.hold_streak_alert = self._resolve_int(
            None, "REFLECTION_HOLD_STREAK_ALERT", max(5, self.hold_streak_threshold + 1)
        )

    # ------------------------------------------------------------------
    def reflect(
        self,
        cycle: dict[str, Any],
        *,
        history: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        notes: list[str] = []
        severity = "info"
        labels: list[str] = []
        severity_reasons: list[str] = []
        recommendations: list[dict[str, Any]] = []

        arbi = self._extract(cycle, "arbi")
        stake = self._extract(cycle, "stake")
        capacity = self._extract(cycle, "capacity")

        execution = (
            arbi.get("execution") if isinstance(arbi.get("execution"), dict) else {}
        )
        execution_state = (execution or {}).get("status")
        burn_gas_error = self._is_gas_related_burn_error(execution)
        purchased_diem_error = self._is_purchased_diem_burn_error(execution, arbi)
        post_buy_balance_error = self._is_post_buy_balance_sync_error(execution, arbi)
        # "skipped" status indicates graceful handling (e.g., DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV)
        # and should not trigger any severity escalation
        if execution_state == "skipped":
            notes.append(
                "ArbiDiem execution skipped (likely purchased DIEM without locked sVVV). "
                "Consider selling on DEX or switching to mint-based strategy."
            )
            labels.append("execution_skipped")
        elif execution_state == "error":
            if purchased_diem_error:
                # Purchased DIEM cannot be burned - this is expected behavior, not an anomaly.
                # Downgrade to medium severity since the wallet simply holds DEX-purchased DIEM
                # without locked sVVV collateral.
                severity = "medium"
                severity_reasons.append("purchased_diem_burn_blocked")
                labels.append("purchased_diem_no_svvv")
                notes.append(
                    "DIEM in wallet was purchased on DEX, not minted. Cannot burn without locked sVVV; "
                    "consider selling on DEX instead or acquiring minted DIEM."
                )
            elif post_buy_balance_error:
                # Buy succeeded but second burn failed due to stale balance - recoverable
                severity = "medium"
                severity_reasons.append("post_buy_balance_sync")
                labels.append("balance_sync_delay")
                notes.append(
                    "Burn failed due to stale balance after DEX buy. "
                    "Trade succeeded; this is a temporary sync issue."
                )
            else:
                severity = "high"
                severity_reasons.append("arbi_execution_error")
                notes.append(
                    "ArbiDiem execution error detected; require manual review before resuming live trades."
                )
        elif execution_state == "reflex_halt":
            severity = "medium"
            notes.append(
                "Reflex guardian halted the cycle; confirm anomaly cleared before reenabling."
            )

        if stake.get("status") not in (None, "ok"):
            severity = "high"
            severity_reasons.append("stake_error")
            notes.append(
                "StakeMaster returned a non-ok status; confirm staking heartbeat and contract calls."
            )

        if capacity.get("status") not in (None, "ok"):
            if severity == "info":
                severity = "medium"
            notes.append(
                "Capacity broker degraded; verify tenant limits and Venice proxy availability."
            )

        vol_bps = self._to_float(self._extract(arbi, "signals").get("vol_bps"))
        if (
            vol_bps is not None
            and self.vol_bps_threshold is not None
            and vol_bps > self.vol_bps_threshold
        ):
            notes.append(
                f"Realized volatility at {vol_bps:.1f} bps exceeds the {self.vol_bps_threshold:.1f} bps guard; consider throttling size."
            )
            if severity == "info":
                severity = "medium"

        hold_streak = self._hold_streak(history, include_current=self._is_hold(arbi))
        hold_reasons = self._hold_reasons(
            history, current=arbi if self._is_hold(arbi) else None
        )
        dominant_reason = self._dominant_reason(hold_reasons)
        if (
            self.hold_streak_threshold is not None
            and hold_streak >= self.hold_streak_threshold
            and self._is_hold(arbi)
        ):
            notes.append(
                f"ArbiDiem has held for {hold_streak} consecutive cycles; revisit thresholds or supply assumptions."
            )
        if (
            self.hold_streak_alert is not None
            and hold_streak >= self.hold_streak_alert
            and dominant_reason is not None
        ):
            labels.append("hold_streak_alert")
            # Actionable recommendations based on dominant hold reason
            if dominant_reason == "no_locked_svvv_for_burn":
                recommendations.append(
                    {
                        "action": "inventory_liquidation",
                        "reason": dominant_reason,
                        "streak": hold_streak,
                    }
                )
                notes.append(
                    "Hold streak driven by purchased DIEM without locked sVVV; recommend DEX liquidation or acquiring sVVV collateral."
                )
            elif dominant_reason in {
                "premium_insufficient",
                "market_not_favorable",
                "discount_not_met",
                "risk_rejected",
            }:
                current_premium = self._to_float((arbi.get("why") or {}).get("premium"))
                current_threshold = self._resolve_float(
                    None, "DIEM_PREMIUM_THRESHOLD", 1.01
                )
                suggested = None
                if current_premium is not None and current_premium < 1.0:
                    # Nudge threshold toward market while keeping a safety buffer
                    suggested = max(
                        0.9, round(min(current_threshold, current_premium + 0.06), 4)
                    )
                    recommendations.append(
                        {
                            "action": "threshold_suggestion",
                            "parameter": "DIEM_PREMIUM_THRESHOLD",
                            "current": float(current_threshold)
                            if current_threshold is not None
                            else None,
                            "suggested": suggested,
                            "observed_premium": current_premium,
                            "streak": hold_streak,
                        }
                    )
                    notes.append(
                        f"Hold streak driven by discount ({current_premium:.2f}x vs fair); consider lowering premium trigger toward {suggested or 'market'} within guardrails."
                    )
            elif dominant_reason == "insufficient_balance":
                recommendations.append(
                    {
                        "action": "fund_wallet",
                        "reason": dominant_reason,
                        "streak": hold_streak,
                        "asset": "USDC",
                    }
                )
                notes.append(
                    "Hold streak driven by insufficient USDC balance for trades; deposit USDC to enable buy/burn arbitrage."
                )
            else:
                notes.append(
                    f"Hold streak dominated by '{dominant_reason}'; review policy knobs or liquidity settings."
                )

        premium = self._to_float((arbi.get("why") or {}).get("premium"))
        action = arbi.get("action")
        if premium is not None and action == "mint_sell" and premium < 1.0:
            notes.append(
                "Premium fell below parity while still signalling mint/sell; double-check fair value inputs."
            )
            severity = "high"
            severity_reasons.append("premium_action_mismatch")

        if burn_gas_error:
            labels.append("burn_gas_error")
            labels.append("execution_gas_issue")
            if severity == "high" and severity_reasons == ["arbi_execution_error"]:
                severity = "medium"
                severity_reasons = ["arbi_execution_error_gas"]
                notes.append(
                    "DIEM burn failed because of gas or fee conditions; treat as recoverable and retry after gas top-up or fee bump."
                )

        if not notes:
            notes.append("Cycle completed without critical findings.")

        summary = {
            "severity": severity,
            "notes": notes,
            "streaks": {"hold": hold_streak},
        }
        if severity_reasons:
            summary["severity_reasons"] = severity_reasons
        if labels:
            summary["labels"] = labels
        if recommendations:
            summary["recommendations"] = recommendations
        if premium is not None:
            summary.setdefault("metrics", {})["premium"] = premium
        if vol_bps is not None:
            summary.setdefault("metrics", {})["vol_bps"] = vol_bps
        return summary

    # ------------------------------------------------------------------
    def _resolve_float(
        self, override: float | None, env_name: str, default: float
    ) -> float | None:
        if override is not None:
            return float(override)
        raw = os.getenv(env_name)
        if raw is None:
            return float(default) if default is not None else None
        try:
            return float(raw)
        except ValueError:
            return float(default)

    def _resolve_int(
        self, override: int | None, env_name: str, default: int
    ) -> int | None:
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
    def _extract(self, source: dict[str, Any], key: str) -> dict[str, Any]:
        value = source.get(key, {}) if isinstance(source, dict) else {}
        if isinstance(value, dict):
            return value
        return {}

    def _is_hold(self, arbi: dict[str, Any]) -> bool:
        return (arbi.get("action") or "").lower() == "hold"

    def _hold_streak(
        self, history: Sequence[dict[str, Any]] | None, *, include_current: bool
    ) -> int:
        streak = 1 if include_current else 0
        if not history:
            return streak
        for entry in reversed(history):
            cycle = entry.get("cycle") if isinstance(entry, dict) else None
            if not isinstance(cycle, dict):
                continue
            arbi = cycle.get("arbi")
            if (
                not isinstance(arbi, dict)
                or (arbi.get("action") or "").lower() != "hold"
            ):
                break
            streak += 1
        return streak

    def _hold_reasons(
        self,
        history: Sequence[dict[str, Any]] | None,
        *,
        current: dict[str, Any] | None = None,
    ) -> list[str]:
        """Collect reasons for consecutive hold actions starting from the latest cycle.

        Returns a list of reason strings (lowercased) for streak analysis.
        """

        reasons: list[str] = []

        def _reason_from_arbi(arbi: dict[str, Any]) -> str | None:
            if not isinstance(arbi, dict):
                return None
            r = arbi.get("reason")
            if r:
                return str(r).strip().lower()
            why = arbi.get("why")
            if isinstance(why, dict) and why.get("reason"):
                return str(why.get("reason")).strip().lower()
            return None

        if current is not None and self._is_hold(current):
            cur_reason = _reason_from_arbi(current)
            if cur_reason:
                reasons.append(cur_reason)
            else:
                reasons.append("unspecified")

        if not history:
            return reasons

        for entry in reversed(history):
            cycle = entry.get("cycle") if isinstance(entry, dict) else None
            if not isinstance(cycle, dict):
                continue
            arbi = cycle.get("arbi") if isinstance(cycle, dict) else None
            if not isinstance(arbi, dict) or not self._is_hold(arbi):
                break
            r = _reason_from_arbi(arbi) or "unspecified"
            reasons.append(r)
        return reasons

    def _dominant_reason(self, reasons: list[str]) -> str | None:
        if not reasons:
            return None
        counts: dict[str, int] = {}
        for r in reasons:
            counts[r] = counts.get(r, 0) + 1
        dominant = max(counts, key=counts.get)
        return dominant

    def _to_float(self, value: Any) -> float | None:
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _is_gas_related_burn_error(self, execution: dict[str, Any]) -> bool:
        if not isinstance(execution, dict):
            return False

        burn = execution.get("burn")
        if not isinstance(burn, dict) or burn.get("status") != "error":
            return False

        diag = burn.get("diagnostics") or execution.get("diagnostics") or {}
        diag_values: list[str] = []
        if isinstance(diag, dict):
            for key in ("reason", "message", "details"):
                value = diag.get(key)
                if value:
                    diag_values.append(str(value))

        error_candidates = [
            burn.get("error"),
            burn.get("message"),
            execution.get("error"),
        ]
        combined = " ".join(
            [str(val) for val in error_candidates + diag_values if val is not None]
        ).lower()
        error_type = str(
            burn.get("error_type")
            or burn.get("code")
            or execution.get("error_type")
            or ""
        ).lower()

        keywords = [
            "insufficient funds for gas",
            "gas required exceeds allowance",
            "intrinsic gas",
            "gas estimation failed",
            "cannot estimate gas",
            "out of gas",
            "fee cap",
            "max fee per gas",
            "priority fee",
            "transaction underpriced",
            "replacement transaction underpriced",
            "base fee",
            "gas price too low",
        ]
        if any(k in combined for k in keywords):
            return True

        return any(
            term in error_type for term in ("insufficientfunds", "outofgas", "gas")
        )

    def _is_purchased_diem_burn_error(
        self, execution: dict[str, Any], arbi: dict[str, Any]
    ) -> bool:
        """Check if the error is due to attempting to burn purchased DIEM (no locked sVVV).

        This is expected behavior when the wallet holds DIEM purchased on DEX rather
        than minted. Burning requires locked sVVV collateral that doesn't exist for
        purchased DIEM.
        """
        if not isinstance(execution, dict):
            return False

        # Check execution-level error
        exec_error = str(execution.get("error") or "").lower()
        if exec_error in ("locked_svvv_unknown", "no_locked_svvv"):
            return True

        # Check burn sub-result
        burn = execution.get("burn")
        if isinstance(burn, dict):
            burn_error = str(burn.get("error") or "").lower()
            burn_reason = str(burn.get("reason") or "").lower()
            if burn_error in ("locked_svvv_unknown", "no_locked_svvv"):
                return True
            if "cannot verify locked svvv" in burn_reason:
                return True
            if "no_locked_svvv" in burn_reason or "locked_svvv" in burn_error:
                return True

        # Check arbi rationale for burn eligibility info
        why = arbi.get("why") if isinstance(arbi, dict) else {}
        if isinstance(why, dict):
            burn_eligibility = why.get("burn_eligibility") or (
                why.get("execution", {}) or {}
            ).get("internal", {}).get("burn_eligibility", {})
            if isinstance(burn_eligibility, dict):
                reason = str(burn_eligibility.get("reason") or "").lower()
                if reason in (
                    "cannot_query_locked_svvv",
                    "no_locked_svvv",
                    "insufficient_locked_svvv",
                ):
                    return True

            # Check execution_error field in rationale
            exec_error_class = str(why.get("execution_error_class") or "").lower()
            if exec_error_class in ("locked_svvv_unknown", "no_locked_svvv"):
                return True

            reason_field = str(why.get("reason") or "").lower()
            if reason_field in ("burn_failed", "cannot_burn_purchased_diem"):
                # Confirm it's related to locked sVVV
                exec_err = why.get("execution_error") or {}
                if isinstance(exec_err, dict):
                    err_code = str(exec_err.get("error") or "").lower()
                    if "svvv" in err_code or "locked" in err_code:
                        return True
                elif isinstance(exec_err, str) and "svvv" in exec_err.lower():
                    return True

        return False

    def _is_post_buy_balance_sync_error(
        self, execution: dict[str, Any], _arbi: dict[str, Any]
    ) -> bool:
        """Check if error is due to stale balance after successful buy."""
        if not isinstance(execution, dict):
            return False

        buy = execution.get("buy", {})
        burn = execution.get("burn", {})

        # Buy must have succeeded
        if buy.get("status") not in ("submitted", "sent"):
            return False

        # Burn must have failed with balance error
        if not isinstance(burn, dict):
            return False

        burn_steps = burn.get("steps", [])
        for step in burn_steps:
            if isinstance(step, dict):
                if step.get("error") == "insufficient_diem_balance":
                    return True
                if step.get("reason") == "insufficient_diem_balance":
                    return True

        return False


__all__ = ["ReflectionEngine"]
