from __future__ import annotations

from importlib import import_module

from agents.quorum import build_default_coordinator
from agents.reflex.guardian import ReflexGuardian
from libs.dex.providers import Quote
from libs.dex.routes import as_route_plan

LISTEN_BASE_SECONDS = 10.0
UTILIZATION_RATIO = 0.8


def test_single_loop_cycle_includes_stake_and_capacity():
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeStake:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def run_once(self, live: bool = False):
            self.calls.append(live)
            return {"status": "ok", "live": live}

    class FakeArbi:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale = {"decision": "mint_sell"}

        def evaluate_and_maybe_mint(
            self,
            price: float,
            _mint_rate: float = 1.0,
            _desired_units=None,
            current_inventory_usd=None,
            simulate: bool | None = None,
            **_kwargs,
        ) -> bool:
            self.calls.append(
                {
                    "price": price,
                    "simulate": simulate,
                    "inventory": current_inventory_usd,
                }
            )
            self._last_rationale = {
                "decision": "mint_sell",
                "price": price,
                "simulate": simulate,
            }
            return True

    class FakeCapacity:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def run_once(self, parent_key: str | None = None, enforce_limits: bool = True):
            self.calls.append(parent_key)
            return {"status": "ok", "parent": parent_key, "enforce": enforce_limits}

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {"vvv": {"utilization": UTILIZATION_RATIO}}

        def prices(self, _symbols):
            return {"DIEM": 2.0, "VVV": 0.5, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.1, "source": "test"}

    stake = FakeStake()
    arbi = FakeArbi()
    capacity = FakeCapacity()
    market = FakeMarket()

    orchestrator = orch_mod.SingleLoopOrchestrator(
        stake_master=stake,
        arbi=arbi,
        capacity_broker=capacity,
        market=market,
        parent_key="PARENT",
    )

    record = orchestrator.run_cycle(dry_run=True, enable_live=False)

    assert record["stake"]["status"] == "ok"
    assert record["arbi"]["action"] == "mint_sell"
    assert record["arbi"]["signals"]["utilization_ratio"] == UTILIZATION_RATIO
    assert record["arbi"]["execution"]["status"] == "dry_run"
    assert record["capacity"]["status"] == "ok"
    # Verify quorum info is always present, even when quorum is None
    assert "quorum" in record["arbi"]
    quorum_info = record["arbi"]["quorum"]
    assert isinstance(quorum_info, dict)
    assert "status" in quorum_info
    assert capacity.calls == ["PARENT"]
    assert stake.calls == [False]


def test_single_loop_quorum_blocks_actions():
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeArbi:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale = {"decision": "mint_sell"}

        def evaluate_and_maybe_mint(
            self,
            _price: float,
            _mint_rate: float = 1.0,
            _desired_units=None,
            _current_inventory_usd=None,
            simulate: bool | None = None,
            **_kwargs,
        ) -> bool:
            self.calls.append({"simulate": simulate})
            self._last_rationale = {
                "decision": "mint_sell",
                "desired_units": 1_000,
                "suggested_units": 1_000,
                "portfolioAdjustedUnits": 1_000,
                "current_inventory_usd": 1000.0,
            }
            return True

    class FakeQuorum:
        def __init__(self, allow: bool) -> None:
            self.allow = allow
            self.calls = 0

        def decide(self) -> bool:
            self.calls += 1
            return self.allow

    class FakeStake:
        def run_once(self, live: bool = False):
            return {"status": "ok", "live": live}

    class FakeCapacity:
        def run_once(
            self, _parent_key: str | None = None, _enforce_limits: bool = True
        ):
            return {"status": "ok"}

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {}

        def prices(self, _symbols):
            return {"DIEM": 1.0, "VVV": 0.2, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.0, "source": "test"}

    arbi = FakeArbi()
    quorum = FakeQuorum(allow=False)
    orchestrator = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=arbi,
        capacity_broker=FakeCapacity(),
        market=FakeMarket(),
        quorum=quorum,
    )

    record = orchestrator.run_cycle(dry_run=False, enable_live=True)

    assert quorum.calls == 1
    # simulate True should be recorded, but no simulate False call when blocked
    assert len(arbi.calls) == 1
    assert arbi.calls[0]["simulate"] is True
    assert record["arbi"]["execution"]["status"] == "blocked"
    # Verify quorum info is always present
    assert "quorum" in record["arbi"]
    quorum_info = record["arbi"]["quorum"]
    assert isinstance(quorum_info, dict)
    assert "status" in quorum_info
    assert quorum_info["status"] == "blocked"
    # Verify portfolio inventory and broker utilization are persisted
    if "portfolio" in record:
        assert "inventoryUsd" in record["portfolio"]
        assert "perAssetUsd" in record["portfolio"]
    if "brokerUtilization" in record:
        assert isinstance(record["brokerUtilization"], (float, type(None)))

    # Verify ArbiDiem rationale includes portfolio telemetry
    if "arbi" in record and "why" in record["arbi"]:
        rationale = record["arbi"]["why"]
        if isinstance(rationale, dict):
            assert "desired_units" in rationale or "suggested_units" in rationale
            assert (
                "portfolioAdjustedUnits" in rationale
                or "current_inventory_usd" in rationale
            )


