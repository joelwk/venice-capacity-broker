#!/usr/bin/env python
"""Test price normalization with deterministic market data overrides."""

import os

os.environ['VVV_TOKEN_ADDRESS'] = '0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf'
os.environ['DIEM_TOKEN_ADDRESS'] = '0xf4d97f2da56e8c3098f3a8d538db630a2606a024'
os.environ['QUOTE_TOKEN_ADDRESS'] = '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913'
os.environ['WETH_ADDRESS'] = '0x4200000000000000000000000000000000000006'
os.environ['TRADE_PATH'] = ','.join([
    os.environ['DIEM_TOKEN_ADDRESS'],
    os.environ['WETH_ADDRESS'],
    os.environ['QUOTE_TOKEN_ADDRESS'],
])

from libs.dex.routes import as_route_plan
from services.marketdata.provider import MarketDataProvider


class DemoProvider(MarketDataProvider):
    """Inject deterministic responses to demonstrate normalized outputs."""

    def __init__(self) -> None:
        super().__init__()
        self._vvv_metrics_cache = None
        self._vvv_metrics_cache_t = 0.0

    def _erc20_decimals(self, address: str) -> int:  # type: ignore[override]
        if address.lower() == os.environ['QUOTE_TOKEN_ADDRESS'].lower():
            return 6
        return 18

    def diem_price_with_fallback(self) -> float:  # type: ignore[override]
        return 227.25

    def best_price(self, path, amount_in_decimal: float = 1.0, **kwargs):  # type: ignore[override]
        lower = [p.lower() for p in as_route_plan(path).tokens]
        if lower == [os.environ['VVV_TOKEN_ADDRESS'].lower(), os.environ['QUOTE_TOKEN_ADDRESS'].lower()]:
            return {"provider": "demo", "price": 2.63}
        if lower == [os.environ['WETH_ADDRESS'].lower(), os.environ['QUOTE_TOKEN_ADDRESS'].lower()]:
            return {"provider": "demo", "price": 3200.0}
        raise RuntimeError(f"Unexpected path {path}")


def main() -> None:
    print("=== Price Normalization Demo ===\n")

    provider = DemoProvider()
    symbols = ["VVV", "DIEM", "ETH", "USDC"]
    prices = provider.prices(symbols)

    for symbol in symbols:
        print(f"{symbol}: ${prices[symbol]:.2f}")

    print("\nRatios vs DIEM:")
    for symbol in symbols:
        if symbol == "DIEM":
            continue
        print(f"{symbol}/DIEM: {prices[symbol] / prices['DIEM']:.4f}")


if __name__ == "__main__":
    main()
