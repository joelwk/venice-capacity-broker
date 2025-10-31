from __future__ import annotations

import os
import sys
from pathlib import Path
import importlib.util
from types import SimpleNamespace, ModuleType
from datetime import datetime, timedelta, timezone


def _make_fake_sql_stubs(rows):
    """Install lightweight stubs for sqlmodel and sqlalchemy used by /v1/debug/counters.

    - sqlmodel.select returns a query object supporting where/order_by/limit
    - sqlmodel.Session.exec(q).all() evaluates filters against provided rows
    - sqlalchemy.desc returns a sentinel with is_desc=True so order_by can detect
    - db.models.Counter is patched so attribute comparisons yield ('eq', field, value)
    """

    prev_sqlmodel = sys.modules.get("sqlmodel")
    prev_sqlalchemy = sys.modules.get("sqlalchemy")

    # Pre-stub a minimal sqlmodel so db.models can import classes with table=True
    import sys as _sys
    from types import ModuleType as _ModuleType

    _sqlm = _ModuleType("sqlmodel")

    class _SQLModel:
        def __init_subclass__(cls, **kwargs):  # accept table=True
            pass

    def Field(*args, **kwargs):  # noqa: ANN001
        return None

    _sqlm.SQLModel = _SQLModel  # type: ignore[attr-defined]
    _sqlm.Field = Field  # type: ignore[attr-defined]
    _sys.modules["sqlmodel"] = _sqlm

    # Stub sqlalchemy.desc
    sqlalc = ModuleType("sqlalchemy")

    class _Desc:
        def __init__(self, arg):
            # arg may be a FieldProxy; record its field name if present
            self.is_desc = True
            self.field = getattr(arg, "field", None)

    def desc(arg):  # noqa: ANN001
        return _Desc(arg)

    sqlalc.desc = desc  # type: ignore[attr-defined]
    sys.modules["sqlalchemy"] = sqlalc

    # Field proxy that returns comparable tuples for equality
    class FieldProxy:
        def __init__(self, name: str):
            self.field = name

        def __eq__(self, other):  # noqa: D401, ANN001
            # Produce a simple tuple the fake query understands
            return ("eq", self.field, other)

    # Patch db.models.Counter so attribute access yields FieldProxy
    import db.models as db_models

    class _CounterSentinel:
        def __getattr__(self, name: str):  # noqa: D401
            return FieldProxy(name)

    db_models.Counter = _CounterSentinel()  # type: ignore[assignment]

    # Extend sqlmodel stub (Session + select)
    sqlm = sys.modules["sqlmodel"]
    setattr(sqlm, "_FAKE_ROWS", list(rows))

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):  # noqa: D401
            return list(self._rows)

    class _SelectQuery:
        def __init__(self, rows):
            self._rows = list(rows)
            self._filters = []
            self._asc = False  # default desc
            self._limit = None

        def where(self, cond):  # noqa: ANN001
            if isinstance(cond, tuple) and len(cond) == 3 and cond[0] == "eq":
                self._filters.append(cond)
            return self

        def order_by(self, arg):  # noqa: ANN001
            # asc if not a desc sentinel
            self._asc = not bool(getattr(arg, "is_desc", False))
            return self

        def limit(self, n):  # noqa: ANN001
            try:
                self._limit = int(n)
            except Exception:
                self._limit = None
            return self

    class Session:  # noqa: D401
        def __init__(self, engine):  # noqa: ANN001
            self._engine = engine

        def __enter__(self):  # support `with Session(engine) as s`
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

        def exec(self, query):  # noqa: ANN001
            # Start with provided rows
            rows = list(getattr(sqlm, "_FAKE_ROWS", []))
            # Apply filters
            for kind, field, value in getattr(query, "_filters", []):
                if kind == "eq":
                    rows = [r for r in rows if getattr(r, field) == value]
            # Sort by bucket_start
            rows.sort(key=lambda r: r.bucket_start, reverse=not getattr(query, "_asc", False))
            # Apply limit
            lim = getattr(query, "_limit", None)
            if lim is not None:
                rows = rows[: int(lim)]
            return _Result(rows)

    def select(_model):  # noqa: ANN001
        return _SelectQuery(getattr(sqlm, "_FAKE_ROWS", []))

    sqlm.Session = Session  # type: ignore[attr-defined]
    sqlm.select = select  # type: ignore[attr-defined]

    def _restore():
        if prev_sqlmodel is None:
            sys.modules.pop("sqlmodel", None)
        else:
            sys.modules["sqlmodel"] = prev_sqlmodel
        if prev_sqlalchemy is None:
            sys.modules.pop("sqlalchemy", None)
        else:
            sys.modules["sqlalchemy"] = prev_sqlalchemy
        for _mod in ("db.session", "db.models"):
            sys.modules.pop(_mod, None)

    return _restore


def _load_app(module_name: str):
    app_path = Path("apps/broker_api/app.py").resolve()
    spec = importlib.util.spec_from_file_location(module_name, str(app_path))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register module in sys.modules before exec_module so module code can access sys.modules[__name__]
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return mod


