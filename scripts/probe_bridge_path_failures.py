#!/usr/bin/env python3
"""
Comprehensive diagnostic script to probe bridge path failures.

Tests:
1. Why all providers fail the bridge path (DIEM→VVV→USDC)
2. Direct DIEM→USDC path liquidity
3. Verify route addresses/pool existence on-chain
4. Small exact-in test quotes to map liquidity
"""

import os
import sys
from pathlib import Path
from typing import Any

from web3 import Web3

# Add project root to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

try:
    from libs.env import load_dotenv_if_present

    load_dotenv_if_present(path=str(repo_root / ".env"), override=False)
except Exception:
    pass

from libs.agentkit_ext.web3_utils import get_contract, get_web3  # noqa: E402
from libs.dex.providers import DexAggregator, build_aggregator_from_env  # noqa: E402
from libs.dex.routes import RoutePlan, make_route  # noqa: E402


def _normalize_address(addr: str) -> str:
    """Normalize address to checksum."""
    return Web3.to_checksum_address(addr.lower())


def check_contract_exists(address: str) -> tuple[bool, str]:
    """Verify contract exists on-chain."""
    try:
        w3 = get_web3()
        code = w3.eth.get_code(_normalize_address(address))
        if len(code) == 0:
            return False, "No contract code"
        return True, f"Contract exists ({len(code)} bytes)"
    except Exception as e:
        return False, f"Error: {e}"


def check_pool_reserves(pool_addr: str, name: str) -> dict[str, Any] | None:
    """Check pool reserves via direct RPC call."""
    print(f"\n  Checking {name} reserves...")
    try:
        w3 = get_web3()
        pool = _normalize_address(pool_addr)

        # Try getReserves() (V2 style)
        try:
            reserves_data = w3.eth.call(
                {
                    "to": pool,
                    "data": "0x0902f1ac",  # getReserves()
                }
            )
            if len(reserves_data) >= 96:
                reserve0 = int.from_bytes(reserves_data[0:32], "big")
                reserve1 = int.from_bytes(reserves_data[32:64], "big")

                # Get token addresses
                token0_data = w3.eth.call({"to": pool, "data": "0x0dfe1681"})
                token1_data = w3.eth.call({"to": pool, "data": "0xd21220a7"})
                token0 = "0x" + token0_data[-20:].hex()
                token1 = "0x" + token1_data[-20:].hex()

                # Get decimals
                def get_decimals(addr):
                    try:
                        dec_data = w3.eth.call(
                            {
                                "to": _normalize_address(addr),
                                "data": "0x313ce567",  # decimals()
                            }
                        )
                        return int.from_bytes(dec_data, "big")
                    except Exception:
                        return 18

                dec0 = get_decimals(token0)
                dec1 = get_decimals(token1)

                r0_decimal = reserve0 / (10**dec0)
                r1_decimal = reserve1 / (10**dec1)

                print(f"    Token0: {token0} (decimals: {dec0})")
                print(f"    Token1: {token1} (decimals: {dec1})")
                print(f"    Reserve0: {reserve0:,} ({r0_decimal:,.6f})")
                print(f"    Reserve1: {reserve1:,} ({r1_decimal:,.6f})")

                return {
                    "token0": token0.lower(),
                    "token1": token1.lower(),
                    "reserve0": reserve0,
                    "reserve1": reserve1,
                    "reserve0_decimal": r0_decimal,
                    "reserve1_decimal": r1_decimal,
                    "decimals0": dec0,
                    "decimals1": dec1,
                }
        except Exception as e:
            print(f"    V2 getReserves() failed: {e}")

        # Try slot0() (V3 style)
        try:
            slot0_data = w3.eth.call(
                {
                    "to": pool,
                    "data": "0x3850c7bd",  # slot0()
                }
            )
            if len(slot0_data) >= 32:
                sqrt_price_x96 = int.from_bytes(slot0_data[0:32], "big")
                print(f"    V3 pool detected (sqrt_price_x96: {sqrt_price_x96})")

                token0_data = w3.eth.call({"to": pool, "data": "0x0dfe1681"})
                token1_data = w3.eth.call({"to": pool, "data": "0xd21220a7"})
                token0 = "0x" + token0_data[-20:].hex()
                token1 = "0x" + token1_data[-20:].hex()

                print(f"    Token0: {token0}")
                print(f"    Token1: {token1}")
                return {
                    "type": "v3",
                    "sqrt_price_x96": sqrt_price_x96,
                    "token0": token0.lower(),
                    "token1": token1.lower(),
                }
        except Exception as e:
            print(f"    V3 slot0() failed: {e}")

        return None
    except Exception as e:
        print(f"    Error checking pool: {e}")
        return None


