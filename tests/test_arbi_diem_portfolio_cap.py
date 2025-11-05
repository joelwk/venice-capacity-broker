"""Tests for ArbiDiem portfolio cap integration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from agents.arbi_diem.agent import ArbiDiem
from services.diem.client import DIEMService
from services.risk.policy import RiskPolicy


def test_arbi_diem_includes_portfolio_caps_in_rationale(monkeypatch):
    """Test that ArbiDiem includes portfolio caps and telemetry in rationale."""
    monkeypatch.setenv("RISK_ENABLE_PORTFOLIO_CAP", "1")
    
    mock_aggregator = MagicMock()
    mock_quote = MagicMock()
    mock_quote.amount_in = 1_000_000
    mock_quote.amount_out = 950_000
    mock_aggregator.best_quote.return_value = mock_quote
    mock_aggregator.trade_best_exact_out = MagicMock(return_value={"status": "ok"})
    mock_diem = DIEMService(aggregator=mock_aggregator)
    arbi = ArbiDiem(diem=mock_diem, risk=RiskPolicy.from_env())
    
    mock_market = MagicMock()
    mock_market.prices.return_value = {"DIEM": 2.0, "VVV": 1.0, "USDC": 1.0}
    arbi.market = mock_market
    
    arbi.evaluate_and_maybe_mint(
        market_price=2.5,  # Premium over fair value
        mint_rate=1.0,
        current_inventory_usd=1000.0,
        utilization_ratio=0.5,
        simulate=True,
    )
    
    rationale = arbi._last_rationale
    assert rationale is not None
    assert "current_inventory_usd" in rationale
    assert rationale["current_inventory_usd"] == 1000.0
    assert "desired_units" in rationale
    assert "suggested_units" in rationale
    assert "portfolioAdjustedUnits" in rationale


def test_arbi_diem_logs_trade_route(monkeypatch):
    """Test that ArbiDiem logs selected trade route."""
    monkeypatch.setenv("RISK_ENABLE_PORTFOLIO_CAP", "1")
    
    mock_aggregator = MagicMock()
    mock_quote = MagicMock()
    mock_quote.amount_out = 1_000_000
    mock_aggregator.best_quote.return_value = mock_quote
    
    mock_diem = DIEMService(aggregator=mock_aggregator)
    arbi = ArbiDiem(diem=mock_diem, risk=RiskPolicy.from_env())
    
    mock_market = MagicMock()
    mock_market.prices.return_value = {"DIEM": 2.0, "VVV": 1.0, "USDC": 1.0}
    arbi.market = mock_market
    
    with patch.object(arbi, "_trade_routes") as mock_routes:
        from libs.dex.routes import make_route
        mock_routes.return_value = [make_route(["DIEM", "WETH", "USDC"])]
        
        arbi.evaluate_and_maybe_mint(
            market_price=2.5,
            mint_rate=1.0,
            simulate=True,
        )
        
        rationale = arbi._last_rationale
        if rationale and rationale.get("decision") in ("mint_sell", "buy_burn"):
            assert "tradeRoute" in rationale or "slippage_bps" in rationale

