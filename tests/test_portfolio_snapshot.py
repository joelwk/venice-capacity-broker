"""Tests for portfolio inventory service."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services.portfolio.inventory import PortfolioInventory, PortfolioSnapshot


def test_portfolio_snapshot_empty_when_wallet_unavailable():
    """Test that snapshot returns empty when wallet provider unavailable."""
    inventory = PortfolioInventory()

    with patch("services.portfolio.inventory.describe_treasury_portfolio", None):
        snapshot = inventory.snapshot()
        assert snapshot.inventory_usd == 0.0
        assert len(snapshot.errors) > 0


def test_portfolio_snapshot_computes_usd_valuation():
    """Test that snapshot computes USD valuations correctly."""
    mock_snapshot = {
        "address": "0x123",
        "balances": {
            "USDC": {"units": 1_000_000, "decimals": 6},  # 1 USDC
            "VVV": {"units": 1_000_000_000_000_000_000, "decimals": 18},  # 1 VVV
            "DIEM": {"units": 500_000_000_000_000_000, "decimals": 18},  # 0.5 DIEM
        },
        "errors": [],
    }

    mock_prices = {"USDC": 1.0, "VVV": 2.0, "DIEM": 3.0}

    with (
        patch(
            "services.portfolio.inventory.describe_treasury_portfolio",
            return_value=mock_snapshot,
        ),
        patch("services.portfolio.inventory.MarketDataProvider") as mock_mdp,
    ):
        mock_provider = MagicMock()
        mock_provider.prices.return_value = mock_prices
        mock_mdp.return_value = mock_provider

        inventory = PortfolioInventory(marketdata_provider=mock_provider)
        snapshot = inventory.snapshot()

        assert snapshot.address == "0x123"
        assert snapshot.per_asset_usd["USDC"] == 1.0
        assert snapshot.per_asset_usd["VVV"] == 2.0
        assert snapshot.per_asset_usd["DIEM"] == 1.5  # 0.5 * 3.0
        assert abs(snapshot.inventory_usd - 4.5) < 1e-6  # 1.0 + 2.0 + 1.5


def test_portfolio_balance_helpers():
    """Test portfolio balance helper methods."""
    snapshot = PortfolioSnapshot(
        address="0x123",
        per_asset_usd={"USDC": 100.0, "VVV": 200.0, "DIEM": 50.0},
        inventory_usd=350.0,
    )

    inventory = PortfolioInventory()

    assert inventory.get_usdc_balance(snapshot) == 100.0
    assert inventory.get_vvv_balance(snapshot) == 200.0
    assert inventory.get_diem_balance(snapshot) == 50.0
