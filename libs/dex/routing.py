"""Provider-specific route normalization for DEX providers.

This module ensures routes are properly formatted for each DEX provider:
- Uniswap V2: routes must NOT have fee tiers
- Uniswap V3: routes MUST have fee tiers
- Aerodrome: routes should NOT have fee tiers (uses stable/volatile flag instead)
"""

from __future__ import annotations

from libs.dex.routes import RouteHop, RoutePlan, _normalize_address, make_route


def normalize_route_for_v2(route: RoutePlan) -> RoutePlan:
    """Normalize a route for Uniswap V2 by removing fee tiers.

    Args:
        route: RoutePlan that may contain fee tiers

    Returns:
        RoutePlan with all fee tiers removed (set to None)

    Raises:
        ValueError: If route cannot be normalized (e.g., invalid structure)
    """
    if not route.hops:
        raise ValueError("route must contain at least one hop")

    # Strip fees for V2 and normalize addresses
    v2_hops = [
        RouteHop(
            token_in=_normalize_address(hop.token_in),
            token_out=_normalize_address(hop.token_out),
            fee=None,  # V2 doesn't use fee tiers
        )
        for hop in route.hops
    ]

    return RoutePlan(tuple(v2_hops))


def normalize_route_for_v3(
    route: RoutePlan, default_fee: int | None = None
) -> RoutePlan:
    """Normalize a route for Uniswap V3 by ensuring fee tiers are present.

    Args:
        route: RoutePlan that may be missing fee tiers
        default_fee: Default fee tier to use if a hop is missing fees (e.g., 3000 for 0.3%)

    Returns:
        RoutePlan with all hops having fee tiers

    Raises:
        ValueError: If route cannot be normalized (e.g., missing fees and no default)
    """
    if not route.hops:
        raise ValueError("route must contain at least one hop")

    v3_hops = []
    for hop in route.hops:
        fee = hop.fee
        if fee is None:
            if default_fee is None:
                raise ValueError(
                    f"Uniswap V3 route requires fee tiers. "
                    f"Hop {hop.token_in} -> {hop.token_out} is missing fee."
                )
            fee = default_fee

        # Normalize addresses to strip any @fee annotations before creating RouteHop
        # This ensures addresses are clean hex strings when passed to Web3
        token_in_normalized = _normalize_address(hop.token_in)
        token_out_normalized = _normalize_address(hop.token_out)

        v3_hops.append(
            RouteHop(
                token_in=token_in_normalized,
                token_out=token_out_normalized,
                fee=fee,
            )
        )

    return RoutePlan(tuple(v3_hops))


def normalize_route_for_aerodrome(route: RoutePlan) -> RoutePlan:
    """Normalize a route for Aerodrome by removing fee tiers.

    Aerodrome uses stable/volatile flags instead of fee tiers.

    Args:
        route: RoutePlan that may contain fee tiers

    Returns:
        RoutePlan with all fee tiers removed
    """
    if not route.hops:
        raise ValueError("route must contain at least one hop")

    # Strip fees for Aerodrome (uses stable flag instead) and normalize addresses
    aerodrome_hops = [
        RouteHop(
            token_in=_normalize_address(hop.token_in),
            token_out=_normalize_address(hop.token_out),
            fee=None,  # Aerodrome doesn't use fee tiers
        )
        for hop in route.hops
    ]

    return RoutePlan(tuple(aerodrome_hops))


def ensure_canonical_diem_path(route: RoutePlan) -> RoutePlan:
    """Ensure route follows canonical DIEM -> WETH -> USDC path on Base.

    This is the default path for DIEM trades as specified in the plan.
    If the route doesn't match, returns a normalized canonical version.

    Args:
        route: RoutePlan to check/normalize

    Returns:
        RoutePlan matching canonical path (DIEM -> WETH -> USDC)
    """
    import os

    diem_addr = (
        (
            os.getenv("DIEM_TOKEN_ADDRESS")
            or "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
        )
        .strip()
        .lower()
    )
    weth_addr = (
        (
            os.getenv("WETH_ADDRESS")
            or os.getenv("BASE_WETH_ADDRESS")
            or "0x4200000000000000000000000000000000000006"
        )
        .strip()
        .lower()
    )
    usdc_addr = (
        (
            os.getenv("QUOTE_TOKEN_ADDRESS")
            or os.getenv("USDC_TOKEN_ADDRESS")
            or "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        )
        .strip()
        .lower()
    )

    route_tokens = [t.lower() for t in route.tokens]

    # Check if route matches canonical path
    if (
        len(route_tokens) == 3
        and route_tokens[0] == diem_addr
        and route_tokens[1] == weth_addr
        and route_tokens[2] == usdc_addr
    ):
        return route

    # Return canonical path (without fees for V2 compatibility)
    return make_route([diem_addr, weth_addr, usdc_addr])


__all__ = [
    "ensure_canonical_diem_path",
    "normalize_route_for_aerodrome",
    "normalize_route_for_v2",
    "normalize_route_for_v3",
]
