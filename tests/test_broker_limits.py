from __future__ import annotations

import os

_SKIP_REDIS_TESTS = os.getenv("SKIP_REDIS_TESTS", "").strip().lower() in {"1", "true", "yes", "on"}

def setup_module(module):
    # Configure env before importing app
    os.environ["RATE_LIMITS_ENABLED"] = "true"
    os.environ["RATE_LIMIT_WINDOW_SECONDS"] = "60"
    os.environ["RATE_LIMIT_MAX_REQUESTS"] = "2"
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = "apps/broker_api/tenants.test.json"
    os.environ["BROKER_REQUIRE_ADMIN_TOKEN"] = "false"
    os.environ["BROKER_ADMIN_TOKEN"] = "test-admin"
    # Force in-memory KV (avoid remote Replit DB / Redis during tests)
    os.environ.pop("KV_URL", None)
    os.environ.pop("REPLIT_DB_URL", None)
    os.environ.pop("REDIS_URL", None)
    os.environ.pop("KV_REDIS_URL", None)


def test_rate_limit_enforced(monkeypatch, tmp_path):
    # Point store to temp file
    store_file = tmp_path / "tenants.json"
    os.environ["BROKER_STORE_FILE"] = str(store_file)

    # Import app module from file path (app dir has a dash)
    from pathlib import Path
    import importlib.util

    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    # Register module in sys.modules before exec_module so module code can access sys.modules[__name__]
    import sys
    sys.modules["broker_api_app"] = broker_app
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
    os.environ.pop("KV_REDIS_URL", None)
    monkeypatch.setenv("SQL_DATABASE_URL", "sqlite:///./test-rate-limit.db")

    from pathlib import Path
    import importlib.util
    import time

    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_rl2", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    # Register module in sys.modules before exec_module so module code can access sys.modules[__name__]
    import sys
    sys.modules["broker_api_app_rl2"] = broker_app
    spec.loader.exec_module(broker_app)  # type: ignore[attr-defined]

    from collections import deque

    times = deque([100.0, 100.1, 101.5])

    def fake_now() -> float:
        if len(times) > 1:
            return times.popleft()
        return times[0]

    broker_app._limiter._now = fake_now  # type: ignore[attr-defined]
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

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
    # Wait for window to roll (monkeypatched to no-op but advances via fake_now)
    time.sleep(1.2)
    r3 = client.post("/v1/chat", headers={**headers, "Idempotency-Key": "a3"}, json=payload)
    assert r3.status_code == 200


def test_rate_limit_with_redis_if_available(monkeypatch, tmp_path):
    import os as _os

    if _SKIP_REDIS_TESTS:
        import pytest

        pytest.skip("Redis-backed limiter tests disabled (SKIP_REDIS_TESTS)")

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

    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_rl3", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    # Register module in sys.modules before exec_module so module code can access sys.modules[__name__]
    import sys
    sys.modules["broker_api_app_rl3"] = broker_app
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

    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_rl_off", str(app_path))
    assert spec and spec.loader
    broker_app = importlib.util.module_from_spec(spec)
    # Register module in sys.modules before exec_module so module code can access sys.modules[__name__]
    import sys
    sys.modules["broker_api_app_rl_off"] = broker_app
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


def test_kv_limiter_without_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("KV_REDIS_URL", raising=False)

    from libs.kv import KVStore
    from libs.ratelimit.kv_sliding_window import KVSlidingWindowLimiter
    import time

    kv = KVStore()
    limiter = KVSlidingWindowLimiter(kv)

    allowed1, _ = limiter.allow("tenant:test", limit=1, window_seconds=1)
    allowed2, headers2 = limiter.allow("tenant:test", limit=1, window_seconds=1)

    assert allowed1 is True
    assert allowed2 is False
    assert headers2["X-RateLimit-Remaining"] == "0"

    time.sleep(1.1)
    allowed3, _ = limiter.allow("tenant:test", limit=1, window_seconds=1)
    assert allowed3 is True
