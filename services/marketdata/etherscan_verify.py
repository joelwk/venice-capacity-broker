from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
from typing import Any
from urllib.parse import urlencode

import requests

try:
    from web3 import Web3  # type: ignore
except Exception:  # pragma: no cover - optional dependency in tests
    Web3 = None  # type: ignore[assignment]

try:
    from libs.agentkit_ext.web3_utils import get_web3  # type: ignore
except Exception:  # pragma: no cover - optional dependency in tests
    get_web3 = None  # type: ignore[assignment]


ETHERSCAN_API_URL_DEFAULT = "https://api.etherscan.io/v2/api"


def _timeout_seconds() -> float:
    try:
        raw = os.getenv("ETHERSCAN_TIMEOUT_SECONDS")
        return float(raw) if raw not in (None, "") else 8.0
    except Exception:
        return 8.0


def _max_retries() -> int:
    try:
        raw = os.getenv("ETHERSCAN_MAX_RETRIES")
        val = int(raw) if raw not in (None, "") else 1
    except Exception:
        val = 1
    return max(0, min(3, val))


def _env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip()


def _pad_addr(addr: str) -> str:
    a = addr.lower().removeprefix("0x")
    return ("0" * (64 - len(a))) + a


def _abi_bool32(v: bool) -> str:
    """Return 32-byte ABI-encoded boolean (hex, without 0x)."""
    return ("0" * 63) + ("1" if v else "0")


def _abi_uint24(value: int) -> str:
    iv = int(value)
    if iv < 0 or iv >= (1 << 24):
        raise ValueError("uint24 value must be between 0 and 2**24-1")
    return f"{iv:064x}"


def _selector(sig: str) -> str:
    """Compute 4-byte function selector for a signature like 'getPair(address,address)'."""
    try:
        from web3 import Web3  # type: ignore

        selector = Web3.keccak(text=sig)[:4].hex()
        return "0x" + selector
    except Exception:
        # Fallback for known selectors
        if sig == "getPair(address,address)":
            return "0xe6a43905"
        if sig == "getPair(address,address,bool)":
            # Precomputed with keccak('getPair(address,address,bool)')[:4]
            return "0x8a6f75c0"
        if sig == "getPool(address,address,uint24)":
            return "0x1698ee82"
        if sig == "slot0()":
            return "0x3850c7bd"
        if sig == "liquidity()":
            return "0x1a686502"
        return "0x00000000"


def _etherscan_base_url() -> str:
    """Return the Etherscan v2 base URL (multi‑chain).

    Uses `ETHERSCAN_API_URL` when set; otherwise defaults to
    `https://api.etherscan.io/v2/api`. Chain selection is controlled by the
    `chainid` parameter we already pass on each request.
    """
    return (
        _env("ETHERSCAN_API_URL", ETHERSCAN_API_URL_DEFAULT)
        or ETHERSCAN_API_URL_DEFAULT
    )


def _etherscan_chain_id() -> str:
    return (
        _env("ETHERSCAN_CHAINID")
        or _env("ETHERSCAN_CHAIN_ID")
        or _env("BASE_CHAIN_ID")
        or "8453"
    )


def _etherscan_api_key() -> str | None:
    """Return the universal Etherscan v2 API key.

    Etherscan v2 supports 50+ chains via one key; we only use ETHERSCAN_API_KEY.
    """
    return _env("ETHERSCAN_API_KEY")


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _es_get_via_curl(base: str, params: dict[str, str]) -> dict[str, Any]:
    """Fallback to curl when Python ssl is unavailable."""
    if not _curl_available():
        raise RuntimeError("curl not available for Etherscan fallback")
    url = f"{base}?{urlencode(params)}"
    max_time = str(int(max(1.0, min(30.0, _timeout_seconds()))))
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "--max-time",
                max_time,
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = proc.stdout.strip()
        if not payload:
            return {}
        return json.loads(payload)
    except Exception as exc:
        raise RuntimeError(f"curl fallback failed: {exc}") from exc


