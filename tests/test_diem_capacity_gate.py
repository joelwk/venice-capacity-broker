from __future__ import annotations

import os
from importlib import import_module


def test_diem_capacity_gate_denies_and_skips_actions(monkeypatch):
    calls: list[str] = []

    class FakeActions:
        def mint(self, amount: int):  # noqa: D401
            calls.append("mint")
            return {"tx_hash": "0xmint"}

        def burn(self, amount: int):  # noqa: D401
            calls.append("burn")
            return {"tx_hash": "0xburn"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    # Enable gate and set simple 1:1 rate (tokens)
    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE", "1.0")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    # Available < required => deny
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", "100")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)
    r = svc.mint(200)
    assert r.get("status") == "denied"
    assert r.get("reason") == "insufficient_capacity"
    assert not calls, "actions should not be called when gate denies"


def test_diem_capacity_gate_allows_when_sufficient(monkeypatch):
    calls: list[str] = []

    class FakeActions:
        def mint(self, amount: int):  # noqa: D401
            calls.append("mint")
            return {"status": "sent", "action": "mint", "tx_hash": "0xok"}

        def burn(self, amount: int):  # noqa: D401
            calls.append("burn")
            return {"status": "sent", "action": "burn", "tx_hash": "0xb"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    # Enable gate; available >> required
    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE", "1.0")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", "1000000000000000000000")  # 1e21

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)
    r = svc.mint(200)
    assert r.get("tx_hash") == "0xok"
    assert calls == ["mint"]