def test_router_quote(
    router_addr: str, route: RoutePlan, amount_in: int, provider_name: str
) -> tuple[bool, str | None, Exception | None]:
    """Test router quote directly."""
    try:
        w3 = get_web3()
        router = _normalize_address(router_addr)

        # Try to load router contract
        try:
            router_contract = get_contract(w3, router, "uniswap_v2_router.json")
        except Exception:
            try:
                router_contract = get_contract(w3, router, "aerodrome_router.json")
            except Exception:
                return False, None, Exception("Could not load router ABI")

        # Build path
        path = route.to_uniswap_v2_path(checksum=True)

        # Call getAmountsOut
        try:
            amounts = router_contract.functions.getAmountsOut(amount_in, path).call()
            if amounts and len(amounts) > 0 and amounts[-1] > 0:
                return True, f"Quote: {amounts[0]} -> {amounts[-1]}", None
            return False, "Empty result", None
        except Exception as e:
            return False, None, e
    except Exception as e:
        return False, None, e


def test_bridge_path_legs(
    aggregator: DexAggregator, diem: str, vvv: str, usdc: str, probe_amounts: list[int]
):
    """Test each leg of the bridge path separately."""
    print("\n" + "=" * 80)
    print("TEST 1: Bridge Path Leg Analysis")
    print("=" * 80)

    # Check if DIEM_VVV_DIRECT_SWAP_ENABLE is set
    direct_enabled = os.getenv("DIEM_VVV_DIRECT_SWAP_ENABLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    pair_math_enabled = os.getenv(
        "DIEM_ENABLE_PAIR_MATH_FALLBACK", ""
    ).strip().lower() in {"1", "true", "yes", "on"}
    print("\nConfiguration:")
    print(f"  DIEM_VVV_DIRECT_SWAP_ENABLE: {direct_enabled}")
    print(f"  DIEM_ENABLE_PAIR_MATH_FALLBACK: {pair_math_enabled}")

    # Leg 1: DIEM → VVV
    print("\n[Leg 1] DIEM → VVV")
    print("-" * 80)
    route_leg1 = make_route([diem, vvv])
    print(f"Route: {route_leg1.tokens}")

    # Test with direct swap enabled
    old_direct = os.environ.get("DIEM_VVV_DIRECT_SWAP_ENABLE")
    old_pair_math = os.environ.get("DIEM_ENABLE_PAIR_MATH_FALLBACK")
    try:
        os.environ["DIEM_VVV_DIRECT_SWAP_ENABLE"] = "true"
        os.environ["DIEM_ENABLE_PAIR_MATH_FALLBACK"] = "true"
        from libs.dex.providers import build_aggregator_from_env

        aggregator_enabled = build_aggregator_from_env()

        for amount in probe_amounts:
            print(
                f"\n  Testing with {amount} wei ({amount / 1e18:.6f} DIEM) [direct swap enabled]..."
            )
            quotes = aggregator_enabled.quote_all(amount, route_leg1)
            if quotes:
                for q in quotes:
                    print(
                        f"    ✓ {q.provider}: {q.amount_in} -> {q.amount_out} ({q.amount_out / 1e18:.6f} VVV)"
                    )
            else:
                print("    ✗ No quotes from any provider")
    finally:
        if old_direct is not None:
            os.environ["DIEM_VVV_DIRECT_SWAP_ENABLE"] = old_direct
        elif "DIEM_VVV_DIRECT_SWAP_ENABLE" in os.environ:
            del os.environ["DIEM_VVV_DIRECT_SWAP_ENABLE"]
        if old_pair_math is not None:
            os.environ["DIEM_ENABLE_PAIR_MATH_FALLBACK"] = old_pair_math
        elif "DIEM_ENABLE_PAIR_MATH_FALLBACK" in os.environ:
            del os.environ["DIEM_ENABLE_PAIR_MATH_FALLBACK"]

    # Leg 2: VVV → USDC
    print("\n[Leg 2] VVV → USDC")
    print("-" * 80)
    route_leg2 = make_route([vvv, usdc], fees=[3000])  # Try V3 fee tier
    print(f"Route: {route_leg2.tokens} (fee: 3000)")

    for amount in probe_amounts:
        print(f"\n  Testing with {amount} wei ({amount / 1e18:.6f} VVV)...")
        quotes = aggregator.quote_all(amount, route_leg2)
        if quotes:
            for q in quotes:
                print(
                    f"    ✓ {q.provider}: {q.amount_in} -> {q.amount_out} ({q.amount_out / 1e6:.6f} USDC)"
                )
        else:
            print("    ✗ No quotes from any provider")

    # Full bridge path - with composite metadata for composite routing
    print("\n[Full Path] DIEM → VVV → USDC")
    print("-" * 80)
    route_full = make_route([diem, vvv, usdc], fees=[None, 3000])
    # Attach composite metadata to trigger composite routing
    try:
        from libs.dex.composite import attach_composite_metadata
        from services.marketdata.pathing.env import load_env_config
        from services.marketdata.pathing.fallbacks import (
            get_bridge_trade_path_with_metadata,
        )

        config = load_env_config()
        bridge_metadata = get_bridge_trade_path_with_metadata(config)
        if bridge_metadata:
            bridge_legs = bridge_metadata.get("legs", [])
            if bridge_legs:
                attach_composite_metadata(
                    route_full, bridge_legs=bridge_legs, is_composite=True
                )
                print(f"Route: {route_full.tokens} (with composite metadata)")
            else:
                print(f"Route: {route_full.tokens}")
        else:
            print(f"Route: {route_full.tokens}")
    except Exception as e:
        print(f"Route: {route_full.tokens} (metadata attachment failed: {e})")

    for amount in probe_amounts:
        print(f"\n  Testing with {amount} wei ({amount / 1e18:.6f} DIEM)...")
        # Use best_quote() to trigger composite routing (quote_all() only tries single-provider paths)
        quote = aggregator.best_quote(amount, route_full)
        if quote:
            print(
                f"    ✓ {quote.provider}: {quote.amount_in} -> {quote.amount_out} ({quote.amount_out / 1e6:.6f} USDC)"
            )
            # Also try quote_all to see individual provider attempts
            quotes = aggregator.quote_all(amount, route_full)
            if quotes:
                print(f"      Individual provider quotes: {len(quotes)} found")
                for q in quotes:
                    print(f"        - {q.provider}: {q.amount_in} -> {q.amount_out}")
        else:
            print("    ✗ No quotes from any provider (including composite)")
            # Try quote_all to see what individual providers return
            quotes = aggregator.quote_all(amount, route_full)
            if quotes:
                print(f"      Individual provider quotes: {len(quotes)} found")
                for q in quotes:
                    print(f"        - {q.provider}: {q.amount_in} -> {q.amount_out}")

            # Try direct router calls
            print("    Attempting direct router calls...")
            routers = {
                "uniswap_v2": os.getenv("UNISWAP_V2_ROUTER_ADDRESS"),
                "aerodrome": os.getenv("AERODROME_ROUTER_ADDRESS"),
            }
            for router_name, router_addr in routers.items():
                if router_addr:
                    success, msg, err = test_router_quote(
                        router_addr, route_full, amount, router_name
                    )
                    if success:
                        print(f"      ✓ {router_name}: {msg}")
                    elif err:
                        print(f"      ✗ {router_name}: {type(err).__name__}: {err}")
                    else:
                        print(f"      ✗ {router_name}: {msg}")