def _es_get(params: dict[str, str]) -> dict[str, Any]:
    base = _etherscan_base_url()
    q = {
        "chainid": _etherscan_chain_id(),
        **params,
    }
    key = _etherscan_api_key()
    if key:
        q["apikey"] = key
    timeout = max(1.0, min(30.0, _timeout_seconds()))
    retries = _max_retries()
    for attempt in range(max(1, retries + 1)):
        try:
            r = requests.get(base, params=q, timeout=timeout)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as exc:
            if attempt < retries:
                time.sleep(0.25)
                continue
            if _curl_available():
                try:
                    data = _es_get_via_curl(base, q)
                    break
                except Exception as curl_exc:
                    raise type(exc)(
                        f"{exc} (curl fallback also failed: {curl_exc})"
                    ) from curl_exc
            else:
                raise
    return data  # type: ignore[no-any-return]


def _eth_call_fallback(to: str, data: str) -> str | None:
    if get_web3 is None or Web3 is None:
        return None
    try:
        w3 = get_web3()
        call = {
            "to": Web3.to_checksum_address(to),
            "data": data,
        }
        out = w3.eth.call(call)  # type: ignore[attr-defined]
    except Exception:
        return None
    if isinstance(out, bytes):
        return "0x" + out.hex()
    if isinstance(out, str):
        return out
    return None


def eth_call(to: str, data: str) -> str | None:
    # Prefer direct RPC if available, as it's faster and respects paid endpoints
    out = _eth_call_fallback(to, data)
    if out:
        return out

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
        result = j.get("result") if isinstance(j, dict) else None
        if isinstance(result, str) and result.startswith("0x"):
            return result
    except Exception:
        pass
    return None


def get_pair(factory_addr: str, token_a: str, token_b: str) -> str | None:
    sel = _selector("getPair(address,address)")
    data = sel + _pad_addr(token_a) + _pad_addr(token_b)
    out = eth_call(factory_addr, data)
    if not out or not isinstance(out, str) or len(out) < 42 or not out.startswith("0x"):
        return None
    # Etherscan returns 0x000... when no pair exists
    addr = out[-40:]
    if set(addr) == {"0"}:
        return None
    return "0x" + addr


def get_pair_aerodrome(
    factory_addr: str, token_a: str, token_b: str, stable: bool
) -> str | None:
    """Return Aerodrome pair address for tokenA/tokenB with stable flag.

    Aerodrome/Velodrome factory: getPair(address,address,bool)
    """
    sel = _selector("getPair(address,address,bool)")
    if sel == "0x00000000":
        return None
    data = sel + _pad_addr(token_a) + _pad_addr(token_b) + _abi_bool32(bool(stable))
    out = eth_call(factory_addr, data)
    if not out or not isinstance(out, str) or len(out) < 42 or not out.startswith("0x"):
        return None
    addr = out[-40:]
    if set(addr) == {"0"}:
        return None
    return "0x" + addr


def get_pool_uniswap_v3(
    factory_addr: str, token_a: str, token_b: str, fee: int
) -> str | None:
    """Return Uniswap v3 pool address for tokenA/tokenB/fee via factory getPool."""
    if not factory_addr:
        return None
    try:
        fee_int = int(fee)
    except Exception:
        return None
    if fee_int < 0 or fee_int >= (1 << 24):
        return None
    sel = _selector("getPool(address,address,uint24)")
    if sel == "0x00000000":
        return None
    a = token_a.strip() if token_a else ""
    b = token_b.strip() if token_b else ""
    if not a or not b:
        return None
    if a.lower().startswith("0x"):
        a_norm = a.lower()
    else:
        a_norm = "0x" + a.lower()
    if b.lower().startswith("0x"):
        b_norm = b.lower()
    else:
        b_norm = "0x" + b.lower()
    token0, token1 = (
        (a_norm, b_norm) if int(a_norm, 16) < int(b_norm, 16) else (b_norm, a_norm)
    )
    data = sel + _pad_addr(token0) + _pad_addr(token1) + _abi_uint24(fee_int)
    out = eth_call(factory_addr, data)
    if not out or not isinstance(out, str) or len(out) < 42 or not out.startswith("0x"):
        return None
    addr = out[-40:]
    if set(addr) == {"0"}:
        return None
    return "0x" + addr


