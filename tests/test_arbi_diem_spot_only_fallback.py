from __future__ import annotations

from importlib import import_module


def test_mint_unavailable_premium_uses_spot_sell_wallet_inventory(monkeypatch):
    """
    When DIEMService has mint_unavailable latched, the premium branch must not call
    wallet_first_mint_and_sell (which would try mint+sell). Instead it should spot-sell
    wallet DIEM inventory only.
    """
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_PREMIUM_THRESHOLD", "1.05")
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "100")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    )

    pricing_mod = import_module("libs.pricing.diem")
    md_mod = import_module("services.marketdata.provider")
    svc_mod = import_module("services.diem.client")
    arbi_mod = import_module("agents.arbi_diem.agent")
    routes_mod = import_module("libs.dex.routes")
    qmod = import_module("libs.dex.providers")
    risk_mod = import_module("services.risk.policy")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider())
    monkeypatch.setattr(pricing_mod, "fair_value_per_diem", lambda **kwargs: 1.0)

    route_plan = routes_mod.make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0x4200000000000000000000000000000000000006",  # WETH
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        ]
    )

    class FakeAgg:
        def best_quote(self, amount_in, route):
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in * 2),
                route=route,
            )

        def quote_all(self, amount_in, route):
            return [
                qmod.Quote(
                    provider="stub",
                    amount_in=int(amount_in),
                    amount_out=int(amount_in * 2),
                    route=route,
                )
            ]

        def trade_best(self, amount_in, slippage_bps, route):
            return {"provider": "stub", "tx_hash": "0xtrade"}

    svc = svc_mod.DIEMService(aggregator=FakeAgg())

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    svc.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        svc, type(svc)
    )
    svc._mint_unavailable = True  # simulate latched state

    # Make sure we never call the mint path
    monkeypatch.setattr(
        svc,
        "wallet_first_mint_and_sell",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("wallet_first_mint_and_sell should not be called")
        ),
        raising=True,
    )

    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_check_factory_registration", lambda self: True
    )

    portfolio = {"balances": {"DIEM": {"units": 5 * 10**18, "decimals": 18}}}

    risk = risk_mod.RiskPolicy.from_env()
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    decision = arbi.evaluate_and_maybe_mint(
        market_price=2.0,
        mint_rate=1.0,
        desired_units=10**18,
        simulate=True,
        portfolio_snapshot=portfolio,
    )

    rationale = getattr(arbi, "_last_rationale", {})
    assert decision is True
    assert rationale.get("decision") == "spot_sell"
    assert rationale.get("mint_unavailable_latched") is True


def test_mint_unavailable_discount_uses_spot_buy_not_buy_burn(monkeypatch):
    """
    When mint is unavailable, the discount branch should choose spot_buy and skip
    buy_and_burn logic (burn requires minted DIEM backing).
    """
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "100")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf,0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )

    pricing_mod = import_module("libs.pricing.diem")
    md_mod = import_module("services.marketdata.provider")
    svc_mod = import_module("services.diem.client")
    arbi_mod = import_module("agents.arbi_diem.agent")
    routes_mod = import_module("libs.dex.routes")
    risk_mod = import_module("services.risk.policy")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider())
    monkeypatch.setattr(pricing_mod, "fair_value_per_diem", lambda **kwargs: 1.0)

    # Aggregator shouldn't be invoked in simulate=True spot_buy
    class StubAgg:
        pass

    route_plan = routes_mod.make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ]
    )

    svc = svc_mod.DIEMService(aggregator=StubAgg())

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    svc.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        svc, type(svc)
    )
    svc._mint_unavailable = True

    monkeypatch.setattr(
        svc,
        "wallet_first_buy_and_burn",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("wallet_first_buy_and_burn should not be called")
        ),
        raising=True,
    )

    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_check_factory_registration", lambda self: True
    )
    # Don't let liquidity threshold checks block this decision in tests
    monkeypatch.setattr(
        "agents.arbi_diem.agent.check_diem_vvv_liquidity_threshold",
        lambda min_reserve_out=None: True,
        raising=True,
    )

    portfolio = {
        "balances": {
            "DIEM": {"units": 0, "decimals": 18},
            "USDC": {"units": 10_000_000, "decimals": 6},
        }
    }

    risk = risk_mod.RiskPolicy.from_env()
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    decision = arbi.evaluate_and_maybe_mint(
        market_price=0.8,
        mint_rate=1.0,
        desired_units=10**18,
        simulate=True,
        portfolio_snapshot=portfolio,
    )

    rationale = getattr(arbi, "_last_rationale", {})
    assert decision is True
    assert rationale.get("decision") == "spot_buy"
    assert rationale.get("mint_unavailable_latched") is True
