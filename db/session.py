from __future__ import annotations

import os
from typing import Iterator

try:
    from sqlmodel import SQLModel, Session, create_engine
except Exception:  # noqa: BLE001
    SQLModel = None  # type: ignore
    Session = None  # type: ignore
    create_engine = None  # type: ignore


def _db_url() -> str:
    url = os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("POSTGRES_HOST")
    if not host:
        raise RuntimeError("SQL_DATABASE_URL/DATABASE_URL or POSTGRES_* envs must be set")
    user = os.getenv("POSTGRES_USER", "postgres")
    pwd = os.getenv("POSTGRES_PASSWORD", "")
    db = os.getenv("POSTGRES_DB", "postgres")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    return f"postgresql+psycopg2://{user}:{pwd}@{host}:{port}/{db}"


def get_engine():
    if create_engine is None:
        raise RuntimeError("sqlmodel/sqlalchemy not installed")
    echo = (os.getenv("DATABASE_ECHO") or "false").strip().lower() == "true"
    pool_size = int(os.getenv("DATABASE_POOL_SIZE") or 5)
    return create_engine(_db_url(), echo=echo, pool_size=pool_size)


def create_db_and_tables() -> None:
    if SQLModel is None:
        raise RuntimeError("sqlmodel not installed")
    engine = get_engine()
    SQLModel.metadata.create_all(engine)


def get_session() -> Iterator[Session]:  # type: ignore[type-arg]
    if Session is None:
        raise RuntimeError("sqlmodel not installed")
    engine = get_engine()
    with Session(engine) as session:  # type: ignore[call-arg]
        yield session

