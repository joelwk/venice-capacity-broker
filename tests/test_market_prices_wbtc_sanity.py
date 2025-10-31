from pathlib import Path
import importlib.util
import os

from fastapi.testclient import TestClient

from libs.dex.routes import as_route_plan
from services.marketdata.provider import MarketDataProvider

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


WBTC_ADDRESS = "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"
QUOTE_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
DIEM_ADDRESS = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"


def test_market_prices_clamps_wbtc(monkeypatch):
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", QUOTE_ADDRESS)
    monkeypatch.setenv("WBTC_TOKEN_ADDRESS", WBTC_ADDRESS)
    monkeypatch.setenv("WETH_ADDRESS", WETH_ADDRESS)
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", DIEM_ADDRESS)
    monkeypatch.setenv("TRADE_PATH", f"{DIEM_ADDRESS},{WETH_ADDRESS},{QUOTE_ADDRESS}")

    monkeypatch.setattr(MarketDataProvider, "_validate_trade_paths", lambda self: None)
    monkeypatch.setattr(MarketDataProvider, "_check_wbtc_configuration", lambda self: None)
    monkeypatch.setattr(MarketDataProvider, "_warm_route_liquidity", lambda self, tokens: None)
    monkeypatch.setattr(MarketDataProvider, "_mid_price_from_reserves", lambda self, a, b: None)

    def fake_best_price(self: MarketDataProvider, route, amount_in_decimal: float = 1.0, **kwargs):
        plan = as_route_plan(route)
        tokens = tuple(addr.lower() for addr in plan.tokens)
        if tokens[0] == WBTC_ADDRESS.lower():
            return {"provider": "stub", "price": 2.83}
        raise RuntimeError(f"unexpected route {plan.tokens}")

    monkeypatch.setattr(MarketDataProvider, "best_price", fake_best_price, raising=False)
    monkeypatch.setattr(MarketDataProvider, "_best_price_scan", lambda self, route, start=1.0, min_amount=1e-12, factor=10.0: None)
    monkeypatch.setattr(MarketDataProvider, "_fetch_external_price", lambda self, symbol: 112000.0 if str(symbol).upper() == "WBTC" else None)

    client = TestClient(app)
    response = client.get("/v1/market/prices?symbols=WBTC,USDC")
    payload = response.json()

    assert response.status_code == 200
    assert payload["prices"]["USDC"] == 1.0
    assert payload["prices"]["WBTC"] >= 1000
