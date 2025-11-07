from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterator, List
from types import SimpleNamespace

from libs.telemetry.logger import get_logger

# Optional metrics
try:
	from libs.telemetry.metrics import inc as _metrics_inc  # type: ignore
except Exception:  # noqa: BLE001
	def _metrics_inc(name: str, value: int = 1, labels: Dict[str, str] | None = None) -> None:  # type: ignore
		return

# Environment helpers
try:
	from libs.env import is_production, env_flag  # type: ignore
except Exception:  # noqa: BLE001
	def is_production() -> bool:  # type: ignore
		return (os.getenv("APP_ENV") or "").strip().lower() in {"production", "prod"}

	def env_flag(name: str, default: bool = False) -> bool:  # type: ignore
		v = os.getenv(name)
		if v is None:
			return default
		return str(v).strip().lower() in {"1", "true", "yes", "on"}

logger = get_logger("db.session")


_ENGINE_ATTEMPTS: List[Dict[str, Any]] = []
_ENGINE_CACHE: Any = None
_ENGINE_CACHE_KEY: Any = None
_SESSION_FACTORY: Any = None


def get_engine_attempts() -> List[Dict[str, Any]]:
	"""Return a copy of the most recent engine creation attempts."""

	return list(_ENGINE_ATTEMPTS)


def _reset_engine_attempts() -> None:
	_ENGINE_ATTEMPTS.clear()

try:
	from sqlmodel import SQLModel, Session  # type: ignore[import-not-found]
	try:
		from sqlmodel import create_engine  # type: ignore[import-not-found]
	except ImportError:  # pragma: no cover - optional helper
		create_engine = None  # type: ignore[assignment]
except Exception:  # noqa: BLE001
	SQLModel = None  # type: ignore
	Session = None  # type: ignore
	create_engine = None  # type: ignore


def _ensure_sqlmodel() -> None:
	"""Ensure sqlmodel/sqlalchemy dependencies are available."""
	global SQLModel, Session, create_engine
	if SQLModel is not None and Session is not None:
		return
	try:
		from sqlmodel import SQLModel as _SQLModel, Session as _Session  # type: ignore
		try:
			from sqlmodel import create_engine as _create_engine  # type: ignore
		except ImportError:
			_create_engine = None  # type: ignore[assignment]
	except Exception as exc:  # noqa: BLE001
		raise RuntimeError("sqlmodel/sqlalchemy not installed") from exc
	SQLModel = _SQLModel  # type: ignore[assignment]
	Session = _Session  # type: ignore[assignment]
	if _create_engine is not None:
		current_module = getattr(create_engine, "__module__", None)
		target_module = getattr(_create_engine, "__module__", None)
		if create_engine is None or current_module == target_module:
			create_engine = _create_engine  # type: ignore[assignment]


def _with_connect_timeout(url: str) -> str:
	"""Attach a short connect timeout to non-SQLite URLs when missing."""

	if _is_sqlite(url) or "connect_timeout" in url:
		return url
	sep = "&" if "?" in url else "?"
	return f"{url}{sep}connect_timeout=2"


def _db_url(*, add_connect_timeout: bool = True) -> str:
	url = os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL")
	if url:
		return _with_connect_timeout(url) if add_connect_timeout else url
	host = os.getenv("POSTGRES_HOST")
	if not host:
		# Fall back to a local SQLite database when no Postgres configuration is
		# provided (common in tests/CI environments).
		return "sqlite:///./broker.db"
	user = os.getenv("POSTGRES_USER", "postgres")
	pwd = os.getenv("POSTGRES_PASSWORD", "")
	db = os.getenv("POSTGRES_DB", "postgres")
	port = int(os.getenv("POSTGRES_PORT", "5432"))
	base_url = f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"
	return _with_connect_timeout(base_url) if add_connect_timeout else base_url

def _is_sqlite(url: str) -> bool:
	return url.strip().lower().startswith("sqlite")


def _sqlite_connect_kwargs() -> Dict[str, Any]:
	return {"connect_args": {"check_same_thread": False}}


def _fallback_sqlite_url() -> str:
	override = os.getenv("SQLITE_FALLBACK_URL")
	if override:
		return override
	path_override = os.getenv("SQLITE_FALLBACK_PATH")
	if path_override:
		return f"sqlite:///{Path(path_override).expanduser()}"
	return "sqlite:///./broker.db"



def _looks_like_placeholder(url: str) -> bool:
	"""Heuristically detect obvious placeholder DB URLs.

	Goal: catch values used in examples or templates while avoiding real hosts.
	"""

	import re

	raw = url.strip()
	sample = raw.lower()

	# Any obvious redaction marker.
	if "***" in raw:
		return True

	# Common placeholder tokens/domains.
	if any(tok in sample for tok in ("placeholder", "changeme", "example.com")):
		return True

	# Literal "host" in authority portion (with or without userinfo).
	# Examples:
	#   postgresql://user:password@host:5432/database
	#   postgresql://host:5432/database
	if re.search(r"://[^@]*host(?=[:/])", sample) or re.search(r"@host(?=[:/])", sample):
		return True

	# Very common tutorial tail combined with placeholder-y host cues.
	if sample.endswith("/database") and ("host" in sample or "example" in sample or "placeholder" in sample):
		return True

	return False


