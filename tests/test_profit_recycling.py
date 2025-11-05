"""Tests for profit recycling service."""

from __future__ import annotations

from unittest.mock import MagicMock

from services.treasury.recycle import recycle_profits_to_stake


def test_recycle_profits_dry_run():
    """Test profit recycling in dry-run mode."""
    mock_aggregator = MagicMock()
    mock_quote = MagicMock()
    mock_quote.amount_out = 1_000_000_000_000_000_000  # 1 VVV
    mock_aggregator.best_quote.return_value = mock_quote
    
    mock_stake_master = MagicMock()
    
    usdc_wei = 1_000_000  # 1 USDC (6 decimals)
    
    result = recycle_profits_to_stake(
        amount_usdc_wei=usdc_wei,
        aggregator=mock_aggregator,
        stake_master=mock_stake_master,
        dry_run=True,
    )
    
    assert result["status"] == "dry_run"
    assert result["swap_result"] is not None
    assert result["swap_result"]["preview"] is True
    assert "vvv_out" in result["swap_result"]
    assert mock_stake_master.stake_vvv.called is False


def test_recycle_profits_skips_below_minimum():
    """Test that recycling skips when amount is below minimum."""
    mock_aggregator = MagicMock()
    mock_stake_master = MagicMock()
    
    usdc_wei = 10_000  # 0.01 USDC (below default 10 USD minimum)
    
    result = recycle_profits_to_stake(
        amount_usdc_wei=usdc_wei,
        aggregator=mock_aggregator,
        stake_master=mock_stake_master,
        min_stake_usd=10.0,
        dry_run=True,
    )
    
    assert result["status"] == "skipped"
    assert len(result["errors"]) > 0
    assert "below minimum" in " ".join(result["errors"]).lower()


def test_recycle_profits_skips_when_no_quote():
    """Test that recycling skips when no quote available."""
    mock_aggregator = MagicMock()
    mock_aggregator.best_quote.return_value = None
    
    mock_stake_master = MagicMock()
    
    usdc_wei = 10_000_000  # 10 USDC
    
    result = recycle_profits_to_stake(
        amount_usdc_wei=usdc_wei,
        aggregator=mock_aggregator,
        stake_master=mock_stake_master,
        dry_run=True,
    )
    
    assert result["status"] == "error"
    assert "no quote" in " ".join(result["errors"]).lower()

