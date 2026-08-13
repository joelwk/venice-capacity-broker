"""Tests for DIEM slippage override functionality.

Verifies that agent-provided slippage override is honored by DIEMService
and passed correctly to the aggregator.
"""

from __future__ import annotations

from importlib import import_module

from libs.dex.routes import make_route


def test_diem_slippage_override_agent_override(monkeypatch):
    """Agent passes 700 bps; DIEMService uses override and aggregator receives the same."""
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_SLIPPAGE_OVERRIDE_ENABLE", "1")
    monkeypatch.setenv("DIEM_SLIPPAGE_OVERRIDE_MAX_BPS", "800")

    svc_mod = import_module("services.diem.client")

    class StubAgg:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.providers: list[str] = []

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.calls.append(slippage_bps)
            return {"tx": "0xabc"}

    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)
    simple_route = make_route(["0xusdc", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )

    svc.trade("buy", 100, slippage_bps=50, slippage_override_bps=700)

    assert agg.calls == [700]