def _add_tenant(broker_app, tenant_id: str, subkey: str = "sub-1", label: str = "T1"):
    Tenant = broker_app.Tenant  # type: ignore[attr-defined]
    tenant = Tenant(id=tenant_id, label=label, subkey=subkey, quota=0)
    broker_app.store.upsert(tenant)


def test_counters_requires_admin_token(monkeypatch, tmp_path):
    # Env and store
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_path / "tenants.json")
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"

    # Prepare fake rows and SQL stubs
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            tenant_id="t1",
            scope="chat",
            model=None,
            bucket_start=now - timedelta(minutes=3),
            bucket_seconds=60,
            count=5,
        ),
    ]
    cleanup = _make_fake_sql_stubs(rows)
    try:
        # Patch DB engine fetch to avoid real SQL deps
        import db.session as db_session
        monkeypatch.setattr(db_session, "get_engine", lambda: object())

        broker_app = _load_app("broker_api_counters_auth")
        _add_tenant(broker_app, "t1")

        from fastapi.testclient import TestClient

        client = TestClient(broker_app.app)

        # No auth -> 401
        r = client.get("/v1/debug/counters", params={"tenant_id": "t1"})
        assert r.status_code == 401
    finally:
        cleanup()


def test_counters_validates_tenant_and_bucket_seconds(monkeypatch, tmp_path):
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_path / "tenants2.json")
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"

    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(
            tenant_id="t1",
            scope="chat",
            model=None,
            bucket_start=now,
            bucket_seconds=60,
            count=1,
        ),
    ]
    cleanup = _make_fake_sql_stubs(rows)
    try:
        # Patch DB engine fetch to avoid real SQL deps
        import db.session as db_session
        monkeypatch.setattr(db_session, "get_engine", lambda: object())

        broker_app = _load_app("broker_api_counters_validate")
        _add_tenant(broker_app, "t1")

        from fastapi.testclient import TestClient

        client = TestClient(broker_app.app)
        headers = {"Authorization": "Bearer adminkey"}

        # Missing tenant_id -> 422 (FastAPI validation error)
        r_missing = client.get("/v1/debug/counters", headers=headers)
        assert r_missing.status_code == 422

        # Invalid bucket_seconds -> 400
        r_bad_bs = client.get("/v1/debug/counters", headers=headers, params={"tenant_id": "t1", "bucket_seconds": "abc"})
        assert r_bad_bs.status_code == 400
    finally:
        cleanup()


def test_counters_filters_limit_and_asc(monkeypatch, tmp_path):
    os.environ["BROKER_STORE_BACKEND"] = "json"
    os.environ["BROKER_STORE_FILE"] = str(tmp_path / "tenants3.json")
    os.environ["BROKER_ADMIN_TOKEN"] = "adminkey"

    # Build dataset across scopes and bucket sizes
    now = datetime.now(timezone.utc)
    rows = [
        SimpleNamespace(tenant_id="t1", scope="chat", model=None, bucket_start=now - timedelta(minutes=5), bucket_seconds=60, count=1),
        SimpleNamespace(tenant_id="t1", scope="chat", model=None, bucket_start=now - timedelta(minutes=4), bucket_seconds=60, count=2),
        SimpleNamespace(tenant_id="t1", scope="chat", model=None, bucket_start=now - timedelta(minutes=3), bucket_seconds=60, count=3),
        SimpleNamespace(tenant_id="t1", scope="signals", model=None, bucket_start=now - timedelta(minutes=2), bucket_seconds=300, count=4),
        SimpleNamespace(tenant_id="t1", scope="signals", model=None, bucket_start=now - timedelta(minutes=1), bucket_seconds=300, count=5),
        SimpleNamespace(tenant_id="t2", scope="chat", model=None, bucket_start=now - timedelta(minutes=1), bucket_seconds=60, count=99),
    ]
    cleanup = _make_fake_sql_stubs(rows)
    try:
        # Patch DB engine fetch to avoid real SQL deps
        import db.session as db_session
        monkeypatch.setattr(db_session, "get_engine", lambda: object())

        broker_app = _load_app("broker_api_counters_filters")
        _add_tenant(broker_app, "t1")
        _add_tenant(broker_app, "t2")

        from fastapi.testclient import TestClient

        client = TestClient(broker_app.app)
        headers = {"Authorization": "Bearer adminkey"}

        # Filter to t1 + scope=chat + bucket_seconds=60; asc + limit=2
        params = {"tenant_id": "t1", "scope": "chat", "bucket_seconds": "60", "asc": "1", "limit": "2"}
        r = client.get("/v1/debug/counters", headers=headers, params=params)
        assert r.status_code == 200, r.text
        data = r.json()
        assert isinstance(data, list)
        assert len(data) == 2
        # Shape
        for item in data:
            assert set(["tenant_id", "scope", "model", "bucket_start", "bucket_seconds", "count"]).issubset(item.keys())
            assert item["tenant_id"] == "t1"
            assert item["scope"] == "chat"
            assert item["bucket_seconds"] == 60
        # Ascending by bucket_start
        ts = [item["bucket_start"] for item in data]
        assert ts == sorted(ts)
    finally:
        cleanup()
