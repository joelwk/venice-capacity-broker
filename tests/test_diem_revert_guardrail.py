"""Tests for DIEM route revert guardrail.

Verifies that routes are muted after N reverts and skip execution
until TTL expires.
"""

from __future__ import annotations

import os
from importlib import import_module

import pytest

from libs.dex.routes import make_route


def test_route_revert_guardrail_mutes_and_expires(monkeypatch):
    """After N reverts, route is muted and subsequent cycles skip execution until TTL expires."""
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_THRESHOLD", "2")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_TTL_SECONDS", "3600")

    svc_mod = import_module("services.diem.client")

    class RevertingAgg:
        def __init__(self) -> None:
            self.calls = 0
            self.providers: list[str] = []

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.calls += 1
            raise RuntimeError("execution reverted: no data")

    agg = RevertingAgg()
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

    # First two reverts should be recorded
    for _ in range(2):
        with pytest.raises(RuntimeError):
            svc.trade("buy", 100, slippage_bps=100)

    assert agg.calls == 2
    assert svc._is_route_muted(simple_route) is True

    # Third attempt should be skipped (route is muted)
    with pytest.raises(RuntimeError):
        svc.trade("buy", 120, slippage_bps=90)

    assert agg.calls == 2

    # Expire the mute by adjusting timestamp
    route_key = svc._route_key(simple_route)
    count, first_ts = svc._route_revert_counts[route_key]
    ttl = float(os.getenv("DIEM_ROUTE_REVERT_BAN_TTL_SECONDS", "3600") or 3600)
    svc._route_revert_counts[route_key] = (count, first_ts - ttl - 5)

    # After TTL expiry, route should be retried
    with pytest.raises(RuntimeError):
        svc.trade("buy", 130, slippage_bps=80)

    assert agg.calls == 3