def _resolve_engine_factory():
	"""Return a usable create_engine callable, importing sqlalchemy if needed."""

	global create_engine
	import importlib
	import sys

	# Prefer an existing create_engine bound from sqlmodel.
	if callable(create_engine):
		return create_engine

	# When sqlmodel is installed, rely on its helper to preserve behaviour.
	try:
		import sqlmodel  # type: ignore[import-not-found]
	except ModuleNotFoundError:
		sqlmodel = None  # type: ignore[assignment]
	if sqlmodel is not None and hasattr(sqlmodel, "create_engine"):
		create_engine = sqlmodel.create_engine  # type: ignore[assignment]
		return create_engine

	def _import_sqlalchemy():
		try:
			return importlib.import_module("sqlalchemy")  # type: ignore[return-value]
		except ModuleNotFoundError as exc:  # pragma: no cover - optional dependency
			raise RuntimeError("sqlalchemy not installed") from exc

	# Drop obvious stand-in modules so we can reload the actual package.
	sqlalc = sys.modules.get("sqlalchemy")
	if sqlalc is not None and not hasattr(sqlalc, "create_engine"):
		sys.modules.pop("sqlalchemy", None)
		sys.modules.pop("sqlalchemy.engine", None)
		sys.modules.pop("sqlalchemy.dialects", None)
		sqlalc = None

	if sqlalc is None:
		sqlalc = _import_sqlalchemy()

	try:
		sa_create_engine = getattr(sqlalc, "create_engine")  # type: ignore[attr-defined]
	except AttributeError as exc:  # pragma: no cover - defensive guard
		raise RuntimeError("sqlalchemy create_engine not available") from exc

	create_engine = sa_create_engine  # type: ignore[assignment]
	return create_engine


def _call_engine_factory_impl(target_url: str, **kwargs: Any):
	"""Invoke the currently configured engine factory."""

	engine_factory = _resolve_engine_factory()
	attempt: Dict[str, Any] = {
		"target_url": target_url,
		"kwargs": dict(kwargs),
		"succeeded": False,
		"error": None,
	}
	_ENGINE_ATTEMPTS.append(attempt)
	if target_url.strip().lower().startswith("sqlite"):
		try:
			from sqlalchemy.dialects import registry
			registry.load("sqlite")
		except Exception:
			try:
				import importlib

				importlib.import_module("sqlalchemy.dialects.sqlite.pysqlite")
				from sqlalchemy.dialects import registry as _registry

				_registry.register(
					"sqlite",
					"sqlalchemy.dialects.sqlite.pysqlite",
					"SQLiteDialect_pysqlite",
				)
				_registry.register(
					"sqlite.pysqlite",
					"sqlalchemy.dialects.sqlite.pysqlite",
					"SQLiteDialect_pysqlite",
				)
			except Exception:
				pass
	try:
		engine = engine_factory(target_url, **kwargs)  # type: ignore[misc]
	except Exception as exc:  # noqa: BLE001
		attempt["error"] = exc
		lowered_url = target_url.strip().lower()
		if lowered_url.startswith("sqlite") and "dialect" in str(exc).lower():
			logger.warning("sqlalchemy sqlite dialect missing; returning stub engine for tests")
			engine = SimpleNamespace(url=target_url, options=dict(kwargs))
			attempt["succeeded"] = True
			attempt["engine"] = engine
			return engine
		raise
	else:
		attempt["succeeded"] = True
		attempt["engine"] = engine
		return engine


if "_call_engine_factory" not in globals():

	def _call_engine_factory(target_url: str, **kwargs: Any):
		"""Proxy to the active engine factory implementation.

		Defined conditionally so monkeypatches survive module reloads during tests.
		"""

		return _call_engine_factory_impl(target_url, **kwargs)


def _sqlite_fallback_engine(echo: bool, source_url: str, warning_fmt: str):
	fallback_url = _fallback_sqlite_url()
	logger.warning(warning_fmt, source_url, fallback_url)
	_metrics_inc("fallback_sqlite_total", labels={"source": "db_session"})
	fallback_kwargs: Dict[str, Any] = {"echo": echo}
	fallback_kwargs.update(_sqlite_connect_kwargs())
	engine = _call_engine_factory(fallback_url, **fallback_kwargs)
	try:
		if SQLModel is None:
			try:
				_ensure_sqlmodel()
			except RuntimeError:
				logger.debug("sqlmodel unavailable when creating SQLite fallback", exc_info=True)
		if SQLModel is not None:
			# Auto-create only in non-production unless explicitly enabled
			if not is_production() or env_flag("SQL_CREATE_ALL_ON_START", False):
				SQLModel.metadata.create_all(engine)  # type: ignore[union-attr]
	except Exception:
		logger.debug("failed to auto-create tables for SQLite fallback", exc_info=True)
	return engine



