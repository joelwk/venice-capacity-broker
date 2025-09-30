from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterator

logger = logging.getLogger("db.session")

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
    sample = url.lower()
    tokens = ("user:password@host", "user:pass@host", "user:pass@", "example.com", "placeholder", "changeme")
    if any(tok in sample for tok in tokens):
        return True
    if '***' in sample:
        return True
    if '@host:' in sample or '://host:' in sample:
        return True
    if sample.endswith('/database') and ('host' in sample or 'placeholder' in sample or 'example' in sample):
        return True
    return False



def _sqlite_fallback_engine(echo: bool, source_url: str, warning_fmt: str):
    fallback_url = _fallback_sqlite_url()
    logger.warning(warning_fmt, source_url, fallback_url)
    fallback_kwargs: Dict[str, Any] = {"echo": echo}
    fallback_kwargs.update(_sqlite_connect_kwargs())
    engine = create_engine(fallback_url, **fallback_kwargs)  # type: ignore[misc]
    try:
        if SQLModel is not None:
            SQLModel.metadata.create_all(engine)  # type: ignore[union-attr]
    except Exception:
        logger.debug("failed to auto-create tables for SQLite fallback", exc_info=True)
    return engine



def get_engine():
    _ensure_sqlmodel()
    echo = (os.getenv("DATABASE_ECHO") or "false").strip().lower() == "true"
    pool_size = int(os.getenv("DATABASE_POOL_SIZE") or 5)
    url = _db_url()
    is_sqlite = _is_sqlite(url)
    placeholder = _looks_like_placeholder(url)
    kwargs: Dict[str, Any] = {"echo": echo}
    if is_sqlite:
        kwargs.update(_sqlite_connect_kwargs())
    else:
        kwargs["pool_size"] = pool_size
    if placeholder and not is_sqlite:
        return _sqlite_fallback_engine(echo, url, "placeholder database URL (%s); using SQLite %s")
    try:
        return create_engine(url, **kwargs)  # type: ignore[misc]
    except ModuleNotFoundError as exc:
        message = str(exc).lower()
        if "psycopg2" in message and not is_sqlite:
            return _sqlite_fallback_engine(echo, url, "psycopg2 missing for %s; falling back to SQLite %s")
        raise
    except Exception:
        if placeholder and not is_sqlite:
            return _sqlite_fallback_engine(echo, url, "placeholder database URL (%s); using SQLite %s")
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