def test_direct_diem_usdc(
    aggregator: DexAggregator, diem: str, usdc: str, probe_amounts: list[int]
):
    """Test direct DIEM → USDC path."""
    print("\n" + "=" * 80)
    print("TEST 2: Direct DIEM → USDC Path")
    print("=" * 80)

    route = make_route([diem, usdc])
    print(f"Route: {route.tokens}")

    for amount in probe_amounts:
        print(f"\n  Testing with {amount} wei ({amount / 1e18:.6f} DIEM)...")
        quotes = aggregator.quote_all(amount, route)
        if quotes:
            for q in quotes:
                print(
                    f"    ✓ {q.provider}: {q.amount_in} -> {q.amount_out} ({q.amount_out / 1e6:.6f} USDC)"
                )
        else:
            print("    ✗ No quotes from any provider")


def check_factory_registration(
    pool_addr: str,
    factory_addr: str,
    factory_name: str,
    stable: bool | None = None,
    expected_fee: int | None = None,
) -> tuple[bool, str]:
    """Check if pool is registered with factory."""
    try:
        w3 = get_web3()
        pool = _normalize_address(pool_addr)
        factory = _normalize_address(factory_addr)

        # Get tokens from pool
        token0_data = w3.eth.call({"to": pool, "data": "0x0dfe1681"})
        token1_data = w3.eth.call({"to": pool, "data": "0xd21220a7"})
        token0 = "0x" + token0_data[-20:].hex()
        token1 = "0x" + token1_data[-20:].hex()

        # Try Aerodrome factory (requires stable flag)
        if factory_name.lower().startswith("aerodrome"):
            try:
                factory_contract = get_contract(w3, factory, "aerodrome_factory.json")
                to_try = [True, False] if stable is None else [stable]
                for test_stable in to_try:
                    try:
                        pair = factory_contract.functions.getPair(
                            token0, token1, test_stable
                        ).call()
                    except Exception as inner_exc:
                        return (
                            False,
                            f"Factory lookup failed ({factory_name}, stable={test_stable}): {inner_exc}",
                        )
                    if pair and pair.lower() == pool.lower():
                        return (
                            True,
                            f"Registered with {factory_name} (stable={test_stable})",
                        )
                return (
                    False,
                    f"Factory getPair returned zero address for {factory_name}",
                )
            except Exception as e:
                return False, f"Factory ABI lookup failed ({factory_name}): {e}"

        # Try Uniswap V2 factory
        try:
            factory_contract = get_contract(w3, factory, "uniswap_v2_factory.json")
            pair0 = factory_contract.functions.getPair(token0, token1).call()
            pair1 = factory_contract.functions.getPair(token1, token0).call()
            if pair0.lower() == pool.lower() or pair1.lower() == pool.lower():
                return True, f"Registered with {factory_name}"
        except Exception:
            pass

        # Try Uniswap V3 factory (requires fee)
        if (
            factory_name.lower().startswith("uniswap_v3")
            or "v3" in factory_name.lower()
        ):
            try:
                factory_contract = get_contract(w3, factory, "uniswap_v3_factory.json")
            except Exception as e:
                return False, f"Factory ABI lookup failed ({factory_name}): {e}"
            fees_to_try: list[int] = []
            if expected_fee is not None:
                fees_to_try.append(int(expected_fee))
            fees_to_try.extend([500, 3000, 10000])
            seen_fees = []
            for fee in fees_to_try:
                if fee in seen_fees:
                    continue
                seen_fees.append(fee)
                try:
                    pool_addr_from_factory = factory_contract.functions.getPool(
                        token0, token1, fee
                    ).call()
                except Exception as inner_exc:
                    return (
                        False,
                        f"Factory lookup failed ({factory_name}, fee={fee}): {inner_exc}",
                    )
                if (
                    pool_addr_from_factory
                    and pool_addr_from_factory.lower() == pool.lower()
                ):
                    return True, f"Registered with {factory_name} (fee={fee})"
            return False, f"Factory getPool returned zero address for fees {seen_fees}"

        return False, f"Not registered with {factory_name}"
    except Exception as e:
        return False, f"Error: {e}"