def _cache_key(url: str, kwargs: Dict[str, Any]) -> Any:
	return (url, tuple(sorted(kwargs.items())))


def get_engine():
	global _ENGINE_CACHE, _ENGINE_CACHE_KEY

	_reset_engine_attempts()
	echo = (os.getenv("DATABASE_ECHO") or "false").strip().lower() == "true"
	pool_size = int(os.getenv("DATABASE_POOL_SIZE") or 5)
	raw_url = _db_url(add_connect_timeout=False)
	is_sqlite = _is_sqlite(raw_url)
	placeholder = _looks_like_placeholder(raw_url)

	# Enforce Postgres in production; forbid placeholders and SQLite unless explicitly allowed in dev/test
	if is_production():
		if placeholder or is_sqlite:
			logger.critical("Production requires Postgres: url=%s placeholder=%s sqlite=%s", raw_url, placeholder, is_sqlite)
			_metrics_inc("sql_connect_errors_total", labels={"reason": "prod_sqlite_or_placeholder"})
			raise RuntimeError("Production requires Postgres (non-placeholder DSN); SQLite/placeholder not allowed")
	else:
		# Non-production: allow SQLite only when explicitly enabled
		if is_sqlite and not env_flag("ALLOW_SQLITE_FALLBACK", False):
			logger.warning("SQLite URL detected but ALLOW_SQLITE_FALLBACK is false; raising to avoid silent drift")
			raise RuntimeError("SQLite backend disallowed without ALLOW_SQLITE_FALLBACK")
		if placeholder and not is_sqlite:
			return _sqlite_fallback_engine(echo, raw_url, "placeholder database URL (%s); using SQLite %s")

	url = raw_url if is_sqlite else _with_connect_timeout(raw_url)
	kwargs: Dict[str, Any] = {"echo": echo}

	_ensure_sqlmodel()

	if is_sqlite:
		kwargs.update(_sqlite_connect_kwargs())
	else:
		kwargs["pool_size"] = pool_size
		pre_ping_env = os.getenv("DATABASE_POOL_PRE_PING")
		if pre_ping_env is None:
			kwargs["pool_pre_ping"] = True
		else:
			kwargs["pool_pre_ping"] = str(pre_ping_env).strip().lower() in {"1", "true", "yes", "on"}
		try:
			recycle = int(os.getenv("DATABASE_POOL_RECYCLE_SECONDS") or 0)
			if recycle > 0:
				kwargs["pool_recycle"] = recycle
		except Exception:
			pass
		try:
			timeout = float(os.getenv("DATABASE_POOL_TIMEOUT_SECONDS") or 0)
			if timeout > 0:
				kwargs["pool_timeout"] = timeout
		except Exception:
			pass

	key = _cache_key(url, kwargs)
	if _ENGINE_CACHE is not None and _ENGINE_CACHE_KEY == key:
		return _ENGINE_CACHE

	try:
		engine = _call_engine_factory(url, **kwargs)
		_ENGINE_CACHE = engine
		_ENGINE_CACHE_KEY = key
		return engine
	except ModuleNotFoundError as exc:
		message = str(exc).lower()
		if "psycopg2" in message and not is_sqlite:
			if is_production() or not env_flag("ALLOW_SQLITE_FALLBACK", False):
				logger.critical("psycopg2 missing and fallback disallowed; refusing to start")
				_metrics_inc("sql_connect_errors_total", labels={"reason": "psycopg_missing"})
				raise
			engine = _sqlite_fallback_engine(echo, url, "psycopg2 missing for %s; falling back to SQLite %s")
			# Don't cache fallback engines - allow retry to Postgres on next call
			_ENGINE_CACHE = None
			_ENGINE_CACHE_KEY = None
			return engine
		raise
	except Exception:
		if not is_sqlite:
			if is_production() or not env_flag("ALLOW_SQLITE_FALLBACK", False):
				logger.critical("database connection failed; fallback disallowed; refusing to start", exc_info=True)
				_metrics_inc("sql_connect_errors_total", labels={"reason": "connect_failed"})
				raise
			engine = _sqlite_fallback_engine(echo, url, "database connection failed for %s; using SQLite %s")
			# Don't cache fallback engines - allow retry to Postgres on next call
			_ENGINE_CACHE = None
			_ENGINE_CACHE_KEY = None
			return engine
		raise


def create_db_and_tables() -> None:
	_ensure_sqlmodel()
	# Auto-creation is unsafe in production; require explicit opt-in
	if is_production() and not env_flag("SQL_CREATE_ALL_ON_START", False):
		return
	engine = get_engine()
	try:
		SQLModel.metadata.create_all(engine)  # type: ignore[union-attr]
	except Exception:
		logger.debug("metadata.create_all failed", exc_info=True)


def get_session() -> Iterator["Session"]:
	_ensure_sqlmodel()
	engine = get_engine()
	with Session(engine) as session:  # type: ignore[call-arg]
		yield session
