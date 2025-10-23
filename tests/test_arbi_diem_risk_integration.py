from __future__ import annotations

import os
from importlib import import_module

from libs.dex.routes import make_route


def test_arbi_diem_risk_limits_mint_size(monkeypatch):
    # Arrange fake DIEM actions to capture units
    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"tx_hash": "0xabc"}

        def burn(self, amount: int):  # unused here
            return {"tx_hash": "0xdef"}

        def trade(self, side: str, amount: int):
            calls.append(("trade", amount))
            return {"tx_hash": "0xghi"}

    # Monkeypatch DIEMACTIONS used by DIEMService
    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    pricing_mod = import_module("libs.pricing.diem")
    qmod = import_module("libs.dex.providers")

    # Make fair/day = 1.0 so a price >1.05 triggers mint
    monkeypatch.setattr(pricing_mod, "fair_value_per_diem", lambda alpha: 365.0, raising=True)

    class FakeAgg:
        def best_quote(self, amount_in, route):  # noqa: ANN001
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in * 2),
                route=route,
            )

        def trade_best(self, amount_in, slippage_bps, route):  # noqa: ANN001
            calls.append(("trade", int(amount_in)))
            return {"provider": "stub", "tx_hash": "0xtrade"}

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = make_route(route_tokens)

    def fake_trade_routes(self):  # noqa: ANN001
        return [route_plan]

    def fake_path_from_env(self):  # noqa: ANN001
        return list(route_plan.tokens)

    monkeypatch.setattr(svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True)
    monkeypatch.setattr(svc_mod.DIEMService, "_path_from_env", fake_path_from_env, raising=True)

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    svc._actions = FakeActions()  # type: ignore[attr-defined]

    # Configure policy to limit to 50 USD per trade and set decimals=18
    os.environ["DIEM_ENABLE_SVVV_GATE"] = "0"
    os.environ["RISK_MAX_DIEM_TRADE_USD"] = "50"
    os.environ["DIEM_DECIMALS"] = "18"
    risk = risk_mod.RiskPolicy.from_env()

    class DummyMarket:
        def reserve_cap_units(self, path, take_bps=None):  # noqa: ANN001
            return None

    monkeypatch.setattr(arbi_mod.ArbiDiem, "_market_provider", lambda self: DummyMarket(), raising=False)

    # Mint desire is very large, but price is $2/DIEM, so expect ~25 DIEM worth in units
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    did = arbi.evaluate_and_maybe_mint(market_price=2.0, mint_rate=1.0, desired_units=10**24)
    assert did is True
    assert svc._last_mint and svc._last_mint.get("status") != "denied"
    minted = int(svc._last_mint.get("amount", 0))
    assert minted > 0
    trade_values = [v for k, v in calls if k == "trade"]
    traded = trade_values[0] if trade_values else minted
    usd = risk.usd_from_units(minted, 2.0)
    assert 45.0 <= usd <= 50.0
    assert minted == traded


def test_arbi_diem_env_premium_threshold(monkeypatch):
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.10")
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "100")
    monkeypatch.setenv("DIEM_DECIMALS", "18")

    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"tx_hash": "0xmint"}

        def burn(self, amount: int):
            return {"tx_hash": "0xburn"}

        def trade(self, side: str, amount: int):
            calls.append((side, amount))
            return {"tx_hash": "0xtrade"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    pricing_mod = import_module("libs.pricing.diem")
    monkeypatch.setattr(pricing_mod, "fair_value_per_diem", lambda alpha: 365.0, raising=True)

    qmod = import_module("libs.dex.providers")
    route_mod = import_module("libs.dex.routes")

    class FakeAgg:
        def best_quote(self, amount_in, route):  # noqa: ANN001
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in * 2),
                route=route,
            )

        def trade_best(self, amount_in, slippage_bps, route):  # noqa: ANN001
            return {"provider": "stub", "tx_hash": "0xtrade"}

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = route_mod.make_route(route_tokens)

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(svc_mod.DIEMService, "trade_routes", lambda self: [route_plan], raising=True)
    monkeypatch.setattr(svc_mod.DIEMService, "_path_from_env", lambda self: list(route_plan.tokens), raising=True)

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    svc._actions = FakeActions()  # type: ignore[attr-defined]

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi_mod = import_module("agents.arbi_diem.agent")
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    did_premium_low = arbi.evaluate_and_maybe_mint(market_price=1.08, mint_rate=1.0, desired_units=10**21)
    assert did_premium_low is False
    assert not calls

    did_premium_high = arbi.evaluate_and_maybe_mint(market_price=1.12, mint_rate=1.0, desired_units=10**21)
    assert did_premium_high is True
    assert any(name == "mint" for name, _ in calls)
