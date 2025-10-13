from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from typing import Dict, List, Optional, Sequence, Tuple

import requests

from libs.dex.routes import make_route
from libs.telemetry.logger import get_logger
from services.marketdata.etherscan_verify import get_uniswap_v3_fee

logger = get_logger("marketdata.dynamic_paths")

_DEXSCREENER_BASE_URL = os.getenv("DEXSCREENER_BASE_URL", "https://api.dexscreener.com/latest/dex/tokens")
_DEXSCREENER_TIMEOUT = float(os.getenv("TRADE_PATH_DISCOVERY_TIMEOUT", "10") or 10)
_MIN_DIRECT_LIQ_USD = float(os.getenv("TRADE_PATH_DIRECT_MIN_LIQ_USD", "5000") or 5000)
_MIN_HOP_LIQ_USD = float(os.getenv("TRADE_PATH_HOP_MIN_LIQ_USD", "1500") or 1500)
_MAX_ROUTE_COUNT = int(os.getenv("TRADE_PATH_DYNAMIC_MAX_ROUTES", "4") or 4)

_PAIR_CACHE: Dict[str, Tuple[float, List[Dict[str, object]]]] = {}


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _fetch_pairs_with_curl(url: str) -> List[Dict[str, object]]:
    """Fetch Dexscreener pairs via curl for environments without Python SSL."""
    if not _curl_available():
        return []
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sS",
                "--max-time",
                str(int(_DEXSCREENER_TIMEOUT) if _DEXSCREENER_TIMEOUT >= 1 else 1),
                url,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = proc.stdout.strip()
        if not payload:
            return []
        data = json.loads(payload) or {}
        pairs = data.get("pairs") or []
        return pairs if isinstance(pairs, list) else []
    except Exception as exc:  # noqa: BLE001
        logger.warning("curl fallback failed for %s: %s", url, exc)
        return []


class PairCandidate:
    __slots__ = ("tokens", "dex", "fee", "liquidity", "pool")

    def __init__(
        self,
        tokens: Sequence[str],
        dex: str,
        *,
        fee: Optional[int],
        liquidity: float,
        pool: Optional[str],
    ) -> None:
        self.tokens = tuple(tokens)
        self.dex = dex
        self.fee = fee
        self.liquidity = float(liquidity)
        self.pool = pool


