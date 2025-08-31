from __future__ import annotations

from services.marketdata.provider import MarketDataProvider


def observe_markets(provider: MarketDataProvider) -> dict:
    prices = provider.prices(["VVV", "DIEM"])
    return {"prices": prices}

