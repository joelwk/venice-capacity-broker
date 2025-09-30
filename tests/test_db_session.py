import pytest

from db import session as db_session


def test_get_engine_placeholder_url_short_circuits(monkeypatch, tmp_path):
    placeholder_url = "postgresql://user:pass@host:5432/database"
    sqlite_url = f"sqlite:///{(tmp_path / 'fallback.db')}"
    monkeypatch.setenv("SQL_DATABASE_URL", placeholder_url)
    monkeypatch.setenv("SQLITE_FALLBACK_URL", sqlite_url)

    calls = []

    def fake_create_engine(target_url, **kwargs):
        calls.append((target_url, kwargs))
        return {"url": target_url, "kwargs": kwargs}

    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)

    engine = db_session.get_engine()

    assert engine["url"] == sqlite_url
    assert len(calls) == 1
    assert calls[0][0] == sqlite_url
    assert calls[0][1]["echo"] is False
    assert calls[0][1]["connect_args"]["check_same_thread"] is False


def test_get_engine_falls_back_on_missing_driver(monkeypatch, tmp_path):
    postgres_url = "postgresql://real.example:5432/app"
    sqlite_url = f"sqlite:///{(tmp_path / 'fallback.db')}"
    monkeypatch.setenv("SQL_DATABASE_URL", postgres_url)
    monkeypatch.setenv("SQLITE_FALLBACK_URL", sqlite_url)

    calls = []

    def fake_create_engine(target_url, **kwargs):
        calls.append((target_url, kwargs))
        if target_url.startswith("postgresql"):
            raise ModuleNotFoundError("No module named 'psycopg2'")
        return {"url": target_url, "kwargs": kwargs}

    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)

    engine = db_session.get_engine()

    assert engine["url"] == sqlite_url
    assert calls[0][0] == postgres_url
    assert calls[1][0] == sqlite_url
    assert calls[1][1]["connect_args"]["check_same_thread"] is False
