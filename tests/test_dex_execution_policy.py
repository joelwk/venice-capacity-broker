from __future__ import annotations

from libs.dex.providers import DexAggregator, DexProvider, Quote


class DiscoveryOnlyProvider(DexProvider):
    name = "aerodrome"

    def quote(self, amount_in: int, path):
        return Quote(provider=self.name, amount_in=amount_in, amount_out=95, path=path)

    def trade(self, amount_in: int, min_amount_out: int, path):
        raise AssertionError("discovery-only provider should not execute trades")


class ExecutableProvider(DexProvider):
    name = "uniswap_v2"

    def quote(self, amount_in: int, path):
        return Quote(provider=self.name, amount_in=amount_in, amount_out=90, path=path)

    def trade(self, amount_in: int, min_amount_out: int, path):
        assert min_amount_out <= 90
        return {"provider": self.name, "tx_hash": "0xexec"}


def test_trade_respects_execution_allowlist(monkeypatch):
    monkeypatch.setenv("DEX_DISCOVERY_PROVIDERS", "aerodrome,uniswap_v2")
    monkeypatch.setenv("DEX_EXEC_PROVIDERS", "uniswap_v2")
    try:
        agg = DexAggregator([DiscoveryOnlyProvider(), ExecutableProvider()])
        preview = agg.best_quote(100, ["in", "out"])  # type: ignore[arg-type]
        assert preview is not None
        # Discovery-only venue provides the better preview quote.
        assert preview.provider == "aerodrome"

        result = agg.trade_best(100, min_out_bps=100, path=["in", "out"])  # type: ignore[arg-type]
        assert result["provider"] == "uniswap_v2"
    finally:
        monkeypatch.delenv("DEX_DISCOVERY_PROVIDERS", raising=False)
        monkeypatch.delenv("DEX_EXEC_PROVIDERS", raising=False)
