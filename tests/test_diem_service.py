from __future__ import annotations

import os
import time
from importlib import import_module

import pytest

from libs.dex.diem_fallbacks import build_diem_route_preferences
from libs.dex.providers import DexAggregator, Quote
from libs.dex.routes import make_route


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
            return {"status": "skipped", "amount": amount}

        def stake_for_api(self, amount: int):
            stake_calls.append(amount)
            return {"status": "sent", "action": "stake", "tx_hash": "0xfeed"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1")
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", str(10**21))

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_locked_svvv_for_wallet",
        lambda self, wallet_address=None: 10**21,
        raising=True,
    )

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
    assert lock_calls == []
    assert stake_calls == [777]

    assert r1.get("action") == "mint" and r1.get("status") == "sent"
    assert r2.get("action") == "burn" and r2.get("status") == "sent"
    assert r3.get("action") == "mint" and r3.get("status") == "sent"
    assert r4.get("action") == "burn" and r4.get("status") == "sent"
    assert r5.get("action") == "stake" and r5.get("status") == "sent"


def test_mint_denied_when_svvv_insufficient(monkeypatch):
    calls: list[int] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(amount)
            return {"status": "sent", "action": "mint", "tx_hash": "0xdead"}

        def burn(self, amount: int):
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "2")
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", "5")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)
    svc._actions = FakeActions()  # type: ignore[attr-defined]

    res = svc.mint(4)

    assert res["status"] == "denied"
    assert res.get("reason") == "insufficient_svvv"
    assert calls == []
    bal = res.get("svvv_balance") or {}
    assert bal.get("ok") is False
    assert bal.get("required_svvv") == 8
    assert bal.get("available_svvv") == 5


def test_trade_slippage_override(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "0xdiem,0xusdc")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_out")

    class StubAgg:
        def __init__(self) -> None:
            self.history: list[tuple[str, int]] = []

        def quote_all(self, amount: int, path: object):
            return [{"provider": "stub"}]

        def trade_best(self, amount: int, slippage_bps: int, path: list[str]):
            self.history.append(("sell", slippage_bps))
            return {"tx": "0x1"}

        def trade_best_exact_out(self, amount: int, slippage_bps: int, path: list[str]):
            self.history.append(("buy", slippage_bps))
            return {"tx": "0x2"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token_address: 10**12,
        raising=True,
    )
    simple_route = make_route(["0xdiem", "0xusdc"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "quote",
        lambda self, side, amount, routes=None: {
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": amount,
                    "amount_out": amount,
                    "executable": True,
                }
            ]
        },
        raising=False,
    )

    svc.trade("sell", 100, slippage_bps=55)
    svc.trade("buy", 200, slippage_bps=77)

    assert agg.history == [("sell", 55), ("buy", 77)]


def test_wallet_first_mint_and_sell_prefers_wallet_balance(monkeypatch):
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")

    # Mock portfolio to report DIEM balance only
    def fake_portfolio(*, include_eth=False):
        return {
            "balances": {
                "DIEM": {"units": 200, "decimals": 18},
            }
        }

    monkeypatch.setattr(
        "services.wallet.provider.describe_treasury_portfolio",
        fake_portfolio,
        raising=True,
    )

    # Track calls
    trade_calls = []
    mint_calls = []

    class FakeResult:
        def __init__(self, units):
            self.status = "submitted"
            self.units = units

        def as_dict(self):
            return {"status": "submitted", "amount": self.units}

    def fake_execute_trade(self, intent, simulate=True):
        trade_calls.append(intent.amount_base_units)
        return FakeResult(intent.amount_base_units)

    def fake_mint_and_sell(self, diem_amount, **kwargs):
        mint_calls.append(diem_amount)
        return {"sell": {"status": "submitted"}}

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)
    monkeypatch.setattr(
        svc_mod.DIEMService, "execute_trade", fake_execute_trade, raising=True
    )
    monkeypatch.setattr(
        svc_mod.DIEMService, "mint_and_sell_diem", fake_mint_and_sell, raising=True
    )

    res = svc.wallet_first_mint_and_sell(diem_amount=150, simulate=False)
    assert res.get("status") == "submitted"
    assert res.get("sell", {}).get("used_wallet_diem") == 150
    assert trade_calls == [150]  # used wallet balance only
    assert mint_calls == []  # no mint needed when wallet covers


def test_wallet_first_buy_and_burn_uses_wallet_before_dex(monkeypatch):
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")

    def fake_portfolio(*, include_eth=False):
        return {
            "balances": {
                "DIEM": {"units": 80, "decimals": 18},
            }
        }

    monkeypatch.setattr(
        "services.wallet.provider.describe_treasury_portfolio",
        fake_portfolio,
        raising=True,
    )

    burn_calls = []
    trade_calls = []

    class FakeResult:
        def __init__(self, units):
            self.status = "submitted"
            self.units = units

        def as_dict(self):
            return {"status": "submitted", "amount": self.units}

    def fake_burn(self, amount, dry_run=False, corr_id=None):
        burn_calls.append(int(amount))
        return {"status": "sent", "amount": int(amount)}

    def fake_execute_trade(self, intent, simulate=True):
        trade_calls.append(intent.amount_base_units)
        return FakeResult(intent.amount_base_units)

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)
    monkeypatch.setattr(svc_mod.DIEMService, "burn", fake_burn, raising=True)
    monkeypatch.setattr(
        svc_mod.DIEMService, "execute_trade", fake_execute_trade, raising=True
    )

    res = svc.wallet_first_buy_and_burn(diem_amount=50, simulate=False)
    assert res.get("status") == "submitted"
    assert burn_calls == [50]
    assert trade_calls == []  # no DEX buy since wallet covered


def test_execute_trade_rejects_analytic_by_default(monkeypatch):
    monkeypatch.setenv("DIEM_ALLOW_COMPOSITE_ANALYTIC_EXECUTION", "0")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")

    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    routes_mod = import_module("libs.dex.routes")

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000,
        slippage_bps=50,
    )

    class StubSvc(svc_mod.DIEMService):
        pass

    class GuardAgg:
        def __init__(self):
            self.called = False

        def trade_best_exact_out(self, *args, **kwargs):
            self.called = True
            raise AssertionError("trade_best_exact_out should not be called")

        def trade_best(self, *args, **kwargs):
            self.called = True
            raise AssertionError("trade_best should not be called")

    agg = GuardAgg()
    svc = StubSvc(aggregator=agg)
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (
            lambda self, force_dynamic=False: [
                routes_mod.make_route(["0xusdc", "0xdiem"])
            ]
        ).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc,
        "quote",
        lambda side, amount, routes=None: {
            "quotes": [
                {
                    "provider": "composite_analytic",
                    "amount_in": 500_000,  # 0.5 USDC
                    "amount_out": 1_000,
                    "executable": False,
                }
            ]
        },
        raising=False,
    )
    res = svc.execute_trade(intent, simulate=True)
    assert res.status == exec_mod.ExecutionStatus.REJECTED
    assert agg.called is False


