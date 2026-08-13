from __future__ import annotations

import os
from typing import Any

from libs.telemetry.logger import get_logger
from libs.telemetry.tracing import annotate_span

logger = get_logger("langgraph.nodes")


def _traceable(name: str):
    try:
        enabled = (os.getenv("LANGCHAIN_TRACING_V2") or "false").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        if not enabled:
            return lambda f: f
        from langsmith import traceable  # type: ignore

        return traceable(name=name)
    except Exception:
        return lambda f: f


def debug_premium_span(env: dict, inputs: dict, fair: float, premium: float) -> None:
    """Standardize DIEM premium rationale spans for docs and observability.

    Attaches a LangSmith child span with environment, inputs and computed
    fair value/premium for the current decision.
    """
    try:
        attrs = {
            "DIEM_FAIR_ALPHA": env.get("DIEM_FAIR_ALPHA"),
            "DIEM_PREMIUM_THRESHOLD": env.get("DIEM_PREMIUM_THRESHOLD"),
            "inputs": inputs,
            "fair_value": fair,
            "premium": premium,
        }
        annotate_span(attrs, name="vvv.node.diem_premium.debug")
    except Exception:
        return


@_traceable("vvv.node.wallet")
def wallet_node(state: dict[str, Any]) -> dict[str, Any]:
    """Ensure wallet context is present using AgentKit provider if available."""
    try:
        import os

        from services.wallet.provider import get_default_provider

        provider = get_default_provider()
        network = os.getenv("NETWORK_ID") or os.getenv("BASE_CHAIN_ID", "8453")
        wallet = {"status": "ready", "address": provider.address, "network": network}
    except Exception as e:
        wallet = {"status": "unavailable", "error": str(e)}
    logger.debug("wallet_node: %s", wallet)
    return {"wallet": wallet}


@_traceable("vvv.node.stake_master")
def stake_master_node(state: dict[str, Any]) -> dict[str, Any]:
    """Read staking status via StakingService; safe read-only by default."""
    try:
        from libs.agentkit_ext.actions import VVVActions
        from services.staking.client import StakingService

        staking_svc = StakingService(VVVActions())
        status = staking_svc.status()
        staking = {"heartbeat": "ok", "status": status}
    except Exception as e:
        staking = {"heartbeat": "degraded", "error": str(e)}
    logger.debug("stake_master_node: %s", staking)
    return {"staking": staking}


@_traceable("vvv.node.diem_controller")
def diem_controller_node(state: dict[str, Any]) -> dict[str, Any]:
    """Observe DIEM prices/signals and compute a simple policy decision.

    Env controls:
    - DIEM_FAIR_ALPHA: float, default 0.2 (annualized alpha for fair value helper)
    - DIEM_PREMIUM_THRESHOLD: float, default 1.05 (px/fair_day ratio to trigger mint_sell)
    """
    try:
        from libs.pricing.diem import fair_value_per_diem
        from services.marketdata.provider import MarketDataProvider

        md = MarketDataProvider()
        px = float(md.prices(["DIEM"]).get("DIEM", 1.0))
        vvv_price = float(md.prices(["VVV"]).get("VVV", 1.0))
        alpha = float((os.getenv("DIEM_FAIR_ALPHA") or "0.2").strip() or 0.2)
        threshold = float(
            (os.getenv("DIEM_PREMIUM_THRESHOLD") or "1.05").strip() or 1.05
        )
        # Determine if on-chain liquidity exists
        has_onchain_liquidity = True
        try:
            price_health = md.price_health("DIEM")
            source = price_health.get("source", "")
            if source in ("bridge_vvv", "external_reference"):
                has_onchain_liquidity = False
        except Exception:
            pass

        fair_data = fair_value_per_diem(
            vvv_price=vvv_price,
            mint_rate=1.0,
            emissions_penalty=0.20,
            utilization_current=None,
            utilization_trend=None,
            circulating_supply=None,
            target_supply=38_000,
            discount_rate_apy=0.15,
            growth_rate_apy=0.05,
            historical_ratio=None,
            has_onchain_liquidity=has_onchain_liquidity,
            market_price=px,
        )
        if isinstance(fair_data, dict):
            fair_value = float(fair_data.get("fair_value", 0.0))
        else:
            fair_value = float(fair_data)
        premium = px / fair_value if fair_value > 0 else 0.0
        action = "mint_sell" if premium >= threshold else "hold"
        rationale = {
            "price": px,
            "fair_value": fair_value,
            "alpha": alpha,
            "vvv_price": vvv_price,
            "premium": premium,
            "threshold": threshold,
            "decision": action,
        }
        decision = {
            "action": action,
            "price": px,
            "fair_value": fair_value,
            "premium": premium,
        }
        try:
            annotate_span(rationale, name="vvv.node.diem_controller.attrs")
            debug_premium_span(
                {
                    "DIEM_FAIR_ALPHA": os.getenv("DIEM_FAIR_ALPHA"),
                    "DIEM_PREMIUM_THRESHOLD": os.getenv("DIEM_PREMIUM_THRESHOLD"),
                },
                {"price": px, "vvv_price": vvv_price},
                fair_value,
                premium,
            )
        except Exception:
            pass
        logger.info("diem_controller_rationale: %s", rationale)
    except Exception as e:
        decision = {"action": "hold", "error": str(e)}
    logger.debug("diem_controller_node: %s", decision)
    return {"decision": decision}


@_traceable("vvv.node.broker_router")
def broker_router_node(state: dict[str, Any]) -> dict[str, Any]:
    """Optionally route a chat call via Venice if broker_request is provided.

    Expects state["broker_request"] = {"messages": [...], "model": optional}
    If absent, no-op.
    """
    req = state.get("broker_request")
    if not req:
        routed = {"routed": False}
        logger.debug("broker_router_node: %s", routed)
        return {"broker": routed}
    try:
        from libs.venice_sdk.client import VeniceClient

        vc = VeniceClient()
        messages = req.get("messages") or []
        model = req.get("model")
        try:
            annotate_span(
                {
                    "tenantId": state.get("tenant_id") or state.get("tenantId"),
                    "windowSeconds": state.get("windowSeconds"),
                    "maxRequests": state.get("maxRequests"),
                    "model": model,
                },
                name="vvv.node.broker_router.attrs",
            )
        except Exception:
            pass
        res = vc.chat_completions(messages=messages, model=model)
        routed = {"routed": True, "response": res}
    except Exception as e:
        routed = {"routed": False, "error": str(e)}
    logger.debug("broker_router_node: %s", routed)
    return {"broker": routed}
