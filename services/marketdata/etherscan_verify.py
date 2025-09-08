from __future__ import annotations

import os
import textwrap
from typing import Any, Dict, List, Optional, Tuple

import requests
import time


ETHERSCAN_API_URL_DEFAULT = "https://api.etherscan.io/v2/api"


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _pad_addr(addr: str) -> str:
    a = addr.lower().removeprefix("0x")
    return ("0" * (64 - len(a))) + a


def _etherscan_base_url() -> str:
    return _env("ETHERSCAN_API_URL", ETHERSCAN_API_URL_DEFAULT) or ETHERSCAN_API_URL_DEFAULT


def _etherscan_chain_id() -> str:
    return (
        _env("ETHERSCAN_CHAINID")
        or _env("ETHERSCAN_CHAIN_ID")
        or _env("BASE_CHAIN_ID")
        or "8453"
    )


def _etherscan_api_key() -> Optional[str]:
    return _env("ETHERSCAN_API_KEY")


def _es_get(params: Dict[str, str]) -> Dict[str, Any]:
    base = _etherscan_base_url()
    q = {
        "chainid": _etherscan_chain_id(),
        **params,
    }
    key = _etherscan_api_key()
    if key:
        q["apikey"] = key
    r = requests.get(base, params=q, timeout=10)
    r.raise_for_status()
    return r.json()  # type: ignore[no-any-return]


def eth_call(to: str, data: str) -> Optional[str]:
    try:
        j = _es_get(
            {
                "module": "proxy",
                "action": "eth_call",
                "to": to,
                "data": data,
                "tag": "latest",
            }
        )
        return str(j.get("result")) if j and j.get("result") else None
    except Exception:
        return None


def get_pair(factory_addr: str, token_a: str, token_b: str) -> Optional[str]:
    sel = "0xe6a43905"  # getPair(address,address)
    data = sel + _pad_addr(token_a) + _pad_addr(token_b)
    out = eth_call(factory_addr, data)
    if not out or not isinstance(out, str) or len(out) < 42:
        return None
    # Etherscan returns 0x000... when no pair exists
    addr = out[-40:]
    if set(addr) == {"0"}:
        return None
    return "0x" + addr


def get_reserves(pair_addr: str) -> Optional[Tuple[int, int, int]]:
    # getReserves() => (reserve0, reserve1, blockTimestampLast)
    out = eth_call(pair_addr, "0x0902f1ac")
    if not out or not isinstance(out, str) or not out.startswith("0x"):
        return None
    # Expect 3 * 32 bytes = 96 bytes (plus 0x)
    try:
        s = out[2:].rjust(192, "0")
        r0 = int(s[0:64], 16)
        r1 = int(s[64:128], 16)
        ts = int(s[128:192], 16)
        return (r0, r1, ts)
    except Exception:
        return None


def get_token0(pair_addr: str) -> Optional[str]:
    """Return token0 address for a UniswapV2-like pair via proxy eth_call."""
    try:
        # token0() selector
        out = eth_call(pair_addr, "0x0dfe1681")
        if not out or not isinstance(out, str) or len(out) < 66:
            return None
        return "0x" + out[-40:]
    except Exception:
        return None


def get_token1(pair_addr: str) -> Optional[str]:
    """Return token1 address for a UniswapV2-like pair via proxy eth_call."""
    try:
        out = eth_call(pair_addr, "0xd21220a7")  # token1()
        if not out or not isinstance(out, str) or len(out) < 66:
            return None
        return "0x" + out[-40:]
    except Exception:
        return None


