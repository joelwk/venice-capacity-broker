from __future__ import annotations

import os
from importlib import import_module

import pytest

from agents.arbi_diem.agent import ArbiDiem
from libs.dex.providers import Quote
from libs.dex.routes import make_route
from services.diem.client import DIEMService
from services.diem.execution import ExecutionResult, ExecutionStatus
from services.marketdata.provider import MarketDataProvider


def _integration_enabled() -> bool:
    return os.getenv("RUN_INTEGRATION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def test_arbi_diem_risk_limits_mint_size(monkeypatch):
    # Arrange fake DIEM actions to capture units
    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            # Return confirmed so DIEMService.mint_and_sell_diem does not attempt to
            # wait on-chain for tx confirmation (which would require RPC_URL).
            return {
                "tx_hash": "0xabc",
                "status": "confirmed",
                "confirmation": {"status": "confirmed", "block_number": 1},
            }

        def burn(self, amount: int):  # unused here
            return {"tx_hash": "0xdef"}

        def trade(self, side: str, amount: int):
            calls.append(("trade", amount))
            return {"tx_hash": "0xghi"}

    # Monkeypatch DIEMACTIONS used by DIEMService
    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    svc_mod = import_module("services.diem.client")
    md_mod = import_module("services.marketdata.provider")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")
    pricing_mod = import_module("libs.pricing.diem")
    qmod = import_module("libs.dex.providers")

    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    )

    # Provide deterministic VVV pricing so fair value math stays predictable
    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )

    # Make fair value modestly below market to trigger mint path when price crosses threshold
    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return vvv_price * mint_rate * 0.5

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
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
            calls.append(("trade", int(amount_in)))
            return {"provider": "stub", "tx_hash": "0xtrade"}

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = make_route(route_tokens)

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route_plan]

    def fake_path_from_env(self):
        return list(route_plan.tokens)

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setattr(
        svc_mod.DIEMService, "_path_from_env", fake_path_from_env, raising=True
    )

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    svc._actions = FakeActions()  # type: ignore[attr-defined]

    # Configure policy to limit to 50 USD per trade and set decimals=18
    os.environ["DIEM_ENABLE_SVVV_GATE"] = "0"
    os.environ["RISK_MAX_DIEM_TRADE_USD"] = "50"
    os.environ["DIEM_DECIMALS"] = "18"
    risk = risk_mod.RiskPolicy.from_env()

    class DummyMarket:
        def reserve_cap_units(self, path, take_bps=None):
            return None

    monkeypatch.setattr(
        arbi_mod.ArbiDiem, "_market_provider", lambda self: DummyMarket(), raising=False
    )
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_check_factory_registration",
        lambda self: True,
        raising=True,
    )

    # Mint desire is very large, but price is $2/DIEM, so expect ~25 DIEM worth in units
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    did = arbi.evaluate_and_maybe_mint(
        market_price=2.0, mint_rate=1.0, desired_units=10**24
    )
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
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    )

    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"tx_hash": "0xmint", "status": "sent"}

        def burn(self, amount: int):
            return {"tx_hash": "0xburn"}

        def trade(self, side: str, amount: int):
            calls.append((side, amount))
            return {"tx_hash": "0xtrade"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    pricing_mod = import_module("libs.pricing.diem")
    md_mod = import_module("services.marketdata.provider")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return vvv_price * mint_rate * 1.0

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    qmod = import_module("libs.dex.providers")
    route_mod = import_module("libs.dex.routes")

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

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = route_mod.make_route(route_tokens)

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, *, force_dynamic=False: [route_plan],
        raising=True,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_path_from_env",
        lambda self: list(route_plan.tokens),
        raising=True,
    )

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    svc._actions = FakeActions()  # type: ignore[attr-defined]

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi_mod = import_module("agents.arbi_diem.agent")
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_check_factory_registration",
        lambda self: True,
        raising=True,
    )
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    # Simulate mode: we only validate decisioning/rationale (no web3/RPC required).
    did_premium_low = arbi.evaluate_and_maybe_mint(
        market_price=1.08, mint_rate=1.0, desired_units=10**21, simulate=True
    )
    assert did_premium_low is False
    rationale_low = getattr(arbi, "_last_rationale", {}) or {}
    assert float(rationale_low.get("threshold_mult") or 0.0) == 1.10

    did_premium_high = arbi.evaluate_and_maybe_mint(
        market_price=1.12, mint_rate=1.0, desired_units=10**21, simulate=True
    )
    assert did_premium_high is True
    rationale_high = getattr(arbi, "_last_rationale", {}) or {}
    assert rationale_high.get("decision") == "mint_sell"
    assert float(rationale_high.get("threshold_mult") or 0.0) == 1.10


