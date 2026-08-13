from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass

from libs.dex.diem_routing import (
    get_diem_canonical_routes,
    should_use_diem_canonical_route,
)
from libs.dex.routes import RouteHop, RoutePlan, make_route

from .env import EnvConfig
from .models import RouteCandidate


def _normalize(addr: str) -> str:
    value = (addr or "").strip()
    if not value:
        return ""
    return value.lower()


def _route_key(route: RoutePlan) -> tuple[tuple[str, ...], tuple[int | None, ...]]:
    tokens = tuple(route.tokens)
    fees = tuple(hop.fee for hop in route.hops)
    return tokens, fees


_REASON_PRIORITY: dict[str | None, int] = {
    "diem_canonical": 1,  # Highest priority for DIEM trades
    "diem_direct_preferred": 90,
    "trade_paths": 5,
    "vvv_usdc_v3_pref": 35,
    "suggest_routes": 10,
    "reverse_replay": 25,
    "direct": 30,
    "bridge_token": 40,
    "vvv_bridge": 40,
    "diem_bridge_quote": 70,
    "diem_vvv_quote": 70,
    "diem_bridge_token": 80,
    "diem_vvv_bridge": 80,
}


def _reason_weight(reason: str | None, source: str) -> int:
    if reason in _REASON_PRIORITY:
        return _REASON_PRIORITY[reason]
    if source == "heuristic":
        return 30
    if source == "pools":
        return 20
    if source == "env":
        return 10
    return 0


@dataclass
class DiscoveryContext:
    routes_from_db: Sequence[RoutePlan] = ()


