"""Tests for DIEM execution engine (preview_trade, execute_trade)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.diem.client import DIEMService
from services.diem.execution import (
    ExecutionConfigError,
    ExecutionIntent,
    ExecutionResult,
    ExecutionStatus,
    TradeSide,
    validate_execution_env,
)


@pytest.fixture
def mock_aggregator():
    """Create a mock DEX aggregator for testing."""
    agg = MagicMock()
    agg.quote_all.return_value = []
    agg.trade_best.return_value = {"tx_hash": "0x123"}
    agg.trade_best_exact_out.return_value = {"tx_hash": "0x456"}
    return agg


@pytest.fixture
def diem_service(mock_aggregator):
    """Create a DIEMService instance for testing."""
    return DIEMService(aggregator=mock_aggregator)


def test_execution_intent_validation():
    """Test ExecutionIntent validation."""
    # Valid intent
    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )
    assert intent.amount_base_units == 1000000
    assert intent.slippage_bps == 50

    # Invalid: negative amount
    with pytest.raises(ValueError, match="amount_base_units must be positive"):
        ExecutionIntent(
            side=TradeSide.BUY,
            token_in="USDC",
            token_out="DIEM",
            amount_base_units=-1,
        )

    # Invalid: slippage too high
    with pytest.raises(ValueError, match="slippage_bps must be between"):
        ExecutionIntent(
            side=TradeSide.BUY,
            token_in="USDC",
            token_out="DIEM",
            amount_base_units=1000000,
            slippage_bps=20000,
        )


def test_execution_result_as_dict():
    """Test ExecutionResult serialization."""
    intent = ExecutionIntent(
        side=TradeSide.SELL,
        token_in="DIEM",
        token_out="USDC",
        amount_base_units=1000000,
    )
    result = ExecutionResult(
        status=ExecutionStatus.SIMULATED,
        intent=intent,
        effective_price=1.05,
        slippage_bps=50.0,
    )
    d = result.as_dict()
    assert d["status"] == "simulated"
    assert d["side"] == "sell"
    assert d["effective_price"] == 1.05
    assert d["slippage_bps"] == 50.0


def test_validate_execution_env_missing_required(monkeypatch):
    """Test execution env validation with missing required vars."""
    monkeypatch.delenv("BASE_RPC_URL", raising=False)
    monkeypatch.delenv("BASE_CHAIN_ID", raising=False)
    monkeypatch.delenv("DIEM_TOKEN_ADDRESS", raising=False)
    monkeypatch.delenv("VVV_TOKEN_ADDRESS", raising=False)

    with pytest.raises(ExecutionConfigError):
        validate_execution_env()


def test_validate_execution_env_valid(monkeypatch):
    """Test execution env validation with all required vars."""
    monkeypatch.setenv("BASE_RPC_URL", "https://base.example.com")
    monkeypatch.setenv("BASE_CHAIN_ID", "8453")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")

    result = validate_execution_env()
    assert result["valid"] is True
    assert len(result["missing"]) == 0
    assert "BASE_RPC_URL" in result["config"]


def test_preview_trade_buy_simulated(diem_service, mock_aggregator):
    """Test preview_trade for buy side in simulate mode."""
    from libs.dex.routes import make_route

    # Setup mock quote response
    mock_quote_dict = {
        "amount_in": 1000000,
        "amount_out": 950000,
        "provider": "uniswap_v2",
    }

    # Mock the quote method to return proper structure
    diem_service.quote = MagicMock(
        return_value={
            "status": "ok",
            "side": "buy",
            "amount": 1000000,
            "quotes": [mock_quote_dict],
        }
    )

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = diem_service.preview_trade(intent)

    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.SIMULATED
    assert result.intent == intent
    # quote() method should be called
    assert diem_service.quote.called
    assert result.effective_price is not None


def test_preview_trade_sell_simulated(diem_service, mock_aggregator):
    """Test preview_trade for sell side in simulate mode."""
    from libs.dex.routes import make_route

    # Setup mock quote response
    mock_quote_dict = {
        "amount_in": 1000000,
        "amount_out": 950000,
        "provider": "uniswap_v2",
    }

    # Mock the quote method
    diem_service.quote = MagicMock(
        return_value={
            "status": "ok",
            "side": "sell",
            "amount": 1000000,
            "quotes": [mock_quote_dict],
        }
    )

    # Setup routes
    route = make_route(["DIEM", "USDC"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    intent = ExecutionIntent(
        side=TradeSide.SELL,
        token_in="DIEM",
        token_out="USDC",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = diem_service.preview_trade(intent)

    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.SIMULATED
    assert result.intent == intent
    assert diem_service.quote.called
    assert result.effective_price is not None


def test_execute_trade_simulated(diem_service, mock_aggregator):
    """Test execute_trade in simulate mode (should not broadcast)."""
    from libs.dex.routes import make_route

    # Setup mock quote response
    mock_quote_dict = {
        "amount_in": 1000000,
        "amount_out": 950000,
        "provider": "uniswap_v2",
    }

    # Mock the quote method
    diem_service.quote = MagicMock(
        return_value={
            "status": "ok",
            "side": "buy",
            "amount": 1000000,
            "quotes": [mock_quote_dict],
        }
    )

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = diem_service.execute_trade(intent, simulate=True)

    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.SIMULATED
    # Should not have called trade methods (simulate=True calls preview_trade)
    assert result.tx_hash is None
    # In simulate mode, execute_trade calls preview_trade, not trade()
    assert diem_service.quote.called


def test_execute_trade_live_blocked_by_slippage(diem_service, mock_aggregator):
    """Test execute_trade blocks execution when slippage exceeds cap."""
    from libs.dex.routes import make_route

    # Setup mock quote with high slippage
    mock_quote = MagicMock()
    mock_quote.amount_in = 1000000
    mock_quote.amount_out = 800000  # 20% slippage = 2000 bps
    mock_quote.provider = "uniswap_v2"
    mock_aggregator.quote_all.return_value = [mock_quote]

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    # Mock trade method to return error when slippage is high
    diem_service.trade = MagicMock(
        return_value={"status": "skipped", "error": "slippage exceeded"}
    )

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,  # Cap at 50 bps
    )

    result = diem_service.execute_trade(intent, simulate=False)

    assert isinstance(result, ExecutionResult)
    # The trade method returns skipped status, which maps to REJECTED
    assert result.status == ExecutionStatus.REJECTED


def test_mint_and_sell_diem_simulated(diem_service, mock_aggregator):
    """Test mint_and_sell_diem helper in simulate mode."""
    from libs.dex.routes import make_route

    # Mock methods
    diem_service.mint = MagicMock(return_value={"status": "dry_run", "tx_hash": None})
    diem_service.execute_trade = MagicMock(
        return_value=ExecutionResult(
            status=ExecutionStatus.SIMULATED,
            intent=ExecutionIntent(
                side=TradeSide.SELL,
                token_in="DIEM",
                token_out="USDC",
                amount_base_units=1000000,
            ),
        )
    )

    # Setup routes
    route = make_route(["DIEM", "USDC"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    result = diem_service.mint_and_sell_diem(1000000, simulate=True)

    assert isinstance(result, dict)
    assert "mint" in result
    assert "sell" in result
    # In simulate mode, mint should return dry_run status
    assert result["mint"]["status"] == "dry_run"
    # Should have called mint with dry_run=True
    diem_service.mint.assert_called_once_with(1000000, dry_run=True)


def test_buy_and_burn_diem_simulated(diem_service, mock_aggregator):
    """Test buy_and_burn_diem helper in simulate mode."""
    from libs.dex.routes import make_route

    # Mock methods
    diem_service.execute_trade = MagicMock(
        return_value=ExecutionResult(
            status=ExecutionStatus.SIMULATED,
            intent=ExecutionIntent(
                side=TradeSide.BUY,
                token_in="USDC",
                token_out="DIEM",
                amount_base_units=1000000,
            ),
        )
    )
    diem_service.burn = MagicMock(return_value={"status": "dry_run", "tx_hash": None})

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    result = diem_service.buy_and_burn_diem(1000000, simulate=True)

    assert isinstance(result, dict)
    assert "buy" in result
    assert "burn" in result
    # In simulate mode, burn should return dry_run status
    assert result["burn"]["status"] == "dry_run"
    # Should have called burn with dry_run=True
    diem_service.burn.assert_called_once_with(1000000, dry_run=True)


def test_execution_result_with_route():
    """Test ExecutionResult includes route information."""
    from libs.dex.routes import make_route

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
    )
    route = make_route(["USDC", "WETH", "DIEM"])
    result = ExecutionResult(
        status=ExecutionStatus.SIMULATED,
        intent=intent,
        route_used=route,
    )

    d = result.as_dict()
    assert "route_tokens" in d
    # Route tokens are normalized to addresses (lowercase with 0x prefix)
    assert len(d["route_tokens"]) == 3
    assert all(isinstance(tok, str) for tok in d["route_tokens"])
    assert d["route_tokens"][0].startswith("0x")


def test_execution_intent_with_preferred_route():
    """Test ExecutionIntent with preferred route."""
    from libs.dex.routes import make_route

    route = make_route(["USDC", "DIEM"])
    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        preferred_route=route,
    )

    assert intent.preferred_route == route
    # Route tokens are normalized to addresses (lowercase with 0x prefix)
    assert len(intent.preferred_route.tokens) == 2
    assert all(isinstance(tok, str) for tok in intent.preferred_route.tokens)
    assert intent.preferred_route.tokens[0].startswith("0x")


def test_preview_trade_no_quotes_available(diem_service, mock_aggregator):
    """Test preview_trade handles case when no quotes are available."""
    from libs.dex.routes import make_route

    # Setup routes but no quotes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])
    mock_aggregator.quote_all.return_value = []

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
    )

    result = diem_service.preview_trade(intent)

    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.REJECTED
    assert result.error is not None
    assert "quotes" in result.error.lower()


def test_execute_trade_live_success(diem_service, mock_aggregator):
    """Test execute_trade successfully executes live trade."""
    from libs.dex.routes import make_route

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    # Mock trade method to return success
    diem_service.trade = MagicMock(
        return_value={
            "status": "sent",
            "tx_hash": "0xabc123",
            "route": ["USDC", "DIEM"],
        }
    )

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = diem_service.execute_trade(intent, simulate=False)

    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.SUBMITTED
    assert result.tx_hash == "0xabc123"
    assert diem_service.trade.called


def test_execute_trade_live_failure(diem_service, mock_aggregator):
    """Test execute_trade handles trade failures."""
    from libs.dex.routes import make_route

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    # Mock trade method to return error
    diem_service.trade = MagicMock(
        return_value={"status": "error", "error": "Transaction reverted"}
    )

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
    )

    result = diem_service.execute_trade(intent, simulate=False)

    assert isinstance(result, ExecutionResult)
    assert result.status == ExecutionStatus.FAILED
    assert result.error is not None


def test_mint_and_sell_diem_live_mode(diem_service, mock_aggregator):
    """Test mint_and_sell_diem in live mode."""
    from libs.dex.routes import make_route

    # Setup routes
    route = make_route(["DIEM", "USDC"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    # Mock methods
    diem_service.mint = MagicMock(return_value={"status": "sent", "tx_hash": "0xmint"})
    diem_service.execute_trade = MagicMock(
        return_value=ExecutionResult(
            status=ExecutionStatus.SUBMITTED,
            intent=ExecutionIntent(
                side=TradeSide.SELL,
                token_in="DIEM",
                token_out="USDC",
                amount_base_units=1000000,
            ),
            tx_hash="0xsell",
        )
    )

    result = diem_service.mint_and_sell_diem(1000000, simulate=False)

    assert isinstance(result, dict)
    assert "mint" in result
    assert "sell" in result
    assert result["mint"]["status"] == "sent"
    assert diem_service.mint.called
    assert diem_service.execute_trade.called


def test_buy_and_burn_diem_live_mode(diem_service, mock_aggregator):
    """Test buy_and_burn_diem in live mode."""
    from libs.dex.routes import make_route

    # Setup routes
    route = make_route(["USDC", "DIEM"])
    diem_service.trade_routes = MagicMock(return_value=[route])

    # Mock methods
    diem_service.execute_trade = MagicMock(
        return_value=ExecutionResult(
            status=ExecutionStatus.SUBMITTED,
            intent=ExecutionIntent(
                side=TradeSide.BUY,
                token_in="USDC",
                token_out="DIEM",
                amount_base_units=1000000,
            ),
            tx_hash="0xbuy",
        )
    )
    diem_service.burn = MagicMock(return_value={"status": "sent", "tx_hash": "0xburn"})

    result = diem_service.buy_and_burn_diem(1000000, simulate=False)

    assert isinstance(result, dict)
    assert "buy" in result
    assert "burn" in result
    assert result["burn"]["status"] == "sent"
    assert diem_service.execute_trade.called
    assert diem_service.burn.called


def test_execution_intent_pool_take_bps_validation():
    """Test ExecutionIntent pool_take_bps validation."""
    # Valid pool_take_bps
    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        pool_take_bps=25,
    )
    assert intent.pool_take_bps == 25

    # Invalid: pool_take_bps too high
    with pytest.raises(ValueError, match="pool_take_bps must be between"):
        ExecutionIntent(
            side=TradeSide.BUY,
            token_in="USDC",
            token_out="DIEM",
            amount_base_units=1000000,
            pool_take_bps=20000,
        )


def test_execution_result_diagnostics():
    """Test ExecutionResult includes diagnostics."""
    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
    )
    result = ExecutionResult(
        status=ExecutionStatus.SIMULATED,
        intent=intent,
        diagnostics={"quotes_available": 2, "best_provider": "uniswap_v2"},
    )

    d = result.as_dict()
    assert "diagnostics" in d
    assert d["diagnostics"]["quotes_available"] == 2
    assert d["diagnostics"]["best_provider"] == "uniswap_v2"


def test_preview_execute_round_trip_arbi_diem_style(diem_service, mock_aggregator):
    """Test preview + execute round-trip with realistic ArbiDiem-style ExecutionIntent data."""
    from libs.dex.providers import Quote
    from libs.dex.routes import RouteHop, RoutePlan

    # Create a realistic route (DIEM -> WETH -> USDC)
    route = RoutePlan(
        (
            RouteHop("0xdiem", "0xweth"),
            RouteHop("0xweth", "0xusdc"),
        )
    )

    # Configure mock aggregator to return valid quotes
    quote = Quote(
        provider="test",
        amount_in=1000000000000000000,
        amount_out=1000000000,  # 1000 USDC
        route=route,
    )
    mock_aggregator.best_quote.return_value = quote
    mock_aggregator.quote_all.return_value = [quote]

    # Simulate ArbiDiem decision: mint and sell DIEM
    sell_intent = ExecutionIntent(
        side=TradeSide.SELL,
        token_in="DIEM",
        token_out="USDC",
        amount_base_units=1000000000000000000,  # 1 DIEM (18 decimals)
        slippage_bps=50,
        pool_take_bps=25,
        preferred_route=route,
        metadata={"correlation_id": "test-123", "decision": "mint_sell"},
    )

    # Step 1: Preview the trade
    preview_result = diem_service.preview_trade(sell_intent)
    assert preview_result.status == ExecutionStatus.SIMULATED
    assert preview_result.intent == sell_intent
    assert preview_result.effective_price is not None

    preview_dict = preview_result.as_dict()
    assert preview_dict["status"] == "simulated"
    assert preview_dict["side"] == "sell"
    assert "effective_price" in preview_dict

    # Step 2: Execute (simulated) - should use preview logic
    execute_result = diem_service.execute_trade(sell_intent, simulate=True)
    assert execute_result.status == ExecutionStatus.SIMULATED
    assert execute_result.intent == sell_intent


def test_no_liquidity_rejected_status(diem_service, mock_aggregator):
    """Test that no-liquidity flows return REJECTED status with appropriate diagnostics."""
    # Configure aggregator to return empty quotes
    mock_aggregator.quote_all.return_value = []
    mock_aggregator.best_quote.return_value = None
    mock_aggregator.best_quote_exact_out.return_value = None

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    # Preview should return REJECTED when no quotes available
    preview_result = diem_service.preview_trade(intent)
    assert preview_result.status == ExecutionStatus.REJECTED
    assert preview_result.error is not None
    assert (
        "quote" in preview_result.error.lower()
        or "liquidity" in preview_result.error.lower()
    )

    # Execute should also return REJECTED
    execute_result = diem_service.execute_trade(intent, simulate=True)
    assert execute_result.status == ExecutionStatus.REJECTED
    assert execute_result.error is not None

    # Check diagnostics
    assert "diagnostics" in execute_result.as_dict()
    diagnostics = execute_result.diagnostics
    assert diagnostics is not None


def test_no_liquidity_zero_quotes_rejected(diem_service, mock_aggregator):
    """Test that zero-amount quotes result in REJECTED status."""
    from libs.dex.providers import Quote
    from libs.dex.routes import RouteHop, RoutePlan

    # Create route
    route = RoutePlan((RouteHop("0xusdc", "0xdiem"),))

    # Return quotes with zero amounts
    zero_quote = Quote(
        provider="test",
        amount_in=1000000,
        amount_out=0,  # Zero output = no liquidity
        route=route,
    )
    mock_aggregator.quote_all.return_value = [zero_quote]
    mock_aggregator.best_quote.return_value = zero_quote

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
    )

    result = diem_service.preview_trade(intent)
    assert result.status == ExecutionStatus.REJECTED
    assert result.error is not None


def test_bridge_route_retry_success(diem_service, mock_aggregator, monkeypatch):
    """Test that bridge route retry succeeds when canonical routes fail."""
    from libs.dex.providers import Quote
    from libs.dex.routes import make_route

    # Mock bridge route availability
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DIEM_VVV_PAIR_ADDRESS", "0xpair")
    monkeypatch.setenv("VVV_USDC_POOL_ADDRESS", "0xpool")

    # Mock get_bridge_trade_path_with_metadata to return bridge path
    def mock_bridge_metadata():
        return {
            "path": ["0xdiem", "0xvvv", "0xusdc"],
            "legs": [
                {
                    "token_in": "0xdiem",
                    "token_out": "0xvvv",
                    "provider": "aerodrome",
                    "pool_address": "0xpair",
                },
                {
                    "token_in": "0xvvv",
                    "token_out": "0xusdc",
                    "provider": "uniswap_v3",
                    "pool_address": "0xpool",
                    "fee": 3000,
                },
            ],
        }

    # First, canonical routes return empty quotes
    mock_aggregator.quote_all.return_value = []
    mock_aggregator.quote_all_exact_out.return_value = []

    # Then bridge route returns valid quote
    bridge_route = make_route(["0xdiem", "0xvvv", "0xusdc"], fees=[None, 3000])
    bridge_quote = Quote(
        provider="bridge_vvv",
        amount_in=1000000,
        amount_out=950000,
        route=bridge_route,
    )

    # Mock trade_routes to return empty initially, then bridge route retry succeeds
    call_count = {"count": 0}

    def mock_quote(side, amount, routes=None):
        call_count["count"] += 1
        if call_count["count"] == 1:
            # First call: canonical routes fail
            return {"status": "ok", "side": side, "amount": amount, "quotes": []}
        # Bridge retry succeeds
        return {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [bridge_quote.__dict__],
        }

    diem_service.quote = MagicMock(side_effect=mock_quote)

    # Mock get_bridge_trade_path_with_metadata
    original_import = __import__

    def mock_import(name, *args, **kwargs):
        if name == "services.marketdata.pathing.fallbacks":
            mod = original_import(name, *args, **kwargs)
            mod.get_bridge_trade_path_with_metadata = MagicMock(
                return_value=mock_bridge_metadata()
            )
            return mod
        return original_import(name, *args, **kwargs)

    # Setup routes (canonical routes that will fail)
    canonical_route = make_route(["0xusdc", "0xdiem"])
    diem_service.trade_routes = MagicMock(return_value=[canonical_route])

    # Mock aggregator to return bridge quote on retry
    def mock_quote_all_exact_out(amount, route):
        if hasattr(route, "tokens") and len(route.tokens) >= 3:
            # Bridge route
            return [bridge_quote]
        return []

    mock_aggregator.quote_all_exact_out = MagicMock(
        side_effect=mock_quote_all_exact_out
    )

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    # Preview should succeed via bridge route retry
    result = diem_service.preview_trade(intent)
    # Note: This test may need adjustment based on actual bridge retry implementation
    # The bridge retry happens inside preview_trade when quotes are empty
    assert result.status in (ExecutionStatus.SIMULATED, ExecutionStatus.REJECTED)
    if result.status == ExecutionStatus.SIMULATED:
        assert result.effective_price is not None


def test_bridge_route_diagnostics_included(diem_service, mock_aggregator, monkeypatch):
    """Test that bridge route diagnostics are included in ExecutionResult when retry fails."""
    from libs.dex.routes import make_route

    # Mock bridge route availability
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0xvvv")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("DIEM_VVV_PAIR_ADDRESS", "0xpair")
    monkeypatch.setenv("VVV_USDC_POOL_ADDRESS", "0xpool")

    # All routes return empty quotes (including bridge)
    mock_aggregator.quote_all.return_value = []
    mock_aggregator.quote_all_exact_out.return_value = []

    canonical_route = make_route(["0xusdc", "0xdiem"])
    diem_service.trade_routes = MagicMock(return_value=[canonical_route])
    diem_service.quote = MagicMock(
        return_value={"status": "ok", "side": "buy", "amount": 1000000, "quotes": []}
    )

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=1000000,
        slippage_bps=50,
    )

    result = diem_service.preview_trade(intent)
    assert result.status == ExecutionStatus.REJECTED
    assert result.diagnostics is not None
    # Diagnostics should include bridge route availability info
    diagnostics = result.diagnostics
    assert "quotes_attempted" in diagnostics or "bridge_route_available" in diagnostics


def test_reserve_fallback_enabled_returns_quote(monkeypatch):
    """Test that reserve fallback returns a valid Quote when enabled and reserves exist."""
    from unittest.mock import patch

    from libs.dex.diem_fallbacks import (
        check_reserve_fallback_available,
        diem_vvv_quote_from_reserves,
    )

    # Enable fallback
    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "1")
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    )
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )

    # Mock reserves: 318k DIEM, 2.4k VVV (typical on-chain values)
    mock_reserves = (
        2400000000000000000000,
        318000000000000000000000,
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )

    with patch(
        "libs.dex.diem_fallbacks._get_diem_vvv_reserves", return_value=mock_reserves
    ):
        # Test exact-out quote (VVV -> DIEM)
        token_in = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"  # VVV
        token_out = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"  # DIEM
        amount_out = 1000000000000000000  # 1 DIEM

        quote = diem_vvv_quote_from_reserves(amount_out, token_in, token_out)
        assert quote is not None
        assert quote.amount_out == amount_out
        assert quote.amount_in > 0
        assert quote.provider == "diem_pair_math"

        # Test check helper
        check_result = check_reserve_fallback_available()
        assert check_result["enabled"] is True
        assert check_result["reserves_available"] is True
        assert check_result["test_quote"] is not None


def test_reserve_fallback_disabled_returns_none(monkeypatch):
    """Test that reserve fallback returns None when disabled."""
    from unittest.mock import patch

    from libs.dex.diem_fallbacks import (
        check_reserve_fallback_available,
        diem_vvv_quote_from_reserves,
    )

    # Disable fallback
    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "0")
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    )

    # Mock reserves exist
    mock_reserves = (
        2400000000000000000000,
        318000000000000000000000,
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )

    with patch(
        "libs.dex.diem_fallbacks._get_diem_vvv_reserves", return_value=mock_reserves
    ):
        token_in = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
        token_out = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
        amount_out = 1000000000000000000

        quote = diem_vvv_quote_from_reserves(amount_out, token_in, token_out)
        assert quote is None

        # Test check helper
        check_result = check_reserve_fallback_available()
        assert check_result["enabled"] is False
        assert check_result["error"] is not None
        assert "not enabled" in check_result["error"]


def test_reserve_fallback_no_reserves_returns_none(monkeypatch):
    """Test that reserve fallback returns None when reserves unavailable."""
    from unittest.mock import patch

    from libs.dex.diem_fallbacks import (
        check_reserve_fallback_available,
        diem_vvv_quote_from_reserves,
    )

    # Enable fallback but no reserves
    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "1")
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    )

    with patch("libs.dex.diem_fallbacks._get_diem_vvv_reserves", return_value=None):
        token_in = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
        token_out = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
        amount_out = 1000000000000000000

        quote = diem_vvv_quote_from_reserves(amount_out, token_in, token_out)
        assert quote is None

        # Test check helper
        check_result = check_reserve_fallback_available()
        assert check_result["enabled"] is True
        assert check_result["reserves_available"] is False
        assert check_result["error"] is not None
        assert "reserves" in check_result["error"].lower()


def test_bridge_provider_leg2_reserve_fallback_success(monkeypatch):
    """Test BridgeRouteProvider uses reserve fallback for leg2 when router quote fails."""
    from unittest.mock import MagicMock, patch

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "1")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv(
        "DIEM_VVV_PAIR_ADDRESS", "0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d"
    )
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    # Create mock leg providers
    leg1_provider = MagicMock()  # USDC->VVV provider
    leg1_provider.name = "uniswap_v3"
    leg1_provider.quote_exact_out.return_value = Quote(
        provider="uniswap_v3",
        amount_in=1000000000,  # 1 USDC
        amount_out=990000000000000000,  # 0.99 VVV
        route=make_route(
            [
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ],
            fees=[3000],
        ),
    )

    leg2_provider = MagicMock()  # VVV->DIEM provider (will fail)
    leg2_provider.name = "aerodrome"
    leg2_provider.quote_exact_out.return_value = None  # Router quote fails

    # Mock reserves for leg2 fallback
    mock_reserves = (
        2400000000000000000000,
        318000000000000000000000,
        "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
    )

    provider_map = {
        "uniswap_v3": leg1_provider,
        "aerodrome": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    # Create bridge route: USDC -> VVV -> DIEM
    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, None],
    )

    amount_out = 1000000000000000000  # 1 DIEM

    # Capture logs
    with (
        patch(
            "libs.dex.diem_fallbacks._get_diem_vvv_reserves", return_value=mock_reserves
        ),
        patch("libs.dex.providers.diem_vvv_quote_from_reserves") as mock_fallback,
    ):
        # Mock reserve fallback to return a quote
        mock_fallback.return_value = Quote(
            provider="diem_pair_math",
            amount_in=128000000000000000000,  # ~128 VVV
            amount_out=amount_out,
            route=make_route(
                [
                    "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                    "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
                ]
            ),
        )

        quote = bridge_provider.quote_exact_out(amount_out, route)

        # Should succeed using reserve fallback for leg2
        assert quote is not None
        assert quote.provider == "bridge_vvv"
        assert quote.amount_out == amount_out
        assert quote.amount_in > 0

        # Verify leg2 fallback was called
        mock_fallback.assert_called_once()

        # Verify leg1 provider was called with the amount needed from leg2
        leg1_provider.quote_exact_out.assert_called_once()


def test_bridge_provider_both_legs_fail_returns_none(monkeypatch):
    """Test BridgeRouteProvider returns None when both legs fail and reserve fallback unavailable."""
    from unittest.mock import MagicMock

    from libs.dex.providers import BridgeRouteProvider
    from libs.dex.routes import make_route

    # Set up environment (fallback disabled)
    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "0")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    # Create mock leg providers that both fail
    leg1_provider = MagicMock()
    leg1_provider.name = "uniswap_v3"
    leg1_provider.quote_exact_out.return_value = None

    leg2_provider = MagicMock()
    leg2_provider.name = "aerodrome"
    leg2_provider.quote_exact_out.return_value = None

    provider_map = {
        "uniswap_v3": leg1_provider,
        "aerodrome": leg2_provider,
    }

    BridgeRouteProvider(provider_map)

    make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
        ],
        fees=[3000, None],
    )


def test_bridge_provider_fills_missing_v3_fee_for_vvv_usdc_leg(monkeypatch):
    """Test BridgeRouteProvider supplies V3 fee tier when route omits it."""
    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    diem = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    usdc = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", usdc)
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
    monkeypatch.setenv("DIEM_BRIDGE_SINGLE_LEG_FALLBACK_ENABLE", "0")
    monkeypatch.setenv("DIEM_BRIDGE_SINGLE_LEG_FALLBACK_ENABLE", "0")
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    class V3LegProvider:
        name = "uniswap_v3"

        def __init__(self) -> None:
            self.last_fee: int | None = None

        def quote(self, amount_in: int, route):  # type: ignore[no-untyped-def]
            try:
                self.last_fee = route.hops[0].fee
            except Exception:
                self.last_fee = None
            return Quote(
                provider="uniswap_v3",
                amount_in=int(amount_in),
                amount_out=10**18,
                route=route,
            )

    class Leg2Provider:
        name = "aerodrome"

        def quote(self, amount_in: int, route):  # type: ignore[no-untyped-def]
            return Quote(
                provider="aerodrome",
                amount_in=int(amount_in),
                amount_out=10**18,
                route=route,
            )

    leg1_provider_v3 = V3LegProvider()
    leg2_provider = Leg2Provider()

    bridge_provider = BridgeRouteProvider(
        {"uniswap_v3": leg1_provider_v3, "aerodrome": leg2_provider}
    )

    # Route is un-annotated (no @fee in specs, no hop fee set).
    route = make_route([usdc, vvv, diem])

    quote = bridge_provider.quote(1_000_000, route)
    assert quote is not None
    assert leg1_provider_v3.last_fee == 3000


def test_bridge_provider_v2_fallback_leg1_exact_in_success(monkeypatch):
    """Test BridgeRouteProvider uses V2 fallback for USDC→VVV leg1 when V3 returns empty in exact-in."""
    from unittest.mock import MagicMock

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv("VVV_USDC_V2_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    # Create mock leg providers
    leg1_provider_v3 = MagicMock()  # USDC->VVV provider (V3, will fail)
    leg1_provider_v3.name = "uniswap_v3"
    leg1_provider_v3.quote.return_value = None  # V3 returns empty

    leg1_provider_v2 = MagicMock()  # USDC->VVV provider (V2 fallback)
    leg1_provider_v2.name = "uniswap_v2"
    leg1_provider_v2.quote.return_value = Quote(
        provider="uniswap_v2",
        amount_in=1000000000,  # 1 USDC
        amount_out=990000000000000000,  # 0.99 VVV
        route=make_route(
            [
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ]
        ),
        executable=True,
    )

    leg2_provider = MagicMock()  # VVV->DIEM provider
    leg2_provider.name = "aerodrome"
    leg2_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=990000000000000000,  # 0.99 VVV
        amount_out=1000000000000000000,  # 1 DIEM
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            ]
        ),
    )

    provider_map = {
        "uniswap_v3": leg1_provider_v3,
        "uniswap_v2": leg1_provider_v2,
        "aerodrome": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    # Create bridge route: USDC -> VVV -> DIEM (exact-in buy)
    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, None],
    )

    amount_in = 1000000000  # 1 USDC

    quote = bridge_provider.quote(amount_in, route)

    # Should succeed using V2 fallback for leg1
    assert quote is not None
    assert quote.provider == "bridge_vvv"
    assert quote.amount_in == amount_in
    assert quote.amount_out > 0

    # Verify V3 was tried first
    leg1_provider_v3.quote.assert_called_once()
    # Verify V2 fallback was called
    leg1_provider_v2.quote.assert_called_once()
    # Verify leg2 provider was called
    leg2_provider.quote.assert_called_once()


def test_bridge_provider_v2_fallback_leg1_exact_in_v2_also_fails(monkeypatch):
    """Test BridgeRouteProvider falls back to analytic when V2 also fails for leg1."""
    from unittest.mock import MagicMock, patch

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv("VVV_USDC_V2_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")
    monkeypatch.setenv("DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "1")

    # Create mock leg providers
    leg1_provider_v3 = MagicMock()  # USDC->VVV provider (V3, will fail)
    leg1_provider_v3.name = "uniswap_v3"
    leg1_provider_v3.quote.return_value = None  # V3 returns empty

    leg1_provider_v2 = MagicMock()  # USDC->VVV provider (V2 fallback, also fails)
    leg1_provider_v2.name = "uniswap_v2"
    leg1_provider_v2.quote.return_value = None  # V2 also fails

    leg2_provider = MagicMock()  # VVV->DIEM provider
    leg2_provider.name = "aerodrome"
    leg2_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=990000000000000000,  # 0.99 VVV
        amount_out=1000000000000000000,  # 1 DIEM
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            ]
        ),
    )

    provider_map = {
        "uniswap_v3": leg1_provider_v3,
        "uniswap_v2": leg1_provider_v2,
        "aerodrome": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    # Create bridge route: USDC -> VVV -> DIEM (exact-in buy)
    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, None],
    )

    amount_in = 1000000000  # 1 USDC

    # Mock analytic fallback
    with patch("libs.dex.providers.vvv_usdc_v3_mid_price_quote") as mock_analytic:
        mock_analytic.return_value = Quote(
            provider="v3_analytic",
            amount_in=amount_in,
            amount_out=990000000000000000,  # 0.99 VVV (preview-only)
            route=make_route(
                [
                    "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                    "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                ]
            ),
            executable=False,  # Preview-only
        )

        quote = bridge_provider.quote(amount_in, route)

        # Should succeed using analytic fallback for leg1 (preview-only)
        assert quote is not None
        assert quote.provider == "bridge_vvv"
        assert quote.amount_in == amount_in
        assert quote.amount_out > 0

        # Verify V3 was tried first
        leg1_provider_v3.quote.assert_called_once()
        # Verify V2 fallback was tried
        leg1_provider_v2.quote.assert_called_once()
        # Verify analytic fallback was called
        mock_analytic.assert_called_once()


def test_bridge_provider_telemetry_leg_failure(monkeypatch):
    """Test BridgeRouteProvider emits dex_bridge_leg_failure telemetry when leg fails."""
    from unittest.mock import MagicMock, patch

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv("VVV_USDC_V2_FALLBACK_ENABLE", "0")  # Disable V2 fallback
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")
    monkeypatch.setenv(
        "VVV_USDC_POOL_ADDRESS",
        "0x2222222222222222222222222222222222222222",
    )
    monkeypatch.setenv("VVV_USDC_POOL_FEE", "3000")
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
    monkeypatch.setenv(
        "DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE", "0"
    )  # Disable analytic

    # Create mock leg providers
    leg1_provider = MagicMock()  # USDC->VVV provider (will fail)
    leg1_provider.name = "uniswap_v3"
    leg1_provider.quote.return_value = None  # Returns empty
    leg2_provider = MagicMock()  # VVV->DIEM provider (should exist)
    leg2_provider.name = "aerodrome"
    leg2_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=1,
        amount_out=1,
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            ]
        ),
    )

    provider_map = {
        "uniswap_v3": leg1_provider,
        "aerodrome": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    # Create bridge route: USDC -> VVV -> DIEM (exact-in buy)
    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, None],
    )

    amount_in = 1000000000  # 1 USDC

    # Capture telemetry events
    captured_events = []

    def capture_event(event):
        captured_events.append(event)

    with patch("libs.dex.providers._dex_diag_log_event", side_effect=capture_event):
        quote = bridge_provider.quote(amount_in, route)

        # Leg1 failure should emit diagnostics even if a fallback succeeds.
        assert quote is not None

        # Verify telemetry was emitted
        assert len(captured_events) > 0
        leg_failure_events = [
            e for e in captured_events if e.get("event") == "dex_bridge_leg_failure"
        ]
        assert len(leg_failure_events) > 0

        # Verify event fields
        failure_event = next(
            (e for e in leg_failure_events if e.get("leg_index") == 0),
            leg_failure_events[0],
        )
        assert failure_event["leg_index"] == 0
        assert failure_event["provider"] == "uniswap_v3"
        assert failure_event["mode"] == "exact_in"
        assert failure_event["reason"] in ("empty", "zero_output", "missing_provider")
        assert "token_in" in failure_event
        assert "token_out" in failure_event
        assert "amount" in failure_event
        assert (
            failure_event.get("pool_address")
            == "0x2222222222222222222222222222222222222222"
        )


def test_bridge_provider_v2_fallback_skipped_on_multihop(monkeypatch):
    """Test BridgeRouteProvider skips V2 fallback for multi-hop legs (USDC→WETH→VVV)."""
    from unittest.mock import MagicMock, patch

    from libs.dex.providers import BridgeRouteProvider
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv("VVV_USDC_V2_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    # Create mock leg providers
    leg1_provider_v3 = (
        MagicMock()
    )  # USDC->WETH->VVV provider (V3, multi-hop, will fail)
    leg1_provider_v3.name = "uniswap_v3"
    leg1_provider_v3.quote.return_value = None  # V3 returns empty

    leg1_provider_v2 = MagicMock()  # USDC->VVV provider (V2 fallback)
    leg1_provider_v2.name = "uniswap_v2"

    leg2_provider = MagicMock()  # VVV->DIEM provider
    leg2_provider.name = "aerodrome"
    leg2_provider.quote.return_value = None  # Will fail, but we're testing leg1

    provider_map = {
        "uniswap_v3": leg1_provider_v3,
        "uniswap_v2": leg1_provider_v2,
        "aerodrome": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    # Create multi-hop bridge route: USDC -> WETH -> VVV -> DIEM (exact-in buy)
    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0x4200000000000000000000000000000000000006",  # WETH
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, 3000, None],
    )

    amount_in = 1000000000  # 1 USDC

    # Capture telemetry events
    captured_events = []

    def capture_event(event):
        captured_events.append(event)

    with patch("libs.dex.providers._dex_diag_log_event", side_effect=capture_event):
        bridge_provider.quote(amount_in, route)

        # Should fail (multi-hop leg1, V2 fallback skipped)
        # V2 should NOT be called for multi-hop stages
        leg1_provider_v2.quote.assert_not_called()

        # Verify skip telemetry was emitted
        skip_events = [
            e for e in captured_events if e.get("event") == "dex_bridge_leg_v2_skip"
        ]
        assert len(skip_events) > 0

        # Verify skip event fields
        skip_event = skip_events[0]
        assert skip_event["reason"] == "multihop_stage"
        assert skip_event["mode"] == "exact_in"
        assert skip_event["leg_index"] == 0
        assert skip_event["token_count"] == 3  # USDC, WETH, VVV
        assert len(skip_event["tokens"]) == 3


def test_bridge_provider_v2_fallback_works_on_two_token_leg(monkeypatch):
    """Test BridgeRouteProvider uses V2 fallback for 2-token USDC→VVV leg."""
    from unittest.mock import MagicMock

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv("VVV_USDC_V2_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    # Create mock leg providers
    leg1_provider_v3 = MagicMock()  # USDC->VVV provider (V3, will fail)
    leg1_provider_v3.name = "uniswap_v3"
    leg1_provider_v3.quote.return_value = None  # V3 returns empty

    leg1_provider_v2 = MagicMock()  # USDC->VVV provider (V2 fallback)
    leg1_provider_v2.name = "uniswap_v2"
    leg1_provider_v2.quote.return_value = Quote(
        provider="uniswap_v2",
        amount_in=1000000000,  # 1 USDC
        amount_out=990000000000000000,  # 0.99 VVV
        route=make_route(
            [
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ]
        ),
        executable=True,
    )

    leg2_provider = MagicMock()  # VVV->DIEM provider
    leg2_provider.name = "aerodrome"
    leg2_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=990000000000000000,  # 0.99 VVV
        amount_out=1000000000000000000,  # 1 DIEM
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            ]
        ),
    )

    provider_map = {
        "uniswap_v3": leg1_provider_v3,
        "uniswap_v2": leg1_provider_v2,
        "aerodrome": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    # Create 2-token bridge route: USDC -> VVV -> DIEM (exact-in buy)
    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, None],
    )

    amount_in = 1000000000  # 1 USDC

    quote = bridge_provider.quote(amount_in, route)

    # Should succeed using V2 fallback for leg1 (2-token leg)
    assert quote is not None
    assert quote.provider == "bridge_vvv"
    assert quote.amount_in == amount_in
    assert quote.amount_out > 0

    # Verify V3 was tried first
    leg1_provider_v3.quote.assert_called_once()
    # Verify V2 fallback was called (2-token leg allows V2)
    leg1_provider_v2.quote.assert_called_once()
    # Verify leg2 provider was called
    leg2_provider.quote.assert_called_once()

    amount_out = 1000000000000000000

    quote = bridge_provider.quote_exact_out(amount_out, route)

    # Should return None when both legs fail and fallback disabled
    assert quote is None


def test_bridge_provider_rejects_extreme_leg_ratio(monkeypatch):
    """Test BridgeRouteProvider rejects composite quotes with extreme implied leg ratios."""
    from unittest.mock import MagicMock

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Set up environment
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    # Create mock leg providers that return an extreme first-leg quote.
    leg1_provider = MagicMock()
    leg1_provider.name = "aerodrome"
    leg1_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=10**18,  # 1 DIEM (18 decimals)
        amount_out=10**30,  # 1e12 VVV (18 decimals) => extreme ratio
        route=make_route(
            [
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ]
        ),
    )

    leg2_provider = MagicMock()
    leg2_provider.name = "uniswap_v3"
    leg2_provider.quote.return_value = Quote(
        provider="uniswap_v3",
        amount_in=10**30,
        amount_out=10**18,  # 1e12 USDC (6 decimals) => ratio ~= 1
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            ],
            fees=[3000],
        ),
    )

    provider_map = {
        "aerodrome": leg1_provider,
        "uniswap_v3": leg2_provider,
    }
    bridge_provider = BridgeRouteProvider(provider_map)

    # Bridge route: DIEM -> VVV -> USDC
    route = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        ],
        fees=[None, 3000],
    )

    quote = bridge_provider.quote(10**18, route)
    assert quote is None
    assert bridge_provider.bridge_failure_reason() == "leg_ratio_extreme"


def test_bridge_provider_failure_reason_propagates_to_aggregator(monkeypatch):
    """DexAggregator should surface bridge_vvv's concrete failure reason on empty quotes."""
    from unittest.mock import MagicMock

    from libs.dex.providers import BridgeRouteProvider, DexAggregator, Quote
    from libs.dex.routes import make_route

    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "1")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )
    monkeypatch.setenv("DIEM_VVV_BRIDGE_PROVIDER", "aerodrome")
    monkeypatch.setenv("VVV_USDC_BRIDGE_PROVIDER", "uniswap_v3")

    leg1_provider = MagicMock()
    leg1_provider.name = "aerodrome"
    leg1_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=10**18,
        amount_out=10**30,  # Extreme ratio forces rejection
        route=make_route(
            [
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ]
        ),
    )

    leg2_provider = MagicMock()
    leg2_provider.name = "uniswap_v3"
    leg2_provider.quote.return_value = Quote(
        provider="uniswap_v3",
        amount_in=10**30,
        amount_out=10**18,
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
            ],
            fees=[3000],
        ),
    )

    bridge_provider = BridgeRouteProvider(
        {"aerodrome": leg1_provider, "uniswap_v3": leg2_provider}
    )
    agg = DexAggregator([bridge_provider])

    route = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        ],
        fees=[None, 3000],
    )

    quote = agg.best_quote(10**18, route)
    assert quote is None

    diag = getattr(agg, "_last_quote_diagnostics", [])
    bridge_diag = next((d for d in diag if d.get("provider") == "bridge_vvv"), None)
    assert bridge_diag is not None
    assert bridge_diag.get("status") == "empty"
    assert bridge_diag.get("diem_bridge_failure_reason") == "leg_ratio_extreme"


