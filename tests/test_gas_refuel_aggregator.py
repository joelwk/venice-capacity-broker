from __future__ import annotations

from types import SimpleNamespace


def test_gas_refuel_uses_env_aggregator_factory(monkeypatch):
    """Test that GasRefuelService uses the env-configured aggregator factory."""
    from services.wallet.gas_refuel import GasRefuelService

    calls = {"factory": 0, "quote": 0, "trade": 0}

    class FakeAggregator:
        def best_quote(self, amount_in, route):
            calls["quote"] += 1
            # Return a realistic ETH amount for USDC swap
            # $1 USDC at ETH=$3300 = ~0.0003 ETH = 0.0003e18 wei
            # For 1 USDC (1_000_000 wei, 6 decimals), output should be ~300_000_000_000_000 wei
            eth_out = int(
                amount_in * 300_000_000_000
            )  # Scaled for 6-decimal USDC to 18-decimal ETH
            return SimpleNamespace(amount_out=eth_out, provider="fake", route=route)

        def trade_best(self, amount_in, slippage_bps, route):
            calls["trade"] += 1
            return {"provider": "fake", "tx_hash": "0xtrade"}

    def factory():
        calls["factory"] += 1
        return FakeAggregator()

    monkeypatch.setattr(
        "libs.dex.providers.build_aggregator_from_env", factory, raising=True
    )

    refuel = GasRefuelService()
    monkeypatch.setattr(
        refuel, "_get_token_address", lambda symbol: "0xabc", raising=True
    )

    result = refuel._execute_swap_to_weth(symbol="USDC", amount_wei=1_000_000)

    assert calls["factory"] == 1
    assert calls["quote"] == 1
    assert calls["trade"] == 1
    assert result["success"] is True
    assert result.get("provider") == "fake"


def test_gas_refuel_blocks_catastrophic_quote(monkeypatch):
    """Test that GasRefuelService blocks swaps with unreasonably bad quotes.

    This guards against catastrophic losses like swapping 60 VVV for 0.001 ETH
    when fair value should be ~$66 worth of ETH (~0.02 ETH).
    """
    from services.wallet.gas_refuel import GasRefuelService

    calls = {"quote": 0, "trade": 0}

    class BadQuoteAggregator:
        def best_quote(self, amount_in, route):
            calls["quote"] += 1
            # Return a catastrophically bad quote: essentially 0 ETH output
            # This simulates the bug scenario where 60 VVV got ~0.001 ETH
            return SimpleNamespace(
                amount_out=1_000_000_000_000_000,  # 0.001 ETH - way below fair value
                provider="bad_pool",
                route=route,
            )

        def trade_best(self, amount_in, slippage_bps, route):
            calls["trade"] += 1
            return {"provider": "bad_pool", "tx_hash": "0xbad"}

    def factory():
        return BadQuoteAggregator()

    monkeypatch.setattr(
        "libs.dex.providers.build_aggregator_from_env", factory, raising=True
    )

    refuel = GasRefuelService()
    monkeypatch.setattr(
        refuel, "_get_token_address", lambda symbol: "0xvvv", raising=True
    )

    # Try to swap 60 VVV (60e18 wei) - should be worth ~$66 = ~0.02 ETH at $3300/ETH
    vvv_amount = 60_000_000_000_000_000_000  # 60 VVV in wei
    result = refuel._execute_swap_to_weth(symbol="VVV", amount_wei=vvv_amount)

    # Quote was called
    assert calls["quote"] == 1
    # Trade should NOT be called due to price sanity check
    assert calls["trade"] == 0
    # Result should indicate failure/blocking
    assert result["success"] is False
    assert "below fair value" in result.get("error", "").lower()
