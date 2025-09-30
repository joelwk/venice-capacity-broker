import pytest

from db import session as db_session


def _spy_create_engine(calls):
    def _inner(target_url, **kwargs):
        result = {"target_url": target_url, "kwargs": kwargs}
        calls.append(result)
        return result

    return _inner


def test_get_engine_placeholder_url_short_circuits(monkeypatch, tmp_path):
    placeholder_url = "postgresql://user:password@host:5432/database"
    sqlite_url = f"sqlite:///{(tmp_path / 'fallback.db')}"
    monkeypatch.setenv("SQL_DATABASE_URL", placeholder_url)
    monkeypatch.setenv("SQLITE_FALLBACK_URL", sqlite_url)

    calls = []
    monkeypatch.setattr(db_session, "create_engine", _spy_create_engine(calls))

    engine = db_session.get_engine()

    assert engine is calls[0]
    assert len(calls) == 1
    assert calls[0]["target_url"] == sqlite_url
    kwargs = calls[0]["kwargs"]
    assert kwargs["echo"] is False
    assert kwargs["connect_args"]["check_same_thread"] is False


def test_get_engine_falls_back_on_missing_driver(monkeypatch, tmp_path):
    postgres_url = "postgresql://real.example:5432/app"
    sqlite_url = f"sqlite:///{(tmp_path / 'fallback.db')}"
    monkeypatch.setenv("SQL_DATABASE_URL", postgres_url)
    monkeypatch.setenv("SQLITE_FALLBACK_URL", sqlite_url)

    calls = []

    def fake_create_engine(target_url, **kwargs):
        result = {"target_url": target_url, "kwargs": kwargs}
        calls.append(result)
        if target_url.startswith("postgresql"):
            raise ModuleNotFoundError("No module named 'psycopg2'")
        return result

    monkeypatch.setattr(db_session, "create_engine", fake_create_engine)

    engine = db_session.get_engine()

    assert engine is calls[-1]
    assert len(calls) == 2
    assert calls[0]["target_url"] == postgres_url
    assert calls[1]["target_url"] == sqlite_url
    kwargs = calls[1]["kwargs"]
    assert kwargs["connect_args"]["check_same_thread"] is False
