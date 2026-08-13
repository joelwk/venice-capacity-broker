from __future__ import annotations

from services.marketdata.dynamic_paths import (
    _MIN_DIRECT_LIQ_USD,
    _MIN_HOP_LIQ_USD,
    PairCandidate,
    _build_route_metadata,
)


def test_build_route_metadata_uniswap_direct():
    candidate = PairCandidate(
        tokens=("0xaaa", "0xbbb"),
        dex="uniswap_v3",
        fee=500,
        liquidity=_MIN_DIRECT_LIQ_USD * 2,
        pool="0xpool1",
    )
    meta = _build_route_metadata(
        hops=(candidate,),
        route_type="direct",
        source="dynamic:v3",
    )
    assert meta["discoveryOnly"] is False
    assert meta["venues"] == ["uniswap_v3"]
    assert meta["hops"][0]["fee"] == 500
    assert meta["hops"][0]["liquidityUsd"] >= _MIN_DIRECT_LIQ_USD
    assert meta["type"] == "direct"


def test_build_route_metadata_marks_discovery_only():
    aerodrome_candidate = PairCandidate(
        tokens=("0xaaa", "0xccc"),
        dex="aerodrome",
        fee=None,
        liquidity=_MIN_HOP_LIQ_USD * 1.5,
        pool="0xpool2",
    )
    usdc_candidate = PairCandidate(
        tokens=("0xccc", "0xbbb"),
        dex="uniswap_v2",
        fee=None,
        liquidity=_MIN_HOP_LIQ_USD * 2,
        pool="0xpool3",
    )
    meta = _build_route_metadata(
        hops=(aerodrome_candidate, usdc_candidate),
        route_type="bridge",
        source="dynamic:v2",
    )
    assert meta["discoveryOnly"] is True
    assert meta["venues"] == ["aerodrome", "uniswap_v2"]
    assert len(meta["hops"]) == 2
    assert meta["type"] == "bridge"
