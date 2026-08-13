"""RPC URL validation and startup guards for configuration unity.

This module provides utilities to validate RPC endpoint configuration
and fail fast when free/public endpoints are detected in production.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Known free/public RPC endpoints that should not be used in production
PUBLIC_RPC_HOSTS = {
    "base.drpc.org",
    "mainnet.base.org",
    "base-rpc.publicnode.com",
    "base.publicnode.com",
    "base.blockpi.network",
    "base.llamarpc.com",
    "base.meowrpc.com",
    "1rpc.io",
    "base-mainnet.public.blastapi.io",
    "gateway.tenderly.co",
    "base.diamondswap.org",
    "base.merkle.io",
}

# Paid RPC providers (Alchemy demo endpoint excluded)
PAID_RPC_INDICATORS = {
    "alchemy.com",
    "infura.io",
    "quicknode.com",
    "ankr.com",
}


def _is_paid_rpc(url: str) -> bool:
    """Check if URL is a paid RPC endpoint."""
    url_lower = url.lower()
    # Exclude Alchemy demo endpoint
    if "alchemy.com" in url_lower and "/v2/demo" in url_lower:
        return False
    return any(indicator in url_lower for indicator in PAID_RPC_INDICATORS)


def _is_public_rpc(url: str) -> bool:
    """Check if URL is a known public/free RPC endpoint."""
    url_lower = url.lower()
    return any(host in url_lower for host in PUBLIC_RPC_HOSTS)


def get_rpc_urls_from_env() -> list[str]:
    """Extract all RPC URLs from environment variables.

    Returns:
        List of RPC URLs in priority order (URLS first, then single URL).
    """
    urls: list[str] = []

    # Check plural forms first (higher priority)
    for key in ("RPC_URLS", "BASE_RPC_URLS"):
        val = os.getenv(key)
        if val:
            # Split comma or space-separated URLs
            for candidate in val.replace(",", " ").split():
                cleaned = candidate.strip()
                if cleaned:
                    urls.append(cleaned)

    # Check singular forms (lower priority)
    for key in ("RPC_URL", "BASE_RPC_URL"):
        val = os.getenv(key)
        if val:
            val = val.strip()
            if val:
                urls.append(val)

    return urls


def validate_rpc_configuration(
    fail_on_public: bool = True,
    require_paid: bool = False,
    allow_dry_run: bool = True,
) -> dict[str, Any]:
    """Validate RPC configuration and return diagnostic information.

    Args:
        fail_on_public: If True, raise ValueError when public RPCs are detected.
        require_paid: If True, require at least one paid RPC endpoint.
        allow_dry_run: If True, allow public RPCs when in dry-run mode.

    Returns:
        Dictionary with validation results:
        - urls: List of configured RPC URLs
        - has_paid: Whether any paid RPC is configured
        - has_public: Whether any public RPC is configured
        - primary_url: First URL in the list
        - is_valid: Whether configuration passes validation

    Raises:
        ValueError: If validation fails and fail_on_public is True.
    """
    urls = get_rpc_urls_from_env()

    if not urls:
        error_msg = (
            "No RPC URLs configured. Set BASE_RPC_URL or BASE_RPC_URLS "
            "(or RPC_URL/RPC_URLS) in environment variables."
        )
        if fail_on_public:
            raise ValueError(error_msg)
        return {
            "urls": [],
            "has_paid": False,
            "has_public": False,
            "primary_url": None,
            "is_valid": False,
            "error": error_msg,
        }

    has_paid = any(_is_paid_rpc(url) for url in urls)
    has_public = any(_is_public_rpc(url) for url in urls)
    primary_url = urls[0] if urls else None

    # Check if we're in dry-run mode
    is_dry_run = os.getenv("DRY_RUN", "").lower() in (
        "1",
        "true",
        "yes",
        "on",
    ) or os.getenv("ENABLE_LIVE", "").lower() not in ("1", "true", "yes", "on")

    # Validation logic
    is_valid = True
    error_msg = None

    if require_paid and not has_paid:
        is_valid = False
        error_msg = (
            "Production requires a paid RPC endpoint (Alchemy, Infura, QuickNode, etc.). "
            f"Configured URLs: {urls[:3]}"
        )

    if fail_on_public and has_public:
        if allow_dry_run and is_dry_run:
            logger.warning(
                "Public RPC endpoints detected in dry-run mode: %s. "
                "This is allowed for testing but should not be used in production.",
                [url for url in urls if _is_public_rpc(url)][:3],
            )
        else:
            is_valid = False
            error_msg = (
                f"Public RPC endpoint detected: {primary_url}. "
                "Production requires a paid RPC endpoint. "
                "Set BASE_RPC_URLS=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY "
                "in docker/.env.local or environment variables."
            )

    if error_msg and fail_on_public:
        raise ValueError(error_msg)

    return {
        "urls": urls,
        "has_paid": has_paid,
        "has_public": has_public,
        "primary_url": primary_url,
        "is_valid": is_valid,
        "error": error_msg,
    }


def log_rpc_configuration() -> None:
    """Log RPC configuration for observability.

    This should be called at startup to help diagnose configuration issues.
    """
    try:
        validation = validate_rpc_configuration(
            fail_on_public=False, require_paid=False, allow_dry_run=True
        )

        urls = validation["urls"]
        primary_url = validation["primary_url"]

        if not urls:
            logger.warning("No RPC URLs configured in environment")
            return

        # Mask API keys in logs
        def mask_url(url: str) -> str:
            if not url:
                return url
            # Mask Alchemy keys
            if "/v2/" in url:
                parts = url.split("/v2/")
                if len(parts) > 1:
                    key = parts[1].split("/")[0]
                    if len(key) > 12:
                        masked_key = key[:8] + "..." + key[-4:]
                        return f"{parts[0]}/v2/{masked_key}"
            # Mask other common patterns
            if "?" in url:
                base, query = url.split("?", 1)
                if "api_key" in query.lower() or "apikey" in query.lower():
                    return f"{base}?***"
            return url

        logger.info(
            "RPC configuration: primary=%s, total=%d, has_paid=%s, has_public=%s",
            mask_url(primary_url) if primary_url else "None",
            len(urls),
            validation["has_paid"],
            validation["has_public"],
        )

        if validation["has_public"] and not validation["has_paid"]:
            logger.warning(
                "Using public RPC endpoints without paid fallback. "
                "This may cause rate limiting and failures. "
                "Configure BASE_RPC_URLS with a paid provider (Alchemy, Infura, etc.)."
            )

        # Log all URLs in debug mode
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "RPC URLs (priority order): %s", [mask_url(url) for url in urls[:5]]
            )

    except Exception as exc:
        logger.warning("Failed to validate RPC configuration: %s", exc, exc_info=True)
