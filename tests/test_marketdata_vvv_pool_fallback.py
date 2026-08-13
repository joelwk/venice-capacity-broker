from __future__ import annotations


def test_marketdata_vvv_prefers_configured_pool_price(monkeypatch):
    monkeypatch.setenv("MARKETDATA_INIT_LIGHT", "1")
    # Minimal env required for MarketDataProvider init
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xF4D97F2Da56E8C3098F3A8D538Db630A2606A024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )
    monkeypatch.setenv(
        "VVV_USDC_POOL_ADDRESS", "0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530"
    )

    from services.marketdata import provider as provider_mod
    from services.marketdata.provider import MarketDataProvider

    # Force deterministic pool-derived VVV/USD without touching web3/DEX quotes.
    monkeypatch.setattr(
        provider_mod, "vvv_usd_price", lambda _cfg: 1.2345, raising=True
    )

    md = MarketDataProvider()
    prices = md.prices(["VVV"])
    assert abs(float(prices["VVV"]) - 1.2345) < 1e-12

    health = md.price_health("VVV")
    assert health.get("source") in {"vvv_usdc_pool", "unknown"}
