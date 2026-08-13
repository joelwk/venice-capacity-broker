from importlib import import_module


def test_preview_sell_intermediate_amount_out_does_not_emit_slippage_sanity_exceeded(
    monkeypatch,
):
    qmod = import_module("libs.dex.providers")
    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    # DIEM -> WETH -> USDC route, but quote.amount_out is the *intermediate* WETH amount.
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    weth_addr = "0x4200000000000000000000000000000000000006"
    usdc_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    route = make_route([diem_addr, weth_addr, usdc_addr])

    class StubAgg:
        def __init__(self):
            self._last_quote_diagnostics = []

        def quote_all(self, amount, route_plan):
            return [
                qmod.Quote(
                    provider="uniswap_v2",
                    amount_in=int(amount),
                    # WETH-scale amount returned for a route that ends in USDC.
                    amount_out=10**18,
                    route=route_plan,
                )
            ]

    class StubMarket:
        def prices(self, symbols):
            return {"DIEM": 1.0, "USDC": 1.0, "WETH": 2000.0}

    def fake_trade_routes(self, *, force_dynamic=False):
        return [route]

    monkeypatch.setattr(
        svc_mod.DIEMService, "trade_routes", fake_trade_routes, raising=True
    )
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("RISK_SLIPPAGE_SANITY_MAX_BPS", "50000")

    svc = svc_mod.DIEMService(aggregator=StubAgg(), market_data=StubMarket())
    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.SELL,
        token_in="DIEM",
        token_out="USDC",
        amount_base_units=10**18,
        slippage_bps=300,
    )

    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.slippage_bps is None
    assert result.diagnostics.get("slippage_sanity_exceeded") is None
    assert result.diagnostics.get("slippage_sanity_not_comparable") is True
    assert result.diagnostics.get("slippage_sanity_not_comparable_reason") in {
        "sanity_cap_exceeded",
        "route_mismatch",
        "missing_route",
        "usd_unavailable",
    }
    assert result.diagnostics.get("coherence_incoherent_preview") is None


def test_preview_buy_coherence_uses_usdc_and_diem_base_units(monkeypatch):
    import pytest

    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    # Route endpoints are unknown addresses so any route-decimal heuristics would
    # default to 18, which previously could produce a false incoherent-preview mute.
    route = make_route(
        [
            "0x00000000000000000000000000000000000000aa",
            "0x00000000000000000000000000000000000000bb",
        ]
    )

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols):
            return {str(s): self.price(str(s)) for s in (symbols or [])}

    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.10")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_TTL_SECONDS", "3600")

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [route]).__get__(svc, type(svc)),
        raising=False,
    )

    monkeypatch.setattr(
        svc,
        "quote",
        lambda side, amount, routes=None: {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "stub",
                    "amount_in": 100 * 1_000_000,  # 100 USDC
                    "amount_out": 10**18,  # 1.0 DIEM
                    "route": route,
                    "path": list(route.tokens),
                    "executable": True,
                }
            ],
            "diagnostics": [],
            "quote_summary": {},
        },
        raising=False,
    )

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=123,
        slippage_bps=50,
        preferred_route=route,
    )
    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.diagnostics.get("coherence_preview_price_usd") == pytest.approx(100.0)
    assert result.diagnostics.get("coherence_incoherent_preview") is None
    assert svc._is_route_muted(route, side="buy") is False


def test_preview_buy_selects_best_exact_in_quote_to_avoid_muting(monkeypatch):
    import pytest

    svc_mod = import_module("services.diem.client")
    exec_mod = import_module("services.diem.execution")
    from libs.dex.routes import make_route

    route = make_route(["0xusdc", "0xvvv", "0xdiem"])

    class MarketStub:
        def price(self, symbol: str) -> float:
            sym = str(symbol or "").strip().upper()
            if sym == "DIEM":
                return 100.0
            if sym == "USDC":
                return 1.0
            return 0.0

        def prices(self, symbols):
            return {str(s): self.price(str(s)) for s in (symbols or [])}

    monkeypatch.setenv("DIEM_DECIMALS", "18")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_BUY_EXECUTION_MODE", "exact_in")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.10")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_TTL_SECONDS", "3600")

    svc = svc_mod.DIEMService(aggregator=None, market_data=MarketStub())
    monkeypatch.setattr(
        svc,
        "trade_routes",
        (lambda self, force_dynamic=False: [route]).__get__(svc, type(svc)),
        raising=False,
    )

    amount_in = 100 * 1_000_000  # 100 USDC
    good_amount_out = 10**18  # 1.0 DIEM
    bad_amount_out = good_amount_out // 235  # ~234x divergence vs market

    monkeypatch.setattr(
        svc,
        "quote",
        lambda side, amount, routes=None: {
            "status": "ok",
            "side": side,
            "amount": amount,
            "quotes": [
                {
                    "provider": "bad",
                    "amount_in": amount_in,
                    "amount_out": int(bad_amount_out),
                    "route": route,
                    "path": list(route.tokens),
                    "executable": True,
                },
                {
                    "provider": "good",
                    "amount_in": amount_in,
                    "amount_out": int(good_amount_out),
                    "route": route,
                    "path": list(route.tokens),
                    "executable": True,
                },
            ],
            "diagnostics": [],
            "quote_summary": {"executable_quote_count": 2},
        },
        raising=False,
    )

    intent = exec_mod.ExecutionIntent(
        side=exec_mod.TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=123,
        slippage_bps=50,
        preferred_route=route,
    )
    result = svc.preview_trade(intent)
    assert result.status == exec_mod.ExecutionStatus.SIMULATED
    assert result.amount_out == int(good_amount_out)
    assert result.diagnostics.get("coherence_preview_price_usd") == pytest.approx(100.0)
    assert result.diagnostics.get("coherence_incoherent_preview") is None
    assert svc._is_route_muted(route, side="buy") is False
