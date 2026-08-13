#!/usr/bin/env python3
"""Check which router/factory the DIEM/VVV pool uses."""

from __future__ import annotations

import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

try:
    from libs.env import load_dotenv_if_present

    load_dotenv_if_present(path=str(repo_root / ".env"), override=False)
except Exception:
    pass

from web3 import Web3  # noqa: E402

from libs.agentkit_ext.web3_utils import get_web3  # noqa: E402

# Known factories
FACTORIES = {
    "0x420DD381b31aEf6683db6B902084cB0FFECe40Da": "Aerodrome Volatile Factory",
    "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A": "Aerodrome Stable Factory",
    "0x33128a8fc17869897dce68ed026d694621f6fdfd": "Uniswap V3 Factory",
}

ROUTERS = {
    "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5": "Aerodrome Slipstream",
    "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43": "Aerodrome Classic",
}


def get_pair_factory(pair_addr: str) -> tuple[str | None, str | None]:
    """Get factory address from a V2 pair."""
    try:
        w3 = get_web3()
        pair_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "factory",
                "outputs": [{"name": "", "type": "address"}],
                "type": "function",
            }
        ]
        pair = w3.eth.contract(
            address=Web3.to_checksum_address(pair_addr), abi=pair_abi
        )
        factory = pair.functions.factory().call()
        factory_checksum = Web3.to_checksum_address(factory)
        factory_name = FACTORIES.get(factory_checksum, "Unknown Factory")
        return factory_checksum, factory_name
    except Exception as e:
        return None, f"Error: {e}"


def get_router_from_factory(factory_addr: str) -> str | None:
    """Determine which router matches a factory."""
    # Aerodrome Volatile Factory -> Slipstream router
    if factory_addr.lower() == "0x420dd381b31aef6683db6b902084cb0ffece40da":
        return "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"  # Slipstream
    # Aerodrome Stable Factory -> Slipstream router
    if factory_addr.lower() == "0x5e7bb104d84c7cb9b682aac2f3d509f5f406809a":
        return "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"  # Slipstream
    return None


def main() -> None:
    """Check which router the DIEM/VVV pool uses."""
    print("=" * 70)
    print("DIEM/VVV Pool Router Check")
    print("=" * 70)
    print()

    diem_vvv_pair = os.getenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    )
    current_aerodrome_router = os.getenv("AERODROME_ROUTER_ADDRESS", "")

    print(f"DIEM/VVV Pair Address: {diem_vvv_pair}")
    print()

    factory_addr, factory_name = get_pair_factory(diem_vvv_pair)

    if factory_addr:
        print(f"Factory Address: {factory_addr}")
        print(f"Factory Name:     {factory_name}")
        print()

        # Determine correct router
        correct_router = get_router_from_factory(factory_addr)

        if correct_router:
            router_name = ROUTERS.get(correct_router, "Unknown")
            print(f"Recommended Router: {correct_router}")
            print(f"Router Name:        {router_name}")
            print()

            if current_aerodrome_router:
                current_lower = current_aerodrome_router.lower()
                correct_lower = correct_router.lower()

                if current_lower == correct_lower:
                    print("✓ Current router matches pool factory!")
                else:
                    print("✗ MISMATCH DETECTED!")
                    print()
                    print(f"Current router:    {current_aerodrome_router}")
                    print(
                        f"                  ({ROUTERS.get(current_aerodrome_router, 'Unknown')})"
                    )
                    print()
                    print(f"Required router:   {correct_router}")
                    print(f"                  ({router_name})")
                    print()
                    print("=" * 70)
                    print("FIX REQUIRED")
                    print("=" * 70)
                    print()
                    print("Update your .env file:")
                    print(f"AERODROME_ROUTER_ADDRESS={correct_router}")
                    print()
            else:
                print("⚠ AERODROME_ROUTER_ADDRESS not set in .env")
                print()
                print("Add to your .env file:")
                print(f"AERODROME_ROUTER_ADDRESS={correct_router}")
        else:
            print("⚠ Could not determine router from factory")
            print("  Factory might be Uniswap V3 or unknown")
    else:
        print(f"✗ Failed to read factory from pair: {factory_name}")
        print()
        print("This could mean:")
        print("  - Pair address is incorrect")
        print("  - RPC connection issue")
        print("  - Pair is not a standard V2 pair")

    print()


if __name__ == "__main__":
    main()
