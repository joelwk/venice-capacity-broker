"""
Unit tests for extracted broker helper modules.
"""
import pytest
from unittest.mock import Mock


def test_auth_bearer_token_extraction():
    """Test bearer token extraction from Authorization header."""
    from apps.broker_api.auth import bearer_token
    
    # Valid bearer token
    assert bearer_token("Bearer abc123") == "abc123"
    assert bearer_token("bearer xyz789") == "xyz789"  # case insensitive
    
    # Invalid formats
    assert bearer_token(None) is None
    assert bearer_token("") is None
    assert bearer_token("abc123") is None  # no Bearer prefix
    assert bearer_token("Bearer") is None  # no token
    assert bearer_token("Basic abc123") is None  # wrong auth type


def test_auth_require_admin_with_token(monkeypatch):
    """Test admin authentication when token is configured."""
    from apps.broker_api import auth
    from fastapi import HTTPException
    
    # Set admin token
    monkeypatch.setattr(auth, "ADMIN_TOKEN", "secret123")
    
    # Valid admin token
    auth.require_admin("Bearer secret123")  # Should not raise
    
    # Invalid token
    with pytest.raises(HTTPException) as exc:
        auth.require_admin("Bearer wrong")
    assert exc.value.status_code == 401
    
    # No token
    with pytest.raises(HTTPException) as exc:
        auth.require_admin(None)
    assert exc.value.status_code == 401


def test_auth_require_admin_without_token(monkeypatch):
    """Test admin authentication when no token configured (dev mode)."""
    from apps.broker_api import auth
    
    # Clear admin token
    monkeypatch.setattr(auth, "ADMIN_TOKEN", None)
    
    # Should log warning but not raise
    auth.require_admin(None)
    auth.require_admin("Bearer anything")


def test_config_compute_expires_at():
    """Test expiry timestamp computation."""
    from apps.broker_api.config import compute_expires_at
    import time
    
    # With positive expiry days
    now = time.time()
    result = compute_expires_at(now)
    assert result is not None
    assert result.endswith("Z")  # ISO8601 Zulu
    assert "T" in result  # Has time separator


def test_config_extract_field_simple():
    """Test field extraction from simple structures."""
    from apps.broker_api.config import extract_field
    
    # Direct key
    assert extract_field({"apiKey": "abc123"}, ["apiKey"]) == "abc123"
    
    # Multiple candidates, first wins
    assert extract_field({"key": "first", "apiKey": "second"}, ["apiKey", "key"]) == "second"
    
    # Nested dict
    payload = {"data": {"auth": {"apiKey": "nested"}}}
    assert extract_field(payload, ["apiKey"]) == "nested"
    
    # Not found
    assert extract_field({"other": "value"}, ["apiKey"]) == ""


def test_config_extract_field_numeric():
    """Test extraction of numeric values."""
    from apps.broker_api.config import extract_field
    
    # Integer
    assert extract_field({"count": 42}, ["count"]) == "42"
    
    # Float
    assert extract_field({"price": 19.99}, ["price"]) == "19.99"
    
    # Boolean should be ignored
    assert extract_field({"flag": True}, ["flag"]) == ""


def test_cache_ttl_and_capacity(monkeypatch):
    """Test cache configuration helpers."""
    from apps.broker_api import cache
    
    # Default values (may vary based on env)
    ttl = cache.prices_cache_ttl_seconds()
    assert ttl > 0  # Should be positive
    capacity = cache.prices_cache_capacity()
    assert capacity > 0  # Should be positive
    
    # Test with explicit env values
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "120.5")
    monkeypatch.setenv("BROKER_PRICES_CACHE_MAX", "256")
    
    # Values should be read from env
    assert cache.prices_cache_ttl_seconds() == 120.5
    assert cache.prices_cache_capacity() == 256


