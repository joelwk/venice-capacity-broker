"""Unit tests for DIEM buy amount_in sanity checks."""

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


class SanityAgg:
    def __init__(self, slot0_amount_in: int, best_quote_out: int | None = None):
        self.slot0_amount_in = int(slot0_amount_in)
        self.best_quote_out = best_quote_out
        self.exact_in_calls: list[dict[str, object]] = []
        self.exact_out_calls: list[dict[str, object]] = []
        self.best_quote_calls: list[dict[str, object]] = []
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
        amount_out = (
            int(self.best_quote_out)
            if self.best_quote_out is not None
            else int(amount_in) * 10**12
        )
        self.best_quote_calls.append(
            {
                "amount_in": int(amount_in),
                "route": route,
                "allowed_providers": allowed_providers,
            }
        )
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
        return {"tx_hash": "0x1", "provider": "test"}

    def trade_best_exact_out(
        self,
        amount_out: int,
        slippage_bps: int,
        route: object,
        allowed_providers=None,
    ):
        self.exact_out_calls.append(
            {
                "amount_out": int(amount_out),
                "slippage_bps": int(slippage_bps),
                "route": route,
                "allowed_providers": allowed_providers,
            }
        )
        return {"tx_hash": "0x2", "provider": "test"}


def _bind_trade_routes(svc, routes):
    bound = (lambda self, force_dynamic=False: routes).__get__(svc, type(svc))
    return bound


def _build_service(monkeypatch, agg, market):
    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=agg, market_data=market)

    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
    quote_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()
    route = make_route([diem_addr or "0xdiem", quote_addr or "0xusdc"])

    monkeypatch.setattr(
        svc, "trade_routes", _bind_trade_routes(svc, [route]), raising=False
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
    return svc


def _enable_amount_in_sanity(monkeypatch):
    monkeypatch.setenv("VENICE_OFFLINE_SIGNALS", "0")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)


def _base_env(monkeypatch):
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_BUFFER_BPS", "0")


def test_amount_in_sanity_passes_when_ratio_below_threshold(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "1")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_THRESHOLD", "2.0")

    agg = SanityAgg(slot0_amount_in=2_000_000)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})
    svc = _build_service(monkeypatch, agg, market)

    _enable_amount_in_sanity(monkeypatch)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    assert len(agg.exact_in_calls) == 1


def test_amount_in_sanity_aborts_when_ratio_exceeds_threshold(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "1")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_THRESHOLD", "2.0")

    agg = SanityAgg(slot0_amount_in=500_000)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})
    svc = _build_service(monkeypatch, agg, market)

    _enable_amount_in_sanity(monkeypatch)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "skipped"
    assert result["reason"] == "amount_in_sanity_failed"
    assert not agg.exact_in_calls


def test_amount_in_sanity_disabled_when_flag_off(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "0")

    agg = SanityAgg(slot0_amount_in=500_000)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})
    svc = _build_service(monkeypatch, agg, market)

    _enable_amount_in_sanity(monkeypatch)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    assert len(agg.exact_in_calls) == 1


def test_scaling_loop_respects_cumulative_max_scale(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "0")
    monkeypatch.setenv("DIEM_BUY_EXACT_IN_MAX_CUMULATIVE_SCALE", "1.1")

    agg = SanityAgg(slot0_amount_in=2_000_000)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})
    svc = _build_service(monkeypatch, agg, market)

    _enable_amount_in_sanity(monkeypatch)

    agg.best_quote_out = 10**17

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    assert not agg.exact_in_calls
    assert len(agg.exact_out_calls) == 1


def test_direct_slot0_quote_used_as_baseline(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_ENABLE", "1")
    monkeypatch.setenv("DIEM_BUY_AMOUNT_IN_SANITY_THRESHOLD", "5.0")
    monkeypatch.setenv("DIEM_BUY_QUOTE_VALIDATION_ENABLE", "0")

    agg = SanityAgg(slot0_amount_in=2_000_000)
    market = MarketStub({"USDC": 1.0, "DIEM": 2.0})
    svc = _build_service(monkeypatch, agg, market)

    _enable_amount_in_sanity(monkeypatch)

    result = svc.trade("buy", 10**18, slippage_bps=50)

    assert result["status"] == "sent"
    assert agg.best_quote_exact_out_calls
    match = next(
        (
            entry
            for entry in agg.best_quote_exact_out_calls
            if entry.get("allowed_providers") == ["aerodrome_cl"]
        ),
        None,
    )
    assert match is not None
    tokens = list(getattr(match["route"], "tokens", []))
    assert len(tokens) == 2
