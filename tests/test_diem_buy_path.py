from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from libs.dex.providers import AerodromeCLDexProvider, DexAggregator, Quote
from libs.dex.routes import as_route_plan, make_route
from services.diem.client import DIEMService
from services.diem.execution import ExecutionIntent, ExecutionResult, ExecutionStatus


def test_diem_service_buy_uses_aggregator_if_available(monkeypatch):
    svc_mod = import_module("services.diem.client")

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv("TRADE_PATH", f"{diem_addr},{usdc_addr}")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 10**9,
        raising=True,
    )

    # Fake aggregator with exact-in support (preferred for DIEM buys).
    class FakeAgg:
        def quote_all(self, amount_in, path):
            plan = as_route_plan(path)
            return [
                Quote(
                    provider="fake",
                    amount_in=int(amount_in),
                    amount_out=1000,
                    route=plan,
                )
            ]

        def best_quote(self, amount_in: int, route, *, allowed_providers=None):
            plan = as_route_plan(route)
            return Quote(
                provider="fake",
                amount_in=int(amount_in),
                amount_out=1000,
                route=plan,
            )

        def trade_best(self, *_a, **_k):
            return {"provider": "fake", "tx_hash": "0xagg"}

    route = make_route([diem_addr, usdc_addr])
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, force_dynamic=False: [route],
        raising=True,
    )

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    res = svc.trade("buy", 123)
    assert res.get("tx_hash") == "0xagg"


def test_direct_diem_usdc_best_quote_prefers_aerodrome_cl(monkeypatch):
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    router_addr = "0x" + "1" * 40
    pool_addr = "0x" + "2" * 40

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv("AERODROME_CL_ROUTER_ADDRESS", router_addr)
    monkeypatch.setenv("DIEM_USDC_POOL_ADDRESS", pool_addr)
    monkeypatch.setenv("DIEM_USDC_TICK_SPACING", "100")
    monkeypatch.setenv("DEX_EXEC_PROVIDERS", "aerodrome_cl")

    class RouterFnStub:
        def exactInputSingle(self, _params):
            class Tx:
                def build_transaction(self, _tx):
                    return {"data": b"\x01"}

            return Tx()

    class RouterStub:
        def __init__(self):
            self.functions = RouterFnStub()

    router_stub = RouterStub()

    def fake_refresh(self):
        self.w3 = object()
        self.router = router_stub

    monkeypatch.setattr(
        AerodromeCLDexProvider, "_refresh_provider", fake_refresh, raising=True
    )
    monkeypatch.setattr(
        AerodromeCLDexProvider, "_ensure_allowance", lambda *a, **k: None, raising=True
    )

    aerodrome_cl = AerodromeCLDexProvider(router_addr, pool_addr, 100)
    other = SimpleNamespace(
        name="uniswap_v3",
        supports_exact_out=True,
        quote=MagicMock(return_value=None),
        trade=MagicMock(),
    )
    agg = DexAggregator([aerodrome_cl, other])
    route = make_route([diem_addr, usdc_addr])

    with patch("libs.dex.diem_fallbacks.diem_usdc_slot0_quote") as mock_slot0:
        mock_slot0.return_value = Quote(
            provider="aerodrome_cl",
            amount_in=1_000_000,
            amount_out=2_000_000,
            route=route,
        )
        quote = agg.best_quote(1_000_000, route, allowed_providers=["aerodrome_cl"])

    assert quote is not None
    assert quote.provider == "aerodrome_cl"
    assert other.quote.call_count == 0


def test_direct_diem_usdc_trade_best_executes_aerodrome_cl(monkeypatch):
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    router_addr = "0x" + "3" * 40
    pool_addr = "0x" + "4" * 40

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv("AERODROME_CL_ROUTER_ADDRESS", router_addr)
    monkeypatch.setenv("DIEM_USDC_POOL_ADDRESS", pool_addr)
    monkeypatch.setenv("DIEM_USDC_TICK_SPACING", "100")
    monkeypatch.setenv("DEX_EXEC_PROVIDERS", "aerodrome_cl")

    class RouterFnStub:
        def exactInputSingle(self, _params):
            class Tx:
                def build_transaction(self, _tx):
                    return {"data": b"\x02"}

            return Tx()

    class RouterStub:
        def __init__(self):
            self.functions = RouterFnStub()

    router_stub = RouterStub()

    def fake_refresh(self):
        self.w3 = object()
        self.router = router_stub

    monkeypatch.setattr(
        AerodromeCLDexProvider, "_refresh_provider", fake_refresh, raising=True
    )
    monkeypatch.setattr(
        AerodromeCLDexProvider,
        "_ensure_allowance",
        lambda *a, **k: "0xapprove",
        raising=True,
    )
    monkeypatch.setattr(
        "libs.dex.providers.get_address",
        lambda: "0x" + "5" * 40,
        raising=True,
    )
    sent = {}

    def fake_send_tx(to_addr, data):
        sent["to"] = to_addr
        sent["data"] = data
        return "0xtx"

    monkeypatch.setattr("libs.dex.providers.send_tx", fake_send_tx, raising=True)

    aerodrome_cl = AerodromeCLDexProvider(router_addr, pool_addr, 100)
    other = SimpleNamespace(
        name="uniswap_v3",
        supports_exact_out=True,
        quote=MagicMock(return_value=None),
        trade=MagicMock(side_effect=AssertionError("unexpected trade")),
    )
    agg = DexAggregator([aerodrome_cl, other])
    route = make_route([diem_addr, usdc_addr])

    with patch("libs.dex.diem_fallbacks.diem_usdc_slot0_quote") as mock_slot0:
        mock_slot0.return_value = Quote(
            provider="aerodrome_cl",
            amount_in=1_000_000,
            amount_out=2_000_000,
            route=route,
        )
        result = agg.trade_best(
            1_000_000, 100, route, allowed_providers=["aerodrome_cl"]
        )

    assert result.get("provider") == "aerodrome_cl"
    assert result.get("tx_hash") == "0xtx"
    assert sent.get("to") == router_addr


