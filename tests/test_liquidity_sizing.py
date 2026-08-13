from __future__ import annotations

from importlib import import_module


def test_liquidity_adjustment_halves_until_within_cap(monkeypatch):
    # Arrange a fake aggregator that worsens price for large sizes
    class FakeAgg:
        def best_quote(self, amount_in: int, path):
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
    monkeypatch.setenv("TRADE_PATH", "0xIn,0xOut")

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
    adjusted, last_bps = agent._adjust_for_liquidity(desired, market_price=2.0)
    # Should reduce at least once, and last_bps within cap
    assert adjusted < desired
    assert last_bps is not None and last_bps <= float(risk.slippage_bps_cap) + 1e-6


def test_liquidity_adjustment_continues_on_converged_slip(monkeypatch):
    class FlatAgg:
        def best_quote(self, amount_in: int, path):
            class Q:
                def __init__(self, ai: int, ao: int):
                    self.amount_in = ai
                    self.amount_out = ao

            def _out(px: float) -> int:
                return int((amount_in / 1e18) * px * 1e6)

            price = 1.96 if amount_in >= 100 * 10**18 else 2.0
            return Q(amount_in, _out(price))

    diem_mod = import_module("services.diem.client")
    svc = diem_mod.DIEMService(aggregator=FlatAgg())

    monkeypatch.setenv("TRADE_PATH", "0xIn,0xOut")

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)

    arbi_mod = import_module("agents.arbi_diem.agent")
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    monkeypatch.setattr(agent, "_decimals_out", lambda: 6, raising=True)

    desired = 400 * 10**18
    adjusted, last_bps = agent._adjust_for_liquidity(desired, market_price=2.0)

    assert adjusted < 100 * 10**18
    assert last_bps is not None and last_bps <= float(risk.slippage_bps_cap) + 1e-6


def test_liquidity_adjustment_respects_min_trade_usd(monkeypatch):
    """Test that liquidity adjustment stops at minimum trade USD threshold."""

    class HighSlippageAgg:
        def best_quote(self, amount_in: int, path):
            class Q:
                def __init__(self, ai: int, ao: int):
                    self.amount_in = ai
                    self.amount_out = ao

            def _out(px: float) -> int:
                return int((amount_in / 1e18) * px * 1e6)

            # All sizes have high slippage (100 bps)
            return Q(amount_in, _out(1.98))  # 1% worse = 100 bps

    diem_mod = import_module("services.diem.client")
    svc = diem_mod.DIEMService(aggregator=HighSlippageAgg())

    monkeypatch.setenv("TRADE_PATH", "0xIn,0xOut")
    # Set strict slippage cap and minimum trade USD
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")  # 50 bps cap
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "1.0")  # $1 minimum
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "10")

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)

    arbi_mod = import_module("agents.arbi_diem.agent")
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    monkeypatch.setattr(agent, "_decimals_out", lambda: 6, raising=True)

    # Market price 2.0, desired size 1000 DIEM = $2000
    # Min trade USD = $1 = 0.5 DIEM = 0.5e18 units
    desired = 1000 * 10**18
    adjusted, last_bps = agent._adjust_for_liquidity(desired, market_price=2.0)

    # Should stop at minimum trade size (not zero)
    min_units = risk.units_from_usd(1.0, 2.0)  # $1 at $2/DIEM = 0.5e18
    assert adjusted >= min_units
    # Slippage will still exceed cap, but we stopped at minimum
    assert last_bps is not None
    assert last_bps > 50.0  # Still exceeds cap


def test_liquidity_adjustment_respects_max_steps(monkeypatch):
    """Test that liquidity adjustment respects maximum adjustment steps."""
    quote_calls = []

    class ConstantSlippageAgg:
        def best_quote(self, amount_in: int, path):
            quote_calls.append(amount_in)

            class Q:
                def __init__(self, ai: int, ao: int):
                    self.amount_in = ai
                    self.amount_out = ao

            def _out(px: float) -> int:
                return int((amount_in / 1e18) * px * 1e6)

            # Constant high slippage regardless of size
            return Q(amount_in, _out(1.98))  # 100 bps

    diem_mod = import_module("services.diem.client")
    svc = diem_mod.DIEMService(aggregator=ConstantSlippageAgg())

    monkeypatch.setenv("TRADE_PATH", "0xIn,0xOut")
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "0.01")  # Very low min
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "5")  # Limit to 5 steps

    risk_mod = import_module("services.risk.policy")
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)

    arbi_mod = import_module("agents.arbi_diem.agent")
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    monkeypatch.setattr(agent, "_decimals_out", lambda: 6, raising=True)

    desired = 1000 * 10**18
    adjusted, last_bps = agent._adjust_for_liquidity(desired, market_price=2.0)

    # Should make at most 5 adjustment steps (initial + 5 iterations)
    # Plus initial check = 6 total calls max
    assert len(quote_calls) <= 6
    # Should have reduced size
    assert adjusted < desired
