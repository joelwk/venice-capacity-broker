"""
Tenant store initialization for the Venice Broker API.

Provides a factory to build the appropriate TenantStore backend (SQL or JSON).
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING

from libs.telemetry.logger import get_logger

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
    
    if backend == "sql" and SQLTenantStore is not None:
        try:
            store = SQLTenantStore()  # type: ignore
            logger.info("broker.store: using SQL backend")
            return store
        except Exception as e:
            logger.warning(
                "broker.store: SQL backend requested but failed: %s; falling back to JSON", e
            )
            return TenantStore()
    else:
        if backend == "sql":
            logger.warning("broker.store: SQL backend requested but SQLTenantStore unavailable; using JSON")
        return TenantStore()


__all__ = ["build_store"]

