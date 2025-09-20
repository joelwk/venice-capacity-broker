from __future__ import annotations

import math

import pytest

from libs.pricing.engine import MarketPricingEngine


class _FakeMarketData:
    def __init__(self, diem: float = 200.0, eth: float = 4000.0) -> None:
        self._diem = diem
        self._eth = eth

    def prices(self, symbols):  # noqa: D401
        out = {}
        for sym in symbols:
            key = sym.upper()
            if key == "DIEM":
                out[sym] = self._diem
            elif key == "ETH":
                out[sym] = self._eth
            elif key == "USDC":
                out[sym] = 1.0
        return out

    def diem_price_with_fallback(self):  # noqa: D401
        return self._diem


def _patch_marketdata(monkeypatch, diem: float = 200.0, eth: float = 4000.0) -> None:
    fake = _FakeMarketData(diem=diem, eth=eth)
    monkeypatch.setattr(
        "services.marketdata.provider.MarketDataProvider",
        lambda: fake,
        raising=False,
    )


def test_budget_eth_converts_to_usd(monkeypatch):
    _patch_marketdata(monkeypatch, diem=220.0, eth=4400.0)
    eng = MarketPricingEngine()
    draft = eng.price_from_budget(0.25, "ETH")
    # 0.25 ETH * 4400 USD/ETH = 1100 USD budget, /220 = 5 units
    assert math.isclose(draft.units, 5.0, rel_tol=1e-6)
    assert draft.total_price > 0


def test_budget_too_small_triggers_min(monkeypatch):
    _patch_marketdata(monkeypatch, diem=220.0, eth=4400.0)
    eng = MarketPricingEngine()
    with pytest.raises(ValueError, match="budget must cover at least"):
        eng.price_from_budget(0.0001, "ETH")


def test_budget_eth_requires_price(monkeypatch):
    _patch_marketdata(monkeypatch, diem=220.0, eth=0.0)
    eng = MarketPricingEngine()
    with pytest.raises(ValueError, match="ETH pricing unavailable"):
        eng.price_from_budget(0.25, "ETH")
