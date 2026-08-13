from __future__ import annotations

import json
import os
from dataclasses import dataclass

from libs.dex.routes import RoutePlan, as_route_plan, make_route


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _coerce_route_entry(entry: object) -> str | None:
    if entry is None:
        return None
    if isinstance(entry, str):
        cleaned = entry.strip()
        return cleaned or None
    if isinstance(entry, (list, tuple)):
        try:
            return json.dumps(entry)
        except Exception:
            return None
    if isinstance(entry, dict):
        try:
            return json.dumps(entry)
        except Exception:
            return None
    return None


def _parse_route_spec(raw: str) -> RoutePlan:
    spec = raw.strip()
    if not spec:
        raise ValueError("route specification must be non-empty")
    if spec[0] in "[{":
        data = json.loads(spec)
        return as_route_plan(data)
    if "->" in spec:
        tokens = [token.strip() for token in spec.split("->") if token.strip()]
        if len(tokens) < 2:
            raise ValueError("route string must include at least two tokens")
        return make_route(tokens)
    segments = [segment.strip() for segment in spec.split(",") if segment.strip()]
    if len(segments) < 2:
        return as_route_plan(spec)
    tokens: list[str] = []
    token_fees: list[int | None] = []
    for segment in segments:
        if "@" in segment:
            addr_part, fee_part = segment.split("@", 1)
        else:
            addr_part, fee_part = segment, ""
        addr = addr_part.strip()
        if not addr:
            continue
        tokens.append(addr)
        try:
            fee_val = int(fee_part.strip()) if fee_part.strip() else None
        except Exception as exc:
            raise ValueError(f"invalid fee tier in route spec: {segment}") from exc
        token_fees.append(fee_val)
    if len(tokens) < 2:
        raise ValueError("route must include at least two addresses")
    # Fees apply to hops (between tokens), not tokens themselves
    # Fee annotation on token[i] applies to hop[i-1] (from token[i-1] to token[i])
    # For route "DIEM,VVV,USDC@3000":
    #   - hop[0] (DIEM→VVV): no fee annotation on VVV, so None
    #   - hop[1] (VVV→USDC): fee annotation on USDC, so 3000
    # So hop_fees[i] = token_fees[i+1] for i in range(len(tokens)-1)
    hop_fees: list[int | None] = []
    for i in range(len(tokens) - 1):
        if i + 1 < len(token_fees):
            hop_fees.append(token_fees[i + 1])
        else:
            hop_fees.append(None)
    return make_route(tokens, hop_fees)


@dataclass
class EnvConfig:
    quote_token: str | None
    bridge_token: str | None
    diem_token: str | None
    vvv_token: str | None
    trade_paths: list[RoutePlan]
    progressive_live: bool
    progressive_min_cycles: int | None
    diem_vvv_pair: str | None
    diem_usdc_pool: str | None
    vvv_usdc_pool: str | None
    vvv_weth_pool: str | None  # High-liquidity VVV/WETH Aerodrome pool for 3-hop routes


def load_env_config() -> EnvConfig:
    try:
        from libs.env import bootstrap_env  # type: ignore

        bootstrap_env()
    except Exception:
        pass

    trade_paths: list[RoutePlan] = []
    raw_paths = os.getenv("TRADE_PATHS")
    if raw_paths:
        try:
            parsed = json.loads(raw_paths)
        except Exception as exc:
            raise ValueError("TRADE_PATHS must be valid JSON") from exc
        if not isinstance(parsed, list):
            raise ValueError("TRADE_PATHS must be a JSON array")
        for entry in parsed:
            spec = _coerce_route_entry(entry)
            if not spec:
                continue
            try:
                trade_paths.append(_parse_route_spec(spec))
            except Exception:
                continue
    if not trade_paths:
        for key in ("TRADE_PATH", "TRADE_PATH_2"):
            raw = os.getenv(key)
            if not raw:
                continue
            try:
                trade_paths.append(_parse_route_spec(raw))
            except Exception:
                continue

    quote_token = (os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip() or None
    bridge_token = (os.getenv("BRIDGE_TOKEN_ADDRESS") or "").strip() or None
    diem_token = (os.getenv("DIEM_TOKEN_ADDRESS") or "").strip() or None
    vvv_token = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip() or None

    progressive_live = _truthy(os.getenv("STAKEMASTER_PROGRESSIVE_ENABLE")) or _truthy(
        os.getenv("PROGRESSIVE_LIVE"), False
    )
    progressive_min_cycles_raw = os.getenv("STAKEMASTER_PROGRESSIVE_CYCLES")
    if progressive_min_cycles_raw is not None and progressive_min_cycles_raw.strip():
        try:
            progressive_min_cycles = max(0, int(progressive_min_cycles_raw))
        except Exception:
            progressive_min_cycles = None
    else:
        progressive_min_cycles = None

    diem_vvv_pair = (os.getenv("DIEM_VVV_PAIR_ADDRESS") or "").strip() or None
    diem_usdc_pool = (os.getenv("DIEM_USDC_POOL_ADDRESS") or "").strip() or None
    vvv_usdc_pool = (
        (os.getenv("VVV_USDC_POOL_ADDRESS") or "").strip()
        or (os.getenv("VVV_USDC_POOL_V3_ADDRESS") or "").strip()
        or None
    )
    # High-liquidity VVV/WETH Aerodrome pool for 3-hop routes
    vvv_weth_pool = (os.getenv("VVV_WETH_POOL_ADDRESS") or "").strip() or None

    return EnvConfig(
        quote_token=quote_token,
        bridge_token=bridge_token,
        diem_token=diem_token,
        vvv_token=vvv_token,
        trade_paths=trade_paths,
        progressive_live=progressive_live,
        progressive_min_cycles=progressive_min_cycles,
        diem_vvv_pair=diem_vvv_pair,
        diem_usdc_pool=diem_usdc_pool,
        vvv_usdc_pool=vvv_usdc_pool,
        vvv_weth_pool=vvv_weth_pool,
    )


__all__ = ["EnvConfig", "load_env_config"]
