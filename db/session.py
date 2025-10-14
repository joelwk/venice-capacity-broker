from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, Iterator, List

from libs.telemetry.logger import get_logger

logger = get_logger("db.session")


_ENGINE_ATTEMPTS: List[Dict[str, Any]] = []


def get_engine_attempts() -> List[Dict[str, Any]]:
    """Return a copy of the most recent engine creation attempts."""

    return list(_ENGINE_ATTEMPTS)


def _reset_engine_attempts() -> None:
    _ENGINE_ATTEMPTS.clear()

try:
    from sqlmodel import SQLModel, Session, create_engine
except Exception:  # noqa: BLE001
    SQLModel = None  # type: ignore
    Session = None  # type: ignore
    create_engine = None  # type: ignore


def _ensure_sqlmodel() -> None:
    """Ensure sqlmodel/sqlalchemy dependencies are available."""
    global SQLModel, Session, create_engine
    if SQLModel is not None and Session is not None and create_engine is not None:
        return
    try:
        from sqlmodel import SQLModel as _SQLModel, Session as _Session, create_engine as _create_engine  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("sqlmodel/sqlalchemy not installed") from exc
    SQLModel = _SQLModel  # type: ignore[assignment]
    Session = _Session  # type: ignore[assignment]
    current_module = getattr(create_engine, "__module__", None)
    target_module = getattr(_create_engine, "__module__", None)
    if create_engine is None or current_module == target_module:
        create_engine = _create_engine  # type: ignore[assignment]


def _db_url() -> str:
    url = os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("POSTGRES_HOST")
    if not host:
        # Fall back to a local SQLite database when no Postgres configuration is
        # provided (common in tests/CI environments).
        return "sqlite:///./broker.db"
    user = os.getenv("POSTGRES_USER", "postgres")
    pwd = os.getenv("POSTGRES_PASSWORD", "")
    db = os.getenv("POSTGRES_DB", "postgres")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"

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


def _call_engine_factory(target_url: str, **kwargs: Any):
    """Invoke the currently configured engine factory."""

    engine_factory = _resolve_engine_factory()
    attempt: Dict[str, Any] = {
        "target_url": target_url,
        "kwargs": dict(kwargs),
        "succeeded": False,
        "error": None,
    }
    _ENGINE_ATTEMPTS.append(attempt)
    try:
        engine = engine_factory(target_url, **kwargs)  # type: ignore[misc]
    except Exception as exc:  # noqa: BLE001
        attempt["error"] = exc
        raise
    else:
        attempt["succeeded"] = True
        attempt["engine"] = engine
        return engine


def _sqlite_fallback_engine(echo: bool, source_url: str, warning_fmt: str):
    fallback_url = _fallback_sqlite_url()
    logger.warning(warning_fmt, source_url, fallback_url)
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
            SQLModel.metadata.create_all(engine)  # type: ignore[union-attr]
    except Exception:
        logger.debug("failed to auto-create tables for SQLite fallback", exc_info=True)
    return engine



def get_engine():
    _reset_engine_attempts()
    echo = (os.getenv("DATABASE_ECHO") or "false").strip().lower() == "true"
    pool_size = int(os.getenv("DATABASE_POOL_SIZE") or 5)
    url = _db_url()
    is_sqlite = _is_sqlite(url)

    placeholder = _looks_like_placeholder(url)

    if placeholder and not is_sqlite:
        return _sqlite_fallback_engine(echo, url, "placeholder database URL (%s); using SQLite %s")

    _ensure_sqlmodel()

    kwargs: Dict[str, Any] = {"echo": echo}
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

    try:
        return _call_engine_factory(url, **kwargs)
    except ModuleNotFoundError as exc:
        message = str(exc).lower()
        if "psycopg2" in message and not is_sqlite:
            return _sqlite_fallback_engine(echo, url, "psycopg2 missing for %s; falling back to SQLite %s")
        raise
    except Exception:
        if not is_sqlite:
            return _sqlite_fallback_engine(echo, url, "database connection failed for %s; using SQLite %s")
        raise


def create_db_and_tables() -> None:
    _ensure_sqlmodel()
    engine = get_engine()
    SQLModel.metadata.create_all(engine)  # type: ignore[union-attr]


def get_session() -> Iterator["Session"]:
    _ensure_sqlmodel()
    engine = get_engine()
    with Session(engine) as session:  # type: ignore[call-arg]
        yield session
