from __future__ import annotations

import os
import importlib
import contextlib
import pytest


def _set_env(env: dict[str, str]) -> None:
	for k in list(os.environ.keys()):
		if k in env:
			continue
		# leave unrelated vars
	pass
	for k, v in env.items():
		os.environ[k] = v


def test_db_session_forbids_sqlite_in_production(monkeypatch):
	monkeypatch.setenv("APP_ENV", "production")
	monkeypatch.setenv("SQL_DATABASE_URL", "sqlite:///./test.db")
	with pytest.raises(RuntimeError):
		mod = importlib.import_module("db.session")
		importlib.reload(mod)
		mod.get_engine()  # type: ignore[attr-defined]


def test_db_session_allows_sqlite_in_dev_with_flag(monkeypatch):
	monkeypatch.setenv("APP_ENV", "development")
	monkeypatch.setenv("ALLOW_SQLITE_FALLBACK", "1")
	monkeypatch.delenv("SQL_DATABASE_URL", raising=False)
	# no POSTGRES_HOST -> sqlite url path
	mod = importlib.import_module("db.session")
	importlib.reload(mod)
	eng = mod.get_engine()  # type: ignore[attr-defined]
	assert eng is not None


def test_kv_forbids_inmemory_in_production(monkeypatch):
	monkeypatch.setenv("APP_ENV", "production")
	monkeypatch.delenv("REDIS_URL", raising=False)
	monkeypatch.delenv("REPLIT_DB_URL", raising=False)
	with pytest.raises(RuntimeError):
		mod = importlib.import_module("libs.kv.client")
		importlib.reload(mod)
		mod.KVStore()  # type: ignore[attr-defined]


def test_kv_allows_inmemory_in_dev_with_flag(monkeypatch):
	monkeypatch.setenv("APP_ENV", "development")
	monkeypatch.setenv("ALLOW_INMEMORY_KV_FALLBACK", "1")
	monkeypatch.delenv("REDIS_URL", raising=False)
	monkeypatch.delenv("REPLIT_DB_URL", raising=False)
	mod = importlib.import_module("libs.kv.client")
	importlib.reload(mod)
	kv = mod.KVStore()  # type: ignore[attr-defined]
	kv.set("a", "1", ttl_s=1)
	assert kv.get("a") == "1"
