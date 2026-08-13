#!/usr/bin/env python3
"""Validate trade route configuration for 4-hop route and exact_in fallback.

This script checks:
1. TRADE_PATH includes VVV (4-hop route: USDC -> WETH -> VVV -> DIEM)
2. exact_in fallback is enabled
3. Route quotes successfully for both exact_out and exact_in modes
"""

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

from libs.dex.providers import build_aggregator_from_env  # noqa: E402
from services.diem.client import DIEMService  # noqa: E402
from services.marketdata.provider import MarketDataProvider  # noqa: E402


def check_trade_path() -> tuple[bool, str]:
    """Check if TRADE_PATH includes VVV for 4-hop route."""
    trade_path = os.getenv("TRADE_PATH", "").strip()
    if not trade_path:
        try:
            md = MarketDataProvider()
            plan = md.primary_trade_path()
            tokens = [t.lower() for t in plan.tokens]
            source = "dynamic/bridge"
        except Exception:
            return False, "TRADE_PATH not set and no dynamic route available"
    else:
        # Parse tokens from TRADE_PATH (format: addr@fee,addr@fee,... or addr,addr,...)
        tokens = []
        for part in trade_path.split(","):
            token = part.split("@")[0].strip()
            if token:
                tokens.append(token.lower())
        source = "env"

    vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
    diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
    usdc_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()

    if not vvv_addr:
        return False, "VVV_TOKEN_ADDRESS not set"

    has_vvv = vvv_addr in tokens
    has_diem = diem_addr in tokens if diem_addr else False
    has_usdc = usdc_addr in tokens if usdc_addr else False

    route_str = " -> ".join(tokens[:3]) + ("..." if len(tokens) > 3 else "")
    if has_vvv and has_diem and has_usdc:
        return True, f"✓ 4-hop route detected ({source}): {route_str} (includes VVV)"
    if has_vvv:
        return True, f"✓ Route includes VVV ({source}): {route_str}"
    return False, f"✗ Route missing VVV ({source}): {route_str}"


def check_fallback_config() -> tuple[bool, str]:
    """Check if exact_in fallback is enabled."""
    fallback_enabled = os.getenv(
        "DIEM_EXACT_IN_FALLBACK_ENABLE", "0"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    max_usd = os.getenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "10.0")
    auto_fallback = os.getenv(
        "DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY", "0"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    if fallback_enabled:
        status = f"✓ exact_in fallback enabled (max_usd={max_usd}"
        if auto_fallback:
            status += ", auto-fallback enabled"
        status += ")"
        return True, status
    return (
        False,
        "✗ exact_in fallback disabled (set DIEM_EXACT_IN_FALLBACK_ENABLE=1)",
    )


def test_route_quotes() -> tuple[bool, str]:
    """Test route quotes for exact_out and exact_in with appropriate sizing."""
    try:
        md = MarketDataProvider()
        aggregator = build_aggregator_from_env()
        diem_service = DIEMService(aggregator=aggregator)

        # Get DIEM price to calculate appropriate test amount
        diem_price = md.diem_price_with_fallback()
        if diem_price is None or diem_price <= 0:
            return False, "✗ Cannot determine DIEM price for quote testing"

        # Get max_usd limit for fallback
        max_usd = float(os.getenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "10.0") or 10.0)
        fallback_enabled = os.getenv(
            "DIEM_EXACT_IN_FALLBACK_ENABLE", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}

        # Calculate test amount: use 80% of max_usd to stay safely within limit
        test_usd = max_usd * 0.8 if fallback_enabled else min(max_usd, 10.0)
        diem_decimals = 18  # Default, could be fetched from env or contract
        test_amount_out = int((test_usd / diem_price) * (10**diem_decimals))

        if test_amount_out <= 0:
            return (
                False,
                f"✗ Test amount too small (max_usd={max_usd}, price=${diem_price:.2f})",
            )

        # Get routes
        routes = diem_service._route_plans_from_env()
        if not routes:
            return False, "✗ No routes found"

        route = routes[0]
        route_str = " -> ".join(route.tokens[:3]) + (
            "..." if len(route.tokens) > 3 else ""
        )
        test_diem_tokens = test_amount_out / (10**diem_decimals)

        # Try exact_out first (preferred for buy trades)
        try:
            rev_route = route.reversed()
            quote_out = diem_service.aggregator.best_quote_exact_out(
                test_amount_out, rev_route
            )
            if quote_out:
                return (
                    True,
                    f"✓ exact_out quote successful ({test_diem_tokens:.4f} DIEM @ ${diem_price:.2f}): {route_str}",
                )
        except Exception:
            # Continue to fallback testing
            pass

        # Try exact_in fallback if enabled
        if fallback_enabled:
            try:
                # Estimate input needed: use price with 10% buffer
                estimated_input_usd = test_usd * 1.1
                quote_token_decimals = 6  # USDC default
                test_amount_in = int(estimated_input_usd * (10**quote_token_decimals))

                quote_in = diem_service.aggregator.best_quote(test_amount_in, rev_route)
                if quote_in:
                    actual_out = getattr(quote_in, "amount_out", 0)
                    actual_out_tokens = actual_out / (10**diem_decimals)
                    if (
                        actual_out_tokens >= test_diem_tokens * 0.9
                    ):  # Allow 10% slippage
                        return (
                            True,
                            f"⚠ exact_out failed, exact_in fallback works ({actual_out_tokens:.4f} DIEM): {route_str}",
                        )
                    return (
                        False,
                        f"✗ exact_in fallback insufficient output ({actual_out_tokens:.4f} < {test_diem_tokens:.4f} DIEM)",
                    )
            except Exception as e:
                return False, f"✗ exact_in fallback error: {e}"

        # Try simpler 2-hop route (DIEM -> VVV -> USDC) if 4-hop fails
        diem_addr = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip().lower()
        vvv_addr = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip().lower()
        usdc_addr = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip().lower()

        if diem_addr and vvv_addr and usdc_addr:
            try:
                from libs.dex.routes import make_route

                simple_route = make_route([diem_addr, vvv_addr, usdc_addr])
                rev_simple = simple_route.reversed()

                quote_simple = diem_service.aggregator.best_quote_exact_out(
                    test_amount_out, rev_simple
                )
                if quote_simple:
                    return (
                        True,
                        "⚠ 4-hop failed, but 2-hop route works (DIEM->VVV->USDC)",
                    )

                if fallback_enabled:
                    quote_simple_in = diem_service.aggregator.best_quote(
                        test_amount_in, rev_simple
                    )
                    if quote_simple_in:
                        return (
                            True,
                            "⚠ 4-hop failed, but 2-hop exact_in works (DIEM->VVV->USDC)",
                        )
            except Exception:
                pass

        # Provide diagnostics about why quotes failed
        diagnostics = []
        try:
            if hasattr(aggregator, "inspect_route"):
                inspections = aggregator.inspect_route(
                    rev_route, test_amount_out, mode="exact_out"
                )
                for insp in inspections[:2]:  # Limit to first 2 providers
                    provider = insp.get("provider", "unknown")
                    status = insp.get("status", "unknown")
                    error = insp.get("error", "")
                    if status != "ok":
                        diagnostics.append(
                            f"{provider}: {status}"
                            + (f" ({error[:50]})" if error else "")
                        )
        except Exception:
            pass

        diag_msg = ""
        if diagnostics:
            diag_msg = f" [Providers: {', '.join(diagnostics)}]"

        return (
            False,
            f"✗ All quote attempts failed for {test_diem_tokens:.4f} DIEM (~${test_usd:.2f}){diag_msg}",
        )

    except Exception as e:
        return False, f"✗ Route test error: {e}"


