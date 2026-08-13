from __future__ import annotations

import logging
import os
import re
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text
from sqlmodel import SQLModel

import db.models  # noqa: F401

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config
logger = logging.getLogger("alembic.schema")
_SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_url() -> str:
    return os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL") or ""


def get_schema() -> str | None:
    raw = (
        os.getenv("DATABASE_SCHEMA")
        or os.getenv("SQL_DATABASE_SCHEMA")
        or os.getenv("SQL_SCHEMA")
        or os.getenv("SCHEMA")
    )
    if raw is None:
        return None
    schema = raw.strip()
    if not schema:
        return None
    if not _SCHEMA_NAME_RE.match(schema):
        logger.warning("Ignoring invalid DATABASE_SCHEMA '%s'", schema)
        return None
    return schema


target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = get_url()
    schema = get_schema()
    if url:
        config.set_main_option("sqlalchemy.url", url)

    configure_kwargs = dict(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=schema,
        include_schemas=bool(schema),
    )
    if schema:
        configure_kwargs["schema_translate_map"] = {None: schema}
    context.configure(**configure_kwargs)

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    cfg = config.get_section(config.config_ini_section)
    url = get_url()
    schema = get_schema()
    if url:
        cfg["sqlalchemy.url"] = url

    connectable = engine_from_config(
        cfg,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        # Step 1: Create schema if needed (separate transaction)
        if schema:
            trans = None
            try:
                trans = connection.begin()
                connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
                trans.commit()
                logger.info("Schema '%s' ensured", schema)
            except Exception as e:
                logger.warning("Failed to create schema '%s': %s", schema, e)
                if trans is not None:
                    trans.rollback()

        # Step 2: Run migrations (separate transaction)
        if schema:
            # Set search_path for this connection
            trans_sp = connection.begin()
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            trans_sp.commit()

        configure_kwargs = dict(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=schema,
            include_schemas=bool(schema),
        )
        if schema:
            configure_kwargs["schema_translate_map"] = {None: schema}
        context.configure(**configure_kwargs)

        # Run migrations with explicit transaction handling
        trans_migration = connection.begin()
        try:
            if schema:
                connection.execute(text(f'SET search_path TO "{schema}", public'))
            context.run_migrations()
            trans_migration.commit()
            logger.info("Migrations committed successfully")
        except Exception as e:
            logger.error("Migration failed: %s", e, exc_info=True)
            trans_migration.rollback()
            raise


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
