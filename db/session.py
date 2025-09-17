from __future__ import annotations

import os
from typing import Iterator

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


def get_engine():
    _ensure_sqlmodel()
    echo = (os.getenv("DATABASE_ECHO") or "false").strip().lower() == "true"
    pool_size = int(os.getenv("DATABASE_POOL_SIZE") or 5)
    return create_engine(_db_url(), echo=echo, pool_size=pool_size)  # type: ignore[misc]


def create_db_and_tables() -> None:
    _ensure_sqlmodel()
    engine = get_engine()
    SQLModel.metadata.create_all(engine)  # type: ignore[union-attr]


def get_session() -> Iterator["Session"]:
    _ensure_sqlmodel()
    engine = get_engine()
    with Session(engine) as session:  # type: ignore[call-arg]
        yield session
