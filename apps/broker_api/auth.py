"""
Authentication helpers for the Venice Broker API.

Provides bearer token extraction, admin authentication, and tenant context resolution.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from fastapi import HTTPException

from libs.telemetry.logger import get_logger

if TYPE_CHECKING:
    from .tenant_store import Tenant, TenantStore

logger = get_logger("broker.auth")

# Load from environment at module level
ADMIN_TOKEN = os.getenv("BROKER_ADMIN_TOKEN")
REQUIRE_ADMIN_ENV = os.getenv("BROKER_REQUIRE_ADMIN_TOKEN")
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_FALSY_VALUES = {"0", "false", "no", "off"}


def _is_truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in _TRUTHY_VALUES


def _is_explicit_false(raw: str | None) -> bool:
    return raw is not None and raw.strip().lower() in _FALSY_VALUES


REQUIRE_ADMIN = _is_truthy(REQUIRE_ADMIN_ENV)
REQUIRE_ADMIN_EXPLICITLY_FALSE = _is_explicit_false(REQUIRE_ADMIN_ENV)

_INITIAL_ADMIN_TOKEN = ADMIN_TOKEN


def _clean_token(raw: str | None) -> str | None:
    """Normalize admin token values by stripping and collapsing blanks."""
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def current_admin_token() -> str | None:
    """Return the active admin token considering runtime overrides.

    Priority order:
    1. A monkeypatched ADMIN_TOKEN value (used heavily in tests).
    2. The BROKER_ADMIN_TOKEN environment variable if present.
    """
    # Detect monkeypatch overrides - tests mutate ADMIN_TOKEN directly.
    if ADMIN_TOKEN != _INITIAL_ADMIN_TOKEN:
        return _clean_token(ADMIN_TOKEN)

    env_token = _clean_token(os.getenv("BROKER_ADMIN_TOKEN"))
    if env_token is not None:
        return env_token
    # Environment cleared and no monkeypatch override; treat as no admin token.
    return None


def _resolve_admin_requirement() -> tuple[bool, bool]:
    """Determine whether admin auth is required and if it was explicitly disabled."""
    env_val = os.getenv("BROKER_REQUIRE_ADMIN_TOKEN")
    if env_val is not None:
        normalized = env_val.strip().lower()
        if normalized in _FALSY_VALUES:
            return (False, True)
        if normalized in _TRUTHY_VALUES:
            return (True, False)
    if REQUIRE_ADMIN_EXPLICITLY_FALSE:
        return (False, True)
    return (REQUIRE_ADMIN, False)


def validate_admin_config() -> None:
    """Validate admin token configuration at startup.

    Raises RuntimeError if REQUIRE_ADMIN is true but ADMIN_TOKEN is not set.
    """
    require_admin, _ = _resolve_admin_requirement()
    token = current_admin_token()
    if require_admin and not token:
        logger.error(
            "security: BROKER_REQUIRE_ADMIN_TOKEN=true; "
            "BROKER_ADMIN_TOKEN is unset; refusing to start"
        )
        message = "BROKER_ADMIN_TOKEN required for startup"
        raise RuntimeError(message)
    if token:
        logger.info(
            "security: admin token configured; admin endpoints require bearer token"
        )
    else:
        logger.warning(
            "security: BROKER_ADMIN_TOKEN not set; admin endpoints allowed for "
            "development only"
        )


def bearer_token(authorization: str | None) -> str | None:
    """Extract bearer token from Authorization header.

    Args:
        authorization: Authorization header value (e.g., "Bearer <token>")

    Returns:
        Token string if valid bearer format, None otherwise
    """
    if not authorization:
        return None
    parts = authorization.split()
    bearer_parts = 2
    if len(parts) == bearer_parts and parts[0].lower() == "bearer":
        return parts[1]
    return None


def require_admin(authorization: str | None) -> None:
    """Verify that the authorization header contains a valid admin token.

    Args:
        authorization: Authorization header value

    Raises:
        HTTPException: 401 if token is invalid or missing when required
    """
    # Read env vars dynamically to support test scenarios set after module load
    _require_admin, explicitly_false = _resolve_admin_requirement()
    admin_token = current_admin_token()

    # If REQUIRE_ADMIN is explicitly False, skip auth check (for dev/testing)
    if explicitly_false and not admin_token:
        return

    # If ADMIN_TOKEN is set, require valid token (unless explicitly disabled above)
    if admin_token:
        token = bearer_token(authorization)
        if token != admin_token:
            raise HTTPException(status_code=401, detail="admin auth required")
    else:
        logger.warning(
            "BROKER_ADMIN_TOKEN not set; allowing admin endpoints for development"
        )


def tenant_by_subkey(store: TenantStore, token: str | None) -> Tenant | None:
    """Find tenant by their subkey token.

    Args:
        store: TenantStore instance
        token: Subkey token to search for

    Returns:
        Tenant if found, None otherwise
    """
    if not token:
        return None
    for t in store.all().values():
        if t.subkey == token:
            return t
    return None


def auth_context(
    store: TenantStore, authorization: str | None
) -> tuple[str, Tenant | None]:
    """Resolve authorization header to context (role, tenant).

    Admin tokens take precedence over tenant subkeys.

    Args:
        store: TenantStore instance
        authorization: Authorization header value

    Returns:
        Tuple of (role, tenant) where role is "admin" or "tenant"

    Raises:
        HTTPException: 401 if authorization fails
    """
    token = bearer_token(authorization)

    # Admin takes precedence
    admin_token = current_admin_token()
    if admin_token and token == admin_token:
        return ("admin", None)

    # Tenant by subkey
    t = tenant_by_subkey(store, token)
    if t:
        return ("tenant", t)

    # No valid auth found
    raise HTTPException(status_code=401, detail="unauthorized")


__all__ = [
    "ADMIN_TOKEN",
    "REQUIRE_ADMIN",
    "auth_context",
    "bearer_token",
    "current_admin_token",
    "require_admin",
    "tenant_by_subkey",
    "validate_admin_config",
]
