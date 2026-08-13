#!/usr/bin/env python3
"""Check TRADE_PATH and recommend fee tiers for V3 hops."""

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

# Known pool configurations
VVV_USDC_POOL_FEE = int(os.getenv("VVV_USDC_POOL_FEE", "3000") or 3000)
UNISWAP_V3_DEFAULT_FEE = int(os.getenv("UNISWAP_V3_DEFAULT_FEE", "3000") or 3000)


def main():
    print("=" * 70)
    print("TRADE_PATH Fee Configuration Check")
    print("=" * 70)
    print()

    trade_path = os.getenv("TRADE_PATH", "")
    if not trade_path:
        print("✗ TRADE_PATH not set")
        return 1

    print(f"Current TRADE_PATH: {trade_path}")
    print()

    # Parse tokens
    tokens = [t.strip() for t in trade_path.split(",") if t.strip()]
    print(f"Tokens ({len(tokens)}):")
    for i, token in enumerate(tokens):
        print(f"  {i + 1}. {token}")
    print()

    # Check if fees are specified
    has_fees = any("@" in token for token in tokens)

    if has_fees:
        print("✓ Fees are specified in TRADE_PATH")
        print()
        print("Current format:")
        for i, token in enumerate(tokens):
            if "@" in token:
                addr, fee = token.split("@", 1)
                print(f"  {i + 1}. {addr} @ {fee}")
            else:
                print(f"  {i + 1}. {token}")
    else:
        print("⚠ No fees specified in TRADE_PATH")
        print()
        print("For Uniswap V3 pools, fees must be specified using @ syntax.")
        print()
        print("Your route appears to be:")
        print("  DIEM -> VVV -> WETH -> USDC")
        print()
        print("Pool types:")
        print("  - DIEM/VVV: V2 (Aerodrome) - no fee needed")
        print("  - VVV/WETH: V3 (Uniswap) - needs fee tier")
        print("  - WETH/USDC: V3 (Uniswap) - needs fee tier")
        print()
        print("Recommended TRADE_PATH:")

        # Build recommended path
        diem = tokens[0]
        vvv = tokens[1] if len(tokens) > 1 else "VVV_TOKEN_ADDRESS"

        # Fees apply to hops:
        # - Hop 1 (DIEM->VVV): V2, no fee
        # - Hop 2 (VVV->WETH): V3, fee 3000
        # - Hop 3 (WETH->USDC): V3, fee 3000
        #
        # The parser uses @ on the destination token of each hop
        # So: DIEM,VVV@3000,WETH@3000,USDC means:
        # - DIEM->VVV: fee from VVV = 3000 (WRONG - this is V2!)
        # - VVV->WETH: fee from WETH = 3000 (CORRECT)
        # - WETH->USDC: fee from USDC = None (WRONG - needs 3000)
        #
        # Actually, looking at the parser code more carefully:
        # It creates hop_fees list with len(tokens)-1 entries
        # Each segment can have @fee, and fees are assigned to hops sequentially
        # So: DIEM,VVV@3000,WETH@3000,USDC creates:
        # - hop_fees[0] = 3000 (from VVV@3000) -> DIEM->VVV hop
        # - hop_fees[1] = 3000 (from WETH@3000) -> VVV->WETH hop
        # - hop_fees[2] = None (from USDC) -> WETH->USDC hop
        #
        # But we need:
        # - hop_fees[0] = None (DIEM->VVV is V2)
        # - hop_fees[1] = 3000 (VVV->WETH is V3)
        # - hop_fees[2] = 3000 (WETH->USDC is V3)
        #
        # So the format should be: DIEM,VVV,WETH@3000,USDC@3000
        # This gives:
        # - hop_fees[0] = None (from VVV, no @)
        # - hop_fees[1] = 3000 (from WETH@3000)
        # - hop_fees[2] = 3000 (from USDC@3000)

        recommended = f"{diem},{vvv},0x4200000000000000000000000000000000000006@{UNISWAP_V3_DEFAULT_FEE},0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@{UNISWAP_V3_DEFAULT_FEE}"

        print(f"  {recommended}")
        print()
        print("Explanation:")
        print("  - DIEM -> VVV: V2 pool, no fee needed")
        print(f"  - VVV -> WETH: V3 pool, fee {UNISWAP_V3_DEFAULT_FEE}")
        print(f"  - WETH -> USDC: V3 pool, fee {UNISWAP_V3_DEFAULT_FEE}")
        print()
        print("Add to your .env file:")
        print(f"TRADE_PATH={recommended}")
        print()
        print("Note: If your actual token addresses differ, adjust accordingly.")
        print("      The @fee syntax applies the fee to the hop ending at that token.")

    print()
    print("=" * 70)
    print("Current Configuration")
    print("=" * 70)
    print(f"VVV_USDC_POOL_FEE: {VVV_USDC_POOL_FEE}")
    print(f"UNISWAP_V3_DEFAULT_FEE: {UNISWAP_V3_DEFAULT_FEE}")
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
