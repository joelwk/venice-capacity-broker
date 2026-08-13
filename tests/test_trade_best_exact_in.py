"""Tests for trade_best_exact_in aggregator method (exact-in execution fallback)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from libs.dex.providers import DexAggregator, Quote

DIEM_BASE_UNITS = 1_000_000
QUOTED_USDC_OUT = 100_000_000
SLIPPAGE_BPS = 150
EXPECTED_MIN_OUT = 98_500_000


class TestTradeBestExactIn:
    """Test the trade_best_exact_in method for exact-in execution fallback."""

    def test_trade_best_exact_in_executes_with_slippage(self):
        """Executes using best quote with slippage protection."""
        # Create mock provider
        mock_provider = MagicMock()
        mock_provider.name = "uniswap_v2"
        mock_provider.trade = MagicMock(
            return_value={"tx_hash": "0xabc", "provider": "uniswap_v2"}
        )

        # Create aggregator with mock provider
        agg = DexAggregator(providers=[mock_provider])

        # Mock best_quote to return a valid quote
        mock_quote = Quote(
            provider="uniswap_v2",
            path=["0xDIEM", "0xUSDC"],
            amount_in=DIEM_BASE_UNITS,  # 1 DIEM
            amount_out=QUOTED_USDC_OUT,  # 100 USDC (6 decimals)
        )
        with patch.object(agg, "best_quote", return_value=mock_quote):
            result = agg.trade_best_exact_in(
                amount_in=DIEM_BASE_UNITS,
                min_out_bps=SLIPPAGE_BPS,  # 1.5% slippage allowed
                route=["0xDIEM", "0xUSDC"],
            )

        assert result["provider"] == "uniswap_v2"
        assert result["tx_hash"] == "0xabc"

        # Verify trade was called with correct min_out (98.5% of quoted output)
        mock_provider.trade.assert_called_once()
        call_args = mock_provider.trade.call_args
        # min_out should be 100_000_000 * (10000 - 150) / 10000 = 98_500_000
        assert call_args[0][1] == EXPECTED_MIN_OUT  # min_out

    def test_trade_best_exact_in_raises_on_no_quote(self):
        """trade_best_exact_in should raise RuntimeError when no quote available."""
        mock_provider = MagicMock()
        mock_provider.name = "uniswap_v2"

        agg = DexAggregator(providers=[mock_provider])

        # Mock best_quote to return None (no quotes available)
        with (
            patch.object(agg, "best_quote", return_value=None),
            pytest.raises(RuntimeError, match="No executable exact-in quotes"),
        ):
            agg.trade_best_exact_in(
                amount_in=DIEM_BASE_UNITS,
                min_out_bps=SLIPPAGE_BPS,
                route=["0xDIEM", "0xUSDC"],
            )

    def test_trade_best_exact_in_requires_route(self):
        """trade_best_exact_in should raise ValueError when no route provided."""
        mock_provider = MagicMock()
        mock_provider.name = "uniswap_v2"

        agg = DexAggregator(providers=[mock_provider])

        with pytest.raises(ValueError, match="route/path is required"):
            agg.trade_best_exact_in(amount_in=DIEM_BASE_UNITS, min_out_bps=SLIPPAGE_BPS)

    def test_trade_best_exact_in_uses_execution_providers_only(self):
        """Uses execution providers, not quote-only providers."""
        # Create mock providers
        exec_provider = MagicMock()
        exec_provider.name = "uniswap_v2"
        exec_provider.trade = MagicMock(
            return_value={"tx_hash": "0xdef", "provider": "uniswap_v2"}
        )

        quote_only_provider = MagicMock()
        quote_only_provider.name = "composite"

        agg = DexAggregator(providers=[exec_provider, quote_only_provider])
        # Simulate DEX_EXEC_PROVIDERS setting
        agg._execution_provider_names = {"uniswap_v2"}

        mock_quote = Quote(
            provider="uniswap_v2",
            path=["0xDIEM", "0xUSDC"],
            amount_in=DIEM_BASE_UNITS,
            amount_out=QUOTED_USDC_OUT,
        )

        with patch.object(
            agg, "best_quote", return_value=mock_quote
        ) as mock_best_quote:
            agg.trade_best_exact_in(
                amount_in=DIEM_BASE_UNITS,
                min_out_bps=SLIPPAGE_BPS,
                route=["0xDIEM", "0xUSDC"],
            )

            # Verify best_quote was called with allowed_providers filter
            mock_best_quote.assert_called_once()
            call_kwargs = mock_best_quote.call_args[1]
            assert call_kwargs.get("allowed_providers") == {"uniswap_v2"}

    def test_trade_best_exact_in_handles_provider_failure(self):
        """trade_best_exact_in should propagate provider errors appropriately."""
        mock_provider = MagicMock()
        mock_provider.name = "uniswap_v2"
        mock_provider.trade = MagicMock(side_effect=RuntimeError("Trade reverted"))

        agg = DexAggregator(providers=[mock_provider])

        mock_quote = Quote(
            provider="uniswap_v2",
            path=["0xDIEM", "0xUSDC"],
            amount_in=DIEM_BASE_UNITS,
            amount_out=QUOTED_USDC_OUT,
        )

        with (
            patch.object(agg, "best_quote", return_value=mock_quote),
            pytest.raises(RuntimeError, match="Trade reverted"),
        ):
            agg.trade_best_exact_in(
                amount_in=DIEM_BASE_UNITS,
                min_out_bps=SLIPPAGE_BPS,
                route=["0xDIEM", "0xUSDC"],
            )

    def test_trade_best_exact_in_clamps_to_risk_max_slippage(self, monkeypatch):
        """Clamps requested slippage to RISK_MAX_SLIPPAGE_BPS before signing."""
        monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")

        mock_provider = MagicMock()
        mock_provider.name = "uniswap_v2"
        mock_provider.trade = MagicMock(
            return_value={"tx_hash": "0xabc", "provider": "uniswap_v2"}
        )
        agg = DexAggregator(providers=[mock_provider])

        mock_quote = Quote(
            provider="uniswap_v2",
            path=["0xDIEM", "0xUSDC"],
            amount_in=DIEM_BASE_UNITS,
            amount_out=QUOTED_USDC_OUT,
        )
        with patch.object(agg, "best_quote", return_value=mock_quote):
            agg.trade_best_exact_in(
                amount_in=DIEM_BASE_UNITS,
                min_out_bps=150,  # request 1.5% but cap is 0.5%
                route=["0xDIEM", "0xUSDC"],
            )

        # 0.5% slippage => min_out = 100_000_000 * 9950 / 10000 = 99_500_000
        call_args = mock_provider.trade.call_args
        assert call_args[0][1] == 99_500_000
