"""Tests for AerodromeCL provider exact-out support."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from libs.dex.routes import make_route

# Valid Ethereum addresses for testing
DIEM_ADDR = "0xf4D97F2dA56e8c3098F3a8D538dB630a2606a024"
USDC_ADDR = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
POOL_ADDR = "0x1a9a8B26D8E8B8bD9B3b8eB8eD3b8eB8eD3b8eB8"
ROUTER_ADDR = "0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5"
WETH_ADDR = "0x4200000000000000000000000000000000000006"


@pytest.fixture(autouse=True)
def _mock_env(monkeypatch):
    """Configure required environment for AerodromeCL."""
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", DIEM_ADDR)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", USDC_ADDR)
    monkeypatch.setenv("DIEM_USDC_POOL_ADDRESS", POOL_ADDR)
    monkeypatch.setenv("AERODROME_CL_ROUTER_ADDRESS", ROUTER_ADDR)
    monkeypatch.setenv("DIEM_USDC_TICK_SPACING", "100")


@pytest.fixture
def _mock_web3():
    """Mock web3 for testing without RPC."""
    with patch("libs.dex.providers.get_web3") as mock_w3:
        mock_instance = MagicMock()
        mock_w3.return_value = mock_instance
        yield mock_instance


@pytest.mark.usefixtures("_mock_web3")
class TestAerodromeCLProviderExactOut:
    """Test AerodromeCL exact-out capabilities."""

    def test_supports_exact_out_is_true(self):
        """AerodromeCLDexProvider should declare supports_exact_out=True."""
        with patch("libs.dex.providers.get_contract") as mock_contract:
            mock_contract.return_value = MagicMock()
            from libs.dex.providers import AerodromeCLDexProvider

            provider = AerodromeCLDexProvider(
                router_address=ROUTER_ADDR,
                pool_address=POOL_ADDR,
                tick_spacing=100,
            )
            assert provider.supports_exact_out is True

    def test_quote_exact_out_returns_quote_for_diem_usdc_route(self):
        """quote_exact_out should return a quote for DIEM/USDC route."""
        with patch("libs.dex.providers.get_contract") as mock_contract:
            mock_contract.return_value = MagicMock()

            from libs.dex.providers import AerodromeCLDexProvider

            provider = AerodromeCLDexProvider(
                router_address=ROUTER_ADDR,
                pool_address=POOL_ADDR,
                tick_spacing=100,
            )

            # Mock the slot0 quote function
            mock_quote_result = MagicMock()
            mock_quote_result.amount_in = 100_000_000  # 100 USDC
            mock_quote_result.amount_out = (
                1_000_000  # 1 DIEM (0.001 DIEM in 18 decimals)
            )
            mock_quote_result.provider = "aerodrome_cl"

            with patch(
                "libs.dex.diem_fallbacks.diem_usdc_slot0_quote_exact_out",
                return_value=mock_quote_result,
            ):
                route = make_route([USDC_ADDR, DIEM_ADDR])
                quote = provider.quote_exact_out(1_000_000, route)

                assert quote is not None
                assert quote.amount_in > 0
                assert quote.provider == "aerodrome_cl"

    def test_quote_exact_out_returns_none_for_non_cl_route(self):
        """quote_exact_out should return None for non-DIEM/USDC routes."""
        with patch("libs.dex.providers.get_contract") as mock_contract:
            mock_contract.return_value = MagicMock()

            from libs.dex.providers import AerodromeCLDexProvider

            provider = AerodromeCLDexProvider(
                router_address=ROUTER_ADDR,
                pool_address=POOL_ADDR,
                tick_spacing=100,
            )

            route = make_route([WETH_ADDR, USDC_ADDR])  # Not DIEM/USDC
            quote = provider.quote_exact_out(1_000_000, route)

            assert quote is None

    def test_has_trade_exact_out_method(self):
        """AerodromeCLDexProvider should have trade_exact_out method."""
        with patch("libs.dex.providers.get_contract") as mock_contract:
            mock_contract.return_value = MagicMock()

            from libs.dex.providers import AerodromeCLDexProvider

            provider = AerodromeCLDexProvider(
                router_address=ROUTER_ADDR,
                pool_address=POOL_ADDR,
                tick_spacing=100,
            )

            assert hasattr(provider, "trade_exact_out")
            assert callable(provider.trade_exact_out)


@pytest.mark.usefixtures("_mock_web3")
class TestAerodromeCLInspection:
    """Test inspection flow handles aerodrome_cl properly."""

    def test_inspect_aerodrome_cl_not_unsupported(self, monkeypatch):
        """aerodrome_cl should not be marked as 'unsupported' in inspection."""
        monkeypatch.setenv("DEX_PROVIDERS", "aerodrome_cl")
        monkeypatch.setenv("DEX_EXEC_PROVIDERS", "aerodrome_cl")

        with patch("libs.dex.providers.get_contract") as mock_contract:
            mock_router = MagicMock()
            mock_contract.return_value = mock_router

            from libs.dex.providers import AerodromeCLDexProvider, DexAggregator

            provider = AerodromeCLDexProvider(
                router_address=ROUTER_ADDR,
                pool_address=POOL_ADDR,
                tick_spacing=100,
            )

            # Mock quote to return a valid result
            mock_quote = MagicMock()
            mock_quote.amount_in = 1_000_000
            mock_quote.amount_out = 219_000_000
            provider.quote = MagicMock(return_value=mock_quote)

            agg = DexAggregator([provider])
            route = make_route([DIEM_ADDR, USDC_ADDR])

            inspections = agg._inspect_route(route, 1_000_000, "exact_in")

            # Find aerodrome_cl result
            cl_results = [i for i in inspections if i.get("provider") == "aerodrome_cl"]
            assert len(cl_results) > 0

            cl_result = cl_results[0]
            assert cl_result.get("status") != "unsupported", (
                f"aerodrome_cl should not be 'unsupported', got: {cl_result}"
            )
