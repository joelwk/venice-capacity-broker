#!/usr/bin/env python3
"""Check which RPC endpoint is currently being used."""

import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

try:
    from web3 import Web3

    from libs.agentkit_ext.web3_utils import resolve_rpc_url, rpc_url_candidates
except ImportError as e:
    print(f"Error importing dependencies: {e}")
    print("Make sure you're running from the project root with dependencies installed")
    sys.exit(1)


def main():
    print("=" * 60)
    print("RPC Endpoint Configuration Check")
    print("=" * 60)
    print()

    # Check environment variables
    print("Environment Variables:")
    base_rpc_url = os.getenv("BASE_RPC_URL")
    base_rpc_urls = os.getenv("BASE_RPC_URLS")
    rpc_url = os.getenv("RPC_URL")
    rpc_urls = os.getenv("RPC_URLS")

    if base_rpc_url:
        print(f"  BASE_RPC_URL: {base_rpc_url}")
        # Mask API key for security
        if "alchemy" in base_rpc_url.lower():
            parts = base_rpc_url.split("/v2/")
            if len(parts) > 1:
                key_part = parts[1]
                masked = (
                    key_part[:8] + "..." + key_part[-4:]
                    if len(key_part) > 12
                    else "***"
                )
                print(f"    (masked): {parts[0]}/v2/{masked}")
    else:
        print("  BASE_RPC_URL: (not set)")

    if base_rpc_urls:
        print(f"  BASE_RPC_URLS: {base_rpc_urls[:100]}...")
    else:
        print("  BASE_RPC_URLS: (not set)")

    if rpc_url:
        print(f"  RPC_URL: {rpc_url}")
    else:
        print("  RPC_URL: (not set)")

    if rpc_urls:
        print(f"  RPC_URLS: {rpc_urls[:100]}...")
    else:
        print("  RPC_URLS: (not set)")

    print()
    print("RPC Candidates (in priority order):")
    try:
        candidates = rpc_url_candidates()
        for i, url in enumerate(candidates[:10], 1):  # Show first 10
            masked_url = url
            if "alchemy" in url.lower() and "/v2/" in url:
                parts = url.split("/v2/")
                if len(parts) > 1:
                    key_part = parts[1]
                    masked = (
                        key_part[:8] + "..." + key_part[-4:]
                        if len(key_part) > 12
                        else "***"
                    )
                    masked_url = f"{parts[0]}/v2/{masked}"
            marker = " <-- CURRENT" if i == 1 else ""
            print(f"  {i}. {masked_url}{marker}")
        if len(candidates) > 10:
            print(f"  ... and {len(candidates) - 10} more fallback endpoints")
    except Exception as e:
        print(f"  Error getting candidates: {e}")

    print()
    print("Currently Selected RPC:")
    try:
        selected = resolve_rpc_url(validate=False)
        masked_selected = selected
        if "alchemy" in selected.lower() and "/v2/" in selected:
            parts = selected.split("/v2/")
            if len(parts) > 1:
                key_part = parts[1]
                masked = (
                    key_part[:8] + "..." + key_part[-4:]
                    if len(key_part) > 12
                    else "***"
                )
                masked_selected = f"{parts[0]}/v2/{masked}"
        print(f"  {masked_selected}")

        # Test connectivity
        print()
        print("Testing connectivity...")
        try:
            w3 = Web3(Web3.HTTPProvider(selected))
            if w3.is_connected():
                chain_id = w3.eth.chain_id
                block_number = w3.eth.block_number
                print("  ✅ Connected successfully")
                print(f"  Chain ID: {chain_id}")
                print(f"  Latest block: {block_number}")
                if chain_id == 8453:
                    print("  ✅ Correct chain (Base mainnet)")
                else:
                    print(f"  ⚠️  Warning: Expected chain ID 8453, got {chain_id}")
            else:
                print("  ❌ Failed to connect")
        except Exception as e:
            print(f"  ❌ Connection test failed: {e}")

    except Exception as e:
        print(f"  Error resolving RPC: {e}")

    print()
    print("=" * 60)
    print("Recommendations:")
    print("=" * 60)
    if not base_rpc_url and not base_rpc_urls:
        print("⚠️  No BASE_RPC_URL or BASE_RPC_URLS set!")
        print("   The system is using public fallback RPCs.")
        print()
        print("To use your Alchemy endpoint, set:")
        print("  BASE_RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_API_KEY")
        print()
        print("Or for multiple endpoints:")
        print(
            "  BASE_RPC_URLS=https://base-mainnet.g.alchemy.com/v2/YOUR_API_KEY,https://..."
        )
    elif (
        "alchemy" in (base_rpc_url or "").lower()
        or "alchemy" in (base_rpc_urls or "").lower()
    ):
        if "demo" in (base_rpc_url or "").lower():
            print("⚠️  Using Alchemy demo endpoint (rate-limited)")
            print("   Set BASE_RPC_URL to your paid Alchemy endpoint")
        else:
            print("✅ Alchemy endpoint configured")
            if not selected.startswith("https://base-mainnet.g.alchemy.com"):
                print("⚠️  But currently using a different endpoint")
                print("   Check RPC health tracker - may have switched due to failures")
    else:
        print("ℹ️  Using non-Alchemy endpoint")
        print("   To use Alchemy, set BASE_RPC_URL to your endpoint")


if __name__ == "__main__":
    main()
