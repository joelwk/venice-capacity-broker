from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

from libs.telemetry.logger import get_logger
from services.staking.client import (
    StakingService,
    is_stake_estimate_overflow_error,
    run_with_stake_overflow_backoff,
    stake_estimate_error_signature,
)

try:
    from libs.telemetry.events import emit as _emit_event
except Exception:

    def _emit_event(kind: str, payload: dict) -> None:  # type: ignore
        return


_HEARTBEAT_KV_KEY = "staking:heartbeat:last"
_CLAIM_KV_KEY = "staking:claim:last"


logger = get_logger("agent.stake_master")


@dataclass
class StakeMaster:
    staking: StakingService
    heartbeat_interval_hours: float = 48.0
    venice_client: object | None = None
    auto_stake_max_attempts: int = 3
    market: Any | None = None
    _kv_store: object | None = field(default=None, init=False, repr=False)
    _venice_cached: object | None = field(default=None, init=False, repr=False)
    _auto_stake_attempted: bool = field(default=False, init=False, repr=False)
    _auto_stake_attempts: int = field(default=0, init=False, repr=False)
    _last_claim_cached: float | None = field(default=None, init=False, repr=False)
    _stake_recommendation: dict[str, Any] | None = field(
        default=None, init=False, repr=False
    )

    def ingest_recommendation(self, rec: dict[str, Any] | None) -> None:
        """Store a soft staking recommendation (e.g., ArbiDiem insufficient_svvv).

        The recommendation does not change staking policy; it is surfaced in outputs
        for observability and may be annotated when an unrelated stake happens to
        reduce the shortfall.
        """
        if rec is None:
            self._stake_recommendation = None
            return
        try:
            self._stake_recommendation = dict(rec)
        except Exception:
            self._stake_recommendation = rec

    def run_once(
        self, live: bool = False, recommendation: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Single heartbeat cycle.

        Reads staking status; if ``live`` and rewards are available, it attempts a
        claim. Returns a structured summary so orchestrators can record the
        outcome without parsing logs.
        """
        status = self.staking.status()
        logger.info(f"Status: {status}")
        _emit_event(
            "staking.status",
            {
                "staked": int(status.get("staked", 0)),
                "rewards": int(status.get("rewards", 0)),
                "active": bool(status.get("active_staker")),
                "cooldown_remaining": status.get("cooldown", {}).get(
                    "seconds_remaining"
                ),
            },
        )

        # Merge optional recommendation passed in with any previously ingested one.
        effective_recommendation = (
            recommendation if recommendation is not None else self._stake_recommendation
        )

        claim_info: dict[str, Any] = {
            "attempted": False,  # Will be set to True only if we actually attempt the claim
            "executed": False,
            "tx": None,
            "reason": None,
        }
        stake_action: dict[str, Any] = {
            "attempted": False,
            "executed": False,
            "tx": None,
            "reason": None,
        }
        staked_units = int(status.get("staked", 0))
        min_active_units = int(
            status.get("min_active_stake")
            or os.getenv("VVV_ACTIVE_MIN_STAKE_UNITS", "0")
            or 0
        )
        progressive_env = str(
            os.getenv("STAKEMASTER_PROGRESSIVE_ENABLE", "true")
        ).strip().lower() in {"1", "true", "yes", "on"}
        try:
            max_attempts_env = os.getenv("STAKEMASTER_AUTO_STAKE_MAX_ATTEMPTS")
            max_attempts = (
                int(max_attempts_env)
                if max_attempts_env
                else int(self.auto_stake_max_attempts)
            )
        except Exception:
            max_attempts = int(self.auto_stake_max_attempts)
        max_attempts = max(1, max_attempts)
        stake_action["max_attempts"] = max_attempts
        if (
            live
            and progressive_env
            and staked_units <= 0
            and min_active_units > 0
            and self._auto_stake_attempts < max_attempts
        ):
            available_units = self._wallet_vvv_balance()
            if available_units is not None:
                try:
                    stake_action["available"] = int(available_units)
                except Exception:
                    stake_action["available"] = available_units
            try:
                stake_action["required"] = int(min_active_units)
            except Exception:
                stake_action["required"] = min_active_units
            if available_units is None:
                try:
                    logger.warning(
                        "Auto-stake skipped: unable to determine VVV balance"
                    )
                except Exception:
                    pass
                stake_action.update(
                    {
                        "attempted": False,
                        "executed": False,
                        "reason": "balance_unknown",
                    }
                )
                try:
                    _emit_event(
                        "staking.auto_stake",
                        {
                            "status": "skipped",
                            "units": int(min_active_units),
                            "reason": "balance_unknown",
                        },
                    )
                except Exception:
                    pass
                self._auto_stake_attempted = True
                self._auto_stake_attempts = max_attempts
            elif int(available_units) < int(min_active_units):
                available_int = int(available_units)
                required_int = int(min_active_units)
                logger.warning(
                    "Auto-stake skipped: insufficient VVV balance",
                    extra={"available": available_int, "required": required_int},
                )
                stake_action.update(
                    {
                        "attempted": False,
                        "executed": False,
                        "reason": "insufficient_balance",
                    }
                )
                stake_action["available"] = available_int
                stake_action["required"] = required_int
                try:
                    _emit_event(
                        "staking.auto_stake",
                        {
                            "status": "skipped",
                            "units": required_int,
                            "available": available_int,
                            "reason": "insufficient_balance",
                        },
                    )
                except Exception:
                    pass
                self._auto_stake_attempted = True
                self._auto_stake_attempts = max_attempts
            else:
                nonce_state = self._nonce_state()
                pending_nonce = False
                if nonce_state and int(nonce_state.get("pending", 0)) > int(
                    nonce_state.get("latest", 0)
                ):
                    pending_nonce = True
                    try:
                        logger.warning(
                            "Auto-stake skipped: pending transaction requires nonce clearance",
                            extra={
                                "nonce_latest": nonce_state.get("latest"),
                                "nonce_pending": nonce_state.get("pending"),
                            },
                        )
                    except Exception:
                        pass
                    stake_action.update(
                        {
                            "attempted": False,
                            "executed": False,
                            "reason": "pending_nonce",
                            "nonce": nonce_state,
                            "attempts": self._auto_stake_attempts,
                        }
                    )
                    _emit_event(
                        "staking.auto_stake",
                        {
                            "status": "skipped",
                            "units": int(min_active_units),
                            "reason": "pending_nonce",
                            "nonce": nonce_state,
                        },
                    )
                    self._auto_stake_attempted = True
                    self._auto_stake_attempts = max_attempts

                if not pending_nonce:
                    stake_action["attempted"] = True
                    try:
                        approve_tx = None
                        try:
                            approve_tx = self.staking.approve(int(min_active_units))
                            stake_action["approve"] = approve_tx
                        except Exception as approve_exc:
                            logger.warning(f"Auto-stake approve failed: {approve_exc}")
                            stake_action.update(
                                {
                                    "executed": False,
                                    "reason": f"approve_error:{approve_exc}",
                                }
                            )
                            _emit_event(
                                "staking.auto_stake",
                                {
                                    "status": "error",
                                    "units": int(min_active_units),
                                    "error": f"approve:{approve_exc}",
                                },
                            )
                            self._auto_stake_attempted = True
                            raise

                        res = None
                        # Round to stake increment before staking
                        rounded_min_active = self._round_to_stake_increment(
                            int(min_active_units)
                        )
                        staked_units_actual = rounded_min_active
                        overflow_attempts: list[dict[str, Any]] = []
                        overflow_stop_reason: str | None = None
                        stake_action["stake_units_requested"] = rounded_min_active
                        if rounded_min_active != int(min_active_units):
                            stake_action["stake_units_original"] = int(min_active_units)
                        price_usd = self._vvv_price_usd()
                        min_stake_usd = self._idle_min_stake_usd()
                        (
                            res,
                            staked_units_actual,
                            overflow_attempts,
                            overflow_stop_reason,
                        ) = self._stake_with_overflow_backoff(
                            rounded_min_active,
                            price_usd=(
                                None
                                if price_usd in (None, 0, 0.0)
                                else float(price_usd)
                            ),
                            min_usd=float(min_stake_usd) if min_stake_usd else None,
                        )
                        if overflow_attempts:
                            stake_action["stake_overflow_attempts"] = overflow_attempts
                            stake_action["stake_overflow_retries"] = len(
                                overflow_attempts
                            )
                        if overflow_stop_reason:
                            stake_action["stake_overflow_stop_reason"] = (
                                overflow_stop_reason
                            )
                        if res is None:
                            last_error = (
                                overflow_attempts[-1]["error"]
                                if overflow_attempts
                                else "stake_estimate_failed"
                            )
                            error_msg = f"stake_estimate_failed:{last_error}"
                            raise RuntimeError(error_msg) from None
                        logger.info(
                            f"Auto-stake executed: units={staked_units_actual} result={res}"
                        )
                        _emit_event(
                            "staking.auto_stake",
                            {
                                "status": (
                                    res.get("status") if isinstance(res, dict) else "ok"
                                ),
                                "units": int(staked_units_actual),
                            },
                        )
                        stake_action.update(
                            {
                                "executed": True,
                                "tx": res,
                                "reason": (
                                    "auto_stake"
                                    if int(staked_units_actual) == int(min_active_units)
                                    else "auto_stake_backoff"
                                ),
                                "staked_units": int(staked_units_actual),
                                "attempts": self._auto_stake_attempts + 1,
                            }
                        )
                        self._auto_stake_attempted = True
                        self._auto_stake_attempts = max_attempts
                        status = self.staking.status()
                        staked_units = int(status.get("staked", staked_units))
                    except Exception as exc:
                        logger.warning(f"Auto-stake attempt failed: {exc}")
                        self._auto_stake_attempts += 1
                        attempt_reason = f"stake_error:{exc}"
                        stake_action.update(
                            {
                                "executed": False,
                                "reason": attempt_reason,
                                "attempts": self._auto_stake_attempts,
                            }
                        )
                        try:
                            message_lower = str(exc).lower()
                        except Exception:
                            message_lower = ""
                        recovery_tx: dict[str, Any] | None = None
                        recovery_error: Exception | None = None
                        nonce_details: dict[str, int] | None = None
                        if self._is_overflow_estimate_error(exc):
                            attempt_reason = "contract_overflow"
                            stake_action["reason"] = attempt_reason
                            stake_action["followup"] = "stake_size_backoff_exhausted"
                            self._auto_stake_attempted = True
                            self._auto_stake_attempts = max_attempts
                        nonce_issue = any(
                            term in message_lower
                            for term in [
                                "nonce too low",
                                "replacement transaction underpriced",
                            ]
                        )
                        if nonce_issue:
                            nonce_details = nonce_state or self._nonce_state()
                            if nonce_details:
                                stake_action["nonce"] = nonce_details
                            try:
                                recovery_tx = self._retry_stake_with_gas_bump(
                                    int(min_active_units), nonce_state=nonce_details
                                )
                            except Exception as retry_exc:
                                recovery_error = retry_exc
                                recovery_tx = None
                            if recovery_tx:
                                stake_action.update(
                                    {
                                        "executed": True,
                                        "tx": recovery_tx,
                                        "reason": "auto_stake_retry",
                                        "followup": "nonce_recovered",
                                        "attempts": self._auto_stake_attempts,
                                    }
                                )
                                self._auto_stake_attempted = True
                                self._auto_stake_attempts = max_attempts
                                try:
                                    logger.info(
                                        "Auto-stake nonce recovery succeeded",
                                        extra={
                                            "nonce_latest": (
                                                (nonce_details or {}).get("latest")
                                                if nonce_details
                                                else None
                                            ),
                                            "nonce_pending": (
                                                (nonce_details or {}).get("pending")
                                                if nonce_details
                                                else None
                                            ),
                                        },
                                    )
                                except Exception:
                                    pass
                                try:
                                    _emit_event(
                                        "staking.auto_stake",
                                        {
                                            "status": "recovered",
                                            "units": int(min_active_units),
                                            "nonce": nonce_details,
                                        },
                                    )
                                except Exception:
                                    pass
                                status = self.staking.status()
                                staked_units = int(status.get("staked", staked_units))
                            else:
                                stake_action["followup"] = "nonce_conflict"
                                self._auto_stake_attempted = True
                                self._auto_stake_attempts = max_attempts
                                if recovery_error is not None:
                                    stake_action["retry_error"] = str(recovery_error)
                        if recovery_tx is None:
                            _emit_event(
                                "staking.auto_stake",
                                {
                                    "status": "error",
                                    "units": int(min_active_units),
                                    "error": str(exc),
                                    "attempt": self._auto_stake_attempts,
                                    "max_attempts": max_attempts,
                                },
                            )
                            if self._auto_stake_attempts >= max_attempts:
                                self._auto_stake_attempted = True
                            else:
                                self._auto_stake_attempted = False

        elif live and progressive_env and staked_units <= 0 and min_active_units > 0:
            stake_action.update(
                {
                    "attempted": False,
                    "executed": False,
                    "reason": "attempts_exhausted",
                    "attempts": self._auto_stake_attempts,
                    "max_attempts": max_attempts,
                }
            )

        rewards = int(status.get("rewards", 0))
        claim_info["rewards"] = rewards

        valuation = self._reward_value_summary(rewards)
        if valuation:
            claim_info["valuation"] = valuation

        min_units = self._min_claim_units()
        if min_units > 0:
            claim_info["min_units"] = int(min_units)

        min_interval = self._min_claim_interval_seconds()
        if min_interval > 0:
            claim_info["min_interval_seconds"] = float(min_interval)

        last_claim_ts = self._last_claim_ts()
        if last_claim_ts is not None:
            claim_info["last_claim_ts"] = float(last_claim_ts)

        estimate: dict[str, Any] | None = None

        if live:
            reason: str | None = None
            now = time.time()

            # Dynamic claim gating: compare reward value against estimated gas cost.
            min_claim_value_usd = float(
                os.getenv("STAKEMASTER_MIN_CLAIM_USD", "0.10") or 0.0
            )
            gas_buffer_mult = float(
                os.getenv("STAKEMASTER_CLAIM_GAS_BUFFER_MULT", "2.0") or 2.0
            )

            reward_usd = valuation.get("usd") if isinstance(valuation, dict) else None
            reward_eth = valuation.get("eth") if isinstance(valuation, dict) else None

            if rewards > 0:
                estimate = self._estimate_claim_cost()
                if estimate is not None:
                    self._augment_estimate_values(estimate, valuation)
                    claim_info["gas_estimate"] = estimate

            gas_fee_usd = None
            gas_fee_eth = None
            if isinstance(estimate, dict):
                gas_fee_usd = estimate.get("fee_usd")
                gas_fee_eth = estimate.get("fee_eth")
                if gas_fee_usd is None:
                    fee_eth = estimate.get("fee_eth")
                    eth_price = None
                    if isinstance(valuation, dict):
                        eth_price = valuation.get("eth_price_usd")
                    if fee_eth is not None and eth_price not in (None, 0):
                        try:
                            gas_fee_usd = float(fee_eth) * float(eth_price)
                        except Exception:
                            gas_fee_usd = None

            value_gate_passed: bool | None = None
            required_reward_usd: float | None = None
            required_reward_eth: float | None = None

            if (
                reward_usd is not None
                and gas_fee_usd is not None
                and float(gas_fee_usd) >= 0
            ):
                required_reward_usd = max(
                    float(min_claim_value_usd),
                    float(gas_fee_usd) * float(gas_buffer_mult),
                )
                value_gate_passed = float(reward_usd) >= float(required_reward_usd)
            elif (
                reward_eth is not None
                and gas_fee_eth is not None
                and float(gas_fee_eth) >= 0
            ):
                required_reward_eth = float(gas_fee_eth) * float(gas_buffer_mult)
                value_gate_passed = float(reward_eth) >= float(required_reward_eth)
            elif reward_usd is not None and float(min_claim_value_usd) > 0:
                required_reward_usd = float(min_claim_value_usd)
                value_gate_passed = float(reward_usd) >= float(required_reward_usd)

            claim_info.update(
                {
                    "min_claim_value_usd": float(min_claim_value_usd),
                    "gas_buffer_mult": float(gas_buffer_mult),
                    "reward_usd": reward_usd,
                    "gas_fee_usd": gas_fee_usd,
                    "gas_fee_eth": gas_fee_eth,
                    "gas_cost_usd": gas_fee_usd,
                    "gas_cost_eth": gas_fee_eth,
                    "required_reward_usd": required_reward_usd,
                    "required_reward_eth": required_reward_eth,
                    "value_gate_threshold_usd": required_reward_usd,
                    "value_gate_threshold_eth": required_reward_eth,
                    "value_gate_passed": value_gate_passed,
                }
            )

            # Pre-flight gate: check rewards/interval thresholds
            if rewards <= 0:
                reason = "no_rewards"
            elif min_interval > 0 and last_claim_ts is not None:
                elapsed = now - last_claim_ts
                if elapsed < min_interval:
                    reason = "below_min_interval"
                    claim_info["cooldown_remaining_seconds"] = max(
                        0.0, float(min_interval - elapsed)
                    )
            elif value_gate_passed is False:
                reason = "below_min_value_usd"
            elif (
                reward_usd is None
                and value_gate_passed is None
                and min_units > 0
                and rewards < min_units
            ):
                reason = "below_min_units"
                claim_info["deficit_units"] = int(min_units - rewards)

            if reason is None:
                # Only set attempted=True when we pass pre-flight gates
                claim_info["attempted"] = True
                if estimate is None:
                    estimate = self._estimate_claim_cost()
                    if estimate is not None:
                        self._augment_estimate_values(estimate, valuation)
                        claim_info["gas_estimate"] = estimate
                if estimate is None:
                    reason = "gas_estimate_unavailable"
                elif self._gas_exceeds_reward(estimate, valuation):
                    reason = "gas_exceeds_reward"

            if reason is None:
                try:
                    res = self.staking.claim()
                except Exception as exc:
                    logger.warning(f"Claim execution failed: {exc}")
                    claim_info.update(
                        {
                            "executed": False,
                            "reason": f"claim_error:{exc}",
                        }
                    )
                else:
                    logger.info(f"Claim result: {res}")
                    _emit_event(
                        "staking.claim",
                        {
                            "status": res.get("status"),
                            "tx_hash": res.get("tx_hash"),
                            "rewards": rewards,
                        },
                    )
                    claim_info.update(
                        {
                            "executed": True,
                            "tx": res,
                            "reason": "claimed",
                        }
                    )
                    self._record_claim_ts(now)
            else:
                if estimate is None and reason not in {
                    "no_rewards",
                    "below_min_interval",
                    "below_min_units",
                    "below_min_value_usd",
                }:
                    estimate = self._estimate_claim_cost()
                    if estimate is not None:
                        self._augment_estimate_values(estimate, valuation)
                        claim_info["gas_estimate"] = estimate

                if reason == "no_rewards":
                    logger.info("No rewards to claim (live mode)")
                elif reason == "below_min_units":
                    logger.info(
                        "Claim skipped: rewards below threshold",
                        extra={"rewards": rewards, "min_units": min_units},
                    )
                elif reason == "below_min_value_usd":
                    logger.info(
                        "Claim skipped: reward below required value",
                        extra={
                            "reward_usd": (
                                valuation.get("usd")
                                if isinstance(valuation, dict)
                                else None
                            ),
                            "fee_usd": (
                                None if estimate is None else estimate.get("fee_usd")
                            ),
                            "gas_buffer_mult": claim_info.get("gas_buffer_mult"),
                            "threshold_usd": claim_info.get("value_gate_threshold_usd"),
                            "threshold_eth": claim_info.get("value_gate_threshold_eth"),
                        },
                    )
                elif reason == "below_min_interval":
                    elapsed = 0.0 if last_claim_ts is None else now - last_claim_ts
                    logger.info(
                        "Claim skipped: minimum interval not met",
                        extra={"elapsed": elapsed, "required": min_interval},
                    )
                elif reason == "gas_exceeds_reward":
                    fee_eth = None if estimate is None else estimate.get("fee_eth")
                    reward_eth = (
                        valuation.get("eth") if isinstance(valuation, dict) else None
                    )
                    logger.info(
                        "Claim skipped: estimated gas exceeds reward value",
                        extra={
                            "fee_eth": fee_eth,
                            "reward_eth": reward_eth,
                            "fee_usd": (
                                None if estimate is None else estimate.get("fee_usd")
                            ),
                            "reward_usd": (
                                valuation.get("usd")
                                if isinstance(valuation, dict)
                                else None
                            ),
                        },
                    )
                elif reason == "gas_estimate_unavailable":
                    logger.warning("Claim skipped: unable to estimate gas cost")
                else:
                    logger.info(f"Claim skipped: {reason}")
                claim_info["reason"] = reason
        else:
            # Dry-run: mirror live claim gating for accurate operator messaging.
            now = time.time()
            min_claim_value_usd = float(
                os.getenv("STAKEMASTER_MIN_CLAIM_USD", "0.10") or 0.0
            )
            gas_buffer_mult = float(
                os.getenv("STAKEMASTER_CLAIM_GAS_BUFFER_MULT", "2.0") or 2.0
            )
            reward_usd = valuation.get("usd") if isinstance(valuation, dict) else None
            reward_eth = valuation.get("eth") if isinstance(valuation, dict) else None

            if rewards > 0:
                estimate = self._estimate_claim_cost()
                if estimate is not None:
                    self._augment_estimate_values(estimate, valuation)
                    claim_info["gas_estimate"] = estimate

            gas_fee_usd = None
            gas_fee_eth = None
            if isinstance(estimate, dict):
                gas_fee_usd = estimate.get("fee_usd")
                gas_fee_eth = estimate.get("fee_eth")
                if gas_fee_usd is None:
                    fee_eth = estimate.get("fee_eth")
                    eth_price = None
                    if isinstance(valuation, dict):
                        eth_price = valuation.get("eth_price_usd")
                    if fee_eth is not None and eth_price not in (None, 0):
                        try:
                            gas_fee_usd = float(fee_eth) * float(eth_price)
                        except Exception:
                            gas_fee_usd = None

            value_gate_passed: bool | None = None
            required_reward_usd: float | None = None
            required_reward_eth: float | None = None
            if (
                reward_usd is not None
                and gas_fee_usd is not None
                and float(gas_fee_usd) >= 0
            ):
                required_reward_usd = max(
                    float(min_claim_value_usd),
                    float(gas_fee_usd) * float(gas_buffer_mult),
                )
                value_gate_passed = float(reward_usd) >= float(required_reward_usd)
            elif (
                reward_eth is not None
                and gas_fee_eth is not None
                and float(gas_fee_eth) >= 0
            ):
                required_reward_eth = float(gas_fee_eth) * float(gas_buffer_mult)
                value_gate_passed = float(reward_eth) >= float(required_reward_eth)
            elif reward_usd is not None and float(min_claim_value_usd) > 0:
                required_reward_usd = float(min_claim_value_usd)
                value_gate_passed = float(reward_usd) >= float(required_reward_usd)

            claim_info.update(
                {
                    "min_claim_value_usd": float(min_claim_value_usd),
                    "gas_buffer_mult": float(gas_buffer_mult),
                    "reward_usd": reward_usd,
                    "gas_fee_usd": gas_fee_usd,
                    "gas_fee_eth": gas_fee_eth,
                    "gas_cost_usd": gas_fee_usd,
                    "gas_cost_eth": gas_fee_eth,
                    "required_reward_usd": required_reward_usd,
                    "required_reward_eth": required_reward_eth,
                    "value_gate_threshold_usd": required_reward_usd,
                    "value_gate_threshold_eth": required_reward_eth,
                    "value_gate_passed": value_gate_passed,
                }
            )

            dry_reason: str | None = None
            if rewards <= 0:
                dry_reason = "no_rewards"
            elif min_interval > 0 and last_claim_ts is not None:
                elapsed = now - last_claim_ts
                if elapsed < min_interval:
                    dry_reason = "below_min_interval"
                    claim_info["cooldown_remaining_seconds"] = max(
                        0.0, float(min_interval - elapsed)
                    )
            elif value_gate_passed is False:
                dry_reason = "below_min_value_usd"
            elif (
                reward_usd is None
                and value_gate_passed is None
                and min_units > 0
                and rewards < min_units
            ):
                dry_reason = "below_min_units"
                claim_info["deficit_units"] = int(min_units - rewards)

            if dry_reason is None:
                if estimate is None and rewards > 0:
                    estimate = self._estimate_claim_cost()
                    if estimate is not None:
                        self._augment_estimate_values(estimate, valuation)
                        claim_info["gas_estimate"] = estimate
                if rewards > 0 and estimate is None:
                    dry_reason = "gas_estimate_unavailable"
                elif (
                    rewards > 0
                    and estimate is not None
                    and self._gas_exceeds_reward(estimate, valuation)
                ):
                    dry_reason = "gas_exceeds_reward"

            # Normalize dry-run gating reasons so operator logs match live gating semantics.
            would_claim_live = dry_reason is None
            gate_reason: str
            if would_claim_live:
                gate_reason = "dry_run"
            elif dry_reason == "below_min_interval":
                gate_reason = "min_interval_not_met"
            elif dry_reason in {"gas_exceeds_reward", "below_min_value_usd"}:
                # Determine whether the value gate was driven by the minimum claim
                # threshold or by the gas buffer multiple.
                try:
                    eth_price_usd = (
                        float(valuation.get("eth_price_usd"))
                        if isinstance(valuation, dict)
                        and valuation.get("eth_price_usd") not in (None, 0)
                        else None
                    )
                except Exception:
                    eth_price_usd = None

                if reward_usd is None and reward_eth is not None and eth_price_usd:
                    try:
                        reward_usd = float(reward_eth) * float(eth_price_usd)
                    except Exception:
                        reward_usd = None
                if (
                    required_reward_usd is None
                    and required_reward_eth is not None
                    and eth_price_usd
                ):
                    try:
                        required_reward_usd = float(required_reward_eth) * float(
                            eth_price_usd
                        )
                    except Exception:
                        required_reward_usd = None

                if reward_usd is not None:
                    try:
                        min_threshold = float(min_claim_value_usd)
                    except Exception:
                        min_threshold = 0.0
                    gas_threshold = None
                    if gas_fee_usd is not None:
                        try:
                            gas_threshold = float(gas_fee_usd) * float(gas_buffer_mult)
                        except Exception:
                            gas_threshold = None
                    if float(reward_usd) < float(min_threshold):
                        gate_reason = "below_min_claim_usd"
                    elif gas_threshold is not None and float(reward_usd) < float(
                        gas_threshold
                    ):
                        gate_reason = "below_gas_buffer"
                    else:
                        gate_reason = "below_min_claim_usd"
                else:
                    gate_reason = "below_min_claim_usd"
            else:
                gate_reason = str(dry_reason)

            claim_info["would_claim"] = bool(would_claim_live)
            claim_info["dry_run_gate_reason"] = gate_reason
            claim_info["reward_usd"] = reward_usd
            claim_info["required_reward_usd"] = required_reward_usd
            logger.info(
                "Dry-run: claim decision",
                extra={
                    "would_claim": bool(would_claim_live),
                    "gate_reason": gate_reason,
                    "reward_usd": reward_usd,
                    "required_reward_usd": required_reward_usd,
                    "min_claim_value_usd": float(min_claim_value_usd),
                    "gas_fee_usd": gas_fee_usd,
                    "gas_buffer_mult": float(gas_buffer_mult),
                    "min_interval_seconds": float(min_interval),
                    "cooldown_remaining_seconds": claim_info.get(
                        "cooldown_remaining_seconds"
                    ),
                },
            )
            claim_info["reason"] = "dry_run"

        idle_stake_action: dict[str, Any] | None = None
        # Auto-compound gating
        compound_enabled = self._env_flag("STAKEMASTER_AUTO_COMPOUND_ENABLE", True)
        compound_only_if_claimed = self._env_flag(
            "STAKEMASTER_COMPOUND_ONLY_IF_CLAIMED", True
        )
        claim_executed = bool(claim_info.get("executed"))
        should_compound = (
            live
            and compound_enabled
            and (claim_executed or not compound_only_if_claimed)
        )
        should_idle_stake = live and self._env_flag(
            "STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", True
        )
        if should_compound or should_idle_stake:
            mode = "compound" if should_compound else "idle"
            idle_stake_action = self._maybe_stake_idle(
                live=live, recommendation=effective_recommendation, mode=mode
            )
            stake_action["idle"] = idle_stake_action
        else:
            idle_stake_action = {
                "attempted": False,
                "executed": False,
                "reason": "disabled",
                "mode": "idle",
            }
            stake_action["idle"] = idle_stake_action

        heartbeat_forced = not bool(status.get("active_staker"))
        heartbeat_sent, heartbeat_error = self._ensure_heartbeat(force=heartbeat_forced)
        if heartbeat_forced and not heartbeat_sent and heartbeat_error:
            try:
                logger.warning(
                    "Heartbeat forced but not sent",
                    extra={"reason": heartbeat_error},
                )
            except Exception:
                pass

        return {
            "status": "ok",
            "live": bool(live),
            "snapshot": status,
            "claim": claim_info,
            "stake_action": stake_action,
            "idle_stake_action": idle_stake_action,
            "recommendation": effective_recommendation,
            "heartbeat": {
                "sent": heartbeat_sent,
                "forced": heartbeat_forced,
                "error": heartbeat_error,
            },
        }

    @staticmethod
    def _parse_int(raw: str | None) -> int | None:
        if raw is None:
            return None
        value = str(raw).strip()
        if value == "":
            return None
        try:
            return int(value, 0)
        except Exception:
            try:
                return int(float(value))
            except Exception:
                return None

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return str(raw).strip().lower() in {"1", "true", "yes", "on"}

    def _parse_units_env(
        self,
        name: str,
        *,
        default_tokens: float | None = None,
        default_units: int | None = None,
    ) -> int:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            try:
                return max(0, int(str(raw), 0))
            except Exception:
                try:
                    tokens = float(raw)
                    scale = float(10 ** self._vvv_decimals())
                    return max(0, int(tokens * scale))
                except Exception:
                    return 0
        if default_units is not None:
            return max(0, int(default_units))
        if default_tokens is not None:
            try:
                scale = float(10 ** self._vvv_decimals())
                return max(0, int(float(default_tokens) * scale))
            except Exception:
                return 0
        return 0

    def _vvv_decimals(self) -> int:
        raw = os.getenv("VVV_DECIMALS")
        parsed = self._parse_int(raw)
        if parsed is None:
            return 18
        return max(0, parsed)

    def _vvv_scale(self) -> int:
        try:
            return 10 ** self._vvv_decimals()
        except Exception:
            return 10**18

    def _round_to_stake_increment(self, units: int) -> int:
        """Round down stake amount to configured increment.

        Args:
            units: Amount in base units

        Returns:
            Rounded down amount, or original if increment is disabled
        """
        increment = self._parse_int(os.getenv("STAKEMASTER_STAKE_INCREMENT_UNITS", "0"))
        if increment is None or increment <= 0:
            return int(units)
        return (int(units) // int(increment)) * int(increment)

    def _min_claim_units(self) -> int:
        direct = self._parse_int(
            os.getenv("STAKEMASTER_MIN_CLAIM_UNITS")
            or os.getenv("STAKEMASTER_MIN_CLAIM_WEI")
        )
        if direct is not None:
            return max(0, direct)
        tokens_raw = os.getenv("STAKEMASTER_MIN_CLAIM_TOKENS")
        tokens = None
        if tokens_raw is not None and str(tokens_raw).strip() != "":
            try:
                tokens = float(tokens_raw)
            except Exception:
                tokens = None
        if tokens is None:
            return 0
        scale = 10 ** self._vvv_decimals()
        try:
            return max(0, int(tokens * scale))
        except Exception:
            return 0

    def _min_claim_interval_seconds(self) -> float:
        raw = os.getenv("STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS")
        if raw is None or str(raw).strip() == "":
            default_raw = os.getenv("STAKEMASTER_MIN_CLAIM_INTERVAL_DEFAULT", "3600")
        else:
            default_raw = raw
        try:
            interval = float(default_raw)
        except Exception:
            interval = 3600.0
        return max(0.0, interval)

    def _idle_min_stake_usd(self) -> float:
        """Minimum USD value to stake idle VVV.

        Checks STAKEMASTER_IDLE_STAKE_MIN_USD first (default: 0.20 for optimal compounding).
        Falls back to STAKEMASTER_MIN_STAKE_USD only if the idle-specific var is explicitly
        set to empty string (indicating "use main threshold").
        """
        raw = os.getenv("STAKEMASTER_IDLE_STAKE_MIN_USD")
        if raw is None:
            # Not set at all: use sensible default for idle compounding (lower than general)
            raw = "0.20"
        elif str(raw).strip() == "":
            # Explicitly empty: fall back to general min_stake threshold
            raw = os.getenv("STAKEMASTER_MIN_STAKE_USD", "10.0")
        try:
            return max(0.0, float(raw))
        except Exception:
            return 0.0

    def _idle_wallet_buffer_units(self) -> int:
        return self._parse_units_env(
            "STAKEMASTER_WALLET_VVV_BUFFER_UNITS", default_tokens=0.25
        )

    def _idle_max_per_cycle_units(self) -> int:
        value = self._parse_units_env(
            "STAKEMASTER_IDLE_STAKE_MAX_PER_CYCLE_UNITS", default_tokens=10.0
        )
        return value if value > 0 else 0

    def _last_claim_ts(self) -> float | None:
        if self._last_claim_cached is not None:
            return self._last_claim_cached
        store = self._kv()
        if store is None:
            return self._last_claim_cached
        try:
            raw = store.get(_CLAIM_KV_KEY)
            if raw is None:
                return self._last_claim_cached
            ts = float(raw)
        except Exception:
            return self._last_claim_cached
        self._last_claim_cached = ts
        return ts

    def _record_claim_ts(self, ts: float) -> None:
        self._last_claim_cached = float(ts)
        store = self._kv()
        if store is None:
            return
        try:
            store.set(_CLAIM_KV_KEY, str(float(ts)))
        except Exception:
            return

    def _estimate_claim_cost(self) -> dict[str, Any] | None:
        try:
            return self.staking.estimate_claim_cost()
        except Exception as exc:
            logger.warning(f"Claim gas estimate unavailable: {exc}")
            return None

    def _reward_value_summary(self, rewards: int) -> dict[str, Any]:
        summary: dict[str, Any] = {"raw": int(rewards)}
        decimals = self._vvv_decimals()
        try:
            scale = float(10**decimals)
        except OverflowError:
            scale = float(10**18)
        tokens = None
        if rewards and scale:
            tokens = float(rewards) / scale
        elif rewards == 0:
            tokens = 0.0
        summary["tokens"] = tokens

        price_map: dict[str, float] = {}
        if self.market is not None:
            try:
                price_map = self.market.prices(["VVV", "ETH"])
            except Exception as exc:
                try:
                    logger.debug(f"Reward pricing failed: {exc}")
                except Exception:
                    pass
                price_map = {}

        vvv_price_usd = price_map.get("VVV")
        eth_price_usd = price_map.get("ETH")
        if vvv_price_usd is not None:
            summary["price_usd"] = float(vvv_price_usd)
        if eth_price_usd is not None:
            summary["eth_price_usd"] = float(eth_price_usd)
        usd_value = None
        eth_value = None
        if tokens is not None and vvv_price_usd is not None:
            usd_value = tokens * float(vvv_price_usd)
            summary["usd"] = usd_value
        if usd_value is not None and eth_price_usd not in (None, 0.0):
            try:
                eth_value = usd_value / float(eth_price_usd)
                summary["eth"] = eth_value
            except Exception:
                pass
        elif tokens is not None and eth_price_usd is None and vvv_price_usd is None:
            summary["eth"] = None
        return summary

    def _vvv_price_usd(self) -> float | None:
        if self.market is None:
            return None
        try:
            prices = self.market.prices(["VVV"]) or {}
            raw = prices.get("VVV")
            if raw in (None, "", 0, 0.0):
                return None
            return float(raw)
        except Exception:
            try:
                logger.debug("VVV price lookup failed for idle stake")
            except Exception:
                pass
            return None

    @staticmethod
    def _augment_estimate_values(
        estimate: dict[str, Any], valuation: dict[str, Any]
    ) -> None:
        fee_wei = estimate.get("fee_wei")
        if fee_wei is None:
            return
        try:
            fee_eth = float(fee_wei) / 1e18
        except Exception:
            fee_eth = None
        if fee_eth is not None:
            estimate["fee_eth"] = fee_eth
        eth_price_usd = (
            valuation.get("eth_price_usd") if isinstance(valuation, dict) else None
        )
        if fee_eth is not None and eth_price_usd not in (None, 0.0):
            try:
                estimate["fee_usd"] = fee_eth * float(eth_price_usd)
            except Exception:
                pass

    @staticmethod
    def _gas_exceeds_reward(
        estimate: dict[str, Any] | None, valuation: dict[str, Any]
    ) -> bool:
        if not isinstance(estimate, dict):
            return False
        fee_eth = estimate.get("fee_eth")
        fee_usd = estimate.get("fee_usd")
        reward_eth = valuation.get("eth") if isinstance(valuation, dict) else None
        reward_usd = valuation.get("usd") if isinstance(valuation, dict) else None
        if fee_eth is not None and reward_eth is not None:
            try:
                return float(reward_eth) <= float(fee_eth)
            except Exception:
                pass
        if fee_usd is not None and reward_usd is not None:
            try:
                return float(reward_usd) <= float(fee_usd)
            except Exception:
                pass
        return False

    @staticmethod
    def _is_overflow_estimate_error(exc: Exception) -> bool:
        """Return True when gas estimation failed due to arithmetic overflow/underflow."""

        try:
            return bool(is_stake_estimate_overflow_error(exc))
        except Exception:
            return False

    def _stake_with_overflow_backoff(
        self,
        units: int,
        *,
        max_retries: int | None = None,
        price_usd: float | None = None,
        min_usd: float | None = None,
    ) -> tuple[dict[str, Any] | None, int, list[dict[str, Any]], str | None]:
        """Try staking with size backoff when gas estimation panics."""

        try:
            scale = float(self._vvv_scale())
        except Exception:
            scale = 1e18

        def _stop_if(candidate_units: int) -> str | None:
            if min_usd in (None, 0, 0.0) or price_usd in (None, 0, 0.0):
                return None
            if scale <= 0:
                return None
            try:
                candidate_value = (float(candidate_units) / scale) * float(price_usd)
            except Exception:
                return None
            if candidate_value < float(min_usd):
                return "below_min_usd"
            return None

        tx, staked_units, attempts, stop_reason = run_with_stake_overflow_backoff(
            self.staking.stake,
            int(units),
            max_retries=max_retries,
            stop_if=_stop_if,
        )
        return (
            tx if isinstance(tx, dict) or tx is None else {"raw": tx},
            int(staked_units),
            attempts,
            stop_reason,
        )

    def stake_vvv(self, amount_wei: int, reason: str = "manual") -> dict[str, Any]:
        """
        Stake a specific amount of VVV tokens.

        Args:
            amount_wei: Amount in base units (18 decimals)
            reason: Reason for staking (for logging/telemetry)

        Returns:
            Dict with status, tx, and any errors
        """
        result: dict[str, Any] = {
            "status": "pending",
            "executed": False,
            "tx": None,
            "reason": reason,
            "amount_wei": amount_wei,
            "errors": [],
        }

        try:
            # Round down to stake increment if configured
            original_amount_wei = int(amount_wei)
            amount_wei = self._round_to_stake_increment(original_amount_wei)
            result["amount_wei"] = amount_wei
            if amount_wei != original_amount_wei:
                result["amount_wei_original"] = original_amount_wei

            if amount_wei <= 0:
                result["status"] = "skipped"
                result["errors"].append("amount_wei must be positive")
                return result

            min_stake_usd = float(os.getenv("STAKEMASTER_MIN_STAKE_USD", "10.0"))
            vvv_price = 0.0
            if self.market:
                try:
                    prices = self.market.prices(["VVV"]) or {}
                    vvv_price = float(prices.get("VVV", 0.0))
                except Exception:
                    pass

            if vvv_price > 0:
                vvv_decimals = 18
                try:
                    vvv_decimals = int(os.getenv("VVV_DECIMALS", "18"))
                except Exception:
                    pass
                amount_vvv = float(amount_wei) / (10.0**vvv_decimals)
                amount_usd = amount_vvv * vvv_price
                if amount_usd < min_stake_usd:
                    result["status"] = "skipped"
                    result["errors"].append(
                        f"amount {amount_usd:.2f} USD below minimum {min_stake_usd:.2f} USD"
                    )
                    return result

            gas_estimate = self._estimate_claim_cost()
            if gas_estimate:
                gas_usd = float(gas_estimate.get("gas_usd", 0.0))
                if vvv_price > 0:
                    vvv_decimals = 18
                    try:
                        vvv_decimals = int(os.getenv("VVV_DECIMALS", "18"))
                    except Exception:
                        pass
                    amount_vvv = float(amount_wei) / (10.0**vvv_decimals)
                    stake_usd = amount_vvv * vvv_price
                    if gas_usd > 0 and stake_usd > 0 and gas_usd >= stake_usd * 0.1:
                        result["status"] = "skipped"
                        result["errors"].append(
                            f"gas cost {gas_usd:.2f} USD exceeds 10% of stake value {stake_usd:.2f} USD"
                        )
                        return result

            try:
                res = self.staking.stake(amount_wei)
                result["status"] = "completed"
                result["executed"] = True
                result["tx"] = res
                logger.info(f"Staked {amount_wei} VVV: {res}")
                try:
                    _emit_event(
                        "staking.stake_vvv",
                        {
                            "status": (
                                res.get("status") if isinstance(res, dict) else "ok"
                            ),
                            "units": amount_wei,
                            "reason": reason,
                        },
                    )
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(f"Stake execution failed: {exc}")
                result["status"] = "error"
                result["errors"].append(str(exc))
        except Exception as exc:
            logger.exception("stake_vvv failed")
            result["status"] = "error"
            result["errors"].append(str(exc))

        return result

    def _maybe_stake_idle(
        self,
        *,
        live: bool,
        recommendation: dict[str, Any] | None,
        mode: str = "idle",
    ) -> dict[str, Any]:
        action: dict[str, Any] = {
            "attempted": False,
            "executed": False,
            "tx": None,
            "reason": None,
            "mode": mode,
        }

        if not live:
            action["reason"] = "dry_run"
            return action

        if (
            not self._env_flag("STAKEMASTER_AUTO_STAKE_IDLE_ENABLE", True)
            and mode == "idle"
        ):
            action["reason"] = "disabled"
            return action

        buffer_units = self._idle_wallet_buffer_units()
        max_per_cycle = self._idle_max_per_cycle_units()
        price_usd = self._vvv_price_usd()
        min_stake_usd = self._idle_min_stake_usd()

        balance = self._wallet_vvv_balance()
        if balance is None:
            action.update({"reason": "balance_unknown"})
            return action

        from services.staking.limits import IdleStakeLimits

        limits = IdleStakeLimits.from_env()
        stakeable = max(0, int(balance) - int(buffer_units))
        stake_amount = limits.apply(
            requested_units=int(balance),
            wallet_balance_units=int(balance),
        )

        action.update(
            {
                "wallet_balance": int(balance),
                "buffer_units": int(buffer_units),
                "stakeable_units": int(stakeable),
                "max_per_cycle_units": int(max_per_cycle),
                "min_stake_usd": float(min_stake_usd),
                "price_usd": price_usd if price_usd is None else float(price_usd),
            }
        )

        if isinstance(recommendation, dict):
            try:
                rec_copy = dict(recommendation)
            except Exception:
                rec_copy = recommendation
            action["recommendation"] = rec_copy
            recommended_units = rec_copy.get("shortfall_units") or rec_copy.get(
                "required_units"
            )
            try:
                rec_units = int(recommended_units)
            except Exception:
                rec_units = None
            if rec_units is not None:
                action["recommendation_units"] = rec_units
                action["recommendation_covered"] = bool(stake_amount >= rec_units)

        if stake_amount <= 0:
            action["reason"] = "below_buffer"
            return action

        stake_value_usd = None
        if price_usd not in (None, 0, 0.0):
            try:
                stake_value_usd = (float(stake_amount) / float(self._vvv_scale())) * (
                    float(price_usd)
                )
                action["stake_value_usd"] = float(stake_value_usd)
            except Exception:
                stake_value_usd = None

        if (
            stake_value_usd is not None
            and min_stake_usd > 0
            and float(stake_value_usd) < float(min_stake_usd)
        ):
            action["reason"] = "below_min_usd"
            return action

        if recommendation and stake_amount > 0:
            try:
                shortfall = recommendation.get("shortfall_units")
                if shortfall is not None:
                    helped = min(int(stake_amount), max(0, int(shortfall)))
                    action["recommendation_help_units"] = helped
                    action["recommendation_helped"] = helped > 0
                else:
                    action["recommendation_helped"] = True
            except Exception:
                action["recommendation_helped"] = True

        nonce_state = self._nonce_state()
        if self._nonce_pending(nonce_state):
            action.update({"reason": "pending_nonce", "nonce": nonce_state})
            return action

        action["attempted"] = True
        try:
            approve_tx = self.staking.approve(int(stake_amount))
            action["approve"] = approve_tx
        except Exception as exc:
            # Check for arithmetic overflow in gas estimation (contract issue)
            if is_stake_estimate_overflow_error(exc):
                signature = stake_estimate_error_signature(exc) or "unknown"
                logger.info(
                    "Idle stake approve skipped (contract arithmetic overflow)",
                    extra={"error_signature": signature},
                )
                action.update(
                    {
                        "reason": "contract_overflow",
                        "overflow_error": str(exc),
                        "overflow_error_signature": signature,
                    }
                )
                return action
            logger.warning(f"Idle stake approve failed: {exc}")
            action.update({"reason": f"approve_error:{exc}"})
            try:
                _emit_event(
                    "staking.idle_stake",
                    {
                        "status": "error",
                        "units": int(stake_amount),
                        "error": f"approve:{exc}",
                    },
                )
            except Exception:
                pass
            return action

        try:
            tx, staked_units, overflow_attempts, overflow_stop_reason = (
                self._stake_with_overflow_backoff(
                    int(stake_amount),
                    price_usd=(
                        None if price_usd in (None, 0, 0.0) else float(price_usd)
                    ),
                    min_usd=float(min_stake_usd) if min_stake_usd else None,
                )
            )
            if overflow_attempts:
                # Keep the public surface stable for tests/ops: only expose units + error.
                action["stake_overflow_attempts"] = [
                    {
                        "units": int(att.get("units", 0) or 0),
                        "error": str(att.get("error", "")),
                    }
                    for att in overflow_attempts
                ]
                action["stake_overflow_retries"] = len(
                    action["stake_overflow_attempts"]
                )
            if overflow_attempts or overflow_stop_reason:
                backoff_raw = os.getenv(
                    "STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT",
                    os.getenv("STAKEMASTER_STAKE_OVERFLOW_BACKOFF_MULT", "0.5"),
                )
                try:
                    action["stake_overflow_backoff_mult"] = float(
                        backoff_raw if backoff_raw is not None else 0.5
                    )
                except Exception:
                    action["stake_overflow_backoff_mult"] = 0.5
            if overflow_stop_reason:
                action["stake_overflow_stop_reason"] = overflow_stop_reason
            action["stake_units_requested"] = int(stake_amount)
            if tx is None:
                if overflow_stop_reason == "below_min_usd":
                    action["reason"] = "below_min_usd"
                    return action
                action.update(
                    {
                        "executed": False,
                        "reason": "contract_overflow",
                        "overflow_error": (
                            overflow_attempts[-1]["error"]
                            if overflow_attempts
                            else "overflow_retry_exhausted"
                        ),
                    }
                )
                return action
            action.update(
                {
                    "executed": True,
                    "tx": tx,
                    "reason": "idle_stake",
                    "staked_units": int(staked_units),
                }
            )
            try:
                _emit_event(
                    "staking.idle_stake",
                    {
                        "status": tx.get("status") if isinstance(tx, dict) else "ok",
                        "units": int(staked_units),
                        "buffer_units": int(buffer_units),
                        "max_per_cycle_units": int(max_per_cycle),
                    },
                )
            except Exception:
                pass
            logger.info(
                "Idle stake executed",
                extra={
                    "units": int(staked_units),
                    "buffer_units": int(buffer_units),
                    "max_per_cycle_units": int(max_per_cycle),
                },
            )
        except Exception as exc:
            logger.warning(f"Idle stake failed: {exc}")
            action.update({"executed": False, "reason": f"stake_error:{exc}"})
            try:
                _emit_event(
                    "staking.idle_stake",
                    {
                        "status": "error",
                        "units": int(stake_amount),
                        "error": str(exc),
                    },
                )
            except Exception:
                pass

        return action

    def _wallet_vvv_balance(self) -> int | None:
        actions = getattr(self.staking, "actions", None)
        if actions is None:
            return None
        erc20 = getattr(actions, "erc20", None)
        if erc20 is None:
            return None
        try:
            from libs.agentkit_ext.agentkit_wallet import get_address

            balance = erc20.functions.balanceOf(get_address()).call()
            return int(balance)
        except Exception as exc:
            try:
                logger.debug(f"VVV balance lookup failed: {exc}")
            except Exception:
                pass
            return None

    def _priority_fee_target(self, w3) -> int:
        env_value = os.getenv("STAKEMASTER_PRIORITY_FEE_WEI")
        candidate: int | None = None
        if env_value and env_value.strip():
            try:
                candidate = int(env_value, 0)
            except Exception:
                try:
                    candidate = int(float(env_value))
                except Exception:
                    candidate = None
        if candidate is None:
            try:
                priority_attr = getattr(w3.eth, "max_priority_fee", None)
                if priority_attr is not None:
                    candidate = int(priority_attr)
            except Exception:
                candidate = None
        if candidate is None:
            try:
                candidate = int(w3.to_wei(1, "gwei"))
            except Exception:
                candidate = 1_000_000_000
        multiplier = 1.5
        bump_env = os.getenv("STAKEMASTER_PRIORITY_FEE_BUMP_MULT")
        if bump_env:
            try:
                mul = float(bump_env)
                if mul > 1.0:
                    multiplier = mul
            except Exception:
                pass
        min_env = os.getenv("STAKEMASTER_PRIORITY_FEE_MIN_WEI")
        min_priority = None
        if min_env and min_env.strip():
            try:
                min_priority = int(min_env, 0)
            except Exception:
                try:
                    min_priority = int(float(min_env))
                except Exception:
                    min_priority = None
        bumped = int(candidate * multiplier)
        if bumped <= candidate:
            bumped = candidate + 1_000_000_000
        if min_priority is not None and bumped < min_priority:
            bumped = int(min_priority)
        return max(candidate, bumped)

    def _build_gas_overrides(self, w3, nonce: int) -> dict[str, int]:
        overrides: dict[str, int] = {"nonce": int(nonce)}

        # Get chain_id for Base-specific handling
        chain_id = None
        try:
            chain_id = w3.eth.chain_id
        except Exception:
            pass

        BASE_CHAIN_ID = 8453
        is_base = chain_id == BASE_CHAIN_ID

        # Base-specific max gas price cap (default 5 gwei)
        base_max_gas_price_wei = None
        try:
            raw = os.getenv("BASE_GAS_PRICE_MAX_WEI")
            if raw:
                base_max_gas_price_wei = int(str(raw), 0)
        except Exception:
            pass
        if base_max_gas_price_wei is None:
            from web3 import Web3

            base_max_gas_price_wei = int(Web3.to_wei(5, "gwei"))  # Default 5 gwei cap

        rpc_url = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL") or "unknown"

        base_fee = None
        try:
            block = w3.eth.get_block("latest")
            base_fee = (
                block.get("baseFeePerGas")
                if isinstance(block, dict)
                else getattr(block, "baseFeePerGas", None)
            )
            if base_fee is not None:
                base_fee = int(base_fee)
        except Exception:
            base_fee = None

        priority_fee = self._priority_fee_target(w3)
        overrides["maxPriorityFeePerGas"] = int(priority_fee)

        if base_fee is not None:
            max_fee = int(base_fee) * 2 + int(priority_fee)
            # On Base, check for anomaly and cap if needed
            if is_base:
                if max_fee > base_max_gas_price_wei * 2:
                    logger.warning(
                        "Base gas price anomaly in StakeMaster: base_fee=%s wei (%.2f gwei), "
                        "priority_fee=%s wei (%.2f gwei), computed max_fee=%s wei (%.2f gwei) > cap=%s wei (%.2f gwei). "
                        "RPC=%s. Capping to max.",
                        base_fee,
                        base_fee / 1e9 if base_fee else 0,
                        priority_fee,
                        priority_fee / 1e9 if priority_fee else 0,
                        max_fee,
                        max_fee / 1e9,
                        base_max_gas_price_wei * 2,
                        (base_max_gas_price_wei * 2) / 1e9,
                        rpc_url,
                    )
                    max_fee = base_max_gas_price_wei * 2
        else:
            max_fee = int(priority_fee) * 2

        overrides["maxFeePerGas"] = max_fee
        gas_limit_env = os.getenv("STAKEMASTER_STAKE_GAS_LIMIT")
        if gas_limit_env:
            try:
                overrides["gas"] = int(gas_limit_env, 0)
            except Exception:
                try:
                    overrides["gas"] = int(float(gas_limit_env))
                except Exception:
                    pass
        overrides["type"] = 2
        return overrides

    def _retry_stake_with_gas_bump(
        self,
        units: int,
        *,
        nonce_state: dict[str, int] | None = None,
    ) -> dict[str, Any] | None:
        actions = getattr(self.staking, "actions", None)
        if actions is None:
            return None
        w3 = getattr(actions, "w3", None)
        if w3 is None:
            return None
        state = nonce_state or self._nonce_state()
        if not state:
            return None
        latest = int(state.get("latest", 0))
        pending = int(state.get("pending", latest))
        nonce = max(latest, pending)
        overrides = self._build_gas_overrides(w3, nonce)
        # Round to stake increment before retry
        rounded_units = self._round_to_stake_increment(int(units))
        return self.staking.stake(rounded_units, gas_overrides=overrides)

    def _nonce_state(self) -> dict[str, int] | None:
        actions = getattr(self.staking, "actions", None)
        if actions is None:
            return None
        w3 = getattr(actions, "w3", None)
        if w3 is None:
            return None
        try:
            from libs.agentkit_ext.agentkit_wallet import get_address

            address = get_address()
            latest = int(w3.eth.get_transaction_count(address, "latest"))
            pending = int(w3.eth.get_transaction_count(address, "pending"))
            return {"latest": latest, "pending": pending}
        except Exception as exc:
            try:
                logger.debug(f"Nonce lookup failed: {exc}")
            except Exception:
                pass
            return None

    @staticmethod
    def _nonce_pending(nonce_state: dict[str, int] | None) -> bool:
        if not nonce_state:
            return False
        try:
            return int(nonce_state.get("pending", 0)) > int(
                nonce_state.get("latest", 0)
            )
        except Exception:
            return False

    # --- heartbeat helpers -------------------------------------------------
    def _kv(self):  # lazy to keep Replit/Redis optional
        if self._kv_store is None:
            try:
                from libs.kv.client import KVStore  # type: ignore

                self._kv_store = KVStore()
            except Exception:
                self._kv_store = None
        return self._kv_store

    def _venice(self):  # lazy import to avoid failing when Venice not configured
        if self._venice_cached is not None:
            return self._venice_cached
        if self.venice_client is not None:
            self._venice_cached = self.venice_client
            return self._venice_cached
        try:
            from libs.venice_sdk.client import VeniceClient  # type: ignore

            self._venice_cached = VeniceClient()
        except Exception as exc:
            logger.debug(f"Heartbeat Venice client unavailable: {exc}")
            self._venice_cached = None
        return self._venice_cached

    def _should_send_heartbeat(self, now: float, *, force: bool) -> bool:
        if (os.getenv("STAKEMASTER_HEARTBEAT_DISABLE") or "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            return False
        interval_hours = float(
            os.getenv(
                "STAKEMASTER_HEARTBEAT_INTERVAL_HOURS",
                str(self.heartbeat_interval_hours),
            )
            or self.heartbeat_interval_hours
        )
        if interval_hours <= 0:
            return False
        store = self._kv()
        if store is None:
            # Without KV we can still best-effort run once per process boot
            return True
        try:
            last_raw = store.get(_HEARTBEAT_KV_KEY)
        except Exception as exc:
            try:
                logger.warning(
                    "Heartbeat KV lookup failed; treating as stale",
                    extra={"error": str(exc)},
                )
            except Exception:
                pass
            _emit_event(
                "staking.heartbeat", {"status": "error", "error": f"kv_read:{exc}"}
            )
            return True
        if force:
            return True
        try:
            last = float(last_raw) if last_raw is not None else 0.0
        except Exception:
            last = 0.0
        return (now - last) >= interval_hours * 3600.0

    def _ensure_heartbeat(self, *, force: bool = False) -> tuple[bool, str | None]:
        now = time.time()
        if not self._should_send_heartbeat(now, force=force):
            return False, None
        client = self._venice()
        if client is None:
            return False, "venice_client_unavailable"
        model = os.getenv("VENICE_HEARTBEAT_MODEL", os.getenv("VENICE_DEFAULT_MODEL"))
        if not model:
            logger.warning(
                "VENICE_HEARTBEAT_MODEL and VENICE_DEFAULT_MODEL not set, using 'qwen3-4b'"
            )
            model = "qwen3-4b"

        prompt = os.getenv(
            "STAKEMASTER_HEARTBEAT_PROMPT",
            "Please respond with a single word 'alive' and maybe a riddle if you feel like it.",
        )
        messages = [
            {
                "role": "system",
                "content": "You are a Venice API heartbeat used to retain active staker status.",
            },
            {"role": "user", "content": prompt},
        ]

        # Log heartbeat attempt with diagnostic info
        base_url = getattr(client.config, "base_url", "unknown")
        api_key_set = bool(getattr(client.config, "api_key", None))
        logger.debug(
            "Heartbeat attempt: model=%s, base_url=%s, api_key_set=%s",
            model,
            base_url,
            api_key_set,
        )

        try:
            response = client.chat_completions(
                messages=messages, model=model, temperature=0.0, max_tokens=8
            )
        except Exception as exc:
            error_type = type(exc).__name__
            error_str = str(exc)
            # Differentiate Venice API issues from transient network errors
            is_venice_api_error = False
            error_category = "network"
            if "404" in error_str or "not found" in error_str.lower():
                is_venice_api_error = True
                error_category = "venice_404"
            elif (
                "401" in error_str
                or "403" in error_str
                or "unauthorized" in error_str.lower()
            ):
                is_venice_api_error = True
                error_category = "venice_auth"
            elif (
                "500" in error_str
                or "502" in error_str
                or "503" in error_str
                or "504" in error_str
            ):
                is_venice_api_error = True
                error_category = "venice_server"
            elif "timeout" in error_str.lower() or "timed out" in error_str.lower():
                error_category = "timeout"
            elif "connection" in error_str.lower() or "refused" in error_str.lower():
                error_category = "connection"

            logger.warning(
                f"Heartbeat request failed: {error_type}",
                extra={
                    "error": error_str,
                    "error_category": error_category,
                    "is_venice_api_error": is_venice_api_error,
                    "forced": force,
                },
            )
            _emit_event(
                "staking.heartbeat",
                {"status": "error", "error": error_str, "category": error_category},
            )
            return False, f"request_failed:{error_category}"

        store = self._kv()
        if store is not None:
            try:
                store.set(_HEARTBEAT_KV_KEY, str(now))
            except Exception as exc:
                logger.debug(f"Failed to persist heartbeat timestamp: {exc}")
        _emit_event(
            "staking.heartbeat",
            {
                "status": "ok",
                "model": model,
                "response_id": (
                    response.get("id") if isinstance(response, dict) else None
                ),
            },
        )
        return True, None