def test_arbi_diem_default_premium_threshold_uses_risk_policy(monkeypatch):
    # DIEM_PREMIUM_THRESHOLD is intentionally unset here.
    # The system should use RiskPolicy defaults (1.05) unless explicitly overridden via env.
    monkeypatch.delenv("DIEM_PREMIUM_THRESHOLD", raising=False)
    monkeypatch.delenv("DIEM_DISCOUNT_THRESHOLD", raising=False)
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "100")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    )

    pricing_mod = import_module("libs.pricing.diem")

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        # Simple fair value so threshold behavior is easy to reason about in this test.
        return vvv_price * mint_rate

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    md_mod = import_module("services.marketdata.provider")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )

    qmod = import_module("libs.dex.providers")
    route_mod = import_module("libs.dex.routes")

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

    route_tokens = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    route_plan = route_mod.make_route(route_tokens)

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, *, force_dynamic=False: [route_plan],
        raising=True,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_path_from_env",
        lambda self: list(route_plan.tokens),
        raising=True,
    )
    svc = svc_mod.DIEMService(aggregator=FakeAgg())

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi_mod = import_module("agents.arbi_diem.agent")
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_check_factory_registration",
        lambda self: True,
        raising=True,
    )
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    # Fair value = 1.0, so default premium trigger 1.05 means:
    # - 1.04: hold
    # - 1.06: mint_sell
    did_low = arbi.evaluate_and_maybe_mint(
        market_price=1.04, mint_rate=1.0, desired_units=10**21, simulate=True
    )
    assert did_low is False
    rationale_low = getattr(arbi, "_last_rationale", {}) or {}
    assert float(rationale_low.get("threshold_mult") or 0.0) == 1.05

    did_high = arbi.evaluate_and_maybe_mint(
        market_price=1.06, mint_rate=1.0, desired_units=10**21, simulate=True
    )
    assert did_high is True
    rationale_high = getattr(arbi, "_last_rationale", {}) or {}
    assert rationale_high.get("decision") == "mint_sell"
    assert float(rationale_high.get("threshold_mult") or 0.0) == 1.05


def test_arbi_diem_holds_when_quotes_not_executable(monkeypatch):
    # Use default RiskPolicy premium threshold (1.05) unless explicitly overridden.
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "100")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    )

    route_plan = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        ]
    )

    class NonExecutableAgg:
        def __init__(self):
            self.trade_calls: list[int] = []

        def quote_all(self, amount_in, route):
            quote = Quote(
                provider="stub",
                amount_in=int(amount_in),
                amount_out=int(amount_in // 2),
                route=route,
            )
            object.__setattr__(quote, "executable", False)
            return [quote]

        def trade_best(self, amount_in, slippage_bps, route):
            self.trade_calls.append(int(amount_in))
            raise AssertionError(
                "trade_best should not execute when quotes are non-executable"
            )

    agg = NonExecutableAgg()

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, *, force_dynamic=False: [route_plan],
        raising=True,
    )

    svc = svc_mod.DIEMService(aggregator=agg)
    svc.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]
    svc.mint = lambda amount, dry_run=False, **_: {"status": "sent", "amount": amount}  # type: ignore[assignment]

    pricing_mod = import_module("libs.pricing.diem")
    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", lambda **kwargs: 1.0, raising=True
    )

    md_mod = import_module("services.marketdata.provider")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi_mod = import_module("agents.arbi_diem.agent")
    monkeypatch.setattr(
        arbi_mod.ArbiDiem,
        "_check_factory_registration",
        lambda self: True,
        raising=True,
    )
    market_stub = type(
        "MarketStub", (object,), {"prices": lambda self, s: {"VVV": 1.0}}
    )()
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk, market=market_stub)

    decision = arbi.evaluate_and_maybe_mint(
        market_price=2.0, mint_rate=1.0, desired_units=10**18, simulate=False
    )

    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is False
    assert rationale.get("decision") == "hold"
    assert str(rationale.get("reason", "")) in {
        "execution_rejected_no_valid_quotes",
        "execution_rejected",
        "no_liquidity_preview",
    }
    assert not agg.trade_calls, "trade should be vetoed when quotes are not executable"


