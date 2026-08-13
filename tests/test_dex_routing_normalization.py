"""Tests for provider-specific route normalization."""

from __future__ import annotations

import pytest

from libs.dex.routes import make_route
from libs.dex.routing import (
    ensure_canonical_diem_path,
    normalize_route_for_aerodrome,
    normalize_route_for_v2,
    normalize_route_for_v3,
)


def test_normalize_route_for_v2_strips_fees():
    """V2 routes must not have fee tiers."""
    # Create route with fees (V3-style)
    route_with_fees = make_route(["0xtoken1", "0xtoken2", "0xtoken3"], fees=[3000, 500])

    # Normalize for V2
    v2_route = normalize_route_for_v2(route_with_fees)

    # All fees should be None
    assert all(hop.fee is None for hop in v2_route.hops)
    assert len(v2_route.hops) == 2
    assert v2_route.tokens == route_with_fees.tokens


def test_normalize_route_for_v2_preserves_no_fees():
    """V2 normalization of already-V2 route should be idempotent."""
    route_no_fees = make_route(["0xtoken1", "0xtoken2"])
    v2_route = normalize_route_for_v2(route_no_fees)

    assert all(hop.fee is None for hop in v2_route.hops)
    assert v2_route.tokens == route_no_fees.tokens


def test_normalize_route_for_v3_ensures_fees():
    """V3 routes must have fee tiers."""
    route_no_fees = make_route(["0xtoken1", "0xtoken2"])

    # Normalize with default fee
    v3_route = normalize_route_for_v3(route_no_fees, default_fee=3000)

    # All hops should have fees
    assert all(hop.fee is not None for hop in v3_route.hops)
    assert all(hop.fee == 3000 for hop in v3_route.hops)


def test_normalize_route_for_v3_preserves_existing_fees():
    """V3 normalization should preserve existing fees."""
    route_with_fees = make_route(["0xtoken1", "0xtoken2", "0xtoken3"], fees=[3000, 500])

    v3_route = normalize_route_for_v3(route_with_fees, default_fee=10000)

    # Should preserve original fees
    assert v3_route.hops[0].fee == 3000
    assert v3_route.hops[1].fee == 500


def test_normalize_route_for_v3_requires_default_if_missing():
    """V3 normalization should raise if fees missing and no default."""
    route_no_fees = make_route(["0xtoken1", "0xtoken2"])

    with pytest.raises(ValueError, match="fee tier"):
        normalize_route_for_v3(route_no_fees, default_fee=None)


def test_normalize_route_for_aerodrome_strips_fees():
    """Aerodrome routes should not have fee tiers."""
    route_with_fees = make_route(["0xtoken1", "0xtoken2"], fees=[3000])

    aerodrome_route = normalize_route_for_aerodrome(route_with_fees)

    assert all(hop.fee is None for hop in aerodrome_route.hops)


def test_ensure_canonical_diem_path():
    """Canonical DIEM path should be DIEM -> WETH -> USDC."""
    import os

    # Mock addresses
    diem = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    weth = "0x4200000000000000000000000000000000000006"
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    # Set env vars temporarily
    old_diem = os.environ.get("DIEM_TOKEN_ADDRESS")
    old_weth = os.environ.get("WETH_ADDRESS")
    old_usdc = os.environ.get("QUOTE_TOKEN_ADDRESS")

    try:
        os.environ["DIEM_TOKEN_ADDRESS"] = diem
        os.environ["WETH_ADDRESS"] = weth
        os.environ["QUOTE_TOKEN_ADDRESS"] = usdc

        # Test canonical path
        canonical = make_route([diem, weth, usdc])
        result = ensure_canonical_diem_path(canonical)
        assert result.tokens == [diem.lower(), weth.lower(), usdc.lower()]

        # Test non-canonical path gets normalized
        non_canonical = make_route([diem, usdc])
        result2 = ensure_canonical_diem_path(non_canonical)
        assert len(result2.tokens) == 3
        assert result2.tokens[0].lower() == diem.lower()
        assert result2.tokens[1].lower() == weth.lower()
        assert result2.tokens[2].lower() == usdc.lower()
    finally:
        if old_diem:
            os.environ["DIEM_TOKEN_ADDRESS"] = old_diem
        elif "DIEM_TOKEN_ADDRESS" in os.environ:
            del os.environ["DIEM_TOKEN_ADDRESS"]
        if old_weth:
            os.environ["WETH_ADDRESS"] = old_weth
        elif "WETH_ADDRESS" in os.environ:
            del os.environ["WETH_ADDRESS"]
        if old_usdc:
            os.environ["QUOTE_TOKEN_ADDRESS"] = old_usdc
        elif "QUOTE_TOKEN_ADDRESS" in os.environ:
            del os.environ["QUOTE_TOKEN_ADDRESS"]
