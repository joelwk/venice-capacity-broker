from __future__ import annotations

from libs.dex.providers import DexAggregator, DexProvider, Quote


class FakeAerodromeExactOut(DexProvider):
    name = "aerodrome"

    def quote(self, amount_in: int, path):
        # exact-in is supported; return modest output to allow selection
        return Quote(provider=self.name, amount_in=amount_in, amount_out=95, path=path)

    def trade(self, amount_in: int, min_amount_out: int, path):
        assert min_amount_out <= 95
        return {"provider": self.name, "tx_hash": "0xaero"}

    def quote_exact_out(self, amount_out: int, path):
        # Aggregator should skip Aerodrome for exact-out; raise if called
        raise AssertionError("Aerodrome should be skipped for exact-out")


class FakeUniswapExactOut(DexProvider):
    name = "uniswap_v2"

    def quote(self, amount_in: int, path):
        # exact-in supported with slightly worse output than Aerodrome
        return Quote(provider=self.name, amount_in=amount_in, amount_out=90, path=path)

    def trade(self, amount_in: int, min_amount_out: int, path):
        assert min_amount_out <= 90
        return {"provider": self.name, "tx_hash": "0xuni"}

    def quote_exact_out(self, amount_out: int, path):
        # requires 2x input for simplicity
        return Quote(
            provider=self.name,
            amount_in=amount_out * 2,
            amount_out=amount_out,
            path=path,
        )

    def trade_exact_out(self, amount_out: int, max_amount_in: int, path):
        assert max_amount_in >= amount_out * 2
        return {"provider": self.name, "tx_hash": "0xuni_exact_out"}


def test_exact_out_skips_aerodrome_and_picks_uniswap():
    agg = DexAggregator([FakeAerodromeExactOut(), FakeUniswapExactOut()])
    q = agg.best_quote_exact_out(100, ["in", "out"])  # type: ignore[arg-type]
    assert q is not None
    assert q.provider == "uniswap_v2"
    assert q.amount_in == 200 and q.amount_out == 100


def test_exact_out_only_aerodrome_yields_no_quotes():
    agg = DexAggregator([FakeAerodromeExactOut()])
    q = agg.best_quote_exact_out(100, ["in", "out"])  # type: ignore[arg-type]
    assert q is None


def test_exact_in_can_use_aerodrome_and_trade():
    agg = DexAggregator([FakeAerodromeExactOut(), FakeUniswapExactOut()])
    # Aerodrome returns better amount_out (95 vs 90), so it should be selected
    q = agg.best_quote(100, ["in", "out"])  # type: ignore[arg-type]
    assert q is not None and q.provider == "aerodrome" and q.amount_out == 95
    # Trade on the selected provider with small slippage bps
    res = agg.trade_best(100, min_out_bps=100, path=["in", "out"])  # type: ignore[arg-type]
    assert res["tx_hash"] == "0xaero"


def test_exact_out_preview_uses_execution_allowlist_and_avoids_mode_unsupported_churn(
    monkeypatch,
):
    monkeypatch.setenv("DEX_EXEC_PROVIDERS", "aerodrome,uniswap_v2")

    agg = DexAggregator([FakeAerodromeExactOut(), FakeUniswapExactOut()])
    quotes = agg.quote_all_exact_out(100, ["in", "out"])  # type: ignore[arg-type]
    assert quotes
    assert all(q.provider != "aerodrome" for q in quotes)

    diags = getattr(agg, "_last_quote_diagnostics", []) or []
    assert not any(
        (
            d.get("provider") == "aerodrome"
            or str(d.get("provider")).lower() == "aerodrome"
        )
        for d in diags
        if isinstance(d, dict)
    )


def test_exact_in_trade_clamps_slippage_to_risk_cap(monkeypatch):
    """Exact-in executions must not exceed RISK_MAX_SLIPPAGE_BPS."""
    monkeypatch.setenv("RISK_MAX_SLIPPAGE_BPS", "50")

    class RecordingProvider(DexProvider):
        name = "uniswap_v2"

        def __init__(self) -> None:
            self.min_out: int | None = None

        def quote(self, amount_in: int, path):
            return Quote(
                provider=self.name, amount_in=amount_in, amount_out=100, path=path
            )

        def trade(self, amount_in: int, min_amount_out: int, path):
            self.min_out = int(min_amount_out)
            return {"provider": self.name, "tx_hash": "0xok"}

    prov = RecordingProvider()
    agg = DexAggregator([prov])

    # Request 5% slippage, but cap is 0.5% => min_out should be 99 (from 100 out).
    agg.trade_best(100, min_out_bps=500, path=["in", "out"])  # type: ignore[arg-type]
    assert prov.min_out == 99
