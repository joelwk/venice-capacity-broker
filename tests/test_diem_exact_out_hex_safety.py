"""
Test DIEM exact_out hex-string safety for Uniswap V3 routes.

This test verifies that:
1. Routes with @fee suffixes in token addresses are properly normalized before Web3 calls
2. trade_exact_out does not fail with "must be a hex string" errors
3. Address normalization strips @fee suffixes before checksum conversion
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from libs.dex.providers import UniswapV3DexProvider
from libs.dex.routes import make_route


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    """Set up environment variables for DIEM bridge route."""
    monkeypatch.setenv(
        "UNISWAP_V3_ROUTER_ADDRESS", "0x2626664c2603336E57B271c5C0b26F42126e2e6"
    )
    monkeypatch.setenv(
        "UNISWAP_V3_QUOTER_ADDRESS", "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    )
    monkeypatch.setenv(
        "BASE_RPC_URL", "https://base-mainnet.g.alchemy.com/v2/demo"
    )  # gitleaks:allow demo API key
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )


@pytest.fixture
def uniswap_v3_provider():
    """Create UniswapV3 provider with test config."""
    router = os.getenv(
        "UNISWAP_V3_ROUTER_ADDRESS", "0x2626664c2603336E57B271c5C0b26F42126e2e6"
    )
    quoter = os.getenv(
        "UNISWAP_V3_QUOTER_ADDRESS", "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
    )
    return UniswapV3DexProvider(router, quoter, default_fee=3000)


def test_route_with_fee_suffix_normalizes_addresses():
    """Test that routes with @fee suffixes normalize addresses correctly."""
    from libs.dex.routes import _normalize_address

    # Address with @3000 suffix (like in the error message)
    addr_with_suffix = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913@3000"
    normalized = _normalize_address(addr_with_suffix)

    # Should strip @3000 and return clean hex
    assert normalized == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    assert "@" not in normalized
    assert normalized.startswith("0x")


def test_make_route_strips_fee_suffixes_from_addresses():
    """Test that make_route normalizes addresses even when they contain @fee suffixes."""
    # Create route with addresses that have @fee suffixes (simulating TRADE_PATH parsing)
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    usdc_addr_with_suffix = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913@3000"

    # make_route should normalize addresses and extract fees
    route = make_route([diem_addr, vvv_addr, usdc_addr_with_suffix], fees=[None, 3000])

    # Verify addresses are normalized (no @ suffixes)
    assert "@" not in route.tokens[0]
    assert "@" not in route.tokens[1]
    assert "@" not in route.tokens[2]

    # Verify fees are correctly assigned to hops
    assert route.hops[0].fee is None
    assert route.hops[1].fee == 3000


def test_trade_exact_out_normalizes_token_addresses(uniswap_v3_provider):
    """Test that trade_exact_out normalizes addresses before Web3.to_checksum_address."""
    from libs.dex.routes import RouteHop, RoutePlan

    # Create a route where token addresses might have @fee suffixes
    # This simulates a route that was constructed from TRADE_PATH with @3000 annotation
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf"
    usdc_addr_with_suffix = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913@3000"

    # Create route with address containing @fee suffix
    hops = [
        RouteHop(token_in=diem_addr, token_out=vvv_addr, fee=None),
        RouteHop(token_in=vvv_addr, token_out=usdc_addr_with_suffix, fee=3000),
    ]
    route = RoutePlan(tuple(hops))

    # Mock Web3 and contract calls to avoid actual on-chain calls
    with (
        patch("libs.dex.providers.get_web3") as mock_web3,
        patch("libs.dex.providers.get_contract") as mock_contract,
        patch("libs.dex.providers.send_tx") as mock_send_tx,
    ):
        mock_w3 = MagicMock()
        mock_web3.return_value = mock_w3

        mock_erc20 = MagicMock()
        mock_erc20.functions.allowance.return_value.call.return_value = 0

        mock_router = MagicMock()
        mock_router.functions.exactOutput.return_value.build_transaction.return_value = {
            "data": "0x1234"
        }

        mock_contract.side_effect = lambda w3, addr, abi: (
            mock_erc20 if abi == "erc20.json" else mock_router
        )
        mock_send_tx.return_value = "0xabcd"

        # This should not raise "must be a hex string" error
        # The address normalization should strip @3000 before Web3.to_checksum_address
        result = uniswap_v3_provider.trade_exact_out(
            amount_out=1000000000000000000,  # 1 DIEM (18 decimals)
            max_amount_in=2000000000,  # Max 2000 USDC (6 decimals)
            route=route,
        )

        # Verify result structure
        assert "tx_hash" in result
        assert "provider" in result
        assert result["provider"] == "uniswap_v3"

        # Verify that get_contract was called with normalized addresses (no @ suffix)
        contract_calls = [call[0][1] for call in mock_contract.call_args_list]
        for addr in contract_calls:
            if isinstance(addr, str):
                assert "@" not in addr, f"Address {addr} should not contain @ suffix"


def test_ensure_allowance_normalizes_token_address(uniswap_v3_provider):
    """Test that _ensure_allowance normalizes token address before Web3 operations."""
    from unittest.mock import MagicMock, patch

    token_with_suffix = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913@3000"

    with (
        patch("libs.dex.providers.get_contract") as mock_contract,
        patch("libs.dex.providers.encode_contract_call") as mock_encode,
        patch("libs.dex.providers.send_tx") as mock_send_tx,
    ):
        mock_erc20 = MagicMock()
        mock_erc20.functions.allowance.return_value.call.return_value = 0
        mock_contract.return_value = mock_erc20
        mock_encode.return_value = "0xabcd"
        mock_send_tx.return_value = "0x1234"

        # This should not raise "must be a hex string" error
        uniswap_v3_provider._ensure_allowance(
            token=token_with_suffix,
            owner="0xabc0000000000000000000000000000000000001",
            spender="0x2626664c2603336E57B271c5C0b26F42126e2e6",
            required=1000000,
        )

        # Verify get_contract was called with normalized address
        call_args = mock_contract.call_args[0]
        normalized_addr = call_args[1]
        assert "@" not in normalized_addr, (
            f"Address {normalized_addr} should not contain @ suffix"
        )

        # Verify send_tx was called with normalized address
        send_tx_addr = mock_send_tx.call_args[0][0]
        assert "@" not in send_tx_addr, (
            f"Address {send_tx_addr} should not contain @ suffix"
        )


def test_diem_bridge_route_exact_out_hex_safety(uniswap_v3_provider):
    """
    Integration test: Verify DIEM→VVV→USDC bridge route works without hex-string errors.

    This test uses the exact route configuration from the runtime logs that caused
    the "must be a hex string" error.
    """
    # Route from runtime logs: DIEM→VVV→USDC with fees [3000, 3000]
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    # Create route (addresses should be normalized even if they had @fee suffixes)
    route = make_route([diem_addr, vvv_addr, usdc_addr], fees=[None, 3000])

    # Verify route is properly normalized
    assert "@" not in route.tokens[0]
    assert "@" not in route.tokens[1]
    assert "@" not in route.tokens[2]

    # Mock Web3 operations to avoid actual on-chain calls
    with (
        patch("libs.dex.providers.get_web3") as mock_web3,
        patch("libs.dex.providers.get_contract") as mock_contract,
        patch("libs.dex.providers.send_tx") as mock_send_tx,
    ):
        mock_w3 = MagicMock()
        mock_web3.return_value = mock_w3

        mock_erc20 = MagicMock()
        mock_erc20.functions.allowance.return_value.call.return_value = 0

        mock_router = MagicMock()
        mock_router.functions.exactOutput.return_value.build_transaction.return_value = {
            "data": "0x1234"
        }

        def contract_side_effect(w3, addr, abi):
            if abi == "erc20.json":
                return mock_erc20
            return mock_router

        mock_contract.side_effect = contract_side_effect
        mock_send_tx.return_value = "0xabcd"

        # This should execute without "must be a hex string" error
        result = uniswap_v3_provider.trade_exact_out(
            amount_out=14975148964919950,  # Amount from runtime logs
            max_amount_in=2000000000,  # Max input
            route=route,
        )

        assert result is not None
        assert "tx_hash" in result

        # Verify all addresses passed to Web3 operations are normalized
        for call in mock_contract.call_args_list:
            addr = call[0][1]
            if isinstance(addr, str):
                assert "@" not in addr, f"Address {addr} should not contain @ suffix"
