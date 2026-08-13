"""Test quote sizes from $1 to $1000 to find execution threshold."""

import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.dex.providers import build_aggregator_from_env
from libs.dex.routes import make_route


def test_quote_sizes():
    """Test quotes at various sizes to find execution threshold."""
    # Initialize aggregator
    try:
        aggregator = build_aggregator_from_env()
    except Exception as e:
        print(f"Error: Failed to initialize DexAggregator: {e}")
        sys.exit(1)

    # Get token addresses
    diem_addr = os.getenv("DIEM_TOKEN_ADDRESS", "").strip()
    usdc_addr = os.getenv("QUOTE_TOKEN_ADDRESS", "").strip()

    if not diem_addr or not usdc_addr:
        print("Error: DIEM_TOKEN_ADDRESS and QUOTE_TOKEN_ADDRESS must be set")
        sys.exit(1)

    # Test route: DIEM -> USDC (will use bridge_vvv internally)
    route = make_route([diem_addr, usdc_addr])

    # Test sizes in USD: $1, $10, $100, $500, $1000
    # DIEM has 18 decimals, so 1 DIEM = 1e18 base units
    # Assuming DIEM price ~$141, 1 DIEM ≈ $141
    # So $1 ≈ 1e18 / 141 ≈ 7e15 base units

    test_sizes_usd = [1, 10, 100, 500, 1000]
    diem_price_usd = 141.0  # Approximate, adjust based on actual price

    print("Testing quote sizes for DIEM -> USDC route")
    print("=" * 60)

    results = []

    for size_usd in test_sizes_usd:
        # Convert USD to DIEM tokens, then to base units
        diem_tokens = size_usd / diem_price_usd
        amount_in_base = int(diem_tokens * 1e18)

        # NOTE: Keep output ASCII-only so this test runs cleanly on Windows shells
        # using cp1252 (or other non-UTF8) encodings.
        print(
            f"\nTesting ${size_usd} (~={diem_tokens:.6f} DIEM = {amount_in_base} base units)"
        )
        print("-" * 60)

        try:
            # Try exact-in quote
            quote = aggregator.best_quote(amount_in_base, route)

            if quote:
                amount_out = quote.amount_out
                # USDC has 6 decimals
                usdc_amount = amount_out / 1e6
                effective_price = usdc_amount / diem_tokens if diem_tokens > 0 else 0

                print("  ✓ Quote succeeded")
                print(
                    f"    Amount out: {amount_out} base units ({usdc_amount:.2f} USDC)"
                )
                print(f"    Effective price: ${effective_price:.2f} per DIEM")
                print(f"    Provider: {quote.provider}")

                results.append(
                    {
                        "size_usd": size_usd,
                        "amount_in": amount_in_base,
                        "success": True,
                        "amount_out": amount_out,
                        "usdc_amount": usdc_amount,
                        "effective_price": effective_price,
                        "provider": quote.provider,
                    }
                )
            else:
                print("  ✗ Quote failed (no quote returned)")
                results.append(
                    {
                        "size_usd": size_usd,
                        "amount_in": amount_in_base,
                        "success": False,
                        "error": "no_quote",
                    }
                )
        except Exception as e:
            print(f"  ✗ Quote failed with error: {e}")
            results.append(
                {
                    "size_usd": size_usd,
                    "amount_in": amount_in_base,
                    "success": False,
                    "error": str(e),
                }
            )

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)

    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]

    print(f"Successful quotes: {len(successful)}/{len(results)}")
    print(f"Failed quotes: {len(failed)}/{len(results)}")

    if successful:
        print("\nSuccessful sizes:")
        for r in successful:
            print(
                f"  ${r['size_usd']}: {r['usdc_amount']:.2f} USDC via {r['provider']}"
            )

    if failed:
        print("\nFailed sizes:")
        for r in failed:
            print(f"  ${r['size_usd']}: {r.get('error', 'unknown')}")

        # Find threshold
        if successful:
            max_successful = max(r["size_usd"] for r in successful)
            min_failed = min(r["size_usd"] for r in failed)
            print(f"\nExecution threshold: Between ${max_successful} and ${min_failed}")

    return results


if __name__ == "__main__":
    results = test_quote_sizes()
    sys.exit(0 if any(r.get("success") for r in results) else 1)
