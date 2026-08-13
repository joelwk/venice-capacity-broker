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


def test_tenant_create_issues_scoped_key_with_limit_and_expiry(monkeypatch, tmp_path):
    store_file = tmp_path / "tenants_contract.json"
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_file))
    monkeypatch.setenv("BROKER_STORE_BACKEND", "json")
    monkeypatch.setenv("ALLOW_JSON_FALLBACK", "true")
    monkeypatch.setenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    monkeypatch.setenv("VENICE_PARENT_KEY", "parent-key")
    monkeypatch.setenv("BROKER_DEFAULT_EXPIRY_DAYS", "30")
    monkeypatch.delenv("BROKER_ADMIN_TOKEN", raising=False)

    broker_app = _load_app("broker_api_app_contract_tenant_create")

    calls: list[tuple[str, str, object, str]] = []

    def fake_issue(parent_key, label, consumption_limit, expires_at):  # type: ignore[no-untyped-def]
        calls.append((parent_key, label, consumption_limit, expires_at))
        return {"apiKey": "new-subkey", "id": "new-key-id"}

    monkeypatch.setattr(broker_app.keys, "issue_scoped_key", fake_issue, raising=False)  # type: ignore[attr-defined]

    client = TestClient(broker_app.app)
    payload = {"tenant_id": "t1", "label": "Tenant One", "quota": 777}
    resp = client.post("/v1/tenants", json=payload)
    assert resp.status_code == 200, resp.text

    assert calls, "expected issue_scoped_key to be called"
    parent_key, label, consumption_limit, expires_at = calls[-1]
    assert parent_key == "parent-key"
    assert label == "Tenant One"
    assert consumption_limit == 777
    assert (
        isinstance(expires_at, str)
        and expires_at.endswith("Z")
        and len(expires_at) > 10
    )


def test_broker_venice_subkey_defaults_expires_at(monkeypatch, tmp_path):
    store_file = tmp_path / "tenants_contract_venice.json"
    monkeypatch.setenv("BROKER_STORE_FILE", str(store_file))
    monkeypatch.setenv("BROKER_STORE_BACKEND", "json")
    monkeypatch.setenv("ALLOW_JSON_FALLBACK", "true")
    monkeypatch.setenv("BROKER_REQUIRE_ADMIN_TOKEN", "false")
    monkeypatch.setenv("VENICE_PARENT_KEY", "parent-key")
    monkeypatch.setenv("BROKER_DEFAULT_EXPIRY_DAYS", "30")
    monkeypatch.delenv("BROKER_ADMIN_TOKEN", raising=False)

    broker_app = _load_app("broker_api_app_contract_venice_subkey")

    captured: dict[str, object] = {}

    def fake_create_scoped_subkey(
        self, parent_key, label, consumption_limit, expires_at
    ):  # type: ignore[no-untyped-def]
        captured["parent_key"] = parent_key
        captured["label"] = label
        captured["consumption_limit"] = consumption_limit
        captured["expires_at"] = expires_at
        return {"apiKey": "new-subkey", "id": "new-key-id"}

    monkeypatch.setattr(
        VeniceClient, "create_scoped_subkey", fake_create_scoped_subkey, raising=True
    )

    client = TestClient(broker_app.app)
    payload = {"label": "debug", "consumptionLimit": {"diem": 10}}
    resp = client.post("/v1/venice/subkey", json=payload)
    assert resp.status_code == 200, resp.text

    assert captured["parent_key"] == "parent-key"
    assert captured["label"] == "debug"
    assert captured["consumption_limit"] == {"diem": 10}
    expires_at = captured["expires_at"]
    assert (
        isinstance(expires_at, str)
        and expires_at.endswith("Z")
        and len(expires_at) > 10
    )
