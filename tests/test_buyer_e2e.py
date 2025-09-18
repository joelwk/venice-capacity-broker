from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
import pytest

# Remove lightweight stubs so we can reload the real modules when available.
for _mod in ("sqlmodel", "sqlalchemy", "db.session", "db.models"):
    sys.modules.pop(_mod, None)

# Skip when SQLModel is unavailable (features require DB models).
pytest.importorskip("sqlmodel")



def _load_broker_app_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("broker_app_test", "apps/broker-api/app.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def test_buyer_lifecycle_quote_verify_subkey(monkeypatch):
    # Enable features and static pricing for ETH
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PURCHASES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "static")
    monkeypatch.setenv("PRICE_UNIT_ETH_WEI", str(10**15))  # 0.001 ETH per unit
    monkeypatch.setenv("PRICE_QUOTE_TTL_SECONDS", "120")
    # Minimal chain/payment config
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    # Provide a temporary SQLite database so SQLModel path is available even when
    # Postgres/SQL_DATABASE_URL is not configured in CI environments.
    monkeypatch.setenv("SQL_DATABASE_URL", "sqlite:///./test-buyer-e2e.db")

    mod = _load_broker_app_module()

    # Stub RPC calls to confirm ETH payment
    tx_payload = {
        "to": "0xabc0000000000000000000000000000000000001",
        "from": "0xdef0000000000000000000000000000000000002",
        "value": hex(5 * 10**15),  # placeholder; updated once quote is fetched
    }

    def _fake_rpc(url: str, method: str, params: list):  # noqa: ANN001
        if method == "eth_getTransactionReceipt":
            return {"status": hex(1), "blockNumber": hex(12345), "logs": []}
        if method == "eth_getTransactionByHash":
            return tx_payload
        raise AssertionError(f"unexpected rpc call: {method}")

    monkeypatch.setattr(mod, "_rpc_call", _fake_rpc, raising=True)

    # Stub subkey issuance to avoid Venice network
    def _fake_issue(parent_key: str, label: str, consumption_limit: int, expires_at: str | None = None):  # noqa: ANN001
        return {"apiKey": "sk-test-123", "id": "kid-1"}

    monkeypatch.setattr(mod.keys, "issue_scoped_key", _fake_issue, raising=True)

    # Use FastAPI TestClient
    from fastapi.testclient import TestClient

    client = TestClient(mod.app)
    # Get a quote for ETH
    q = client.get("/v1/quotes", params={"units": 5, "asset": "ETH"})
    assert q.status_code == 200, q.text
    data = q.json()
    tx_payload["value"] = hex(int(data["totalPrice"]))
    assert int(data["units"]) == 5 and data["asset"] == "ETH"
    # Verify purchase with a fake tx hash
    v = client.post(
        "/v1/purchases/verify",
        json={
            "quoteId": data["quoteId"],
            "txHash": "0xdeadbeef",
            "buyerAddress": "0xBEEf000000000000000000000000000000000003",
        },
    )
    assert v.status_code == 200, v.text
    out = v.json()
    assert out["status"] in {"confirmed", "fulfilled"}
    # If fulfilled, subkey must be present
    if out["status"] == "fulfilled":
        assert out["subkey"] == "sk-test-123"


def test_budget_quote_path(monkeypatch):
    monkeypatch.setenv("QUOTES_ENABLED", "true")
    monkeypatch.setenv("PRICE_ENGINE", "market")
    monkeypatch.setenv("PURCHASE_UNITS_KIND", "diem")
    monkeypatch.setenv("BASE_RPC_URL", "http://localhost:8545")
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")
    monkeypatch.setenv("SQL_DATABASE_URL", "sqlite:///./test-buyer-e2e.db")

    mod = _load_broker_app_module()

    # Force deterministic pricing without on-chain calls.
    engine = mod._pricing.engine

    def _fake_prices() -> tuple[float, float, float]:
        # base_unit_usd, diem_usd, eth_usd
        return (200.0, 200.0, 4000.0)

    monkeypatch.setattr(engine, "_resolve_prices", _fake_prices, raising=True)

    from fastapi.testclient import TestClient

    client = TestClient(mod.app)
    resp = client.get("/v1/quotes", params={"budget": 10, "asset": "ETH"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["asset"] == "ETH"
    assert pytest.approx(payload["units"], rel=1e-6) == 0.05
    eth_amount = payload["totalPrice"] / 1e18
    assert pytest.approx(eth_amount, rel=1e-6) == 0.0025

    too_small = client.get("/v1/quotes", params={"budget": 0.1, "asset": "ETH"})
    assert too_small.status_code == 400
    assert "at least" in too_small.json()["detail"].lower()
