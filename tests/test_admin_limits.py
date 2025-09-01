from __future__ import annotations

import os
from pathlib import Path
import importlib.util


def _load_app(module_name: str):
    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location(module_name, str(app_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _add_tenant(broker_app, tenant_id: str, subkey: str = "sub-1", label: str = "T1"):
    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id=tenant_id, label=label, subkey=subkey, quota=0)
    broker_app.store.upsert(tenant)


def setup_module(module):
    # Common env defaults for limit values
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "2"


def test_admin_limits_auth_and_defaults(tmp_path):
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_path / "tenants_admin_limits.json")
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"
    # Ensure KV admin is available even when limiter disabled
    os.environ["RATE_LIMITS_ENABLED"] = "false"

    broker_app = _load_app("broker_api_admin_limits_1")
    _add_tenant(broker_app, "t1")

    from fastapi.testclient import TestClient

    client = TestClient(broker_app.app)

    # No auth -> 401
    r_unauth = client.get("/v1/tenants/t1/broker-limits")
    assert r_unauth.status_code == 401

    # With admin token -> defaults reflect env
    headers = {"Authorization": "Bearer adminkey"}
    r = client.get("/v1/tenants/t1/broker-limits", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["windowSeconds"] == 60
    assert body["maxRequests"] == 2
    assert "label" in body


def test_admin_limits_post_valid_invalid_and_idempotent(tmp_path):
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_path / "tenants_admin_limits2.json")
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"
    os.environ["RATE_LIMITS_ENABLED"] = "true"  # also exercises limiter branch init

    broker_app = _load_app("broker_api_admin_limits_2")
    _add_tenant(broker_app, "t2")

    from fastapi.testclient import TestClient

    client = TestClient(broker_app.app)
    headers = {"Authorization": "Bearer adminkey"}

    # Invalid payloads -> 422
    r_bad1 = client.post(
        "/v1/tenants/t2/broker-limits",
        headers=headers,
        json={"windowSeconds": 0},  # ge=1
    )
    assert r_bad1.status_code == 422
    r_bad2 = client.post(
        "/v1/tenants/t2/broker-limits",
        headers=headers,
        json={"maxRequests": -1},  # ge=0
    )
    assert r_bad2.status_code == 422

    # Valid set
    payload = {"windowSeconds": 10, "maxRequests": 3, "label": "premium"}
    r1 = client.post("/v1/tenants/t2/broker-limits", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.json()["windowSeconds"] == 10
    assert r1.json()["maxRequests"] == 3
    assert r1.json()["label"] == "premium"

    # Idempotent update (same values)
    r2 = client.post("/v1/tenants/t2/broker-limits", headers=headers, json=payload)
    assert r2.status_code == 200
    assert r2.json() == r1.json()

    # GET reflects persisted KV values
    r_get = client.get("/v1/tenants/t2/broker-limits", headers=headers)
    assert r_get.status_code == 200
    assert r_get.json() == r1.json()

    # Unknown tenant -> 404 for both GET and POST
    r_get_404 = client.get("/v1/tenants/nope/broker-limits", headers=headers)
    assert r_get_404.status_code == 404
    r_post_404 = client.post("/v1/tenants/nope/broker-limits", headers=headers, json=payload)
    assert r_post_404.status_code == 404

