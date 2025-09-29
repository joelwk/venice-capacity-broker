from __future__ import annotations

from datetime import datetime, timezone

import pytest

from db.models import DexPool
from db.session import create_db_and_tables, get_session
from services.marketdata import pools
from services.marketdata.provider import MarketDataProvider



def _addr(suffix: str) -> str:
    body = suffix.lower().rjust(40, "0")
    return "0x" + body[-40:]


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    # Ensure route cache does not leak across tests
    from services.marketdata import token_watcher

    monkeypatch.setenv("SQL_CREATE_ALL_ON_START", "true")
    token_watcher._PRICE_PATH_CACHE.clear()  # type: ignore[attr-defined]


def test_suggest_routes_for_tokens(tmp_path, monkeypatch):
    db_path = tmp_path / "pools.db"
    monkeypatch.setenv("SQL_DATABASE_URL", f"sqlite:///{db_path}")
    create_db_and_tables()

    token_a = _addr("1")
    token_b = _addr("2")
    token_c = _addr("3")
    token_d = _addr("4")

    now = datetime.now(timezone.utc)

    with next(get_session()) as session:  # type: ignore[call-arg]
        session.add(
            DexPool(
                pool_address=_addr("11"),
                factory_address=_addr("aa"),
                factory_type="uniswap_v2",
                chain_id=8453,
                token0=token_a.lower(),
                token1=token_b.lower(),
                fee=None,
                stable=None,
                tick_spacing=None,
                block_number=100,
                discovered_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                DexPool(
                    pool_address=_addr("12"),
                    factory_address=_addr("bb"),
                    factory_type="uniswap_v2",
                    chain_id=8453,
                    token0=token_a.lower(),
                    token1=token_c.lower(),
                    fee=None,
                    stable=None,
                    tick_spacing=None,
                    block_number=101,
                    discovered_at=now,
                    updated_at=now,
                ),
                DexPool(
                    pool_address=_addr("13"),
                    factory_address=_addr("cc"),
                    factory_type="uniswap_v2",
                    chain_id=8453,
                    token0=token_c.lower(),
                    token1=token_b.lower(),
                    fee=None,
                    stable=None,
                    tick_spacing=None,
                    block_number=102,
                    discovered_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.add_all(
            [
                DexPool(
                    pool_address=_addr("14"),
                    factory_address=_addr("dd"),
                    factory_type="uniswap_v3",
                    chain_id=8453,
                    token0=token_a.lower(),
                    token1=token_d.lower(),
                    fee=500,
                    stable=None,
                    tick_spacing=60,
                    block_number=103,
                    discovered_at=now,
                    updated_at=now,
                ),
                DexPool(
                    pool_address=_addr("15"),
                    factory_address=_addr("dd"),
                    factory_type="uniswap_v3",
                    chain_id=8453,
                    token0=token_d.lower(),
                    token1=token_b.lower(),
                    fee=1000,
                    stable=None,
                    tick_spacing=60,
                    block_number=104,
                    discovered_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()

    routes = pools.suggest_routes_for_tokens(token_a, token_b, max_routes=10)

    direct = tuple([token_a.lower(), token_b.lower()])
    assert any(tuple(route.tokens) == direct for route in routes)

    via_c = tuple([token_a.lower(), token_c.lower(), token_b.lower()])
    assert any(tuple(route.tokens) == via_c for route in routes)

    via_d = None
    fees = None
    for route in routes:
        toks = tuple(route.tokens)
        if toks == (token_a.lower(), token_d.lower(), token_b.lower()):
            via_d = toks
            fees = tuple(hop.fee for hop in route.hops)
            break
    assert via_d is not None
    assert fees == (500, 1000)


def test_route_candidates_prioritize_manual(monkeypatch, tmp_path):
    token_a = _addr("21")
    token_b = _addr("22")
    token_c = _addr("23")

    monkeypatch.setenv("TRADE_PATH", f"{token_a},{token_c},{token_b}")
    monkeypatch.setenv("DEX_PROVIDERS", "[]")
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", token_b)
    monkeypatch.delenv("WBTC_TOKEN_ADDRESS", raising=False)
    monkeypatch.delenv("TRADE_PATHS", raising=False)
    monkeypatch.delenv("TRADE_PATH_2", raising=False)
    monkeypatch.delenv("SQL_DATABASE_URL", raising=False)
    monkeypatch.delenv("DEX_BRIDGE_TOKEN_ADDRESS", raising=False)
    monkeypatch.delenv("MARKETDATA_WARM_SYMBOLS", raising=False)

    from unittest.mock import patch

    with patch.object(MarketDataProvider, "_warm_route_liquidity", lambda self, tokens: None), \
         patch.object(MarketDataProvider, "_validate_trade_paths", lambda self: None), \
         patch.object(MarketDataProvider, "_check_wbtc_configuration", lambda self: None):
        provider = MarketDataProvider()
        routes = provider.route_candidates(token_a, token_b)

        assert routes, "expected at least one candidate route"
        tokens_list = [tuple(route.tokens) for route in routes]
        assert (token_a.lower(), token_c.lower(), token_b.lower()) in tokens_list
