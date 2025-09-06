from __future__ import annotations

from importlib import import_module


def test_uniswap_v2_trade_falls_back_to_fot(monkeypatch):
    prov_mod = import_module("libs.dex.providers")

    # Build a provider instance without running __init__ (avoid web3)
    provider = object.__new__(prov_mod.UniswapV2DexProvider)
    # Set required attributes
    provider.router_addr = "0xrouter"
    provider.recipient = "0xrecipient"
    # Disable allowance checks
    provider._ensure_allowance = lambda *args, **kwargs: None  # type: ignore[attr-defined]

    class _SwapObj:
        def build_transaction(self, data):  # noqa: ANN001
            return {"data": b"fot"}

    class _Functions:
        def swapExactTokensForTokens(self, *a, **k):  # noqa: ANN001
            raise RuntimeError("standard swap fails to trigger fot fallback")

        def swapExactTokensForTokensSupportingFeeOnTransferTokens(self, *a, **k):  # noqa: ANN001
            return _SwapObj()

    class _Router:
        functions = _Functions()

    provider.router = _Router()

    sent = {}

    def fake_send_tx(to: str, data):  # noqa: ANN001
        sent["to"] = to
        sent["data"] = data
        return "0xok"

    monkeypatch.setattr(prov_mod, "send_tx", fake_send_tx, raising=True)

    out = prov_mod.UniswapV2DexProvider.trade(provider, 100, 90, ["0xdiem", "0xusdc"])  # type: ignore[arg-type]
    assert out["tx_hash"] == "0xok"
    assert out.get("fot_fallback") == "true"

