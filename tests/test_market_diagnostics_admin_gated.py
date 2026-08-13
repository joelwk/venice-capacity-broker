import os

from fastapi.testclient import TestClient


def test_market_diagnostics_requires_admin_token(monkeypatch):
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"  # gitleaks:allow test value
    os.environ["BROKER_REQUIRE_ADMIN_TOKEN"] = "true"

    from apps.broker_api.app import app

    client = TestClient(app)
    res = client.get("/v1/market/diagnostics")
    assert res.status_code == 401


def test_market_diagnostics_allows_admin_token(monkeypatch):
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"  # gitleaks:allow test value
    os.environ["BROKER_REQUIRE_ADMIN_TOKEN"] = "true"

    from apps.broker_api.app import app

    client = TestClient(app)
    res = client.get(
        "/v1/market/diagnostics?symbols=USDC",
        headers={"Authorization": "Bearer adminkey"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "probe" in body
    assert body["probe"]["symbols"] == ["USDC"]
