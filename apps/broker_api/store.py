"""
Tenant store initialization for the Venice Broker API.

Provides a factory to build the appropriate TenantStore backend (SQL or JSON).
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from libs.telemetry.logger import get_logger

# Optional metrics and env helpers
try:
    from libs.telemetry.metrics import inc as _metrics_inc  # type: ignore
except Exception:

    def _metrics_inc(name: str, value: int = 1, labels: dict | None = None) -> None:  # type: ignore
        return


try:
    from libs.env import env_flag, is_production, is_test_env  # type: ignore
except Exception:

    def is_production() -> bool:  # type: ignore
        return (os.getenv("APP_ENV") or "").strip().lower() in {"production", "prod"}

    def env_flag(name: str, default: bool = False) -> bool:  # type: ignore
        v = os.getenv(name)
        if v is None:
            return default
        return str(v).strip().lower() in {"1", "true", "yes", "on"}

    def is_test_env() -> bool:  # type: ignore
        return bool(os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules)


if TYPE_CHECKING:
    from .tenant_store import TenantStore

logger = get_logger("broker.store")


def build_store() -> TenantStore:
    """Build TenantStore instance based on BROKER_STORE_BACKEND environment variable.

    Returns:
            TenantStore instance (SQL-backed or JSON file-backed)

    Environment:
            BROKER_STORE_BACKEND: "sql" to use SQL backend, anything else uses JSON (default: "sql")
    """
    from .tenant_store import TenantStore

    # Try to import SQL backend
    try:
        from .tenant_store_sql import SQLTenantStore
    except Exception:
        SQLTenantStore = None  # type: ignore

    backend = (os.getenv("BROKER_STORE_BACKEND") or "sql").strip().lower()
    prod = is_production()

    if backend == "sql" and SQLTenantStore is not None:
        try:
            store = SQLTenantStore()  # type: ignore
            logger.info("broker.store: using SQL backend")
            return store
        except Exception as e:
            if prod:
                logger.critical(
                    "broker.store: SQL backend required in production; init failed: %s",
                    e,
                )
                raise
            logger.warning("broker.store: SQL backend requested but failed: %s", e)
            # fall through to JSON gating below
    elif backend == "sql":
        if prod:
            logger.critical(
                "broker.store: SQL backend required in production; SQLTenantStore unavailable"
            )
            raise RuntimeError("SQL tenant store unavailable in production")
        logger.warning(
            "broker.store: SQL backend requested but SQLTenantStore unavailable; will consider JSON fallback"
        )

    # JSON fallback gating
    if prod:
        logger.critical(
            "broker.store: JSON backend is forbidden in production; set BROKER_STORE_BACKEND=sql and configure Postgres"
        )
        raise RuntimeError(
            "Production requires SQL tenant store; JSON backend forbidden"
        )

    # Non-production: allow JSON only with explicit flag
    if not (env_flag("ALLOW_JSON_FALLBACK", False) or is_test_env()):
        raise RuntimeError("JSON tenant store disabled without ALLOW_JSON_FALLBACK")

    _metrics_inc("fallback_json_store_total", labels={"component": "tenant_store"})
    logger.warning("broker.store: using JSON backend (dev fallback)")
    return TenantStore()


__all__ = ["build_store"]
