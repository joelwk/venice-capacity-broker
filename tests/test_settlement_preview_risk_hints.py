"""Test settlement preview risk hints functionality."""

import pytest
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def mock_dex_aggregator():
    """Mock DEX aggregator for testing."""
    mock = Mock()
    mock.best_quote_exact_out.return_value = Mock(
        provider="uniswap_v2",
        path=["0xDIEM", "0xWETH", "0xUSDC"],
        amount_in=1000000,  # 1 DIEM (assuming 6 decimals)
        amount_out=1000000,  # 1 USDC
        gas_estimate=150000,
    )
    return mock


@pytest.fixture
def mock_market_data():
    """Mock market data provider."""
    mock = Mock()
    mock._erc20_decimals.side_effect = lambda addr: 6 if "USDC" in addr else 18
    mock._mid_price_from_reserves.return_value = 1.0  # 1:1 mid price
    mock._weth_address.return_value = "0xWETH"
    mock.reserve_cap_units.return_value = 10000000  # 10 DIEM cap
    return mock


@pytest.fixture
def mock_etherscan_data():
    """Mock etherscan cache data."""
    return {
        "pair": "0xPAIR",
        "reserves": [100000000, 100000000],  # 100 DIEM, 100 USDC reserves
        "token0": "0xDIEM",
        "token1": "0xUSDC"
    }


def test_settlement_preview_with_slippage_and_pool_take(mock_dex_aggregator, mock_market_data, mock_etherscan_data, monkeypatch):
    """Test that settlement preview returns slippageBps and poolTakeBps."""
    # Set env var before importing app
    monkeypatch.setenv("SETTLEMENT_ENABLED", "true")
    
    with patch("libs.dex.providers.build_aggregator_from_env", return_value=mock_dex_aggregator):
        with patch("services.marketdata.provider.MarketDataProvider", return_value=mock_market_data):
            with patch("services.marketdata.etherscan_verify.get_cached_pair_info_for_tokens", 
                      return_value=mock_etherscan_data):
                # Import and reload app module after setting env var
                import importlib
                import sys
                # Reload via sys.modules to ensure we're reloading the module object
                if "apps.broker_api.app" in sys.modules:
                    importlib.reload(sys.modules["apps.broker_api.app"])
                import apps.broker_api.app as app_module
                
                from fastapi.testclient import TestClient
                client = TestClient(app_module.app)
                
                # Test exact-out quote
                response = client.get(
                    "/v1/settlement/quote",
                    params={
                        "fromToken": "0xDIEM",
                        "toAsset": "USDC",
                        "amountOut": 1000000  # 1 USDC
                    }
                )
                
                assert response.status_code == 200
                data = response.json()
                
                # Check response structure
                assert "slippageBps" in data
                assert "poolTakeBps" in data
                assert data["slippageBps"] is not None
                assert data["poolTakeBps"] is not None
                
                # Pool take should be 1% (1M input / 100M reserve)
                assert data["poolTakeBps"] == 100


def test_settlement_preview_fallback_with_risk_hints(mock_market_data, mock_etherscan_data, monkeypatch):
    """Test fallback path calculates slippage and pool take."""
    # Set env var before importing app
    monkeypatch.setenv("SETTLEMENT_ENABLED", "true")
    
    # Mock aggregator to fail, triggering fallback
    mock_agg = Mock()
    mock_agg.best_quote_exact_out.return_value = None
    
    with patch("libs.dex.providers.build_aggregator_from_env", return_value=mock_agg):
        with patch("services.marketdata.provider.MarketDataProvider", return_value=mock_market_data):
            with patch("services.marketdata.etherscan_verify.get_cached_pair_info_for_tokens", 
                      return_value=mock_etherscan_data):
                # Import and reload app module after setting env var
                import importlib
                import sys
                # Reload via sys.modules to ensure we're reloading the module object
                if "apps.broker_api.app" in sys.modules:
                    importlib.reload(sys.modules["apps.broker_api.app"])
                import apps.broker_api.app as app_module
                
                from fastapi.testclient import TestClient
                client = TestClient(app_module.app)
                
                response = client.get(
                    "/v1/settlement/quote",
                    params={
                        "fromToken": "0xDIEM",
                        "toAsset": "USDC",
                        "amountOut": 1000000  # 1 USDC
                    }
                )
                
                assert response.status_code == 200
                data = response.json()
                
                # Check fallback indicators
                assert data["approx"] is True
                assert data["provider"] is None
                
                # Should still have risk hints
                assert "slippageBps" in data
                assert "poolTakeBps" in data
                # Fallback calculates slippage based on constant product
                assert isinstance(data["slippageBps"], int)
                assert isinstance(data["poolTakeBps"], int)