def get_uniswap_v3_liquidity(pool_addr: str) -> int | None:
    sel = _selector("liquidity()")
    if sel == "0x00000000":
        return None
    out = eth_call(pool_addr, sel)
    if not out or not isinstance(out, str) or len(out) < 66 or not out.startswith("0x"):
        return None
    try:
        return int(out, 16)
    except Exception:
        return None


def get_uniswap_v3_sqrt_price_x96(pool_addr: str) -> int | None:
    sel = _selector("slot0()")
    if sel == "0x00000000":
        return None
    out = eth_call(pool_addr, sel)
    if not out or not isinstance(out, str) or len(out) < 66 or not out.startswith("0x"):
        return None
    try:
        return int(out[2:66], 16)
    except Exception:
        return None


def get_uniswap_v3_fee(pool_addr: str) -> int | None:
    sel = _selector("fee()")
    if sel == "0x00000000":
        return None
    out = eth_call(pool_addr, sel)
    if not out or not isinstance(out, str) or len(out) < 10 or not out.startswith("0x"):
        return None
    try:
        return int(out, 16)
    except Exception:
        return None


def get_reserves(pair_addr: str) -> tuple[int, int, int] | None:
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


def get_token0(pair_addr: str) -> str | None:
    """Return token0 address for a UniswapV2-like pair via proxy eth_call."""
    try:
        # token0() selector
        out = eth_call(pair_addr, "0x0dfe1681")
        if (
            not out
            or not isinstance(out, str)
            or len(out) < 66
            or not out.startswith("0x")
        ):
            return None
        return "0x" + out[-40:]
    except Exception:
        return None


def get_token1(pair_addr: str) -> str | None:
    """Return token1 address for a UniswapV2-like pair via proxy eth_call."""
    try:
        out = eth_call(pair_addr, "0xd21220a7")  # token1()
        if (
            not out
            or not isinstance(out, str)
            or len(out) < 66
            or not out.startswith("0x")
        ):
            return None
        return "0x" + out[-40:]
    except Exception:
        return None


def _factories() -> dict[str, str]:
    return {
        "uniswap_v2": _env(
            "UNISWAP_V2_FACTORY_ADDRESS", "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6"
        )
        or "",
        "uniswap_v3": _env(
            "UNISWAP_V3_FACTORY_ADDRESS", "0x33128a8fC17869897dce68Ed026d694621f6FDfD"
        )
        or "",
        "aerodrome_vol": _env(
            "AERODROME_FACTORY_VOLATILE", "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"
        )
        or "",
        "aerodrome_stable": _env(
            "AERODROME_FACTORY_STABLE", "0x5e7BB104d84c7CB9B682AaC2F3d509f5F406809A"
        )
        or "",
    }


def _name(addr: str, sym: dict[str, str]) -> str:
    a = addr.lower()
    for k, v in sym.items():
        if v.lower() == a:
            return k
    return addr


