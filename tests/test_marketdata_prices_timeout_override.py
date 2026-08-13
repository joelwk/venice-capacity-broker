import time


def test_marketdata_prices_timeout_override(monkeypatch):
    # Import inside test to avoid side effects during collection.
    from services.marketdata.provider import MarketDataProvider

    provider = MarketDataProvider()

    # Force misses so the batch uses the thread pool path.
    monkeypatch.setattr(provider, "_cache_price_get", lambda *args, **kwargs: None)
    monkeypatch.setattr(provider, "_cache_price_set", lambda *args, **kwargs: None)

    def slow_price(sym: str):
        # One fast, one slow to ensure timeout_s affects returned payload.
        if sym == "FAST":
            time.sleep(0.05)
            return 123.0
        if sym == "SLOW":
            time.sleep(1.0)
            return 456.0
        return 0.0

    monkeypatch.setattr(provider, "_price_for_symbol", slow_price)
    monkeypatch.setattr(provider, "_valid_price", lambda value: float(value) > 0.0)

    # Tight timeout should return partial results (timeout mode).
    out_fast = provider.prices(["FAST", "SLOW"], timeout_s=0.2)
    assert out_fast.get("FAST") == 123.0
    assert "SLOW" not in out_fast

    # Larger timeout should allow both to complete.
    out_full = provider.prices(["FAST", "SLOW"], timeout_s=2.0)
    assert out_full.get("FAST") == 123.0
    assert out_full.get("SLOW") == 456.0
