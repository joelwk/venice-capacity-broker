from __future__ import annotations

from libs.dex.providers import DexAggregator, Quote, DexProvider
from libs.dex.routes import as_route_plan, make_route


class FakeExactOutProvider(DexProvider):
    name = "fake_uni"
    supports_exact_out = True

    def quote(self, amount_in: int, path):  # noqa: ANN001
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):  # noqa: ANN001
        raise NotImplementedError

    # exact-out support
    def quote_exact_out(self, amount_out: int, path):  # noqa: ANN001
        plan = as_route_plan(path)
        # requires 2x input to keep ordering simple
        return Quote(provider=self.name, amount_in=amount_out * 2, amount_out=amount_out, route=plan)

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):  # noqa: ANN001
        assert max_amount_in >= amount_out * 2
        return {"provider": self.name, "tx_hash": "0xok"}


class FailingV3Provider(DexProvider):
    name = "uniswap_v3"
    supports_exact_out = True

    def quote(self, amount_in: int, path):  # noqa: ANN001
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):  # noqa: ANN001
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, path):  # noqa: ANN001
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):  # noqa: ANN001
        raise NotImplementedError


class V2ExactOutProvider(DexProvider):
    name = "uniswap_v2"
    supports_exact_out = True

    def quote(self, amount_in: int, path):  # noqa: ANN001
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):  # noqa: ANN001
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, path):  # noqa: ANN001
        plan = as_route_plan(path)
        plan.ensure_v2()
        return Quote(provider=self.name, amount_in=amount_out * 2, amount_out=amount_out, route=plan)

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):  # noqa: ANN001
        plan = as_route_plan(path)
        plan.ensure_v2()
        assert max_amount_in >= amount_out * 2
        return {"provider": self.name, "tx_hash": "0xok-v2"}


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

