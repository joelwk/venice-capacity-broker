import types

from graph.workflows.orchestrator import SingleLoopOrchestrator


class FakeStakeMaster:
    def __init__(self) -> None:
        self.last_ingested = None
        self.last_run_recommendation = None

    def ingest_recommendation(self, rec):
        self.last_ingested = rec

    def run_once(self, *, live=False, recommendation=None):
        self.last_run_recommendation = recommendation
        return {"status": "ok", "recommendation": recommendation}


class FakeArbi:
    def __init__(self):
        self._last_rationale = None
        self.risk = types.SimpleNamespace(
            exposure_usd=lambda **kwargs: (0, {}),
            volatility_bps=lambda hist: 0,
        )


class FakeMarket:
    def prices(self, symbols):
        return {"DIEM": 1.0, "VVV": 1.0, "USDC": 1.0}

    def unified_signals(self, ttl_s=30):
        return {}

    def utilization_volatility_bps(self, window=3):
        return 0.0


class FakeCapacity:
    def run_once(self, parent_key=None):
        return {"status": "ok"}


def test_orchestrator_carries_stake_recommendation_to_next_cycle(monkeypatch):
    stake = FakeStakeMaster()
    arbi = FakeArbi()
    market = FakeMarket()
    cap = FakeCapacity()

    orch = SingleLoopOrchestrator(
        stake_master=stake,
        arbi=arbi,
        capacity_broker=cap,
        market=market,
        quorum=None,
        ai_treasurer=None,
        parent_key=None,
        memory_store=None,
        reflection=None,
        reflex_guard=None,
        portfolio_inventory=None,
    )

    # Keep quote/gas checks no-op for the test
    monkeypatch.setattr(
        orch, "_check_quote_consolidation", lambda dry_run, force=False: None
    )
    monkeypatch.setattr(orch, "_check_gas_refuel", lambda dry_run: None)

    stake_rec = {"requested_svvv_units": 123, "reason": "insufficient_svvv"}

    def _invoke_arbi_first(*args, **kwargs):
        arbi._last_rationale = {
            "decision": "hold",
            "reason": "insufficient_svvv",
            "stake_recommendation": stake_rec,
        }
        return False

    def _invoke_arbi_second(*args, **kwargs):
        return False

    # First cycle: capture recommendation
    orch._invoke_arbi = types.MethodType(_invoke_arbi_first, orch)
    orch.run_cycle(
        dry_run=True, enable_live=False, mint_rate=1.0, progressive_live=False
    )
    assert orch._pending_stake_recommendation is not None
    assert orch._pending_stake_recommendation.get("requested_svvv_units") == 123
    assert orch._pending_stake_recommendation.get("reason") == "insufficient_svvv"

    # Second cycle: recommendation ingested by StakeMaster
    orch._invoke_arbi = types.MethodType(_invoke_arbi_second, orch)
    orch.run_cycle(
        dry_run=True, enable_live=False, mint_rate=1.0, progressive_live=False
    )

    assert stake.last_ingested is not None
    assert stake.last_ingested.get("requested_svvv_units") == 123
    assert stake.last_run_recommendation is not None
    assert stake.last_run_recommendation.get("requested_svvv_units") == 123