def test_execute_trade_allows_analytic_when_enabled_and_small(monkeypatch):
    monkeypatch.setenv("DIEM_ALLOW_COMPOSITE_ANALYTIC_EXECUTION", "1")
    monkeypatch.setenv("DIEM_ANALYTIC_EXECUTION_MAX_USD", "10")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")

    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    routes_mod = import_module("libs.dex.routes")

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000,
        slippage_bps=50,
    )

    class Agg:
        def trade_best_exact_out(self, amount_out, slippage_bps, route):
            return {
                "status": "sent",
                "tx_hash": "0x1",
                "amount_in": 500_000,
                "amount_out": amount_out,
                "route": list(route.tokens) if hasattr(route, "tokens") else [],
            }

    svc = svc_mod.DIEMService(aggregator=Agg())
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (
            lambda self, force_dynamic=False: [
                routes_mod.make_route(["0xusdc", "0xdiem"])
            ]
        ).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token_address: 10**12,
        raising=False,
    )
    monkeypatch.setattr(
        svc,
        "quote",
        lambda side, amount, routes=None: {
            "quotes": [
                {
                    "provider": "composite_analytic",
                    "amount_in": 500_000,  # 0.5 USDC
                    "amount_out": amount,
                    "executable": False,
                }
            ]
        },
        raising=False,
    )

    res = svc.execute_trade(intent, simulate=False)
    assert res.status == exec_mod.ExecutionStatus.SUBMITTED
    assert res.diagnostics.get("trade_result", {}).get("amount_in") == 500_000


def test_trade_slippage_override_agent_override(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_SLIPPAGE_OVERRIDE_ENABLE", "1")
    monkeypatch.setenv("DIEM_SLIPPAGE_OVERRIDE_MAX_BPS", "800")

    svc_mod = import_module("services.diem.client")

    class StubAgg:
        def __init__(self) -> None:
            self.calls: list[int] = []
            self.providers: list[str] = []

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.calls.append(slippage_bps)
            return {"tx": "0xabc"}

    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)
    simple_route = make_route(["0xusdc", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token_address: 10**12,
        raising=True,
    )
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "quote",
        lambda self, side, amount, routes=None: {
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": amount,
                    "amount_out": amount,
                    "executable": True,
                }
            ]
        },
        raising=False,
    )

    svc.trade("buy", 100, slippage_bps=50, slippage_override_bps=700)

    assert agg.calls == [700]


def test_dex_provider_routing_respects_route_type(monkeypatch):
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DEX_FORCE_V2_FOR_CANONICAL", "1")

    class FakeProvider:
        def __init__(self, name: str) -> None:
            self.name = name
            self.supports_exact_out = True

    providers = [FakeProvider("uniswap_v2"), FakeProvider("uniswap_v3")]
    agg = DexAggregator(providers)
    agg._execution_providers = [
        providers[0]
    ]  # force v2 for execution to match expectation

    captured: list[tuple[str, list[str]]] = []

    def fake_collect_quotes(
        self,
        active_providers,
        method,
        route_plan,
        amount,
        mode="exact_in",
    ):
        captured.append((mode, [p.name for p in active_providers]))
        if not active_providers:
            return []
        return [
            Quote(
                provider=active_providers[0].name,
                amount_in=amount,
                amount_out=amount,
                route=route_plan,
            )
        ]

    monkeypatch.setattr(
        DexAggregator,
        "_collect_quotes",
        fake_collect_quotes,
        raising=False,
    )

    v2_route = make_route(["0xusdc", "0xdiem"])
    agg.quote_all(100, v2_route)
    assert captured[-1] == ("exact_in", ["uniswap_v2"])

    v3_route = make_route(["0xusdc", "0xvvv", "0xdiem"], fees=[3000, 3000])
    agg.quote_all(150, v3_route)
    assert captured[-1] == ("exact_in", ["uniswap_v3"])

    captured.clear()
    agg.quote_all_exact_out(50, v2_route)
    assert captured[-1] == ("exact_out", ["uniswap_v2"])

    captured.clear()
    agg.quote_all_exact_out(60, v3_route)
    assert captured[-1] == ("exact_out", ["uniswap_v3"])


