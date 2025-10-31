"""
Pricing service builder for Venice Broker API.

Simple wrapper to create PricingService instances for quotes and settlement.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from services.pricing.service import PricingService

logger = logging.getLogger("broker.api.services.pricing")


def build_pricing_service() -> PricingService:
    """
    Build PricingService instance for quotes/clearing/settlement.
    
    Returns:
        PricingService instance
        
    Raises:
        ImportError: If PricingService cannot be imported
    """
    try:
        from services.pricing.service import PricingService
        return PricingService()
    except Exception as e:
        logger.error("Failed to build pricing service: %s", e)
        raise

