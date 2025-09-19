from __future__ import annotations

from libs.dex.providers import DexAggregator, Quote, DexProvider
from libs.dex.routes import as_route_plan


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
