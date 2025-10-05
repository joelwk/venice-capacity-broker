from types import SimpleNamespace

import db.session as session


def _setup_postgres_env(monkeypatch):
    monkeypatch.setenv('POSTGRES_HOST', '127.0.0.1')
    monkeypatch.setenv('POSTGRES_USER', 'postgres')
    monkeypatch.setenv('POSTGRES_PASSWORD', 'secret')
    monkeypatch.setenv('POSTGRES_DB', 'postgres')
    monkeypatch.delenv('SQL_DATABASE_URL', raising=False)
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.delenv('DATABASE_POOL_PRE_PING', raising=False)
    monkeypatch.delenv('DATABASE_POOL_RECYCLE_SECONDS', raising=False)
    monkeypatch.delenv('DATABASE_POOL_TIMEOUT_SECONDS', raising=False)


def test_get_engine_sets_pool_pre_ping_by_default(monkeypatch):
    _setup_postgres_env(monkeypatch)
    captured = {}

    def fake_call(url, **kwargs):
        captured['url'] = url
        captured['kwargs'] = kwargs
        return object()

    monkeypatch.setattr(session, '_ensure_sqlmodel', lambda: None)
    monkeypatch.setattr(session, '_call_engine_factory', fake_call)
    session.get_engine()

    assert 'kwargs' in captured, 'engine factory was not invoked'
    assert captured['kwargs']['pool_pre_ping'] is True


def test_get_engine_allows_pool_pre_ping_disable(monkeypatch):
    _setup_postgres_env(monkeypatch)
    monkeypatch.setenv('DATABASE_POOL_PRE_PING', '0')
    captured = {}

    def fake_call(url, **kwargs):
        captured['url'] = url
        captured['kwargs'] = kwargs
        return object()

    monkeypatch.setattr(session, '_ensure_sqlmodel', lambda: None)
    monkeypatch.setattr(session, '_call_engine_factory', fake_call)
    session.get_engine()

    assert 'kwargs' in captured, 'engine factory was not invoked'
    assert captured['kwargs']['pool_pre_ping'] is False