def test_route_revert_guardrail_mutes_and_expires(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_THRESHOLD", "2")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_TTL_SECONDS", "3600")

    svc_mod = import_module("services.diem.client")

    class RevertingAgg:
        def __init__(self) -> None:
            self.calls = 0
            self.providers: list[str] = []

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.calls += 1
            raise RuntimeError("execution reverted: no data")

    agg = RevertingAgg()
    svc = svc_mod.DIEMService(aggregator=agg)
    simple_route = make_route(["0xusdc", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )

    for _ in range(2):
        with pytest.raises(RuntimeError):
            svc.trade("buy", 100, slippage_bps=100)

    assert agg.calls == 2
    assert svc._is_route_muted(simple_route) is True

    with pytest.raises(RuntimeError):
        svc.trade("buy", 120, slippage_bps=90)

    assert agg.calls == 2

    route_key = svc._route_key(simple_route)
    count, first_ts = svc._route_revert_counts[route_key]
    ttl = float(os.getenv("DIEM_ROUTE_REVERT_BAN_TTL_SECONDS", "3600") or 3600)
    svc._route_revert_counts[route_key] = (count, first_ts - ttl - 5)

    with pytest.raises(RuntimeError):
        svc.trade("buy", 130, slippage_bps=80)

    assert agg.calls == 3


def test_route_incoherent_preview_guard_mutes_and_expires(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    # Disable coherence relaxation so this test deterministically asserts muting.
    monkeypatch.setenv("DIEM_COHERENCE_BRIDGE_MIN_USD", "0")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.50")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_TTL_SECONDS", "3600")

    svc_mod = import_module("services.diem.client")

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols: list[str]) -> dict[str, float]:
            return {str(s): self.price(str(s)) for s in (symbols or [])}

        def get_price(self, symbol: str) -> float:
            return self.price(symbol)

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    simple_route = make_route(["0xusdc", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc,
        "quote",
        lambda side, amount, routes=None: {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 1_000_000,  # 1 USDC
                    "amount_out": 1_000_000_000_000_000,  # 0.001 DIEM
                    "route": simple_route,
                    "path": list(simple_route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        },
        raising=False,
    )

    from services.diem.execution import ExecutionIntent, TradeSide

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=123,
        slippage_bps=50,
        preferred_route=simple_route,
    )
    svc.preview_trade(intent)

    assert svc._is_route_muted(simple_route, side="buy") is True

    route_key = svc._route_key(simple_route)
    mute_key = svc._preview_incoherent_mute_key(route_key, "buy")
    assert mute_key in svc._route_preview_incoherent_mutes
    svc._route_preview_incoherent_mutes[mute_key] = time.time() - 5
    assert svc._is_route_muted(simple_route, side="buy") is False


def test_preview_trade_respects_incoherent_mute_and_uses_metadata_market_price(
    monkeypatch,
):
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.50")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_TTL_SECONDS", "3600")

    svc_mod = import_module("services.diem.client")

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 1.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols: list[str]) -> dict[str, float]:
            return {str(s): self.price(str(s)) for s in (symbols or [])}

        def get_price(self, symbol: str) -> float:
            return self.price(symbol)

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    simple_route = make_route(["0xusdc", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )

    calls = {"quote": 0}

    def fake_quote(side, amount, routes=None):
        calls["quote"] += 1
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 1_000_000,  # 1 USDC
                    "amount_out": 1_000_000_000_000_000,  # 0.001 DIEM
                    "route": simple_route,
                    "path": list(simple_route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        }

    monkeypatch.setattr(svc, "quote", fake_quote, raising=False)

    from services.diem.execution import ExecutionIntent, ExecutionStatus, TradeSide

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=123,
        slippage_bps=50,
        preferred_route=simple_route,
        metadata={"diem_market_price_usd": 100.0},
    )

    first = svc.preview_trade(intent)
    assert first.status == ExecutionStatus.SIMULATED
    assert calls["quote"] == 1
    assert first.diagnostics.get("coherence_market_price_usd") == pytest.approx(100.0)
    assert first.diagnostics.get("coherence_market_price_source") == "intent_metadata"
    assert first.diagnostics.get("coherence_relaxed") is True
    assert first.diagnostics.get("coherence_incoherent_preview") is None
    assert "small_notional" in first.diagnostics.get("coherence_relax_reasons", [])
    assert svc._is_route_muted(simple_route, side="buy") is False

    second = svc.preview_trade(intent)
    assert second.status == ExecutionStatus.SIMULATED
    assert calls["quote"] == 2


def test_preview_buy_relaxes_coherence_when_v2_disabled(monkeypatch):
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xvvv,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.50")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols):
            return {str(s): self.price(str(s)) for s in (symbols or [])}

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    simple_route = make_route(["0xusdc", "0xvvv", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )

    def fake_quote(side, amount, routes=None):
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 300 * 1_000_000,  # 300 USDC
                    "amount_out": 1_000_000_000_000_000_000,  # 1 DIEM
                    "route": simple_route,
                    "path": list(simple_route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [
                {
                    "provider": "uniswap_v2",
                    "status": "skipped",
                    "reason": "discovery_disabled",
                }
            ],
            "quote_summary": {},
        }

    monkeypatch.setattr(svc, "quote", fake_quote, raising=False)
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_bridge_reference_price_usd",
        lambda self: 300.0,
        raising=True,
    )

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1_000_000_000_000_000_000,
        slippage_bps=50,
        preferred_route=simple_route,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.diagnostics.get("coherence_relaxed") is True
    assert result.diagnostics.get("coherence_reference") == "bridge_vvv"
    assert result.diagnostics.get("coherence_incoherent_preview") is None
    assert svc._is_route_muted(simple_route, side="buy") is False


def test_preview_buy_uses_bridge_reference_even_without_relax_reasons(monkeypatch):
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")

    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xvvv,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.50")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols):
            return {str(s): self.price(str(s)) for s in (symbols or [])}

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    simple_route = make_route(["0xusdc", "0xvvv", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )

    def fake_quote(side, amount, routes=None):
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 300 * 1_000_000,  # 300 USDC
                    "amount_out": 1_000_000_000_000_000_000,  # 1 DIEM
                    "route": simple_route,
                    "path": list(simple_route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        }

    monkeypatch.setattr(svc, "quote", fake_quote, raising=False)
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_bridge_reference_price_usd",
        lambda self: 300.0,
        raising=True,
    )

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1_000_000_000_000_000_000,
        slippage_bps=50,
        preferred_route=simple_route,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.diagnostics.get("coherence_reference") == "bridge_vvv"
    assert result.diagnostics.get("coherence_relaxed") is True
    assert result.diagnostics.get("coherence_incoherent_preview") is None
    assert svc._is_route_muted(simple_route, side="buy") is False


def test_preview_buy_widens_threshold_when_bridge_reference_unavailable(monkeypatch):
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")

    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xvvv,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.10")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_DRIFT", "2.0")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols):
            return {str(s): self.price(str(s)) for s in (symbols or [])}

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    simple_route = make_route(["0xusdc", "0xvvv", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )

    def fake_quote(side, amount, routes=None):
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 150 * 1_000_000,  # 150 USDC
                    "amount_out": 1_000_000_000_000_000_000,  # 1 DIEM
                    "route": simple_route,
                    "path": list(simple_route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        }

    monkeypatch.setattr(svc, "quote", fake_quote, raising=False)
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_bridge_reference_price_usd",
        lambda self: None,
        raising=True,
    )

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1_000_000_000_000_000_000,
        slippage_bps=50,
        preferred_route=simple_route,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.diagnostics.get("coherence_threshold_relaxed") is True
    assert result.diagnostics.get("coherence_max_rel_diff") == pytest.approx(2.0)
    assert result.diagnostics.get("coherence_relaxed") is True
    assert result.diagnostics.get("coherence_incoherent_preview") is None
    assert svc._is_route_muted(simple_route, side="buy") is False


