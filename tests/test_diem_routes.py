"""
Unit and integration tests for DIEM routing.

Tests DIEM-aware routing, canonical paths, and fallback mechanisms.
"""

import os

import pytest

from libs.dex.diem_fallbacks import build_two_stage_diem_route
from libs.dex.diem_routing import (
    build_diem_canonical_route,
    get_diem_canonical_routes,
    should_use_diem_canonical_route,
)
from libs.dex.routes import RoutePlan
from services.marketdata.pathing.env import EnvConfig


@pytest.fixture
def mock_diem_config():
    """Create a mock DIEM configuration."""
    return EnvConfig(
        diem_token="0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        vvv_token="0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        quote_token="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        diem_vvv_pair="0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d",
        diem_usdc_pool="0xbc3231036ee1eca03e5f67fecedc640d21610823",
        vvv_usdc_pool="0x67a11022b7b6ed66f81233f6c8ed6e48f7826530",
        vvv_weth_pool=None,
        trade_paths=[],
        bridge_token=None,
        progressive_live=False,
        progressive_min_cycles=None,
    )


def test_build_diem_to_usdc_route(mock_diem_config):
    """Test building DIEM -> USDC canonical route."""
    diem = mock_diem_config.diem_token
    usdc = mock_diem_config.quote_token

    route = build_diem_canonical_route(diem, usdc, mock_diem_config)

    assert route is not None
    assert len(route.tokens) == 3
    assert route.tokens[0].lower() == diem.lower()
    assert route.tokens[-1].lower() == usdc.lower()
    # Should have VVV as intermediate token
    assert mock_diem_config.vvv_token.lower() in [t.lower() for t in route.tokens]


def test_build_usdc_to_diem_route(mock_diem_config):
    """Test building USDC -> DIEM canonical route."""
    diem = mock_diem_config.diem_token
    usdc = mock_diem_config.quote_token

    route = build_diem_canonical_route(usdc, diem, mock_diem_config)

    assert route is not None
    assert len(route.tokens) == 3
    assert route.tokens[0].lower() == usdc.lower()
    assert route.tokens[-1].lower() == diem.lower()


def test_build_two_stage_routes_forward(mock_diem_config):
    """Two-stage routing should yield DIEM->VVV and VVV->USDC legs."""
    diem = mock_diem_config.diem_token
    usdc = mock_diem_config.quote_token

    stage_routes = build_two_stage_diem_route(diem, usdc, config=mock_diem_config)
    assert stage_routes is not None

    stage1, stage2 = stage_routes
    assert tuple(stage1.tokens) == (diem, mock_diem_config.vvv_token)
    assert tuple(stage2.tokens) == (mock_diem_config.vvv_token, usdc)


def test_build_two_stage_routes_reverse(mock_diem_config):
    """Two-stage routing should also work for USDC->DIEM direction."""
    diem = mock_diem_config.diem_token
    usdc = mock_diem_config.quote_token

    stage_routes = build_two_stage_diem_route(usdc, diem, config=mock_diem_config)
    assert stage_routes is not None

    stage1, stage2 = stage_routes
    assert tuple(stage1.tokens) == (usdc, mock_diem_config.vvv_token)
    assert tuple(stage2.tokens) == (mock_diem_config.vvv_token, diem)


def test_should_use_diem_canonical_route(mock_diem_config):
    """Test detection of DIEM trades."""
    diem = mock_diem_config.diem_token
    usdc = mock_diem_config.quote_token

    assert should_use_diem_canonical_route(diem, usdc, mock_diem_config) is True
    assert should_use_diem_canonical_route(usdc, diem, mock_diem_config) is True


def test_get_diem_canonical_routes(mock_diem_config):
    """Test getting canonical routes for DIEM trades."""
    diem = mock_diem_config.diem_token
    usdc = mock_diem_config.quote_token

    routes = get_diem_canonical_routes(diem, usdc, mock_diem_config)

    assert len(routes) > 0
    assert isinstance(routes[0], RoutePlan)


def test_diem_route_without_pools():
    """Test that routes return None when pools are not configured."""
    config = EnvConfig(
        diem_token="0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        vvv_token="0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        quote_token="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        diem_vvv_pair=None,  # Missing pool
        diem_usdc_pool=None,
        vvv_usdc_pool=None,  # Missing pool
        vvv_weth_pool=None,
        trade_paths=[],
        bridge_token=None,
        progressive_live=False,
        progressive_min_cycles=None,
    )

    diem = config.diem_token
    usdc = config.quote_token

    route = build_diem_canonical_route(diem, usdc, config)
    assert route is None


@pytest.mark.skipif(
    not os.getenv("DIEM_TOKEN_ADDRESS") or not os.getenv("VVV_TOKEN_ADDRESS"),
    reason="DIEM/VVV addresses not configured",
)
def test_diem_routes_integration():
    """Integration test with real addresses from environment."""
    from services.marketdata.pathing.env import load_env_config

    config = load_env_config()
    if not config.diem_vvv_pair or not config.vvv_usdc_pool:
        pytest.skip("DIEM pools not configured")

    diem = config.diem_token
    usdc = config.quote_token

    if not diem or not usdc:
        pytest.skip("DIEM or USDC addresses not configured")

    routes = get_diem_canonical_routes(diem, usdc, config)
    assert len(routes) > 0
