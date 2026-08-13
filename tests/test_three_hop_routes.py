"""
Test three-hop routing via WETH for DIEM trades.

This test validates that the route preferences include:
1. Direct VVV route: DIEM -> VVV -> USDC (2-hop)
2. High-liquidity WETH route: DIEM -> VVV -> WETH -> USDC (3-hop)
3. Fallback WETH route: DIEM -> WETH -> USDC (2-hop)
"""

import os


def test_three_hop_route_preferences_buy():
    """Test that USDC -> DIEM includes 3-hop route via WETH."""
    # Set all required environment variables before importing
    os.environ["VVV_WETH_POOL_ADDRESS"] = "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce"
    os.environ["VVV_WETH_POOL_FEE"] = "500"
    os.environ["DIEM_ENABLE_THREE_HOP_WETH"] = "1"
    os.environ["DIEM_MAX_ROUTE_HOPS"] = "3"
    os.environ["DIEM_VVV_PAIR_ADDRESS"] = "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    os.environ["VVV_USDC_POOL_ADDRESS"] = "0x67a11022b7b6ed66f81233f6c8ed6e48f7826530"
    os.environ["DIEM_TOKEN_ADDRESS"] = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    os.environ["VVV_TOKEN_ADDRESS"] = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    os.environ["QUOTE_TOKEN_ADDRESS"] = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    from libs.dex.diem_fallbacks import build_diem_route_preferences
    from services.marketdata.pathing.env import load_env_config

    config = load_env_config()
    print(f"Config: diem_token={config.diem_token}, vvv_token={config.vvv_token}")
    print(
        f"Config: quote_token={config.quote_token}, diem_vvv_pair={config.diem_vvv_pair}"
    )
    print(
        f"Config: vvv_usdc_pool={config.vvv_usdc_pool}, vvv_weth_pool={config.vvv_weth_pool}"
    )

    usdc = os.getenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )
    diem = os.getenv("DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024")

    routes = build_diem_route_preferences(usdc, diem, config)

    print("\n=== USDC -> DIEM Routes (Buy) ===")
    for i, route in enumerate(routes, 1):
        print(f"{i}. {' -> '.join(route.tokens[:6])} ({len(route.tokens) - 1} hops)")
        print(f"   fees: {[h.fee for h in route.hops]}")

    # Verify we have at least 2 routes
    assert len(routes) >= 2, f"Expected at least 2 routes, got {len(routes)}"

    # Verify 3-hop route exists (USDC -> WETH -> VVV -> DIEM)
    three_hop_found = any(len(route.tokens) == 4 for route in routes)
    assert three_hop_found, "Expected 3-hop route via WETH"

    # Verify WETH is in one of the routes
    weth = "0x4200000000000000000000000000000000000006"
    weth_route_found = any(
        weth.lower() in [t.lower() for t in route.tokens] for route in routes
    )
    assert weth_route_found, "Expected WETH in route"


def test_three_hop_route_preferences_sell():
    """Test that DIEM -> USDC includes 3-hop route via WETH."""
    # Set environment variables before importing
    os.environ["VVV_WETH_POOL_ADDRESS"] = "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce"
    os.environ["VVV_WETH_POOL_FEE"] = "500"
    os.environ["DIEM_ENABLE_THREE_HOP_WETH"] = "1"
    os.environ["DIEM_MAX_ROUTE_HOPS"] = "3"
    os.environ["DIEM_VVV_PAIR_ADDRESS"] = "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    os.environ["VVV_USDC_POOL_ADDRESS"] = "0x67a11022b7b6ed66f81233f6c8ed6e48f7826530"

    from libs.dex.diem_fallbacks import build_diem_route_preferences
    from services.marketdata.pathing.env import load_env_config

    config = load_env_config()

    usdc = os.getenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )
    diem = os.getenv("DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024")

    routes = build_diem_route_preferences(diem, usdc, config)

    print("\n=== DIEM -> USDC Routes (Sell) ===")
    for i, route in enumerate(routes, 1):
        print(f"{i}. {' -> '.join(route.tokens[:6])} ({len(route.tokens) - 1} hops)")
        print(f"   fees: {[h.fee for h in route.hops]}")

    # Verify we have at least 2 routes
    assert len(routes) >= 2, f"Expected at least 2 routes, got {len(routes)}"

    # Verify 3-hop route exists (DIEM -> VVV -> WETH -> USDC)
    three_hop_found = any(len(route.tokens) == 4 for route in routes)
    assert three_hop_found, "Expected 3-hop route via WETH"


def test_env_config_loads_vvv_weth_pool():
    """Test that EnvConfig loads vvv_weth_pool correctly."""
    os.environ["VVV_WETH_POOL_ADDRESS"] = "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce"

    from services.marketdata.pathing.env import load_env_config

    config = load_env_config()

    print(f"\nvvv_weth_pool: {config.vvv_weth_pool}")
    assert config.vvv_weth_pool == "0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce"


if __name__ == "__main__":
    test_env_config_loads_vvv_weth_pool()
    test_three_hop_route_preferences_buy()
    test_three_hop_route_preferences_sell()
    print("\nAll tests passed!")