def verify_trade_path(
    path: list[str], fees: list[int | None] | None = None
) -> dict[str, Any]:
    """Verify adjacent hops in TRADE_PATH via Etherscan proxy calls.

    Returns a structured dict suitable for pretty printing.
    """
    if len(path) < 2:
        raise ValueError("TRADE_PATH must include at least two addresses")

    hop_count = len(path) - 1
    if fees is None:
        fee_list: list[int | None] = [None] * hop_count
    else:
        fee_list = []
        for item in list(fees):
            if item is None:
                fee_list.append(None)
                continue
            try:
                fee_list.append(int(item))
            except Exception:
                fee_list.append(None)
        if len(fee_list) < hop_count:
            fee_list.extend([None] * (hop_count - len(fee_list)))
        elif len(fee_list) > hop_count:
            fee_list = fee_list[:hop_count]

    # Symbol map for nicer labels when possible
    sym = {
        "DIEM": _env("DIEM_TOKEN_ADDRESS", "") or "",
        "VVV": _env("VVV_TOKEN_ADDRESS", "") or "",
        "USDC": _env("QUOTE_TOKEN_ADDRESS", "") or "",
        "WETH": _env("WETH_ADDRESS", "0x4200000000000000000000000000000000000006")
        or "0x4200000000000000000000000000000000000006",
    }
    fac = _factories()
    # Direct pool address configs (checked before factory lookup)
    diem_addr = _norm(_env("DIEM_TOKEN_ADDRESS", ""))
    vvv_addr = _norm(_env("VVV_TOKEN_ADDRESS", ""))
    quote_addr = _norm(_env("QUOTE_TOKEN_ADDRESS", ""))
    diem_vvv_pair = _norm(_env("DIEM_VVV_PAIR_ADDRESS", ""))
    vvv_usdc_pool = _norm(_env("VVV_USDC_POOL_ADDRESS", ""))
    vvv_usdc_fee = _env("VVV_USDC_POOL_FEE", "3000")
    try:
        vvv_usdc_fee_int = int(vvv_usdc_fee) if vvv_usdc_fee else None
    except Exception:
        vvv_usdc_fee_int = None

    hops: list[dict[str, Any]] = []
    for i in range(hop_count):
        a, b = path[i], path[i + 1]
        # Strip @fee annotations from token addresses if present
        if "@" in a:
            a = a.split("@", 1)[0].strip()
        if "@" in b:
            b = b.split("@", 1)[0].strip()
        fee_val = fee_list[i] if i < len(fee_list) else None
        a_norm = _norm(a)
        b_norm = _norm(b)
        rec: dict[str, Any] = {
            "from": _name(a, sym),
            "to": _name(b, sym),
            "uniswap_v2": {"pair": None, "reserves": None},
            "aerodrome_vol": {"pair": None, "reserves": None},
            "aerodrome_stable": {"pair": None, "reserves": None},
            "direct_config": {"pair": None, "reserves": None, "source": None},
        }
        if fee_val is not None:
            rec["uniswap_v3"] = {
                "pool": None,
                "fee": fee_val,
                "liquidity": None,
                "sqrt_price_x96": None,
            }

        # Check direct pool configs before factory lookup
        # DIEM/VVV pair check
        if diem_vvv_pair and {a_norm, b_norm} == {diem_addr, vvv_addr}:
            try:
                reserves = get_reserves(diem_vvv_pair)
                if reserves:
                    rec["direct_config"]["pair"] = diem_vvv_pair
                    rec["direct_config"]["reserves"] = reserves
                    rec["direct_config"]["source"] = "DIEM_VVV_PAIR_ADDRESS"
                    # Also populate aerodrome_vol as a fallback since DIEM/VVV is V2-style
                    if not rec["aerodrome_vol"]["pair"]:
                        rec["aerodrome_vol"]["pair"] = diem_vvv_pair
                        rec["aerodrome_vol"]["reserves"] = reserves
                    try:
                        t0 = get_token0(diem_vvv_pair)
                        t1 = get_token1(diem_vvv_pair)
                        if t0:
                            rec["direct_config"]["token0"] = t0
                        if t1:
                            rec["direct_config"]["token1"] = t1
                        _cache_update_tokens(a, b, diem_vvv_pair, reserves, t0, t1)
                    except Exception:
                        pass
            except Exception:
                pass

        # VVV/USDC pool check (V3)
        if vvv_usdc_pool and {a_norm, b_norm} == {vvv_addr, quote_addr}:
            # Auto-probe V3 fee tiers if not provided
            probe_fees = [fee_val] if fee_val is not None else []
            if not probe_fees and vvv_usdc_fee_int:
                probe_fees = [vvv_usdc_fee_int]
            if not probe_fees:
                # Default V3 fee tiers to probe
                probe_fees = [100, 500, 1000, 3000, 10000]

            for probe_fee in probe_fees:
                try:
                    pool = get_pool_uniswap_v3(
                        fac.get("uniswap_v3", ""), a, b, probe_fee
                    )
                    if pool and pool.lower() == vvv_usdc_pool.lower():
                        # Found the configured pool
                        if "uniswap_v3" not in rec:
                            rec["uniswap_v3"] = {
                                "pool": None,
                                "fee": probe_fee,
                                "liquidity": None,
                                "sqrt_price_x96": None,
                            }
                        rec["uniswap_v3"]["pool"] = pool
                        rec["uniswap_v3"]["fee"] = probe_fee
                        rec["uniswap_v3"]["liquidity"] = get_uniswap_v3_liquidity(pool)
                        rec["uniswap_v3"]["sqrt_price_x96"] = (
                            get_uniswap_v3_sqrt_price_x96(pool)
                        )
                        rec["direct_config"]["pair"] = pool
                        rec["direct_config"]["source"] = "VVV_USDC_POOL_ADDRESS"
                        try:
                            t0 = get_token0(pool)
                            t1 = get_token1(pool)
                            if t0:
                                rec["uniswap_v3"]["token0"] = t0
                            if t1:
                                rec["uniswap_v3"]["token1"] = t1
                            _cache_update_tokens(a, b, pool, None, t0, t1)
                        except Exception:
                            pass
                        break
                except Exception:
                    continue

        # Uniswap V2
        try:
            p = get_pair(fac.get("uniswap_v2", ""), a, b)
            if p:
                rec["uniswap_v2"]["pair"] = p
                rec["uniswap_v2"]["reserves"] = get_reserves(p)
                try:
                    t0 = get_token0(p)
                    t1 = get_token1(p)
                    if t0:
                        rec["uniswap_v2"]["token0"] = t0
                    if t1:
                        rec["uniswap_v2"]["token1"] = t1
                    try:
                        _cache_update_tokens(
                            a, b, p, rec["uniswap_v2"].get("reserves"), t0, t1
                        )
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass
        # Aerodrome volatile
        try:
            p = get_pair_aerodrome(fac.get("aerodrome_vol", ""), a, b, stable=False)
            if not p:
                p = get_pair_aerodrome(fac.get("aerodrome_vol", ""), b, a, stable=False)
            if p:
                rec["aerodrome_vol"]["pair"] = p
                rec["aerodrome_vol"]["reserves"] = get_reserves(p)
        except Exception:
            pass
        # Aerodrome stable
        try:
            p = get_pair_aerodrome(fac.get("aerodrome_stable", ""), a, b, stable=True)
            if not p:
                p = get_pair_aerodrome(
                    fac.get("aerodrome_stable", ""), b, a, stable=True
                )
            if p:
                rec["aerodrome_stable"]["pair"] = p
                rec["aerodrome_stable"]["reserves"] = get_reserves(p)
        except Exception:
            pass

        # Uniswap V3 pool discovery fallback: probe common fee tiers when no V2/Aerodrome pairs found
        has_v2_pair = (
            rec["uniswap_v2"]["pair"] is not None
            or rec["aerodrome_vol"]["pair"] is not None
            or rec["aerodrome_stable"]["pair"] is not None
        )
        if not has_v2_pair and fee_val is None:
            # Probe common V3 fee tiers to discover pools
            probe_fees = [100, 500, 1000, 3000, 10000]
            # Check VVV_USDC_POOL_FEE first if this looks like VVV->USDC
            vvv_addr = _norm(_env("VVV_TOKEN_ADDRESS", ""))
            quote_addr = _norm(_env("QUOTE_TOKEN_ADDRESS", ""))
            vvv_usdc_fee_raw = _env("VVV_USDC_POOL_FEE", "3000")
            try:
                vvv_usdc_fee = int(vvv_usdc_fee_raw) if vvv_usdc_fee_raw else None
            except Exception:
                vvv_usdc_fee = None
            if (
                vvv_addr
                and quote_addr
                and vvv_usdc_fee
                and {a_norm, b_norm} == {vvv_addr, quote_addr}
            ):
                # Prioritize the configured fee tier
                probe_fees = [vvv_usdc_fee] + [
                    f for f in probe_fees if f != vvv_usdc_fee
                ]

            for probe_fee in probe_fees:
                try:
                    pool = get_pool_uniswap_v3(
                        fac.get("uniswap_v3", ""), a, b, probe_fee
                    )
                    if pool:
                        info = rec.setdefault("uniswap_v3", {})
                        info["fee"] = probe_fee
                        info["pool"] = pool
                        info["liquidity"] = get_uniswap_v3_liquidity(pool)
                        info["sqrt_price_x96"] = get_uniswap_v3_sqrt_price_x96(pool)
                        try:
                            t0 = get_token0(pool)
                            t1 = get_token1(pool)
                            if t0:
                                info["token0"] = t0
                            if t1:
                                info["token1"] = t1
                            try:
                                _cache_update_tokens(a, b, pool, None, t0, t1)
                            except Exception:
                                pass
                        except Exception:
                            pass
                        break  # Found a pool, stop probing
                except Exception:
                    continue

        # Uniswap V3 pool lookup when fee tier provided
        if fee_val is not None:
            try:
                pool = get_pool_uniswap_v3(fac.get("uniswap_v3", ""), a, b, fee_val)
            except Exception:
                pool = None
            detected_fee = fee_val
            if not pool:
                # Try alternative fee tiers if the requested one doesn't exist
                alt_fees = _discover_extra_fee_tiers(fee_val)
                for alt in alt_fees:
                    try:
                        pool = get_pool_uniswap_v3(fac.get("uniswap_v3", ""), a, b, alt)
                    except Exception:
                        pool = None
                    if pool:
                        detected_fee = alt
                        break
            if pool:
                info = rec.setdefault("uniswap_v3", {"fee": detected_fee})
                info["fee"] = detected_fee
                if detected_fee != fee_val:
                    info["requested_fee"] = fee_val
                info["pool"] = pool
                info["liquidity"] = get_uniswap_v3_liquidity(pool)
                info["sqrt_price_x96"] = get_uniswap_v3_sqrt_price_x96(pool)
                try:
                    t0 = get_token0(pool)
                    t1 = get_token1(pool)
                    if t0:
                        info["token0"] = t0
                    if t1:
                        info["token1"] = t1
                    try:
                        _cache_update_tokens(a, b, pool, None, t0, t1)
                    except Exception:
                        pass
                except Exception:
                    pass
            else:
                # ensure structure exists for reporting when no pool is found
                rec.setdefault("uniswap_v3", {"fee": fee_val, "pool": None})
        hops.append(rec)
    return {
        "chainid": _etherscan_chain_id(),
        "path": path,
        "fees": fee_list,
        "hops": hops,
    }