def verify_path_encoding(route: RoutePlan, provider_name: str) -> dict[str, Any]:
    """Verify path encoding matches provider expectations."""
    result = {
        "provider": provider_name,
        "route_tokens": route.tokens,
        "route_hops": len(route.hops),
        "encoded_path": None,
        "encoding_valid": False,
        "errors": [],
    }

    try:
        if provider_name == "uniswap_v2":
            path = route.to_uniswap_v2_path(checksum=True)
            result["encoded_path"] = path
            result["encoding_valid"] = len(path) >= 2
            if len(path) < 2:
                result["errors"].append("Path must have at least 2 tokens")
        elif provider_name == "uniswap_v3":
            try:
                path_bytes = route.to_uniswap_v3_path_bytes(reverse=False)
                result["encoded_path"] = path_bytes.hex()
                result["encoding_valid"] = True
                # Check fee tiers
                for i, hop in enumerate(route.hops):
                    if hop.fee is None:
                        result["errors"].append(f"Hop {i} missing fee tier")
                        result["encoding_valid"] = False
            except Exception as e:
                result["errors"].append(f"V3 encoding failed: {e}")
                result["encoding_valid"] = False
        elif provider_name == "aerodrome":
            # Aerodrome uses struct array format: [{from, to, stable}, ...]
            # Verify route can be normalized (fees stripped) and path is valid
            try:
                from libs.dex.routing import normalize_route_for_aerodrome

                normalized = normalize_route_for_aerodrome(route)
                path = normalized.to_uniswap_v2_path(checksum=True)
                result["encoded_path"] = path
                result["encoding_valid"] = len(path) >= 2
                # Check that fees are stripped (Aerodrome doesn't use fee tiers)
                has_fees = any(hop.fee is not None for hop in route.hops)
                if has_fees:
                    result["warnings"] = result.get("warnings", [])
                    result["warnings"].append(
                        "Route has fees but Aerodrome strips them (uses stable flag instead)"
                    )
            except Exception as e:
                result["errors"].append(f"Aerodrome normalization failed: {e}")
                result["encoding_valid"] = False
            # Check if stable flag is needed
            stable_env = os.getenv("AERODROME_STABLE", "").strip().lower()
            result["stable_flag"] = stable_env in {"1", "true", "yes", "on"}
    except Exception as e:
        result["errors"].append(f"Encoding error: {e}")
        result["encoding_valid"] = False

    return result


