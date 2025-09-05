from __future__ import annotations

from importlib import import_module


def test_diem_service_mint_burn_monkeypatched(monkeypatch):
    # Monkeypatch DIEMACTIONS before importing DIEMService so constructor uses the fake
    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):  # noqa: D401
            calls.append(("mint", amount))
            return {"status": "sent", "action": "mint", "tx_hash": "0xdead"}

        def burn(self, amount: int):  # noqa: D401
            calls.append(("burn", amount))
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    svc_mod = import_module("services.diem.client")

    class _StubAgg:  # minimal stub to avoid env for Dex aggregator
        pass

    svc = svc_mod.DIEMService(aggregator=_StubAgg())
    r1 = svc.mint(123)
    r2 = svc.burn(456)

    assert calls == [("mint", 123), ("burn", 456)]
    assert r1.get("action") == "mint" and r1.get("status") == "sent"
    assert r2.get("action") == "burn" and r2.get("status") == "sent"

