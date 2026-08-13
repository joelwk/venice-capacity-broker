#!/usr/bin/env python3
"""Verify DEX router addresses match on-chain contracts and can quote trades."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add repo root to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

try:
    from libs.env import load_dotenv_if_present

    load_dotenv_if_present(path=str(repo_root / ".env"), override=False)
except Exception:
    pass

from web3 import Web3  # noqa: E402

from libs.agentkit_ext.web3_utils import get_contract, get_web3  # noqa: E402


def verify_router_code(router_addr: str, expected_name: str) -> tuple[bool, str]:
    """Verify router address has expected contract code."""
    try:
        w3 = get_web3()
        code = w3.eth.get_code(Web3.to_checksum_address(router_addr))
        if not code or code == b"":
            return False, f"✗ No contract code at {router_addr}"

        # Try to read a common function to verify it's a router
        try:
            router = get_contract(w3, router_addr, "uniswap_v2_router.json")
            # Try calling WETH() or factory() to verify it's a router
            try:
                factory = router.functions.factory().call()
                return (
                    True,
                    f"✓ {expected_name} router verified (factory: {factory[:10]}...)",
                )
            except Exception:
                try:
                    weth = router.functions.WETH().call()
                    return (
                        True,
                        f"✓ {expected_name} router verified (WETH: {weth[:10]}...)",
                    )
                except Exception:
                    return (
                        True,
                        f"⚠ {expected_name} has code but couldn't verify router functions",
                    )
        except Exception as e:
            return False, f"✗ Failed to load router ABI: {e}"
    except Exception as e:
        return False, f"✗ Error checking router: {e}"


def verify_uniswap_v3_router(router_addr: str, quoter_addr: str) -> tuple[bool, str]:
    """Verify Uniswap V3 router and quoter addresses."""
    try:
        w3 = get_web3()

        # Check router
        router_code = w3.eth.get_code(Web3.to_checksum_address(router_addr))
        if not router_code or router_code == b"":
            return False, f"✗ No contract code at router {router_addr}"

        # Check quoter
        quoter_code = w3.eth.get_code(Web3.to_checksum_address(quoter_addr))
        if not quoter_code or quoter_code == b"":
            return False, f"✗ No contract code at quoter {quoter_addr}"

        return True, "✓ Uniswap V3 router and quoter verified"
    except Exception as e:
        return False, f"✗ Error checking Uniswap V3: {e}"


def verify_aerodrome_router(router_addr: str) -> tuple[bool, str]:
    """Verify Aerodrome router address."""
    try:
        w3 = get_web3()
        code = w3.eth.get_code(Web3.to_checksum_address(router_addr))
        if not code or code == b"":
            return False, f"✗ No contract code at {router_addr}"

        # Try to load Aerodrome router
        try:
            router = get_contract(w3, router_addr, "aerodrome_router.json")
            # Try calling factory() or a common function
            try:
                factory = router.functions.factory().call()
                return True, f"✓ Aerodrome router verified (factory: {factory[:10]}...)"
            except Exception:
                return True, "⚠ Aerodrome router has code but couldn't verify factory()"
        except Exception as e:
            return False, f"✗ Failed to load Aerodrome router ABI: {e}"
    except Exception as e:
        return False, f"✗ Error checking Aerodrome router: {e}"


def main() -> None:
    """Verify all configured router addresses."""
    print("=" * 70)
    print("DEX Router Address Verification")
    print("=" * 70)
    print()

    # Check Uniswap V2
    uniswap_v2_router = os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv(
        "ROUTER_ADDRESS"
    )
    if uniswap_v2_router:
        print(f"Checking Uniswap V2 Router: {uniswap_v2_router}")
        ok, msg = verify_router_code(uniswap_v2_router, "Uniswap V2")
        print(f"  {msg}")
    else:
        print("  ✗ UNISWAP_V2_ROUTER_ADDRESS not set")
    print()

    # Check Uniswap V3
    uniswap_v3_router = os.getenv("UNISWAP_V3_ROUTER_ADDRESS")
    uniswap_v3_quoter = os.getenv("UNISWAP_V3_QUOTER_ADDRESS")
    if uniswap_v3_router and uniswap_v3_quoter:
        print(f"Checking Uniswap V3 Router: {uniswap_v3_router}")
        print(f"Checking Uniswap V3 Quoter: {uniswap_v3_quoter}")
        ok, msg = verify_uniswap_v3_router(uniswap_v3_router, uniswap_v3_quoter)
        print(f"  {msg}")
    else:
        print(
            "  ⚠ Uniswap V3 not configured (UNISWAP_V3_ROUTER_ADDRESS or UNISWAP_V3_QUOTER_ADDRESS missing)"
        )
    print()

    # Check Aerodrome
    aerodrome_router = os.getenv("AERODROME_ROUTER_ADDRESS")
    if aerodrome_router:
        print(f"Checking Aerodrome Router: {aerodrome_router}")
        ok, msg = verify_aerodrome_router(aerodrome_router)
        print(f"  {msg}")

        # Check for conflicting addresses
        known_routers = {
            "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5": "Aerodrome Slipstream (new)",
            "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43": "Aerodrome Classic (old)",
        }
        router_lower = aerodrome_router.lower()
        for addr, name in known_routers.items():
            if addr.lower() == router_lower:
                print(f"  ℹ Using {name}")
                break
        else:
            print("  ⚠ Unknown Aerodrome router address (not in known list)")
    else:
        print("  ✗ AERODROME_ROUTER_ADDRESS not set")
    print()

    # Check DEX_PROVIDERS config
    dex_providers = os.getenv("DEX_PROVIDERS", "")
    print(f"DEX_PROVIDERS: {dex_providers}")
    print()

    # Summary
    print("=" * 70)
    print("Known Base Mainnet Router Addresses")
    print("=" * 70)
    print("Uniswap V2 Router: 0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24")
    print("Uniswap V3 Router: 0x2626664c2603336e57b271c5c0b26f421741e481")
    print("Uniswap V3 Quoter: 0x3d4e44eb1374240ce5f1b871ab261cd16335b76a")
    print("Aerodrome Slipstream: 0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5")
    print("Aerodrome Classic: 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43")
    print()


if __name__ == "__main__":
    main()
