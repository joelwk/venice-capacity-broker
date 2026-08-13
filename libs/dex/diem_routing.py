"""
DIEM-aware routing module.

This module provides DIEM-specific routing logic that uses the canonical
DIEM/VVV and VVV/USDC pools as sources of truth for DIEM trades.
"""

import os
from typing import TYPE_CHECKING, Optional

from libs.dex.routes import RouteHop, RoutePlan

if TYPE_CHECKING:  # pragma: no cover
    from services.marketdata.pathing.env import EnvConfig
else:  # Fallback type when importing would create circular dependency
    EnvConfig = object  # type: ignore[misc,assignment]


def _ensure_config(config: Optional["EnvConfig"]) -> "EnvConfig":
    """Lazily load EnvConfig to avoid circular imports at module import time."""
    if config is not None:
        return config
    from services.marketdata.pathing.env import load_env_config  # Local import

    return load_env_config()


def _normalize_address(addr: str) -> str:
    """Normalize address to lowercase."""
    if not addr:
        return ""
    return addr.strip().lower()


def is_diem_token(token: str, config: Optional["EnvConfig"] = None) -> bool:
    """Check if token is DIEM."""
    config = _ensure_config(config)
    diem_addr = _normalize_address(
        config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or ""
    )
    return _normalize_address(token) == diem_addr


def is_vvv_token(token: str, config: Optional["EnvConfig"] = None) -> bool:
    """Check if token is VVV."""
    config = _ensure_config(config)
    vvv_addr = _normalize_address(
        config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or ""
    )
    return _normalize_address(token) == vvv_addr


def is_usdc_token(token: str, config: Optional["EnvConfig"] = None) -> bool:
    """Check if token is USDC (quote token)."""
    config = _ensure_config(config)
    quote_addr = _normalize_address(
        config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or ""
    )
    return _normalize_address(token) == quote_addr


