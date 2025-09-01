from __future__ import annotations

import os
import json
from importlib import reload


def setup_module(module):
    # Configure env before importing app
    os.environ["RATE_LIMITS_ENABLED"] = "true"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "2"
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = "apps/broker-api/tenants.test.json"


def test_rate_limit_enforced(monkeypatch, tmp_path):
    # Point store to temp file
    store_file = tmp_path / "tenants.json"
    os.environ["BROKER_STORE_FILE"] = str(store_file)

    # Import app module from file path (app dir has a dash)
    from pathlib import Path
    import importlib.util

    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker_app)  # type: ignore[attr-defined]

    # Stub VeniceClient.chat_completions to avoid network calls
    def fake_chat(self, messages, model=None, **kw):  # noqa: ANN001
        return {"status": "ok", "echo": messages}

    from libs import venice_sdk

    monkeypatch.setattr(venice_sdk.client.VeniceClient, "chat_completions", fake_chat, raising=True)

    # Insert a tenant directly into store using app's Tenant class
    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id="t1", label="T1", subkey="sub-1", quota=0)
    broker_app.store.upsert(tenant)

    # Use TestClient to call chat 3 times; expect 3rd to 429
    from fastapi.testclient import TestClient

    client = TestClient(broker_app.app)
    headers = {"Authorization": "Bearer sub-1"}
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    headers1 = dict(headers)
    headers1["Idempotency-Key"] = "k1"
    r1 = client.post("/v1/chat", headers=headers1, json=payload)
    assert r1.status_code == 200, r1.text
    headers2 = dict(headers)
    headers2["Idempotency-Key"] = "k2"
    r2 = client.post("/v1/chat", headers=headers2, json=payload)
    assert r2.status_code == 200, r2.text
    headers3 = dict(headers)
    headers3["Idempotency-Key"] = "k3"
    r3 = client.post("/v1/chat", headers=headers3, json=payload)
    assert r3.status_code == 429, r3.text
    # Headers present
    assert "X-RateLimit-Limit" in r3.headers
    assert "X-RateLimit-Remaining" in r3.headers
    assert "X-RateLimit-Reset" in r3.headers
    assert "Retry-After" in r3.headers


def test_rate_limit_resets_without_redis(monkeypatch, tmp_path):
    # Configure fast 1s window and 1 request per window
    store_file = tmp_path / "tenants2.json"
    os.environ["BROKER_STORE_FILE"] = str(store_file)
    os.environ["RATE_LIMITS_ENABLED"] = "true"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "1"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "1"
    os.environ.pop("REDIS_URL", None)

    from pathlib import Path
    import importlib.util
    import time

    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_rl2", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker_app)  # type: ignore[attr-defined]

    def fake_chat(self, messages, model=None, **kw):  # noqa: ANN001
        return {"status": "ok", "echo": messages}

    from libs import venice_sdk

    monkeypatch.setattr(venice_sdk.client.VeniceClient, "chat_completions", fake_chat, raising=True)

    # Insert tenant
    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id="t2", label="T2", subkey="sub-2", quota=0)
    broker_app.store.upsert(tenant)

    from fastapi.testclient import TestClient

    client = TestClient(broker_app.app)
    headers = {"Authorization": "Bearer sub-2"}
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    r1 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a1"}, json=payload)
    assert r1.status_code == 200
    r2 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a2"}, json=payload)
    assert r2.status_code == 429
    # Wait for window to roll
    time.sleep(1.2)
    r3 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a3"}, json=payload)
    assert r3.status_code == 200


def test_rate_limit_with_redis_if_available(monkeypatch, tmp_path):
    import os as _os

    if not _os.getenv("REDIS_URL"):
        import pytest

        pytest.skip("REDIS_URL not set; skipping redis-backed limiter test")
    store_file = tmp_path / "tenants3.json"
    os.environ["BROKER_STORE_FILE"] = str(store_file)
    os.environ["RATE_LIMITS_ENABLED"] = "true"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "1"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "1"

    from pathlib import Path
    import importlib.util

    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_rl3", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker_app)  # type: ignore[attr-defined]

    def fake_chat(self, messages, model=None, **kw):  # noqa: ANN001
        return {"status": "ok", "echo": messages}

    from libs import venice_sdk

    monkeypatch.setattr(venice_sdk.client.VeniceClient, "chat_completions", fake_chat, raising=True)

    # Insert tenant
    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id="t3", label="T3", subkey="sub-3", quota=0)
    broker_app.store.upsert(tenant)

    from fastapi.testclient import TestClient

    client = TestClient(broker_app.app)
    headers = {"Authorization": "Bearer sub-3"}
    payload = {"messages": [{"role": "user", "content": "hi"}]}
    # First allowed, second blocked
    r1 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "b1"}, json=payload)
    r2 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "b2"}, json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 429


def test_no_rate_limit_when_disabled(monkeypatch, tmp_path):
    # Even with low limits configured, disabling limiter should allow requests
    store_file = tmp_path / "tenants_no_rl.json"
    os.environ["BROKER_STORE_FILE"] = str(store_file)
    os.environ["RATE_LIMITS_ENABLED"] = "false"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "1"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "1"

    from pathlib import Path
    import importlib.util

    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_rl_off", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(broker_app)  # type: ignore[attr-defined]

    def fake_chat(self, messages, model=None, **kw):  # noqa: ANN001
        return {"status": "ok", "echo": messages}

    from libs import venice_sdk

    monkeypatch.setattr(venice_sdk.client.VeniceClient, "chat_completions", fake_chat, raising=True)

    # Insert tenant
    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id="t_off", label="T_off", subkey="sub-off", quota=0)
    broker_app.store.upsert(tenant)

    from fastapi.testclient import TestClient

    client = TestClient(broker_app.app)
    headers = {"Authorization": "Bearer sub-off"}
    payload = {"messages": [{"role": "user", "content": "hi"}]}

    # All requests should pass (no 429) since limiter is disabled
    r1 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "z1"}, json=payload)
    r2 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "z2"}, json=payload)
    r3 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "z3"}, json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r3.status_code == 200
