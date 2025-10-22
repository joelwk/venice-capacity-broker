from __future__ import annotations

import os
import time
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from libs.dex.providers import DexAggregator, Quote as DexQuote
from libs.dex.routes import RoutePlan

from services.marketdata.etherscan_verify import (
    get_cached_pair_info_for_tokens,
    verify_trade_path,
)

from .models import HopTelemetry, QuoteRequest, RouteCandidate, RouteEvaluation


def _gas_cost_usd_default() -> float:
    raw = os.getenv("PATH_GAS_COST_USD")
    if raw is None or raw.strip() == "":
        return 0.0
    try:
        return max(0.0, float(raw))
    except Exception:
        return 0.0


def _normalize(addr: str) -> str:
    value = (addr or "").strip()
    if not value:
        return ""
    if value.startswith("0x"):
        return value.lower()
    return "0x" + value.lower()


@lru_cache(maxsize=256)
def _erc20_decimals(address: str) -> int:
    if not address:
        return 18
    try:
        from web3 import Web3  # type: ignore
        from libs.agentkit_ext.web3_utils import get_contract, get_web3

        w3 = get_web3()
        contract = get_contract(w3, Web3.to_checksum_address(address), "erc20.json")
        return int(contract.functions.decimals().call())
    except Exception:
        # Fallback decimals heuristics (USDC = 6, otherwise 18)
        addr_norm = _normalize(address)
        if addr_norm.endswith(("13", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913")):
            return 6
        return 18


def _normalize_amount(amount: int, decimals: int) -> float:
    if amount <= 0:
        return 0.0
    return float(amount) / float(10 ** int(decimals))


def _uni_v2_out(amount_in: int, reserve_in: int, reserve_out: int, fee_bps: int = 30) -> int:
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return 0
    fee_n = 10_000 - int(fee_bps)
    amount_in_with_fee = int(amount_in) * fee_n // 10_000
    num = int(reserve_out) * int(amount_in_with_fee)
    den = int(reserve_in) + int(amount_in_with_fee)
    if den <= 0:
        return 0
    return int(num // den)


def _approx_exec_price(amount_in: int, route: RoutePlan, fee_bps_per_hop: int = 30) -> Optional[float]:
    try:
        route.ensure_v2()
    except Exception:
        return None
    tokens = list(route.tokens)
    if len(tokens) < 2:
        return None
    amt = int(amount_in)
    for idx in range(len(tokens) - 1):
        a = tokens[idx]
        b = tokens[idx + 1]
        info = get_cached_pair_info_for_tokens(a, b)
        if not info:
            info = get_cached_pair_info_for_tokens(b, a)
        if not info:
            return None
        reserves = info.get("reserves")
        token0 = info.get("token0")
        token1 = info.get("token1")
        if not isinstance(reserves, tuple) or len(reserves) < 2 or not token0 or not token1:
            return None
        t0 = _normalize(token0)
        t1 = _normalize(token1)
        ain = _normalize(a)
        aout = _normalize(b)
        if ain == t0 and aout == t1:
            rin, rout = int(reserves[0]), int(reserves[1])
        elif ain == t1 and aout == t0:
            rin, rout = int(reserves[1]), int(reserves[0])
        else:
            return None
        out_units = _uni_v2_out(amt, rin, rout, fee_bps=int(fee_bps_per_hop))
        if out_units <= 0:
            return None
        amt = out_units
    dec_in = _erc20_decimals(tokens[0])
    dec_out = _erc20_decimals(tokens[-1])
    if dec_in < 0 or dec_out < 0:
        return None
    inp = _normalize_amount(amount_in, dec_in)
    outp = _normalize_amount(amt, dec_out)
    if inp <= 0:
        return None
    return float(outp / inp)


def _quote_via_aggregator(aggregator: DexAggregator, amount_in: int, route: RoutePlan) -> Optional[DexQuote]:
    start = time.perf_counter()
    try:
        quote = aggregator.best_quote(amount_in, route)
        if quote is None:
            return None
        return quote
    except Exception:
        return None
    finally:
        elapsed = time.perf_counter() - start
        if elapsed > 1.5 and os.getenv("DIEM_DEBUG_ROUTES"):
            from libs.telemetry.logger import get_logger

            logger = get_logger("marketdata.pathing")
            logger.debug("aggregator quote latency %.3fs route=%s", elapsed, list(route.tokens))


def _quote_to_dict(quote: DexQuote, route: RoutePlan) -> Dict[str, Any]:
    tokens = list(route.tokens)
    dec_in = _erc20_decimals(tokens[0])
    dec_out = _erc20_decimals(tokens[-1])
    price = 0.0
    if quote.amount_in > 0 and quote.amount_out > 0:
        norm_in = _normalize_amount(quote.amount_in, dec_in)
        norm_out = _normalize_amount(quote.amount_out, dec_out)
        if norm_in > 0:
            price = norm_out / norm_in
    payload: Dict[str, Any] = {
        "provider": quote.provider,
        "amount_in": int(quote.amount_in),
        "amount_out": int(quote.amount_out),
        "decimals": {"in": dec_in, "out": dec_out},
        "price": price,
        "path": list(getattr(quote, "path", quote.route.tokens if quote.route else tokens)),
    }
    return payload


def _ensure_route_verified(route: RoutePlan) -> None:
    tokens = list(route.tokens)
    if len(tokens) < 2:
        return
    try:
        verify_trade_path(tokens, [hop.fee for hop in route.hops])
    except Exception:
        return


def _pool_direction(
    token_in: str,
    token_out: str,
    info: Dict[str, Any],
) -> Tuple[Optional[int], Optional[int], Optional[int], bool]:
    reserves = info.get("reserves")
    if not isinstance(reserves, tuple) or len(reserves) < 2:
        return None, None, None, False
    token0 = _normalize(info.get("token0") or "")
    token1 = _normalize(info.get("token1") or "")
    ain = _normalize(token_in)
    aout = _normalize(token_out)
    if token0 and token1:
        if ain == token0 and aout == token1:
            return int(reserves[0]), int(reserves[1]), int(reserves[2] if len(reserves) > 2 else 0), True
        if ain == token1 and aout == token0:
            return int(reserves[1]), int(reserves[0]), int(reserves[2] if len(reserves) > 2 else 0), True
    return None, None, None, False


def _pool_take_bps(amount_in: int, reserve_in: int) -> Optional[float]:
    if amount_in <= 0 or reserve_in <= 0:
        return None
    ratio = float(amount_in) / float(reserve_in)
    if ratio <= 0:
        return None
    return min(10000.0, ratio * 10_000.0)


def _price_lookup(price_map: Dict[str, float], addr: str) -> Optional[float]:
    if not price_map:
        return None
    key = _normalize(addr)
    if not key:
        return None
    value = price_map.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def enrich_route(
    request: QuoteRequest,
    candidate: RouteCandidate,
    *,
    aggregator: DexAggregator,
    price_map: Optional[Dict[str, float]] = None,
) -> RouteEvaluation:
    evaluation = RouteEvaluation(candidate=candidate)
    plan = candidate.route
    price_map = {(_normalize(k) if isinstance(k, str) else k): float(v) for k, v in (price_map or {}).items() if v is not None}

    quote = _quote_via_aggregator(aggregator, request.amount_in_wei, plan)
    if quote:
        evaluation.quote = _quote_to_dict(quote, plan)
    else:
        evaluation.errors.append("aggregator_empty")

    # Attempt approximate execution price for slippage context
    if evaluation.quote:
        approx_price = _approx_exec_price(request.amount_in_wei, plan)
        if approx_price and approx_price > 0:
            try:
                exec_price = float(evaluation.quote.get("price") or 0.0)
            except Exception:
                exec_price = 0.0
            if exec_price > 0:
                diff = abs(exec_price - approx_price) / approx_price
                diff_bps = diff * 10_000.0
                evaluation.quote["slippage_bps"] = diff_bps
                evaluation.quote["approx_price"] = approx_price
                if diff > 0.05:
                    evaluation.errors.append("aggregator_price_outlier")
                    evaluation.quote = None

    # Build hop telemetry (verify path lazily)
    tokens = list(plan.tokens)
    fees = [hop.fee for hop in plan.hops]
    ensure_verified = False

    hop_inputs: List[int] = []
    running_amount = request.amount_in_wei
    hop_inputs.append(running_amount)

    # Pre-compute hop amounts using reserves where available to align pool take
    for idx in range(len(tokens) - 1):
        a = tokens[idx]
        b = tokens[idx + 1]
        info = get_cached_pair_info_for_tokens(a, b)
        if not info:
            info = get_cached_pair_info_for_tokens(b, a)
        if not info:
            ensure_verified = True
            continue
        reserve_in, reserve_out, _, mapped = _pool_direction(a, b, info)
        if reserve_in is None or reserve_out is None:
            continue
        fee_bps = fees[idx] if idx < len(fees) and fees[idx] is not None else 30
        running_amount = _uni_v2_out(running_amount, reserve_in, reserve_out, fee_bps=int(fee_bps))
        if running_amount <= 0:
            break
        hop_inputs.append(running_amount)

    if ensure_verified:
        _ensure_route_verified(plan)

    hops: List[HopTelemetry] = []
    for idx in range(len(tokens) - 1):
        token_in = tokens[idx]
        token_out = tokens[idx + 1]
        info = get_cached_pair_info_for_tokens(token_in, token_out)
        source = "cache"
        if not info:
            info = get_cached_pair_info_for_tokens(token_out, token_in)
            if info:
                source = "cache_reverse"
        if not info:
            source = "verify_path"
            # ensure verify ran
            _ensure_route_verified(plan)
            info = get_cached_pair_info_for_tokens(token_in, token_out) or get_cached_pair_info_for_tokens(
                token_out, token_in
            )

        telemetry = HopTelemetry(token_in=token_in, token_out=token_out, pool=None, status="missing")
        if info:
            pool_addr = info.get("pair")
            if pool_addr:
                telemetry.pool = pool_addr
            telemetry.diagnostics["source"] = source
            reserve_in, reserve_out, timestamp, mapped = _pool_direction(token_in, token_out, info)
            if reserve_in is not None and reserve_out is not None and mapped:
                fee_bps = fees[idx] if idx < len(fees) and fees[idx] is not None else 30
                telemetry.status = "ok"
                telemetry.metrics["fee_bps"] = fee_bps
                telemetry.metrics["reserve_in"] = reserve_in
                telemetry.metrics["reserve_out"] = reserve_out
                telemetry.metrics["timestamp"] = timestamp
                try:
                    dec_in = _erc20_decimals(token_in)
                    dec_out = _erc20_decimals(token_out)
                    reserve_in_tokens = reserve_in / float(10 ** dec_in)
                    reserve_out_tokens = reserve_out / float(10 ** dec_out)
                    telemetry.metrics["reserve_in_tokens"] = reserve_in_tokens
                    telemetry.metrics["reserve_out_tokens"] = reserve_out_tokens
                    price_in = _price_lookup(price_map, token_in)
                    if price_in is not None and price_in > 0:
                        telemetry.metrics["reserve_in_usd"] = reserve_in_tokens * price_in
                    price_out = _price_lookup(price_map, token_out)
                    if price_out is not None and price_out > 0:
                        telemetry.metrics["reserve_out_usd"] = reserve_out_tokens * price_out
                except Exception:
                    pass
                if idx < len(hop_inputs):
                    take_bps = _pool_take_bps(hop_inputs[idx], reserve_in)
                    if take_bps is not None:
                        telemetry.metrics["pool_take_bps"] = take_bps
                if timestamp:
                    telemetry.metrics["age_seconds"] = max(0, int(time.time()) - int(timestamp))
            else:
                telemetry.status = "unknown"
        else:
            telemetry.errors.append("pool_unavailable")
        hops.append(telemetry)

    evaluation.hops = hops
    # Gas cost metadata for scoring
    if evaluation.quote is not None:
        evaluation.quote.setdefault("gas_cost_usd", _gas_cost_usd_default())
    return evaluation


__all__ = ["enrich_route"]
