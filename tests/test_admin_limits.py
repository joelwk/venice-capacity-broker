from __future__ import annotations

import os
from pathlib import Path


def _import_app(tmp_store: Path):
    # Import broker app fresh with env
    import importlib.util

    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_store)
    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_limits", str(app_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _seed_tenant(mod, tenant_id: str = "t-1", subkey: str = "sub-1") -> None:  # noqa: ANN001
    Tenant = mod.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id=tenant_id, label="T1", subkey=subkey, quota=0)
    mod.store.upsert(tenant)


def test_admin_limits_endpoints_auth_and_update(tmp_path):
    os.environ["BROKER_ADMIN_TOKEN"] = "dev-admin"
    os.environ["BROKER_REQUIRE_ADMIN_TOKEN"] = "true"

    store_file = tmp_path / "tenants.json"
    app_mod = _import_app(store_file)
    _seed_tenant(app_mod, tenant_id="t-1", subkey="sub-k")

    from fastapi.testclient import TestClient

    client = TestClient(app_mod.app)

    # Auth matrix: admin ok
    hdrs_admin = {"Authorization": "Bearer dev-admin"}
    r_list = client.get("/v1/tenants", headers=hdrs_admin)
    assert r_list.status_code == 200
    assert any(t["id"] == "t-1" for t in r_list.json())

    # Tenant token cannot access admin endpoints
    hdrs_tenant = {"Authorization": "Bearer sub-k"}
    r_list_forbidden = client.get("/v1/tenants", headers=hdrs_tenant)
    assert r_list_forbidden.status_code == 401

    # Get limits default
    r_get = client.get("/v1/tenants/t-1/broker-limits", headers=hdrs_admin)
    assert r_get.status_code == 200
    assert "windowSeconds" in r_get.json()

    # Update limits (idempotent)
    payload = {"windowSeconds": 30, "maxRequests": 3, "label": "basic"}
    r_set = client.post("/v1/tenants/t-1/broker-limits", headers=hdrs_admin, json=payload)
    assert r_set.status_code == 200, r_set.text
    r_get2 = client.get("/v1/tenants/t-1/broker-limits", headers=hdrs_admin)
    assert r_get2.json()["windowSeconds"] == 30
    assert r_get2.json()["maxRequests"] == 3
    assert r_get2.json()["label"] == "basic"
    # Idempotent update (same payload)
    r_set2 = client.post("/v1/tenants/t-1/broker-limits", headers=hdrs_admin, json=payload)
    assert r_set2.status_code == 200

    # Invalid payload: negative window -> 422
    bad = client.post("/v1/tenants/t-1/broker-limits", headers=hdrs_admin, json={"windowSeconds": -5})
    assert bad.status_code in (400, 422)

    # Revoke
    r_rev = client.post("/v1/tenants/t-1/revoke", headers=hdrs_admin)
    assert r_rev.status_code == 200
    r_t = client.get("/v1/tenants/t-1", headers=hdrs_admin)
    assert r_t.json()["status"] == "revoked"
