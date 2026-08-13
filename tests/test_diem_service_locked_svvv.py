"""Unit tests for DIEM locked sVVV and mint rate queries.

Tests the contract interaction layer for querying locked sVVV balances
and mint rates from the sVVV staking contract (VVV_STAKING_ADDRESS).

Note: The deployed StakingV2 contract provides:
- balanceOf(address): Total sVVV balance (including locked)
- balanceOfUnlocked(address): sVVV available for unstaking
- getDiemAmountOut(uint256): DIEM amount for given sVVV input

Locked sVVV = balanceOf - balanceOfUnlocked
"""

from __future__ import annotations

from importlib import import_module
from unittest.mock import MagicMock, patch

from web3 import Web3


def test_locked_svvv_for_wallet_success(monkeypatch):
    """Test _locked_svvv_for_wallet returns correct locked amount."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock Web3 and contract
    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    # Total balance = 10 sVVV, Unlocked = 5 sVVV, Locked = 5 sVVV
    mock_contract.functions.balanceOf.return_value.call.return_value = 10 * 10**18
    mock_contract.functions.balanceOfUnlocked.return_value.call.return_value = (
        5 * 10**18
    )

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._locked_svvv_for_wallet()

    assert result == 5 * 10**18
    # Verify both functions were called with the address
    checksummed = Web3.to_checksum_address("0xabc0000000000000000000000000000000000001")
    mock_contract.functions.balanceOf.assert_called_once_with(checksummed)
    mock_contract.functions.balanceOfUnlocked.assert_called_once_with(checksummed)


def test_locked_svvv_for_wallet_with_address_parameter(monkeypatch):
    """Test _locked_svvv_for_wallet uses provided address parameter."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    test_address = "0x1234567890123456789012345678901234567890"
    # Total = 20, Unlocked = 8, Locked = 12
    expected_locked = 12 * 10**18

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_contract.functions.balanceOf.return_value.call.return_value = 20 * 10**18
    mock_contract.functions.balanceOfUnlocked.return_value.call.return_value = (
        8 * 10**18
    )

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._locked_svvv_for_wallet(wallet_address=test_address)

    assert result == expected_locked
    checksummed = Web3.to_checksum_address(test_address)
    mock_contract.functions.balanceOf.assert_called_once_with(checksummed)


def test_locked_svvv_for_wallet_all_unlocked(monkeypatch):
    """Test _locked_svvv_for_wallet returns 0 when all sVVV is unlocked."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )
    monkeypatch.setenv("TREASURY_ADDRESS", "0xabc0000000000000000000000000000000000001")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    # Total = Unlocked = 10 sVVV, Locked = 0
    mock_contract.functions.balanceOf.return_value.call.return_value = 10 * 10**18
    mock_contract.functions.balanceOfUnlocked.return_value.call.return_value = (
        10 * 10**18
    )

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._locked_svvv_for_wallet()

    assert result == 0


def test_locked_svvv_for_wallet_contract_call_raises(monkeypatch):
    """Test _locked_svvv_for_wallet returns None when contract call raises."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_contract.functions.balanceOf.return_value.call.side_effect = Exception(
        "Contract call failed"
    )

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._locked_svvv_for_wallet()

    assert result is None


def test_locked_svvv_for_wallet_no_staking_address(monkeypatch):
    """Test _locked_svvv_for_wallet returns None when VVV_STAKING_ADDRESS not set."""
    monkeypatch.delenv("VVV_STAKING_ADDRESS", raising=False)

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    result = svc._locked_svvv_for_wallet()

    assert result is None


def test_locked_svvv_for_wallet_no_web3(monkeypatch):
    """Test _locked_svvv_for_wallet returns None when Web3 unavailable."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    with patch("services.diem.client.DIEMService._get_web3", return_value=None):
        result = svc._locked_svvv_for_wallet()

    assert result is None


def test_query_mint_rate_onchain_success(monkeypatch):
    """Test _query_mint_rate_onchain returns mint rate using getDiemAmountOut."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # 1 sVVV (1e18) gives 0.5 DIEM (0.5e18) => rate = 2e18 sVVV per DIEM
    diem_output = 5 * 10**17  # 0.5 DIEM
    expected_rate = 2 * 10**18  # 2 sVVV per DIEM

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_contract.functions.getDiemAmountOut.return_value.call.return_value = (
        diem_output
    )

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._query_mint_rate_onchain()

    assert result == expected_rate
    assert isinstance(result, int)
    # Verify getDiemAmountOut was called with 1e18 (1 sVVV)
    mock_contract.functions.getDiemAmountOut.assert_called_once_with(10**18)


def test_query_mint_rate_onchain_one_to_one(monkeypatch):
    """Test _query_mint_rate_onchain for 1:1 conversion rate."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # 1 sVVV gives 1 DIEM => rate = 1e18 sVVV per DIEM
    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_contract.functions.getDiemAmountOut.return_value.call.return_value = 10**18

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._query_mint_rate_onchain()

    assert result == 10**18


def test_query_mint_rate_onchain_zero_output_returns_none(monkeypatch):
    """Test _query_mint_rate_onchain returns None when getDiemAmountOut returns 0."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_contract.functions.getDiemAmountOut.return_value.call.return_value = 0

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._query_mint_rate_onchain()

    assert result is None


def test_query_mint_rate_onchain_contract_call_raises(monkeypatch):
    """Test _query_mint_rate_onchain returns None when contract call raises."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    mock_w3 = MagicMock()
    mock_contract = MagicMock()
    mock_contract.functions.getDiemAmountOut.return_value.call.side_effect = Exception(
        "Contract call failed"
    )

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract", return_value=mock_contract
        ):
            result = svc._query_mint_rate_onchain()

    assert result is None


def test_query_mint_rate_onchain_no_staking_address(monkeypatch):
    """Test _query_mint_rate_onchain returns None when VVV_STAKING_ADDRESS not set."""
    monkeypatch.delenv("VVV_STAKING_ADDRESS", raising=False)

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    result = svc._query_mint_rate_onchain()

    assert result is None


def test_query_mint_rate_onchain_no_web3(monkeypatch):
    """Test _query_mint_rate_onchain returns None when Web3 unavailable."""
    monkeypatch.setenv(
        "VVV_STAKING_ADDRESS", "0x321b7ff75154472B18EDb199033fF4D116F340Ff"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    with patch("services.diem.client.DIEMService._get_web3", return_value=None):
        result = svc._query_mint_rate_onchain()

    assert result is None


def test_query_mint_rate_onchain_invalid_address_handles_gracefully(monkeypatch):
    """Test _query_mint_rate_onchain handles invalid address without raising."""
    monkeypatch.setenv("VVV_STAKING_ADDRESS", "invalid_address")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    mock_w3 = MagicMock()

    with patch("services.diem.client.DIEMService._get_web3", return_value=mock_w3):
        with patch(
            "libs.agentkit_ext.web3_utils.get_contract",
            side_effect=ValueError("Invalid address"),
        ):
            result = svc._query_mint_rate_onchain()

    # Should return None, not raise
    assert result is None
