"""Tests for DIEM burn eligibility checks.

Verifies that the system correctly handles the case where DIEM was purchased on DEX
(no locked sVVV) vs minted (has locked sVVV backing).
"""

from __future__ import annotations

from importlib import import_module
from unittest.mock import MagicMock


def test_can_burn_diem_with_locked_svvv(monkeypatch):
    """When wallet has locked sVVV, burn should be allowed."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")  # 1:1

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock _locked_svvv_for_wallet to return sufficient locked sVVV
    amount = 10**18  # 1 DIEM
    locked = 2 * 10**18  # 2 sVVV locked (more than needed)
    svc._locked_svvv_for_wallet = MagicMock(return_value=locked)

    result = svc.can_burn_diem(amount)

    assert result["can_burn"] is True
    assert result["locked_svvv"] == locked
    assert result["reason"] == "sufficient_locked_svvv"


def test_can_burn_diem_no_locked_svvv(monkeypatch):
    """When wallet has no locked sVVV (purchased DIEM), burn should be blocked."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock _locked_svvv_for_wallet to return zero (purchased DIEM)
    amount = 10**18
    svc._locked_svvv_for_wallet = MagicMock(return_value=0)

    result = svc.can_burn_diem(amount)

    assert result["can_burn"] is False
    assert result["locked_svvv"] == 0
    assert result["reason"] == "no_locked_svvv"
    assert "recommendation" in result
    assert "Sell" in result["recommendation"] or "DEX" in result["recommendation"]


def test_can_burn_diem_partial_locked_svvv(monkeypatch):
    """When wallet has some locked sVVV but not enough, partial burn info returned."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")  # 1:1

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock: trying to burn 10 DIEM but only 3 sVVV locked
    amount = 10 * 10**18  # 10 DIEM
    locked = 3 * 10**18  # Only 3 sVVV locked
    svc._locked_svvv_for_wallet = MagicMock(return_value=locked)

    result = svc.can_burn_diem(amount)

    assert result["can_burn"] is False
    assert result["locked_svvv"] == locked
    assert result["reason"] == "insufficient_locked_svvv"
    assert "max_burnable_diem" in result
    assert result["max_burnable_diem"] == 3 * 10**18  # Can only burn 3


def test_wallet_first_buy_and_burn_blocks_on_no_locked_svvv(monkeypatch):
    """wallet_first_buy_and_burn should block execution when no locked sVVV."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    # Explicitly disable skip mode to test error behavior
    monkeypatch.delenv("DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV", raising=False)

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock portfolio to have DIEM
    svc._portfolio_balances = MagicMock(
        return_value={"DIEM": {"units": 5 * 10**18, "decimals": 18}}
    )
    # Mock no locked sVVV
    svc._locked_svvv_for_wallet = MagicMock(return_value=0)

    # Attempt live execution (simulate=False)
    result = svc.wallet_first_buy_and_burn(
        diem_amount=2 * 10**18,
        simulate=False,
    )

    assert result["status"] == "error"
    assert result["burn"]["error"] == "no_locked_svvv"
    assert "recommendation" in result["burn"]
    assert result["internal"]["burn_eligibility"]["reason"] == "no_locked_svvv"


def test_wallet_first_buy_and_burn_blocks_on_locked_unknown(monkeypatch):
    """wallet_first_buy_and_burn should block when locked sVVV cannot be read."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    # Explicitly disable skip mode to test error behavior
    monkeypatch.delenv("DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV", raising=False)

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock portfolio to have DIEM
    svc._portfolio_balances = MagicMock(
        return_value={"DIEM": {"units": 2 * 10**18, "decimals": 18}}
    )
    # locked sVVV is unknown (contract query failed)
    svc._locked_svvv_for_wallet = MagicMock(return_value=None)

    result = svc.wallet_first_buy_and_burn(
        diem_amount=10**18,
        simulate=False,
    )

    assert result["status"] == "error"
    assert result["burn"]["error"] == "locked_svvv_unknown"
    assert (
        result["internal"]["burn_eligibility"]["reason"] == "cannot_query_locked_svvv"
    )


def test_wallet_first_buy_and_burn_allows_simulation(monkeypatch):
    """Simulation mode should proceed without locked sVVV check."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock portfolio to have DIEM
    svc._portfolio_balances = MagicMock(
        return_value={"DIEM": {"units": 5 * 10**18, "decimals": 18}}
    )
    # Don't set up locked sVVV mock - simulation shouldn't need it

    # Mock burn to return success in dry_run mode
    svc.burn = MagicMock(return_value={"status": "dry_run", "action": "burn"})

    # Attempt simulation (simulate=True)
    result = svc.wallet_first_buy_and_burn(
        diem_amount=2 * 10**18,
        simulate=True,
    )

    # Should succeed without checking locked sVVV
    assert result["status"] != "error"
    assert result["internal"]["used_wallet_diem"] == 2 * 10**18


