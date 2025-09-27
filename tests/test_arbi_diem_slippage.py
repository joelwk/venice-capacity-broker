from __future__ import annotations

from importlib import import_module


def test_arbi_diem_rejects_on_slippage(monkeypatch):
    # Arrange DIEMService with aggregator best_quote returning poor price
    qmod = import_module("libs.dex.providers")

    class FakeQuote(qmod.Quote):  # type: ignore[type-arg]
        pass

    class FakeAgg:
        def best_quote(self, amount_in, path):  # noqa: ANN001
            # amount_out implies exec price well below market
            return qmod.Quote(provider="uniswap_v2", amount_in=int(amount_in), amount_out=int(amount_in // 2), path=path)  # type: ignore[arg-type]

    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")

    # Patch DIEMService._path_from_env to avoid env
    def fake_path(self):  # noqa: ANN001
        return ["0xdiem", "0xusdc"]

    monkeypatch.setattr(svc_mod.DIEMService, "_path_from_env", fake_path, raising=True)

    # Patch out decimals lookups
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)
    monkeypatch.setattr(arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True)

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    # Market price 1.0; exec preview ~0.5 -> 5000 bps slippage; cap default 150 -> reject
    did = agent.evaluate_and_maybe_mint(market_price=1.0, mint_rate=1.0, desired_units=10 ** 18)
    assert did is False


def test_arbi_diem_holds_without_quotes(monkeypatch):
    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    from libs.dex.routes import make_route

    class NoQuoteAgg:
        def best_quote(self, amount_in, route):  # noqa: ANN001
            return None

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

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setattr(arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True)

    risk = risk_mod.RiskPolicy.from_env()
    svc = svc_mod.DIEMService(aggregator=NoQuoteAgg())
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    did = agent.evaluate_and_maybe_mint(market_price=10.0, mint_rate=1.0, desired_units=10 ** 18)
    assert did is False
    rationale = getattr(agent, "_last_rationale", {})
    assert rationale.get("reason") == "no_liquidity_preview"

