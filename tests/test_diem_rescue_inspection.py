from __future__ import annotations

import time

from libs.dex.providers import _RESERVE_CACHE, DexAggregator, DexProvider, Quote
from libs.dex.routes import make_route


class FailingV3(DexProvider):
    name = "uniswap_v3"
    supports_exact_out = True

    def quote(self, amount_in: int, route):
        return None

    def trade(self, amount_in: int, min_amount_out: int, route):
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, route):
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route):
        raise NotImplementedError


class RescueAero(DexProvider):
    name = "aerodrome"
    supports_exact_out = True

    def quote(self, amount_in: int, route):
        plan = route if hasattr(route, "tokens") else make_route(route)
        return Quote(
            provider=self.name,
            amount_in=int(amount_in),
            amount_out=int(amount_in * 2),
            route=plan,
        )

    def trade(self, amount_in: int, min_amount_out: int, route):
        raise NotImplementedError

    def quote_exact_out(self, amount_out: int, route):
        return None

    def trade_exact_out(self, amount_out: int, max_amount_in: int, route):
        raise NotImplementedError


def test_diem_rescue_inspection_seeds_reserves(monkeypatch):
    diem_addr = "0xf4d97f2da56e8c3098f3a8d538db630a2606a024"
    vvv_addr = "0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf"
    quote_addr = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    pair_addr = "0x1111111111111111111111111111111111111111"

    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", diem_addr)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", vvv_addr)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", quote_addr)
    monkeypatch.setenv("DIEM_VVV_PAIR_ADDRESS", pair_addr)

    reserve_key = pair_addr.lower()
    monkeypatch.setitem(
        _RESERVE_CACHE,
        reserve_key,
        (time.monotonic(), (10**24, 5 * 10**23, diem_addr, vvv_addr)),
    )

    aggregator = DexAggregator([FailingV3(), RescueAero()])
    aggregator._discovery_providers = {"uniswap_v3"}
    aggregator._discovery_provider_names = ["uniswap_v3"]
    aggregator._execution_providers = {"uniswap_v3", "aerodrome"}
    aggregator._execution_provider_names = ["uniswap_v3", "aerodrome"]

    def fake_inspect(route_plan, amount, mode):
        return [
            {"provider": "uniswap_v3", "status": "empty"},
            {"provider": "aerodrome", "status": "empty"},
        ]

    aggregator._inspect_route = fake_inspect  # type: ignore[assignment]

    route = make_route(
        [quote_addr, "0x4200000000000000000000000000000000000006", diem_addr],
        fees=[500, 500],
    )
    quote = aggregator.best_quote(1_000_000_000_000_000_000, route)

    assert quote is not None
    assert quote.provider == "aerodrome"

    diag = aggregator._last_quote_diagnostics
    rescue_entries = [
        entry for entry in diag if entry.get("stage") == "rescue_inspection"
    ]

    assert rescue_entries, "rescue inspection diagnostics should be recorded"
    assert all(entry.get("status") == "ok" for entry in rescue_entries)
    assert any(
        entry.get("inspection_reason") == "diem_rescue_reserves"
        for entry in rescue_entries
    )
    assert all(entry.get("allowance", {}).get("seeded") for entry in rescue_entries)
    assert all(entry.get("balance", {}).get("seeded") for entry in rescue_entries)
    assert not any(entry.get("reason") == "inspection_empty" for entry in diag)