def test_direct_diem_usdc_route_prefers_aerodrome_cl_without_metadata(monkeypatch):
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    router_addr = "0x" + "6" * 40
    pool_addr = "0x" + "7" * 40

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv("TRADE_PATH", f"{diem_addr},{usdc_addr}")
    monkeypatch.setenv("AERODROME_CL_ROUTER_ADDRESS", router_addr)
    monkeypatch.setenv("DIEM_USDC_POOL_ADDRESS", pool_addr)
    monkeypatch.setenv("DIEM_USDC_TICK_SPACING", "100")
    monkeypatch.setenv("DEX_PROVIDERS", "aerodrome_cl")
    monkeypatch.setenv("DEX_EXEC_PROVIDERS", "aerodrome_cl")

    class AggStub:
        _execution_provider_names = ["aerodrome_cl"]

    svc = DIEMService(aggregator=AggStub())
    route = make_route([diem_addr, usdc_addr])
    preferred = svc._preferred_providers_for_route(route)

    assert preferred == ["aerodrome_cl"]


def test_diem_service_buy_falls_back_to_actions(monkeypatch):
    calls = []

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc_addr)
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv("TRADE_PATH", f"{diem_addr},{usdc_addr}")

    class FakeActions:
        def trade(self, side: str, amount: int):
            calls.append((side, amount))
            return {"provider": "actions", "tx_hash": "0xact"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    svc_mod = import_module("services.diem.client")

    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 10**9,
        raising=True,
    )

    class NoAgg:
        pass

    route = make_route([diem_addr, usdc_addr])
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, force_dynamic=False: [route],
        raising=True,
    )

    svc = svc_mod.DIEMService(aggregator=NoAgg())
    res = svc.trade("buy", 555)
    # With no aggregator and actions fallback disabled, trade should be skipped
    assert calls == []
    assert res.get("status") == "skipped"


def test_wallet_first_sell_prefers_wallet_inventory(monkeypatch):
    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    wallet_calls: list[int] = []
    mint_calls: list[int] = []

    def fake_execute_trade(intent: ExecutionIntent, simulate: bool = True):
        wallet_calls.append(int(intent.amount_base_units))
        return ExecutionResult(
            status=ExecutionStatus.CONFIRMED,
            intent=intent,
            tx_hash="0xwallet",
        )

    def fake_mint_and_sell_diem(diem_amount: int, **kwargs):
        mint_calls.append(int(diem_amount))
        return {
            "mint": {"status": "sent"},
            "sell": {"status": "submitted", "tx_hash": "0xmint"},
        }

    monkeypatch.setattr(svc, "execute_trade", fake_execute_trade)
    monkeypatch.setattr(svc, "mint_and_sell_diem", fake_mint_and_sell_diem)

    snapshot = {"balances": {"DIEM": {"units": 60, "decimals": 18}}}
    res = svc.wallet_first_mint_and_sell(
        diem_amount=100, simulate=False, portfolio_snapshot=snapshot
    )

    assert res["internal"]["used_wallet_diem"] == 60
    assert res["internal"]["minted_for_sell"] == 40
    assert wallet_calls == [60]
    assert mint_calls == [40]
    assert res["status"] == "submitted"


def test_buy_trade_uses_reversed_route_once(monkeypatch):
    svc_mod = import_module("services.diem.client")

    quote_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_out")
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6F7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv("TRADE_PATH", f"{diem_addr},{weth_addr},{quote_addr}")
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_get_input_token_balance",
        lambda self, token: 10**9,
        raising=True,
    )

    base_route = make_route([diem_addr, weth_addr, quote_addr], [3000, 500])

    class RouteAwareAgg:
        def __init__(self):
            self.quote_routes: list[tuple[str, ...]] = []
            self.trade_routes: list[tuple[str, ...]] = []
            self.providers: list = []

        def quote_all_exact_out(self, amount_out, path, **_kwargs):
            plan = as_route_plan(path)
            self.quote_routes.append(tuple(plan.tokens))
            return [
                Quote(
                    provider="stub",
                    amount_in=amount_out * 2,
                    amount_out=amount_out,
                    route=plan,
                )
            ]

        def trade_best_exact_out(self, amount_out, max_in_bps, route, **_kwargs):
            plan = as_route_plan(route)
            self.trade_routes.append(tuple(plan.tokens))
            return {
                "provider": "stub",
                "tx_hash": "0xroute",
                "max_amount_in": amount_out * 2,
                "amount_out": amount_out,
                "route": list(plan.tokens),
            }

    agg = RouteAwareAgg()

    # Force trade_routes to return sell-direction path (DIEM -> WETH -> USDC)
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "trade_routes",
        lambda self, force_dynamic=False: [base_route],
        raising=True,
    )

    svc = svc_mod.DIEMService(aggregator=agg)
    res = svc.trade("buy", 100)

    # Routes passed to aggregator should be reversed exactly once (USDC -> WETH -> DIEM)
    assert agg.quote_routes, "quote_all_exact_out should be invoked"
    assert agg.trade_routes, "trade_best_exact_out should be invoked"
    assert agg.quote_routes[0][0].lower() == quote_addr.lower()
    assert agg.quote_routes[0][-1].lower() == diem_addr.lower()
    assert agg.trade_routes[0][0].lower() == quote_addr.lower()
    assert agg.trade_routes[0][-1].lower() == diem_addr.lower()
    assert res.get("tx_hash") == "0xroute"
