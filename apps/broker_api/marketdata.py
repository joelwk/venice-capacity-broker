"""
Market data provider singleton for Venice Broker API.

Provides thread-safe singleton access to MarketDataProvider instance.
"""

from __future__ import annotations

import logging
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from services.marketdata.provider import MarketDataProvider

logger = logging.getLogger("broker.api.marketdata")

_provider_instance: MarketDataProvider | None = None
_provider_factory_id: int | None = None
_provider_lock = Lock()


def get_marketdata_provider(status_code: int = 500) -> MarketDataProvider:
    """
    Get or create singleton MarketDataProvider instance.

    Thread-safe singleton pattern with factory ID tracking to handle
    reloads or module changes.

    Args:
        status_code: HTTP status code to use if provider creation fails

    Returns:
        MarketDataProvider instance

    Raises:
        HTTPException: If provider cannot be created
    """
    global _provider_instance, _provider_factory_id

    try:
        from services.marketdata.provider import MarketDataProvider
    except Exception as import_err:
        logger.exception("market prices provider import failed")
        raise HTTPException(
            status_code=status_code, detail=f"provider unavailable: {import_err}"
        ) from import_err

    factory_id = id(MarketDataProvider)
    instance = _provider_instance

    # Check if we need to create a new instance (first call or factory changed)
    with _provider_lock:
        if instance is None or _provider_factory_id != factory_id:
            try:
                _provider_instance = MarketDataProvider()
                _provider_factory_id = factory_id
                logger.info("market data provider initialized")
            except Exception as create_err:
                logger.exception("market data provider creation failed")
                raise HTTPException(
                    status_code=status_code,
                    detail=f"provider creation failed: {create_err}",
                ) from create_err
        return _provider_instance
