from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from collections.abc import Sequence

import requests

from libs.dex.routes import RoutePlan, make_route
from libs.telemetry.logger import get_logger
from services.marketdata.etherscan_verify import get_uniswap_v3_fee
from services.marketdata.token_classifier import characteristics_as_dict, classify_token

logger = get_logger("marketdata.dynamic_paths")

_DEXSCREENER_BASE_URL = os.getenv(
    "DEXSCREENER_BASE_URL", "https://api.dexscreener.com/latest/dex/tokens"
)
_DEXSCREENER_TIMEOUT = float(os.getenv("TRADE_PATH_DISCOVERY_TIMEOUT", "10") or 10)
_MIN_DIRECT_LIQ_USD = float(os.getenv("TRADE_PATH_DIRECT_MIN_LIQ_USD", "5000") or 5000)
_MIN_HOP_LIQ_USD = float(os.getenv("TRADE_PATH_HOP_MIN_LIQ_USD", "1500") or 1500)
_MAX_ROUTE_COUNT = int(os.getenv("TRADE_PATH_DYNAMIC_MAX_ROUTES", "4") or 4)

_PAIR_CACHE: dict[str, tuple[float, list[dict[str, object]]]] = {}


def _extract_price_usd(pair: dict[str, object], token_address: str) -> float:
    """Best-effort extraction of token price in USD from a Dexscreener pair."""
    try:
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        base_addr = str(base.get("address") or "").lower()
        quote_addr = str(quote.get("address") or "").lower()
        token_l = token_address.lower()

        # Dexscreener occasionally includes priceUsd at multiple levels
        pair_price = float(pair.get("priceUsd") or 0.0)
        base_price = float(base.get("priceUsd") or 0.0)
        quote_price = float(quote.get("priceUsd") or 0.0)

        if token_l == base_addr:
            if base_price > 0:
                return base_price
            if pair_price > 0:
                return pair_price
            if quote_price > 0 and pair_price > 0:
                # If quote token price is known and pair price is base/quote, multiply
                return pair_price * quote_price
        if token_l == quote_addr:
            if quote_price > 0:
                return quote_price
            if pair_price > 0:
                # Interpret pair_price as base price; invert when token is quote
                try:
                    return 1.0 / pair_price if pair_price > 0 else 0.0
                except ZeroDivisionError:
                    return 0.0
        return pair_price
    except Exception:
        return 0.0


def _max_liquidity_usd(
    pairs: Sequence[dict[str, object]],
    start_addr: str,
    end_addr: str,
) -> float:
    """Return maximum observed liquidity for given token pair."""
    max_liq = 0.0
    start_l = start_addr.lower()
    end_l = end_addr.lower()
    for pair in pairs:
        base = str(pair.get("baseToken", {}).get("address") or "").lower()
        quote = str(pair.get("quoteToken", {}).get("address") or "").lower()
        if {base, quote} != {start_l, end_l}:
            continue
        try:
            liquidity = float(pair.get("liquidity", {}).get("usd") or 0.0)
        except Exception:
            liquidity = 0.0
        max_liq = max(max_liq, liquidity)
    return max_liq


def _curl_available() -> bool:
    return shutil.which("curl") is not None


def _fetch_pairs_with_curl(url: str) -> list[dict[str, object]]:
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
    except Exception as exc:
        logger.warning("curl fallback failed for %s: %s", url, exc)
        return []


class PairCandidate:
    __slots__ = ("dex", "fee", "liquidity", "pool", "tokens")

    def __init__(
        self,
        tokens: Sequence[str],
        dex: str,
        *,
        fee: int | None,
        liquidity: float,
        pool: str | None,
    ) -> None:
        self.tokens = tuple(tokens)
        self.dex = dex
        self.fee = fee
        self.liquidity = float(liquidity)
        self.pool = pool


def _liquidity_threshold(is_direct: bool) -> float:
    return _MIN_DIRECT_LIQ_USD if is_direct else _MIN_HOP_LIQ_USD


