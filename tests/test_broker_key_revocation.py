from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient


def _load_app(module_name: str):
    import sys

    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location(module_name, str(app_path))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register module in sys.modules before exec_module so module code can access sys.modules[__name__]
    sys.modules[module_name] = module
    spec.loader.exec_module(module)  # type: ignore[attr-defined]
    return module


def test_rotate_revoke_old_key(monkeypatch, tmp_path):
    store_file = tmp_path / "tenants_rotate.json"
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_file))
    monkeypatch.setenv("BROKER_STORE_BACKEND", "json")
    monkeypatch.setenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    monkeypatch.setenv("VENICE_PARENT_KEY", "parent-key")
    monkeypatch.delenv("BROKER_ADMIN_TOKEN", raising=False)

    broker_app = _load_app("broker_api_app_rotate")

    revoke_calls: list[str] = []

    def fake_issue(parent_key, label, consumption_limit, expires_at):
        return {"apiKey": "new-subkey", "id": "new-key-id"}

    def fake_revoke(key_id: str):
        revoke_calls.append(key_id)
        return {"status": "ok"}

    monkeypatch.setattr(broker_app.keys, "issue_scoped_key", fake_issue, raising=False)  # type: ignore[attr-defined]
    monkeypatch.setattr(broker_app.keys, "revoke_key", fake_revoke, raising=False)  # type: ignore[attr-defined]

    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    existing = Tenant(
        id="tenant-1",
        label="Tenant One",
        subkey="old-sub",
        quota=10,
        expires_at="2025-12-31",
        key_id="old-key",
    )
    broker_app.store.upsert(existing)

    client = TestClient(broker_app.app)
    payload = {"tenant_id": "tenant-1", "label": "Tenant One"}
    resp = client.post("/v1/tenants?rotate=true&revoke_old=true", json=payload)
    assert resp.status_code == 200, resp.text
    assert revoke_calls == ["old-key"]
    updated = broker_app.store.get("tenant-1")
    assert updated is not None
    assert updated.subkey == "new-subkey"
    assert updated.key_id == "new-key-id"


def test_admin_revoke_endpoint(monkeypatch, tmp_path):
    store_file = tmp_path / "tenants_revoke.json"
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_file))
    monkeypatch.setenv("BROKER_STORE_BACKEND", "json")
    monkeypatch.setenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    monkeypatch.setenv("VENICE_PARENT_KEY", "parent-key")
    monkeypatch.delenv("BROKER_ADMIN_TOKEN", raising=False)

    broker_app = _load_app("broker_api_app_revoke")

    revoke_calls: list[str] = []

    def fake_revoke(key_id: str):
        revoke_calls.append(key_id)
        return {"status": "ok"}

    monkeypatch.setattr(broker_app.keys, "revoke_key", fake_revoke, raising=False)  # type: ignore[attr-defined]

    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(
        id="tenant-2", label="Tenant Two", subkey="sub", quota=5, key_id="key-to-revoke"
    )
    broker_app.store.upsert(tenant)

    client = TestClient(broker_app.app)
    resp = client.post("/v1/tenants/tenant-2/revoke")
    assert resp.status_code == 200, resp.text
    assert revoke_calls == ["key-to-revoke"]
    updated = broker_app.store.get("tenant-2")
    assert updated is not None
    assert updated.status == "revoked"