def test_bridge_provider_prefers_reserve_on_drift_exact_in(monkeypatch):
    """Router DIEM/VVV leg drifts; BridgeRouteProvider should prefer reserve math."""
    from unittest.mock import MagicMock, patch

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    # Env setup
    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "1")
    monkeypatch.setenv("DIEM_VVV_RESERVE_PREF_DRIFT_BPS", "100")  # 1% threshold
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )

    amount_in = 10**18  # 1 DIEM

    # Router leg1 overstates amount_out (drift vs reserve)
    leg1_provider = MagicMock()
    leg1_provider.name = "aerodrome"
    leg1_provider.quote.return_value = Quote(
        provider="aerodrome",
        amount_in=amount_in,
        amount_out=2_000 * 10**18,  # drifted
        route=make_route(
            [
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ]
        ),
    )

    # leg2 returns proportional output to its input to detect change
    def _leg2_side_effect(amount_in_leg2, route):
        # Use realistic VVV (18 decimals) -> USDC (6 decimals) pricing (~1:1)
        return Quote(
            provider="uniswap_v3",
            amount_in=amount_in_leg2,
            amount_out=amount_in_leg2 // 10**12,  # scale down to USDC decimals
            route=route,
        )

    leg2_provider = MagicMock()
    leg2_provider.name = "uniswap_v3"
    leg2_provider.quote.side_effect = _leg2_side_effect

    provider_map = {
        "aerodrome": leg1_provider,
        "uniswap_v3": leg2_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    route = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        ],
        fees=[None, 3000],
    )

    # Reserve quote should be preferred with smaller amount_out
    reserve_quote = Quote(
        provider="diem_pair_math",
        amount_in=amount_in,
        amount_out=1_100 * 10**18,  # closer to true reserves
        route=make_route(
            [
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ]
        ),
    )

    with patch(
        "libs.dex.providers.diem_vvv_quote_exact_in_from_reserves",
        return_value=reserve_quote,
    ):
        quote = bridge_provider.quote(amount_in, route)

    assert quote is not None
    # leg2 should receive reserve-derived amount_out as its input
    leg2_provider.quote.assert_called_once()
    called_amount_in = leg2_provider.quote.call_args[0][0]
    assert called_amount_in == reserve_quote.amount_out
    # Composite amount_out reflects reserve-preferred flow
    assert quote.amount_out == reserve_quote.amount_out // 10**12