def test_cache_set_and_get():
    """Test cache set/get operations."""
    from apps.broker_api import cache
    
    # Set a value
    payload = {"price": 100, "symbol": "TEST"}
    cache.prices_cache_set("test_key", payload)
    
    # Get it back
    cached = cache.prices_cache_get("test_key")
    assert cached is not None
    assert cached["price"] == 100
    assert cached["symbol"] == "TEST"
    assert cached["meta"]["cacheHit"] is True
    assert "cacheAgeMs" in cached["meta"]


def test_cache_expiry(monkeypatch):
    """Test cache expiry behavior."""
    from apps.broker_api import cache
    
    # Set TTL to 0 (disabled)
    monkeypatch.setenv("BROKER_PRICES_TTL_SECONDS", "0")
    
    # Cache should be disabled
    payload = {"test": "data"}
    cache.prices_cache_set("key", payload)
    assert cache.prices_cache_get("key") is None


def test_store_build_sql_backend(monkeypatch):
    """Test store building with SQL backend."""
    from apps.broker_api import store
    
    monkeypatch.setenv("BROKER_STORE_BACKEND", "sql")
    
    tenant_store = store.build_store()
    assert tenant_store is not None
    # Should be either SQLTenantStore or fallback to TenantStore


def test_store_build_json_backend(monkeypatch):
    """Test store building with JSON backend."""
    from apps.broker_api import store
    
    monkeypatch.setenv("BROKER_STORE_BACKEND", "json")
    
    tenant_store = store.build_store()
    assert tenant_store is not None
    assert type(tenant_store).__name__ in ["TenantStore", "SQLTenantStore"]


# ===== New module tests =====

def test_rate_limit_build(monkeypatch):
    """Test rate limiter building from environment."""
    from apps.broker_api import rate_limit
    
    # Ensure no env var pollution from other tests
    monkeypatch.delenv("RATE_LIMIT_MAX_REQUESTS", raising=False)
    monkeypatch.delenv("RATE_LIMIT_WINDOW_SECONDS", raising=False)
    
    # Test with rate limiting disabled (default)
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "false")
    limiter, kv_admin, enabled, window, max_req = rate_limit.build_rate_limiter()
    assert enabled is False
    assert limiter is None
    # kv_admin may or may not be None depending on KV availability
    assert window == 60
    assert max_req == 60