def build_diem_canonical_route(
    token_in: str,
    token_out: str,
    config: Optional["EnvConfig"] = None,
) -> RoutePlan | None:
    """
    Build canonical DIEM route: DIEM -> VVV -> USDC or reverse.

    This uses the configured DIEM/VVV pair and VVV/USDC pool as sources of truth.

    Args:
        token_in: Input token address
        token_out: Output token address
        config: Optional EnvConfig (will load if not provided)

    Returns:
        RoutePlan if this is a DIEM trade and canonical route can be built, None otherwise
    """
    config = _ensure_config(config)

    # Lazy import to avoid circular dependency
    from libs.dex.composite import attach_composite_metadata

    diem_addr = _normalize_address(
        config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or ""
    )
    vvv_addr = _normalize_address(
        config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or ""
    )
    quote_addr = _normalize_address(
        config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or ""
    )

    if not diem_addr or not vvv_addr or not quote_addr:
        return None

    token_in_norm = _normalize_address(token_in)
    token_out_norm = _normalize_address(token_out)

    # Check if this is a DIEM trade
    is_diem_in = token_in_norm == diem_addr
    is_diem_out = token_out_norm == diem_addr
    is_usdc_in = token_in_norm == quote_addr
    is_usdc_out = token_out_norm == quote_addr
    is_vvv_in = token_in_norm == vvv_addr
    is_vvv_out = token_out_norm == vvv_addr

    # DIEM -> USDC: DIEM -> VVV -> USDC
    if is_diem_in and is_usdc_out:
        # Check that canonical pools are configured
        if not config.diem_vvv_pair or not config.vvv_usdc_pool:
            return None

        # Build route: DIEM -> VVV -> USDC
        # VVV/USDC is V3, so we need fee tier
        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
            vvv_usdc_fee = int(fee_str)
        except Exception:
            vvv_usdc_fee = 3000  # Default V3 fee

        hops = [
            RouteHop(token_in, vvv_addr, fee=None),  # DIEM/VVV is V2/Aerodrome
            RouteHop(vvv_addr, token_out, fee=vvv_usdc_fee),  # VVV/USDC is V3
        ]
        plan = RoutePlan(tuple(hops))

        # Attach composite metadata with provider + stable hint
        diem_leg_provider = (
            os.getenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome").strip().lower()
            or "aerodrome"
        )
        stable_env = (
            os.getenv("DIEM_VVV_STABLE") or os.getenv("AERODROME_STABLE") or "true"
        )
        try:
            diem_vvv_stable = str(stable_env).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        except Exception:
            diem_vvv_stable = True

        legs = [
            {
                "token_in": token_in,
                "token_out": vvv_addr,
                "provider": diem_leg_provider,
                "pool_address": config.diem_vvv_pair,
                "fee": None,
                "stable": diem_vvv_stable,
            },
            {
                "token_in": vvv_addr,
                "token_out": token_out,
                "provider": "uniswap_v3",
                "pool_address": config.vvv_usdc_pool,
                "fee": vvv_usdc_fee,
            },
        ]
        return attach_composite_metadata(plan, bridge_legs=legs, is_composite=True)

    # USDC -> DIEM: USDC -> VVV -> DIEM
    if is_usdc_in and is_diem_out:
        # Check that canonical pools are configured
        if not config.diem_vvv_pair or not config.vvv_usdc_pool:
            return None

        # Build route: USDC -> VVV -> DIEM
        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
            vvv_usdc_fee = int(fee_str)
        except Exception:
            vvv_usdc_fee = 3000

        hops = [
            RouteHop(token_in, vvv_addr, fee=vvv_usdc_fee),  # USDC/VVV is V3
            RouteHop(vvv_addr, token_out, fee=None),  # VVV/DIEM is V2/Aerodrome
        ]
        plan = RoutePlan(tuple(hops))

        diem_leg_provider = (
            os.getenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome").strip().lower()
            or "aerodrome"
        )
        stable_env = (
            os.getenv("DIEM_VVV_STABLE") or os.getenv("AERODROME_STABLE") or "true"
        )
        try:
            diem_vvv_stable = str(stable_env).strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        except Exception:
            diem_vvv_stable = True
        legs = [
            {
                "token_in": token_in,
                "token_out": vvv_addr,
                "provider": "uniswap_v3",
                "pool_address": config.vvv_usdc_pool,
                "fee": vvv_usdc_fee,
            },
            {
                "token_in": vvv_addr,
                "token_out": token_out,
                "provider": diem_leg_provider,
                "pool_address": config.diem_vvv_pair,
                "fee": None,
                "stable": diem_vvv_stable,
            },
        ]
        return attach_composite_metadata(plan, bridge_legs=legs, is_composite=True)

    # DIEM -> VVV: direct hop
    if is_diem_in and is_vvv_out:
        if not config.diem_vvv_pair:
            return None
        hops = [RouteHop(token_in, token_out, fee=None)]
        return RoutePlan(tuple(hops))

    # VVV -> DIEM: direct hop
    if is_vvv_in and is_diem_out:
        if not config.diem_vvv_pair:
            return None
        hops = [RouteHop(token_in, token_out, fee=None)]
        return RoutePlan(tuple(hops))

    # VVV -> USDC: direct hop (V3)
    if is_vvv_in and is_usdc_out:
        if not config.vvv_usdc_pool:
            return None
        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
            vvv_usdc_fee = int(fee_str)
        except Exception:
            vvv_usdc_fee = 3000
        hops = [RouteHop(token_in, token_out, fee=vvv_usdc_fee)]
        return RoutePlan(tuple(hops))

    # USDC -> VVV: direct hop (V3)
    if is_usdc_in and is_vvv_out:
        if not config.vvv_usdc_pool:
            return None
        vvv_usdc_fee = None
        try:
            fee_str = os.getenv("VVV_USDC_POOL_FEE") or "3000"
            vvv_usdc_fee = int(fee_str)
        except Exception:
            vvv_usdc_fee = 3000
        hops = [RouteHop(token_in, token_out, fee=vvv_usdc_fee)]
        return RoutePlan(tuple(hops))

    return None


def get_diem_canonical_routes(
    token_in: str,
    token_out: str,
    config: Optional["EnvConfig"] = None,
) -> list[RoutePlan]:
    """
    Get canonical DIEM routes for a trade.

    Returns a list of RoutePlan objects using the canonical DIEM/VVV and VVV/USDC pools.
    Returns empty list if this is not a DIEM trade or canonical pools are not configured.
    """
    route = build_diem_canonical_route(token_in, token_out, config)
    if route:
        return [route]
    return []


def should_use_diem_canonical_route(
    token_in: str,
    token_out: str,
    config: Optional["EnvConfig"] = None,
) -> bool:
    """
    Determine if we should use canonical DIEM routing for this trade.

    Returns True if:
    - This is a DIEM trade (involves DIEM, VVV, or USDC)
    - Canonical pools are configured
    """
    config = _ensure_config(config)

    if not config.diem_vvv_pair or not config.vvv_usdc_pool:
        return False

    diem_addr = _normalize_address(
        config.diem_token or os.getenv("DIEM_TOKEN_ADDRESS") or ""
    )
    vvv_addr = _normalize_address(
        config.vvv_token or os.getenv("VVV_TOKEN_ADDRESS") or ""
    )
    quote_addr = _normalize_address(
        config.quote_token or os.getenv("QUOTE_TOKEN_ADDRESS") or ""
    )

    if not diem_addr or not vvv_addr or not quote_addr:
        return False

    token_in_norm = _normalize_address(token_in)
    token_out_norm = _normalize_address(token_out)

    canonical_tokens = {diem_addr, vvv_addr, quote_addr}
    return token_in_norm in canonical_tokens and token_out_norm in canonical_tokens
