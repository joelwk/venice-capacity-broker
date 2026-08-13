#!/usr/bin/env python3
"""Test database and KV store validation enhancements.

This test validates:
1. Local run without Postgres shows clear warning
2. Replit environment without production DB URL triggers specific remediation guidance
3. Live mode with in-memory KV fallback is blocked
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

# Add repo root to path
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))


def test_local_postgres_warning():
    """Test that local runs without Postgres trigger appropriate warnings."""
    print("\n" + "=" * 60)
    print("TEST 1: Local run without Postgres configuration")
    print("=" * 60)

    test_env = {
        "APP_ENV": "development",
        "SQL_DATABASE_URL": "",
        "ALLOW_SQLITE_FALLBACK": "",
        "BROKER_ADMIN_TOKEN": "test-token",  # gitleaks:allow test value
        "SESSION_SECRET": "test-secret",  # gitleaks:allow test value
        "REDIS_URL": "redis://localhost:6379/0",
        "VENICE_API_BASE_URL": "https://api.venice.ai/api/v1",
        "VENICE_PARENT_KEY": "test-key",  # gitleaks:allow test value
        "VENICE_API_KEY": "test-key",  # gitleaks:allow test value
        "BASE_RPC_URL": "https://mainnet.base.org",
        "BASE_CHAIN_ID": "8453",
        "QUOTES_ENABLED": "true",
        "PURCHASES_ENABLED": "true",
    }

    # Remove any docker/replit environment variables
    with patch.dict(os.environ, test_env, clear=False):
        # Explicitly remove replit indicators
        for key in [
            "REPLIT_DB_URL",
            "REPLIT_ENVIRONMENT",
            "REPLIT_ENV",
            "REPLIT_WORKSPACE_ID",
            "REPLIT_APP_ID",
        ]:
            if key in os.environ:
                del os.environ[key]

        from scripts.validate_broker_env import _detect_execution_context, validate_env

        context = _detect_execution_context()
        print(f"Detected context: {context}")
        assert context == "local", f"Expected 'local', got '{context}'"

        report = validate_env()

        print(f"\nTotal issues found: {len(report['issues'])}")
        print(
            f"Issue categories: {set(issue['category'] for issue in report['issues'])}"
        )

        # Check for any SQL-related issues mentioning local context
        local_postgres_issues = [
            issue
            for issue in report["issues"]
            if "Local" in issue["message"]
            and ("Postgres" in issue["message"] or "SQL" in issue["message"])
        ]

        # Also check for general SQL configuration issues that apply to local context
        sql_issues = [
            issue
            for issue in report["issues"]
            if "SQL_DATABASE_URL" in issue["message"]
        ]

        # Check storage category issues
        storage_issues = [
            issue for issue in report["issues"] if issue["category"] == "storage"
        ]

        print(f"\nFound {len(local_postgres_issues)} local-specific Postgres issue(s)")
        for issue in local_postgres_issues:
            print(f"  - {issue['severity'].upper()}: {issue['message']}")
            print(f"    Remediation: {issue['remediation'][:100]}...")

        print(f"\nFound {len(sql_issues)} SQL configuration issue(s)")
        for issue in sql_issues[:3]:  # Show first 3
            print(f"  - {issue['severity'].upper()}: {issue['message']}")

        print(f"\nFound {len(storage_issues)} storage-related issue(s)")
        for issue in storage_issues[:5]:  # Show first 5
            print(f"  - {issue['severity'].upper()}: {issue['message'][:80]}...")

        # Either local-specific or general SQL/storage issue should be present
        assert (
            len(local_postgres_issues) > 0
            or len(sql_issues) > 0
            or len(storage_issues) > 0
        ), "Expected local Postgres, SQL configuration, or storage warning"
        print("\n✓ TEST 1 PASSED: Local Postgres/SQL configuration warning triggered")


def test_replit_production_database_validation():
    """Test that Replit environment without production DB triggers warnings."""
    print("\n" + "=" * 60)
    print("TEST 2: Replit environment without production database")
    print("=" * 60)

    test_env = {
        "APP_ENV": "production",
        "REPLIT_DB_URL": "https://kv.replit.com/v0/test-token",  # gitleaks:allow test URL
        "REPLIT_ENVIRONMENT": "production",
        "SQL_DATABASE_URL": "",  # Missing production database
        "BROKER_ADMIN_TOKEN": "test-token",  # gitleaks:allow test value
        "SESSION_SECRET": "test-secret",  # gitleaks:allow test value
        "VENICE_API_BASE_URL": "https://api.venice.ai/api/v1",
        "VENICE_PARENT_KEY": "test-key",  # gitleaks:allow test value
        "VENICE_API_KEY": "test-key",  # gitleaks:allow test value
        "BASE_RPC_URL": "https://mainnet.base.org",
        "BASE_CHAIN_ID": "8453",
        "QUOTES_ENABLED": "true",
        "PURCHASES_ENABLED": "true",
    }

    with patch.dict(os.environ, test_env, clear=False):
        # Ensure Replit indicators are present
        os.environ["REPLIT_DB_URL"] = (
            "https://kv.replit.com/v0/test-token"  # gitleaks:allow test URL
        )
        os.environ["REPLIT_ENVIRONMENT"] = "production"

        from scripts.validate_broker_env import _detect_execution_context, validate_env

        context = _detect_execution_context()
        print(f"Detected context: {context}")
        assert context == "replit", f"Expected 'replit', got '{context}'"

        report = validate_env()

        # Check for Replit production database issues
        replit_db_issues = [
            issue
            for issue in report["issues"]
            if "Replit production database" in issue["message"]
        ]

        print(f"\nFound {len(replit_db_issues)} Replit production database issue(s)")
        for issue in replit_db_issues:
            print(f"  - {issue['severity'].upper()}: {issue['message']}")
            if len(issue["remediation"]) > 200:
                print(f"    Remediation: {issue['remediation'][:200]}...")
            else:
                print(f"    Remediation: {issue['remediation']}")

        # Check stage checks
        stages = report.get("stages", {})
        replit_checks = [
            check
            for stage in stages.values()
            for check in stage.get("checks", [])
            if "Replit production database" in check.get("name", "")
        ]

        print(f"\nFound {len(replit_checks)} Replit database check(s)")
        for check in replit_checks:
            print(
                f"  - {check['name']}: {'✓' if check['ok'] else '✗'} {check.get('detail', '')}"
            )

        assert len(replit_db_issues) > 0 or any(not c["ok"] for c in replit_checks), (
            "Expected Replit production database warning"
        )
        print("\n✓ TEST 2 PASSED: Replit production database validation triggered")


def test_live_mode_kv_fallback_blocked():
    """Test that live mode with in-memory KV fallback is blocked."""
    print("\n" + "=" * 60)
    print("TEST 3: Live mode with in-memory KV fallback")
    print("=" * 60)

    test_env = {
        "APP_ENV": "production",
        "AUTOSTART_ORCHESTRATOR_LIVE": "1",
        "ALLOW_INMEMORY_KV_FALLBACK": "1",  # Should trigger critical error
        "SQL_DATABASE_URL": "postgresql://user:pass@host:5432/db",  # gitleaks:allow test connection string
        "BROKER_ADMIN_TOKEN": "test-token",  # gitleaks:allow test value
        "SESSION_SECRET": "test-secret",  # gitleaks:allow test value
        "REDIS_URL": "",  # No durable KV store
        "REPLIT_DB_URL": "",
    }

    with patch.dict(os.environ, test_env, clear=True):
        from scripts.validate_broker_env import validate_env

        report = validate_env()

        # Check for live mode KV fallback critical error
        live_kv_issues = [
            issue
            for issue in report["issues"]
            if "Live mode" in issue["message"]
            and "in-memory" in issue["message"].lower()
        ]

        print(f"\nFound {len(live_kv_issues)} live mode KV issue(s)")
        for issue in live_kv_issues:
            print(f"  - {issue['severity'].upper()}: {issue['message']}")
            print(f"    Impact: {issue['impact']}")
            print(f"    Remediation: {issue['remediation']}")

        # Check stage checks for live mode
        stages = report.get("stages", {})
        live_checks = [
            check
            for stage in stages.values()
            for check in stage.get("checks", [])
            if "live mode" in check.get("name", "").lower()
        ]

        print(f"\nFound {len(live_checks)} live mode KV check(s)")
        for check in live_checks:
            print(
                f"  - {check['name']}: {'✓' if check['ok'] else '✗'} {check.get('detail', '')}"
            )

        critical_issues = [i for i in live_kv_issues if i["severity"] == "critical"]
        assert len(critical_issues) > 0, (
            "Expected critical error for live mode with in-memory KV fallback"
        )
        print("\n✓ TEST 3 PASSED: Live mode KV fallback validation triggered")


def test_live_mode_with_proper_config():
    """Test that live mode with proper configuration passes validation."""
    print("\n" + "=" * 60)
    print("TEST 4: Live mode with proper durable KV store")
    print("=" * 60)

    test_env = {
        "APP_ENV": "production",
        "AUTOSTART_ORCHESTRATOR_LIVE": "1",
        "ALLOW_INMEMORY_KV_FALLBACK": "0",  # Properly disabled
        "SQL_DATABASE_URL": "postgresql://user:pass@host:5432/db",  # gitleaks:allow test connection string
        "REDIS_URL": "redis://redis:6379/0",  # Durable KV store
        "BROKER_ADMIN_TOKEN": "test-token",  # gitleaks:allow test value
        "SESSION_SECRET": "test-secret",  # gitleaks:allow test value
        "VENICE_API_BASE_URL": "https://api.venice.ai/api/v1",
        "VENICE_PARENT_KEY": "test-parent-key",  # gitleaks:allow test value
        "VENICE_API_KEY": "test-api-key",  # gitleaks:allow test value
        "BASE_RPC_URL": "https://mainnet.base.org",
        "BASE_CHAIN_ID": "8453",
        "VVV_TOKEN_ADDRESS": "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # gitleaks:allow Base mainnet contract
        "VVV_STAKING_ADDRESS": "0x321b7ff75154472B18EDb199033fF4D116F340Ff",  # gitleaks:allow Base mainnet contract
        "DIEM_TOKEN_ADDRESS": "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # gitleaks:allow Base mainnet contract
        "QUOTE_TOKEN_ADDRESS": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # gitleaks:allow Base mainnet contract
        "WETH_ADDRESS": "0x4200000000000000000000000000000000000006",  # gitleaks:allow Base mainnet contract
        "ETH_PRIVATE_KEY": "0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",  # gitleaks:allow test private key
        "CDP_API_KEY_ID": "test-cdp-key-id",  # gitleaks:allow test value
        "CDP_API_KEY_SECRET": "test-cdp-secret",  # gitleaks:allow test value
        "CDP_WALLET_SECRET": "test-wallet-secret",  # gitleaks:allow test value
        "QUOTES_ENABLED": "true",
        "PURCHASES_ENABLED": "true",
    }

    with patch.dict(os.environ, test_env, clear=True):
        from scripts.validate_broker_env import validate_env

        report = validate_env()

        # Check stage checks for live mode
        stages = report.get("stages", {})
        live_stage = stages.get("orchestrator_live", {})
        live_checks = live_stage.get("checks", [])

        kv_checks = [
            check
            for check in live_checks
            if "KV" in check.get("name", "")
            and "live mode" in check.get("name", "").lower()
        ]

        print(f"\nFound {len(kv_checks)} live mode KV check(s)")
        for check in kv_checks:
            print(
                f"  - {check['name']}: {'✓' if check['ok'] else '✗'} {check.get('detail', '')}"
            )

        # Should not have critical live mode KV issues
        live_kv_critical = [
            issue
            for issue in report["issues"]
            if "Live mode" in issue["message"]
            and "in-memory" in issue["message"].lower()
            and issue["severity"] == "critical"
        ]

        print(f"\nCritical live mode KV issues: {len(live_kv_critical)}")
        assert len(live_kv_critical) == 0, (
            "Should not have critical live mode KV issues with proper config"
        )

        print("\n✓ TEST 4 PASSED: Live mode with proper configuration validated")


if __name__ == "__main__":
    print("\nDatabase and KV Store Validation Tests")
    print("=" * 60)

    try:
        test_local_postgres_warning()
        test_replit_production_database_validation()
        test_live_mode_kv_fallback_blocked()
        test_live_mode_with_proper_config()

        print("\n" + "=" * 60)
        print("ALL TESTS PASSED ✓")
        print("=" * 60)
        sys.exit(0)
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ UNEXPECTED ERROR: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(2)
