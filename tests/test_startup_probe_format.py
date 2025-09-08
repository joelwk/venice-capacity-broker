from __future__ import annotations

from services.marketdata.etherscan_verify import format_report


def test_startup_probe_formatting_smoke():
    # Minimal fake result to ensure the formatter is stable and compact
    fake = {
        "chainid": "8453",
        "path": [
            "0xf4d97f2da56e8c3098f3a8d538db630a2606a024",
            "0x4200000000000000000000000000000000000006",
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        ],
        "hops": [
            {
                "from": "DIEM",
                "to": "WETH",
                "uniswap_v2": {"pair": None, "reserves": None},
                "aerodrome_vol": {"pair": "0xabc", "reserves": (1, 2, 3)},
                "aerodrome_stable": {"pair": None, "reserves": None},
            },
            {
                "from": "WETH",
                "to": "USDC",
                "uniswap_v2": {"pair": "0xdef", "reserves": (3, 4, 5)},
                "aerodrome_vol": {"pair": None, "reserves": None},
                "aerodrome_stable": {"pair": None, "reserves": None},
            },
        ],
    }
    out = format_report(fake)
    assert "DEX verify (chain 8453)" in out
    assert "Hop 1" in out and "Hop 2" in out
    assert "Aerodrome Volatile: pair=0xabc reserves=1,2 ts=3" in out
    assert "UniswapV2: pair=0xdef reserves=3,4 ts=5" in out

