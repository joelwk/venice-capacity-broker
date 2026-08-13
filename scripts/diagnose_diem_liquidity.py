#!/usr/bin/env python3
"""
Diagnostic script to investigate DIEM liquidity and pricing issues.
Queries on-chain reserves and compares with system pricing.
"""

import os
import sys
import time
from pathlib import Path

from web3 import Web3

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

# Add parent to path
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _load_env_file() -> None:
    """Load local env file when python-dotenv is available."""
    env_path = REPO_ROOT / ".env"
    if load_dotenv is None:
        print("[WARN] python-dotenv not installed, using shell environment only")
        return

    if env_path.exists():
        load_dotenv(env_path)
        print(f"[OK] Loaded environment from {env_path}")
    else:
        print(f"[WARN] No .env file found at {env_path}")
        print("       Attempting to use environment variables from shell/secrets...")


_load_env_file()


def check_pair_reserves(rpc_url: str, pair_address: str, name: str):
    """Query Uniswap V2 pair reserves"""
    print(f"\n{'=' * 60}")
    print(f"Checking {name}: {pair_address}")
    print(f"{'=' * 60}")

    w3 = Web3(Web3.HTTPProvider(rpc_url))
    pair_addr = Web3.to_checksum_address(pair_address)

    # Add delay to avoid rate limits
    time.sleep(1)

    # getReserves() selector
    try:
        reserves_data = w3.eth.call(
            {
                "to": pair_addr,
                "data": "0x0902f1ac",  # getReserves()
            }
        )
    except Exception as e:
        print(f"[ERROR] Failed to fetch reserves: {e}")
        print("[INFO] Try using a different RPC or wait a moment for rate limits")
        return None

    if len(reserves_data) < 96:
        print(f"[ERROR] No reserves data returned (len={len(reserves_data)})")
        return None

    reserve0 = int.from_bytes(reserves_data[0:32], "big")
    reserve1 = int.from_bytes(reserves_data[32:64], "big")
    timestamp = int.from_bytes(reserves_data[64:96], "big")

    # token0() selector
    token0_data = w3.eth.call({"to": pair_addr, "data": "0x0dfe1681"})
    token0 = "0x" + token0_data[-20:].hex()

    # token1() selector
    token1_data = w3.eth.call({"to": pair_addr, "data": "0xd21220a7"})
    token1 = "0x" + token1_data[-20:].hex()

    print(f"Token0: {token0}")
    print(f"Token1: {token1}")
    print(f"Reserve0: {reserve0:,}")
    print(f"Reserve1: {reserve1:,}")
    print(f"Last Update: {timestamp}")

    # Get decimals for each token
    def get_decimals(token_addr):
        try:
            dec_data = w3.eth.call(
                {
                    "to": Web3.to_checksum_address(token_addr),
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

    print(f"Reserve0 (decimal): {r0_decimal:,.6f}")
    print(f"Reserve1 (decimal): {r1_decimal:,.6f}")

    if reserve0 > 0 and reserve1 > 0:
        price_0_in_1 = r1_decimal / r0_decimal
        price_1_in_0 = r0_decimal / r1_decimal
        print(f"Price (token1/token0): {price_0_in_1:,.8f}")
        print(f"Price (token0/token1): {price_1_in_0:,.8f}")

        # Check TVL
        tvl_token0 = r0_decimal
        tvl_token1 = r1_decimal
        print(f"TVL Token0: {tvl_token0:,.2f}")
        print(f"TVL Token1: {tvl_token1:,.2f}")

        if r0_decimal < 0.01 or r1_decimal < 0.01:
            print("[WARN] Dust liquidity detected!")
    else:
        print("[ERROR] EMPTY POOL - No liquidity!")

    return {
        "token0": token0,
        "token1": token1,
        "reserve0": reserve0,
        "reserve1": reserve1,
        "reserve0_decimal": r0_decimal,
        "reserve1_decimal": r1_decimal,
        "decimals0": dec0,
        "decimals1": dec1,
    }


def calculate_bridge_price(
    diem_vvv_data, vvv_usdc_data, diem_addr, vvv_addr, usdc_addr
):
    """Replicate bridge_vvv_price logic"""
    print(f"\n{'=' * 60}")
    print("Calculating Bridge Price (DIEM -> VVV -> USDC)")
    print(f"{'=' * 60}")

    if not diem_vvv_data or not vvv_usdc_data:
        print("[ERROR] Missing pair data")
        return None

    # Identify which reserve is which in DIEM/VVV pair
    if diem_vvv_data["token0"].lower() == diem_addr.lower():
        diem_reserve = diem_vvv_data["reserve0_decimal"]
        vvv_in_diem_pair = diem_vvv_data["reserve1_decimal"]
    else:
        diem_reserve = diem_vvv_data["reserve1_decimal"]
        vvv_in_diem_pair = diem_vvv_data["reserve0_decimal"]

    print(f"DIEM reserve in DIEM/VVV pair: {diem_reserve:,.6f}")
    print(f"VVV reserve in DIEM/VVV pair: {vvv_in_diem_pair:,.6f}")

    # Identify which reserve is which in VVV/USDC pair
    if vvv_usdc_data["token0"].lower() == vvv_addr.lower():
        vvv_in_usdc_pair = vvv_usdc_data["reserve0_decimal"]
        usdc_reserve = vvv_usdc_data["reserve1_decimal"]
    else:
        vvv_in_usdc_pair = vvv_usdc_data["reserve1_decimal"]
        usdc_reserve = vvv_usdc_data["reserve0_decimal"]

    print(f"VVV reserve in VVV/USDC pair: {vvv_in_usdc_pair:,.6f}")
    print(f"USDC reserve in VVV/USDC pair: {usdc_reserve:,.6f}")

    # Calculate prices
    if diem_reserve > 0:
        diem_per_vvv = diem_reserve / vvv_in_diem_pair if vvv_in_diem_pair > 0 else 0
        print(f"DIEM per VVV: {diem_per_vvv:,.8f}")
    else:
        diem_per_vvv = 0

    if vvv_in_usdc_pair > 0:
        vvv_price_usd = usdc_reserve / vvv_in_usdc_pair
        print(f"VVV price (USD): ${vvv_price_usd:,.6f}")
    else:
        vvv_price_usd = 0

    if diem_per_vvv > 0 and vvv_price_usd > 0:
        # DIEM price = (VVV/DIEM) * (USD/VVV) = USD/DIEM
        diem_price_usd = (1 / diem_per_vvv) * vvv_price_usd
        print(f"\n[OK] DIEM price (calculated): ${diem_price_usd:,.8f}")
        return diem_price_usd
    print("\n[ERROR] Cannot calculate price (insufficient liquidity)")
    return None


def main():
    print("DIEM Liquidity Diagnostic Tool")
    print("=" * 60)

    # Load config - try multiple RPC resolution methods
    rpc_url = None
    try:
        from libs.agentkit_ext.web3_utils import resolve_rpc_url

        rpc_url = resolve_rpc_url(validate=False)
        print("[OK] Resolved RPC URL via web3_utils")
    except Exception as e:
        print(f"[WARN] Could not use resolve_rpc_url: {e}")
        rpc_url = os.getenv("BASE_RPC_URL") or os.getenv("RPC_URL")

    if not rpc_url:
        print("[ERROR] No RPC URL found. Set BASE_RPC_URL or RPC_URL in .env")
        print("\nTry:")
        print("  export BASE_RPC_URL='https://mainnet.base.org'")
        print("  # or")
        print("  export BASE_RPC_URL='https://base.publicnode.com'")
        return 1

    diem_vvv_pair = os.getenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )
    vvv_usdc_pool = os.getenv(
        "VVV_USDC_POOL_ADDRESS", "0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530"
    )

    diem_addr = os.getenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    vvv_addr = os.getenv(
        "VVV_TOKEN_ADDRESS", "0xacfE4f0FdCbC9a67db2ED297A94e332d55a3c6B5"
    )
    usdc_addr = os.getenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    )

    print(f"RPC URL: {rpc_url}")
    print(f"DIEM Token: {diem_addr}")
    print(f"VVV Token: {vvv_addr}")
    print(f"USDC Token: {usdc_addr}")

    # Check DIEM/VVV pair
    diem_vvv_data = check_pair_reserves(rpc_url, diem_vvv_pair, "DIEM/VVV Pair")

    # Check VVV/USDC pool
    vvv_usdc_data = check_pair_reserves(rpc_url, vvv_usdc_pool, "VVV/USDC Pool")

    # Calculate bridge price
    if diem_vvv_data and vvv_usdc_data:
        bridge_price = calculate_bridge_price(
            diem_vvv_data, vvv_usdc_data, diem_addr, vvv_addr, usdc_addr
        )

        if bridge_price:
            print(f"\n{'=' * 60}")
            print("DIAGNOSIS")
            print(f"{'=' * 60}")
            print(f"Bridge Price: ${bridge_price:,.8f}")
            print("External Reference: $121.88 (from logs)")
            print(f"Discrepancy: {abs(bridge_price - 121.88) / 121.88 * 100:.2f}%")

            if bridge_price < 0.01:
                print("\n[WARN] ISSUE: Bridge price is extremely low!")
                print("       This suggests either:")
                print("       1. DIEM/VVV pair has dust/incorrect liquidity")
                print("       2. Token ordering is reversed")
                print("       3. Pair is not actively used for trading")
            elif bridge_price < 1.0:
                print(
                    "\n[WARN] ISSUE: Bridge price < $1, but DIEM should be ~$1/day value"
                )
                print("       Market may not reflect fundamental value")
            elif abs(bridge_price - 121.88) / 121.88 > 0.15:
                print("\n[WARN] ISSUE: Large discrepancy with external reference")
                print("       Either bridge calculation or external data is wrong")

    print(f"\n{'=' * 60}")
    print("RECOMMENDATIONS")
    print(f"{'=' * 60}")
    print("1. If pairs have dust liquidity: Bootstrap with meaningful TVL")
    print("2. If external price is wrong: Remove/correct external price source")
    print("3. If bridge calc is wrong: Debug token ordering or decimals")
    print("4. Run: python apps/cli/main.py market:pools:list --token DIEM")
    print("5. Check BaseScan for actual trading activity")

    return 0


if __name__ == "__main__":
    sys.exit(main())