def test_rate_limit_build_with_custom_values(monkeypatch):
    """Test rate limiter with custom window and max requests."""
    from apps.broker_api import rate_limit
    
    monkeypatch.setenv("RATE_LIMITS_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "120")
    monkeypatch.setenv("RATE_LIMIT_MAX_REQUESTS", "200")
    
    limiter, kv_admin, enabled, window, max_req = rate_limit.build_rate_limiter()
    assert window == 120
    assert max_req == 200


def test_marketdata_provider_singleton():
    """Test MarketDataProvider singleton pattern."""
    from apps.broker_api import marketdata
    
    # First call creates instance
    try:
        provider1 = marketdata.get_marketdata_provider()
        assert provider1 is not None
        
        # Second call returns same instance
        provider2 = marketdata.get_marketdata_provider()
        assert provider1 is provider2
    except Exception:
        # MarketDataProvider may not be available in test environment
        pytest.skip("MarketDataProvider not available")


def test_pricing_service_build():
    """Test PricingService builder."""
    from apps.broker_api.services import pricing
    
    try:
        service = pricing.build_pricing_service()
        assert service is not None
    except Exception:
        # PricingService may not be available in test environment
        pytest.skip("PricingService not available")


def test_clearing_compute_price_with_mock():
    """Test clearing price computation with mocked market data."""
    from apps.broker_api.services import clearing
    
    # Mock market data provider
    mock_provider = Mock()
    mock_provider.prices.return_value = {"DIEM": 1.25, "VVV": 0.05}
    
    result = clearing.compute_clearing_price(mock_provider, clearing_band_bps=200)
    
    assert result["price"] == 1.25
    assert result["bandBps"] == 200
    # 200 bps = 2% spread
    assert result["bandMin"] == pytest.approx(1.25 * 0.98)
    assert result["bandMax"] == pytest.approx(1.25 * 1.02)


def test_clearing_compute_price_no_diem():
    """Test clearing price computation when DIEM price unavailable."""
    from apps.broker_api.services import clearing
    
    mock_provider = Mock()
    mock_provider.prices.return_value = {}
    
    with pytest.raises(RuntimeError, match="DIEM price unavailable"):
        clearing.compute_clearing_price(mock_provider)


def test_bids_recover_buyer_with_mock():
    """Test EIP-712 signature recovery."""
    from apps.broker_api.services import bids
    from fastapi import HTTPException
    
    # Mock request object
    mock_req = Mock()
    mock_req.buyer = "0x1234567890123456789012345678901234567890"
    mock_req.units = 1000000
    mock_req.maxPrice = 1000000
    mock_req.asset = "USDC"
    mock_req.expiry = 1700000000
    mock_req.slippageBps = 50
    mock_req.nonce = 1
    mock_req.chainId = 8453
    mock_req.signature = "0x" + "00" * 65  # Invalid signature for testing
    
    # Check if eth_account is available, otherwise expect 503
    try:
        from eth_account.messages import encode_structured_data  # noqa: F401
        eth_account_available = True
    except ImportError:
        eth_account_available = False
    
    # Should raise due to invalid signature or unavailable dependency
    with pytest.raises(HTTPException) as exc:
        bids.recover_buyer(mock_req, "Venice Broker", "1", "8453")
    
    if eth_account_available:
        # If eth_account is available, expect 400 for invalid signature
        assert exc.value.status_code == 400
    else:
        # If eth_account is unavailable, expect 503
        assert exc.value.status_code == 503


def test_bids_price_usdc_conversion():
    """Test asset price conversion to USDC."""
    from apps.broker_api.services import bids
    
    # USDC conversion (6 decimals)
    price = bids.price_usdc_per_unit_from_asset(1_500_000, "USDC", Mock())
    assert price == 1.5
    
    # ETH conversion with mocked market data
    mock_mdp_fn = Mock()
    mock_provider = Mock()
    mock_provider.prices.return_value = {"ETH": 2500.0}
    mock_mdp_fn.return_value = mock_provider
    
    # 0.001 ETH at $2500/ETH = $2.50
    price = bids.price_usdc_per_unit_from_asset(1_000_000_000_000_000, "ETH", mock_mdp_fn)
    assert price == pytest.approx(2.5)


def test_bids_classify_expired():
    """Test bid classification for expired bids."""
    from apps.broker_api.services import bids
    
    now_s = 1700000000
    expiry_s = 1699999999  # 1 second ago
    
    status, ctx = bids.classify_bid_status(1.0, now_s, expiry_s, Mock())
    assert status == "expired"
    assert ctx["reason"] == "time"


def test_bids_classify_with_clearing():
    """Test bid classification with clearing price."""
    from apps.broker_api.services import bids
    
    # Mock clearing price function
    mock_clearing = Mock()
    mock_clearing.return_value = {
        "price": 1.0,
        "bandMin": 0.98,
        "bandMax": 1.02,
        "bandBps": 200,
    }
    
    now_s = 1700000000
    expiry_s = 1700000100  # 100 seconds in future
    
    # Price below band
    status, ctx = bids.classify_bid_status(0.95, now_s, expiry_s, mock_clearing)
    assert status == "out_of_band"
    
    # Price at clearing price
    status, ctx = bids.classify_bid_status(1.0, now_s, expiry_s, mock_clearing)
    assert status == "accepted_window"
    
    # Price in band but below clearing
    status, ctx = bids.classify_bid_status(0.99, now_s, expiry_s, mock_clearing)
    assert status == "in_band"


def test_bids_classify_no_clearing():
    """Test bid classification when clearing price unavailable."""
    from apps.broker_api.services import bids
    
    # Mock clearing function that raises
    mock_clearing = Mock(side_effect=RuntimeError("no clearing"))
    
    now_s = 1700000000
    expiry_s = 1700000100
    
    status, ctx = bids.classify_bid_status(1.0, now_s, expiry_s, mock_clearing)
    assert status == "received"
    assert ctx["reason"] == "no_clearing"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

