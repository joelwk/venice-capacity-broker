from __future__ import annotations

from libs.dex.routes import make_route
from services.diem.client import DIEMService


class _DummyAggregator:
    def __init__(self, good_route):
        self.good_tokens = list(good_route.tokens)

    def quote_all(self, amount, route):
        try:
            tokens = list(route.tokens)
        except Exception:
            tokens = []
        return ["ok"] if tokens == self.good_tokens else []


class _DummyProvider:
    def __init__(self, env_route, dyn_route):
        self._env_route = env_route
        self._dyn_route = dyn_route

    def _collect_trade_paths(self, force_dynamic: bool = False):
        if force_dynamic:
            return [self._env_route, self._dyn_route]
        return [self._env_route]

    def _address_for_symbol(self, symbol: str) -> str | None:
        mapping = {
            "DIEM": "0xdiem",
            "USDC": "0xusdc",
            "WETH": "0xweth",
            "ETH": "0xweth",
            "VVV": "0xvvv",
        }
        return mapping.get(symbol.upper())

    def route_metadata(self, route):
        return {"source": "dynamic" if route is self._dyn_route else "env"}

    def get_decimals(self, token_address: str) -> int:
        return 18


def test_trade_routes_fall_back_to_dynamic_when_env_path_dead(monkeypatch):
    env_route = make_route(["diem", "weth", "usdc"])
    dyn_route = make_route(["diem", "usdc"])

    provider = _DummyProvider(env_route, dyn_route)
    agg = _DummyAggregator(dyn_route)

    svc = DIEMService(aggregator=agg, market_data=provider)

    routes = svc.trade_routes()

    assert routes, "expected at least one route"
    assert routes[0] is dyn_route
    assert any(route is dyn_route for route in routes)


def test_incoherent_preview_mute_scoped_to_side(monkeypatch):
    monkeypatch.setenv("TRADE_PATH", "0xusdc,0xdiem")
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0xdiem")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0xusdc")
    monkeypatch.setenv("QUOTE_TOKEN_DECIMALS", "6")
    monkeypatch.setenv("DIEM_DECIMALS", "18")
    # Disable coherence relaxation so this test deterministically asserts muting.
    monkeypatch.setenv("DIEM_COHERENCE_BRIDGE_MIN_USD", "0")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_ENABLE", "1")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MAX_REL_DIFF", "0.50")
    monkeypatch.setenv("DIEM_ROUTE_COHERENCE_MUTE_TTL_SECONDS", "3600")

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

    svc = DIEMService(aggregator=None, market_data=MarketStub())
    route = make_route(["0xusdc", "0xdiem"])
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
                    "amount_in": 1_000_000,  # 1 USDC
                    "amount_out": 1_000_000_000_000_000,  # 0.001 DIEM
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

    from services.diem.execution import ExecutionIntent, TradeSide

    intent = ExecutionIntent(
        side=TradeSide.BUY,
        token_in="USDC",
        token_out="DIEM",
        amount_base_units=123,
        slippage_bps=50,
        preferred_route=route,
        metadata={"diem_market_price_usd": 100.0},
    )
    svc.preview_trade(intent)

    assert svc._is_route_muted(route, side="buy") is True
    assert svc._is_route_muted(route, side="sell") is False


def test_trade_routes_filters_weth_when_disabled(monkeypatch):
    monkeypatch.setenv("DIEM_DISABLE_CANONICAL_WETH", "1")
    monkeypatch.setenv("WETH_ADDRESS", "0xweth")

    env_route = make_route(["diem", "weth", "usdc"])
    dyn_route = make_route(["diem", "usdc"])

    class Provider(_DummyProvider):
        def _collect_trade_paths(self, force_dynamic: bool = False):
            return [self._env_route, self._dyn_route]

    provider = Provider(env_route, dyn_route)
    svc = DIEMService(aggregator=None, market_data=provider)

    routes = svc.trade_routes()
    assert routes
    assert routes[0] is dyn_route