# --- Lightweight liquidity cache (best-effort) ---
_LIQ_CACHE: dict[str, Any] = {
    "by_pair": {},  # pair_addr(lower) -> {"reserves": (r0,r1,ts), "token0": addr, "token1": addr, "cached_at": epoch}
    "by_tokens": {},  # (a.lower(), b.lower()) -> {"pair": addr, "reserves": (..), "cached_at": epoch}
}


def _norm(a: str | None) -> str:
    if not a:
        return ""
    s = str(a).strip().lower()
    return "0x" + s.removeprefix("0x") if s else ""


def _cache_update_pair(
    pair: str,
    reserves: tuple[int, int, int] | None = None,
    token0: str | None = None,
    token1: str | None = None,
) -> None:
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


def _cache_update_tokens(
    a: str,
    b: str,
    pair: str | None,
    reserves: tuple[int, int, int] | None,
    token0: str | None,
    token1: str | None,
) -> None:
    try:
        if not pair:
            return
        p = _norm(pair)
        _cache_update_pair(p, reserves, token0, token1)
        k = (_norm(a), _norm(b))
        _LIQ_CACHE["by_tokens"][k] = {
            "pair": p,
            "reserves": reserves,
            "cached_at": int(time.time()),
        }
    except Exception:
        return


def warm_cache_for_path(
    path: list[str], fees: list[int | None] | None = None
) -> dict[str, Any]:
    """Populate cache entries for the given path via verify_trade_path.

    Returns the same structure as verify_trade_path and updates cache.
    """
    res = verify_trade_path(path, fees)
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


