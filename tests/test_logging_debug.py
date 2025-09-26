from __future__ import annotations

from typing import List

import pytest

from libs.dex.providers import DexAggregator
from libs.dex.routes import make_route
from services.diem.client import DIEMService
from services.marketdata.provider import MarketDataProvider


class _StubProvider:
    def _collect_trade_paths(self) -> List:
        return []

    def _parse_route_spec(self, raw: str):
        tokens = [token.strip() for token in raw.split(",") if token.strip()]
        return make_route(tokens)

    def _address_for_symbol(self, symbol: str) -> str | None:
        symbols = {
            "DIEM": "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            "USDC": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        }
        return symbols.get(symbol.upper())


def test_diem_trade_routes_logs_when_debug_enabled(monkeypatch, caplog):
    monkeypatch.setenv("DIEM_DEBUG_ROUTES", "1")
    monkeypatch.setenv("TRADE_PATH", "diem,usdc")

    svc = DIEMService(aggregator=None, market_data=_StubProvider())

    with caplog.at_level("INFO", logger="services.diem.client"):
        routes = svc.trade_routes()

    joined = " ".join(caplog.messages)
    assert "DIEM trade_routes raw" in joined
    assert "DIEM trade_routes resolved" in joined
    assert "DIEM trade_routes selected" in joined
    assert routes, "trade_routes should return at least one route"


def test_dex_aggregator_no_quotes_logs_route(monkeypatch):
    monkeypatch.setenv("DIEM_DEBUG_ROUTES", "1")
    import libs.dex.providers as providers_mod

    records: list[str] = []

    class CaptureLogger:
        def warning(self, msg, *args, **kwargs):
            records.append(msg % args if args else msg)

        def info(self, msg, *args, **kwargs):
            records.append(msg % args if args else msg)

    monkeypatch.setattr(providers_mod, "_logger", CaptureLogger())

    agg = DexAggregator([])
    diem_path = [
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        "0x4200000000000000000000000000000000000006",
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
    ]
    with pytest.raises(RuntimeError):
        agg.trade_best(123, 100, make_route(diem_path))

    combined = " ".join(records)
    assert "dex aggregator no quotes route" in combined
    for token in diem_path:
        assert token in combined
    assert "amount_in=123" in combined


def test_marketdata_price_sanity_logs_details(monkeypatch):
    monkeypatch.setenv("MARKETDATA_DEBUG_SANITY", "1")
    provider = object.__new__(MarketDataProvider)
    provider._record_counter = lambda *args, **kwargs: None  # type: ignore[attr-defined]
    provider._norm_symbol_label = lambda symbol: symbol.upper()  # type: ignore[attr-defined]
    provider._active_stats = None  # type: ignore[attr-defined]
    provider._external_price = lambda symbol: 2.0  # type: ignore[attr-defined]

    import services.marketdata.provider as provider_mod

    records: list[str] = []

    class CaptureLogger:
        def warning(self, msg, *args, **kwargs):
            records.append(msg % args if args else msg)

        def info(self, msg, *args, **kwargs):
            records.append(msg % args if args else msg)

        def debug(self, msg, *args, **kwargs):
            records.append(msg % args if args else msg)

    monkeypatch.setattr(provider_mod, "_logger", CaptureLogger())

    result = provider._apply_price_sanity("DIEM", 1.0)

    assert result == pytest.approx(2.0)
    combined = " ".join(records)
    assert "price sanity: clamp applied" in combined
    assert "symbol=DIEM" in combined
