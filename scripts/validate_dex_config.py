#!/usr/bin/env python3
"""
Validate DEX router/factory configuration against Base on-chain reality.

This script checks:
- Router addresses are valid contracts
- Factory addresses match routers
- DIEM/VVV and VVV/USDC pools exist and are accessible
- Pool addresses match configured factories
"""

import os
import sys

from libs.agentkit_ext.web3_utils import get_contract, get_web3
from libs.env import load_dotenv_if_present

load_dotenv_if_present()


def _normalize_address(addr: str) -> str:
    """Normalize address to checksummed format."""
    from web3 import Web3

    return Web3.to_checksum_address(addr)


def check_contract_exists(w3, address: str) -> bool:
    """Check if address is a contract."""
    try:
        code = w3.eth.get_code(address)
        return len(code) > 0
    except Exception:
        return False


def get_factory_from_pair(w3, pair_addr: str) -> str | None:
    """Get factory address from a Uniswap V2 / Aerodrome pair."""
    try:
        # Standard V2 pair ABI for factory()
        pair_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "factory",
                "outputs": [{"name": "", "type": "address"}],
                "type": "function",
            }
        ]
        pair = get_contract(w3, pair_addr, abi=pair_abi)
        factory = pair.functions.factory().call()
        return _normalize_address(factory)
    except Exception as e:
        print(f"  ⚠️  Could not get factory from pair {pair_addr}: {e}")
        return None


def get_factory_from_router(w3, router_addr: str) -> str | None:
    """Get factory address from router."""
    try:
        # Standard router ABI for factory()
        router_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "factory",
                "outputs": [{"name": "", "type": "address"}],
                "type": "function",
            }
        ]
        router = get_contract(w3, router_addr, abi=router_abi)
        factory = router.functions.factory().call()
        return _normalize_address(factory)
    except Exception:
        return None


def check_pair_reserves(w3, pair_addr: str) -> tuple[int, int] | None:
    """Get reserves from a V2 pair."""
    try:
        pair_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "getReserves",
                "outputs": [
                    {"name": "_reserve0", "type": "uint112"},
                    {"name": "_reserve1", "type": "uint112"},
                    {"name": "_blockTimestampLast", "type": "uint32"},
                ],
                "type": "function",
            }
        ]
        pair = get_contract(w3, pair_addr, abi=pair_abi)
        reserves = pair.functions.getReserves().call()
        return (reserves[0], reserves[1])
    except Exception:
        return None


def check_v3_pool_liquidity(w3, pool_addr: str) -> int | None:
    """Get liquidity from a V3 pool."""
    try:
        pool_abi = [
            {
                "constant": True,
                "inputs": [],
                "name": "liquidity",
                "outputs": [{"name": "", "type": "uint128"}],
                "type": "function",
            }
        ]
        pool = get_contract(w3, pool_addr, abi=pool_abi)
        liquidity = pool.functions.liquidity().call()
        return liquidity
    except Exception:
        return None


def validate_router(name: str, router_addr: str | None) -> dict[str, any]:
    """Validate a router configuration."""
    result = {
        "name": name,
        "router_address": router_addr,
        "valid": False,
        "factory_from_router": None,
        "errors": [],
    }

    if not router_addr:
        result["errors"].append("Router address not configured")
        return result

    try:
        w3 = get_web3()
        router_addr_norm = _normalize_address(router_addr)

        if not check_contract_exists(w3, router_addr_norm):
            result["errors"].append(f"Router {router_addr_norm} is not a contract")
            return result

        factory = get_factory_from_router(w3, router_addr_norm)
        result["factory_from_router"] = factory
        result["valid"] = True

    except Exception as e:
        result["errors"].append(f"Error validating router: {e}")

    return result


def validate_diem_pools() -> dict[str, any]:
    """Validate DIEM/VVV and VVV/USDC pool configuration."""
    result = {
        "diem_vvv_pair": {
            "address": None,
            "valid": False,
            "factory": None,
            "reserves": None,
            "errors": [],
        },
        "vvv_usdc_pool": {
            "address": None,
            "valid": False,
            "factory": None,
            "liquidity": None,
            "errors": [],
        },
    }

    diem_vvv_pair = os.getenv("DIEM_VVV_PAIR_ADDRESS")
    vvv_usdc_pool = os.getenv("VVV_USDC_POOL_ADDRESS")

    if not diem_vvv_pair and not vvv_usdc_pool:
        return result

    try:
        w3 = get_web3()

        # Check DIEM/VVV pair
        if diem_vvv_pair:
            result["diem_vvv_pair"]["address"] = diem_vvv_pair
            pair_norm = _normalize_address(diem_vvv_pair)

            if not check_contract_exists(w3, pair_norm):
                result["diem_vvv_pair"]["errors"].append("Pair is not a contract")
            else:
                factory = get_factory_from_pair(w3, pair_norm)
                result["diem_vvv_pair"]["factory"] = factory

                reserves = check_pair_reserves(w3, pair_norm)
                if reserves:
                    result["diem_vvv_pair"]["reserves"] = reserves
                    result["diem_vvv_pair"]["valid"] = True
                else:
                    result["diem_vvv_pair"]["errors"].append("Could not read reserves")

        # Check VVV/USDC pool (V3)
        if vvv_usdc_pool:
            result["vvv_usdc_pool"]["address"] = vvv_usdc_pool
            pool_norm = _normalize_address(vvv_usdc_pool)

            if not check_contract_exists(w3, pool_norm):
                result["vvv_usdc_pool"]["errors"].append("Pool is not a contract")
            else:
                liquidity = check_v3_pool_liquidity(w3, pool_norm)
                if liquidity is not None:
                    result["vvv_usdc_pool"]["liquidity"] = liquidity
                    result["vvv_usdc_pool"]["valid"] = True
                else:
                    result["vvv_usdc_pool"]["errors"].append("Could not read liquidity")

    except Exception as e:
        result["diem_vvv_pair"]["errors"].append(f"Error: {e}")
        result["vvv_usdc_pool"]["errors"].append(f"Error: {e}")

    return result


