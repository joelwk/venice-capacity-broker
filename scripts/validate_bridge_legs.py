#!/usr/bin/env python3
"""Validate bridge legs directly with small exact-in probes.

Tests:
- DIEM→VVV (Aerodrome/V2) with AERODROME_STABLE toggled true/false
- VVV→USDC (Uniswap V3) with fee 3000 using quote_all and quote_all_exact_out

Confirms router addresses, fee tier, and token order.
"""

import os
import sys
from pathlib import Path

# Add project root to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from libs.dex.providers import DexAggregator, build_aggregator_from_env  # noqa: E402
from libs.dex.routes import make_route  # noqa: E402

# Load environment variables
try:
    from libs.env import load_dotenv_if_present

    load_dotenv_if_present(path=str(repo_root / ".env"), override=False)
except Exception:
    pass


def get_token_addresses() -> tuple[str, str, str]:
    """Get DIEM, VVV, and USDC token addresses from environment."""
    diem = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip()
    vvv = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip()
    usdc = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip()

    if not diem:
        raise ValueError("DIEM_TOKEN_ADDRESS not set")
    if not vvv:
        raise ValueError("VVV_TOKEN_ADDRESS not set")
    if not usdc:
        raise ValueError("QUOTE_TOKEN_ADDRESS not set")

    return diem.lower(), vvv.lower(), usdc.lower()


def get_router_addresses() -> dict[str, str | None]:
    """Get router addresses for each provider."""
    return {
        "aerodrome": (os.getenv("AERODROME_ROUTER_ADDRESS") or "").strip() or None,
        "uniswap_v2": (os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or "").strip() or None,
        "uniswap_v3": (os.getenv("UNISWAP_V3_ROUTER_ADDRESS") or "").strip() or None,
    }


