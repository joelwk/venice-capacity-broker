from __future__ import annotations

from importlib import import_module


def test_buy_burn_skips_when_wallet_diem_has_no_locked_svvv(monkeypatch):
    # Keep discount branch enabled and deterministic
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )

    # Liquidity gate should not block the path
    monkeypatch.setattr(
        "agents.arbi_diem.agent.check_diem_vvv_liquidity_threshold",
        lambda min_reserve_out=None: True,
        raising=True,
    )

    md_mod = import_module("services.marketdata.provider")
    pricing_mod = import_module("libs.pricing.diem")
    svc_mod = import_module("services.diem.client")
    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    routes_mod = import_module("libs.dex.routes")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )
    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", lambda **kwargs: 1.0, raising=True
    )

    # Aggregator only needs capability flags; methods should never be called in this path
    class StubAggregator:
        def best_quote_exact_out(self, amount_out, route):
            raise AssertionError("best_quote_exact_out should not be called")

        def trade_best_exact_out(self, amount_out, slippage_bps, route):
            raise AssertionError("trade_best_exact_out should not be called")

    route_plan = routes_mod.make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ]
    )

    svc = svc_mod.DIEMService(aggregator=StubAggregator())
    svc.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]
    svc._locked_svvv_for_wallet = lambda: 0  # type: ignore[assignment]

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    svc.trade_routes = fake_trade_routes.__get__(  # type: ignore[assignment]
        svc, type(svc)
    )

    risk = risk_mod.RiskPolicy.from_env()
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    portfolio = {
        "balances": {
            "DIEM": {"units": 5 * 10**18, "decimals": 18},
            "USDC": {"units": 0, "decimals": 6},
        }
    }

    decision = arbi.evaluate_and_maybe_mint(
        market_price=0.8,
        mint_rate=1.0,
        desired_units=10**18,
        simulate=False,
        portfolio_snapshot=portfolio,
    )

    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is False
    assert rationale.get("decision") == "hold"
    assert rationale.get("reason") == "no_locked_svvv_for_burn"