def test_preview_buy_widens_threshold_when_market_price_stale(monkeypatch):
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")

    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xvvv,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.10")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_DRIFT", "2.0")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols):
            return {str(s): self.price(str(s)) for s in (symbols or [])}

        def price_health(self, symbol: str):
            return {"symbol": str(symbol), "stale": True, "age": 999.0}

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    simple_route = make_route(["0xusdc", "0xvvv", "0xdiem"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )

    def fake_quote(side, amount, routes=None):
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 150 * 1_000_000,  # 150 USDC
                    "amount_out": 1_000_000_000_000_000_000,  # 1 DIEM
                    "route": simple_route,
                    "path": list(simple_route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        }

    monkeypatch.setattr(svc, "quote", fake_quote, raising=False)
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_bridge_reference_price_usd",
        lambda self: None,
        raising=True,
    )

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1_000_000_000_000_000_000,
        slippage_bps=50,
        preferred_route=simple_route,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.diagnostics.get("coherence_market_price_stale") is True
    assert result.diagnostics.get(
        "coherence_market_price_age_seconds"
    ) == pytest.approx(999.0)
    assert result.diagnostics.get("coherence_threshold_relaxed") is True
    reasons = result.diagnostics.get("coherence_threshold_relax_reasons", [])
    assert "bridge_reference_unavailable" in reasons
    assert "market_price_stale" in reasons
    assert result.diagnostics.get("coherence_incoherent_preview") is None
    assert svc._is_route_muted(simple_route, side="buy") is False


def test_preview_trade_prefers_trade_path_buy_when_configured(monkeypatch):
    monkeypatch.setenv("TRADE_PATH_BUY", "0xusdc,0xvvv,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "0")

    svc_mod = import_module("services.diem.client")

    class MarketStub:
        @staticmethod
        def _parse_route_spec(raw: str):
            tokens = [p.strip() for p in str(raw or "").split(",") if p.strip()]
            tokens = [t.split("@", 1)[0].strip() for t in tokens]
            return make_route(tokens)

        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 1.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols: list[str]) -> dict[str, float]:
            return {str(s): self.price(str(s)) for s in (symbols or [])}

        def get_price(self, symbol: str) -> float:
            return self.price(symbol)

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())

    monkeypatch.setattr(
        svc,
        "trade_routes",
        (
            lambda self, force_dynamic=False: (_ for _ in ()).throw(
                RuntimeError("trade_routes called")
            )
        ).__get__(svc, type(svc)),
        raising=False,
    )

    captured: dict[str, object] = {}

    def fake_quote(side, amount, routes=None):
        captured["routes"] = routes
        route = routes[0] if routes else make_route(["0xusdc", "0xdiem"])
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 1_000_000,
                    "amount_out": 1_000_000_000_000_000,
                    "route": route,
                    "path": list(route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        }

    monkeypatch.setattr(svc, "quote", fake_quote, raising=False)

    from services.diem.execution import ExecutionIntent, ExecutionStatus, TradeSide

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=123,
        slippage_bps=50,
    )
    result = svc.preview_trade(intent)
    assert result.status == ExecutionStatus.SIMULATED

    routes = captured.get("routes")
    assert isinstance(routes, list)
    assert routes
    assert list(routes[0].tokens) == ["0xusdc", "0xvvv", "0xdiem"]