def _factories() -> Dict[str, str]:
    return {
        "uniswap_v2": _env("UNISWAP_V2_FACTORY_ADDRESS", "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6") or "",
        "aerodrome_vol": _env("AERODROME_FACTORY_VOLATILE", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da") or "",
        "aerodrome_stable": _env("AERODROME_FACTORY_STABLE", "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A") or "",
    }


def _name(addr: str, sym: Dict[str, str]) -> str:
    a = addr.lower()
    for k, v in sym.items():
        if v.lower() == a:
            return k
    return addr


def verify_trade_path(path: List[str]) -> Dict[str, Any]:
    """Verify adjacent hops in TRADE_PATH via Etherscan proxy calls.

    Returns a structured dict suitable for pretty printing.
    """
    if len(path) < 2:
        raise ValueError("TRADE_PATH must include at least two addresses")
    # Symbol map for nicer labels when possible
    sym = {
        "DIEM": _env("DIEM_TOKEN_ADDRESS", "") or "",
        "VVV": _env("VVV_TOKEN_ADDRESS", "") or "",
        "USDC": _env("QUOTE_TOKEN_ADDRESS", "") or "",
        "WETH": _env("WETH_ADDRESS", "0x4200000000000000000000000000000000000006") or "0x4200000000000000000000000000000000000006",
    }
    fac = _factories()
    hops: List[Dict[str, Any]] = []
    for i in range(len(path) - 1):
        a, b = path[i], path[i + 1]
        rec: Dict[str, Any] = {
            "from": _name(a, sym),
            "to": _name(b, sym),
            "uniswap_v2": {"pair": None, "reserves": None},
            "aerodrome_vol": {"pair": None, "reserves": None},
            "aerodrome_stable": {"pair": None, "reserves": None},
        }
        # Uniswap V2
        try:
            p = get_pair(fac["uniswap_v2"], a, b)
            if p:
                rec["uniswap_v2"]["pair"] = p
                rec["uniswap_v2"]["reserves"] = get_reserves(p)
                # Include token addresses when available
                try:
                    t0 = get_token0(p)
                    t1 = get_token1(p)
                    if t0:
                        rec["uniswap_v2"]["token0"] = t0
                    if t1:
                        rec["uniswap_v2"]["token1"] = t1
                    # Update local liquidity cache (best-effort)
                    try:
                        _cache_update_tokens(a, b, p, rec["uniswap_v2"].get("reserves"), t0, t1)
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass
        # Aerodrome volatile
        try:
            p = get_pair(fac["aerodrome_vol"], a, b)
            if p:
                rec["aerodrome_vol"]["pair"] = p
                rec["aerodrome_vol"]["reserves"] = get_reserves(p)
        except Exception:
            pass
        # Aerodrome stable
        try:
            p = get_pair(fac["aerodrome_stable"], a, b)
            if p:
                rec["aerodrome_stable"]["pair"] = p
                rec["aerodrome_stable"]["reserves"] = get_reserves(p)
        except Exception:
            pass
        hops.append(rec)
    return {
        "chainid": _etherscan_chain_id(),
        "path": path,
        "hops": hops,
    }

# --- Lightweight liquidity cache (best-effort) ---
_LIQ_CACHE: Dict[str, Any] = {
    "by_pair": {},        # pair_addr(lower) -> {"reserves": (r0,r1,ts), "token0": addr, "token1": addr, "cached_at": epoch}
    "by_tokens": {},      # (a.lower(), b.lower()) -> {"pair": addr, "reserves": (..), "cached_at": epoch}
}

def _norm(a: Optional[str]) -> str:
    if not a:
        return ""
    s = str(a).strip().lower()
    return "0x" + s.removeprefix("0x") if s else ""

def _cache_update_pair(pair: str, reserves: Optional[tuple[int,int,int]] = None, token0: Optional[str] = None, token1: Optional[str] = None) -> None:
    try:
        p = _norm(pair)
        if not p:
            return
        ent = _LIQ_CACHE["by_pair"].setdefault(p, {})
        if isinstance(reserves, tuple) and len(reserves) >= 2:
            ent["reserves"] = reserves
        if token0:
            ent["token0"] = _norm(token0)
        if token1:
            ent["token1"] = _norm(token1)
        ent["cached_at"] = int(time.time())
    except Exception:
        return

def _cache_update_tokens(a: str, b: str, pair: Optional[str], reserves: Optional[tuple[int,int,int]], token0: Optional[str], token1: Optional[str]) -> None:
    try:
        if not pair:
            return
        p = _norm(pair)
        _cache_update_pair(p, reserves, token0, token1)
        k = (_norm(a), _norm(b))
        _LIQ_CACHE["by_tokens"][k] = {"pair": p, "reserves": reserves, "cached_at": int(time.time())}
    except Exception:
        return

def warm_cache_for_path(path: List[str]) -> Dict[str, Any]:
    """Populate cache entries for the given path via verify_trade_path.

    Returns the same structure as verify_trade_path and updates cache.
    """
    res = verify_trade_path(path)
    # verify_trade_path already updates cache for UniswapV2 entries; ensure Aerodrome too
    try:
        for hop in res.get("hops", []) or []:
            for key in ("aerodrome_vol", "aerodrome_stable"):
                ent = hop.get(key) or {}
                p = ent.get("pair")
                if p and ent.get("reserves"):
                    _cache_update_pair(p, ent.get("reserves"))
    except Exception:
        pass
    return res

def get_cached_pair_info_for_tokens(a: str, b: str) -> Optional[Dict[str, Any]]:
    """Return cached info for token pair (a->b) if available.

    Keys: pair, reserves, token0, token1
    """
    try:
        k = (_norm(a), _norm(b))
        ent = _LIQ_CACHE["by_tokens"].get(k)
        if not ent:
            return None
        out: Dict[str, Any] = {"pair": ent.get("pair"), "reserves": ent.get("reserves")}
        # Enrich from by_pair
        try:
            p = _norm(ent.get("pair"))
            bp = _LIQ_CACHE["by_pair"].get(p) or {}
            if bp:
                if bp.get("token0"):
                    out["token0"] = bp.get("token0")
                if bp.get("token1"):
                    out["token1"] = bp.get("token1")
        except Exception:
            pass
        return out
    except Exception:
        return None

def get_liquidity_cache_summary() -> Dict[str, Any]:
    """Return a compact snapshot of the current liquidity cache."""
    try:
        by_tokens = {
            f"{k[0]}->{k[1]}": {"pair": v.get("pair"), "has_reserves": bool(v.get("reserves"))}
            for k, v in _LIQ_CACHE.get("by_tokens", {}).items()
        }
        by_pair = {p: {"has_reserves": bool(v.get("reserves"))} for p, v in _LIQ_CACHE.get("by_pair", {}).items()}
        return {"by_tokens": by_tokens, "by_pair": by_pair}
    except Exception:
        return {"by_tokens": {}, "by_pair": {}}


def format_report(result: Dict[str, Any]) -> str:
    lines: List[str] = []
    cid = result.get("chainid")
    path = result.get("path") or []
    lines.append(f"DEX verify (chain {cid})")
    lines.append("Path: " + " -> ".join(path))
    lines.append("")
    for idx, hop in enumerate(result.get("hops", []), start=1):
        lines.append(f"Hop {idx}: {hop.get('from')} -> {hop.get('to')}")
        for key, label in (
            ("uniswap_v2", "UniswapV2"),
            ("aerodrome_vol", "Aerodrome Volatile"),
            ("aerodrome_stable", "Aerodrome Stable"),
        ):
            ent = hop.get(key) or {}
            p = ent.get("pair")
            rez = ent.get("reserves")
            if p:
                if isinstance(rez, tuple):
                    lines.append(f" - {label}: pair={p} reserves={rez[0]},{rez[1]} ts={rez[2]}")
                else:
                    lines.append(f" - {label}: pair={p} reserves=(n/a)")
            else:
                lines.append(f" - {label}: (no pair)")
        lines.append("")
    return textwrap.dedent("\n".join(lines)).strip() + "\n"