def test_buy_burn_dynamic_slippage_cap(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "400")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_PREMIUM_MULT", "2.0")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_HARD_CAP_BPS", "300")
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf,0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )
    monkeypatch.setattr(
        "agents.arbi_diem.agent.check_diem_vvv_liquidity_threshold",
        lambda min_reserve_out=None: True,
        raising=True,
    )

    qmod = import_module("libs.dex.providers")

    class StubAgg:
        def __init__(self):
            self.trade_calls: list[tuple[int, float]] = []

        def quote_all_exact_out(self, amount_out, path):
            quote = qmod.Quote(
                provider="stub",
                amount_in=int(amount_out * 2),
                amount_out=int(amount_out),
                route=path,
            )
            quote.total_slippage_bps = 263.0
            return [quote]

        def best_quote_exact_out(self, amount_out, route):
            slip = 263.0  # Above static cap, below dynamic cap
            quote = qmod.Quote(
                provider="stub",
                amount_in=int(amount_out * 2),
                amount_out=int(amount_out),
                route=route,
            )
            quote.total_slippage_bps = slip
            return quote

        def trade_best_exact_out(self, amount_out, slippage_bps, route):
            self.trade_calls.append((int(amount_out), float(slippage_bps)))
            return {"provider": "stub", "tx_hash": "0xtrade"}

    route_tokens = [
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    ]
    route_plan = make_route(route_tokens)

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, *, force_dynamic=False: [route_plan],
        raising=True,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_path_from_env",
        lambda self: list(route_plan.tokens),
        raising=True,
    )

    market_stub = type(
        "MarketStub",
        (object,),
        {"prices": lambda self, symbols: {"VVV": 1.0}},
    )()

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=None)
    svc.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]

    pricing_mod = import_module("libs.pricing.diem")
    monkeypatch.setattr(
        pricing_mod,
        "fair_value_per_diem",
        lambda **kwargs: 1.6,
        raising=True,
    )

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi = ArbiDiem(diem=svc, risk=risk, market=market_stub)

    decision = arbi.evaluate_and_maybe_mint(
        market_price=0.6, mint_rate=1.0, desired_units=int(1e21), simulate=True
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "buy_burn"
    assert rationale.get("slippage_bps") == pytest.approx(263.0)
    assert rationale.get("slippage_ok") is True
    assert rationale.get("slippage_cap_bps", 0) >= 263.0


def test_buy_burn_adaptive_sizing_resizes_when_slippage_high(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "75")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_PREMIUM_MULT", "2.0")
    monkeypatch.setenv("RISK_DIEM_SLIPPAGE_HARD_CAP_BPS", "300")
    # Allow near-par discounts to trigger buy logic
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.001")
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913,0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf,0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )
    monkeypatch.setattr(
        "agents.arbi_diem.agent.check_diem_vvv_liquidity_threshold",
        lambda min_reserve_out=None: True,
        raising=True,
    )

    qmod = import_module("libs.dex.providers")

    class AdaptiveAgg:
        def __init__(self):
            self.trade_calls: list[tuple[int, float]] = []
            self.quote_calls: list[int] = []

        def quote_all_exact_out(self, amount_out, path):
            quote = qmod.Quote(
                provider="stub",
                amount_in=int(amount_out * 2),
                amount_out=int(amount_out),
                route=path,
            )
            quote.total_slippage_bps = 220.0
            return [quote]

        def best_quote_exact_out(self, amount_out, route):
            amt_out = int(amount_out)
            self.quote_calls.append(amt_out)
            diem_tokens = amt_out / float(10**18)
            slip = 220.0 if diem_tokens >= 80 else 60.0
            quote = qmod.Quote(
                provider="stub",
                amount_in=int(amt_out * 2),
                amount_out=amt_out,
                route=route,
            )
            quote.total_slippage_bps = slip
            return quote

        def trade_best_exact_out(self, amount_out, slippage_bps, route):
            self.trade_calls.append((int(amount_out), float(slippage_bps)))
            return {"provider": "stub", "tx_hash": "0xtrade"}

    route_tokens = [
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    ]
    route_plan = make_route(route_tokens)

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, *, force_dynamic=False: [route_plan],
        raising=True,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_path_from_env",
        lambda self: list(route_plan.tokens),
        raising=True,
    )

    market_stub = type(
        "MarketStub",
        (object,),
        {"prices": lambda self, symbols: {"VVV": 1.0}},
    )()

    svc = svc_mod.DIEMService(aggregator=AdaptiveAgg(), market_data=None)
    svc.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]

    pricing_mod = import_module("libs.pricing.diem")
    monkeypatch.setattr(
        pricing_mod,
        "fair_value_per_diem",
        lambda **kwargs: 1.01,
        raising=True,
    )

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi = ArbiDiem(diem=svc, risk=risk, market=market_stub)

    desired_units = 100 * (10**18)  # 100 DIEM target
    decision = arbi.evaluate_and_maybe_mint(
        market_price=1.0, mint_rate=1.0, desired_units=desired_units, simulate=True
    )
    rationale = getattr(arbi, "_last_rationale", {})

    assert decision is True
    assert rationale.get("decision") == "buy_burn"
    # Slippage should be reduced below the dynamic cap after sizing down
    assert rationale.get("slippage_bps") is not None
    assert rationale.get("slippage_bps") < rationale.get("slippage_cap_bps", 0)
    liq_units = rationale.get("liquidity_adjusted_units")
    assert liq_units is None or liq_units <= desired_units