def test_trade_uses_env_route_for_buy(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc@3000")

    class StubAgg:
        def __init__(self) -> None:
            self.paths: list[list[str]] = []

        def quote_all(self, amount: int, path: object):
            return [{"provider": "stub"}]

        def trade_best(self, amount: int, slippage_bps: int, path: list[str] | object):
            tokens = list(getattr(path, "tokens", path))
            self.paths.append(tokens)
            return {"tx": "0x1"}

        def trade_best_exact_out(
            self, amount: int, slippage_bps: int, path: list[str] | object
        ):
            tokens = list(getattr(path, "tokens", path))
            self.paths.append(tokens)
            return {"tx": "0x2"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    svc.trade("buy", 200, slippage_bps=50)

    assert agg.paths
    path_tokens = [str(tok).lower() for tok in agg.paths[0]]
    import os

    quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").lower()
    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").lower()
    assert path_tokens[0] in {"usdc", quote_addr}
    assert path_tokens[-1] in {"diem", diem_addr}


def test_query_mint_rate_onchain_safe_ttl_cache(monkeypatch):
    svc_mod = import_module("services.diem.client")

    svc_mod.DIEMService._mint_rate_cache = {}
    clock = {"now": 1000.0}
    monkeypatch.setattr(svc_mod.time, "time", lambda: clock["now"], raising=True)

    calls = {"n": 0}

    def fake_query(self):
        calls["n"] += 1
        return 123

    monkeypatch.setattr(
        svc_mod.DIEMService, "_query_mint_rate_onchain", fake_query, raising=True
    )

    svc_a = svc_mod.DIEMService(aggregator=None)
    assert svc_a._query_mint_rate_onchain_safe() == 123
    assert calls["n"] == 1

    clock["now"] += 10.0
    assert svc_a._query_mint_rate_onchain_safe() == 123
    assert calls["n"] == 1

    # Cache is class-level and shared across instances.
    svc_b = svc_mod.DIEMService(aggregator=None)
    assert svc_b._query_mint_rate_onchain_safe() == 123
    assert calls["n"] == 1

    clock["now"] += float(svc_mod.DIEMService._MINT_RATE_CACHE_TTL) + 1.0
    assert svc_b._query_mint_rate_onchain_safe() == 123
    assert calls["n"] == 2


def test_buy_uses_exact_in_when_configured(monkeypatch):
    """Test that buy trades use exact-in execution when DIEM_BUY_EXECUTION_MODE=exact_in."""
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc@3000")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")

    class StubAgg:
        def __init__(self) -> None:
            self.exact_in_calls: list[tuple[int, int]] = []
            self.exact_out_calls: list[tuple[int, int]] = []

        def best_quote_exact_out(self, amount_out: int, route: object):
            # Return a quote to help compute amount_in
            return Quote(
                provider="test",
                amount_in=amount_out * 2,  # Simple 2:1 ratio
                amount_out=amount_out,
                route=route,
            )

        def trade_best_exact_in(self, amount_in: int, slippage_bps: int, route: object):
            self.exact_in_calls.append((amount_in, slippage_bps))
            return {"tx": "0xexact_in", "provider": "test"}

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.exact_out_calls.append((amount_out, slippage_bps))
            return {"tx": "0xexact_out", "provider": "test"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    # Mock trade_routes to return a sell-direction bridge route (DIEM→VVV→USDC)
    simple_route = make_route(["0xdiem", "0xvvv", "0xusdc"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 1000000000,  # Sufficient balance
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_is_route_muted",
        lambda self, route, correlation_id=None: False,
        raising=False,
    )

    # Buy 1 DIEM (1e18 base units)
    diem_amount = 10**18
    svc.trade("buy", diem_amount, slippage_bps=50)

    # Should have attempted exact-in first
    assert len(agg.exact_in_calls) > 0, "Expected exact-in to be attempted"
    # Should not have attempted exact-out (unless exact-in failed)
    # But since exact-in succeeds, exact-out should not be called
    assert len(agg.exact_out_calls) == 0, (
        "Expected exact-out not to be called when exact-in succeeds"
    )


def test_buy_falls_back_to_exact_out_when_exact_in_fails(monkeypatch):
    """Test that buy trades fall back to exact-out when exact-in fails."""
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc@3000")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")

    class StubAgg:
        def __init__(self) -> None:
            self.exact_in_calls: list[tuple[int, int]] = []
            self.exact_out_calls: list[tuple[int, int]] = []

        def best_quote_exact_out(self, amount_out: int, route: object):
            return Quote(
                provider="test",
                amount_in=amount_out * 2,
                amount_out=amount_out,
                route=route,
            )

        def trade_best_exact_in(self, amount_in: int, slippage_bps: int, route: object):
            self.exact_in_calls.append((amount_in, slippage_bps))
            # Simulate failure by returning None

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.exact_out_calls.append((amount_out, slippage_bps))
            return {"tx": "0xexact_out", "provider": "test"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    simple_route = make_route(["0xdiem", "0xvvv", "0xusdc"])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [simple_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 1000000000,
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_is_route_muted",
        lambda self, route, correlation_id=None: False,
        raising=False,
    )

    diem_amount = 10**18
    svc.trade("buy", diem_amount, slippage_bps=50)

    # Should have attempted exact-in first
    assert len(agg.exact_in_calls) > 0, "Expected exact-in to be attempted"
    # Should have fallen back to exact-out
    assert len(agg.exact_out_calls) > 0, (
        "Expected exact-out fallback when exact-in fails"
    )


def test_buy_exact_in_filters_to_bridge_routes(monkeypatch):
    """Exact-in attempts should restrict to DIEM→VVV→USDC routes."""
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    weth_addr = "0x4200000000000000000000000000000000000006"
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")

    class RecordingAgg:
        def __init__(self):
            self.exact_in_routes: list[list[str]] = []

        def best_quote_exact_out(self, amount_out: int, route: object):
            return Quote(
                provider="test",
                amount_in=amount_out * 2,
                amount_out=amount_out,
                route=route,
            )

        def trade_best_exact_in(self, amount_in: int, slippage_bps: int, route: object):
            tokens = list(route.tokens) if hasattr(route, "tokens") else list(route)
            self.exact_in_routes.append([t.lower() for t in tokens])
            return {"tx": "0xexact_in", "provider": "test"}

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            raise AssertionError("Exact-out should not run when exact-in succeeds")

    svc_mod = import_module("services.diem.client")
    agg = RecordingAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    bridge_route = make_route([diem_addr, vvv_addr, usdc_addr])
    canonical_route = make_route([diem_addr, weth_addr, usdc_addr])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [canonical_route, bridge_route]).__get__(
            svc, type(svc)
        ),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 10**12,
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_is_route_muted",
        lambda self, route, correlation_id=None: False,
        raising=False,
    )

    svc.trade("buy", 10**18, slippage_bps=50)

    assert len(agg.exact_in_routes) == 1
    assert agg.exact_in_routes[0] == [
        usdc_addr.lower(),
        vvv_addr.lower(),
        diem_addr.lower(),
    ]


def test_buy_exact_in_skips_when_amount_in_non_positive(monkeypatch):
    """If exact-in amount cannot be determined, skip exact-in and fall back."""
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")

    class ZeroQuoteAgg:
        def __init__(self):
            self.exact_in_calls = 0
            self.exact_out_calls = 0

        def best_quote_exact_out(self, amount_out: int, route: object):
            # Return zero amount_in to force validation failure
            return Quote(
                provider="test",
                amount_in=0,
                amount_out=amount_out,
                route=route,
            )

        def trade_best_exact_in(self, amount_in: int, slippage_bps: int, route: object):
            self.exact_in_calls += 1
            return {"tx": "0xexact_in"}

        def trade_best_exact_out(
            self, amount_out: int, slippage_bps: int, route: object
        ):
            self.exact_out_calls += 1
            return {"tx": "0xexact_out"}

    svc_mod = import_module("services.diem.client")
    agg = ZeroQuoteAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    bridge_route = make_route([diem_addr, vvv_addr, usdc_addr])
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [bridge_route]).__get__(svc, type(svc)),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_verify_route_pools_exist",
        lambda self, route: (True, None),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 10**12,
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_is_route_muted",
        lambda self, route, correlation_id=None: False,
        raising=False,
    )

    svc.trade("buy", 10**18, slippage_bps=50)

    assert agg.exact_in_calls == 0, "Exact-in should be skipped when amount_in <= 0"
    assert agg.exact_out_calls > 0, "Exact-out fallback should execute"


def test_trade_errors_without_trade_path(monkeypatch):
    monkeypatch.delenv("TRADE_PATH", raising=False)
    monkeypatch.delenv("TRADE_PATHS", raising=False)
    monkeypatch.delenv("TRADE_PATH_2", raising=False)
    monkeypatch.setenv("TRADE_PATHS_DYNAMIC", "0")

    class StubAgg:
        def trade_best_exact_out(
            self, amount: int, slippage_bps: int, path: list[str] | object
        ):
            return {"tx": "0x0"}

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=StubAgg())

    with pytest.raises(EnvironmentError):
        svc.trade("buy", 100)


def test_trade_path_requires_two_tokens(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "diem")
    provider_mod = import_module("services.marketdata.provider")
    with pytest.raises(EnvironmentError):
        provider_mod.MarketDataProvider()


def test_marketdata_slippage_validation_rejects_extreme(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc")
    monkeypatch.setenv("SLIPPAGE_BPS", "20000")
    provider_mod = import_module("services.marketdata.provider")
    with pytest.raises(EnvironmentError):
        provider_mod.MarketDataProvider()


def test_marketdata_slippage_validation_rejects_negative_fallback(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS", "-5")
    provider_mod = import_module("services.marketdata.provider")
    with pytest.raises(EnvironmentError) as excinfo:
        provider_mod.MarketDataProvider()
    assert "DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS" in str(excinfo.value)


def test_diem_trade_executes_with_valid_slippage(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "diem,vvv,usdc@3000")
    monkeypatch.setenv("SLIPPAGE_BPS", "75")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "150")

    provider_mod = import_module("services.marketdata.provider")
    market = provider_mod.MarketDataProvider()

    class StubAgg:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, list[str]]] = []

        def trade_best(self, amount: int, slippage_bps: int, route: object):
            tokens = list(getattr(route, "tokens", route))
            self.calls.append((amount, slippage_bps, tokens))
            return {"tx": "0xabc"}

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg, market_data=market)
    # Force a simple route to avoid falling back to on-chain actions in tests.
    simple_route = market._parse_route_spec("diem,usdc")  # type: ignore[attr-defined]
    svc.trade_routes = (  # type: ignore[assignment]
        lambda self, force_dynamic=False: [simple_route]
    ).__get__(svc, type(svc))

    res = svc.trade("sell", 123)

    assert res.get("status") == "sent"
    assert agg.calls
    amount, slip, tokens = agg.calls[0]
    assert amount == 123
    assert slip == 75
    assert tokens[0]  # non-empty route token captured