def test_burn_skips_when_locked_unknown(monkeypatch):
    """Direct burn should fail fast when locked sVVV cannot be determined."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    svc._locked_svvv_for_wallet = MagicMock(return_value=None)

    result = svc.burn(amount=10**18, dry_run=False)

    assert result["status"] == "error"
    assert result["error"] == "locked_svvv_unknown"


def test_wallet_first_skips_gracefully_with_config(monkeypatch):
    """wallet_first_buy_and_burn returns skip status when DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV=1."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    # Enable graceful skip mode
    monkeypatch.setenv("DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV", "1")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock portfolio to have DIEM
    svc._portfolio_balances = MagicMock(
        return_value={"DIEM": {"units": 5 * 10**18, "decimals": 18}}
    )
    # Mock no locked sVVV (purchased DIEM)
    svc._locked_svvv_for_wallet = MagicMock(return_value=0)

    result = svc.wallet_first_buy_and_burn(
        diem_amount=2 * 10**18,
        simulate=False,
    )

    # With skip config enabled, status should be "skipped" not "error"
    assert result["status"] == "skipped"
    assert result["burn"]["status"] == "skipped"
    assert result["burn"]["reason"] == "purchased_diem_no_locked_svvv"
    assert "recommendation" in result["burn"]


def test_burn_skips_balance_check_when_flag_set(monkeypatch):
    """burn() should bypass balance check when skip_balance_check=True."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    monkeypatch.setenv(
        "DIEM_BURN_CUSTODY_AWARE", "0"
    )  # Disable custody check for simpler test

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock portfolio to return insufficient balance
    svc._portfolio_balances = MagicMock(
        return_value={"DIEM": {"units": 0, "decimals": 18}}
    )
    # Mock locked sVVV to be sufficient
    svc._locked_svvv_for_wallet_safe = MagicMock(return_value=10 * 10**18)
    svc._query_mint_rate_onchain_safe = MagicMock(return_value=10**18)
    # Mock the actions burn call
    mock_actions = MagicMock()
    mock_actions.burn = MagicMock(return_value={"status": "sent", "tx_hash": "0x123"})
    svc._get_actions = MagicMock(return_value=mock_actions)

    # With skip_balance_check=False, should fail balance check
    result_no_skip = svc.burn(10**18, skip_balance_check=False, dry_run=False)
    assert result_no_skip["status"] == "error"
    assert result_no_skip.get("reason") == "insufficient_diem_balance"

    # With skip_balance_check=True, should bypass balance check and proceed
    result_skip = svc.burn(10**18, skip_balance_check=True, dry_run=False)
    assert result_skip["status"] == "sent"
    assert "tx_hash" in result_skip


def test_wallet_first_skips_gracefully_on_unknown_svvv_with_config(monkeypatch):
    """wallet_first_buy_and_burn returns skip when locked sVVV query fails and config is set."""
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv("WALLET_FIRST_ARB_ENABLE", "1")
    monkeypatch.setenv("DIEM_MINT_RATE_SVVV_PER_DIEM", "1000000000000000000")
    # Enable graceful skip mode
    monkeypatch.setenv("DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV", "1")

    svc_mod = import_module("services.diem.client")
    svc = svc_mod.DIEMService(aggregator=None)

    # Mock portfolio to have DIEM
    svc._portfolio_balances = MagicMock(
        return_value={"DIEM": {"units": 2 * 10**18, "decimals": 18}}
    )
    # locked sVVV query returns None (failed)
    svc._locked_svvv_for_wallet = MagicMock(return_value=None)

    result = svc.wallet_first_buy_and_burn(
        diem_amount=10**18,
        simulate=False,
    )

    # With skip config enabled, status should be "skipped" not "error"
    assert result["status"] == "skipped"
    assert result["burn"]["status"] == "skipped"
    assert result["burn"]["reason"] == "locked_svvv_query_failed"
