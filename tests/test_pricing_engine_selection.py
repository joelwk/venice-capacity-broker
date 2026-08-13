from __future__ import annotations

import pytest


class _FakeMarketDataProvider:
    def prices(self, symbols):
        # Keep deterministic, plausible values.
        out = {}
        for sym in symbols:
            key = str(sym).upper()
            if key == "DIEM":
                out[sym] = 200.0
            elif key == "ETH":
                out[sym] = 4000.0
            elif key == "USDC":
                out[sym] = 1.0
            elif key == "WBTC":
                out[sym] = 100000.0
            elif key == "VVV":
                out[sym] = 2.0
        return out

    def last_prices_stats(self):
        return {}

    def price_health(self, symbol: str, max_age: float = 180.0):
        return {"symbol": symbol, "valid": True, "source": "prefetch", "value": 200.0}

    def diem_price_with_fallback(self):
        return 200.0


def test_pricing_service_defaults_to_market_when_static_not_configured(monkeypatch):
    # Unset explicit engine selection and static unit pricing.
    monkeypatch.delenv("PRICE_ENGINE", raising=False)
    monkeypatch.delenv("PRICE_UNIT_ETH_WEI", raising=False)
    monkeypatch.delenv("PRICE_UNIT_USDC", raising=False)
    # Keep warmup fast and deterministic.
    monkeypatch.setenv("PRICING_WARMUP_TIMEOUT_SECONDS", "0.25")
    monkeypatch.setenv("PRICING_LOCK_TIMEOUT_SECONDS", "0.25")

    # Avoid real network calls.
    monkeypatch.setattr(
        "services.marketdata.provider.MarketDataProvider",
        lambda: _FakeMarketDataProvider(),
        raising=False,
    )

    from services.pricing.service import PricingService

    svc = PricingService()
    quote = svc.get_quote(units=1.0, asset="USDC")
    assert quote["asset"] == "USDC"
    assert pytest.approx(float(quote["units"]), rel=1e-9) == 1.0
    assert int(quote["unitPrice"]) > 0
    assert int(quote["totalPrice"]) > 0


def test_pricing_service_defaults_to_static_when_unit_price_configured(monkeypatch):
    monkeypatch.delenv("PRICE_ENGINE", raising=False)
    monkeypatch.setenv("PRICE_UNIT_USDC", "250000")  # $0.25 per unit

    from services.pricing.service import PricingService

    svc = PricingService()
    quote = svc.get_quote(units=2.0, asset="USDC")
    assert quote["asset"] == "USDC"
    assert int(quote["unitPrice"]) > 0
    assert int(quote["totalPrice"]) > 0
