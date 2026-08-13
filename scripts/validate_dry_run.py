"""Validate complete dry-run cycle with enhanced diagnostics."""

import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from libs.telemetry.logger import get_logger

logger = get_logger("validate_dry_run")


def check_diagnostics_log(log_path: str = "logs/dex_diagnostics.jsonl"):
    """Check dex diagnostics log for patterns."""
    if not os.path.exists(log_path):
        logger.warning(f"Diagnostics log not found: {log_path}")
        return {}

    patterns = {
        "circuit_open": 0,
        "leg_provider_failure": 0,
        "spl_error": 0,
        "timeout": 0,
        "successful_quotes": 0,
        "empty_quotes": 0,
    }

    try:
        with open(log_path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    event = entry.get("event", "")

                    if event == "dex_quote_failure":
                        reason = entry.get("reason", "")
                        if "circuit_open" in str(entry.get("circuit_open", {})):
                            patterns["circuit_open"] += 1
                        if "timeout" in reason.lower():
                            patterns["timeout"] += 1
                        if "empty" in reason.lower():
                            patterns["empty_quotes"] += 1

                    elif event == "dex_composite_leg_failed":
                        patterns["leg_provider_failure"] += 1

                    elif "spl" in str(entry).lower():
                        patterns["spl_error"] += 1

                    elif event == "dex_quote_success" or (
                        entry.get("status") == "ok" and entry.get("amount_out", 0) > 0
                    ):
                        patterns["successful_quotes"] += 1
                except Exception:
                    continue
    except Exception as e:
        logger.error(f"Error reading diagnostics log: {e}")

    return patterns


def validate_config():
    """Validate required configuration is present."""
    required_vars = [
        "DIEM_TOKEN_ADDRESS",
        "VVV_TOKEN_ADDRESS",
        "QUOTE_TOKEN_ADDRESS",
        "BASE_RPC_URL",
        "UNISWAP_V3_QUOTER_ADDRESS",
    ]

    missing = []
    for var in required_vars:
        if not os.getenv(var):
            missing.append(var)

    if missing:
        logger.error(f"Missing required environment variables: {', '.join(missing)}")
        return False

    # Check optional but recommended vars
    recommended_vars = [
        "DIEM_ENABLE_PAIR_MATH_FALLBACK",
        "DEX_PROVIDER_TIMEOUT_SECONDS",
        "DEX_CIRCUIT_FAILURE_THRESHOLD",
        "UNISWAP_V3_SQRT_PRICE_LIMIT",
    ]

    missing_recommended = []
    for var in recommended_vars:
        if not os.getenv(var):
            missing_recommended.append(var)

    if missing_recommended:
        logger.warning(
            f"Missing recommended environment variables: {', '.join(missing_recommended)}"
        )

    return True


def main():
    """Main validation function."""
    print("=" * 60)
    print("Dry-Run Validation")
    print("=" * 60)

    # Check configuration
    print("\n1. Checking configuration...")
    if not validate_config():
        print("  ✗ Configuration validation failed")
        sys.exit(1)
    print("  ✓ Configuration valid")

    # Check diagnostics log
    print("\n2. Checking diagnostics log...")
    patterns = check_diagnostics_log()

    if patterns:
        print("  Diagnostics patterns found:")
        for pattern, count in patterns.items():
            status = "✓" if count == 0 or pattern == "successful_quotes" else "⚠"
            print(f"    {status} {pattern}: {count}")

        # Check for issues
        issues = []
        if patterns.get("circuit_open", 0) > 0:
            issues.append("Circuit breakers are open")
        if patterns.get("leg_provider_failure", 0) > 0:
            issues.append("Leg provider failures detected")
        if patterns.get("spl_error", 0) > 0:
            issues.append("SPL errors detected")
        if patterns.get("timeout", 0) > 0:
            issues.append("Timeout errors detected")
        if patterns.get("successful_quotes", 0) == 0:
            issues.append("No successful quotes found")

        if issues:
            print("\n  ⚠ Issues detected:")
            for issue in issues:
                print(f"    - {issue}")
        else:
            print("\n  ✓ No major issues detected")
    else:
        print("  ⚠ No diagnostics data found (run a dry-run cycle first)")

    print("\n" + "=" * 60)
    print("Validation complete")
    print("=" * 60)

    return len(patterns.get("successful_quotes", [])) > 0 if patterns else False


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