def _fetch_pairs(token_address: str) -> List[Dict[str, object]]:
    key = token_address.lower()
    cached = _PAIR_CACHE.get(key)
    if cached:
        cached_ts, cached_pairs = cached
        ttl = float(os.getenv("TRADE_PATH_DISCOVERY_CACHE_TTL", "300") or 300)
        if (time.time() - cached_ts) < ttl:
            return cached_pairs

    url = f"{_DEXSCREENER_BASE_URL.rstrip('/')}/{token_address}"
    filtered: List[Dict[str, object]] = []
    request_exc: Optional[Exception] = None
    try:
        resp = requests.get(url, timeout=_DEXSCREENER_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        request_exc = exc
    else:
        try:
            data = resp.json() or {}
            pairs = data.get("pairs") or []
            filtered = [
                pair for pair in pairs if isinstance(pair, dict) and str(pair.get("chainId", "")).lower() == "base"
            ]
        except Exception as exc:  # noqa: BLE001
            request_exc = exc
            filtered = []
    if not filtered and request_exc is not None:
        logger.warning("dexscreener fetch failed for %s via requests: %s", token_address, request_exc)
        fallback_pairs: List[Dict[str, object]] = []
        if _curl_available():
            fallback_pairs = _fetch_pairs_with_curl(url)
        else:
            logger.warning("curl not available; skipping dexscreener fallback for %s", token_address)
        filtered = [
            pair for pair in fallback_pairs if isinstance(pair, dict) and str(pair.get("chainId", "")).lower() == "base"
        ]

    _PAIR_CACHE[key] = (time.time(), filtered)
    return filtered


def _classify_pair(pair: Dict[str, object]) -> Optional[str]:
    dex_id = str(pair.get("dexId") or "").lower()
    labels = [str(label).lower() for label in pair.get("labels") or []]
    if "uniswap" in dex_id:
        if "v3" in labels:
            return "uniswap_v3"
        if "v2" in labels:
            return "uniswap_v2"
        if "v4" in labels:
            return "uniswap_v4"
        # Default Uniswap on Base is v3
        return "uniswap_v3"
    if "aerodrome" in dex_id:
        return "aerodrome"
    return None


def _best_pair(
    pairs: Sequence[Dict[str, object]],
    start_addr: str,
    end_addr: str,
    *,
    min_liquidity: float,
    allowed_dex: Optional[Sequence[str]] = None,
) -> Optional[PairCandidate]:
    if not pairs:
        return None
    start_l = start_addr.lower()
    end_l = end_addr.lower()
    allowed = {dex.lower() for dex in (allowed_dex or [])}
    best: Optional[PairCandidate] = None
    for pair in pairs:
        base = str(pair.get("baseToken", {}).get("address") or "").lower()
        quote = str(pair.get("quoteToken", {}).get("address") or "").lower()
        if {base, quote} != {start_l, end_l}:
            continue
        dex = _classify_pair(pair)
        if dex is None:
            continue
        if allowed and dex not in allowed:
            continue
        try:
            liquidity = float(pair.get("liquidity", {}).get("usd") or 0.0)
        except Exception:
            liquidity = 0.0
        if liquidity < min_liquidity:
            continue
        fee: Optional[int] = None
        pool = str(pair.get("pairAddress") or "") or None
        if dex == "uniswap_v3":
            if not pool:
                continue
            fee = get_uniswap_v3_fee(pool)
            if fee is None:
                continue
        candidate = PairCandidate(tokens=(start_l, end_l), dex=dex, fee=fee, liquidity=liquidity, pool=pool)
        if best is None or candidate.liquidity > best.liquidity:
            best = candidate
    return best


def _auto_bridge_tokens(default_tokens: Sequence[str]) -> List[str]:
    raw = os.getenv("TRADE_PATH_BRIDGE_ADDRESSES")
    if raw:
        items = [item.strip() for item in raw.split(",") if item.strip()]
        return items or list(default_tokens)
    return list(default_tokens)


def _unique_routes(routes: Sequence[PairCandidate], *, fees: Optional[Sequence[Optional[int]]] = None) -> Dict[str, object]:
    tokens = [token.lower() for token in routes[0].tokens]
    if len(routes) > 1:
        for candidate in routes[1:]:
            tokens.append(candidate.tokens[1].lower())
    route: Dict[str, object] = {"tokens": tokens}
    if fees is not None:
        route["fees"] = list(fees)
    return route


def _discover_v3_routes(
    diem_addr: str,
    quote_addr: str,
    bridge_tokens: Sequence[str],
    *,
    pairs_cache: Dict[str, List[Dict[str, object]]],
) -> List[Dict[str, object]]:
    routes: List[Dict[str, object]] = []
    diem_pairs = pairs_cache[diem_addr.lower()]
    direct = _best_pair(
        diem_pairs,
        diem_addr,
        quote_addr,
        min_liquidity=_MIN_DIRECT_LIQ_USD,
        allowed_dex=("uniswap_v3",),
    )
    if direct:
        routes.append({"tokens": list(direct.tokens), "fees": [direct.fee] if direct.fee is not None else None})

    for bridge in bridge_tokens:
        bridge_l = bridge.lower()
        if bridge_l in {diem_addr.lower(), quote_addr.lower()}:
            continue
        bridge_pair = _best_pair(
            diem_pairs,
            diem_addr,
            bridge,
            min_liquidity=_MIN_HOP_LIQ_USD,
            allowed_dex=("uniswap_v3",),
        )
        if not bridge_pair:
            continue
        bridge_pairs = pairs_cache.setdefault(bridge_l, _fetch_pairs(bridge))
        second = _best_pair(
            bridge_pairs,
            bridge,
            quote_addr,
            min_liquidity=_MIN_HOP_LIQ_USD,
            allowed_dex=("uniswap_v3",),
        )
        if not second:
            continue
        fees = []
        for candidate in (bridge_pair, second):
            if candidate.fee is None:
                break
            fees.append(candidate.fee)
        else:
            route = {"tokens": [diem_addr.lower(), bridge_l, quote_addr.lower()], "fees": fees}
            routes.append(route)
    return routes


def _discover_v2_routes(
    diem_addr: str,
    quote_addr: str,
    bridge_tokens: Sequence[str],
    *,
    pairs_cache: Dict[str, List[Dict[str, object]]],
) -> List[Dict[str, object]]:
    """Discover Aerodrome/UniswapV2 style routes (no fee tiers)."""
    routes: List[Dict[str, object]] = []
    allowed = ("uniswap_v2", "aerodrome")
    diem_pairs = pairs_cache[diem_addr.lower()]
    direct = _best_pair(
        diem_pairs,
        diem_addr,
        quote_addr,
        min_liquidity=_MIN_DIRECT_LIQ_USD,
        allowed_dex=allowed,
    )
    if direct:
        routes.append({"tokens": list(direct.tokens)})

    for bridge in bridge_tokens:
        bridge_l = (bridge or "").lower()
        if not bridge_l or bridge_l in {diem_addr.lower(), quote_addr.lower()}:
            continue
        first = _best_pair(
            diem_pairs,
            diem_addr,
            bridge,
            min_liquidity=_MIN_HOP_LIQ_USD,
            allowed_dex=allowed,
        )
        if not first:
            continue
        bridge_pairs = pairs_cache.setdefault(bridge_l, _fetch_pairs(bridge))
        second = _best_pair(
            bridge_pairs,
            bridge,
            quote_addr,
            min_liquidity=_MIN_HOP_LIQ_USD,
            allowed_dex=allowed,
        )
        if not second:
            continue
        tokens = [diem_addr.lower(), bridge_l, quote_addr.lower()]
        routes.append({"tokens": tokens})
    return routes


def discover_trade_paths(logger_instance=None) -> List[Dict[str, object]]:
    log = logger_instance or logger
    diem = os.getenv("DIEM_TOKEN_ADDRESS")
    quote = os.getenv("QUOTE_TOKEN_ADDRESS")
    if not diem or not quote:
        log.warning("dynamic trade path discovery skipped: DIEM_TOKEN_ADDRESS or QUOTE_TOKEN_ADDRESS missing")
        return []
    diem = diem.strip()
    quote = quote.strip()
    weth = (os.getenv("WETH_ADDRESS") or "").strip()
    bridge_defaults: List[str] = []
    if weth:
        bridge_defaults.append(weth)
    vvv = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip()
    if vvv:
        bridge_defaults.append(vvv)

    pairs_cache: Dict[str, List[Dict[str, object]]] = {}
    pairs_cache[diem.lower()] = _fetch_pairs(diem)
    bridge_tokens = _auto_bridge_tokens(bridge_defaults)
    for token in bridge_tokens:
        if token:
            pairs_cache.setdefault(token.lower(), _fetch_pairs(token))
    # Ensure quote token pairs are available when bridge token equals quote (e.g., WETH -> USDC)
    pairs_cache.setdefault(quote.lower(), _fetch_pairs(quote))

    routes: List[Dict[str, object]] = []
    routes.extend(
        _discover_v3_routes(
            diem,
            quote,
            bridge_tokens,
            pairs_cache=pairs_cache,
        )
    )
    routes.extend(
        _discover_v2_routes(
            diem,
            quote,
            bridge_tokens,
            pairs_cache=pairs_cache,
        )
    )
    uniq: Dict[Tuple[Tuple[str, ...], Tuple[Optional[int], ...]], Dict[str, object]] = {}
    ordered: List[Dict[str, object]] = []
    for spec in routes:
        tokens = tuple(str(addr).lower() for addr in spec.get("tokens") or [])
        fees = tuple(spec.get("fees") or [])
        key = (tokens, fees)
        if key in uniq:
            continue
        uniq[key] = spec
        ordered.append(spec)
        if len(ordered) >= _MAX_ROUTE_COUNT:
            break
    if not ordered:
        log.warning("dynamic trade path discovery produced no usable routes")
    else:
        for spec in ordered:
            log.info("dynamic trade path: tokens=%s fees=%s", spec.get("tokens"), spec.get("fees"))
    return ordered


def discover_trade_route_plans(logger_instance=None) -> List["RoutePlan"]:
    specs = discover_trade_paths(logger_instance=logger_instance)
    plans: List["RoutePlan"] = []
    for spec in specs:
        tokens = spec.get("tokens")
        if not tokens or len(tokens) < 2:
            continue
        fees = spec.get("fees")
        try:
            plan = make_route(tokens, fees)
        except Exception:
            logger.warning("failed to build dynamic trade route tokens=%s fees=%s", tokens, fees, exc_info=True)
            continue
        plans.append(plan)
    return plans
