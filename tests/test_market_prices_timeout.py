import time

import pytest

from services.marketdata.provider import MarketDataProvider


@pytest.mark.parametrize("symbol_count", [1, 3])
def test_marketdata_prices_timeouts_return_quickly(monkeypatch, symbol_count):
    monkeypatch.setenv("MARKETDATA_PRICES_TIMEOUT_SECONDS", "0.1")
    provider = MarketDataProvider()

    call_count = 0

    def slow_price(self, symbol: str):
        nonlocal call_count
        call_count += 1
        time.sleep(0.3)
        return 123.0

    monkeypatch.setattr(
        MarketDataProvider, "_price_for_symbol", slow_price, raising=False
    )

    symbols = ["DIEM"] + [f"TOKEN{i}" for i in range(symbol_count)]
    start = time.time()
    result = provider.prices(symbols)
    duration = time.time() - start

    assert duration < 0.25
    assert result == {}
    assert call_count == len(symbols)