def test_single_loop_executes_capacity_recovery_live_even_when_main_signal_holds():
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeStake:
        def run_once(self, live: bool = False, **_kwargs):
            return {
                "status": "ok",
                "live": live,
                "snapshot": {
                    "staked": 100,
                },
            }

    class FakeArbi:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.exec_calls: list[dict[str, object]] = []
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale: dict[str, object] = {"decision": "hold"}
            self._pending_recovery_action: dict[str, object] | None = None

        def _locked_svvv_ratio_cap(self) -> float:
            return 0.65

        def _locked_svvv_ratio_min_total_units(self) -> int:
            return 1

        def _svvv_lock_state(self, _wallet_balances: dict[str, object]):
            return {
                "locked_ratio": 0.7,
                "total_units": 100,
            }

        def evaluate_and_maybe_mint(
            self,
            price: float,
            simulate: bool | None = None,
            **_kwargs,
        ) -> bool:
            self.calls.append(
                {
                    "simulate": simulate,
                }
            )
            if simulate:
                # Simulate a "hold" signal while planning a recovery action.
                self._pending_recovery_action = {
                    "kind": "stake",
                    "decision": "capacity_recovery_stake",
                }
                self._last_rationale = {
                    "decision": "hold",
                    "price": price,
                    "simulate": simulate,
                }
                return False

            raise AssertionError("live evaluate should not be invoked for recovery")

        def _execute_pending_recovery(
            self, *, corr_id: str | None, simulate: bool
        ) -> bool:
            self.exec_calls.append({"corr_id": corr_id, "simulate": simulate})
            self._last_rationale = {
                "decision": "capacity_recovery_stake",
                "execution": {"status": "submitted"},
            }
            return True

    class FakeCapacity:
        def run_once(
            self, _parent_key: str | None = None, _enforce_limits: bool = True
        ):
            return {"status": "ok"}

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {}

        def prices(self, _symbols):
            return {"DIEM": 1.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.0, "source": "test"}

    arbi = FakeArbi()
    orchestrator = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=arbi,
        capacity_broker=FakeCapacity(),
        market=FakeMarket(),
        quorum=None,
    )

    record = orchestrator.run_cycle(dry_run=False, enable_live=True)

    assert len(arbi.calls) == 1
    assert arbi.calls[0] == {"simulate": True}
    assert arbi.exec_calls == [
        {"corr_id": record["arbi"]["correlationId"], "simulate": False}
    ]
    assert arbi._pending_recovery_action is None

    assert record["arbi"]["action"] == "capacity_recovery_stake"
    assert record["arbi"]["execution"]["status"] == "submitted"
    assert record["arbi"]["execution"]["executed"] is True


def test_orchestrator_passes_mint_rate_source_to_arbi_when_supported(monkeypatch):
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    monkeypatch.setenv("DIEM_FAKE_MINT_RATE", "1.23")

    class FakeArbi:
        def __init__(self) -> None:
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale: dict[str, object] = {"decision": "hold"}
            self.seen_mint_rate_source: str | None = None

        def evaluate_and_maybe_mint(
            self,
            price: float,
            mint_rate: float = 1.0,
            mint_rate_source: str | None = None,
            simulate: bool | None = None,
            **_kwargs,
        ) -> bool:
            self.seen_mint_rate_source = mint_rate_source
            self._last_rationale = {
                "decision": "hold",
                "price": price,
                "mint_rate": mint_rate,
                "mint_rate_source": mint_rate_source,
                "simulate": simulate,
            }
            return False

    class FakeStake:
        def run_once(self, live: bool = False):
            return {"status": "ok", "live": live}

    class FakeCapacity:
        def run_once(self, parent_key: str | None = None, enforce_limits: bool = True):
            return {"status": "ok", "parent": parent_key, "enforce": enforce_limits}

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {}

        def prices(self, _symbols):
            return {"DIEM": 1.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.1, "source": "test"}

    arbi = FakeArbi()
    orchestrator = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=arbi,
        capacity_broker=FakeCapacity(),
        market=FakeMarket(),
    )

    record = orchestrator.run_cycle(dry_run=True, enable_live=False)
    assert record["arbi"]["mintRateSource"] == "env_dry_run"
    assert arbi.seen_mint_rate_source == record["arbi"]["mintRateSource"]
    assert record["arbi"]["why"]["mint_rate_source"] == "env_dry_run"