def print_config(aggregator: DexAggregator, diem: str, vvv: str, usdc: str):
    """Print current configuration."""
    print("=" * 80)
    print("BRIDGE LEG VALIDATION - Configuration")
    print("=" * 80)
    print(f"DIEM: {diem}")
    print(f"VVV:  {vvv}")
    print(f"USDC: {usdc}")
    print()

    routers = get_router_addresses()
    print("Router Addresses:")
    for provider, addr in routers.items():
        status = addr if addr else "NOT SET"
        print(f"  {provider:15} {status}")
    print()

    # Check AERODROME_STABLE setting
    stable_env = os.getenv("AERODROME_STABLE") or os.getenv("DIEM_VVV_STABLE") or "true"
    stable_val = str(stable_env).strip().lower() in {"1", "true", "yes", "on"}
    print(f"AERODROME_STABLE: {stable_val} (from env: {stable_env})")

    # Check DIEM_VVV_PAIR_ADDRESS
    pair_addr = os.getenv("DIEM_VVV_PAIR_ADDRESS", "").strip()
    print(f"DIEM_VVV_PAIR_ADDRESS: {pair_addr if pair_addr else 'NOT SET'}")

    # Check DIEM_VVV_DIRECT_SWAP_ENABLE
    direct_enabled = os.getenv("DIEM_VVV_DIRECT_SWAP_ENABLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    print(f"DIEM_VVV_DIRECT_SWAP_ENABLE: {direct_enabled}")

    # Check VVV_USDC_POOL_FEE
    fee_env = os.getenv("VVV_USDC_POOL_FEE") or "3000"
    print(f"VVV_USDC_POOL_FEE: {fee_env}")
    print()

    # List active providers
    provider_names = [p.name for p in aggregator.providers]
    print(f"Active DEX Providers: {', '.join(provider_names)}")
    print("=" * 80)
    print()


def test_diem_vvv_leg(
    aggregator: DexAggregator,
    diem: str,
    vvv: str,
    probe_amount: int,
    stable: bool,
) -> list:
    """Test DIEM→VVV leg with specified stable flag."""
    print(f"Testing DIEM→VVV leg (AERODROME_STABLE={stable})")
    print(f"  Probe amount: {probe_amount} (wei)")

    route = make_route([diem, vvv])
    print(f"  Route: {route.tokens}")

    # Check DIEM_VVV_PAIR_ADDRESS
    pair_addr = os.getenv("DIEM_VVV_PAIR_ADDRESS", "").strip()
    if pair_addr:
        print(f"  DIEM_VVV_PAIR_ADDRESS: {pair_addr}")
    else:
        print("  DIEM_VVV_PAIR_ADDRESS: NOT SET")

    # Temporarily set AERODROME_STABLE and enable direct swap
    old_stable = os.environ.get("AERODROME_STABLE")
    old_diem_stable = os.environ.get("DIEM_VVV_STABLE")
    old_direct = os.environ.get("DIEM_VVV_DIRECT_SWAP_ENABLE")

    try:
        os.environ["AERODROME_STABLE"] = "true" if stable else "false"
        os.environ["DIEM_VVV_STABLE"] = "true" if stable else "false"
        # Enable direct swap to use reserve fallback
        os.environ["DIEM_VVV_DIRECT_SWAP_ENABLE"] = "true"

        # Rebuild aggregator to pick up new env
        aggregator = build_aggregator_from_env()

        quotes = aggregator.quote_all(probe_amount, route)

        print(f"  Results: {len(quotes)} quote(s)")
        for q in quotes:
            print(f"    Provider: {q.provider}")
            print(f"      Amount in:  {q.amount_in}")
            print(f"      Amount out: {q.amount_out}")
            if hasattr(q, "route") and q.route:
                print(f"      Route tokens: {q.route.tokens}")

        return quotes

    finally:
        # Restore original env
        if old_stable is not None:
            os.environ["AERODROME_STABLE"] = old_stable
        elif "AERODROME_STABLE" in os.environ:
            del os.environ["AERODROME_STABLE"]

        if old_diem_stable is not None:
            os.environ["DIEM_VVV_STABLE"] = old_diem_stable
        elif "DIEM_VVV_STABLE" in os.environ:
            del os.environ["DIEM_VVV_STABLE"]

        if old_direct is not None:
            os.environ["DIEM_VVV_DIRECT_SWAP_ENABLE"] = old_direct
        elif "DIEM_VVV_DIRECT_SWAP_ENABLE" in os.environ:
            del os.environ["DIEM_VVV_DIRECT_SWAP_ENABLE"]


def test_vvv_usdc_leg(
    aggregator: DexAggregator,
    vvv: str,
    usdc: str,
    probe_amount: int,
    fee: int = 3000,
) -> tuple[list, list]:
    """Test VVV→USDC leg with Uniswap V3."""
    print(f"Testing VVV→USDC leg (Uniswap V3, fee={fee})")
    print(f"  Probe amount: {probe_amount} (wei)")

    route = make_route([vvv, usdc], fees=[fee])
    print(f"  Route: {route.tokens}")
    print(f"  Fee tier: {fee} (0.3%)")

    # Test exact-in
    print("  Testing quote_all (exact-in)...")
    quotes_in = aggregator.quote_all(probe_amount, route)
    print(f"    Results: {len(quotes_in)} quote(s)")
    for q in quotes_in:
        print(f"      Provider: {q.provider}")
        print(f"        Amount in:  {q.amount_in}")
        print(f"        Amount out: {q.amount_out}")
        if hasattr(q, "route") and q.route:
            print(f"        Route tokens: {q.route.tokens}")
            if q.route.hops:
                print(f"        Fee: {q.route.hops[0].fee}")

    # Test exact-out (use a small USDC amount)
    # USDC has 6 decimals, so 1 USDC = 1_000_000
    probe_usdc = 1_000_000  # 1 USDC
    print(f"  Testing quote_all_exact_out (exact-out, {probe_usdc} wei = 1 USDC)...")
    quotes_out = aggregator.quote_all_exact_out(probe_usdc, route)
    print(f"    Results: {len(quotes_out)} quote(s)")
    for q in quotes_out:
        print(f"      Provider: {q.provider}")
        print(f"        Amount in:  {q.amount_in}")
        print(f"        Amount out: {q.amount_out}")
        if hasattr(q, "route") and q.route:
            print(f"        Route tokens: {q.route.tokens}")
            if q.route.hops:
                print(f"        Fee: {q.route.hops[0].fee}")

    return quotes_in, quotes_out


def main():
    """Run bridge leg validation."""
    try:
        diem, vvv, usdc = get_token_addresses()
        aggregator = build_aggregator_from_env()

        print_config(aggregator, diem, vvv, usdc)

        # Small probe amount: 10^15 wei = 0.001 DIEM (18 decimals)
        probe_amount = 10**15

        print("\n" + "=" * 80)
        print("LEG 1: DIEM → VVV (Aerodrome/V2)")
        print("=" * 80)

        # Test with stable=True
        print("\n[Test A] AERODROME_STABLE=true")
        quotes_stable_true = test_diem_vvv_leg(
            aggregator, diem, vvv, probe_amount, stable=True
        )

        # Test with stable=False
        print("\n[Test B] AERODROME_STABLE=false")
        quotes_stable_false = test_diem_vvv_leg(
            aggregator, diem, vvv, probe_amount, stable=False
        )

        # Summary for DIEM→VVV
        print("\n" + "-" * 80)
        print("DIEM→VVV Summary:")
        print(f"  Stable=True:  {len(quotes_stable_true)} quote(s)")
        print(f"  Stable=False: {len(quotes_stable_false)} quote(s)")

        if quotes_stable_true and not quotes_stable_false:
            print("  ✓ Only stable=True works")
        elif quotes_stable_false and not quotes_stable_true:
            print("  ✓ Only stable=False works")
        elif quotes_stable_true and quotes_stable_false:
            print("  ✓ Both stable settings work")
        else:
            print("  ✗ Neither stable setting works")
        print("-" * 80)

        print("\n" + "=" * 80)
        print("LEG 2: VVV → USDC (Uniswap V3)")
        print("=" * 80)

        # Test VVV→USDC with fee 3000
        quotes_in, quotes_out = test_vvv_usdc_leg(
            aggregator, vvv, usdc, probe_amount, fee=3000
        )

        # Summary for VVV→USDC
        print("\n" + "-" * 80)
        print("VVV→USDC Summary:")
        print(f"  Exact-in quotes:  {len(quotes_in)}")
        print(f"  Exact-out quotes: {len(quotes_out)}")

        if quotes_in:
            print("  ✓ Exact-in quoting works")
        else:
            print("  ✗ Exact-in quoting failed")

        if quotes_out:
            print("  ✓ Exact-out quoting works")
        else:
            print("  ✗ Exact-out quoting failed")
        print("-" * 80)

        print("\n" + "=" * 80)
        print("VALIDATION COMPLETE")
        print("=" * 80)

        # Exit with error if critical tests failed
        if not quotes_stable_true and not quotes_stable_false:
            print("\nERROR: DIEM→VVV leg failed for both stable settings")
            sys.exit(1)
        if not quotes_in:
            print("\nERROR: VVV→USDC exact-in quoting failed")
            sys.exit(1)
        if not quotes_out:
            print("\nWARNING: VVV→USDC exact-out quoting failed (may be expected)")

        print("\n✓ All critical validations passed")

    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
