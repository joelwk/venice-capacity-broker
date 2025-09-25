from __future__ import annotations

from importlib import import_module


def test_diem_service_mint_burn_monkeypatched(monkeypatch):
    calls: list[tuple[str, int]] = []
    lock_calls: list[int] = []
    stake_calls: list[int] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"status": "sent", "action": "mint", "tx_hash": "0xdead"}

        def burn(self, amount: int):
            calls.append(("burn", amount))
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

        def lock_svvv(self, amount: int):
            lock_calls.append(amount)
            return {"status": "locked", "amount": amount}

        def stake_for_api(self, amount: int):
            stake_calls.append(amount)
            return {"status": "sent", "action": "stake", "tx_hash": "0xfeed"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv("DIEM_LOCK_ON_MINT", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1")

    svc_mod = import_module("services.diem.client")

    class _StubAgg:  # minimal stub to avoid env for Dex aggregator
        pass

    svc = svc_mod.DIEMService(aggregator=_StubAgg())
    r1 = svc.mint(123)
    r2 = svc.burn(456)
    r3 = svc.mint_diem(321, lock=False)
    r4 = svc.burn_diem(654)
    r5 = svc.stake_diem_for_api(777)

    assert calls == [
        ("mint", 123),
        ("burn", 456),
        ("mint", 321),
        ("burn", 654),
    ]
    assert lock_calls == [123]
    assert stake_calls == [777]

    assert r1.get("action") == "mint" and r1.get("status") == "sent"
    assert r2.get("action") == "burn" and r2.get("status") == "sent"
    assert r3.get("action") == "mint" and r3.get("status") == "sent"
    assert r4.get("action") == "burn" and r4.get("status") == "sent"
    assert r5.get("action") == "stake" and r5.get("status") == "sent"


def test_trade_slippage_override(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "0xdiem,0xusdc")

    class StubAgg:
        def __init__(self) -> None:
            self.history: list[tuple[str, int]] = []

        def trade_best(self, amount: int, slippage_bps: int, path: list[str]):
            self.history.append(("sell", slippage_bps))
            return {"tx": "0x1"}

        def trade_best_exact_out(self, amount: int, slippage_bps: int, path: list[str]):
            self.history.append(("buy", slippage_bps))
            return {"tx": "0x2"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    svc.trade("sell", 100, slippage_bps=55)
    svc.trade("buy", 200, slippage_bps=77)

    assert agg.history == [("sell", 55), ("buy", 77)]