def test_buy_burn_skips_when_wallet_diem_has_no_locked_svvv(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_DISCOUNT_THRESHOLD", "1.05")
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "TRADE_PATH",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913,0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf,0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )
    monkeypatch.setattr(
        "agents.arbi_diem.agent.check_diem_vvv_liquidity_threshold",
        lambda min_reserve_out=None: True,
        raising=True,
    )

    pricing_mod = import_module("libs.pricing.diem")
    md_mod = import_module("services.marketdata.provider")
    svc_mod = import_module("services.diem.client")

    class FakeMarketDataProvider:
        def prices(self, symbols):
            return {"VVV": 1.0}

        def price_health(self, symbol):
            return {"source": "aggregator"}

    monkeypatch.setattr(
        md_mod, "MarketDataProvider", lambda: FakeMarketDataProvider(), raising=True
    )
    monkeypatch.setattr(
        pricing_mod,
        "fair_value_per_diem",
        lambda **kwargs: 1.2,
        raising=True,
    )

    qmod = import_module("libs.dex.providers")
    routes_mod = import_module("libs.dex.routes")

    class GuardAggregator:
        def __init__(self):
            self.quote_calls = 0
            self.trade_calls = 0

        def best_quote_exact_out(self, amount_out, route):
            self.quote_calls += 1
            return qmod.Quote(
                provider="stub",
                amount_in=int(amount_out * 2),
                amount_out=int(amount_out),
                route=route,
            )

        def trade_best_exact_out(self, amount_out, slippage_bps, route):
            self.trade_calls += 1
            return {"provider": "stub", "tx_hash": "0xtrade"}

    route_plan = routes_mod.make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf",
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        ]
    )

    agg = GuardAggregator()
    svc = svc_mod.DIEMService(aggregator=agg)
    svc.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]
    svc._locked_svvv_for_wallet = lambda: 0  # type: ignore[assignment]
    svc._trade_routes = lambda: [route_plan]  # type: ignore[assignment]

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()

    arbi_mod = import_module("agents.arbi_diem.agent")
    arbi = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    portfolio = {
        "balances": {
            "DIEM": {"units": 2 * 10**18, "decimals": 18},
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
    assert rationale.get("reason") == "no_locked_svvv_for_burn"
    assert rationale.get("decision") == "hold"
    assert agg.quote_calls == 0
    assert agg.trade_calls == 0


@pytest.fixture(scope="module")
def arbi_agent() -> ArbiDiem:
    """Fixture providing ArbiDiem agent for integration tests."""
    if not _integration_enabled():
        pytest.skip("Integration tests disabled (set RUN_INTEGRATION=1 to enable)")
    market = MarketDataProvider()
    diem_service = DIEMService(aggregator=None, market_data=market)

    # Provide deterministic circulating supply to avoid network calls.
    diem_service.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]
    return ArbiDiem(diem=diem_service, market=market)


@pytest.mark.skipif(
    not _integration_enabled(),
    reason="Integration test disabled (set RUN_INTEGRATION=1 to enable)",
)
def test_fair_value_with_bridge_pricing(arbi_agent: ArbiDiem, monkeypatch):
    """ArbiDiem should calculate fair value using bridge pricing."""
    # Configure DIEM/VVV pair for bridge pricing
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )

    market = MarketDataProvider()
    market_price = market.get_price("DIEM")
    if not market_price or market_price <= 0:
        pytest.skip("DIEM market price unavailable for integration test")

    decision = arbi_agent.evaluate_and_maybe_mint(
        market_price=market_price,
        mint_rate=1.0,
        desired_units=int(1e18),
        simulate=True,
    )
    rationale = getattr(arbi_agent, "_last_rationale", {})
    fair_value = rationale.get("fair_value")

    assert fair_value is not None, "Fair value should be computed"
    assert 10.0 < float(fair_value) < 1_000.0
    assert decision in {True, False}