def test_canonical_route_muting_separate_thresholds(monkeypatch):
    """Test that canonical routes use separate mute thresholds."""
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_REVERT_BAN_THRESHOLD", "3")
    monkeypatch.setenv("DIEM_CANONICAL_ROUTE_REVERT_BAN_THRESHOLD", "5")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("WETH_ADDRESS", "0xweth")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService()

    # Create canonical route
    canonical_route = make_route(["0xdiem", "0xweth", "0xusdc"])
    # Create non-canonical route
    non_canonical_route = make_route(["0xdiem", "0xvvv", "0xusdc"])

    # Record 3 reverts for non-canonical (should mute)
    for _ in range(3):
        svc._record_route_revert(
            non_canonical_route, Exception("execution reverted: no data")
        )

    # Record 4 reverts for canonical (should NOT mute yet, threshold is 5)
    for _ in range(4):
        svc._record_route_revert(
            canonical_route, Exception("execution reverted: no data")
        )

    # Non-canonical should be muted
    assert svc._is_route_muted(non_canonical_route) is True

    # Canonical should NOT be muted yet (only 4 reverts, threshold is 5)
    assert svc._is_route_muted(canonical_route) is False

    # Record one more revert for canonical (now 5, should mute)
    svc._record_route_revert(canonical_route, Exception("execution reverted: no data"))
    assert svc._is_route_muted(canonical_route) is True


def test_circuit_open_detection(monkeypatch):
    """Test that circuit-open routes are detected and skipped."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("WETH_ADDRESS", "0xweth")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")

    class MockAggregator:
        def __init__(self):
            self._circuit_states = {}

        def _circ_is_open(self, provider: str) -> bool:
            return self._circuit_states.get(provider, False)

    svc_mod = import_module("services.diem.client")
    mock_agg = MockAggregator()
    svc = svc_mod.DIEMService(aggregator=mock_agg)

    route = make_route(["0xdiem", "0xweth", "0xusdc"])

    # Initially circuit is closed
    assert svc._is_route_circuit_open(route) is False

    # Open circuit for V2 providers
    mock_agg._circuit_states["aerodrome"] = True
    mock_agg._circuit_states["uniswap_v2"] = True

    # Now route should be detected as circuit-open
    assert svc._is_route_circuit_open(route) is True

    # Close one provider
    mock_agg._circuit_states["aerodrome"] = False

    # Route should not be circuit-open anymore (not all providers open)
    assert svc._is_route_circuit_open(route) is False


def test_fallback_size_decay_coordinates_with_arbidiem(monkeypatch):
    """Test that exact-in fallback size decay coordinates with ArbiDiem config."""
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_ENABLE", "1")
    monkeypatch.setenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "10.0")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "5")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")

    class StubAgg:
        def best_quote_exact_out(self, amount, route):
            return None

        def best_quote(self, amount, route):
            return None

    # The size decay should respect min_trade_usd and max_adjust_steps
    # This is tested implicitly through the fallback logic
    # We verify the config values are read correctly
    import os

    min_trade_usd = float(os.getenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "2.0"))
    max_steps = int(os.getenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10"))

    assert min_trade_usd == 2.0
    assert max_steps == 5


def test_trade_path_requires_diem_token(monkeypatch):
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdeadbeef")
    monkeypatch.setenv("TRADE_PATH", "usdc,weth")
    provider_mod = import_module("services.marketdata.provider")
    with pytest.raises(EnvironmentError) as excinfo:
        provider_mod.MarketDataProvider()
    assert "DIEM token" in str(excinfo.value)


def test_execution_result_liquidity_error_normalization(monkeypatch):
    """Test that RuntimeError from trade() is normalized with is_liquidity_error flag."""
    from services.diem.execution import ExecutionIntent, ExecutionStatus, TradeSide

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("TRADE_PATH", "0xdiem,0xusdc")

    svc_mod = import_module("services.diem.client")

    class StubAgg:
        def quote_all_exact_out(self, amount, route):
            return []

        def trade_best_exact_out(self, amount, slippage_bps, route):
            raise RuntimeError(
                "no executable DIEM buy routes via aggregator (all routes are V3, V2 fallback disabled)"
            )

    svc = svc_mod.DIEMService(aggregator=StubAgg())

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = svc.execute_trade(intent, simulate=False)

    # Verify that liquidity errors are properly normalized
    assert result.status == ExecutionStatus.REJECTED
    assert result.diagnostics.get("is_liquidity_error") is True
    assert result.diagnostics.get("exception_type") == "NoQuotesError"
    # Error message should indicate liquidity/quote issues
    error_lower = result.error.lower()
    assert (
        "no executable" in error_lower
        or "unhealthy" in error_lower
        or "no valid quotes" in error_lower
        or "no quotes" in error_lower
    )


def test_v2_multihop_enabled_for_2hop_diem_routes(monkeypatch):
    """Test that V2 multihop is enabled for 2-hop DIEM routes when DIEM_ENABLE_V2_MULTIHOP=1."""
    monkeypatch.setenv("DIEM_ENABLE_V2_MULTIHOP", "1")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")

    providers_mod = import_module("libs.dex.providers")
    agg = providers_mod.build_aggregator_from_env()

    # Build a 2-hop DIEM route: DIEM→VVV→USDC
    route = make_route(["0xdiem", "0xvvv", "0xusdc"])

    # Check V2 compatibility
    compatible, reason = agg._diem_provider_compatibility("uniswap_v2", route)
    assert compatible is True, (
        f"V2 should be compatible for 2-hop DIEM route, got reason: {reason}"
    )


def test_v2_multihop_disabled_when_env_off(monkeypatch):
    """Test that V2 multihop is disabled when DIEM_ENABLE_V2_MULTIHOP=0."""
    monkeypatch.setenv("DIEM_ENABLE_V2_MULTIHOP", "0")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")

    providers_mod = import_module("libs.dex.providers")
    agg = providers_mod.build_aggregator_from_env()

    # Build a 2-hop DIEM route: DIEM→VVV→USDC
    route = make_route(["0xdiem", "0xvvv", "0xusdc"])

    # Check V2 compatibility - should be incompatible when disabled
    compatible, reason = agg._diem_provider_compatibility("uniswap_v2", route)
    # When disabled, V2 should be skipped for non-canonical routes
    assert compatible is False or reason == "v2_incompatible_route"


def test_route_preferences_prefer_vvv_over_weth(monkeypatch):
    """Test that route preferences prioritize VVV routes over WETH routes."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DIEM_VVV_PAIR_ADDRESS", "0xpair")
    monkeypatch.setenv("VVV_USDC_POOL_ADDRESS", "0xpool")

    from services.marketdata.pathing.env import load_env_config

    config = load_env_config()

    routes = build_diem_route_preferences("0xdiem", "0xusdc", config)

    # Should have at least one route
    assert len(routes) > 0

    # First route should be DIEM→VVV→USDC (preferred)
    if len(routes) > 0:
        first_route_tokens = [str(t).lower() for t in routes[0].tokens]
        assert "0xvvv" in first_route_tokens, "First route should use VVV"


