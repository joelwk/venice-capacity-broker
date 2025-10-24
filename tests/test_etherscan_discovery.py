from __future__ import annotations

from importlib import import_module


def test_reserve_cap_units_direct_pair(monkeypatch):
    md_mod = import_module("services.marketdata.provider")
    es_mod = import_module("services.marketdata.etherscan_verify")

    # Addresses (40-hex lower)
    diem = "0x" + "1" * 40
    usdc = "0x" + "2" * 40
    pair = "0x" + "f" * 40

    # Stub discovery to return a uniswap_v2 entry with pair
    def fake_verify(path):  # noqa: ANN001
        return {
            "chainid": "8453",
            "path": path,
            "hops": [
                {
                    "from": path[0],
                    "to": path[1],
                    "uniswap_v2": {"pair": pair, "reserves": (1000 * 10**18, 1_000_000 * 10**6)},
                    "aerodrome_vol": {"pair": None, "reserves": None},
                    "aerodrome_stable": {"pair": None, "reserves": None},
                }
            ],
        }

    monkeypatch.setattr(es_mod, "verify_trade_path", fake_verify, raising=True)
    # token0/token1 identify mapping: token0=diem, token1=usdc
    monkeypatch.setattr(es_mod, "get_token0", lambda addr: diem, raising=True)
    monkeypatch.setattr(es_mod, "get_token1", lambda addr: usdc, raising=True)

    monkeypatch.setenv("RISK_MAX_POOL_TAKE_BPS", "100")  # 1%
    monkeypatch.setenv("DIEM_TOKEN_ADDRESS", "0x" + "3" * 40)
    monkeypatch.setenv("VVV_TOKEN_ADDRESS", "0x" + "4" * 40)
    monkeypatch.setenv("QUOTE_TOKEN_ADDRESS", "0x" + "5" * 40)

    provider = md_mod.MarketDataProvider()
    cap = provider.reserve_cap_units([diem, usdc])
    # 1% of 1000e18 = 10e18
    assert cap == 10 * 10**18