def main() -> int:
    """Main validation function."""
    print("DEX Configuration Validation")
    print("=" * 60)

    w3 = get_web3()
    chain_id = w3.eth.chain_id
    print(f"\nChain ID: {chain_id}")

    if chain_id != 8453:
        print("⚠️  Warning: Expected Base mainnet (chain_id=8453)")

    print("\n1. Router Validation")
    print("-" * 60)

    routers = [
        ("Uniswap V2", os.getenv("UNISWAP_V2_ROUTER_ADDRESS")),
        ("Aerodrome", os.getenv("AERODROME_ROUTER_ADDRESS")),
        ("Uniswap V3", os.getenv("UNISWAP_V3_ROUTER_ADDRESS")),
    ]

    router_results = []
    for name, addr in routers:
        if not addr:
            print(f"{name}: Not configured")
            continue

        result = validate_router(name, addr)
        router_results.append(result)

        status = "✅" if result["valid"] else "❌"
        print(f"{status} {name}: {addr}")

        if result["factory_from_router"]:
            print(f"   Factory: {result['factory_from_router']}")

        if result["errors"]:
            for err in result["errors"]:
                print(f"   Error: {err}")

    print("\n2. DIEM Pool Validation")
    print("-" * 60)

    pool_results = validate_diem_pools()

    # DIEM/VVV pair
    diem_vvv = pool_results["diem_vvv_pair"]
    if diem_vvv["address"]:
        status = "✅" if diem_vvv["valid"] else "❌"
        print(f"{status} DIEM/VVV Pair: {diem_vvv['address']}")

        if diem_vvv["factory"]:
            print(f"   Factory: {diem_vvv['factory']}")

        if diem_vvv["reserves"]:
            r0, r1 = diem_vvv["reserves"]
            print(f"   Reserves: {r0} / {r1}")

        if diem_vvv["errors"]:
            for err in diem_vvv["errors"]:
                print(f"   Error: {err}")
    else:
        print("DIEM/VVV Pair: Not configured")

    # VVV/USDC pool
    vvv_usdc = pool_results["vvv_usdc_pool"]
    if vvv_usdc["address"]:
        status = "✅" if vvv_usdc["valid"] else "❌"
        print(f"{status} VVV/USDC Pool: {vvv_usdc['address']}")

        if vvv_usdc["liquidity"]:
            print(f"   Liquidity: {vvv_usdc['liquidity']}")

        if vvv_usdc["errors"]:
            for err in vvv_usdc["errors"]:
                print(f"   Error: {err}")
    else:
        print("VVV/USDC Pool: Not configured")

    print("\n3. Factory Address Validation")
    print("-" * 60)

    factories = [
        ("Uniswap V2 Factory", os.getenv("UNISWAP_V2_FACTORY_ADDRESS")),
        ("Aerodrome Volatile Factory", os.getenv("AERODROME_FACTORY_VOLATILE")),
        ("Aerodrome Stable Factory", os.getenv("AERODROME_FACTORY_STABLE")),
        ("Uniswap V3 Factory", os.getenv("UNISWAP_V3_FACTORY_ADDRESS")),
        ("DIEM/VVV Factory", os.getenv("DIEM_VVV_FACTORY_ADDRESS")),
        ("VVV/USDC Factory", os.getenv("VVV_USDC_POOL_FACTORY")),
    ]

    for name, addr in factories:
        if not addr:
            continue

        try:
            addr_norm = _normalize_address(addr)
            is_contract = check_contract_exists(w3, addr_norm)
            status = "✅" if is_contract else "❌"
            print(f"{status} {name}: {addr_norm}")

            # Cross-check with router factory if available
            for router_result in router_results:
                if router_result["factory_from_router"] == addr_norm:
                    print(f"   ✓ Matches {router_result['name']} router factory")
                    break

            # Cross-check with DIEM/VVV pair factory
            if diem_vvv["factory"] == addr_norm:
                print("   ✓ Matches DIEM/VVV pair factory")

        except Exception as e:
            print(f"❌ {name}: {addr} - Error: {e}")

    print("\n" + "=" * 60)

    # Summary
    all_valid = (
        all(r["valid"] for r in router_results if r["router_address"])
        and diem_vvv["valid"]
        and vvv_usdc["valid"]
    )

    if all_valid:
        print("✅ All configured DEX components are valid")
        return 0
    print("❌ Some DEX components failed validation")
    return 1


if __name__ == "__main__":
    sys.exit(main())