def test_settlement_preview_exceeds_slippage_cap(mock_market_data, monkeypatch):
    """Test that quotes exceeding slippage cap are rejected with detailed error."""
    # Set env var before importing app
    monkeypatch.setenv("SETTLEMENT_ENABLED", "true")
    
    # Mock aggregator with high slippage quote
    mock_agg = Mock()
    mock_agg.best_quote_exact_out.return_value = Mock(
        provider="uniswap_v2",
        path=["0xDIEM", "0xUSDC"],
        amount_in=2000000,  # 2 DIEM for 1 USDC = 50% slippage
        amount_out=1000000,
        gas_estimate=150000,
    )
    
    # Mock mid price to be 1:1
    mock_market_data._mid_price_from_reserves.return_value = 1.0
    
    with patch("libs.dex.providers.build_aggregator_from_env", return_value=mock_agg):
        with patch("services.marketdata.provider.MarketDataProvider", return_value=mock_market_data):
            # Import and reload app after setting env var
            import importlib
            import sys
            # Reload via sys.modules to ensure we're reloading the module object
            if "apps.broker_api.app" in sys.modules:
                importlib.reload(sys.modules["apps.broker_api.app"])
            import apps.broker_api.app as app_module
            
            from fastapi.testclient import TestClient
            client = TestClient(app_module.app)
            
            response = client.get(
                "/v1/settlement/quote",
                params={
                    "fromToken": "0xDIEM",
                    "toAsset": "USDC",
                    "amountOut": 1000000
                }
            )
            
            assert response.status_code == 409
            assert "slippage" in response.json()["detail"]
            assert "exceeds cap" in response.json()["detail"]
            # Should include the actual values
            assert "bps" in response.json()["detail"]


def test_settlement_preview_exceeds_pool_take_cap(mock_market_data, mock_etherscan_data, monkeypatch):
    """Test that quotes exceeding pool take cap are rejected with detailed error."""
    # Set env var before importing app
    monkeypatch.setenv("SETTLEMENT_ENABLED", "true")
    
    # Set small reserves to trigger pool take cap
    mock_etherscan_data["reserves"] = [1000000, 1000000]  # Small pool
    
    mock_agg = Mock()
    mock_agg.best_quote_exact_out.return_value = Mock(
        provider="uniswap_v2",
        path=["0xDIEM", "0xUSDC"],
        amount_in=100000,  # 10% of pool
        amount_out=100000,
        gas_estimate=150000,
    )
    
    # Mock reserve cap to be exceeded
    mock_market_data.reserve_cap_units.return_value = 5000  # Very small cap
    
    with patch("libs.dex.providers.build_aggregator_from_env", return_value=mock_agg):
        with patch("services.marketdata.provider.MarketDataProvider", return_value=mock_market_data):
            with patch("services.marketdata.etherscan_verify.get_cached_pair_info_for_tokens", 
                      return_value=mock_etherscan_data):
                monkeypatch.setenv("RISK_MAX_POOL_TAKE_BPS", "25")
                # Import and reload app module after setting env var
                import importlib
                import sys
                # Reload via sys.modules to ensure we're reloading the module object
                if "apps.broker_api.app" in sys.modules:
                    importlib.reload(sys.modules["apps.broker_api.app"])
                import apps.broker_api.app as app_module
                
                from fastapi.testclient import TestClient
                client = TestClient(app_module.app)
                
                response = client.get(
                    "/v1/settlement/quote",
                    params={
                        "fromToken": "0xDIEM",
                        "toAsset": "USDC",
                        "amountOut": 100000
                    }
                )
                
                assert response.status_code == 409
                assert "pool take cap" in response.json()["detail"]
                # Should show actual vs allowed values
                assert "bps >" in response.json()["detail"]
                assert "allowed" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