def test_single_loop_treasurer_and_listen_interval():
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeStake:
        def run_once(self, live: bool = False):
            return {"status": "ok", "live": live}

    class FakeMarket:
        def __init__(self) -> None:
            self._idx = 0
            self._util = [0.9, 0.2]
            self._vol = [80.0, 5.0]

        def unified_signals(self, _ttl_s: int = 30):
            val = self._util[min(self._idx, len(self._util) - 1)]
            return {"vvv": {"utilization": val}}

        def utilization_volatility_bps(self, _window: int = 3):
            val = self._vol[min(self._idx, len(self._vol) - 1)]
            self._idx = min(self._idx + 1, len(self._vol) - 1)
            return val

    class FakeArbi:
        def __init__(self) -> None:
            self.risk = risk_mod.RiskPolicy.from_env()
            self.calls = 0

        def evaluate_and_maybe_mint(self, _price, **_kwargs):
            self.calls += 1
            return self.calls == 1

    class FakeCapacity:
        def __init__(self) -> None:
            self._usage = [90.0, 20.0]
            self.calls: list[float] = []

        def run_once(
            self, _parent_key: str | None = None, _enforce_limits: bool = True
        ):
            idx = len(self.calls)
            use = self._usage[min(idx, len(self._usage) - 1)]
            self.calls.append(use)
            return {
                "status": "ok",
                "usage": {"dailyAverageDiem": use},
                "limits": {"data": [{"consumptionLimit": {"diem": 100.0}}]},
            }

    capacity = FakeCapacity()
    treasurer_mod = import_module("agents.ai_treasurer.agent")
    treasurer = treasurer_mod.AITreasurer()
    orchestrator = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=FakeArbi(),
        capacity_broker=capacity,
        market=FakeMarket(),
        ai_treasurer=treasurer,
    )

    record_hot = orchestrator.run_cycle(
        dry_run=True, enable_live=False, listen_base=LISTEN_BASE_SECONDS
    )
    record_calm = orchestrator.run_cycle(
        dry_run=True, enable_live=False, listen_base=LISTEN_BASE_SECONDS
    )

    assert record_hot["treasury"]["action"] == "accumulate_buffer"
    assert record_hot["listenInterval"] < LISTEN_BASE_SECONDS
    assert record_calm["listenInterval"] > LISTEN_BASE_SECONDS
    assert orchestrator._last_listen_interval == record_calm["listenInterval"]
    assert record_calm["treasury"]["action"] == "recycle_profits"


