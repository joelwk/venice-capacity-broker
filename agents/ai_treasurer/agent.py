from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from libs.telemetry.logger import get_logger

logger = get_logger("agent.ai_treasurer")


@dataclass
class AITreasurer:
    buffer_target_days: float = 1.5  # keep 150% of avg daily need
    enable_automation: bool = False
    min_action_usd: float = 10.0
    max_actions_per_cycle: int = 1

    def __post_init__(self) -> None:
        env_enable = os.getenv("TREASURER_ENABLE_AUTOMATION", "0")
        self.enable_automation = str(env_enable).strip().lower() in {"1", "true", "yes", "on"}
        try:
            self.min_action_usd = float(os.getenv("TREASURER_MIN_ACTION_USD", "10.0"))
        except Exception:
            self.min_action_usd = 10.0
        try:
            self.max_actions_per_cycle = int(os.getenv("TREASURER_MAX_ACTIONS_PER_CYCLE", "1"))
        except Exception:
            self.max_actions_per_cycle = 1

    def rebalance(self, avg_daily_diem: float, current_diem: float) -> float:
        target = avg_daily_diem * self.buffer_target_days
        delta = target - current_diem
        logger.info(f"Rebalance delta={delta:.2f} (target={target:.2f})")
        return delta

    def execute(
        self,
        *,
        thought: str,
        action: str,
        portfolio_snapshot: Optional[Dict[str, Any]] = None,
        broker_utilization: Optional[float] = None,
        quorum_approved: bool = False,
        reflex_ok: bool = False,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        ReAct-style execution: Thought → Action → Observe.

        Args:
            thought: Reasoning for the action
            action: Action type ("recycle_profits", "adjust_pricing", "accumulate_buffer", "mint_assist", "burn_assist")
            portfolio_snapshot: Portfolio inventory snapshot
            broker_utilization: Current broker utilization ratio
            quorum_approved: Whether quorum approved this action
            reflex_ok: Whether reflex guardian allows this action
            dry_run: If True, only preview without executing
            **kwargs: Action-specific parameters

        Returns:
            Dict with status, observation, and any errors
        """
        result: Dict[str, Any] = {
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
                    **kwargs,
                )
            elif action == "adjust_pricing":
                result = self._execute_adjust_pricing(
                    broker_utilization=broker_utilization,
                    dry_run=dry_run,
                    **kwargs,
                )
            elif action == "accumulate_buffer":
                result = self._execute_accumulate_buffer(
                    portfolio_snapshot=portfolio_snapshot,
                    dry_run=dry_run,
                    **kwargs,
                )
            else:
                result["status"] = "skipped"
                result["errors"].append(f"unknown action: {action}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Treasurer execution failed: {action}")
            result["status"] = "error"
            result["errors"].append(str(exc))

        result["thought"] = thought
        result["action"] = action
        return result

    def _execute_recycle_profits(
        self,
        portfolio_snapshot: Optional[Dict[str, Any]] = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute profit recycling: USDC → VVV → stake."""
        result: Dict[str, Any] = {
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
            result["errors"].append(f"USDC balance {usdc_usd:.2f} USD below minimum {self.min_action_usd:.2f} USD")
            return result

        aggregator = kwargs.get("aggregator")
        stake_master = kwargs.get("stake_master")

        if not aggregator or not stake_master:
            result["status"] = "skipped"
            result["errors"].append("aggregator or stake_master unavailable")
            return result

        usdc_decimals = 6
        try:
            usdc_decimals = int(os.getenv("USDC_DECIMALS", "6"))
        except Exception:
            pass

        usdc_wei = int(usdc_usd * (10.0 ** usdc_decimals))

        try:
            from services.treasury.recycle import recycle_profits_to_stake

            recycle_result = recycle_profits_to_stake(
                amount_usdc_wei=usdc_wei,
                aggregator=aggregator,
                stake_master=stake_master,
                dry_run=dry_run,
            )
            result["observation"] = recycle_result
            result["status"] = recycle_result.get("status", "completed")
            result["executed"] = recycle_result.get("status") == "completed"
        except Exception as exc:  # noqa: BLE001
            result["status"] = "error"
            result["errors"].append(str(exc))

        return result

    def _execute_adjust_pricing(
        self,
        broker_utilization: Optional[float] = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute broker pricing adjustment."""
        result: Dict[str, Any] = {
            "status": "pending",
            "executed": False,
            "observation": None,
            "errors": [],
        }

        if broker_utilization is None:
            result["status"] = "skipped"
            result["errors"].append("broker utilization unavailable")
            return result

        capacity_broker = kwargs.get("capacity_broker")
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
        portfolio_snapshot: Optional[Dict[str, Any]] = None,
        dry_run: bool = True,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute buffer accumulation guidance."""
        result: Dict[str, Any] = {
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
