from __future__ import annotations

import pytest

from libs.dex.providers import DexAggregator, DexProvider, Quote
from libs.telemetry.metrics import render_prom


class _NoQuoteProvider(DexProvider):
    name = "noquote"

    def quote(self, amount_in: int, path):
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):
        raise NotImplementedError


def test_agg_no_quotes_increments_metric():
    agg = DexAggregator([_NoQuoteProvider()])
    q = agg.best_quote(123, ["in", "out"])  # type: ignore[arg-type]
    assert q is None
    prom = render_prom()
    assert "vvv_dex_agg_no_quotes_total" in prom


class _ErrorTradeExactOutProvider(DexProvider):
    name = "fake"

    def quote(self, amount_in: int, path):
        return None

    def trade(self, amount_in: int, min_amount_out: int, path):
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, path):
        return Quote(
            provider=self.name, amount_in=100, amount_out=amount_out, path=path
        )

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):
        raise RuntimeError("boom")


def test_agg_trade_errors_exact_out_increments_metric():
    agg = DexAggregator([_ErrorTradeExactOutProvider()])
    with pytest.raises(RuntimeError):
        agg.trade_best_exact_out(100, max_in_bps=50, path=["in", "out"])  # type: ignore[arg-type]
    prom = render_prom()
    # Find the errors metric line and assert it includes both labels
    lines = [
        ln
        for ln in prom.splitlines()
        if ln.startswith("vvv_dex_agg_trade_errors_total{")
    ]
    assert any(('provider="fake"' in ln and 'mode="exact_out"' in ln) for ln in lines)
