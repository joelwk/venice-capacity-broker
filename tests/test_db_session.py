import pytest

from db import session as db_session

import sys

@pytest.fixture(autouse=True)
def reload_db_session(monkeypatch):
    """Force reload db.session before each test to avoid caching issues."""
    if 'db.session' in sys.modules:
        del sys.modules['db.session']
    import db.session  # Re-import fresh
    monkeypatch.setattr('sys.modules', sys.modules, raising=False)  # Ensure consistent state


def _spy_create_engine(calls):
    def _inner(target_url, **kwargs):
        result = {"target_url": target_url, "kwargs": kwargs}
        calls.append(result)
        return result

    return _inner


def test_get_engine_placeholder_url_short_circuits(monkeypatch, tmp_path):
    sqlite_url = f"sqlite:///{(tmp_path / 'fallback.db')}"
    placeholder_hits = []
    engine_calls = []

    def fake_placeholder(value: str) -> bool:
        placeholder_hits.append(value)
        return True

    monkeypatch.setattr(db_session, "_looks_like_placeholder", fake_placeholder)
    monkeypatch.setattr(db_session, "create_engine", _spy_create_engine(engine_calls))

    for placeholder_url in (
        "postgresql://user:password@host:5432/database",
        "postgresql://***host:5432/database",
        "***host:5432/database",
    ):
        monkeypatch.setenv("SQL_DATABASE_URL", placeholder_url)
        monkeypatch.setenv("SQLITE_FALLBACK_URL", sqlite_url)
        engine_calls.clear()
        placeholder_hits.clear()

        engine = db_session.get_engine()

        assert placeholder_hits == [placeholder_url]
        assert len(engine_calls) == 1
        assert engine is engine_calls[0]
        assert engine_calls[0]["target_url"] == sqlite_url
        kwargs = engine_calls[0]["kwargs"]
        assert kwargs["echo"] is False
        assert kwargs["connect_args"]["check_same_thread"] is False


def test_get_engine_falls_back_on_missing_driver(monkeypatch, tmp_path):
    sqlite_url = f"sqlite:///{(tmp_path / 'fallback.db')}"
    engine_calls = []
    placeholder_hits = []

    def fake_placeholder(_: str) -> bool:
        placeholder_hits.append(_)
        return False

    def fake_create_engine(target_url, **kwargs):
        result = {"target_url": target_url, "kwargs": kwargs}
        engine_calls.append(result)
        if target_url.startswith("postgresql"):
            raise ModuleNotFoundError("No module named 'psycopg2'")
        return result

    monkeypatch.setattr(db_session, "_looks_like_placeholder", fake_placeholder)
    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)

    for real_url in (
        "postgresql://real.example:5432/app",
        "postgresql://mainnet.base.org:5432/app?sslmode=require",
    ):
        monkeypatch.setenv("SQL_DATABASE_URL", real_url)
        monkeypatch.setenv("SQLITE_FALLBACK_URL", sqlite_url)
        engine_calls.clear()
        placeholder_hits.clear()

        engine = db_session.get_engine()

        assert placeholder_hits == [real_url]
        assert len(engine_calls) == 2
        assert engine is engine_calls[-1]
        assert engine_calls[0]["target_url"] == real_url
        assert engine_calls[1]["target_url"] == sqlite_url
        kwargs = engine_calls[1]["kwargs"]
        assert kwargs["connect_args"]["check_same_thread"] is False


def test_placeholder_detection_variants():
    for candidate in (
        "postgresql://user:password@host:5432/database",
        "postgresql://***host:5432/database",
        "postgresql://example.com/database",
        "***host:5432/database",
    ):
        assert db_session._looks_like_placeholder(candidate)


def test_placeholder_detection_real_url():
    assert not db_session._looks_like_placeholder("postgresql://mainnet.base.org:5432/app?sslmode=require")
