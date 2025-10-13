from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional, Sequence

from libs.dex.providers import DexAggregator, build_aggregator_from_env
from libs.dex.routes import RoutePlan

from services.marketdata import pools as pool_svc
from services.memory.store import MemoryStore

from .discovery import DiscoveryContext, discover_routes
from .env import EnvConfig, load_env_config
from .enrichment import enrich_route
from .fallbacks import bridge_fallback, external_reference_fallback
from .models import (
    GuardrailContext,
    PolicyContext,
    QuoteMode,
    QuoteRequest,
    QuoteResult,
    RouteEvaluation,
)
from .scoring import multi_objective_score


ExternalPriceFetcher = Callable[[str], Optional[float]]


class PathQuoteEngine:
    """Coordinates route discovery, scoring, and fallbacks."""

    def __init__(
        self,
        *,
        memory_store: Optional[MemoryStore] = None,
        external_price_fetcher: Optional[ExternalPriceFetcher] = None,
    ) -> None:
        self._memory = memory_store or MemoryStore(buffer_size=64)
        self._external_price_fetcher = external_price_fetcher
        self._aggregator_instance: Optional[DexAggregator] = None
        self._price_cache: Dict[str, float] = {}

    # ------------------------------------------------------------------
    @property
    def memory(self) -> MemoryStore:
        return self._memory

    # ------------------------------------------------------------------
    def _get_aggregator(self) -> DexAggregator:
        if self._aggregator_instance is None:
            self._aggregator_instance = build_aggregator_from_env()
        return self._aggregator_instance

    # ------------------------------------------------------------------
    def _slippage_limit_bps(self) -> Optional[float]:
        import os

        raw = os.getenv("RISK_MAX_SLIPPAGE_BPS")
        if raw is None or raw.strip() == "":
            return None
        try:
            val = float(raw)
            return val if val > 0 else None
        except Exception:
            return None

    # ------------------------------------------------------------------
    def _build_guardrails(self, mode: QuoteMode) -> GuardrailContext:
        import os

        max_pool_take = os.getenv("RISK_MAX_POOL_TAKE_BPS")
        max_pool_take_bps: Optional[float] = None
        if max_pool_take and max_pool_take.strip():
            try:
                max_pool_take_bps = float(max_pool_take)
            except Exception:
                max_pool_take_bps = None
        fetch_mode = "live" if mode == QuoteMode.LIVE else "dry"
        if mode == QuoteMode.PROGRESSIVE:
            fetch_mode = "progressive"
        vol_bps = os.getenv("REFLECTION_VOL_BPS_THRESHOLD")
        volatility: Optional[float] = None
        if vol_bps and vol_bps.strip():
            try:
                volatility = float(vol_bps)
            except Exception:
                volatility = None
        utilization_env = os.getenv("MARKETDATA_UTILIZATION_HINT")
        utilization: Optional[float] = None
        if utilization_env and utilization_env.strip():
            try:
                utilization = float(utilization_env)
            except Exception:
                utilization = None
        return GuardrailContext(
            fetch_mode=fetch_mode,
            max_pool_take_bps=max_pool_take_bps,
            volatility_bps=volatility,
            utilization=utilization,
        )

    # ------------------------------------------------------------------
    def _build_policy(self, config: EnvConfig, request: QuoteRequest) -> PolicyContext:
        import os

        liquidity_floor = os.getenv("PATH_LIQUIDITY_FLOOR_USD")
        try:
            liquidity_floor_usd = float(liquidity_floor) if liquidity_floor else None
        except Exception:
            liquidity_floor_usd = None
        stale_penalty = os.getenv("PATH_STALE_ROUTE_PENALTY_BPS")
        try:
            stale_penalty_bps = float(stale_penalty) if stale_penalty else None
        except Exception:
            stale_penalty_bps = None
        return PolicyContext(
            tenant_tier=request.tenant_tier,
            progressive_mode=config.progressive_live and request.mode != QuoteMode.DRY_RUN,
            progressive_cycle=request.progressive_cycle,
            progressive_min_cycles=config.progressive_min_cycles,
            liquidity_floor_usd=liquidity_floor_usd,
            stale_route_penalty_bps=stale_penalty_bps,
        )

    # ------------------------------------------------------------------
    def _price_map(self, config: EnvConfig) -> Dict[str, float]:
        fetcher = self._external_price_fetcher
        if fetcher is None:
            return {}
        mapping: Dict[str, float] = {}
        for symbol, addr in (
            ("DIEM", config.diem_token),
            ("VVV", config.vvv_token),
            ("USDC", config.quote_token),
        ):
            if not addr:
                continue
            if addr in self._price_cache:
                mapping[addr.lower()] = self._price_cache[addr]
                continue
            price = None
            try:
                price = fetcher(symbol)
            except Exception:
                price = None
            if price is None:
                continue
            try:
                price_f = float(price)
            except Exception:
                continue
            self._price_cache[addr] = price_f
            mapping[addr.lower()] = price_f
        return mapping

    # ------------------------------------------------------------------
    @staticmethod
    def _symbol_for_address(config: EnvConfig, address: str) -> Optional[str]:
        norm = (address or "").strip().lower()
        if not norm:
            return None
        mapping: Dict[str, str] = {}
        if config.diem_token:
            mapping[config.diem_token.strip().lower()] = "DIEM"
        if config.vvv_token:
            mapping[config.vvv_token.strip().lower()] = "VVV"
        if config.quote_token:
            mapping[config.quote_token.strip().lower()] = os.getenv("QUOTE_TOKEN_SYMBOL", "QUOTE").upper()
        return mapping.get(norm)

    # ------------------------------------------------------------------
    def _discover_routes(
        self,
        token_in: str,
        token_out: str,
        config: EnvConfig,
    ) -> List[RouteEvaluation]:
        try:
            routes_from_db: Sequence[RoutePlan] = pool_svc.suggest_routes_for_tokens(token_in, token_out)
        except Exception:
            routes_from_db = ()
        discovery_ctx = DiscoveryContext(routes_from_db=routes_from_db)
        candidates = discover_routes(token_in, token_out, config, discovery=discovery_ctx)
        return [RouteEvaluation(candidate=candidate) for candidate in candidates]

    # ------------------------------------------------------------------
    def quote(self, request: QuoteRequest) -> Optional[QuoteResult]:
        config = load_env_config()
        guardrails = self._build_guardrails(request.mode)
        policy = self._build_policy(config, request)
        price_map = self._price_map(config)
        evaluations: List[RouteEvaluation] = []

        candidate_evaluations = self._discover_routes(request.token_in, request.token_out, config)
        aggregator = self._get_aggregator()
        for evaluation in candidate_evaluations:
            enriched = enrich_route(
                request,
                evaluation.candidate,
                aggregator=aggregator,
                price_map=price_map,
            )
            enriched.guardrail_penalty = 0.0
            enriched.policy_penalty = 0.0
            enriched.score = float("inf")
            if enriched.valid_quote():
                score, guard_pen, policy_pen, breakdown = multi_objective_score(
                    enriched,
                    guardrails,
                    policy,
                    slippage_limit_bps=self._slippage_limit_bps(),
                )
                enriched.guardrail_penalty = guard_pen
                enriched.policy_penalty = policy_pen
                enriched.score = score
                if isinstance(enriched.quote, dict):
                    enriched.quote.setdefault("score_breakdown", breakdown)
            evaluations.append(enriched)

        best_eval: Optional[RouteEvaluation] = None
        for evaluation in evaluations:
            if not evaluation.valid_quote():
                continue
            if best_eval is None or (evaluation.score or float("inf")) < (best_eval.score or float("inf")):
                best_eval = evaluation

        if best_eval and best_eval.valid_quote():
            quote_dict = best_eval.quote or {}
            amount_in = int(quote_dict.get("amount_in") or request.amount_in_wei)
            amount_out = int(quote_dict.get("amount_out") or 0)
            price = float(quote_dict.get("price") or 0.0)
            provider = str(quote_dict.get("provider") or "")
            result = QuoteResult(
                amount_in=amount_in,
                amount_out=amount_out,
                price=price,
                provider=provider,
                route=best_eval.candidate.route,
                score=float(best_eval.score or 0.0),
                guardrails=guardrails,
                policy=policy,
                mode=request.mode,
                source=best_eval.candidate.source,
                metadata={
                    "score_breakdown": quote_dict.get("score_breakdown"),
                    "policy_penalty": best_eval.policy_penalty,
                    "guardrail_penalty": best_eval.guardrail_penalty,
                    "hops": [hop.as_dict() for hop in best_eval.hops],
                },
            )
            self._record(request, result, evaluations)
            return result

        # Fallback: bridge path for DIEM price
        fallback_result: Optional[QuoteResult] = None
        if config.diem_token and config.quote_token:
            token_in_l = request.token_in.lower()
            token_out_l = request.token_out.lower()
            if token_in_l == (config.diem_token or "").strip().lower() and token_out_l == (
                config.quote_token or ""
            ).strip().lower():
                fallback_result = bridge_fallback(
                    amount_in=request.amount_in_wei,
                    config=config,
                    guardrails=guardrails,
                    policy=policy,
                    mode=request.mode,
                )
        if fallback_result is None and self._external_price_fetcher:
            fallback_result = external_reference_fallback(
                token_in=request.token_in,
                token_out=request.token_out,
                amount_in=request.amount_in_wei,
                fetcher=self._external_price_fetcher,
                guardrails=guardrails,
                policy=policy,
                mode=request.mode,
                token_symbol=self._symbol_for_address(config, request.token_in),
            )
        if fallback_result:
            self._record(request, fallback_result, evaluations)
            return fallback_result

        self._record(request, None, evaluations)
        return None

    # ------------------------------------------------------------------
    def _record(
        self,
        request: QuoteRequest,
        result: Optional[QuoteResult],
        evaluations: Sequence[RouteEvaluation],
    ) -> None:
        payload = {
            "ts": time.time(),
            "path_quote": {
                "request": {
                    "token_in": request.token_in,
                    "token_out": request.token_out,
                    "amount_in": request.amount_in_wei,
                    "mode": request.mode.value,
                    "tenant_tier": request.tenant_tier,
                    "progressive_cycle": request.progressive_cycle,
                },
                "result": result.as_dict() if result else None,
                "evaluations": [evaluation.as_dict() for evaluation in evaluations],
            },
        }
        try:
            self._memory.record_cycle(payload)
        except Exception:
            # Memory logging should never raise to callers
            pass


__all__ = ["PathQuoteEngine", "QuoteRequest", "QuoteResult", "QuoteMode"]
