from __future__ import annotations

from importlib import import_module


def test_diem_service_buy_uses_aggregator_if_available(monkeypatch):
    svc_mod = import_module("services.diem.client")

    # Fake aggregator with exact-out
    class FakeAgg:
        def trade_best_exact_out(self, amount_out, max_in_bps, path):  # noqa: ANN001
            return {"provider": "fake", "tx_hash": "0xagg"}

        def quote_all_exact_out(self, amount_out, path):  # noqa: ANN001
            return []

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    res = svc.trade("buy", 123)
    assert res.get("tx_hash") == "0xagg"


def test_diem_service_buy_falls_back_to_actions(monkeypatch):
    calls = []

    class FakeActions:
        def trade(self, side: str, amount: int):  # noqa: D401
            calls.append((side, amount))
            return {"provider": "actions", "tx_hash": "0xact"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    svc_mod = import_module("services.diem.client")

    class NoAgg:
        pass

    svc = svc_mod.DIEMService(aggregator=NoAgg())
    res = svc.trade("buy", 555)
    assert calls == [("buy", 555)]
    assert res.get("tx_hash") == "0xact"