def _build_route_metadata(
    *, hops: Sequence[PairCandidate], route_type: str, source: str
) -> dict[str, object]:
    liquidity_thresholds: list[float] = []
    hop_payloads: list[dict[str, object]] = []
    venues: list[str] = []
    discovery_only = False
    for idx, candidate in enumerate(hops):
        token_in, token_out = candidate.tokens
        threshold = _liquidity_threshold(is_direct=len(hops) == 1 and idx == 0)
        liquidity_thresholds.append(threshold)
        dex_name = candidate.dex or "unknown"
        venue = dex_name.lower()
        venues.append(venue)
        if venue not in {"uniswap_v2", "uniswap_v3"}:
            discovery_only = True
        hop_payloads.append(
            {
                "token_in": token_in,
                "token_out": token_out,
                "dex": dex_name,
                "pool": candidate.pool,
                "fee": candidate.fee,
                "liquidityUsd": candidate.liquidity,
                "minLiquidityRequiredUsd": threshold,
            }
        )
    ratios: list[float] = []
    for candidate, threshold in zip(hops, liquidity_thresholds):
        if threshold <= 0:
            ratios.append(0.0)
        else:
            ratios.append(candidate.liquidity / threshold)
    if not ratios:
        health = "unknown"
    else:
        min_ratio = min(ratios)
        if min_ratio <= 0:
            health = "unusable"
        elif min_ratio < 1.0:
            health = "degraded"
        elif min_ratio < 1.2:
            health = "stable"
        else:
            health = "healthy"
    metadata: dict[str, object] = {
        "source": source,
        "type": route_type,
        "health": health,
        "hops": hop_payloads,
        "venues": venues,
        "discoveryOnly": discovery_only,
        "minRatio": min(ratios) if ratios else None,
        "updatedAt": time.time(),
    }
    return metadata


def _fetch_pairs(token_address: str) -> list[dict[str, object]]:
    key = token_address.lower()
    cached = _PAIR_CACHE.get(key)
    if cached:
        cached_ts, cached_pairs = cached
        ttl = float(os.getenv("TRADE_PATH_DISCOVERY_CACHE_TTL", "300") or 300)
        if (time.time() - cached_ts) < ttl:
            return cached_pairs

    url = f"{_DEXSCREENER_BASE_URL.rstrip('/')}/{token_address}"
    filtered: list[dict[str, object]] = []
    request_exc: Exception | None = None
    try:
        resp = requests.get(url, timeout=_DEXSCREENER_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        request_exc = exc
    else:
        try:
            data = resp.json() or {}
            pairs = data.get("pairs") or []
            filtered = [
                pair
                for pair in pairs
                if isinstance(pair, dict)
                and str(pair.get("chainId", "")).lower() == "base"
            ]
        except Exception as exc:
            request_exc = exc
            filtered = []
    if not filtered and request_exc is not None:
        logger.warning(
            "dexscreener fetch failed for %s via requests: %s",
            token_address,
            request_exc,
        )
        fallback_pairs: list[dict[str, object]] = []
        if _curl_available():
            fallback_pairs = _fetch_pairs_with_curl(url)
        else:
            logger.warning(
                "curl not available; skipping dexscreener fallback for %s",
                token_address,
            )
        filtered = [
            pair
            for pair in fallback_pairs
            if isinstance(pair, dict) and str(pair.get("chainId", "")).lower() == "base"
        ]

    _PAIR_CACHE[key] = (time.time(), filtered)
    return filtered


def _classify_pair(pair: dict[str, object]) -> str | None:
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
    pairs: Sequence[dict[str, object]],
    start_addr: str,
    end_addr: str,
    *,
    min_liquidity: float,
    allowed_dex: Sequence[str] | None = None,
) -> PairCandidate | None:
    if not pairs:
        return None
    start_l = start_addr.lower()
    end_l = end_addr.lower()
    allowed = {dex.lower() for dex in (allowed_dex or [])}
    best: PairCandidate | None = None

    max_liquidity_observed = _max_liquidity_usd(pairs, start_addr, end_addr)
    price_estimate = 0.0
    for pair in pairs:
        price_estimate = _extract_price_usd(pair, start_addr)
        if price_estimate > 0:
            break

    characteristics = classify_token(
        start_addr, price_estimate, max_liquidity_observed or min_liquidity
    )
    effective_min_liquidity = max(min_liquidity, characteristics.min_liquidity_usd)
    if characteristics.requires_high_liquidity:
        logger.debug(
            "token %s classified as high value: %s",
            start_addr,
            characteristics_as_dict(characteristics),
        )

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
        if liquidity < effective_min_liquidity:
            continue
        fee: int | None = None
        pool = str(pair.get("pairAddress") or "") or None
        if dex == "uniswap_v3":
            if not pool:
                continue
            fee = get_uniswap_v3_fee(pool)
            if fee is None:
                continue
        candidate = PairCandidate(
            tokens=(start_l, end_l), dex=dex, fee=fee, liquidity=liquidity, pool=pool
        )
        if best is None or candidate.liquidity > best.liquidity:
            best = candidate
    return best