def get_cached_pair_info_for_tokens(a: str, b: str) -> dict[str, Any] | None:
    """Return cached info for token pair (a->b) if available.

    Keys: pair, reserves, token0, token1
    """
    try:
        k = (_norm(a), _norm(b))
        ent = _LIQ_CACHE["by_tokens"].get(k)
        if not ent:
            return None
        out: dict[str, Any] = {"pair": ent.get("pair"), "reserves": ent.get("reserves")}
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


def get_liquidity_cache_summary() -> dict[str, Any]:
    """Return a compact snapshot of the current liquidity cache."""
    try:
        by_tokens = {
            f"{k[0]}->{k[1]}": {
                "pair": v.get("pair"),
                "has_reserves": bool(v.get("reserves")),
            }
            for k, v in _LIQ_CACHE.get("by_tokens", {}).items()
        }
        by_pair = {
            p: {"has_reserves": bool(v.get("reserves"))}
            for p, v in _LIQ_CACHE.get("by_pair", {}).items()
        }
        return {"by_tokens": by_tokens, "by_pair": by_pair}
    except Exception:
        return {"by_tokens": {}, "by_pair": {}}


def format_report(result: dict[str, Any]) -> str:
    lines: list[str] = []
    cid = result.get("chainid")
    path_tokens = result.get("path") or []
    # Strip @fee annotations from token addresses for display
    clean_tokens = []
    for tok in path_tokens:
        if "@" in str(tok):
            clean_tok = str(tok).split("@", 1)[0].strip()
            clean_tokens.append(clean_tok)
        else:
            clean_tokens.append(str(tok))
    lines.append(f"DEX verify (chain {cid})")
    lines.append("Path: " + " -> ".join(clean_tokens))
    fees = result.get("fees")
    if isinstance(fees, list) and fees:
        fee_render = ["-" if f in (None, "") else str(f) for f in fees]
        lines.append("Fees: " + ", ".join(fee_render))
    lines.append("")
    for idx, hop in enumerate(result.get("hops", []), start=1):
        lines.append(f"Hop {idx}: {hop.get('from')} -> {hop.get('to')}")
        v3_info = hop.get("uniswap_v3") if isinstance(hop, dict) else None
        if isinstance(v3_info, dict):
            requested = v3_info.get("requested_fee")
            fee = v3_info.get("fee")
            label = "UniswapV3"
            if fee is not None:
                label = f"{label}(fee={fee}"
                if requested is not None and requested != fee:
                    label = f"{label}, requested={requested}"
                label = f"{label})"
            pool = v3_info.get("pool")
            if pool:
                extras: list[str] = []
                liq = v3_info.get("liquidity")
                if isinstance(liq, int):
                    extras.append(f"liq={liq}")
                sqrt_px = v3_info.get("sqrt_price_x96")
                if isinstance(sqrt_px, int):
                    extras.append(f"sqrtPxX96={sqrt_px}")
                t0 = v3_info.get("token0")
                t1 = v3_info.get("token1")
                if t0 and t1:
                    extras.append(f"tokens={t0}/{t1}")
                suffix = f" {', '.join(extras)}" if extras else ""
                lines.append(f" - {label}: pool={pool}{suffix}")
            else:
                lines.append(f" - {label}: (no pool)")
        # Direct config pools (checked before factory lookup)
        direct_config = hop.get("direct_config") if isinstance(hop, dict) else None
        if isinstance(direct_config, dict) and direct_config.get("pair"):
            source = direct_config.get("source", "direct_config")
            pair = direct_config.get("pair")
            reserves = direct_config.get("reserves")
            if isinstance(reserves, tuple):
                lines.append(
                    f" - Direct Config ({source}): pair={pair} reserves={reserves[0]},{reserves[1]} ts={reserves[2]}"
                )
            else:
                lines.append(f" - Direct Config ({source}): pair={pair} reserves=(n/a)")

        for key, pretty in (
            ("uniswap_v2", "UniswapV2"),
            ("aerodrome_vol", "Aerodrome Volatile"),
            ("aerodrome_stable", "Aerodrome Stable"),
        ):
            ent = hop.get(key) if isinstance(hop, dict) else None
            pair = ent.get("pair") if isinstance(ent, dict) else None
            reserves = ent.get("reserves") if isinstance(ent, dict) else None
            if pair:
                if isinstance(reserves, tuple):
                    lines.append(
                        f" - {pretty}: pair={pair} reserves={reserves[0]},{reserves[1]} ts={reserves[2]}"
                    )
                else:
                    lines.append(f" - {pretty}: pair={pair} reserves=(n/a)")
            elif isinstance(v3_info, dict) and v3_info.get("fee") is not None:
                lines.append(f" - {pretty}: (no pair; v3 route)")
            else:
                lines.append(f" - {pretty}: (no pair)")
        lines.append("")
    return textwrap.dedent("\n".join(lines)).strip() + "\n"


