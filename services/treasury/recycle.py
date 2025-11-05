"""Treasury profit recycling service for converting USDC profits to VVV staking."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from libs.dex.routes import RoutePlan, make_route
from libs.telemetry.logger import get_logger

try:
    from libs.telemetry.metrics import inc as _metrics_inc
except Exception:  # noqa: BLE001
    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return

logger = get_logger("treasury.recycle")


def recycle_profits_to_stake(
    amount_usdc_wei: int,
    aggregator: Any,
    stake_master: Any,
    *,
    dry_run: bool = True,
    slippage_bps: Optional[int] = None,
    min_stake_usd: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Swap USDC profits to VVV and stake the resulting tokens.

    Args:
        amount_usdc_wei: Amount of USDC in base units (6 decimals)
        aggregator: DEX aggregator instance
        stake_master: StakeMaster agent instance
        dry_run: If True, only preview without executing
        slippage_bps: Max slippage in basis points (default from env or 150)
        min_stake_usd: Minimum USD value to stake (default from env or 10.0)

    Returns:
        Dict with status, swap_result, stake_result, and any errors
    """
    result: Dict[str, Any] = {
        "status": "pending",
        "swap_result": None,
        "stake_result": None,
        "errors": [],
    }

    try:
        slippage = slippage_bps if slippage_bps is not None else int(os.getenv("RISK_MAX_SLIPPAGE_BPS", "150"))
        min_usd = min_stake_usd if min_stake_usd is not None else float(os.getenv("STAKEMASTER_MIN_STAKE_USD", "10.0"))

        if amount_usdc_wei <= 0:
            result["status"] = "skipped"
            result["errors"].append("amount_usdc_wei must be positive")
            return result

        quote_token = os.getenv("QUOTE_TOKEN_ADDRESS") or os.getenv("USDC_TOKEN_ADDRESS")
        vvv_token = os.getenv("VVV_TOKEN_ADDRESS")
        
        if not quote_token or not vvv_token:
            result["status"] = "error"
            result["errors"].append("QUOTE_TOKEN_ADDRESS and VVV_TOKEN_ADDRESS must be set")
            return result

        usdc_decimals = 6
        vvv_decimals = 18

        try:
            usdc_decimals = int(os.getenv("USDC_DECIMALS", "6"))
        except Exception:
            pass

        try:
            vvv_decimals = int(os.getenv("VVV_DECIMALS", "18"))
        except Exception:
            pass

        amount_usdc = float(amount_usdc_wei) / (10.0 ** usdc_decimals)

        if amount_usdc < min_usd:
            result["status"] = "skipped"
            result["errors"].append(f"amount {amount_usdc:.2f} USD below minimum {min_usd:.2f} USD")
            return result

        route = make_route([quote_token, vvv_token])
        if not route:
            result["status"] = "error"
            result["errors"].append("failed to build swap route")
            return result

        quote = aggregator.best_quote(amount_usdc_wei, route)
        if not quote:
            result["status"] = "error"
            result["errors"].append("no quote available from aggregator")
            return result

        min_amount_out = quote.amount_out * (10_000 - slippage) // 10_000
        vvv_out = float(quote.amount_out) / (10.0 ** vvv_decimals)

        if dry_run:
            result["status"] = "dry_run"
            result["swap_result"] = {
                "preview": True,
                "usdc_in": amount_usdc,
                "vvv_out": vvv_out,
                "slippage_bps": slippage,
                "route": list(route.tokens) if hasattr(route, "tokens") else None,
            }
            result["stake_result"] = {
                "preview": True,
                "vvv_units": int(quote.amount_out),
                "vvv_usd": vvv_out,
            }
            return result

        swap_result = aggregator.trade_best(amount_usdc_wei, slippage, route)
        
        result["swap_result"] = {
            "preview": False,
            "tx_hash": swap_result.get("tx_hash"),
            "usdc_in": amount_usdc,
            "vvv_out": vvv_out,
            "route": list(route.tokens) if hasattr(route, "tokens") else None,
        }

        if stake_master and hasattr(stake_master, "stake_vvv"):
            stake_result = stake_master.stake_vvv(int(quote.amount_out), reason="profit_recycling")
            result["stake_result"] = stake_result
        else:
            result["stake_result"] = {
                "status": "skipped",
                "reason": "stake_vvv not available",
            }

        result["status"] = "completed"
        try:
            _metrics_inc("treasury_recycle_total", labels={"status": "completed"})
        except Exception:
            pass

    except Exception as exc:  # noqa: BLE001
        logger.exception("profit recycling failed")
        result["status"] = "error"
        result["errors"].append(str(exc))
        try:
            _metrics_inc("treasury_recycle_total", labels={"status": "error"})
        except Exception:
            pass

    return result


__all__ = ["recycle_profits_to_stake"]

