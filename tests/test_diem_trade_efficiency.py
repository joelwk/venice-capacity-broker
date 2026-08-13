"""End-to-end trade efficiency checks for DIEM buy flows."""

from __future__ import annotations

import os
from importlib import import_module

from libs.dex.providers import Quote
from libs.dex.routes import make_route
from services.diem.execution import ExecutionIntent, TradeSide


class MarketStub:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    def prices(self, symbols):
        return {sym: float(self._prices.get(sym, 0.0)) for sym in symbols}

    def price(self, symbol: str) -> float:
        return float(self._prices.get(symbol, 0.0))


class PreviewAgg:
    def __init__(self, price_usdc_per_diem: float = 2.0):
        self.price = float(price_usdc_per_diem)
        self._last_quote_diagnostics: list[dict[str, object]] = []

    def quote_all(self, amount_in: int, route: object, allowed_providers=None):
        ratio = int(10**12 / max(self.price, 0.000001))
        amount_out = int(amount_in) * ratio
        return [
            Quote(
                provider="aerodrome_cl",
                amount_in=int(amount_in),
                amount_out=int(amount_out),
                route=route,
            )
        ]


class SanityAgg:
    def __init__(self, slot0_amount_in: int):
        self.slot0_amount_in = int(slot0_amount_in)
        self.best_quote_exact_out_calls: list[dict[str, object]] = []

    def best_quote_exact_out(
        self, amount_out: int, route: object, allowed_providers=None
    ):
        self.best_quote_exact_out_calls.append(
            {
                "amount_out": int(amount_out),
                "route": route,
                "allowed_providers": allowed_providers,
            }
        )
        return Quote(
            provider="aerodrome_cl",
            amount_in=int(self.slot0_amount_in),
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
        return {"tx_hash": "0x1", "provider": "test"}


def _bind_trade_routes(svc, routes):
    return (lambda self, force_dynamic=False: routes).__get__(svc, type(svc))


def _direct_buy_route():
    quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "0xusdc").lower()
    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "0xdiem").lower()
    return make_route([quote_addr, diem_addr])


def test_simulated_trade_amount_within_buffer(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")

    agg = PreviewAgg(price_usdc_per_diem=2.0)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=agg, market_data=market)
    monkeypatch.setattr(
        svc,
        "trade_routes",
        _bind_trade_routes(svc, [_direct_buy_route()]),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_is_route_muted",
        lambda self, route, correlation_id=None, side=None: False,
        raising=False,
    )

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=10**18,
        slippage_bps=50,
    )
    result = svc.execute_trade(intent, simulate=True)

    expected_in = 2_000_000
    assert result.amount_in is not None
    diff = abs(result.amount_in - expected_in) / expected_in
    assert diff <= 0.05


def test_trade_logs_include_amount_in_sanity(monkeypatch, caplog):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "1")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_THRESHOLD", "1.1")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")

    agg = SanityAgg(slot0_amount_in=500_000)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=agg, market_data=market)
    monkeypatch.setattr(
        svc,
        "trade_routes",
        _bind_trade_routes(svc, [_direct_buy_route()]),
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
    monkeypatch.setattr(
        svc,
        "quote",
        lambda *args, **kwargs: {"quotes": [{"amount_in": 1, "amount_out": 1}]},
        raising=False,
    )

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    caplog.set_level("ERROR")
    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["reason"] == "amount_in_sanity_failed"
    assert any("amount_in sanity check failed" in rec.message for rec in caplog.records)


def test_trade_diagnostics_include_quote_comparison(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")

    agg = PreviewAgg(price_usdc_per_diem=2.0)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=agg, market_data=market)
    monkeypatch.setattr(
        svc,
        "trade_routes",
        _bind_trade_routes(svc, [_direct_buy_route()]),
        raising=False,
    )
    monkeypatch.setattr(
        svc_mod.DIEMService,
        "_is_route_muted",
        lambda self, route, correlation_id=None, side=None: False,
        raising=False,
    )

    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=10**18,
        slippage_bps=50,
    )
    result = svc.execute_trade(intent, simulate=True)

    diagnostics = result.diagnostics
    assert diagnostics.get("quote_summary") is not None
    assert diagnostics.get("coherence_preview_price_usd") is not None
    assert diagnostics.get("coherence_market_price_usd") is not None
