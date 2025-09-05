from __future__ import annotations

from importlib import import_module


def test_mint_burn_dry_run_and_idempotency(monkeypatch):
    # Use fake actions to ensure no chain calls are made when dry_run
    calls = []

    class FakeActions:
        def mint(self, amount: int):  # noqa: D401
            calls.append(("mint", amount))
            return {"tx_hash": "0xmint"}

        def burn(self, amount: int):  # noqa: D401
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

