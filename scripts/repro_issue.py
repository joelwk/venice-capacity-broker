import logging
import os

from libs.dex.providers import build_aggregator_from_env
from libs.dex.routes import make_route


def main():
    logging.basicConfig(level=logging.DEBUG)

    os.environ["UNISWAP_V3_ROUTER_ADDRESS"] = (
        "0x2626664c2603336e57b271c5c0b26f421741e481"
    )
    os.environ["UNISWAP_V3_QUOTER_ADDRESS"] = (
        "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a"
    )
    os.environ["AERODROME_ROUTER_ADDRESS"] = (
        "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
    )
    os.environ["AERODROME_FACTORY_VOLATILE"] = (
        "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    )
    os.environ["AERODROME_FACTORY_STABLE"] = (
        "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
    )
    os.environ["UNISWAP_V2_ROUTER_ADDRESS"] = (
        "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
    )
    os.environ["DIEM_VVV_PAIR_ADDRESS"] = "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    os.environ["DIEM_TOKEN_ADDRESS"] = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    os.environ["VVV_TOKEN_ADDRESS"] = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    os.environ["BASE_RPC_URL"] = os.getenv("BASE_RPC_URL") or "https://mainnet.base.org"
    os.environ["DEX_PROVIDERS"] = "uniswap_v2,aerodrome,uniswap_v3"
    os.environ["AERODROME_EXACT_OUT_ENABLE"] = "1"
    os.environ["DIEM_VVV_BRIDGE_PROVIDER"] = "aerodrome"
    os.environ["DIEM_DEBUG_ROUTES"] = "1"
    os.environ["LOG_LEVEL"] = "DEBUG"
    os.environ["DEX_PROVIDER_TIMEOUT_SECONDS"] = "15.0"

    try:
        agg = build_aggregator_from_env()
        print("Aggregator built")

        route = make_route(
            [
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
                "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # VVV
            ]
        )
        print(f"Route: {route.tokens}")

        q = agg.best_quote_exact_out(10**18, route)  # 1 DIEM exact-out
        print(f"Quote: {q}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