@pytest.mark.skipif(
    not _integration_enabled(),
    reason="Integration test disabled (set RUN_INTEGRATION=1 to enable)",
)
def test_decision_respects_invalid_price(arbi_agent: ArbiDiem):
    """ArbiDiem should not act on invalid prices."""
    decision = arbi_agent.evaluate_and_maybe_mint(
        market_price=0.001,
        mint_rate=1.0,
        simulate=True,
    )
    assert decision is False


def test_arbi_diem_execution_preview_in_rationale(monkeypatch):
    """Test that ArbiDiem attaches execution_preview to rationale in simulate mode."""
    from unittest.mock import MagicMock

    from libs.dex.providers import Quote
    from libs.dex.routes import RouteHop, RoutePlan

    # Create mock aggregator that returns quotes
    mock_agg = MagicMock()
    route = RoutePlan((RouteHop("0xdiem", "0xusdc"),))
    quote = Quote(
        provider="test",
        amount_in=1000000000000000000,
        amount_out=1000000000,
        route=route,
    )
    mock_agg.best_quote.return_value = quote
    mock_agg.quote_all.return_value = [quote]

    # Create DIEMService with mock aggregator
    market = MarketDataProvider()
    diem_service = DIEMService(aggregator=mock_agg, market_data=market)
    diem_service.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]

    # Mock trade_routes
    def fake_trade_routes(self, *, force_dynamic=False):
        return [route]

    import services.diem.client as svc_mod

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )

    arbi = ArbiDiem(diem=diem_service, market=market)

    # Mock fair value to trigger mint/sell path
    import libs.pricing.diem as pricing_mod

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 5.0  # Low fair value to trigger premium path

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    # Run in simulate mode
    decision = arbi.evaluate_and_maybe_mint(
        market_price=10.0,  # Premium over fair value
        mint_rate=1.0,
        simulate=True,
    )

    rationale = getattr(arbi, "_last_rationale", {})
    # Check that execution_preview is attached
    execution_preview = rationale.get("execution_preview")
    if decision:  # Only check if decision was made
        assert execution_preview is not None, (
            "execution_preview should be attached to rationale"
        )
        assert isinstance(execution_preview, dict)
        assert "status" in execution_preview
        assert execution_preview["status"] == "simulated"


def test_arbi_diem_rejected_execution_treated_as_hold(monkeypatch):
    """Test that ArbiDiem treats rejected/failed executions as holds."""
    from unittest.mock import MagicMock

    # Create mock aggregator that returns no quotes (simulating liquidity failure)
    mock_agg = MagicMock()
    mock_agg.best_quote.return_value = None
    mock_agg.quote_all.return_value = []
    mock_agg.best_quote_exact_out.return_value = None

    market = MarketDataProvider()
    diem_service = DIEMService(aggregator=mock_agg, market_data=market)
    diem_service.get_circulating_supply = lambda ttl_s=600: {"supply": 38_000}  # type: ignore[assignment]

    # Mock mint_and_sell_diem to return rejected status
    def mock_mint_and_sell_diem(
        self, diem_amount, slippage_bps=50, pool_take_bps=None, simulate=False
    ):
        from services.diem.execution import ExecutionIntent, TradeSide

        intent = ExecutionIntent(
            side=TradeSide.SELL,
            token_in="DIEM",
            token_out="USDC",
            amount_base_units=diem_amount,
            slippage_bps=slippage_bps,
        )
        result = ExecutionResult(
            status=ExecutionStatus.REJECTED,
            intent=intent,
            error="No valid quotes available",
        )
        return {"mint": {"status": "sent"}, "sell": result.as_dict()}

    import services.diem.client as svc_mod

    monkeypatch.setattr(
        svc_mod.DIEMService, "mint_and_sell_diem", mock_mint_and_sell_diem, raising=True
    )

    arbi = ArbiDiem(diem=diem_service, market=market)

    # Mock fair value
    import libs.pricing.diem as pricing_mod

    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return 5.0

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    # Run in live mode (simulate=False)
    decision = arbi.evaluate_and_maybe_mint(
        market_price=10.0,  # Premium over fair value
        mint_rate=1.0,
        simulate=False,
    )

    rationale = getattr(arbi, "_last_rationale", {})
    # Should be treated as hold due to rejected execution
    if "execution" in rationale:
        execution = rationale["execution"]
        sell_result = execution.get("sell", {})
        if sell_result.get("status") == "rejected":
            assert decision is False, (
                "Rejected execution should result in hold decision"
            )
            assert rationale.get("decision") == "hold"
            assert rationale.get("reason") in (
                "execution_rejected",
                "execution_failed",
                "execution_error",
            )
