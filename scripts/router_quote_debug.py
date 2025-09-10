from __future__ import annotations

import os
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _load_env() -> None:
    try:
        from libs.env import load_dotenv_if_present  # type: ignore

        load_dotenv_if_present(path=str(_repo_root() / ".env"), override=False)
    except Exception:
        pass


def main() -> None:
    _load_env()
    from libs.agentkit_ext.web3_utils import get_web3, get_contract
    from web3 import Web3  # type: ignore

    router = os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv("ROUTER_ADDRESS")
    path_env = os.getenv("TRADE_PATH")
    if not router:
        print("UNISWAP_V2_ROUTER_ADDRESS is not set")
        raise SystemExit(2)
    if not path_env:
        print("TRADE_PATH is not set")
        raise SystemExit(2)
    path = [Web3.to_checksum_address(p.strip()) for p in path_env.split(",") if p.strip()]
    if len(path) < 2:
        print("TRADE_PATH must contain at least 2 addresses")
        raise SystemExit(2)
    # Amount in base units: default 1 token with DIEM_DECIMALS (or 18)
    try:
        dec = int(os.getenv("DIEM_DECIMALS") or 18)
    except Exception:
        dec = 18
    try:
        amount = int(os.getenv("QUOTE_DEBUG_AMOUNT") or (10 ** dec))
    except Exception:
        amount = 10 ** dec

    print(f"RPC_URL={os.getenv('RPC_URL') or os.getenv('BASE_RPC_URL')} chain_id={os.getenv('BASE_CHAIN_ID')}")
    print(f"router={router}")
    print(f"path={path}")
    print(f"amount_in={amount}")

    w3 = get_web3()
    print(f"connected={w3.is_connected()} block={w3.eth.block_number}")
    r = get_contract(w3, Web3.to_checksum_address(router), "uniswap_v2_router.json")
    try:
        amts = r.functions.getAmountsOut(amount, path).call()
        print(f"getAmountsOut -> {amts}")
    except Exception as e:  # noqa: BLE001
        print(f"getAmountsOut error: {e}")
        # Try smaller input to rule out overflow/rounding
        try:
            amt2 = max(1, amount // 10**6)
            amts2 = r.functions.getAmountsOut(amt2, path).call()
            print(f"getAmountsOut (amt/1e6) -> {amts2}")
        except Exception as e2:  # noqa: BLE001
            print(f"getAmountsOut (amt/1e6) error: {e2}")


if __name__ == "__main__":
    main()

