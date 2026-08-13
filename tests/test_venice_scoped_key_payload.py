from __future__ import annotations

import pytest

from libs.venice_sdk.client import VeniceClient
from services.venice_keys.manager import KeyManager


def test_venice_client_scoped_subkey_payload_includes_limit_and_expiry(monkeypatch):
    captured: dict[str, object] = {}

    def fake_post_with_key(self, path, json, api_key, extra_headers=None):  # type: ignore[no-untyped-def]
        captured["path"] = path
        captured["json"] = json
        captured["api_key"] = api_key
        return {"id": "key-id", "apiKey": "secret"}

    monkeypatch.setattr(
        VeniceClient, "_post_with_key", fake_post_with_key, raising=True
    )

    vc = VeniceClient(base_url="https://api.venice.ai/api/v1", api_key=None)
    out = vc.create_scoped_subkey(
        parent_key="PARENT",
        label="tenant:t1",
        consumption_limit=123,
        expires_at="2099-01-01T00:00:00Z",
    )

    assert out["id"] == "key-id"
    assert captured["api_key"] == "PARENT"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["consumptionLimit"] == {"diem": 123}
    assert payload["expiresAt"] == "2099-01-01T00:00:00Z"
    assert payload["description"] == "tenant:t1"


def test_venice_client_scoped_subkey_requires_expiry():
    vc = VeniceClient(base_url="https://api.venice.ai/api/v1", api_key=None)
    with pytest.raises(ValueError, match="expires_at is required"):
        vc.create_scoped_subkey(
            parent_key="PARENT",
            label="tenant:t1",
            consumption_limit=123,
            expires_at=None,
        )


def test_key_manager_scoped_key_requires_expiry(monkeypatch):
    called: dict[str, object] = {}

    class DummyClient:
        def create_scoped_subkey(
            self, parent_key, label, consumption_limit, expires_at
        ):  # type: ignore[no-untyped-def]
            called["parent_key"] = parent_key
            called["label"] = label
            called["consumption_limit"] = consumption_limit
            called["expires_at"] = expires_at
            return {"ok": True}

    km = KeyManager(client=DummyClient())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expires_at is required"):
        km.issue_scoped_key("PARENT", "t1", 1, expires_at=None)  # type: ignore[arg-type]

    out = km.issue_scoped_key("PARENT", "t1", 1, expires_at="2099-01-01T00:00:00Z")
    assert out == {"ok": True}
    assert called["expires_at"] == "2099-01-01T00:00:00Z"