def replay_minimal_router_call(
    router_addr: str, route: RoutePlan, amount_in: int, provider_name: str
) -> dict[str, Any]:
    """Replay minimal router call to isolate revert location."""
    result = {
        "provider": provider_name,
        "router": router_addr,
        "amount_in": amount_in,
        "route": route.tokens,
        "success": False,
        "error": None,
        "call_data": None,
        "revert_reason": None,
    }

    try:
        w3 = get_web3()
        router = _normalize_address(router_addr)

        # Build call data based on provider
        if provider_name == "uniswap_v2":
            try:
                router_contract = get_contract(w3, router, "uniswap_v2_router.json")
                path = route.to_uniswap_v2_path(checksum=True)
                fn = router_contract.functions.getAmountsOut(amount_in, path)
                call_data = fn._encode_transaction_data()
                # Ensure call_data is bytes before calling hex()
                if isinstance(call_data, bytes):
                    result["call_data"] = call_data.hex()
                else:
                    result["call_data"] = str(call_data)

                # Try the call
                try:
                    amounts = fn.call()
                    result["success"] = True
                    result["amounts"] = amounts
                except Exception as e:
                    result["error"] = str(e)
                    # Try to decode revert reason
                    if hasattr(e, "args") and len(e.args) > 0:
                        result["revert_reason"] = str(e.args[0])
            except Exception as e:
                result["error"] = f"Failed to build call: {e}"

        elif provider_name == "aerodrome":
            try:
                router_contract = get_contract(w3, router, "aerodrome_router.json")
                # Aerodrome uses struct array format: [{from, to, stable}, ...]
                from libs.dex.routing import normalize_route_for_aerodrome

                normalized = normalize_route_for_aerodrome(route)
                path = normalized.to_uniswap_v2_path(checksum=True)
                # Build routes as tuple array - web3.py encodes tuples correctly for struct arrays
                # Aerodrome router expects: (from: address, to: address, stable: bool)[]
                stable_default = os.getenv("AERODROME_STABLE", "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }
                routes = []
                for i in range(len(path) - 1):
                    routes.append((path[i], path[i + 1], stable_default))
                fn = router_contract.functions.getAmountsOut(amount_in, routes)
                call_data = fn._encode_transaction_data()
                # Ensure call_data is bytes before calling hex()
                if isinstance(call_data, bytes):
                    result["call_data"] = call_data.hex()
                else:
                    result["call_data"] = str(call_data)

                try:
                    amounts = fn.call()
                    result["success"] = True
                    result["amounts"] = amounts
                except Exception as e:
                    result["error"] = str(e)
                    if hasattr(e, "args") and len(e.args) > 0:
                        result["revert_reason"] = str(e.args[0])
            except Exception as e:
                result["error"] = f"Failed to build call: {e}"

        elif provider_name == "uniswap_v3":
            try:
                quoter_addr = os.getenv("UNISWAP_V3_QUOTER_ADDRESS")
                if not quoter_addr:
                    result["error"] = "UNISWAP_V3_QUOTER_ADDRESS not set"
                    return result

                quoter = get_contract(w3, quoter_addr, "uniswap_v3_quoter.json")
                path_bytes = route.to_uniswap_v3_path_bytes(reverse=False)
                # Ensure path_bytes is bytes, not string
                if isinstance(path_bytes, str):
                    if path_bytes.startswith("0x"):
                        path_bytes = bytes.fromhex(path_bytes[2:])
                    else:
                        path_bytes = bytes.fromhex(path_bytes)
                elif not isinstance(path_bytes, bytes):
                    path_bytes = bytes(path_bytes)
                fn = quoter.functions.quoteExactInput(path_bytes, amount_in)
                call_data = fn._encode_transaction_data()
                # Ensure call_data is bytes before calling hex()
                if isinstance(call_data, bytes):
                    result["call_data"] = call_data.hex()
                else:
                    result["call_data"] = str(call_data)

                try:
                    amount_out = fn.call()
                    result["success"] = True
                    result["amount_out"] = amount_out
                except Exception as e:
                    result["error"] = str(e)
                    if hasattr(e, "args") and len(e.args) > 0:
                        result["revert_reason"] = str(e.args[0])
            except Exception as e:
                result["error"] = f"Failed to build call: {e}"

    except Exception as e:
        result["error"] = f"Unexpected error: {e}"

    return result


def verify_pool_addresses(
    diem_vvv_pair: str, vvv_usdc_pool: str, diem: str, vvv: str, usdc: str
):
    """Verify pool addresses exist on-chain and check factory registration."""
    print("\n" + "=" * 80)
    print("TEST 3: Pool Address Verification & Factory Registration")
    print("=" * 80)

    # Check DIEM/VVV pair
    print(f"\n[DIEM/VVV Pair] {diem_vvv_pair}")
    print("-" * 80)
    exists, msg = check_contract_exists(diem_vvv_pair)
    print(f"  Contract exists: {exists} - {msg}")
    if exists:
        reserves = check_pool_reserves(diem_vvv_pair, "DIEM/VVV")
        if reserves:
            print("  ✓ Pool has liquidity")

            # Check factory registration - try Aerodrome factories
            aerodrome_volatile_factory = os.getenv(
                "DIEM_VVV_FACTORY_ADDRESS", "0x420dd381b31aef6683db6b902084cb0ffece40da"
            )
            aerodrome_stable_factory = "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"

            print("\n  Checking factory registration...")
            registered_volatile, reg_msg_volatile = check_factory_registration(
                diem_vvv_pair,
                aerodrome_volatile_factory,
                "Aerodrome Volatile Factory",
                stable=False,
            )
            print(f"    Volatile Factory: {reg_msg_volatile}")

            registered_stable, reg_msg_stable = check_factory_registration(
                diem_vvv_pair,
                aerodrome_stable_factory,
                "Aerodrome Stable Factory",
                stable=True,
            )
            print(f"    Stable Factory: {reg_msg_stable}")

            if not registered_volatile and not registered_stable:
                print("    ⚠ WARNING: Pool not registered with Aerodrome factories!")
        else:
            print("  ✗ Pool exists but no reserves found")

    # Check VVV/USDC pool
    print(f"\n[VVV/USDC Pool] {vvv_usdc_pool}")
    print("-" * 80)
    exists, msg = check_contract_exists(vvv_usdc_pool)
    print(f"  Contract exists: {exists} - {msg}")
    if exists:
        reserves = check_pool_reserves(vvv_usdc_pool, "VVV/USDC")
        if reserves:
            print("  ✓ Pool has liquidity")

            # Check if it's a V3 pool and verify with factory
            if reserves.get("type") == "v3":
                v3_factory = os.getenv(
                    "VVV_USDC_POOL_FACTORY",
                    "0x33128a8fc17869897dce68ed026d694621f6fdfd",
                )
                print("\n  Checking V3 factory registration...")
                v3_fee = int(os.getenv("VVV_USDC_POOL_FEE", reserves.get("fee", 3000)))
                registered, reg_msg = check_factory_registration(
                    vvv_usdc_pool,
                    v3_factory,
                    "Uniswap V3 Factory",
                    expected_fee=v3_fee,
                )
                print(f"    V3 Factory: {reg_msg}")
        else:
            print("  ✗ Pool exists but no reserves found")


def test_path_encoding_and_router_calls(diem: str, vvv: str, usdc: str):
    """Test path encoding and replay minimal router calls."""
    print("\n" + "=" * 80)
    print("TEST 5: Path Encoding & Minimal Router Call Replay")
    print("=" * 80)

    # Test bridge path - Leg1 should have no fee (Aerodrome V2), Leg2 should have fee 3000 (V3)
    route_bridge = make_route([diem, vvv, usdc], fees=[None, 3000])
    route_leg1 = make_route([diem, vvv], fees=[None])  # No fee for Aerodrome V2
    route_leg2 = make_route([vvv, usdc], fees=[3000])  # Fee 3000 for V3

    # Attach composite metadata to bridge route for composite routing
    try:
        from libs.dex.composite import attach_composite_metadata
        from services.marketdata.pathing.env import load_env_config
        from services.marketdata.pathing.fallbacks import (
            get_bridge_trade_path_with_metadata,
        )

        config = load_env_config()
        bridge_metadata = get_bridge_trade_path_with_metadata(config)
        if bridge_metadata:
            bridge_legs = bridge_metadata.get("legs", [])
            if bridge_legs:
                attach_composite_metadata(
                    route_bridge, bridge_legs=bridge_legs, is_composite=True
                )
    except Exception:
        pass  # Metadata attachment is optional for testing

    probe_amount = 10**15  # 0.001 DIEM

    routers = {
        "uniswap_v2": os.getenv("UNISWAP_V2_ROUTER_ADDRESS"),
        "aerodrome": os.getenv("AERODROME_ROUTER_ADDRESS"),
        "uniswap_v3": os.getenv("UNISWAP_V3_ROUTER_ADDRESS"),
    }

    print("\n[Path Encoding Verification]")
    print("-" * 80)
    for provider_name, router_addr in routers.items():
        if not router_addr:
            continue

        print(f"\n{provider_name.upper()}:")
        print(f"  Router: {router_addr}")

        # Test encoding for each route
        for route_name, route in [
            ("Leg1 (DIEM→VVV)", route_leg1),
            ("Leg2 (VVV→USDC)", route_leg2),
            ("Full Path", route_bridge),
        ]:
            encoding = verify_path_encoding(route, provider_name)
            print(f"\n  {route_name}:")
            print(f"    Tokens: {encoding['route_tokens']}")
            print(f"    Encoding valid: {encoding['encoding_valid']}")
            if encoding["encoded_path"]:
                if isinstance(encoding["encoded_path"], list):
                    print(f"    Encoded path: {encoding['encoded_path']}")
                else:
                    print(f"    Encoded path (hex): {encoding['encoded_path'][:64]}...")
            if encoding["errors"]:
                print(f"    Errors: {encoding['errors']}")

    print("\n[Minimal Router Call Replay]")
    print("-" * 80)
    for provider_name, router_addr in routers.items():
        if not router_addr:
            continue

        print(f"\n{provider_name.upper()} - Testing Leg1 (DIEM→VVV):")
        result = replay_minimal_router_call(
            router_addr, route_leg1, probe_amount, provider_name
        )
        print(f"  Success: {result['success']}")
        if result["success"]:
            if "amounts" in result:
                print(f"  Amounts: {result['amounts']}")
            if "amount_out" in result:
                print(f"  Amount out: {result['amount_out']}")
        else:
            print(f"  Error: {result['error']}")
            if result["revert_reason"]:
                print(f"  Revert reason: {result['revert_reason']}")
            if result["call_data"]:
                print(f"  Call data: {result['call_data'][:66]}...")

        print(f"\n{provider_name.upper()} - Testing Leg2 (VVV→USDC):")
        result = replay_minimal_router_call(
            router_addr, route_leg2, probe_amount, provider_name
        )
        print(f"  Success: {result['success']}")
        if not result["success"]:
            print(f"  Error: {result['error']}")
            if result["revert_reason"]:
                print(f"  Revert reason: {result['revert_reason']}")

        print(f"\n{provider_name.upper()} - Testing Full Path:")
        result = replay_minimal_router_call(
            router_addr, route_bridge, probe_amount, provider_name
        )
        print(f"  Success: {result['success']}")
        if not result["success"]:
            print(f"  Error: {result['error']}")
            if result["revert_reason"]:
                print(f"  Revert reason: {result['revert_reason']}")


def test_small_exact_in_quotes(
    aggregator: DexAggregator, diem: str, vvv: str, usdc: str
):
    """Test progressively smaller exact-in quotes to map liquidity."""
    print("\n" + "=" * 80)
    print("TEST 4: Small Exact-In Quote Mapping")
    print("=" * 80)

    # Test amounts from 1 DIEM down to 0.000001 DIEM
    test_amounts = [
        10**18,  # 1 DIEM
        10**17,  # 0.1 DIEM
        10**16,  # 0.01 DIEM
        10**15,  # 0.001 DIEM
        10**14,  # 0.0001 DIEM
        10**13,  # 0.00001 DIEM
        10**12,  # 0.000001 DIEM
    ]

    route_bridge = make_route([diem, vvv, usdc], fees=[None, 3000])
    route_direct = make_route([diem, usdc])

    # Attach composite metadata to bridge route for composite routing
    try:
        from libs.dex.composite import attach_composite_metadata
        from services.marketdata.pathing.env import load_env_config
        from services.marketdata.pathing.fallbacks import (
            get_bridge_trade_path_with_metadata,
        )

        config = load_env_config()
        bridge_metadata = get_bridge_trade_path_with_metadata(config)
        if bridge_metadata:
            bridge_legs = bridge_metadata.get("legs", [])
            if bridge_legs:
                attach_composite_metadata(
                    route_bridge, bridge_legs=bridge_legs, is_composite=True
                )
    except Exception:
        pass  # Metadata attachment is optional for testing

    print("\n[Bridge Path] DIEM → VVV → USDC (using best_quote for composite routing)")
    print("-" * 80)
    for amount in test_amounts:
        # Use best_quote() to trigger composite routing (quote_all() only tries single-provider paths)
        quote = aggregator.best_quote(amount, route_bridge)
        if quote:
            print(
                f"  ✓ {amount / 1e18:.6f} DIEM -> {quote.amount_out / 1e6:.6f} USDC ({quote.provider})"
            )
        else:
            # Fallback to quote_all to show individual provider attempts
            quotes = aggregator.quote_all(amount, route_bridge)
            if quotes:
                best = max(quotes, key=lambda q: q.amount_out)
                print(
                    f"  ⚠ {amount / 1e18:.6f} DIEM -> {best.amount_out / 1e6:.6f} USDC ({best.provider}) [single-provider, no composite]"
                )
            else:
                print(f"  ✗ {amount / 1e18:.6f} DIEM -> No quotes")

    print("\n[Direct Path] DIEM → USDC")
    print("-" * 80)
    for amount in test_amounts:
        quotes = aggregator.quote_all(amount, route_direct)
        if quotes:
            best = max(quotes, key=lambda q: q.amount_out)
            print(
                f"  ✓ {amount / 1e18:.6f} DIEM -> {best.amount_out / 1e6:.6f} USDC ({best.provider})"
            )
        else:
            print(f"  ✗ {amount / 1e18:.6f} DIEM -> No quotes")


def main():
    """Run all diagnostic tests."""
    print("=" * 80)
    print("BRIDGE PATH FAILURE DIAGNOSTIC")
    print("=" * 80)

    # Get token addresses
    diem = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
    vvv = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
    usdc = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()

    if not diem or not vvv or not usdc:
        print("ERROR: Missing token addresses in environment")
        print(f"  DIEM_TOKEN_ADDRESS: {diem or 'NOT SET'}")
        print(f"  VVV_TOKEN_ADDRESS: {vvv or 'NOT SET'}")
        print(f"  QUOTE_TOKEN_ADDRESS: {usdc or 'NOT SET'}")
        sys.exit(1)

    print("\nToken Addresses:")
    print(f"  DIEM: {diem}")
    print(f"  VVV:  {vvv}")
    print(f"  USDC: {usdc}")

    # Get pool addresses
    diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip().lower()
    vvv_usdc_pool = (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip().lower()

    print("\nPool Addresses:")
    print(f"  DIEM/VVV Pair: {diem_vvv_pair or 'NOT SET'}")
    print(f"  VVV/USDC Pool: {vvv_usdc_pool or 'NOT SET'}")

    # Build aggregator
    try:
        aggregator = build_aggregator_from_env()
        print(f"\nDEX Providers: {[p.name for p in aggregator.providers]}")
    except Exception as e:
        print(f"\nERROR: Failed to build aggregator: {e}")
        sys.exit(1)

    # Probe amounts: start small to avoid slippage issues
    probe_amounts = [
        10**15,  # 0.001 DIEM
        10**16,  # 0.01 DIEM
        10**17,  # 0.1 DIEM
    ]

    # Run tests
    try:
        # Test 1: Bridge path leg analysis
        test_bridge_path_legs(aggregator, diem, vvv, usdc, probe_amounts)

        # Test 2: Direct DIEM → USDC
        test_direct_diem_usdc(aggregator, diem, usdc, probe_amounts)

        # Test 3: Verify pool addresses and factory registration
        if diem_vvv_pair and vvv_usdc_pool:
            verify_pool_addresses(diem_vvv_pair, vvv_usdc_pool, diem, vvv, usdc)

        # Test 4: Small exact-in quotes
        test_small_exact_in_quotes(aggregator, diem, vvv, usdc)

        # Test 5: Path encoding and minimal router calls
        test_path_encoding_and_router_calls(diem, vvv, usdc)

        print("\n" + "=" * 80)
        print("DIAGNOSTIC COMPLETE")
        print("=" * 80)

    except Exception as e:
        print(f"\nERROR during diagnostics: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
