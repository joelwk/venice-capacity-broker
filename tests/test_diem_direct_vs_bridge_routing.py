"""Integration tests for direct vs bridge DIEM routing."""

from __future__ import annotations

import os
from importlib import import_module

from libs.dex.providers import Quote
from libs.dex.routes import make_route


class MarketStub:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def prices(self, symbols):
        return {sym: float(self._prices.get(sym, 0.0)) for sym in symbols}

    def price(self, symbol: str) -> float:
        return float(self._prices.get(symbol, 0.0))

    def route_metadata(self, plan):
        return {}


class RecordingAgg:
    def __init__(self):
        self.exact_in_calls: list[dict[str, object]] = []

    def best_quote_exact_out(
        self, amount_out: int, route: object, allowed_providers=None
    ):
        amount_in = max(1, int(amount_out / 5e11))
        return Quote(
            provider="test",
            amount_in=int(amount_in),
            amount_out=int(amount_out),
            route=route,
        )

    def best_quote(self, amount_in: int, route: object, allowed_providers=None):
        amount_out = int(amount_in) * 10**12
        return Quote(
            provider="test",
            amount_in=int(amount_in),
            amount_out=int(amount_out),
            route=route,
        )

    def trade_best_exact_in(
        self,
        amount_in: int,
        slippage_bps: int,
        route: object,
        allowed_providers=None,
    ):
        self.exact_in_calls.append(
            {
                "amount_in": int(amount_in),
                "slippage_bps": int(slippage_bps),
                "route": route,
                "allowed_providers": allowed_providers,
            }
        )
        return {"tx_hash": "0xabc", "provider": "test"}


def _bind_trade_routes(svc, routes):
    return (lambda self, force_dynamic=False: routes).__get__(svc, type(svc))


def _direct_and_bridge_routes():
    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "0xdiem").lower()
    quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "0xusdc").lower()
    vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "0xvvv").lower()
    direct = make_route([diem_addr, quote_addr])
    bridge = make_route([diem_addr, vvv_addr, quote_addr])
    return direct, bridge


def test_direct_route_selected_when_direct_only(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_DIRECT_ONLY", "1")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub({}))

    direct, bridge = _direct_and_bridge_routes()
    monkeypatch.setattr(
        svc, "_trade_routes", _bind_trade_routes(svc, [bridge, direct]), raising=False
    )

    routes = svc.trade_routes()
    assert any(len(getattr(r, "tokens", [])) == 2 for r in routes)
    assert all(len(getattr(r, "tokens", [])) != 3 for r in routes)


def test_direct_route_execution_uses_aerodrome_cl(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_QUOTE_VALIDATION_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")
    monkeypatch.setenv("AERODROME_CL_ROUTER_ADDRESS", "0xcl")
    monkeypatch.setenv("DIEM_USDC_POOL_ADDRESS", "0xpool")
    monkeypatch.setenv("DIEM_USDC_TICK_SPACING", "100")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")

    agg = RecordingAgg()
    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(
        aggregator=agg, market_data=MarketStub({"USDC": 1, "DIEM": 2})
    )

    direct, _bridge = _direct_and_bridge_routes()
    monkeypatch.setattr(
        svc, "trade_routes", _bind_trade_routes(svc, [direct]), raising=False
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

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    assert agg.exact_in_calls
    assert agg.exact_in_calls[0]["allowed_providers"] == ["aerodrome_cl"]


def test_bridge_route_avoided_when_direct_liquidity_present(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_QUOTE_VALIDATION_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")
    monkeypatch.setenv("AERODROME_CL_ROUTER_ADDRESS", "0xcl")
    monkeypatch.setenv("DIEM_USDC_POOL_ADDRESS", "0xpool")
    monkeypatch.setenv("DIEM_USDC_TICK_SPACING", "100")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")

    agg = RecordingAgg()
    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(
        aggregator=agg, market_data=MarketStub({"USDC": 1, "DIEM": 2})
    )

    direct, bridge = _direct_and_bridge_routes()
    monkeypatch.setattr(
        svc, "trade_routes", _bind_trade_routes(svc, [bridge, direct]), raising=False
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

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    assert len(agg.exact_in_calls) == 1
    tokens = list(getattr(agg.exact_in_calls[0]["route"], "tokens", []))
    tokens_lower = [str(tok).lower() for tok in tokens]
    quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").lower()
    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").lower()
    assert tokens_lower == [quote_addr, diem_addr]


def test_trade_amount_matches_direct_quote_within_tolerance(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_QUOTE_VALIDATION_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")

    agg = RecordingAgg()
    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(
        aggregator=agg, market_data=MarketStub({"USDC": 1, "DIEM": 2})
    )

    direct, _bridge = _direct_and_bridge_routes()
    monkeypatch.setattr(
        svc, "trade_routes", _bind_trade_routes(svc, [direct]), raising=False
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

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    amount_in = agg.exact_in_calls[0]["amount_in"]
    expected = 2_000_000
    diff = abs(amount_in - expected) / expected
    assert diff <= 0.05
