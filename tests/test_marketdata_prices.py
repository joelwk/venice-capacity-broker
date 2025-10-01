from __future__ import annotations

import math

from libs.dex.routes import as_route_plan


def test_prices_normalized_without_heuristics(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("WETH_ADDRESS", weth_addr)
    monkeypatch.setenv("TRADE_PATH", ",".join([diem_addr, weth_addr, quote_addr]))

    monkeypatch.setattr(MarketDataProvider, "_fetch_external_price", lambda self, symbol: None, raising=False)
    monkeypatch.setattr(MarketDataProvider, "_external_price_ttl", lambda self: 0.0, raising=False)

    MarketDataProvider._price_cache.clear()
    md = MarketDataProvider()

    def fake_decimals(address: str) -> int:
        addr = address.lower()
        if addr == quote_addr.lower():
            return 6
        return 18

    monkeypatch.setattr(md, "_erc20_decimals", fake_decimals)
    monkeypatch.setattr(md, "diem_price_with_fallback", lambda: 227.25)

    def fake_best_price(route, amount_in_decimal: float = 1.0, **kwargs):  # type: ignore[override]
        plan = as_route_plan(route)
        tokens = [p.lower() for p in plan.tokens]
        shortcut = [vvv_addr.lower(), quote_addr.lower()]
        via_weth = [vvv_addr.lower(), weth_addr.lower(), quote_addr.lower()]
        weth_quote = [weth_addr.lower(), quote_addr.lower()]
        if tokens == shortcut or tokens == via_weth:
            return {"provider": "stub", "price": 2.63}
        if tokens == weth_quote:
            return {"provider": "stub", "price": 3200.0}
        raise RuntimeError(f"Unexpected path {plan.tokens}")

    monkeypatch.setattr(md, "best_price", fake_best_price)
    monkeypatch.setenv(
        "VVV_PRICE_PATH",
        f"{vvv_addr}@3000,{weth_addr}@500,{quote_addr}",
    )

    prices = md.prices(["VVV", "DIEM", "ETH", "USDC"])

    assert math.isclose(prices["DIEM"], 227.25, rel_tol=1e-6)
    assert math.isclose(prices["VVV"], 2.63, rel_tol=1e-6)
    assert math.isclose(prices["ETH"], 3200.0, rel_tol=1e-6)
    assert prices["USDC"] == 1.0

def test_price_health_tracks_clamp(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    provider = MarketDataProvider()
    type(provider)._last_price_sources.clear()
    type(provider)._price_clamp_events.clear()

    monkeypatch.setattr(provider, "_external_price", lambda symbol: 1.0, raising=False)

    provider._apply_price_sanity("DIEM", 200.0)

    health = provider.price_health("DIEM", max_age=999)
    assert health["clamped"] is True
    assert str(health.get("source") or "").startswith("external")
    assert health.get("clamp_reason") == "drift"

