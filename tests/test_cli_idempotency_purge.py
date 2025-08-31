from __future__ import annotations

import os
from pathlib import Path


class SharedKV:
    """Simple shared in-memory KV with a class-level store for tests."""

    _store: dict[str, str] = {}

    def __init__(self) -> None:  # noqa: D401
        pass

    def get(self, key: str):  # noqa: ANN001
        return self._store.get(key)

    def set(self, key: str, value, ttl_s=None):  # noqa: ANN001
        self._store[key] = str(value)

    def delete(self, key: str):  # noqa: ANN001
        self._store.pop(key, None)

    def keys(self, prefix: str) -> list[str]:
        return [k for k in list(self._store.keys()) if k.startswith(prefix)]

    def incrby(self, key: str, by: int = 1, ttl_s: int | None = None) -> int:  # noqa: ANN001
        cur = int(self._store.get(key) or 0)
        new_v = cur + int(by)
        self._store[key] = str(new_v)
        return new_v


def _import_app_with_shared_kv(tmp_store: Path, monkeypatch):
    # Monkeypatch KVStore before importing app so middleware/admin use shared KV
    import importlib
    import importlib.util

    # Ensure fresh import of libs.kv so patch takes effect
    import libs.kv as kvpkg

    monkeypatch.setattr(kvpkg, "KVStore", SharedKV, raising=True)

    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_store)
    os.environ["IDEM_TTL_SECONDS"] = "60"
    os.environ["RATE_LIMITS_ENABLED"] = "false"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "1000"

    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_idem", str(app_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _seed_tenant(mod):  # noqa: ANN001
    Tenant = mod.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id="tX", label="Tx", subkey="sub-X", quota=0)
    mod.store.upsert(tenant)


def test_idempotency_and_purge_cli(tmp_path, monkeypatch):
    # Patch CLI to use shared KV as well
    import libs.kv as kvpkg
    monkeypatch.setattr(kvpkg, "KVStore", SharedKV, raising=True)

    mod = _import_app_with_shared_kv(tmp_path / "tenants.json", monkeypatch)
    _seed_tenant(mod)

    from fastapi.testclient import TestClient
    # Stub Venice client chat
    def fake_chat(self, messages, model=None, **kw):  # noqa: ANN001
        return {"status": "ok", "echo": messages}

    from libs import venice_sdk

    monkeypatch.setattr(venice_sdk.client.VeniceClient, "chat_completions", fake_chat, raising=True)

    client = TestClient(mod.app)
    os.environ["RATE_LIMITS_ENABLED"] = "false"
    headers = {"Authorization": "Bearer sub-X"}
    payload = {"messages": [{"role": "user", "content": "hello"}]}

    # First request accepted
    r1 = client.post("/v1/chat", headers=headers, json=payload)
    assert r1.status_code == 200
    assert r1.headers.get("X-Idempotency-Accepted") == "true"
    # Second identical request should be rejected due to idempotency
    r2 = client.post("/v1/chat", headers=headers, json=payload)
    assert r2.status_code == 409
    assert r2.headers.get("X-Idempotency-Accepted") == "false"
    body = r2.json()
    assert body.get("code") == "idempotency_replay"

    # Now run CLI purge for this tenant and scope
    from apps.cli.main import cmd_idem_purge, argparse

    ns = argparse.Namespace(prefix="idem:chat:tX")
    cmd_idem_purge(ns)

    # Ensure keys are gone: next request should be accepted again
    r3 = client.post("/v1/chat", headers=headers, json=payload)
    assert r3.status_code == 200
    assert r3.headers.get("X-Idempotency-Accepted") == "true"
