from __future__ import annotations

from typing import Any, Dict, Callable

from libs.telemetry.logger import get_logger
import os

from .nodes import (
    wallet_node,
    stake_master_node,
    diem_controller_node,
    broker_router_node,
)


logger = get_logger("langgraph.graph")


def _maybe_trace(name: str, fn: Callable[[Dict[str, Any]], Dict[str, Any]]):
    try:
        enabled = (os.getenv("LANGCHAIN_TRACING_V2") or "false").strip().lower() in {"1", "true", "yes"}
        if not enabled:
            return fn
        from langsmith import traceable  # type: ignore

        return traceable(name=name)(fn)
    except Exception:
        return fn


def build_minimal_graph() -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    """
    Build a minimal LangGraph if the library is available; otherwise return a
    simple sequential runner that composes the four nodes.
    """
    try:
        from typing import TypedDict
        from langgraph.graph import StateGraph, START, END  # type: ignore

        class State(TypedDict, total=False):
            wallet: dict
            staking: dict
            decision: dict
            broker: dict

        g = StateGraph(State)  # type: ignore[misc]
        g.add_node("wallet", wallet_node)
        g.add_node("stake", stake_master_node)
        g.add_node("diem", diem_controller_node)
        g.add_node("broker", broker_router_node)

        g.add_edge(START, "wallet")
        g.add_edge("wallet", "stake")
        g.add_edge("stake", "diem")
        g.add_edge("diem", "broker")
        g.add_edge("broker", END)

        app = g.compile()

        def run(state: Dict[str, Any]) -> Dict[str, Any]:
            logger.info("running langgraph pipeline")
            return app.invoke(state)  # type: ignore[attr-defined]

        return _maybe_trace("vvv.langgraph.run", run)

    except Exception as e:  # noqa: BLE001
        logger.info("LangGraph not installed; using sequential fallback (%s)", e)

        def run(state: Dict[str, Any]) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            out.update(wallet_node(state))
            out.update(stake_master_node({**state, **out}))
            out.update(diem_controller_node({**state, **out}))
            out.update(broker_router_node({**state, **out}))
            return out

        return _maybe_trace("vvv.langgraph.run", run)
