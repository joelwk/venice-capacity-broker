"""
Test Aerodrome exact-out quoting with router vs reserve fallback.

This test verifies that:
1. Aerodrome router getAmountsIn calls work when possible
2. Reserve fallback provides valid quotes when router reverts
3. Both paths return consistent results for DIEM→VVV route
"""

import os
import sys

import pytest

from libs.dex.providers import AerodromeDexProvider, build_aggregator_from_env
from libs.dex.routes import make_route

_RUN_LIVE_RPC = str(os.getenv("VENICE_RUN_LIVE_RPC_TESTS", "")).strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
if not _RUN_LIVE_RPC:
    pytest.skip(
        "Skipping live-RPC Aerodrome quoting tests (set VENICE_RUN_LIVE_RPC_TESTS=1 to enable).",
        allow_module_level=True,
    )


@pytest.fixture(autouse=True)
def setup_aerodrome_env(monkeypatch):
    """Set up environment variables needed for Aerodrome exact-out fallback."""
    monkeypatch.setenv(
        "AERODROME_FACTORY_VOLATILE", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    )
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # gitleaks:allow Base mainnet contract
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS",
        "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # gitleaks:allow Base mainnet contract
    )
    # Set a public Base RPC URL for tests (fallback will use public endpoints if this fails)
    monkeypatch.setenv(
        "BASE_RPC_URL", "https://base-mainnet.g.alchemy.com/v2/demo"
    )  # gitleaks:allow demo API key


@pytest.fixture
def aerodrome_provider():
    """Create Aerodrome provider with test config."""
    router = os.getenv(
        "AERODROME_ROUTER_ADDRESS", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
    )
    return AerodromeDexProvider(router, stable=False)


@pytest.fixture
def diem_vvv_route():
    """Create DIEM→VVV route."""
    return make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # VVV
        ]
    )


def test_aerodrome_exact_out_returns_quote(aerodrome_provider, diem_vvv_route):
    """Test that Aerodrome quote_exact_out returns a valid quote (router or fallback)."""
    amount_out = 10**18  # 1 VVV

    quote = aerodrome_provider.quote_exact_out(amount_out, diem_vvv_route)

    assert quote is not None, (
        "Aerodrome quote_exact_out should return a quote (router or fallback)"
    )
    assert quote.provider == "aerodrome"
    assert quote.amount_out == amount_out
    assert quote.amount_in > 0, "amount_in should be positive"

    # Verify the quote is reasonable (DIEM should be cheaper than VVV based on reserves)
    # From logs: ~89.8 VVV per DIEM, so 1 VVV should cost ~0.011 DIEM
    assert quote.amount_in < 10**17, (
        f"amount_in ({quote.amount_in}) seems too high for 1 VVV"
    )


def test_aerodrome_fallback_works_when_router_fails(aerodrome_provider, diem_vvv_route):
    """Test that reserve fallback works when router getAmountsIn reverts."""
    amount_out = 10**18  # 1 VVV

    # Call the fallback method directly
    fallback_quote = aerodrome_provider._quote_exact_out_reserve(
        amount_out, diem_vvv_route
    )

    assert fallback_quote is not None, "Reserve fallback should work for DIEM→VVV"
    assert fallback_quote.provider == "aerodrome"
    assert fallback_quote.amount_out == amount_out
    assert fallback_quote.amount_in > 0


def test_aggregator_includes_aerodrome_quote(diem_vvv_route, monkeypatch):
    """Test that DexAggregator includes Aerodrome quotes in best_quote_exact_out."""
    # Ensure Aerodrome exact-out is enabled and all required env vars are set
    monkeypatch.setenv("AERODROME_EXACT_OUT_ENABLE", "1")
    monkeypatch.setenv("DEX_PROVIDERS", "aerodrome")
    monkeypatch.setenv(
        "AERODROME_ROUTER_ADDRESS", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
    )
    # Ensure factory and pair addresses are set for fallback
    monkeypatch.setenv(
        "AERODROME_FACTORY_VOLATILE", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    )
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d"
    )
    # Ensure BASE_RPC_URL is set for provider initialization
    monkeypatch.setenv(
        "BASE_RPC_URL", "https://base-mainnet.g.alchemy.com/v2/demo"
    )  # gitleaks:allow demo API key
    # Increase timeout for tests (default is 3s, but RPC calls can be slow)
    monkeypatch.setenv("DEX_PROVIDER_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("DEX_AGGREGATE_TIMEOUT_SECONDS", "15")
    # Use single worker to avoid threading issues
    monkeypatch.setenv("DEX_MAX_WORKERS", "1")

    agg = build_aggregator_from_env()
    amount_out = 10**18  # 1 VVV

    quote = agg.best_quote_exact_out(amount_out, diem_vvv_route)

    assert quote is not None, "Aggregator should return an Aerodrome quote"
    assert quote.provider == "aerodrome"
    assert quote.amount_out == amount_out
    assert quote.amount_in > 0


def test_aerodrome_quote_consistency(aerodrome_provider, diem_vvv_route):
    """Test that multiple calls return consistent quotes."""
    amount_out = 10**18  # 1 VVV

    quote1 = aerodrome_provider.quote_exact_out(amount_out, diem_vvv_route)
    quote2 = aerodrome_provider.quote_exact_out(amount_out, diem_vvv_route)

    assert quote1 is not None
    assert quote2 is not None

    # Quotes should be within 1% of each other (allowing for minor reserve changes)
    diff_pct = abs(quote1.amount_in - quote2.amount_in) / quote1.amount_in
    assert diff_pct < 0.01, (
        f"Quotes should be consistent, got {quote1.amount_in} vs {quote2.amount_in}"
    )


if __name__ == "__main__":
    # Allow running as script for manual testing
    import logging

    logging.basicConfig(level=logging.DEBUG)

    os.environ.setdefault(
        "AERODROME_ROUTER_ADDRESS", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
    )
    os.environ.setdefault(
        "AERODROME_FACTORY_VOLATILE", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
    )
    os.environ.setdefault(
        "DIEM_VVV_PAIR_ADDRESS",
        "0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d",  # gitleaks:allow Base mainnet contract
    )
    os.environ.setdefault(
        "DIEM_TOKEN_ADDRESS",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # gitleaks:allow Base mainnet contract
    )
    os.environ.setdefault(
        "VVV_TOKEN_ADDRESS",
        "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # gitleaks:allow Base mainnet contract
    )
    os.environ.setdefault(
        "BASE_RPC_URL",
        os.getenv("BASE_RPC_URL") or "https://mainnet.base.org",
    )
    os.environ.setdefault("AERODROME_EXACT_OUT_ENABLE", "1")
    os.environ.setdefault("DIEM_DEBUG_ROUTES", "1")

    provider = AerodromeDexProvider(
        os.environ["AERODROME_ROUTER_ADDRESS"], stable=False
    )
    route = make_route(
        [
            os.environ["DIEM_TOKEN_ADDRESS"],
            os.environ["VVV_TOKEN_ADDRESS"],
        ]
    )

    print("Testing Aerodrome exact-out quote...")
    quote = provider.quote_exact_out(10**18, route)
    if quote:
        print(
            f"✓ Quote received: {quote.amount_in} DIEM in for {quote.amount_out} VVV out"
        )
    else:
        print("✗ No quote received")
        sys.exit(1)
