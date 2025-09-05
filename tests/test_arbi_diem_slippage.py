from __future__ import annotations

from importlib import import_module


def test_arbi_diem_rejects_on_slippage(monkeypatch):
    # Arrange DIEMService with aggregator best_quote returning poor price
    qmod = import_module("libs.dex.providers")

    class FakeQuote(qmod.Quote):  # type: ignore[type-arg]
        pass

    class FakeAgg:
        def best_quote(self, amount_in, path):  # noqa: ANN001
            # amount_out implies exec price well below market
            return qmod.Quote(provider="uniswap_v2", amount_in=int(amount_in), amount_out=int(amount_in // 2), path=path)  # type: ignore[arg-type]

    svc_mod = import_module("services.diem.client")
    risk_mod = import_module("services.risk.policy")
    arbi_mod = import_module("agents.arbi_diem.agent")

    # Patch DIEMService._path_from_env to avoid env
    def fake_path(self):  # noqa: ANN001
        return ["0xdiem", "0xusdc"]

    monkeypatch.setattr(svc_mod.DIEMService, "_path_from_env", fake_path, raising=True)

    # Patch out decimals lookups
    risk = risk_mod.RiskPolicy.from_env()
    monkeypatch.setattr(risk, "_diem_decimals", lambda: 18, raising=True)
    monkeypatch.setattr(arbi_mod.ArbiDiem, "_decimals_out", lambda self: 6, raising=True)

    svc = svc_mod.DIEMService(aggregator=FakeAgg())
    agent = arbi_mod.ArbiDiem(diem=svc, risk=risk)
    # Market price 1.0; exec preview ~0.5 -> 5000 bps slippage; cap default 150 -> reject
    did = agent.evaluate_and_maybe_mint(market_price=1.0, mint_rate=1.0, desired_units=10 ** 18)
    assert did is False