if __name__ == "__main__":
    # Lightweight CLI: verify a path and print a compact report.
    # Usage examples:
    #   python services/marketdata/etherscan_verify.py \
    #       --path 0xf4d97f2da56e8c3098f3a8d538db630a2606a024,0x4200000000000000000000000000000000000006,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
    #   ETHERSCAN_API_KEY=... BASE_CHAIN_ID=8453 python services/marketdata/etherscan_verify.py
    import argparse as _arg
    import os as _os

    ap = _arg.ArgumentParser(description="Verify DEX trade path via Etherscan v2 proxy")
    ap.add_argument(
        "--path",
        default=_os.getenv("TRADE_PATH", ""),
        help="Comma-separated token addresses (e.g., DIEM,WETH,USDC)",
    )
    args = ap.parse_args()
    p = [a.strip() for a in (args.path or "").split(",") if a.strip()]
    if len(p) < 2:
        print("Provide --path or set TRADE_PATH with at least 2 addresses.")
        raise SystemExit(2)
    res = verify_trade_path(p)
    print(format_report(res))
    # Print cache summary (best-effort)
    try:
        summ = get_liquidity_cache_summary()
        print("Cache by_tokens:")
        for k, v in (summ.get("by_tokens") or {}).items():
            print(f" - {k}: pair={v.get('pair')} has_reserves={v.get('has_reserves')}")
    except Exception:
        pass


def _discover_extra_fee_tiers(current_fee: int) -> list[int]:
    """Return additional Uniswap V3 fee tiers to probe when the requested one is missing."""

    defaults = [100, 500, 1000, 3000, 10000]
    env_raw = _env("UNISWAP_V3_FEE_TIERS")
    tiers: list[int] = []
    if env_raw:
        for part in env_raw.split(","):
            try:
                tiers.append(int(part.strip()))
            except Exception:
                continue
    else:
        tiers = defaults
    seen: set[int] = {int(current_fee)}
    ordered: list[int] = []
    for tier in tiers:
        if tier in seen:
            continue
        seen.add(tier)
        ordered.append(tier)
    return ordered
