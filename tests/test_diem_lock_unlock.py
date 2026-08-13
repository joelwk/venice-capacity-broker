from __future__ import annotations

from importlib import import_module


def test_diem_mint_burn_skip_manual_lock_unlock(monkeypatch):
    calls: list[tuple[str, int]] = []
    lock_unlock: list[tuple[str, int]] = []

    class FakeActions:
        def lock_svvv(self, amount: int):
            lock_unlock.append(("lock", amount))
            return {"status": "sent", "action": "lock_svvv"}

        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"status": "sent", "action": "mint", "tx_hash": "0xmint"}

        def burn(self, amount: int):
            calls.append(("burn", amount))
            return {"status": "sent", "action": "burn", "tx_hash": "0xburn"}

        def unlock_svvv(self, amount: int):
            lock_unlock.append(("unlock", amount))
            return {"status": "sent", "action": "unlock_svvv"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    # Set rate/availability so mint pre-flight passes without touching lock/unlock
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1")
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", str(10**21))
    monkeypatch.setenv("DIEM_LOCK_ON_MINT", "1")
    monkeypatch.setenv("DIEM_UNLOCK_AFTER_BURN", "1")

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_locked_svvv_for_wallet",
        lambda self: 10**21,
        raising=True,
    )
    svc = svc_mod.DIEMService(aggregator=None)
    svc._actions = FakeActions()  # type: ignore[attr-defined]
    amount = 10**18  # 1 DIEM in base units

    r1 = svc.mint(amount)
    r2 = svc.burn(amount)

    assert calls == [("mint", amount), ("burn", amount)]
    assert lock_unlock == []
    assert r1.get("action") == "mint" and r1.get("status") == "sent"
    assert r2.get("action") == "burn" and r2.get("status") == "sent"
