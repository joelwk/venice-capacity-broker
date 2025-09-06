from __future__ import annotations

import pytest

from libs.dex.providers import DexAggregator, DexProvider, Quote
from libs.telemetry.metrics import render_prom


class _FlakyProvider(DexProvider):
    name = "flaky"

    def __init__(self):  # noqa: D401
        self.calls = 0

    def quote(self, amount_in: int, path):  # noqa: ANN001
        return Quote(provider=self.name, amount_in=amount_in, amount_out=amount_in, path=path)

    def trade(self, amount_in: int, min_amount_out: int, path):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("fail")


def test_circuit_opens_and_skips(monkeypatch):
    monkeypatch.setenv("DEX_CIRCUIT_FAILURES", "1")
    monkeypatch.setenv("DEX_CIRCUIT_COOL_OFF_SECONDS", "60")

    prov = _FlakyProvider()
    agg = DexAggregator([prov])

    with pytest.raises(RuntimeError):
        agg.trade_best(100, min_out_bps=0, path=["in", "out"])  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        agg.trade_best(100, min_out_bps=0, path=["in", "out"])  # type: ignore[arg-type]

    prom = render_prom()
    assert "vvv_dex_circuit_open_total{provider=\"flaky\"}" in prom
    assert "vvv_dex_circuit_skips_total{provider=\"flaky\"}" in prom

