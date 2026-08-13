from __future__ import annotations

from importlib import import_module


def test_single_loop_cycle_summary_includes_broker_activity_counts():
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeStake:
        def run_once(self, live: bool = False):
            return {"status": "ok", "live": live}

    class FakeArbi:
        def __init__(self) -> None:
            self.risk = risk_mod.RiskPolicy.from_env()
            self._last_rationale = {"decision": "hold"}

        def evaluate_and_maybe_mint(self, *_args, **_kwargs):
            self._last_rationale = {"decision": "hold"}
            return False

    class FakeMarket:
        def unified_signals(self, _ttl_s: int = 30):
            return {}

        def prices(self, _symbols):
            return {"DIEM": 1.0, "VVV": 1.0, "USDC": 1.0}

        def diem_mint_rate(self, _ttl_s: int = 60):
            return {"tokens_per_diem": 1.0, "source": "test"}

    class FakeCapacity:
        def run_once(self, parent_key=None, enforce_limits: bool = True):  # type: ignore[no-untyped-def]
            return {
                "status": "ok",
                "issued_keys": 2,
                "revoked_keys": 1,
                "active_tenants": 3,
                "last_key_issue_ts": 1234567890,
                "usage": {"dailyAverageDiem": 10.0},
                "limits": {"data": [{"consumptionLimit": {"diem": 100.0}}]},
            }

    orch = orch_mod.SingleLoopOrchestrator(
        stake_master=FakeStake(),
        arbi=FakeArbi(),
        capacity_broker=FakeCapacity(),
        market=FakeMarket(),
        parent_key="PARENT",
    )

    record = orch.run_cycle(dry_run=True, enable_live=False)
    payload = orch._log_cycle_payload(record)

    cap = payload.get("capacity")
    assert isinstance(cap, dict)
    assert cap.get("issued_keys") == 2
    assert cap.get("revoked_keys") == 1
    assert cap.get("active_tenants") == 3
    assert cap.get("last_key_issue_ts") == 1234567890

    agents = payload.get("agents")
    assert isinstance(agents, dict)
    cap2 = agents.get("capacity_broker")
    assert isinstance(cap2, dict)
    assert cap2.get("issued_keys") == 2
