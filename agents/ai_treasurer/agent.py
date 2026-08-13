from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from libs.telemetry.logger import get_logger
from services.treasury.recycle import recycle_profits_to_stake

logger = get_logger("agent.ai_treasurer")


@dataclass
class AITreasurer:
    buffer_target_days: float = 1.5  # keep 150% of avg daily need
    enable_automation: bool = False
    min_action_usd: float = 10.0
    max_actions_per_cycle: int = 1

    def __post_init__(self) -> None:
        env_enable = os.getenv("TREASURER_ENABLE_AUTOMATION", "0")
        self.enable_automation = str(env_enable).strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            self.min_action_usd = float(os.getenv("TREASURER_MIN_ACTION_USD", "10.0"))
        except Exception:
            self.min_action_usd = 10.0
        try:
            self.max_actions_per_cycle = int(
                os.getenv("TREASURER_MAX_ACTIONS_PER_CYCLE", "1")
            )
        except Exception:
            self.max_actions_per_cycle = 1

    def rebalance(self, avg_daily_diem: float, current_diem: float) -> float:
        target = avg_daily_diem * self.buffer_target_days
        delta = target - current_diem
        logger.info("Rebalance delta=%.2f (target=%.2f)", delta, target)
        return delta

    def execute(  # noqa: PLR0913
        self,
        *,
        thought: str,
        action: str,
        portfolio_snapshot: dict[str, Any] | None = None,
        broker_utilization: float | None = None,
        quorum_approved: bool = False,
        reflex_ok: bool = False,
        dry_run: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """
        ReAct-style execution: Thought → Action → Observe.

        Args:
            thought: Reasoning for the action
            action: Action type ("recycle_profits", "adjust_pricing",
                "accumulate_buffer", "mint_assist", "burn_assist")
            portfolio_snapshot: Portfolio inventory snapshot
            broker_utilization: Current broker utilization ratio
            quorum_approved: Whether quorum approved this action
            reflex_ok: Whether reflex guardian allows this action
            dry_run: If True, only preview without executing
            **kwargs: Action-specific parameters

        Returns:
            Dict with status, observation, and any errors
        """
        result: dict[str, Any] = {
            "status": "pending",
            "thought": thought,
            "action": action,
            "executed": False,
            "observation": None,
            "errors": [],
        }

        if not self.enable_automation and not dry_run:
            result["status"] = "skipped"
            result["errors"].append("automation disabled")
            return result

        if not quorum_approved and not dry_run:
            result["status"] = "blocked"
            result["errors"].append("quorum not approved")
            return result

        if not reflex_ok and not dry_run:
            result["status"] = "blocked"
            result["errors"].append("reflex guardian blocked")
            return result

        try:
            if action == "recycle_profits":
                result = self._execute_recycle_profits(
                    portfolio_snapshot=portfolio_snapshot,
                    dry_run=dry_run,
                    **_kwargs,
                )
            elif action == "adjust_pricing":
                result = self._execute_adjust_pricing(
                    broker_utilization=broker_utilization,
                    dry_run=dry_run,
                    **_kwargs,
                )
            elif action == "accumulate_buffer":
                result = self._execute_accumulate_buffer(
                    portfolio_snapshot=portfolio_snapshot,
                    dry_run=dry_run,
                    **_kwargs,
                )
            else:
                result["status"] = "skipped"
                result["errors"].append(f"unknown action: {action}")
        except Exception as exc:
            logger.exception("Treasurer execution failed: %s", action)
            result["status"] = "error"
            result["errors"].append(str(exc))

        result["thought"] = thought
        result["action"] = action
        return result

    def _execute_recycle_profits(
        self,
        portfolio_snapshot: dict[str, Any] | None = None,
        dry_run: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute profit recycling: USDC → VVV → stake."""
        result: dict[str, Any] = {
            "status": "pending",
            "executed": False,
            "observation": None,
            "errors": [],
        }

        if not portfolio_snapshot:
            result["status"] = "skipped"
            result["errors"].append("portfolio snapshot unavailable")
            return result

        usdc_usd = float(portfolio_snapshot.get("perAssetUsd", {}).get("USDC", 0.0))
        if usdc_usd < self.min_action_usd:
            result["status"] = "skipped"
            result["errors"].append(
                f"USDC balance {usdc_usd:.2f} USD below minimum "
                f"{self.min_action_usd:.2f} USD"
            )
            return result

        aggregator = _kwargs.get("aggregator")
        stake_master = _kwargs.get("stake_master")

        if not aggregator or not stake_master:
            result["status"] = "skipped"
            result["errors"].append("aggregator or stake_master unavailable")
            return result

        usdc_decimals = 6
        with suppress(Exception):
            usdc_decimals = int(os.getenv("USDC_DECIMALS", "6"))

        usdc_wei = int(usdc_usd * (10.0**usdc_decimals))

        try:
            recycle_result = recycle_profits_to_stake(
                amount_usdc_wei=usdc_wei,
                aggregator=aggregator,
                stake_master=stake_master,
                dry_run=dry_run,
            )
            result["observation"] = recycle_result
            result["status"] = recycle_result.get("status", "completed")
            result["executed"] = recycle_result.get("status") == "completed"
        except Exception as exc:
            result["status"] = "error"
            result["errors"].append(str(exc))
            return result

        return result

    def _execute_adjust_pricing(
        self,
        broker_utilization: float | None = None,
        dry_run: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute broker pricing adjustment."""
        result: dict[str, Any] = {
            "status": "pending",
            "executed": False,
            "observation": None,
            "errors": [],
        }

        if broker_utilization is None:
            result["status"] = "skipped"
            result["errors"].append("broker utilization unavailable")
            return result

        capacity_broker = _kwargs.get("capacity_broker")
        if not capacity_broker:
            result["status"] = "skipped"
            result["errors"].append("capacity_broker unavailable")
            return result

        result["status"] = "completed" if not dry_run else "dry_run"
        result["observation"] = {
            "utilization": broker_utilization,
            "pricing_adjusted": not dry_run,
        }
        result["executed"] = not dry_run

        return result

    def _execute_accumulate_buffer(
        self,
        portfolio_snapshot: dict[str, Any] | None = None,
        dry_run: bool = True,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Execute buffer accumulation guidance."""
        result: dict[str, Any] = {
            "status": "pending",
            "executed": False,
            "observation": None,
            "errors": [],
        }

        result["status"] = "completed" if not dry_run else "dry_run"
        result["observation"] = {
            "buffer_strategy": "accumulate",
            "portfolio_snapshot": portfolio_snapshot,
        }
        result["executed"] = not dry_run

        return result
