from __future__ import annotations

import math


def test_prices_normalized_without_heuristics(monkeypatch):
    from services.marketdata.provider import MarketDataProvider

    diem_addr = "0xF4d861575ecc9493420A3f5a14F85B13f0b50EB3"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    quote_addr = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    weth_addr = "0x4200000000000000000000000000000000000006"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("WETH_ADDRESS", weth_addr)
    monkeypatch.setenv("TRADE_PATH", ",".join([diem_addr, weth_addr, quote_addr]))

    md = MarketDataProvider()

    def fake_decimals(address: str) -> int:
        addr = address.lower()
        if addr == quote_addr.lower():
            return 6
        return 18

    monkeypatch.setattr(md, "_erc20_decimals", fake_decimals)
    monkeypatch.setattr(md, "diem_price_with_fallback", lambda: 227.25)

    def fake_best_price(path, amount_in_decimal: float = 1.0):  # type: ignore[override]
        if [p.lower() for p in path] == [vvv_addr.lower(), quote_addr.lower()]:
            return {"provider": "stub", "price": 2.63}
        if [p.lower() for p in path] == [weth_addr.lower(), quote_addr.lower()]:
            return {"provider": "stub", "price": 3200.0}
        raise RuntimeError(f"Unexpected path {path}")

    monkeypatch.setattr(md, "best_price", fake_best_price)

    prices = md.prices(["VVV", "DIEM", "ETH", "USDC"])

    assert math.isclose(prices["DIEM"], 227.25, rel_tol=1e-6)
    assert math.isclose(prices["VVV"], 2.63, rel_tol=1e-6)
    assert math.isclose(prices["ETH"], 3200.0, rel_tol=1e-6)
    assert prices["USDC"] == 1.0
