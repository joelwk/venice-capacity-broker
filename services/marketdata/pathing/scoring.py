from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

from .models import GuardrailContext, PolicyContext, RouteEvaluation


def _expected_out_tokens(evaluation: RouteEvaluation) -> float:
    if not evaluation.valid_quote():
        return 0.0
    quote = evaluation.quote or {}
    amount_out = float(quote.get("amount_out") or 0.0)
    decimals = quote.get("decimals") or {}
    dec_out = int(decimals.get("out") or 0)
    if amount_out <= 0 or dec_out < 0:
        return 0.0
    return amount_out / float(10 ** dec_out)


def _slippage_penalty(quote: Dict[str, object], limit_bps: Optional[float]) -> float:
    if limit_bps is None or limit_bps <= 0:
        return 0.0
    try:
        slippage = float(quote.get("slippage_bps") or 0.0)
    except Exception:
        slippage = 0.0
    if slippage <= limit_bps:
        return 0.0
    return (slippage - limit_bps) * 5.0


def _pool_take_penalty(evaluation: RouteEvaluation, max_take_bps: Optional[float]) -> float:
    if max_take_bps is None or max_take_bps <= 0:
        return 0.0
    penalty = 0.0
    for hop in evaluation.hops:
        take = hop.metrics.get("pool_take_bps")
        if take is None:
            continue
        try:
            take_val = float(take)
        except Exception:
            continue
        if take_val <= max_take_bps:
            continue
        penalty += (take_val - max_take_bps) * 2.0
    return penalty


def _liquidity_penalty(evaluation: RouteEvaluation, floor_usd: Optional[float]) -> float:
    if floor_usd is None or floor_usd <= 0:
        return 0.0
    penalty = 0.0
    for hop in evaluation.hops:
        reserve_usd = hop.metrics.get("reserve_in_usd")
        if reserve_usd is None:
            continue
        try:
            reserve_val = float(reserve_usd)
        except Exception:
            continue
        if reserve_val >= floor_usd:
            continue
        deficit = floor_usd - reserve_val
        penalty += deficit * 0.05
    return penalty


def _progressive_penalty(policy: PolicyContext) -> float:
    if not policy.progressive_mode:
        return 0.0
    if policy.progressive_min_cycles is None or policy.progressive_min_cycles <= 0:
        return 0.0
    if policy.progressive_cycle is None:
        return 50.0
    remaining = max(policy.progressive_min_cycles - policy.progressive_cycle, 0)
    if remaining <= 0:
        return 0.0
    return float(remaining) * 25.0


def _provider_penalty(evaluation: RouteEvaluation) -> float:
    provider = evaluation.provider() or ""
    if not provider:
        return 25.0
    if provider in {"approx", "segments"}:
        return 15.0
    return 0.0


def multi_objective_score(
    evaluation: RouteEvaluation,
    guardrails: GuardrailContext,
    policy: PolicyContext,
    *,
    slippage_limit_bps: Optional[float] = None,
) -> Tuple[float, float, float, Dict[str, float]]:
    expected_out = _expected_out_tokens(evaluation)
    # Base score: prefer larger expected_out (lower score is better)
    base_score = -expected_out

    guardrail_penalty = 0.0
    guardrail_penalty += _pool_take_penalty(evaluation, guardrails.max_pool_take_bps)
    guardrail_penalty += _slippage_penalty(evaluation.quote or {}, slippage_limit_bps)

    policy_penalty = 0.0
    policy_penalty += _liquidity_penalty(evaluation, policy.liquidity_floor_usd)
    policy_penalty += _progressive_penalty(policy)
    policy_penalty += _provider_penalty(evaluation)

    score = base_score + guardrail_penalty + policy_penalty
    breakdown = {
        "base": base_score,
        "guardrail_penalty": guardrail_penalty,
        "policy_penalty": policy_penalty,
        "expected_out": expected_out,
    }
    if evaluation.quote and evaluation.quote.get("slippage_bps") is not None:
        try:
            breakdown["slippage_bps"] = float(evaluation.quote["slippage_bps"])
        except Exception:
            pass
    return score, guardrail_penalty, policy_penalty, breakdown


__all__ = ["multi_objective_score"]
