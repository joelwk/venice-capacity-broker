from __future__ import annotations

from unittest.mock import patch

from libs.dex.providers import DexAggregator, DexProvider, Quote
from libs.dex.routes import make_route


def _addr(seed: str) -> str:
    return "0x" + (seed * 40)[:40]


class _StfOnceUniswapV3(DexProvider):
    name = "uniswap_v3"
    supports_exact_out = True

    def __init__(self) -> None:
        self.trade_calls = 0
        self.allowance_calls: list[tuple[str, str, str, int]] = []

    def quote(self, amount_in: int, route):  # pragma: no cover
        return None

    def trade(self, amount_in: int, min_amount_out: int, route):  # type: ignore[override]
        self.trade_calls += 1
        if self.trade_calls == 1:
            raise RuntimeError("execution reverted: STF")
        return {"provider": self.name, "tx_hash": "0x" + "c" * 64}

    def trade_exact_out(
        self, amount_out: int, max_amount_in: int, route
    ):  # pragma: no cover
        raise NotImplementedError

    def _ensure_allowance(
        self, token: str, owner: str, spender: str, required: int
    ) -> str:
        self.allowance_calls.append((token, owner, spender, int(required)))
        return "0x" + "d" * 64


def test_composite_exec_retries_once_on_stf_and_records_retry_info():
    provider = _StfOnceUniswapV3()
    agg = DexAggregator([provider])

    leg_route = make_route([_addr("1"), _addr("2")])
    leg_quote = Quote(
        provider="uniswap_v3", amount_in=100, amount_out=90, route=leg_route
    )
    composite_quote = Quote(
        provider="composite", amount_in=100, amount_out=90, route=leg_route
    )
    object.__setattr__(composite_quote, "_composite_legs", [leg_quote])

    with patch("libs.dex.composite._preflight_allowance_low") as mock_preflight:
        mock_preflight.return_value = {
            "status": "low",
            "token_in": _addr("1"),
            "owner": _addr("3"),
            "spender": _addr("4"),
            "allowance": 0,
            "required": 100,
        }
        res = agg._execute_composite_exact_in(
            composite_quote, 50, correlation_id="cid-2"
        )

    assert res["provider"] == "composite"
    assert res["correlation_id"] == "cid-2"
    assert provider.trade_calls == 2
    assert len(provider.allowance_calls) == 1
    assert res["legs"][0]["retry"]["retried"] is True