def discover_routes(
    token_in: str,
    token_out: str,
    config: EnvConfig,
    *,
    discovery: DiscoveryContext | None = None,
) -> list[RouteCandidate]:
    """Enumerate route candidates seeded from env, DB, and heuristics."""

    src = _normalize(token_in)
    dst = _normalize(token_out)
    if not src or not dst or src == dst:
        return []

    # Get max hops limit (default 2-hop routes)
    max_hops = 2
    try:
        max_hops = int(os.getenv("DIEM_MAX_ROUTE_HOPS", "2") or 2)
        max_hops = max(2, min(max_hops, 4))  # Clamp between 2 and 4
    except Exception:
        pass

    diem_norm = _normalize(config.diem_token or "")
    quote_norm = _normalize(config.quote_token or "")
    vvv_norm = _normalize(config.vvv_token or "")
    bridge_norm = _normalize(config.bridge_token or "")
    try:
        vvv_usdc_fee: int | None = int(
            os.getenv("VVV_USDC_POOL_FEE", "3000").strip() or 3000
        )
    except Exception:
        vvv_usdc_fee = 3000
    vvv_usdc_force_v3 = os.getenv("VVV_USDC_FORCE_V3", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    routes: list[RouteCandidate] = []
    seen: dict[tuple[tuple[str, ...], tuple[int | None, ...]], RouteCandidate] = {}

    def _force_vvv_usdc_v3(
        route: RoutePlan, reason: str | None
    ) -> tuple[RoutePlan, str | None]:
        """Force VVV→USDC hop to V3 when fee is missing to avoid V2 dead pools."""
        if not (vvv_usdc_force_v3 and vvv_norm and quote_norm and vvv_usdc_fee):
            return route, reason
        updated = False
        new_hops: list[RouteHop] = []
        for hop in route.hops:
            tokens = {_normalize(hop.token_in), _normalize(hop.token_out)}
            if hop.fee is None and tokens == {vvv_norm, quote_norm}:
                new_hops.append(RouteHop(hop.token_in, hop.token_out, vvv_usdc_fee))
                updated = True
            else:
                new_hops.append(hop)
        if not updated:
            return route, reason
        try:
            forced_route = RoutePlan(tuple(new_hops))
        except Exception:
            return route, reason
        forced_reason = reason or "vvv_usdc_v3_pref"
        return forced_route, forced_reason

    def _prefer_vvv_usdc_v3(route: RoutePlan, source: str, reason: str | None) -> None:
        if not vvv_norm or not quote_norm:
            return
        if vvv_usdc_fee is None:
            return
        hops = list(getattr(route, "hops", ()))
        if not hops:
            return
        for idx, hop in enumerate(hops):
            try:
                token_in_norm = _normalize(hop.token_in)
                token_out_norm = _normalize(hop.token_out)
            except Exception:
                continue
            if {token_in_norm, token_out_norm} != {vvv_norm, quote_norm}:
                continue
            if hop.fee is not None:
                return
            v3_hops = list(hops)
            try:
                v3_hops[idx] = RouteHop(hop.token_in, hop.token_out, vvv_usdc_fee)
                v3_route = RoutePlan(tuple(v3_hops))
            except Exception:
                return
            v3_reason = reason or "vvv_usdc_v3_pref"
            _add(v3_route, source=source, reason=v3_reason)
            return

    def _add(route: RoutePlan, source: str, reason: str | None = None) -> None:
        # Filter routes by max hops before adding
        try:
            route_hops = len(getattr(route, "hops", ()))
            if route_hops > max_hops:
                return  # Skip routes exceeding max hops
        except Exception:
            pass  # If we can't determine hop count, include it (conservative)
        route, reason = _force_vvv_usdc_v3(route, reason)
        key = _route_key(route)
        existing = seen.get(key)
        if existing:
            if reason:
                new_weight = _reason_weight(reason, source)
                current_weight = _reason_weight(existing.reason, existing.source)
                if new_weight > current_weight or existing.reason is None:
                    existing.reason = reason
            return
        candidate = RouteCandidate(route=route, source=source, reason=reason)
        seen[key] = candidate
        routes.append(candidate)
        _prefer_vvv_usdc_v3(route, source, reason)

    # 0a. Direct DIEM/USDC route (highest priority when configured)
    diem_usdc_pool = config.diem_usdc_pool
    prefer_direct = os.getenv("DIEM_PREFER_DIRECT_ROUTE", "1").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    is_diem_quote_pair = (
        bool(diem_norm) and bool(quote_norm) and {src, dst} == {diem_norm, quote_norm}
    )
    if prefer_direct and diem_usdc_pool and is_diem_quote_pair:
        try:
            direct_route = make_route([token_in, token_out])
            _add(direct_route, source="direct_pool", reason="diem_direct_preferred")
        except Exception:
            pass

    # 0. DIEM canonical routes (highest priority for DIEM trades)
    if should_use_diem_canonical_route(token_in, token_out, config):
        diem_routes = get_diem_canonical_routes(token_in, token_out, config)
        for route in diem_routes:
            _add(route, source="diem_canonical", reason="diem_canonical")

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
            # Filter by max hops: routes from DB discovery should respect DIEM_MAX_ROUTE_HOPS
            try:
                route_hops = len(getattr(plan, "hops", ()))
                if route_hops > max_hops:
                    continue  # Skip routes exceeding max hops
            except Exception:
                pass  # If we can't determine hop count, include it (conservative)
            if _normalize(tokens[0]) == src and _normalize(tokens[-1]) == dst:
                _add(plan, source="pools", reason="suggest_routes")

    if diem_norm and (src == diem_norm or dst == diem_norm):

        def _push(mid_tokens: Sequence[str | None], reason: str) -> None:
            seq: list[str] = [token_in]
            for item in mid_tokens:
                if not item:
                    return
                norm_item = _normalize(item)
                if not norm_item or norm_item == _normalize(seq[-1]):
                    continue
                seq.append(item)
            if seq[-1] != token_out:
                seq.append(token_out)
            if _normalize(seq[0]) != src or _normalize(seq[-1]) != dst:
                return
            # Filter by max hops: don't generate routes exceeding max_hops
            # seq has len(seq) tokens = len(seq)-1 hops
            if len(seq) - 1 > max_hops:
                return  # Skip routes exceeding max hops
            # Prefer V3 on VVV→USDC hop by injecting fee when applicable
            fee_augmented = None
            if (
                vvv_usdc_fee is not None
                and len(seq) >= 2
                and _normalize(seq[-2]) == vvv_norm
                and _normalize(seq[-1]) == quote_norm
            ):
                try:
                    fees: list[int | None] = [None] * (len(seq) - 2) + [vvv_usdc_fee]
                    fee_augmented = make_route(seq, fees)
                    _add(
                        fee_augmented,
                        source="heuristic",
                        reason=f"{reason}_v3" if reason else "v3_pref",
                    )
                except Exception:
                    fee_augmented = None
            try:
                route = make_route(seq)
                _add(route, source="heuristic", reason=reason)
            except Exception:
                if fee_augmented is None:
                    pass

        # Check if WETH routes (bridge_token) should be skipped
        skip_bridge_token = os.getenv(
            "DIEM_DISABLE_CANONICAL_WETH", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        if (
            not skip_bridge_token
            and config.bridge_token
            and bridge_norm not in {src, dst}
        ):
            _push([config.bridge_token], "diem_bridge_token")
        if config.vvv_token and vvv_norm not in {src, dst}:
            _push([config.vvv_token], "diem_vvv_bridge")
        if (
            not skip_bridge_token
            and config.bridge_token
            and config.quote_token
            and quote_norm not in {src, dst}
        ):
            _push([config.bridge_token, config.quote_token], "diem_bridge_quote")
        if config.vvv_token and config.quote_token and quote_norm not in {src, dst}:
            _push([config.vvv_token, config.quote_token], "diem_vvv_quote")

    # 3. Direct path
    try:
        direct = make_route([token_in, token_out])
        _add(direct, source="heuristic", reason="direct")
    except Exception:
        pass

    # 4. Bridge token (usually WETH) - skip if DIEM_DISABLE_CANONICAL_WETH is set
    disable_canonical_weth = os.getenv(
        "DIEM_DISABLE_CANONICAL_WETH", "0"
    ).strip().lower() in {"1", "true", "yes", "on"}
    bridge = config.bridge_token
    if bridge and not disable_canonical_weth:
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
        rev_key = (
            tuple(reversed(rev_tokens)),
            tuple(reversed([hop.fee for hop in reverse.hops])),
        )
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


__all__ = ["DiscoveryContext", "discover_routes"]
