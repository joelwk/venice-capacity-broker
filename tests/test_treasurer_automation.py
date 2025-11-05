"""Tests for AI Treasurer automation with ReAct hooks."""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

from agents.ai_treasurer.agent import AITreasurer


def test_treasurer_execute_blocked_without_quorum():
    """Test that treasurer execution is blocked without quorum approval."""
    os.environ["TREASURER_ENABLE_AUTOMATION"] = "1"
    
    treasurer = AITreasurer()
    
    result = treasurer.execute(
        thought="Test action",
        action="recycle_profits",
        quorum_approved=False,
        reflex_ok=True,
        dry_run=False,
    )
    
    assert result["status"] == "blocked"
    assert "quorum" in " ".join(result["errors"]).lower()


def test_treasurer_execute_blocked_by_reflex():
    """Test that treasurer execution is blocked by reflex guardian."""
    os.environ["TREASURER_ENABLE_AUTOMATION"] = "1"
    
    treasurer = AITreasurer()
    
    result = treasurer.execute(
        thought="Test action",
        action="recycle_profits",
        quorum_approved=True,
        reflex_ok=False,
        dry_run=False,
    )
    
    assert result["status"] == "blocked"
    assert "reflex" in " ".join(result["errors"]).lower()


def test_treasurer_execute_recycle_profits_dry_run():
    """Test treasurer recycle profits execution in dry-run."""
    os.environ["TREASURER_ENABLE_AUTOMATION"] = "1"
    os.environ["TREASURER_MIN_ACTION_USD"] = "10.0"
    
    treasurer = AITreasurer()
    
    portfolio_snapshot = {
        "perAssetUsd": {"USDC": 100.0, "VVV": 200.0},
        "inventoryUsd": 300.0,
    }
    
    mock_aggregator = MagicMock()
    mock_quote = MagicMock()
    mock_quote.amount_out = 1_000_000_000_000_000_000
    mock_aggregator.best_quote.return_value = mock_quote
    
    mock_stake_master = MagicMock()
    
    result = treasurer.execute(
        thought="Recycle USDC profits to VVV stake",
        action="recycle_profits",
        portfolio_snapshot=portfolio_snapshot,
        quorum_approved=True,
        reflex_ok=True,
        dry_run=True,
        aggregator=mock_aggregator,
        stake_master=mock_stake_master,
    )
    
    assert result["status"] in ("dry_run", "completed")
    assert result["action"] == "recycle_profits"
    assert result["observation"] is not None


def test_treasurer_execute_skips_below_minimum():
    """Test that treasurer skips actions below minimum USD threshold."""
    os.environ["TREASURER_ENABLE_AUTOMATION"] = "1"
    os.environ["TREASURER_MIN_ACTION_USD"] = "100.0"
    
    treasurer = AITreasurer()
    
    portfolio_snapshot = {
        "perAssetUsd": {"USDC": 50.0},  # Below minimum
        "inventoryUsd": 50.0,
    }
    
    result = treasurer.execute(
        thought="Recycle small profits",
        action="recycle_profits",
        portfolio_snapshot=portfolio_snapshot,
        quorum_approved=True,
        reflex_ok=True,
        dry_run=True,
    )
    
    assert result["status"] == "skipped"
    assert "below minimum" in " ".join(result["errors"]).lower()


def test_treasurer_rebalance_computation():
    """Test that treasurer rebalance computes correct delta."""
    treasurer = AITreasurer()
    
    avg_daily = 100.0
    current = 120.0
    delta = treasurer.rebalance(avg_daily, current)
    
    target = avg_daily * 1.5  # 150.0
    expected_delta = target - current  # 30.0
    assert abs(delta - expected_delta) < 1e-6