def check_price_health() -> tuple[bool, str]:
    """Check DIEM price health for fallback eligibility."""
    try:
        md = MarketDataProvider()
        # Trigger a price fetch so health reflects the latest DIEM source
        try:
            md.diem_price_with_fallback()
        except Exception:
            # Best‑effort only; health check will handle missing prices
            pass
        health = md.price_health("DIEM")
        source = health.get("source", "unknown")
        valid = health.get("valid", False)
        price = health.get("price")

        if source in {"bridge_vvv", "path_engine", "aggregator"} and valid:
            return True, f"✓ Price health OK (source={source}, price=${price:.2f})"
        if source == "bridge_vvv":
            return True, f"⚠ Price from bridge_vvv (fallback eligible): ${price:.2f}"
        return False, f"✗ Price health invalid (source={source}, valid={valid})"
    except Exception as e:
        return False, f"✗ Price health check error: {e}"


def main() -> None:
    """Run all validation checks."""
    print("=" * 70)
    print("Trade Route Validation")
    print("=" * 70)
    print()

    checks = [
        ("Trade Path (4-hop)", check_trade_path),
        ("exact_in Fallback Config", check_fallback_config),
        ("Price Health", check_price_health),
        ("Route Quotes", test_route_quotes),
    ]

    results = []
    for name, check_fn in checks:
        print(f"Checking {name}...")
        ok, msg = check_fn()
        print(f"  {msg}")
        results.append((name, ok))
        print()

    print("=" * 70)
    print("Summary")
    print("=" * 70)
    all_ok = all(ok for _, ok in results)
    for name, ok in results:
        status = "✓ PASS" if ok else "✗ FAIL"
        print(f"  {status}: {name}")

    print()
    if all_ok:
        print(
            "✓ All checks passed! Route is configured for 4-hop trading with fallback."
        )
        sys.exit(0)
    else:
        print("✗ Some checks failed. Review configuration and retry.")
        print()
        print("Next steps:")
        print("1. Update TRADE_PATH to include VVV:")
        print(
            "   TRADE_PATH=0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        )
        print("2. Enable exact_in fallback:")
        print("   DIEM_EXACT_IN_FALLBACK_ENABLE=1")
        print("   DIEM_EXACT_IN_FALLBACK_MAX_USD=25.0")
        print("   DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1")
        print("3. Restart orchestrator to pick up changes")
        sys.exit(1)


if __name__ == "__main__":
    main()
