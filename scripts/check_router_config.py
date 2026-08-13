#!/usr/bin/env python3
"""Quick check of router configuration vs known Base addresses."""

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

print("=" * 70)
print("Current Router Configuration")
print("=" * 70)
print()

# Known correct addresses for Base mainnet
KNOWN = {
    "uniswap_v2": "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
    "uniswap_v3_router": "0x2626664c2603336e57b271c5c0b26f421741e481",
    "uniswap_v3_quoter": "0x3d4e44eb1374240ce5f1b871ab261cd16335b76a",
    "aerodrome_slipstream": "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5",
    "aerodrome_classic": "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43",
}

# Check what's configured
uniswap_v2 = os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or os.getenv("ROUTER_ADDRESS")
uniswap_v3_router = os.getenv("UNISWAP_V3_ROUTER_ADDRESS")
uniswap_v3_quoter = os.getenv("UNISWAP_V3_QUOTER_ADDRESS")
aerodrome = os.getenv("AERODROME_ROUTER_ADDRESS")
dex_providers = os.getenv("DEX_PROVIDERS", "")

print("Uniswap V2 Router:")
if uniswap_v2:
    match = (
        "✓ MATCH" if uniswap_v2.lower() == KNOWN["uniswap_v2"].lower() else "✗ MISMATCH"
    )
    print(f"  Configured: {uniswap_v2}")
    print(f"  Expected:   {KNOWN['uniswap_v2']}")
    print(f"  Status:    {match}")
else:
    print("  ✗ NOT SET")
print()

print("Uniswap V3 Router:")
if uniswap_v3_router:
    match = (
        "✓ MATCH"
        if uniswap_v3_router.lower() == KNOWN["uniswap_v3_router"].lower()
        else "✗ MISMATCH"
    )
    print(f"  Configured: {uniswap_v3_router}")
    print(f"  Expected:   {KNOWN['uniswap_v3_router']}")
    print(f"  Status:    {match}")
else:
    print("  ✗ NOT SET (required for V3 pools)")
print()

print("Uniswap V3 Quoter:")
if uniswap_v3_quoter:
    match = (
        "✓ MATCH"
        if uniswap_v3_quoter.lower() == KNOWN["uniswap_v3_quoter"].lower()
        else "✗ MISMATCH"
    )
    print(f"  Configured: {uniswap_v3_quoter}")
    print(f"  Expected:   {KNOWN['uniswap_v3_quoter']}")
    print(f"  Status:    {match}")
else:
    print("  ✗ NOT SET (required for V3 quotes)")
print()

print("Aerodrome Router:")
if aerodrome:
    aerodrome_lower = aerodrome.lower()
    if aerodrome_lower == KNOWN["aerodrome_slipstream"].lower():
        print(f"  Configured: {aerodrome}")
        print("  Type:       ✓ Slipstream (new)")
        print("  Status:     ✓ MATCH")
    elif aerodrome_lower == KNOWN["aerodrome_classic"].lower():
        print(f"  Configured: {aerodrome}")
        print("  Type:       ⚠ Classic (old)")
        print("  Status:     ⚠ Using Classic router")
        print(
            f"  Note:       If pools are on Slipstream, use: {KNOWN['aerodrome_slipstream']}"
        )
    else:
        print(f"  Configured: {aerodrome}")
        print("  Status:     ✗ UNKNOWN ADDRESS")
        print(f"  Expected:   Slipstream: {KNOWN['aerodrome_slipstream']}")
        print(f"              Classic:    {KNOWN['aerodrome_classic']}")
else:
    print("  ✗ NOT SET")
print()

print("DEX_PROVIDERS:")
print(f"  {dex_providers}")
print()

print("=" * 70)
print("Recommendations")
print("=" * 70)
print()

issues = []
if not uniswap_v2:
    issues.append("Set UNISWAP_V2_ROUTER_ADDRESS")
if not uniswap_v3_router:
    issues.append("Set UNISWAP_V3_ROUTER_ADDRESS (if using V3 pools)")
if not uniswap_v3_quoter:
    issues.append("Set UNISWAP_V3_QUOTER_ADDRESS (if using V3 pools)")
if not aerodrome:
    issues.append("Set AERODROME_ROUTER_ADDRESS")
elif aerodrome.lower() == KNOWN["aerodrome_classic"].lower():
    issues.append(
        "Consider switching to Aerodrome Slipstream router if pools support it"
    )

if issues:
    print("Issues found:")
    for issue in issues:
        print(f"  - {issue}")
    print()
    print("Add to your .env file:")
    print()
    if not uniswap_v2:
        print(f"UNISWAP_V2_ROUTER_ADDRESS={KNOWN['uniswap_v2']}")
    if not uniswap_v3_router:
        print(f"UNISWAP_V3_ROUTER_ADDRESS={KNOWN['uniswap_v3_router']}")
    if not uniswap_v3_quoter:
        print(f"UNISWAP_V3_QUOTER_ADDRESS={KNOWN['uniswap_v3_quoter']}")
    if not aerodrome:
        print(f"AERODROME_ROUTER_ADDRESS={KNOWN['aerodrome_slipstream']}")
    elif aerodrome.lower() == KNOWN["aerodrome_classic"].lower():
        print("# Try switching to Slipstream:")
        print(f"# AERODROME_ROUTER_ADDRESS={KNOWN['aerodrome_slipstream']}")
else:
    print("✓ All router addresses are configured")
    if aerodrome and aerodrome.lower() == KNOWN["aerodrome_classic"].lower():
        print("⚠ Using Aerodrome Classic - verify pools are on Classic, not Slipstream")
