import os
import sys
from pathlib import Path

# Add repo root to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from libs.env import load_dotenv_if_present  # noqa: E402

load_dotenv_if_present()

from libs.dex.providers import build_aggregator_from_env  # noqa: E402
from services.diem.client import DIEMService  # noqa: E402


def main():
    print(f"DIEM_EXACT_IN_FALLBACK_ENABLE={os.getenv('DIEM_EXACT_IN_FALLBACK_ENABLE')}")
    print(
        f"DIEM_EXACT_IN_FALLBACK_MAX_USD={os.getenv('DIEM_EXACT_IN_FALLBACK_MAX_USD')}"
    )

    agg = build_aggregator_from_env()
    svc = DIEMService(aggregator=agg)

    # Simulate the buy logic
    # Target: 0.0775 DIEM (approx $10)
    target_diem = int(0.0775 * 10**18)

    print(
        f"\nSimulating Buy for {target_diem} units ({target_diem / 10**18:.4f} DIEM)..."
    )

    # 1. Get routes
    try:
        routes = svc.trade_routes()
        print(f"Found {len(routes)} routes")
    except Exception as e:
        print(f"Error getting routes: {e}")
        return

    # 2. Simulate fallback logic
    fallback_enabled = os.getenv(
        "DIEM_EXACT_IN_FALLBACK_ENABLE", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    print(f"Fallback logic enabled in code: {fallback_enabled}")

    if not fallback_enabled:
        print("Fallback disabled, skipping simulation.")
        return

    quote_decimals = int(os.getenv("QUOTE_TOKEN_DECIMALS", "6") or 6)
    max_usd = float(os.getenv("DIEM_EXACT_IN_FALLBACK_MAX_USD", "10.0") or 10.0)
    max_amount_in = int(max_usd * (10**quote_decimals))
    print(f"Max input: {max_amount_in} units (${max_usd})")

    for i, route in enumerate(routes):
        print(f"\nRoute {i}: {[t[:10] for t in route.tokens]}")
        try:
            rev_route = route.reversed()
        except Exception:
            continue

        candidate_ins = []
        # Heuristic sizes
        for factor in (1.0, 0.5, 0.25):
            sized = int(max_amount_in * factor)
            if sized > 0:
                candidate_ins.append(sized)

        print(f"Candidates inputs: {candidate_ins}")

        for amount_in_candidate in candidate_ins:
            print(f"  Checking input {amount_in_candidate}...")
            try:
                quote_in = svc.aggregator.best_quote(amount_in_candidate, rev_route)
                if quote_in:
                    amount_out = quote_in.amount_out
                    min_acceptable = int(target_diem * 0.9)
                    print(
                        f"    Quote out: {amount_out} (Target: {target_diem}, Min: {min_acceptable})"
                    )

                    if amount_out >= min_acceptable:
                        print("    ✅ Fallback would be ACCEPTED")
                    else:
                        print("    ❌ Fallback REJECTED (output too low)")
                else:
                    print("    No quote returned")
            except Exception as e:
                print(f"    Error quoting: {e}")


if __name__ == "__main__":
    main()