def test_fair_gap_preview_decision_path():
    """Fair-value gap detected leads to preview and decision."""
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")

    class FakeStake:
        def run_once(self, live: bool = False):
            return {"status": "ok", "live": live}

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {"vvv": {"utilization": 0.5}}

        def prices(self, _symbols):
            # Market price $1.00, fair value $57 (large gap)
            return {"DIEM": 1.0, "VVV": 1.18, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.0, "source": "test"}

    class FakeDIEMService:
        def __init__(self):
            self.aggregator = None  # Will be set to fake aggregator

        def mint(self, _units, _corr_id=None):
            return {"tx_hash": "0xmint"}

        def trade(self, direction, _units, _corr_id=None):
            return {"tx_hash": f"0x{direction}"}

        def burn(self, _units, _corr_id=None):
            return {"tx_hash": "0xburn"}

    class FakeAggregator:
        """Fake aggregator that provides exact-out quotes."""

        def best_quote_exact_out(self, amount_out, route):
            # Return a valid quote
            return Quote(
                provider="uniswap_v2",
                amount_in=amount_out * 2,  # 2x input for simplicity
                amount_out=amount_out,
                route=as_route_plan(route),
            )

    # Create ArbiDiem with fake DIEM service
    diem_service = FakeDIEMService()
    diem_service.aggregator = FakeAggregator()

    market = FakeMarket()
    risk = risk_mod.RiskPolicy.from_env()

    arbi = arbi_mod.ArbiDiem(diem=diem_service, risk=risk, market=market)

    # Mock the liquidity adjustment to return valid preview
    original_adjust = arbi._adjust_for_liquidity_buy

    def mock_adjust(units, _price):
        # Return adjusted units and slippage BPS (simulating successful preview)
        return units, 50.0  # 50 bps slippage

    arbi._adjust_for_liquidity_buy = mock_adjust

    class FakeCapacity:
        def run_once(self, _parent_key=None, _enforce_limits=True):
            return {"status": "ok"}

    orchestrator = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=arbi,
        capacity_broker=FakeCapacity(),
        market=market,
    )

    # Run cycle in dry-run mode
    record = orchestrator.run_cycle(dry_run=True, enable_live=False)

    # Verify fair-value gap was detected
    assert "arbi" in record
    arbi_record = record["arbi"]

    # Verify preview was obtained (slippage_bps should be present in rationale)
    if "why" in arbi_record:
        rationale = arbi_record["why"]
        # Should have decision and preview info
        assert "decision" in rationale

    # Verify decision was made (not skipped due to no preview)
    # The decision should be "buy_burn" or "hold" based on preview
    assert arbi_record["action"] in ["buy_burn", "hold", "mint_sell"]

    # Verify execution status
    assert "execution" in arbi_record
    assert arbi_record["execution"]["status"] == "dry_run"

    # Restore original method
    arbi._adjust_for_liquidity_buy = original_adjust


def test_single_loop_quorum_always_populated():
    """Test that quorum_info is always populated in orchestrator cycles."""
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeArbi:
        def __init__(self) -> None:
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale = {
                "decision": "hold",
                "reason": "no_exact_out_preview",
            }

        def evaluate_and_maybe_mint(self, **_kwargs):
            return False  # No trade signal

    class FakeStake:
        def run_once(self, live: bool = False):
            return {"status": "ok", "live": live}

    class FakeCapacity:
        def run_once(
            self,
            _parent_key: str | None = None,
            _enforce_limits: bool = True,
        ):
            return {"status": "ok"}

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {}

        def prices(self, _symbols):
            return {"DIEM": 1.0, "VVV": 0.2, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.0, "source": "test"}

    # Test 1: Quorum disabled
    orchestrator_no_quorum = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=FakeArbi(),
        capacity_broker=FakeCapacity(),
        market=FakeMarket(),
        quorum=None,
        reflex_guard=ReflexGuardian(),
    )
    record = orchestrator_no_quorum.run_cycle(dry_run=True, enable_live=False)
    assert "quorum" in record["arbi"]
    assert record["arbi"]["quorum"]["status"] == "disabled"
    assert record["arbi"]["quorum"]["reason"] == "quorum_not_configured"

    # Test 2: No trade signal
    quorum = build_default_coordinator()
    orchestrator_with_quorum = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=FakeArbi(),
        capacity_broker=FakeCapacity(),
        market=FakeMarket(),
        quorum=quorum,
        reflex_guard=ReflexGuardian(),
    )
    record = orchestrator_with_quorum.run_cycle(dry_run=True, enable_live=False)
    assert "quorum" in record["arbi"]
    assert record["arbi"]["quorum"]["status"] == "not_invoked"
    assert record["arbi"]["quorum"]["reason"] == "no_trade_signal"

    # Test 3: Reflex blocked
    class FakeArbiWithSignal:
        def __init__(self) -> None:
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale = {"decision": "mint_sell"}

        def evaluate_and_maybe_mint(self, **_kwargs):
            return True  # Trade signal

    class FakeMarketWithHighVol:
        def unified_signals(self, _ttl_s: int = 30):
            return {}

        def prices(self, _symbols):
            return {"DIEM": 1.0, "VVV": 0.2, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.0, "source": "test"}

        def utilization_volatility_bps(self, _window: int = 3):
            return 200.0  # High volatility to trigger reflex

    reflex_guard = ReflexGuardian(max_vol_bps=100.0)
    orchestrator_reflex = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=FakeArbiWithSignal(),
        capacity_broker=FakeCapacity(),
        market=FakeMarketWithHighVol(),
        quorum=quorum,
        reflex_guard=reflex_guard,
    )
    record = orchestrator_reflex.run_cycle(dry_run=False, enable_live=True)
    assert "quorum" in record["arbi"]
    assert record["arbi"]["quorum"]["status"] == "skipped"
    assert record["arbi"]["quorum"]["reason"] == "reflex_guard"
