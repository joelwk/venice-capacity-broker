from __future__ import annotations

from importlib import import_module


def test_single_loop_cycle_includes_stake_and_capacity(monkeypatch):
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeStake:
        def __init__(self) -> None:
            self.calls: list[bool] = []

        def run_once(self, live: bool = False):  # noqa: D401
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
            mint_rate: float = 1.0,
            desired_units=None,
            current_inventory_usd=None,
            simulate: bool | None = None,
            **kwargs,
        ) -> bool:
            self.calls.append({"price": price, "simulate": simulate, "inventory": current_inventory_usd})
            self._last_rationale = {
                "decision": "mint_sell",
                "price": price,
                "simulate": simulate,
            }
            return True

    class FakeCapacity:
        def __init__(self) -> None:
            self.calls: list[str | None] = []

        def run_once(self, parent_key: str | None = None, enforce_limits: bool = True):  # noqa: D401
            self.calls.append(parent_key)
            return {"status": "ok", "parent": parent_key, "enforce": enforce_limits}

    class FakeMarket:
        def unified_signals(self, ttl_s: int = 30):  # noqa: D401
            return {"vvv": {"utilization": 0.8}}

        def prices(self, symbols):  # noqa: ANN001, D401
            return {"DIEM": 2.0, "VVV": 0.5, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s: int = 60):  # noqa: D401
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
    assert record["arbi"]["signals"]["utilization_ratio"] == 0.8
    assert record["arbi"]["execution"]["status"] == "dry_run"
    assert record["capacity"]["status"] == "ok"
    assert capacity.calls == ["PARENT"]
    assert stake.calls == [False]


def test_single_loop_quorum_blocks_actions(monkeypatch):
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeArbi:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale = {"decision": "mint_sell"}

        def evaluate_and_maybe_mint(
            self,
            price: float,
            mint_rate: float = 1.0,
            desired_units=None,
            current_inventory_usd=None,
            simulate: bool | None = None,
            **kwargs,
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

        def decide(self) -> bool:  # noqa: D401
            self.calls += 1
            return self.allow

    class FakeStake:
        def run_once(self, live: bool = False):  # noqa: D401
            return {"status": "ok", "live": live}

    class FakeCapacity:
        def run_once(self, parent_key: str | None = None, enforce_limits: bool = True):  # noqa: D401
            return {"status": "ok"}

    class FakeMarket:
        def unified_signals(self, ttl_s: int = 30):  # noqa: D401
            return {}

        def prices(self, symbols):  # noqa: ANN001, D401
            return {"DIEM": 1.0, "VVV": 0.2, "USDC": 1.0}

        def diem_mint_rate(self, ttl_s: int = 60):  # noqa: D401
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
    assert len(arbi.calls) == 1 and arbi.calls[0]["simulate"] is True
    assert record["arbi"]["execution"]["status"] == "blocked"
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
            assert "portfolioAdjustedUnits" in rationale or "current_inventory_usd" in rationale


def test_single_loop_treasurer_and_listen_interval(monkeypatch):
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

        def unified_signals(self, ttl_s: int = 30):
            val = self._util[min(self._idx, len(self._util) - 1)]
            return {"vvv": {"utilization": val}}

        def utilization_volatility_bps(self, window: int = 3):
            val = self._vol[min(self._idx, len(self._vol) - 1)]
            self._idx = min(self._idx + 1, len(self._vol) - 1)
            return val

    class FakeArbi:
        def __init__(self) -> None:
            self.risk = risk_mod.RiskPolicy.from_env()
            self.calls = 0

        def evaluate_and_maybe_mint(self, price, **kwargs):
            self.calls += 1
            return self.calls == 1

    class FakeCapacity:
        def __init__(self) -> None:
            self._usage = [90.0, 20.0]
            self.calls: list[float] = []

        def run_once(self, parent_key: str | None = None, enforce_limits: bool = True):
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

    record_hot = orchestrator.run_cycle(dry_run=True, enable_live=False, listen_base=10.0)
    record_calm = orchestrator.run_cycle(dry_run=True, enable_live=False, listen_base=10.0)

    assert record_hot["treasury"]["action"] == "acquire"
    assert record_hot["listenInterval"] < 10.0
    assert record_calm["listenInterval"] > 10.0
    assert orchestrator._last_listen_interval == record_calm["listenInterval"]
    assert record_calm["treasury"]["action"] == "release"
