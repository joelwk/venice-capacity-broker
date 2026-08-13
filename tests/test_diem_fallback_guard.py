"""Tests for DIEMService.trade() fallback guard logic.

Verifies that V3 routes do not fall back to V2 router and that logging
is properly emitted when aggregator attempts fail.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from libs.dex.routes import make_route
from services.diem.client import DIEMService


@pytest.fixture
def v3_route():
    """Create a V3 route (DIEM -> VVV -> USDC with fees)."""
    return make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # VVV
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        ],
        fees=[3000, 3000],
    )


@pytest.fixture
def v2_route():
    """Create a V2 route (DIEM -> USDC, no fees)."""
    return make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        ],
        fees=[None],
    )


@pytest.fixture
def failing_aggregator():
    """Create a mock aggregator that always fails."""
    agg = MagicMock()
    agg.trade_best_exact_out.side_effect = Exception("No quotes available")
    agg.best_quote_exact_out.side_effect = Exception("No quotes available")
    agg.trade_best_exact_in.side_effect = Exception("No quotes available")
    agg.trade_best.side_effect = Exception("No quotes available")
    agg.best_quote.side_effect = Exception("No quotes available")
    return agg


def test_v3_route_no_fallback_when_disabled(monkeypatch, v3_route, failing_aggregator):
    """Test that V3 routes do not fall back to V2 router when fallback is disabled."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    )

    # Mock trade_routes to return V3 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to track if it's called
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should raise RuntimeError instead of calling V2 router
        with pytest.raises(
            RuntimeError, match="no executable DIEM buy routes via aggregator"
        ):
            service.trade("buy", 1000000000000000000)

        # Verify V2 router was NOT called
        mock_actions.trade.assert_not_called()


def test_v3_route_no_fallback_when_enabled_but_incompatible(
    monkeypatch, v3_route, failing_aggregator
):
    """Test that V3 routes still don't use V2 router even when fallback is enabled."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    )

    # Mock trade_routes to return V3 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to track if it's called
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should raise RuntimeError because V3 routes are incompatible with V2 router
        with pytest.raises(RuntimeError, match="V3 routes incompatible with V2 router"):
            service.trade("buy", 1000000000000000000)

        # Verify V2 router was NOT called
        mock_actions.trade.assert_not_called()


def test_v2_route_fallback_when_enabled(monkeypatch, v2_route, failing_aggregator):
    """Test that V2-compatible routes can use fallback when enabled."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    # Mock trade_routes to return V2 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v2_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to return success
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should succeed via fallback
        result = service.trade("buy", 1000000000000000000)

        # Verify V2 router WAS called
        mock_actions.trade.assert_called_once_with("buy", 1000000000000000000)
        assert result["status"] == "sent"
        assert result["tx"] == "0x123"


def test_v2_route_no_fallback_when_disabled(monkeypatch, v2_route, failing_aggregator):
    """Test that V2 routes don't use fallback when disabled."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    # Mock trade_routes to return V2 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v2_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to track if it's called
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should raise RuntimeError because fallback is disabled
        with pytest.raises(
            RuntimeError, match="no executable DIEM buy routes via aggregator"
        ):
            service.trade("buy", 1000000000000000000)

        # Verify V2 router was NOT called
        mock_actions.trade.assert_not_called()


def test_mixed_routes_v3_only_no_fallback(
    monkeypatch, v3_route, v2_route, failing_aggregator
):
    """Test that mixed routes where all are V3 don't use fallback."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "1")

    # Create another V3 route
    v3_route_2 = make_route(
        [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",  # DIEM
            "0x4200000000000000000000000000000000000006",  # WETH
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
        ],
        fees=[3000, 500],
    )

    # Mock trade_routes to return only V3 routes
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route, v3_route_2], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to track if it's called
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should raise RuntimeError because all routes are V3
        with pytest.raises(RuntimeError, match="V3 routes incompatible with V2 router"):
            service.trade("buy", 1000000000000000000)

        # Verify V2 router was NOT called
        mock_actions.trade.assert_not_called()


def test_mixed_routes_with_v2_compatible_uses_fallback(
    monkeypatch, v3_route, v2_route, failing_aggregator
):
    """Test that mixed routes with at least one V2-compatible route can use fallback."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "1")

    # Mock trade_routes to return mixed routes (V3 and V2)
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route, v2_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to return success
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should succeed via fallback because there's a V2-compatible route
        result = service.trade("buy", 1000000000000000000)

        # Verify V2 router WAS called
        mock_actions.trade.assert_called_once_with("buy", 1000000000000000000)
        assert result["status"] == "sent"
        assert result["tx"] == "0x123"


def test_logging_on_aggregator_failure(
    monkeypatch, v3_route, failing_aggregator, caplog
):
    """Test that aggregator failures are properly logged before fallback."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    )

    # Mock trade_routes to return V3 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        with pytest.raises(RuntimeError):
            service.trade("buy", 1000000000000000000, corr_id="test-correlation-id")

        # Verify error logging includes route information
        error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
        assert len(error_logs) >= 1
        assert any(
            "DIEM buy trade failed on all aggregator routes" in r.message
            for r in error_logs
        )
        assert any(
            "all_v3=True" in r.message or "all_routes_v3" in str(r.extra)
            for r in error_logs
        )


