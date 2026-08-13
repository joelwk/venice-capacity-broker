from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from libs.venice_sdk.client import VeniceClient


def _load_app(module_name: str):
    import sys

    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location(module_name, str(app_path))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def test_revoked_tenant_blocked_from_me_and_tenant_endpoints(monkeypatch, tmp_path):
    store_file = tmp_path / "tenants_revocation.json"
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_file))
    monkeypatch.setenv("BROKER_STORE_BACKEND", "json")
    monkeypatch.setenv("ALLOW_JSON_FALLBACK", "true")
    monkeypatch.setenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    monkeypatch.setenv("VENICE_PARENT_KEY", "parent-key")
    monkeypatch.setenv("BROKER_DEFAULT_EXPIRY_DAYS", "30")
    monkeypatch.delenv("BROKER_ADMIN_TOKEN", raising=False)

    broker_app = _load_app("broker_api_app_revocation_enforcement")

    def fake_issue(parent_key, label, consumption_limit, expires_at):  # type: ignore[no-untyped-def]
        return {"apiKey": "tenant-subkey", "id": "key-id-1"}

    monkeypatch.setattr(broker_app.keys, "issue_scoped_key", fake_issue, raising=False)  # type: ignore[attr-defined]

    def fake_get_usage(self, sub_key=None):  # type: ignore[no-untyped-def]
        return {"ok": True, "sub_key": sub_key}

    def fake_get_rate_limits(self, sub_key=None):  # type: ignore[no-untyped-def]
        return {"ok": True, "sub_key": sub_key}

    monkeypatch.setattr(VeniceClient, "get_usage", fake_get_usage, raising=True)
    monkeypatch.setattr(
        VeniceClient, "get_rate_limits", fake_get_rate_limits, raising=True
    )

    client = TestClient(broker_app.app)
    resp = client.post(
        "/v1/tenants", json={"tenant_id": "t1", "label": "Tenant 1", "quota": 10}
    )
    assert resp.status_code == 200, resp.text

    tenant_headers = {"Authorization": "Bearer tenant-subkey"}
    assert client.get("/v1/me", headers=tenant_headers).status_code == 200
    assert client.get("/v1/me/usage", headers=tenant_headers).status_code == 200
    assert client.get("/v1/tenants/t1/usage", headers=tenant_headers).status_code == 200
    assert (
        client.get("/v1/tenants/t1/limits", headers=tenant_headers).status_code == 200
    )

    resp = client.post("/v1/tenants/t1/revoke")
    assert resp.status_code == 200, resp.text

    assert client.get("/v1/me", headers=tenant_headers).status_code == 401
    assert client.get("/v1/me/usage", headers=tenant_headers).status_code == 401
    assert client.get("/v1/me/broker-limits", headers=tenant_headers).status_code == 401
    assert client.get("/v1/tenants/t1/usage", headers=tenant_headers).status_code == 401
    assert (
        client.get("/v1/tenants/t1/limits", headers=tenant_headers).status_code == 401
    )