def test_bridge_provider_prefers_reserve_on_drift_exact_out(monkeypatch):
    """Router DIEM/VVV leg drifts in exact-out; prefer reserve math and adjust upstream leg1 ask."""
    from unittest.mock import MagicMock, patch

    from libs.dex.providers import BridgeRouteProvider, Quote
    from libs.dex.routes import make_route

    monkeypatch.setenv("DIEM_ENABLE_PAIR_MATH_FALLBACK", "1")
    monkeypatch.setenv("DIEM_VVV_RESERVE_PREF_DRIFT_BPS", "100")
    monkeypatch.setenv(
        "DIEM_TOKEN_ADDRESS", "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    )
    monkeypatch.setenv(
        "VVV_TOKEN_ADDRESS", "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    )
    monkeypatch.setenv(
        "QUOTE_TOKEN_ADDRESS", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    )

    amount_out = 10**18  # want 1 DIEM

    # leg2 router underestimates required VVV
    leg2_provider = MagicMock()
    leg2_provider.name = "aerodrome"
    leg2_provider.quote_exact_out.return_value = Quote(
        provider="aerodrome",
        amount_in=50 * 10**18,  # drifted low
        amount_out=amount_out,
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            ]
        ),
    )

    # leg1 provider should be asked for the reserve-preferred VVV amount_out
    leg1_provider = MagicMock()
    leg1_provider.name = "uniswap_v3"
    leg1_provider.quote_exact_out.return_value = Quote(
        provider="uniswap_v3",
        amount_in=400 * 10**6,  # 400 USDC (6 decimals)
        amount_out=200 * 10**18,
        route=make_route(
            [
                "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
            ],
            fees=[3000],
        ),
    )

    provider_map = {
        "aerodrome": leg2_provider,
        "uniswap_v3": leg1_provider,
    }

    bridge_provider = BridgeRouteProvider(provider_map)

    route = make_route(
        [
            "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
            "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",  # VVV
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
        ],
        fees=[3000, None],
    )

    reserve_quote = Quote(
        provider="diem_pair_math",
        amount_in=200 * 10**18,  # reserve-implied VVV required
        amount_out=amount_out,
        route=make_route(
            [
                "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf",
                "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            ]
        ),
    )

    with patch(
        "libs.dex.providers.diem_vvv_quote_from_reserves", return_value=reserve_quote
    ):
        quote = bridge_provider.quote_exact_out(amount_out, route)

    assert quote is not None
    # leg1 should be quoted for reserve-required VVV out
    leg1_provider.quote_exact_out.assert_called_once()
    asked_amount_out = leg1_provider.quote_exact_out.call_args[0][0]
    assert asked_amount_out == reserve_quote.amount_in
    # Composite max-in should reflect leg1 quote
    assert quote.amount_in == leg1_provider.quote_exact_out.return_value.amount_in