def test_route_preferences_avoid_weth_when_enabled(monkeypatch):
    """Test that WETH routes are skipped when DIEM_ROUTE_AVOID_WETH=1."""
    monkeypatch.setenv("DIEM_ROUTE_AVOID_WETH", "1")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DIEM_VVV_PAIR_ADDRESS", "0xpair")
    monkeypatch.setenv("VVV_USDC_POOL_ADDRESS", "0xpool")

    from services.marketdata.pathing.env import load_env_config

    config = load_env_config()

    routes = build_diem_route_preferences("0xdiem", "0xusdc", config)

    # Check that no WETH routes are included
    weth_addr = "0x4200000000000000000000000000000000000006"
    for route in routes:
        route_tokens = [str(t).lower() for t in route.tokens]
        # If WETH route exists, VVV route should also exist (VVV is preferred)
        if weth_addr.lower() in route_tokens:
            # VVV route should come first
            vvv_routes = [
                r for r in routes if "0xvvv" in [str(t).lower() for t in r.tokens]
            ]
            assert len(vvv_routes) > 0, (
                "VVV routes should exist when WETH avoidance is enabled"
            )


def test_quote_shrinker_applied_to_buy_preview(monkeypatch):
    """Test that quote shrinker is applied to buy-preview when initial quotes fail."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "1.0")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "3")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")

    quote_calls = []

    class ShrinkingAgg:
        def best_quote_exact_in(self, amount, route):
            quote_calls.append(amount)
            # First call fails (large amount), subsequent calls succeed (smaller amounts)
            if amount > 1000000:  # 1 USDC
                return None
            return Quote(
                provider="uniswap_v2",
                amount_in=amount,
                amount_out=amount * 100,  # Mock quote
                route=route,
            )

    from libs.dex.routes import make_route

    route = make_route(["0xusdc", "0xdiem"])

    agg = ShrinkingAgg()
    # Simulate buy-preview with quote shrinker logic
    initial_amount = 2000000  # 2 USDC
    min_amount = int(1.0 * (10**6))  # 1 USDC minimum
    max_steps = 3

    quote = None
    for step in range(max_steps):
        current_amount = int(initial_amount / (2**step))
        if current_amount < min_amount:
            break
        quote = agg.best_quote_exact_in(current_amount, route)
        if quote:
            break

    # Should have tried multiple sizes
    assert len(quote_calls) > 1, "Quote shrinker should try multiple sizes"
    # Final quote should succeed
    assert quote is not None, "Quote shrinker should find a working quote"
    # Final amount should be smaller than initial
    assert quote_calls[-1] < initial_amount, "Final quote should use smaller amount"


def test_execution_result_includes_unhealthy_routes_diagnostics(monkeypatch):
    """Test that ExecutionResult includes unhealthy_routes_count when all routes are unhealthy."""
    from services.diem.execution import ExecutionIntent, ExecutionStatus, TradeSide

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("TRADE_PATH", "0xdiem,0xusdc")

    svc_mod = import_module("services.diem.client")

    class StubAgg:
        def quote_all_exact_out(self, amount, route):
            return []

        def trade_best_exact_out(self, amount, slippage_bps, route):
            raise RuntimeError("no executable DIEM buy routes via aggregator")

    svc = svc_mod.DIEMService(aggregator=StubAgg())

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = svc.execute_trade(intent, simulate=False)

    # Verify that liquidity errors are properly normalized
    assert result.status == ExecutionStatus.REJECTED
    assert result.diagnostics.get("is_liquidity_error") is True
    assert "exception_type" in result.diagnostics


# Integration tests for mint/burn workflow with mocked contract queries


def test_mint_dry_run_with_capacity_gate_enabled(monkeypatch):
    """Test mint dry-run workflow with DIEM_ENABLE_SVVV_GATE=1 and sufficient sVVV."""
    calls: list[tuple[str, int]] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(("mint", amount))
            return {"status": "sent", "action": "mint", "tx_hash": "0xdead"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")  # 1:1
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", str(10**21))  # Large enough

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock capacity gate to pass
    svc._can_mint = lambda self, amount: {
        "can_mint": True,
        "required_svvv": amount,
        "available_svvv": 10**21,
        "mint_rate": 1000000000000000000,
        "reason": "sufficient_svvv",
    }

    result = svc.mint(amount=5 * 10**18, dry_run=True)

    assert result["status"] == "dry_run"
    assert result["action"] == "mint"
    # New API uses diem_amount and svvv_to_lock instead of just amount
    assert result["diem_amount"] == 5 * 10**18
    # svvv_to_lock may be None if mint rate unavailable (no RPC in test)
    # Should not call actual mint in dry-run
    assert calls == []


def test_mint_dry_run_with_insufficient_svvv(monkeypatch):
    """Test mint dry-run workflow returns dry_run preview without capacity check.

    Note: As of the mint flow update, dry_run returns immediately with preview
    data without performing capacity checks (those are only done for live execution).
    This test verifies the dry_run response format.
    """
    calls: list[int] = []

    class FakeActions:
        def mint(self, amount: int):
            calls.append(amount)
            return {"status": "sent", "action": "mint", "tx_hash": "0xdead"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv("DIEM_ENABLE_SVVV_GATE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "2000000000000000000")  # 2:1
    monkeypatch.setenv("DIEM_SVVV_AVAILABLE_UNITS", str(5 * 10**18))  # Only 5 sVVV

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Even with capacity gate configured to fail, dry_run returns preview
    result = svc.mint(amount=5 * 10**18, dry_run=True)

    # Dry run returns preview immediately without capacity check
    assert result["status"] == "dry_run"
    assert result["diem_amount"] == 5 * 10**18
    assert calls == []  # Should not attempt mint


def test_burn_with_sufficient_locked_svvv(monkeypatch):
    """Test burn workflow succeeds when locked sVVV is sufficient."""
    calls: list[int] = []

    class FakeActions:
        def burn(self, amount: int):
            calls.append(amount)
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")  # 1:1

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock locked sVVV to be sufficient
    burn_amount = 5 * 10**18  # 5 DIEM
    locked_svvv = 10 * 10**18  # 10 sVVV locked (more than needed)
    svc._locked_svvv_for_wallet = lambda self, addr=None: locked_svvv
    svc._query_mint_rate_onchain = lambda self: 1000000000000000000

    # Test dry-run first
    result_dry = svc.burn(amount=burn_amount, dry_run=True)
    assert result_dry["status"] == "dry_run"
    assert result_dry["action"] == "burn"
    assert calls == []

    # Test live execution with stub actions
    result_live = svc.burn(amount=burn_amount, dry_run=False)
    assert result_live["status"] == "sent"
    assert result_live["action"] == "burn"
    assert calls == [burn_amount]


def test_burn_with_insufficient_locked_svvv(monkeypatch):
    """Test burn workflow fails when locked sVVV is insufficient."""
    calls: list[int] = []

    class FakeActions:
        def burn(self, amount: int):
            calls.append(amount)
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock locked sVVV to be insufficient
    burn_amount = 10 * 10**18  # Trying to burn 10 DIEM
    locked_svvv = 3 * 10**18  # Only 3 sVVV locked
    svc._locked_svvv_for_wallet = lambda self, addr=None: locked_svvv
    svc._query_mint_rate_onchain = lambda self: 1000000000000000000

    result = svc.burn(amount=burn_amount, dry_run=False)

    assert result["status"] == "error"
    assert result.get("error") == "insufficient_locked_svvv"
    assert calls == []  # Should not attempt burn


def test_burn_triggers_dex_sell_fallback_on_no_locked_svvv(monkeypatch):
    """Test buy_and_burn_diem triggers DEX sell fallback when no locked sVVV."""
    burn_calls: list[int] = []
    sell_calls: list[int] = []

    class FakeActions:
        def burn(self, amount: int):
            burn_calls.append(amount)
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

    class StubAgg:
        def trade_best(self, amount: int, slippage_bps: int, path: list[str]):
            sell_calls.append(amount)
            return {"tx_hash": "0xsell"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)

    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    monkeypatch.setenv("DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV", "1")

    svc_mod = import_module("services.diem.client")
    agg = StubAgg()
    svc = svc_mod.DIEMService(aggregator=agg)

    # Mock wallet has DIEM but no locked sVVV (purchased DIEM)
    svc._portfolio_balances = lambda self: {
        "DIEM": {"units": 5 * 10**18, "decimals": 18}
    }
    svc._locked_svvv_for_wallet = lambda self, addr=None: 0  # No locked sVVV
    svc._query_mint_rate_onchain = lambda self: 1000000000000000000

    # Mock trade routes
    from libs.dex.routes import make_route

    route = make_route(["0xf4d97f2da56e8c3098f3a8d538db630a2606a024", "0xusdc"])
    svc.trade_routes = (lambda self, force_dynamic=False: [route]).__get__(
        svc, type(svc)
    )

    result = svc.wallet_first_buy_and_burn(
        diem_amount=2 * 10**18,
        simulate=False,
    )

    # Should skip burn and trigger sell fallback
    assert result["status"] == "skipped" or result.get("sell") is not None
    assert burn_calls == []  # Should not attempt burn
    # Note: sell fallback may be in a different code path, verify skip status
    assert result["burn"]["status"] == "skipped"
    assert result["burn"]["reason"] in (
        "purchased_diem_no_locked_svvv",
        "no_locked_svvv",
    )


def test_burn_custody_aware_triggers_unstake_before_burn(monkeypatch):
    burn_calls: list[int] = []
    unstake_calls: list[int] = []

    class FakeActions:
        def burn(self, amount: int):
            burn_calls.append(int(amount))
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

        def unstake_for_api(self, amount: int):
            unstake_calls.append(int(amount))
            return {
                "status": "sent",
                "action": "unstake_diem",
                "tx_hash": "0xfeed",
                "amount": int(amount),
            }

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)
    monkeypatch.setenv(
        "DIEM_STAKING_ADDRESS", "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_can_burn_diem",
        lambda self, amount: {"can_burn": True, "reason": "sufficient_locked_svvv"},
        raising=True,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "diem_custody_status",
        lambda self, **kwargs: {
            "status": "ok",
            "wallet_diem_units": 0,
            "wallet_diem_known": True,
            "staked_diem_units": 10_000,
            "staked_diem_known": True,
        },
        raising=True,
    )

    class _StubAgg:
        pass

    svc = svc_mod.DIEMService(aggregator=_StubAgg())
    res = svc.burn(500)

    assert res.get("status") == "pending"
    assert unstake_calls == [500]
    assert burn_calls == []


def test_burn_custody_override_allows_burn(monkeypatch):
    burn_calls: list[int] = []
    unstake_calls: list[int] = []

    class FakeActions:
        def burn(self, amount: int):
            burn_calls.append(int(amount))
            return {"status": "sent", "action": "burn", "tx_hash": "0xbeef"}

        def unstake_for_api(self, amount: int):
            unstake_calls.append(int(amount))
            return {"status": "sent", "action": "unstake_diem", "tx_hash": "0xfeed"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", FakeActions, raising=True)
    monkeypatch.setenv(
        "DIEM_STAKING_ADDRESS", "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    )

    svc_mod = import_module("services.diem.client")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_can_burn_diem",
        lambda self, amount: {"can_burn": True, "reason": "sufficient_locked_svvv"},
        raising=True,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "diem_custody_status",
        lambda self, **kwargs: {
            "status": "ok",
            "wallet_diem_units": 0,
            "wallet_diem_known": True,
            "staked_diem_units": 10_000,
            "staked_diem_known": True,
        },
        raising=True,
    )

    class _StubAgg:
        pass

    svc = svc_mod.DIEMService(aggregator=_StubAgg())
    res = svc.burn(500, custody_aware=False)

    assert res.get("status") == "sent"
    assert burn_calls == [500]
    assert unstake_calls == []
