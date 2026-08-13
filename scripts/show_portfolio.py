#!/usr/bin/env python3
"""Display wallet portfolio balances with USD valuations.

Loads environment variables from .env, .env.docker, and docker/.env.local
in the same order as the CLI, ensuring correct ETH_PRIVATE_KEY resolution.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Add repo root to path for imports
REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# Load environment files in CLI order
def _load_env_files():
    """Load environment files in the same order as apps/cli/main.py."""
    docker_env = REPO_ROOT / ".env.docker"
    local_env = REPO_ROOT / "docker" / ".env.local"

    try:
        from libs.env import load_dotenv_if_present
    except Exception:
        try:
            from dotenv import load_dotenv
        except Exception:
            print(
                "Warning: python-dotenv not available, using os.environ only",
                file=sys.stderr,
            )
            return

        load_dotenv(dotenv_path=str(REPO_ROOT / ".env"), override=False)
        if docker_env.exists():
            load_dotenv(dotenv_path=str(docker_env), override=True)
        if local_env.exists():
            load_dotenv(dotenv_path=str(local_env), override=True)
        return

    load_dotenv_if_present(path=str(REPO_ROOT / ".env"), override=False)
    if docker_env.exists():
        load_dotenv_if_present(path=str(docker_env), override=True)
    if local_env.exists():
        load_dotenv_if_present(path=str(local_env), override=True)


def _find_env_source(var_name: str) -> tuple[str, bool]:
    """Determine which env file contains a variable (best effort)."""
    docker_env = REPO_ROOT / ".env.docker"
    local_env = REPO_ROOT / "docker" / ".env.local"
    base_env = REPO_ROOT / ".env"

    # Check in reverse order (last loaded wins)
    if local_env.exists():
        try:
            with local_env.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{var_name}="):
                        return str(local_env), True
        except Exception:
            pass

    if docker_env.exists():
        try:
            with docker_env.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{var_name}="):
                        return str(docker_env), True
        except Exception:
            pass

    if base_env.exists():
        try:
            with base_env.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{var_name}="):
                        return str(base_env), True
        except Exception:
            pass

    return "environment", False


def _format_units(units: int, decimals: int) -> str:
    """Format token units with appropriate precision."""
    divisor = 10.0**decimals
    value = float(units) / divisor

    if decimals <= 6:
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    if decimals <= 18:
        return f"{value:,.9f}".rstrip("0").rstrip(".")
    return f"{value:.18e}"


def _mask_private_key(pk: str | None) -> str:
    """Mask private key for display."""
    if not pk:
        return "<not set>"
    pk_str = str(pk).strip()
    if len(pk_str) <= 10:
        return "<invalid>"
    return f"{pk_str[:6]}...{pk_str[-4]}"


def main():
    """Display portfolio balances."""
    print("=" * 80)
    print("Venice Capacity Broker - Wallet Portfolio")
    print("=" * 80)
    print()

    # Load environment
    _load_env_files()

    # Show environment source for ETH_PRIVATE_KEY
    eth_pk = os.getenv("ETH_PRIVATE_KEY")
    if eth_pk:
        env_file, found = _find_env_source("ETH_PRIVATE_KEY")
        print(f"ETH_PRIVATE_KEY source: {env_file}")
        print(f"ETH_PRIVATE_KEY value: {_mask_private_key(eth_pk)}")
    else:
        print("⚠️  ETH_PRIVATE_KEY: <not set>")
        print("   Set ETH_PRIVATE_KEY in .env, .env.docker, or docker/.env.local")
    print()

    # Initialize portfolio service
    try:
        from services.portfolio.inventory import PortfolioInventory
    except ImportError as exc:
        print(f"❌ Failed to import PortfolioInventory: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        from services.wallet.provider import get_default_provider
    except ImportError:
        get_default_provider = None

    # Get wallet address from private key
    wallet_address = None
    if eth_pk:
        try:
            from eth_account import Account

            account = Account.from_key(eth_pk)
            wallet_address = account.address
            print(f"Wallet Address: {wallet_address}")
        except ImportError:
            print(
                "⚠️  eth_account not available, cannot derive address from private key",
                file=sys.stderr,
            )
        except Exception as exc:
            print(
                f"⚠️  Failed to derive address from ETH_PRIVATE_KEY: {exc}",
                file=sys.stderr,
            )
            print("   Will attempt to use wallet provider...", file=sys.stderr)

    # Try wallet provider as fallback
    if not wallet_address and get_default_provider:
        try:
            provider = get_default_provider()
            wallet_address = provider.address
            print(f"Wallet Address (from provider): {wallet_address}")
        except Exception as exc:
            print(
                f"⚠️  Failed to get wallet address from provider: {exc}", file=sys.stderr
            )

    if not wallet_address:
        print("❌ Failed to get wallet address", file=sys.stderr)
        print(
            "   Ensure ETH_PRIVATE_KEY is set and valid, or install eth_account",
            file=sys.stderr,
        )
        sys.exit(1)

    print()
    print("-" * 80)
    print("Fetching balances...")
    print("-" * 80)
    print()

    # Fetch portfolio snapshot with explicit wallet address
    print(f"DEBUG: Fetching snapshot for address {wallet_address}")

    # Explicitly check configured token addresses
    usd_addr = (
        os.getenv("QUOTE_TOKEN_ADDRESS")
        or os.getenv("USDC_TOKEN_ADDRESS")
        or os.getenv("USDC_ADDRESS")
    )
    print(f"DEBUG: Configured USDC address: {usd_addr}")

    inventory = PortfolioInventory(wallet_address=wallet_address)
    snapshot = inventory.snapshot(include_eth=True)

    if snapshot.errors:
        print("⚠️  Warnings/Errors:")
        for error in snapshot.errors:
            print(f"   - {error}")
        print()

    if not snapshot.address:
        print("❌ Failed to resolve wallet address", file=sys.stderr)
        sys.exit(1)

    # Display balances
    balances = snapshot.balances or {}
    per_asset_usd = snapshot.per_asset_usd or {}

    if not balances:
        print("No balances found (wallet may be empty or tokens not configured)")
        sys.exit(0)

    # Sort assets: ETH first, then by USD value descending
    asset_items = []
    print(f"DEBUG: Balances: {balances}")
    for symbol, balance_info in balances.items():
        if not isinstance(balance_info, dict):
            continue

        # ETH is stored as "wei", other tokens as "units"
        if symbol == "ETH":
            units = balance_info.get("wei")
            decimals = 18
        else:
            units = balance_info.get("units")
            decimals = balance_info.get("decimals", 18)

        if units is None:
            continue
        try:
            units_int = int(units)
        except (TypeError, ValueError):
            continue

        # Skip zero balances (unless explicitly showing them)
        if units_int == 0:
            continue

        usd_value = per_asset_usd.get(symbol, 0.0)
        asset_items.append((symbol, units_int, decimals, usd_value))

    # Sort: ETH first, then by USD value descending
    def sort_key(item):
        symbol, _, _, usd_val = item
        if symbol == "ETH":
            return (-1, 0)  # Always first
        return (0, -usd_val)  # Then by USD value descending

    asset_items.sort(key=sort_key)

    # Display table
    print(f"{'Asset':<10} {'Balance':<30} {'USD Value':<15} {'Token Address':<45}")
    print("-" * 100)

    total_usd = 0.0
    for symbol, units, decimals, usd_value in asset_items:
        balance_str = _format_units(units, decimals)
        usd_str = f"${usd_value:,.2f}" if usd_value > 0 else "-"

        # Token address (ETH doesn't have one)
        if symbol == "ETH":
            token_addr = "Native"
        else:
            token_addr = balances[symbol].get("token_address", "")
            if token_addr:
                token_addr = f"{token_addr[:10]}...{token_addr[-8:]}"
            else:
                token_addr = "N/A"

        print(f"{symbol:<10} {balance_str:<30} {usd_str:<15} {token_addr:<45}")
        total_usd += usd_value

    print("-" * 100)
    print(f"{'TOTAL':<10} {'':<30} ${total_usd:,.2f}")
    print()

    # Summary
    print("Summary:")
    print(f"  • Total Portfolio Value: ${total_usd:,.2f}")
    print(f"  • Assets: {len(asset_items)}")

    # Show individual asset balances
    eth_info = balances.get("ETH", {})
    eth_wei = eth_info.get("wei", 0) if isinstance(eth_info, dict) else 0
    eth_balance_usd = per_asset_usd.get("ETH", 0.0)
    usdc_balance = per_asset_usd.get("USDC", 0.0)
    diem_balance = per_asset_usd.get("DIEM", 0.0)
    vvv_balance = per_asset_usd.get("VVV", 0.0)

    if eth_wei > 0:
        eth_balance_str = _format_units(eth_wei, 18)
        if eth_balance_usd > 0:
            print(f"  • ETH: {eth_balance_str} (${eth_balance_usd:,.2f})")
        else:
            print(f"  • ETH: {eth_balance_str} (price unavailable)")
    if usdc_balance > 0:
        print(f"  • USDC: ${usdc_balance:,.2f}")
    if diem_balance > 0:
        print(f"  • DIEM: ${diem_balance:,.2f}")
    if vvv_balance > 0:
        print(f"  • VVV: ${vvv_balance:,.2f}")

    print()
    print("=" * 80)


if __name__ == "__main__":
    main()
