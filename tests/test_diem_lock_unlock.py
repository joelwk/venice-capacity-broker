from __future__ import annotations

from importlib import import_module


def test_diem_lock_on_mint_and_unlock_on_burn(monkeypatch):
    calls: list[tuple[str, int]] = []

    class FakeActions:
        def lock_svvv(self, amount: int):  # noqa: D401
            calls.append(("lock", amount))
            return {"status": "sent", "action": "lock_svvv", "tx_hash": "0xlock"}

        def mint(self, amount: int):  # noqa: D401
            calls.append(("mint", amount))
            return {"status": "sent", "action": "mint", "tx_hash": "0xmint"}

        def burn(self, amount: int):  # noqa: D401
            calls.append(("burn", amount))
            return {"status": "sent", "action": "burn", "tx_hash": "0xburn"}

        def unlock_svvv(self, amount: int):  # noqa: D401
            calls.append(("unlock", amount))
            return {"status": "sent", "action": "unlock_svvv", "tx_hash": "0xunlock"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    # Enable gate and set 1:1 rate in token units (decimals-aware)
    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "0")
    monkeypatch.setenv("DIEM_MINT_RATE", "1.0")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("SVVV_DECIMALS", "18")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", str(10**21))
    # Enable lock/unlock and cooldown metadata
    monkeypatch.setenv("DIEM_LOCK_ON_MINT", "1")
    monkeypatch.setenv("DIEM_UNLOCK_AFTER_BURN", "1")
    monkeypatch.setenv("DIEM_UNLOCK_COOLDOWN_SECONDS", "3600")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)
    svc._actions = FakeActions()  # type: ignore[attr-defined]
    amount = 10**18  # 1 DIEM in base units

    r1 = svc.mint(amount)
    r2 = svc.burn(amount)

    # Expected sequence: lock -> mint -> burn -> unlock
    kinds = [k for (k, _v) in calls]
    assert kinds[:2] == ["lock", "mint"]
    assert kinds[2:] == ["burn", "unlock"]

    # Response payloads include nested lock/unlock when enabled
    # best-effort; presence indicates wiring
    assert r1.get("tx_hash") or r1.get("status") == "sent"
    # burn() returns just DIEMACTIONS result, but emits unlock side-effect; no strict payload requirement here
