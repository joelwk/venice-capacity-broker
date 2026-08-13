from __future__ import annotations

from importlib import import_module


def test_orchestrator_passes_util_and_volatility(monkeypatch):
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeMarket:
        def __init__(self):
            self._i = 0
            self._px = [1.00, 1.05, 1.11]

        def unified_signals(self, ttl_s=30):
            return {"vvv": {"utilization": 0.8}}

        def prices(self, symbols):
            # Return a changing DIEM price for realized vol computation
            px = self._px[min(self._i, len(self._px) - 1)]
            self._i += 1
            return {"DIEM": px, "USDC": 1.0, "VVV": 1.0}

    seen = {}

    class FakeArbi:
        def __init__(self):
            self.risk = risk_mod.RiskPolicy.from_env()

        def evaluate_and_maybe_mint(
            self,
            price,
            mint_rate=1.0,
            desired_units=None,
            current_inventory_usd=None,
            utilization_ratio=None,
            vol_bps=None,
            **kwargs,
        ):
            seen["util"] = utilization_ratio
            seen["vol"] = vol_bps
            return True

    orch = orch_mod.Orchestrator(market=FakeMarket(), arbi=FakeArbi())
    # Run multiple times to accumulate a small price history
    orch.run_once(dry_run=False)
    orch.run_once(dry_run=False)
    r3 = orch.run_once(dry_run=False)
    # Utilization should be propagated
    assert seen.get("util") == 0.8
    # With >= 3 points and different returns, realized vol should be positive
    assert isinstance(seen.get("vol"), (int, float)) and float(seen["vol"]) >= 0.0
    # Orchestrator record exposes the same signals
    assert "signals" in r3 and r3["signals"]["utilization_ratio"] == 0.8
    assert r3["signals"]["utilization_source"] == "venice"


def test_orchestrator_falls_back_to_onchain_mint_rate_and_preserves_missing_util(
    monkeypatch,
):
    orch_mod = import_module("graph.workflows.orchestrator")
    risk_mod = import_module("services.risk.policy")

    class FakeMarket:
        def unified_signals(self, ttl_s=30):
            # Utilization is missing; orchestrator should apply a baseline fallback.
            return {"vvv": {}}

        def prices(self, symbols):
            return {"DIEM": 1.0, "USDC": 1.0, "VVV": 1.0}

        def diem_mint_rate(self, ttl_s=60):
            # Simulate a common production misconfiguration: an env-derived placeholder
            # mint-rate (1.0) that should NOT mask the real on-chain rate.
            return {"tokens_per_diem": 1.0, "source": "env_float"}

    seen = {}

    class FakeDiem:
        def _query_mint_rate_onchain_safe(self):
            # 250 tokens per DIEM, scaled by 1e18 as per services.diem.client contract query
            return int(250 * 10**18)

    class FakeArbi:
        def __init__(self):
            self.risk = risk_mod.RiskPolicy.from_env()
            self.diem = FakeDiem()

        def evaluate_and_maybe_mint(
            self,
            price,
            mint_rate=1.0,
            utilization_ratio=None,
            **kwargs,
        ):
            seen["mint_rate"] = float(mint_rate)
            seen["util"] = utilization_ratio
            return True

    orch = orch_mod.Orchestrator(market=FakeMarket(), arbi=FakeArbi())
    monkeypatch.setenv("DIEM_FV_ADOPTION_BASE", "0.55")
    r1 = orch.run_once(dry_run=False)

    assert seen.get("util") == 0.55
    assert seen.get("mint_rate") == 250.0
    assert r1["signals"]["utilization_ratio"] == 0.55
    assert r1["signals"]["utilization_source"] == "fallback"


def test_arbi_diem_rationale_uses_size_with_risk(monkeypatch):
    svc_mod = import_module("services.diem.client")
    arbi_mod = import_module("agents.arbi_diem.agent")
    risk_mod = import_module("services.risk.policy")
    pricing_mod = import_module("libs.pricing.diem")

    # Avoid on-chain actions by using a fake DIEM actions implementation and simulate=True
    class FakeActions:
        def mint(self, amount: int):
            return {"tx_hash": "0xmint"}

        def burn(self, amount: int):
            return {"tx_hash": "0xburn"}

        def trade(self, side: str, amount: int):
            return {"tx_hash": "0xtrade"}

    actions_mod = import_module("libs.agentkit_ext.actions")
    monkeypatch.setattr(actions_mod, "DIEMACTIONS", lambda: FakeActions(), raising=True)

    # Ensure market deemed favorable: set fair value modest versus price
    def mock_fair_value(*, vvv_price: float, mint_rate: float, **_: object) -> float:
        return vvv_price * mint_rate * 1.2

    monkeypatch.setattr(
        pricing_mod, "fair_value_per_diem", mock_fair_value, raising=True
    )

    # Risk environment: allow large caps, set decimals, and force volatility cap
    monkeypatch.setenv("RISK_MAX_DIEM_TRADE_USD", "100000")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_UTIL_ALPHA", "0.5")
    monkeypatch.setenv("RISK_MAX_VOLATILITY_BPS", "200")

    svc = svc_mod.DIEMService(aggregator=None)
    risk = risk_mod.RiskPolicy.from_env()
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)

    desired = 100 * 10**18  # 100 DIEM
    util = 0.5  # multiplier = 1 + 0.5*0.5 = 1.25
    vol = 400.0  # cap => scale 200/400 = 0.5
    # Expected suggested units after size_with_risk: 100 * 1.25 * 0.5 = 62.5 DIEM
    expected = int(62.5 * 10**18)
    did = agent.evaluate_and_maybe_mint(
        market_price=10.0,
        mint_rate=1.0,
        desired_units=desired,
        current_inventory_usd=None,
        utilization_ratio=util,
        vol_bps=vol,
        simulate=True,
    )
    assert did is True
    rationale = getattr(agent, "_last_rationale", {})
    assert int(rationale.get("suggested_units", 0)) == expected
    assert rationale.get("decision") == "mint_sell"