def test_logging_on_legacy_fallback_usage(
    monkeypatch, v2_route, failing_aggregator, caplog
):
    """Test that legacy fallback usage is properly logged."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "1")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    )

    # Mock trade_routes to return V2 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v2_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        # Mock _get_actions to return success
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        service.trade("buy", 1000000000000000000, corr_id="test-correlation-id")

        # Verify warning logging for legacy fallback
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "legacy AgentKit/V2 router fallback" in r.message for r in warning_logs
        )
        assert any(
            "path=legacy_v2_actions" in r.message or "legacy_v2_actions" in r.message
            for r in warning_logs
        )


@pytest.fixture
def none_returning_aggregator():
    """Create a mock aggregator that returns None instead of raising."""
    agg = MagicMock()
    agg.trade_best_exact_out.return_value = None
    agg.best_quote_exact_out.return_value = None
    agg.trade_best_exact_in.return_value = None
    agg.trade_best.return_value = None
    agg.best_quote.return_value = None
    return agg


def test_v3_route_aggregator_returns_none(
    monkeypatch, v3_route, none_returning_aggregator, caplog
):
    """Test that V3 routes with aggregator returning None are properly handled and logged."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    )

    # Mock trade_routes to return V3 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route], autospec=True
    ):
        service = DIEMService(aggregator=none_returning_aggregator)

        # Mock _get_actions to track if it's called
        mock_actions = MagicMock()
        mock_actions.trade.return_value = {"status": "sent", "tx": "0x123"}
        service._get_actions = MagicMock(return_value=mock_actions)

        # Should raise RuntimeError instead of calling V2 router
        with pytest.raises(
            RuntimeError, match="no executable DIEM buy routes via aggregator"
        ):
            service.trade("buy", 1000000000000000000, corr_id="test-correlation-id")

        # Verify V2 router was NOT called
        mock_actions.trade.assert_not_called()

        # Verify logging shows None returns
        info_logs = [r for r in caplog.records if r.levelname == "INFO"]
        warning_logs = [r for r in caplog.records if r.levelname == "WARNING"]

        # Should log attempt with route info
        assert any(
            "attempting exact-out aggregator call" in r.message for r in info_logs
        )
        assert any(
            "is_v3=True" in r.message or "is_v3" in str(r.extra) for r in info_logs
        )

        # Should log None return
        assert any("returned None" in r.message for r in warning_logs)

        # Should log route-type guard decision
        assert any("route-type guard" in r.message for r in info_logs)
        assert any(
            "all_routes_v3=True" in r.message or "all_routes_v3" in str(r.extra)
            for r in info_logs
        )


def test_route_type_guard_logging_with_v3_routes(
    monkeypatch, v3_route, failing_aggregator, caplog
):
    """Test that route-type guard logging includes detailed route metadata."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    )

    # Mock trade_routes to return V3 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        with pytest.raises(RuntimeError):
            service.trade("buy", 1000000000000000000, corr_id="test-correlation-id")

        # Verify route-type guard logging
        info_logs = [r for r in caplog.records if r.levelname == "INFO"]
        guard_logs = [r for r in info_logs if "route-type guard" in r.message]

        assert len(guard_logs) >= 1
        guard_log = guard_logs[0]

        # Verify route information is present
        assert "route_types" in guard_log.message or "route_types" in str(
            guard_log.extra
        )
        assert "all_routes_v3" in guard_log.message or "all_routes_v3" in str(
            guard_log.extra
        )
        assert "has_v2_compatible" in guard_log.message or "has_v2_compatible" in str(
            guard_log.extra
        )
        assert "fallback_enabled" in guard_log.message or "fallback_enabled" in str(
            guard_log.extra
        )

        # Verify route metadata includes is_v3 flag
        if hasattr(guard_log, "extra") and isinstance(guard_log.extra, dict):
            route_metadata = guard_log.extra.get("route_metadata", [])
            if route_metadata:
                assert any("is_v3" in str(meta) for meta in route_metadata)


def test_route_type_guard_logging_empty_routes(monkeypatch, failing_aggregator, caplog):
    """Test that route-type guard logging handles empty routes correctly."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")

    # Mock trade_routes to return empty list
    with patch.object(DIEMService, "trade_routes", return_value=[], autospec=True):
        service = DIEMService(aggregator=failing_aggregator)

        with pytest.raises(RuntimeError):
            service.trade("buy", 1000000000000000000, corr_id="test-correlation-id")

        # Verify route-type guard logging for empty routes
        info_logs = [r for r in caplog.records if r.levelname == "INFO"]
        guard_logs = [
            r
            for r in info_logs
            if "route-type guard" in r.message and "no routes available" in r.message
        ]

        assert len(guard_logs) >= 1
        guard_log = guard_logs[0]

        # Verify empty routes are logged
        assert (
            "routes_empty" in str(guard_log.extra)
            or "no routes available" in guard_log.message
        )


def test_aggregator_attempt_logging_includes_venue(
    monkeypatch, v3_route, failing_aggregator, caplog
):
    """Test that aggregator attempt logging includes venue/mode information."""
    monkeypatch.setenv("DIEM_ACTIONS_BUY_FALLBACK_ENABLE", "0")
    monkeypatch.setenv(
        "TRADE_PATH",
        "0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    )

    # Mock trade_routes to return V3 route
    with patch.object(
        DIEMService, "trade_routes", return_value=[v3_route], autospec=True
    ):
        service = DIEMService(aggregator=failing_aggregator)

        with pytest.raises(RuntimeError):
            service.trade("buy", 1000000000000000000, corr_id="test-correlation-id")

        # Verify attempt logging includes venue
        info_logs = [r for r in caplog.records if r.levelname == "INFO"]
        attempt_logs = [
            r for r in info_logs if "attempting exact-out aggregator call" in r.message
        ]

        assert len(attempt_logs) >= 1
        attempt_log = attempt_logs[0]

        # Verify venue/mode is logged
        assert "venue=exact_out" in attempt_log.message or "mode" in str(
            attempt_log.extra
        )
        assert "is_v3" in attempt_log.message or "is_v3" in str(attempt_log.extra)
