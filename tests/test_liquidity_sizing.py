from __future__ import annotations

from importlib import import_module


def test_liquidity_adjustment_halves_until_within_cap(monkeypatch):
    # Arrange a fake aggregator that worsens price for large sizes
    class FakeAgg:
        def best_quote(self, amount_in: int, path):  # noqa: ANN001
            # amount_in is DIEM units (base units, 1e18); path unused
            # Market price we pass later is 2.0 USD/DIEM.
            # For large size (>= 1000e18), simulate exec price 1.97 (< 2.0),
            # for smaller sizes simulate 2.0 exact.
            class Q:
                def __init__(self, ai: int, ao: int):
                    self.amount_in = ai
                    self.amount_out = ao

            # Compute USDC minor units out: price * (amount_in / 1e18) * 1e6
            def _out(px: float) -> int:
                return int((amount_in / 1e18) * px * 1e6)

            if amount_in >= 1000 * 10**18:
                return Q(amount_in, _out(1.97))
            return Q(amount_in, _out(2.0))

    # Build DIEMService with fake aggregator
    diem_mod = import_module("services.diem.client")
    svc = diem_mod.DIEMService(aggregator=FakeAgg())
    # Path required by preview call; value unused by FakeAgg
    import os
    os.environ["TRADE_PATH"] = "0xIn,0xOut"

    # Risk policy uses DIEM decimals; avoid chain reads
    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)

    # Agent under test; avoid token out decimals on-chain
    arbi_mod = import_module("agents.arbi_diem.agent")
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    monkeypatch.setattr(agent, "_decimals_out", lambda: 6, raising=True)

    # Desired is large; market price 2.0
    desired = 2000 * 10**18
    adjusted, last_bps = agent._adjust_for_liquidity(desired, market_price=2.0)  # noqa: SLF001
    # Should reduce at least once, and last_bps within cap
    assert adjusted < desired
    assert last_bps is not None and last_bps <= float(risk.slippage_bps_cap) + 1e-6
