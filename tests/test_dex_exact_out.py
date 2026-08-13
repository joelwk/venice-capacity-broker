from __future__ import annotations

from libs.dex.providers import DexAggregator, DexProvider, Quote
from libs.dex.routes import as_route_plan, make_route


class FakeExactOutProvider(DexProvider):
    name = "fake_uni"
    supports_exact_out = True

    def quote(self, amount_in: int, path):
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):
        raise NotImplementedError

    # exact-out support
    def quote_exact_out(self, amount_out: int, path):
        plan = as_route_plan(path)
        # requires 2x input to keep ordering simple
        return Quote(
            provider=self.name,
            amount_in=amount_out * 2,
            amount_out=amount_out,
            route=plan,
        )

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):
        assert max_amount_in >= amount_out * 2
        return {"provider": self.name, "tx_hash": "0xok"}


class FailingV3Provider(DexProvider):
    name = "uniswap_v3"
    supports_exact_out = True

    def quote(self, amount_in: int, path):
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, path):
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):
        raise NotImplementedError


class V2ExactOutProvider(DexProvider):
    name = "uniswap_v2"
    supports_exact_out = True

    def quote(self, amount_in: int, path):
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, path):
        plan = as_route_plan(path)
        plan.ensure_v2()
        return Quote(
            provider=self.name,
            amount_in=amount_out * 2,
            amount_out=amount_out,
            route=plan,
        )

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):
        plan = as_route_plan(path)
        plan.ensure_v2()
        assert max_amount_in >= amount_out * 2
        return {"provider": self.name, "tx_hash": "0xok-v2"}


class RevertingExactOutProvider(DexProvider):
    """Provider that reverts on exact-out but succeeds on exact-in fallback."""

    name = "revert_fallback"
    supports_exact_out = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    def quote_exact_out(self, amount_out: int, path):
        plan = as_route_plan(path)
        return Quote(
            provider=self.name,
            amount_in=amount_out * 2,
            amount_out=amount_out,
            route=plan,
        )

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):
        self.calls.append(("exact_out", amount_out, max_amount_in))
        raise RuntimeError("revert_on_exact_out")

    def quote(self, amount_in: int, path):
        plan = as_route_plan(path)
        return Quote(
            provider=self.name,
            amount_in=amount_in,
            amount_out=amount_in // 2,
            route=plan,
        )

    def trade(self, amount_in: int, min_amount_out: int, path):
        self.calls.append(("exact_in", amount_in, min_amount_out))
        return {
            "provider": self.name,
            "mode": "exact_in",
            "amount_in": amount_in,
            "min_out": min_amount_out,
            "tx_hash": "0xfallback",
        }


class AnalyticQuoteProvider(RevertingExactOutProvider):
    """Provider that returns non-executable quotes to force direct fallback."""

    name = "analytic_only"

    def quote_exact_out(self, amount_out: int, path):
        quote = super().quote_exact_out(amount_out, path)
        object.__setattr__(quote, "executable", False)
        return quote


class StepDownProvider(RevertingExactOutProvider):
    """Provider that requires step-down sizing before trades succeed."""

    name = "step_down"

    def trade(self, amount_in: int, min_amount_out: int, path):
        self.calls.append(("exact_in", amount_in, min_amount_out))
        if min_amount_out > 60:
            raise RuntimeError("slippage_high")
        return {
            "provider": self.name,
            "mode": "exact_in",
            "amount_in": amount_in,
            "min_out": min_amount_out,
            "tx_hash": "0xstep",
        }


def test_aggregator_best_quote_exact_out_picks_min_input():
    prov = FakeExactOutProvider()
    agg = DexAggregator([prov])
    q = agg.best_quote_exact_out(100, ["in", "out"])  # type: ignore[arg-type]
    assert q is not None
    assert q.provider == "fake_uni"
    assert q.amount_in == 200 and q.amount_out == 100


def test_aggregator_trade_best_exact_out_uses_provider_with_slippage():
    prov = FakeExactOutProvider()
    agg = DexAggregator([prov])
    res = agg.trade_best_exact_out(100, max_in_bps=100, route=["in", "out"])  # type: ignore[arg-type]
    assert res["tx_hash"] == "0xok"


def test_aggregator_exact_out_falls_back_to_v2():
    v3 = FailingV3Provider()
    v2 = V2ExactOutProvider()
    agg = DexAggregator([v3, v2])
    v3_route = make_route(["token_in", "token_out"], [3000])
    quote = agg.best_quote_exact_out(100, v3_route)
    assert quote is not None
    assert quote.provider == "uniswap_v2"
    assert quote.amount_out == 100


def test_trade_best_exact_out_reverts_then_exact_in_fallback(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "0")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "0")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "1")

    prov = RevertingExactOutProvider()
    agg = DexAggregator([prov])
    route = make_route(["token_in", "token_out"])

    res = agg.trade_best_exact_out(100, max_in_bps=100, route=route)

    assert res.get("tx_hash") == "0xfallback"
    # Provider should have attempted exact-out then fallen back to exact-in
    assert any(call[0] == "exact_out" for call in prov.calls)
    assert any(call[0] == "exact_in" for call in prov.calls)


def test_trade_best_exact_out_handles_non_executable_quote(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "0")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "0")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "1")

    prov = AnalyticQuoteProvider()
    agg = DexAggregator([prov])
    route = make_route(["token_in", "token_out"])

    res = agg.trade_best_exact_out(50, max_in_bps=50, route=route)

    assert res.get("tx_hash") == "0xfallback"
    # Non-executable quote should skip exact-out trade path entirely
    assert all(call[0] != "exact_out" for call in prov.calls)
    assert any(call[0] == "exact_in" for call in prov.calls)


def test_exact_in_fallback_steps_down_until_executable(monkeypatch):
    monkeypatch.setenv("DIEM_DECIMALS", "0")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "0")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD", "1")
    monkeypatch.setenv("ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS", "4")

    prov = StepDownProvider()
    agg = DexAggregator([prov])
    route = make_route(["token_in", "token_out"])

    res = agg.trade_best_exact_out(120, max_in_bps=100, route=route)

    assert res.get("tx_hash") == "0xstep"
    # First attempt should fail, later attempt should succeed with a lower min_out
    assert len(prov.calls) >= 2
    successful_calls = [c for c in prov.calls if c[0] == "exact_in" and c[2] <= 60]
    assert successful_calls, (
        "expected fallback sizing to reach a smaller executable trade"
    )