def _auto_bridge_tokens(default_tokens: Sequence[str]) -> list[str]:
    raw = os.getenv("TRADE_PATH_BRIDGE_ADDRESSES")
    if raw:
        items = [item.strip() for item in raw.split(",") if item.strip()]
        return items or list(default_tokens)
    return list(default_tokens)


def _unique_routes(
    routes: Sequence[PairCandidate], *, fees: Sequence[int | None] | None = None
) -> dict[str, object]:
    tokens = [token.lower() for token in routes[0].tokens]
    if len(routes) > 1:
        for candidate in routes[1:]:
            tokens.append(candidate.tokens[1].lower())
    route: dict[str, object] = {"tokens": tokens}
    if fees is not None:
        route["fees"] = list(fees)
    return route


def _discover_v3_routes(
    diem_addr: str,
    quote_addr: str,
    bridge_tokens: Sequence[str],
    *,
    pairs_cache: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    diem_pairs = pairs_cache[diem_addr.lower()]
    direct = _best_pair(
        diem_pairs,
        diem_addr,
        quote_addr,
        min_liquidity=_MIN_DIRECT_LIQ_USD,
        allowed_dex=("uniswap_v3",),
    )
    if direct:
        routes.append(
            {
                "tokens": list(direct.tokens),
                "fees": [direct.fee] if direct.fee is not None else None,
                "metadata": _build_route_metadata(
                    hops=(direct,),
                    route_type="direct",
                    source="dynamic:v3",
                ),
            }
        )

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
            route = {
                "tokens": [diem_addr.lower(), bridge_l, quote_addr.lower()],
                "fees": fees,
                "metadata": _build_route_metadata(
                    hops=(bridge_pair, second),
                    route_type="bridge",
                    source="dynamic:v3",
                ),
            }
            routes.append(route)
    return routes


def _discover_v2_routes(
    diem_addr: str,
    quote_addr: str,
    bridge_tokens: Sequence[str],
    *,
    pairs_cache: dict[str, list[dict[str, object]]],
) -> list[dict[str, object]]:
    """Discover Aerodrome/UniswapV2 style routes (no fee tiers)."""
    routes: list[dict[str, object]] = []
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
        routes.append(
            {
                "tokens": list(direct.tokens),
                "metadata": _build_route_metadata(
                    hops=(direct,),
                    route_type="direct",
                    source="dynamic:v2",
                ),
            }
        )

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
        routes.append(
            {
                "tokens": tokens,
                "metadata": _build_route_metadata(
                    hops=(first, second),
                    route_type="bridge",
                    source="dynamic:v2",
                ),
            }
        )
    return routes


def discover_trade_paths(logger_instance=None) -> list[dict[str, object]]:
    log = logger_instance or logger
    diem = os.getenv("DIEM_TOKEN_ADDRESS")
    quote = os.getenv("QUOTE_TOKEN_ADDRESS")
    if not diem or not quote:
        log.warning(
            "dynamic trade path discovery skipped: DIEM_TOKEN_ADDRESS or QUOTE_TOKEN_ADDRESS missing"
        )
        return []
    diem = diem.strip()
    quote = quote.strip()
    weth = (os.getenv("WETH_ADDRESS") or "").strip()
    bridge_defaults: list[str] = []
    if weth:
        bridge_defaults.append(weth)
    vvv = (os.getenv("VVV_TOKEN_ADDRESS") or "").strip()
    if vvv:
        bridge_defaults.append(vvv)

    pairs_cache: dict[str, list[dict[str, object]]] = {}
    pairs_cache[diem.lower()] = _fetch_pairs(diem)
    bridge_tokens = _auto_bridge_tokens(bridge_defaults)
    for token in bridge_tokens:
        if token:
            pairs_cache.setdefault(token.lower(), _fetch_pairs(token))
    # Ensure quote token pairs are available when bridge token equals quote (e.g., WETH -> USDC)
    pairs_cache.setdefault(quote.lower(), _fetch_pairs(quote))

    routes: list[dict[str, object]] = []
    v3_routes = _discover_v3_routes(
        diem,
        quote,
        bridge_tokens,
        pairs_cache=pairs_cache,
    )
    v2_routes = _discover_v2_routes(
        diem,
        quote,
        bridge_tokens,
        pairs_cache=pairs_cache,
    )

    # Prioritize multi-hop routes over direct routes to ensure bridge routes are discovered
    # This helps when direct routes fill the limit before multi-hop routes are added
    multi_hop_routes: list[dict[str, object]] = []
    direct_routes: list[dict[str, object]] = []

    for spec in v3_routes + v2_routes:
        tokens = spec.get("tokens") or []
        if len(tokens) > 2:
            multi_hop_routes.append(spec)
        else:
            direct_routes.append(spec)

    # Add multi-hop routes first, then direct routes if no multi-hop routes found
    # This ensures bridge routes (DIEM→VVV→USDC) are preferred and prevents
    # "composite routing alignment failures" caused by mixing direct/bridge routes.
    routes.extend(multi_hop_routes)

    if not multi_hop_routes:
        routes.extend(direct_routes)
    elif direct_routes:
        log.info(
            "Skipping %d direct routes in favor of %d multi-hop routes to ensure bridge consistency",
            len(direct_routes),
            len(multi_hop_routes),
        )

    if os.getenv("DIEM_DEBUG_ROUTES"):
        log.debug(
            "Route discovery summary: %d multi-hop, %d direct (V3: %d, V2: %d)",
            len(multi_hop_routes),
            len(direct_routes),
            len(v3_routes),
            len(v2_routes),
        )

    uniq: dict[tuple[tuple[str, ...], tuple[int | None, ...]], dict[str, object]] = {}
    ordered: list[dict[str, object]] = []
    for spec in routes:
        tokens = tuple(str(addr).lower() for addr in spec.get("tokens") or [])
        fees = tuple(spec.get("fees") or [])
        key = (tokens, fees)
        if key in uniq:
            continue
        uniq[key] = spec
        ordered.append(spec)
        if len(ordered) >= _MAX_ROUTE_COUNT:
            if len(multi_hop_routes) > len(
                [s for s in ordered if len(s.get("tokens", [])) > 2]
            ):
                log.warning(
                    "Route limit (%d) reached; some multi-hop routes may be excluded. "
                    "Consider increasing TRADE_PATH_DYNAMIC_MAX_ROUTES.",
                    _MAX_ROUTE_COUNT,
                )
            break
    if not ordered:
        log.warning("dynamic trade path discovery produced no usable routes")
    else:
        for spec in ordered:
            log.info(
                "dynamic trade path: tokens=%s fees=%s",
                spec.get("tokens"),
                spec.get("fees"),
            )
    return ordered


def discover_trade_route_plans(logger_instance=None) -> list[RoutePlan]:
    specs = discover_trade_paths(logger_instance=logger_instance)
    plans: list[RoutePlan] = []
    for spec in specs:
        tokens = spec.get("tokens")
        if not tokens or len(tokens) < 2:
            continue
        fees = spec.get("fees")
        try:
            plan = make_route(tokens, fees)
        except Exception:
            logger.warning(
                "failed to build dynamic trade route tokens=%s fees=%s",
                tokens,
                fees,
                exc_info=True,
            )
            continue
        metadata = spec.get("metadata")
        if metadata:
            # Use object.__setattr__ to bypass frozen dataclass restriction
            object.__setattr__(plan, "_metadata", metadata)
        plans.append(plan)
    return plans
