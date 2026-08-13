from __future__ import annotations

import os
from importlib import import_module


def test_mint_burn_dry_run_and_idempotency(monkeypatch):
    # Use fake actions to ensure no chain calls are made when dry_run
    calls = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"tx_hash": "0xmint"}

        def burn(self, amount: int):
            calls.append(("burn", amount))
            return {"tx_hash": "0xburn"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Dry runs do not call actions
    r1 = svc.mint(100, dry_run=True)
    r2 = svc.burn(50, dry_run=True)
    assert r1["status"] == "dry_run" and r2["status"] == "dry_run"
    assert not calls

    # Idempotent key suppresses duplicate execution
    r3 = svc.mint(1, idem_key="abc")
    r4 = svc.mint(1, idem_key="abc")
    assert r3.get("tx_hash") == "0xmint"
    assert r4.get("status") == "skipped"


def test_diem_trade_executes_with_valid_config(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc@3000")
    monkeypatch.setenv("SLIPPAGE_BPS", "80")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "120")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )

    provider_mod = import_module("services.marketdata.provider")
    market = provider_mod.MarketDataProvider()

    class StubAgg:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, list[str]]] = []

        def quote_all(self, amount: int, route: object):
            return [{"provider": "stub"}]

        def trade_best(self, amount: int, slippage_bps: int, route: object):
            tokens = list(getattr(route, "tokens", route))
            self.calls.append((amount, slippage_bps, tokens))
            return {"tx": "0xabc"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg, market_data=market)

    res = svc.trade("sell", 500)

    assert res.get("status") == "sent"
    assert agg.calls
    amount, slip, tokens = agg.calls[0]
    assert amount == 500
    assert slip == 80
    assert tokens[0].lower() == os.getenv("DIEM_TOKEN_ADDRESS").lower()
