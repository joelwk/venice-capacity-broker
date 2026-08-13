from __future__ import annotations

import importlib.util
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi.testclient import TestClient

APP_PATH = Path("apps/broker_api/app.py").resolve()
os.environ.setdefault("BROKER_REQUIRE_ADMIN_TOKEN", "false")
os.environ.setdefault("BROKER_ADMIN_TOKEN", "test-admin")
os.environ.setdefault("VENICE_PARENT_KEY", "parent-test")
spec = importlib.util.spec_from_file_location("apps.broker_api.app", APP_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
module.__package__ = "apps.broker_api"
spec.loader.exec_module(module)
app = module.app


class StubMarketDataProvider:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def prices(self, symbols, ttl_s=None):
        self.calls.append(list(symbols))
        base = {str(sym).upper(): float(index + 1) for index, sym in enumerate(symbols)}
        return base

    def last_prices_stats(self):
        return {
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_hit_rate": 1.0,
            "dex_calls": len(self.calls),
            "duration_seconds": 0.01,
        }

    def price_health(self, symbol: str, max_age: float = 0.0):
        return {"source": "stub", "valid": True, "age": 0, "symbol": symbol}


def _reset_caches():
    module._env_prices_resp_cache.clear()
    module._prices_resp_cache.clear()


def _patch_provider(monkeypatch):
    provider = StubMarketDataProvider()
    monkeypatch.setattr(
        module, "_get_marketdata_provider", lambda status_code=500: provider
    )
    return provider


def test_env_and_prices_cache_includes_meta(monkeypatch):
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "30")
    provider = _patch_provider(monkeypatch)

    monkeypatch.setattr(
        module, "env_status", lambda: {"pricing": {"discounts": {}}, "features": {}}
    )

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan, raising=False)

    client = TestClient(app)
    try:
        _reset_caches()
        initial_calls = len(provider.calls)
        response1 = client.get("/v1/env-and-prices?symbols=DIEM,USDC")
        payload1 = response1.json()
        assert response1.status_code == 200
        assert len(provider.calls) == initial_calls + 1
        meta1 = payload1["meta"]
        assert meta1["cacheHit"] is False
        assert "refreshedAt" in meta1

        response2 = client.get("/v1/env-and-prices?symbols=DIEM,USDC")
        payload2 = response2.json()
        assert response2.status_code == 200
        assert len(provider.calls) == initial_calls + 1  # cached
        meta2 = payload2["meta"]
        assert meta2["cacheHit"] is True
        assert meta2["cacheAgeMs"] >= 0
        assert meta2["refreshedAt"] >= meta1["refreshedAt"]
    finally:
        client.close()


def test_market_prices_cache_flags_meta(monkeypatch):
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "30")
    provider = _patch_provider(monkeypatch)

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan, raising=False)

    client = TestClient(app)
    try:
        _reset_caches()
        initial_calls = len(provider.calls)
        response1 = client.get("/v1/market/prices?symbols=DIEM,ETH")
        payload1 = response1.json()
        assert response1.status_code == 200
        assert len(provider.calls) == initial_calls + 1
        meta1 = payload1["meta"]
        assert meta1["cacheHit"] is False
        assert "refreshedAt" in meta1

        # Allow measurable cache age
        time.sleep(0.01)
        response2 = client.get("/v1/market/prices?symbols=DIEM,ETH")
        payload2 = response2.json()
        assert response2.status_code == 200
        assert len(provider.calls) == initial_calls + 1
        meta2 = payload2["meta"]
        assert meta2["cacheHit"] is True
        assert meta2["cacheAgeMs"] >= 0
        assert meta2["refreshedAt"] >= meta1["refreshedAt"]
    finally:
        client.close()


def test_market_diem_endpoint(monkeypatch):
    _patch_provider(monkeypatch)

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan, raising=False)

    client = TestClient(app)
    try:
        _reset_caches()
        response = client.get("/v1/market/diem")
        payload = response.json()
        assert response.status_code == 200
        assert "diem" in payload
        payload_diem = payload["diem"]
        assert payload_diem["priceUsd"] == 1.0
        assert payload_diem["health"]["source"] == "stub"
        assert isinstance(payload_diem["diagnostics"], list)
        assert payload["refreshedAt"] >= 0
    finally:
        client.close()


def test_env_and_prices_cache_buy_page_symbols(monkeypatch):
    """Test that buy page symbol set (DIEM,VVV,ETH,USDC,WBTC) uses cache on second call."""
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "30")
    provider = _patch_provider(monkeypatch)

    monkeypatch.setattr(
        module, "env_status", lambda: {"pricing": {"discounts": {}}, "features": {}}
    )

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan, raising=False)

    client = TestClient(app)
    try:
        _reset_caches()
        initial_calls = len(provider.calls)
        # First call with buy page symbol set
        response1 = client.get("/v1/env-and-prices?symbols=DIEM,VVV,ETH,USDC,WBTC")
        payload1 = response1.json()
        assert response1.status_code == 200
        assert len(provider.calls) == initial_calls + 1
        meta1 = payload1["meta"]
        assert meta1["cacheHit"] is False
        assert "refreshedAt" in meta1

        # Second call should hit cache
        response2 = client.get("/v1/env-and-prices?symbols=DIEM,VVV,ETH,USDC,WBTC")
        payload2 = response2.json()
        assert response2.status_code == 200
        assert len(provider.calls) == initial_calls + 1  # cached, no new provider call
        meta2 = payload2["meta"]
        assert meta2["cacheHit"] is True
        assert meta2["cacheAgeMs"] >= 0
        assert meta2["refreshedAt"] >= meta1["refreshedAt"]
    finally:
        client.close()


def test_market_diem_uses_cache(monkeypatch):
    """Test that /v1/market/diem uses env+prices cache when available."""
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "30")
    provider = _patch_provider(monkeypatch)

    monkeypatch.setattr(
        module, "env_status", lambda: {"pricing": {"discounts": {}}, "features": {}}
    )

    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    monkeypatch.setattr(app.router, "lifespan_context", _noop_lifespan, raising=False)

    client = TestClient(app)
    try:
        _reset_caches()
        initial_calls = len(provider.calls)

        # Pre-populate env+prices cache with DIEM
        response_cache = client.get("/v1/env-and-prices?symbols=DIEM,VVV,ETH,USDC,WBTC")
        assert response_cache.status_code == 200
        cache_calls = len(provider.calls)
        assert cache_calls == initial_calls + 1  # One call to populate cache

        # Now call /v1/market/diem - should use cache, not call provider again
        response_diem = client.get("/v1/market/diem")
        payload_diem = response_diem.json()
        assert response_diem.status_code == 200
        assert len(provider.calls) == cache_calls  # No new provider call
        assert "diem" in payload_diem
        assert (
            payload_diem["diem"]["priceUsd"] == 1.0
        )  # DIEM is first symbol, so price=1.0
        # Verify cache-first metadata is present
        assert "_meta" in payload_diem["diem"]
        assert payload_diem["diem"]["_meta"]["source"] == "cache"
    finally:
        client.close()
