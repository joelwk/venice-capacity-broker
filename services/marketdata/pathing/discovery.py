from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from libs.dex.routes import RoutePlan, make_route

from .env import EnvConfig
from .models import RouteCandidate


def _normalize(addr: str) -> str:
    value = (addr or "").strip()
    if not value:
        return ""
    return value.lower()


def _route_key(route: RoutePlan) -> Tuple[Tuple[str, ...], Tuple[Optional[int], ...]]:
    tokens = tuple(route.tokens)
    fees = tuple(hop.fee for hop in route.hops)
    return tokens, fees


@dataclass
class DiscoveryContext:
    routes_from_db: Sequence[RoutePlan] = ()


def discover_routes(
    token_in: str,
    token_out: str,
    config: EnvConfig,
    *,
    discovery: Optional[DiscoveryContext] = None,
) -> List[RouteCandidate]:
    """Enumerate route candidates seeded from env, DB, and heuristics."""

    src = _normalize(token_in)
    dst = _normalize(token_out)
    if not src or not dst or src == dst:
        return []

    routes: List[RouteCandidate] = []
    seen: set[Tuple[Tuple[str, ...], Tuple[Optional[int], ...]]] = set()

    def _add(route: RoutePlan, source: str, reason: Optional[str] = None) -> None:
        key = _route_key(route)
        if key in seen:
            return
        seen.add(key)
        routes.append(RouteCandidate(route=route, source=source, reason=reason))

    # 1. Env trade paths
    for plan in config.trade_paths:
        tokens = tuple(getattr(plan, "tokens", ()))
        if not tokens:
            continue
        if _normalize(tokens[0]) == src and _normalize(tokens[-1]) == dst:
            _add(plan, source="env", reason="trade_paths")

    # 2. Database discovery (pool watcher)
    if discovery and discovery.routes_from_db:
        for plan in discovery.routes_from_db:
            tokens = getattr(plan, "tokens", ())
            if not tokens:
                continue
            if _normalize(tokens[0]) == src and _normalize(tokens[-1]) == dst:
                _add(plan, source="pools", reason="suggest_routes")

    # 3. Direct path
    try:
        direct = make_route([token_in, token_out])
        _add(direct, source="heuristic", reason="direct")
    except Exception:
        pass

    # 4. Bridge token (usually WETH)
    bridge = config.bridge_token
    if bridge:
        bridge_norm = _normalize(bridge)
        if bridge_norm not in {src, dst}:
            try:
                route = make_route([token_in, bridge, token_out])
                _add(route, source="heuristic", reason="bridge_token")
            except Exception:
                pass

    # 5. VVV bridge fallback
    vvv = config.vvv_token
    if vvv:
        vvv_norm = _normalize(vvv)
        if vvv_norm not in {src, dst}:
            try:
                route = make_route([token_in, vvv, token_out])
                _add(route, source="heuristic", reason="vvv_bridge")
            except Exception:
                pass

    # 6. Reciprocal direct (useful when initial direct path fails)
    try:
        reverse = make_route([token_out, token_in])
        rev_tokens = tuple(reverse.tokens)
        rev_key = (tuple(reversed(rev_tokens)), tuple(reversed([hop.fee for hop in reverse.hops])))
        if rev_key in seen:
            # Add reversed route explicitly if not already added
            direct_tokens = list(reversed(rev_tokens))
            try:
                forward = make_route(direct_tokens)
                _add(forward, source="heuristic", reason="reverse_replay")
            except Exception:
                pass
    except Exception:
        pass

    return routes


__all__ = ["discover_routes", "DiscoveryContext"]
