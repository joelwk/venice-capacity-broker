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

    def prices(self, symbols):
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


def _reset_caches():
    module._env_prices_resp_cache.clear()
    module._prices_resp_cache.clear()


def _patch_provider(monkeypatch):
    provider = StubMarketDataProvider()
    monkeypatch.setattr(module, "_get_marketdata_provider", lambda status_code=500: provider)
    return provider


def test_env_and_prices_cache_includes_meta(monkeypatch):
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "30")
    provider = _patch_provider(monkeypatch)

    monkeypatch.setattr(module, "env_status", lambda: {"pricing": {"discounts": {}}, "features": {}})

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
