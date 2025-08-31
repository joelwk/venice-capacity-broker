from __future__ import annotations

import os
import json
from datetime import datetime, timedelta
from pathlib import Path


def _import_app(tmp_store: Path):
    # Import broker app from file path to ensure fresh env per test
    import importlib.util

    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_store)
    app_path = Path("apps/broker-api/app.py").resolve()
    spec = importlib.util.spec_from_file_location("broker_api_app_counters", str(app_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _seed_tenant(mod, tenant_id: str = "t-123") -> None:  # noqa: ANN001
    # Load Tenant dataclass from tenant_store via file path as well
    Tenant = mod.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id=tenant_id, label="T1", subkey="sub-ctrs", quota=0)
    mod.store.upsert(tenant)


def _setup_sqlite_db(file: Path):
    os.environ["SQL_DATABASE_URL"] = f"sqlite:///{file}"
    try:
        from db.session import create_db_and_tables

        create_db_and_tables()
    except Exception:
        import pytest

        pytest.skip("sqlmodel not installed; skipping counters endpoint tests")


def test_admin_counters_ok_and_filters(tmp_path, monkeypatch):
    os.environ["BROKER_ADMIN_TOKEN"] = "dev-admin"
    os.environ["BROKER_REQUIRE_ADMIN_TOKEN"] = "true"

    store_file = tmp_path / "tenants.json"
    db_file = tmp_path / "test.db"

    _setup_sqlite_db(db_file)
    app_mod = _import_app(store_file)
    _seed_tenant(app_mod, tenant_id="t-abc")

    # Seed one SQL counter row directly
    from sqlmodel import Session
    from db.session import get_engine
    from db.models import Counter

    engine = get_engine()
    now = datetime.utcnow().replace(microsecond=0)
    row1 = Counter(tenant_id="t-abc", scope="chat", model=None, bucket_start=now - timedelta(minutes=2), bucket_seconds=60, count=5)
    row2 = Counter(tenant_id="t-abc", scope="chat", model=None, bucket_start=now - timedelta(minutes=1), bucket_seconds=60, count=7)
    with Session(engine) as s:  # type: ignore[call-arg]
        s.add(row1)
        s.add(row2)
        s.commit()

    # Also seed a KV shadow entry and compact (parity with CLI)
    # Use CLI command to compact with --force
    # Create a KV key matching limiter pattern: rl:tenant:{id}:chat:{bucket_epoch}
    from libs.kv import KVStore

    kv = KVStore()
    bucket_epoch = int((now - timedelta(minutes=3)).timestamp() // 60 * 60)
    kv.set(f"rl:tenant:t-abc:chat:{bucket_epoch}", 3)

    from apps.cli.main import cmd_compact_counters, argparse

    ns = argparse.Namespace(force=True)
    cmd_compact_counters(ns)

    # Call admin endpoint and verify shape/order
    from fastapi.testclient import TestClient

    client = TestClient(app_mod.app)
    hdrs = {"Authorization": "Bearer dev-admin"}
    r = client.get("/v1/debug/counters", params={"tenant_id": "t-abc", "asc": False, "limit": 2}, headers=hdrs)
    assert r.status_code == 200, r.text
    data = r.json()
    assert isinstance(data, list)
    assert len(data) == 2
    # Latest bucket first
    ts0 = data[0]["bucket_start"]
    ts1 = data[1]["bucket_start"]
    assert ts0 >= ts1
    for d in data:
        assert d["tenant_id"] == "t-abc"
        assert d["scope"] == "chat"
        assert int(d["bucket_seconds"]) == 60
        assert "count" in d

    # Compare with CLI counters:show JSON (parity)
    from apps.cli.main import cmd_counters_show
    from io import StringIO
    import sys

    args = argparse.Namespace(tenant="t-abc", scope="chat", model=None, bucket_seconds=60, since=None, until=None, limit=2, desc=True, json=True)
    buf = StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        cmd_counters_show(args)
    finally:
        sys.stdout = old
    cli_out = json.loads(buf.getvalue())
    # Rows should match in order and keys
    assert [r["bucket_start"] for r in cli_out] == [r["bucket_start"] for r in data]


def test_admin_counters_auth_and_errors(tmp_path):
    os.environ["BROKER_ADMIN_TOKEN"] = "dev-admin"
    os.environ["BROKER_REQUIRE_ADMIN_TOKEN"] = "true"

    store_file = tmp_path / "tenants.json"
    db_file = tmp_path / "test.db"

    _setup_sqlite_db(db_file)
    app_mod = _import_app(store_file)
    _seed_tenant(app_mod, tenant_id="t-err")

    from fastapi.testclient import TestClient

    client = TestClient(app_mod.app)
    # 401 missing/invalid auth
    r = client.get("/v1/debug/counters", params={"tenant_id": "t-err"})
    assert r.status_code == 401

    # 400 missing tenant_id
    hdrs = {"Authorization": "Bearer dev-admin"}
    r2 = client.get("/v1/debug/counters", headers=hdrs)
    assert r2.status_code == 400

    # 400 invalid bucket_seconds
    r3 = client.get("/v1/debug/counters", params={"tenant_id": "t-err", "bucket_seconds": "oops"}, headers=hdrs)
    assert r3.status_code == 400
