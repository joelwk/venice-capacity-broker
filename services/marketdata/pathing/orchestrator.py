from __future__ import annotations

import math
import os
import time
from collections import deque
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Lock
from typing import Any

from libs.dex.providers import DexAggregator, build_aggregator_from_env
from libs.dex.routes import RoutePlan
from services.marketdata import pools as pool_svc
from services.memory.store import MemoryStore

from .discovery import DiscoveryContext, discover_routes
from .enrichment import enrich_route
from .env import EnvConfig, load_env_config
from .fallbacks import bridge_fallback, external_reference_fallback
from .models import (
    GuardrailContext,
    PolicyContext,
    QuoteMode,
    QuoteRequest,
    QuoteResult,
    RouteCandidate,
    RouteEvaluation,
)
from .scoring import multi_objective_score
from .validation import validate_diem_route_price

ExternalPriceFetcher = Callable[[str], float | None]


def _sanitize_for_json(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        return [_sanitize_for_json(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_for_json(val) for key, val in value.items()}
    if isinstance(value, (int, str, bool)) or value is None:
        return value
    return str(value)


class PathQuoteEngine:
    """Coordinates route discovery, scoring, and fallbacks."""

    def __init__(
        self,
        *,
        memory_store: MemoryStore | None = None,
        external_price_fetcher: ExternalPriceFetcher | None = None,
    ) -> None:
        self._memory = memory_store or MemoryStore(buffer_size=64)
        self._external_price_fetcher = external_price_fetcher
        self._aggregator_instance: DexAggregator | None = None
        self._price_cache: dict[str, float] = {}
        self._route_cache: dict[str, dict[str, Any]] = {}
        self._route_cache_lock = Lock()
        self._provider_timeout_streak: dict[str, int] = {}
        self._provider_backoff: dict[str, float] = {}
        self._provider_lock = Lock()

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
    def _slippage_limit_bps(self) -> float | None:
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
    @staticmethod
    def _soft_timeout_seconds() -> float | None:
        """Best-effort budget to keep pathing inside the provider thread timeout."""

        raw = os.getenv("PATH_ENGINE_SOFT_TIMEOUT_SECONDS")
        if raw is None or raw.strip() == "":
            raw = os.getenv("MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS")
        if raw is None or raw.strip() == "":
            return 10.0
        try:
            val = float(raw) if raw is not None else 0.0
        except Exception:
            return 10.0
        if val <= 0:
            return None
        # Cap to avoid excessively long budgets if env is mis-set
        return min(val, 60.0)

    # ------------------------------------------------------------------
    @staticmethod
    def _soft_timeout_margin_seconds() -> float:
        raw = os.getenv("PATH_ENGINE_SOFT_TIMEOUT_MARGIN_SECONDS")
        try:
            margin = float(raw) if raw else 0.75
        except Exception:
            margin = 0.75
        # Keep the margin sensible so short budgets still run a route
        return max(0.05, min(margin, 5.0))

    # ------------------------------------------------------------------
    @staticmethod
    def _route_cache_ttl_seconds() -> float:
        raw = os.getenv("PATH_ENGINE_ROUTE_CACHE_TTL_SECONDS")
        try:
            ttl = float(raw) if raw else 60.0
        except Exception:
            ttl = 60.0
        # Bound to avoid stale routes lingering too long
        return max(5.0, min(ttl, 600.0))

    # ------------------------------------------------------------------
    @staticmethod
    def _route_worker_count(num_candidates: int) -> int:
        raw = os.getenv("PATH_ENGINE_ROUTE_WORKERS")
        try:
            workers = int(raw) if raw else 2
        except Exception:
            workers = 2
        if workers <= 0:
            workers = 1
        return max(1, min(workers, max(1, num_candidates)))

    # ------------------------------------------------------------------
    @staticmethod
    def _provider_timeout_threshold() -> int:
        raw = os.getenv("PATH_ENGINE_PROVIDER_TIMEOUT_THRESHOLD")
        try:
            threshold = int(raw) if raw else 3
        except Exception:
            threshold = 3
        return max(1, threshold)

    # ------------------------------------------------------------------
    @staticmethod
    def _provider_backoff_seconds() -> float:
        raw = os.getenv("PATH_ENGINE_PROVIDER_BACKOFF_SECONDS")
        try:
            value = float(raw) if raw else 180.0
        except Exception:
            value = 180.0
        return max(5.0, value)

    # ------------------------------------------------------------------
    @staticmethod
    def _min_route_budget_seconds() -> float:
        raw = os.getenv("PATH_ENGINE_MIN_ROUTE_BUDGET_SECONDS")
        try:
            value = float(raw) if raw else 0.35
        except Exception:
            value = 0.35
        return max(0.05, min(value, 5.0))

    # ------------------------------------------------------------------
    def _route_cache_key(self, token_in: str, token_out: str, config: EnvConfig) -> str:
        return "|".join(
            [
                (token_in or "").strip().lower(),
                (token_out or "").strip().lower(),
                (config.bridge_token or "").strip().lower(),
                (config.quote_token or "").strip().lower(),
                (config.vvv_token or "").strip().lower(),
                str(bool(config.progressive_live)),
            ]
        )

    # ------------------------------------------------------------------
    @staticmethod
    def _is_diem_pair(
        token_in: str | None, token_out: str | None, config: EnvConfig
    ) -> bool:
        try:
            diem = (config.diem_token or "").strip().lower()
            quote = (config.quote_token or "").strip().lower()
            if not diem or not quote:
                return False
            return {
                str(token_in or "").strip().lower(),
                str(token_out or "").strip().lower(),
            } == {diem, quote}
        except Exception:
            return False

    # ------------------------------------------------------------------
    def _record_provider_diagnostics(
        self,
        diagnostics: Sequence[dict[str, Any]] | None,
        local_blocklist: set[str] | None = None,
    ) -> None:
        if not diagnostics:
            return
        now = time.perf_counter()
        threshold = self._provider_timeout_threshold()
        backoff = self._provider_backoff_seconds()
        with self._provider_lock:
            for entry in diagnostics:
                try:
                    provider = str(entry.get("provider") or "").strip().lower()
                    status = str(entry.get("status") or "").strip().lower()
                except Exception:
                    continue
                if not provider:
                    continue
                if status == "ok":
                    self._provider_timeout_streak.pop(provider, None)
                    self._provider_backoff.pop(provider, None)
                    continue
                if status in {"timeout", "timeout_pending"}:
                    streak = self._provider_timeout_streak.get(provider, 0) + 1
                    self._provider_timeout_streak[provider] = streak
                    if streak >= threshold:
                        self._provider_backoff[provider] = now + backoff
                    if local_blocklist is not None:
                        local_blocklist.add(provider)
                elif status == "error" and local_blocklist is not None:
                    local_blocklist.add(provider)

    # ------------------------------------------------------------------
    def _slow_provider_blocklist(self) -> list[str]:
        if not self._provider_backoff:
            return []
        now = time.perf_counter()
        with self._provider_lock:
            expired = [
                name for name, until in self._provider_backoff.items() if until <= now
            ]
            for name in expired:
                self._provider_backoff.pop(name, None)
                self._provider_timeout_streak.pop(name, None)
            return [
                name for name, until in self._provider_backoff.items() if until > now
            ]

    # ------------------------------------------------------------------
    def _allowed_providers(
        self, aggregator: DexAggregator, local_blocklist: set[str] | None = None
    ) -> list[str] | None:
        blocked = set(self._slow_provider_blocklist())
        if local_blocklist:
            with self._provider_lock:
                blocked.update(
                    {b.strip().lower() for b in local_blocklist if b.strip()}
                )
        if not blocked:
            return None
        names = [p.name for p in getattr(aggregator, "providers", [])]
        allowed = [name for name in names if name.strip().lower() not in blocked]
        return allowed or None

    # ------------------------------------------------------------------
    @contextmanager
    def _aggregator_budget(self, aggregator: DexAggregator, timeout: float | None):
        """Temporarily tighten aggregator timeouts to honor remaining budget."""

        if timeout is None or timeout <= 0:
            yield
            return
        prev_agg = getattr(aggregator, "_aggregate_timeout", None)
        prev_provider = getattr(aggregator, "_timeout", None)
        try:
            if prev_agg is not None:
                aggregator._aggregate_timeout = min(prev_agg, timeout)
            else:
                aggregator._aggregate_timeout = timeout
            if prev_provider is not None and prev_provider > timeout:
                aggregator._timeout = max(0.05, timeout)
            yield
        finally:
            if prev_agg is not None:
                aggregator._aggregate_timeout = prev_agg
            if prev_provider is not None:
                aggregator._timeout = prev_provider

    # ------------------------------------------------------------------
    def _build_guardrails(self, mode: QuoteMode) -> GuardrailContext:
        import os

        max_pool_take = os.getenv("RISK_MAX_POOL_TAKE_BPS")
        max_pool_take_bps: float | None = None
        if max_pool_take and max_pool_take.strip():
            try:
                max_pool_take_bps = float(max_pool_take)
            except Exception:
                max_pool_take_bps = None
        fetch_mode = "live" if mode == QuoteMode.LIVE else "dry"
        if mode == QuoteMode.PROGRESSIVE:
            fetch_mode = "progressive"
        vol_bps = os.getenv("REFLECTION_VOL_BPS_THRESHOLD")
        volatility: float | None = None
        if vol_bps and vol_bps.strip():
            try:
                volatility = float(vol_bps)
            except Exception:
                volatility = None
        utilization_env = os.getenv("MARKETDATA_UTILIZATION_HINT")
        utilization: float | None = None
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
            progressive_mode=config.progressive_live
            and request.mode != QuoteMode.DRY_RUN,
            progressive_cycle=request.progressive_cycle,
            progressive_min_cycles=config.progressive_min_cycles,
            liquidity_floor_usd=liquidity_floor_usd,
            stale_route_penalty_bps=stale_penalty_bps,
        )

    # ------------------------------------------------------------------
    def _price_map(self, config: EnvConfig) -> dict[str, float]:
        fetcher = self._external_price_fetcher
        if fetcher is None:
            return {}
        mapping: dict[str, float] = {}
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
    def _symbol_for_address(config: EnvConfig, address: str) -> str | None:
        norm = (address or "").strip().lower()
        if not norm:
            return None
        mapping: dict[str, str] = {}
        if config.diem_token:
            mapping[config.diem_token.strip().lower()] = "DIEM"
        if config.vvv_token:
            mapping[config.vvv_token.strip().lower()] = "VVV"
        if config.quote_token:
            mapping[config.quote_token.strip().lower()] = os.getenv(
                "QUOTE_TOKEN_SYMBOL", "QUOTE"
            ).upper()
        if config.bridge_token:
            mapping[config.bridge_token.strip().lower()] = os.getenv(
                "BRIDGE_TOKEN_SYMBOL", "ETH"
            ).upper()
        return mapping.get(norm)

    # ------------------------------------------------------------------
    def _discover_routes(
        self,
        token_in: str,
        token_out: str,
        config: EnvConfig,
    ) -> list[RouteEvaluation]:
        cache_key = self._route_cache_key(token_in, token_out, config)
        ttl = self._route_cache_ttl_seconds()
        now = time.time()
        with self._route_cache_lock:
            cached = self._route_cache.get(cache_key)
            if cached:
                ts = float(cached.get("ts") or 0.0)
                if (now - ts) < ttl:
                    cached_candidates: Sequence[RouteCandidate] = cached.get(
                        "candidates", ()
                    )
                    return [
                        RouteEvaluation(candidate=candidate)
                        for candidate in cached_candidates
                    ]

        try:
            routes_from_db: Sequence[RoutePlan] = pool_svc.suggest_routes_for_tokens(
                token_in, token_out
            )
        except Exception:
            routes_from_db = ()
        discovery_ctx = DiscoveryContext(routes_from_db=routes_from_db)
        candidates = discover_routes(
            token_in, token_out, config, discovery=discovery_ctx
        )
        # Filter routes by max hops (respect DIEM_MAX_ROUTE_HOPS)
        max_hops = 2  # Default to 2-hop routes
        try:
            max_hops = int(os.getenv("DIEM_MAX_ROUTE_HOPS", "2") or 2)
            max_hops = max(2, min(max_hops, 4))  # Clamp between 2 and 4
        except Exception:
            pass
        filtered_candidates = []
        for candidate in candidates:
            try:
                route_hops = len(getattr(candidate.route, "hops", ()))
                if route_hops <= max_hops:
                    filtered_candidates.append(candidate)
            except Exception:
                # If we can't determine hop count, include it (conservative)
                filtered_candidates.append(candidate)
        candidates = filtered_candidates
        with self._route_cache_lock:
            self._route_cache[cache_key] = {"ts": now, "candidates": list(candidates)}
        return [RouteEvaluation(candidate=candidate) for candidate in candidates]

    # ------------------------------------------------------------------
    def quote(self, request: QuoteRequest) -> QuoteResult | None:
        config = load_env_config()
        guardrails = self._build_guardrails(request.mode)
        policy = self._build_policy(config, request)
        price_map = self._price_map(config)
        soft_timeout = self._soft_timeout_seconds()
        if self._is_diem_pair(request.token_in, request.token_out, config):
            soft_timeout = max(soft_timeout or 0.0, 20.0)
        soft_margin = self._soft_timeout_margin_seconds()
        start_ts = time.perf_counter()
        cutoff: float | None = None
        if soft_timeout is not None:
            cutoff = soft_timeout - soft_margin
            if cutoff <= 0:
                cutoff = soft_timeout * 0.5
        evaluations: list[RouteEvaluation] = []
        candidate_evaluations = self._discover_routes(
            request.token_in, request.token_out, config
        )
        time_budget_exhausted = False
        session_blocklist: set[str] = set()
        candidate_queue: deque[RouteEvaluation] = deque(candidate_evaluations)
        queue_lock = Lock()

        # Build aggregators for worker pool (reuse primary for first worker)
        aggregator = self._get_aggregator()
        worker_count = self._route_worker_count(len(candidate_evaluations))
        aggregators: list[DexAggregator] = [aggregator]
        for _ in range(max(0, worker_count - 1)):
            try:
                aggregators.append(build_aggregator_from_env())
            except Exception:
                break
        worker_count = max(1, min(worker_count, len(aggregators)))

        def _next_candidate() -> RouteEvaluation | None:
            with queue_lock:
                if time_budget_exhausted or not candidate_queue:
                    return None
                return candidate_queue.popleft()

        def _mark_budget_exhausted() -> None:
            nonlocal time_budget_exhausted
            with queue_lock:
                time_budget_exhausted = True

        def _evaluate_route(
            agg: DexAggregator, evaluation: RouteEvaluation
        ) -> RouteEvaluation | None:
            elapsed = time.perf_counter() - start_ts
            remaining: float | None = None
            if cutoff is not None:
                remaining = cutoff - elapsed
                if remaining <= 0 or remaining <= self._min_route_budget_seconds():
                    _mark_budget_exhausted()
                    return None
            allowed_providers = self._allowed_providers(agg, session_blocklist)
            with self._aggregator_budget(agg, remaining):
                enriched = enrich_route(
                    request,
                    evaluation.candidate,
                    aggregator=agg,
                    price_map=price_map,
                    allowed_providers=allowed_providers,
                )
            enriched.guardrail_penalty = 0.0
            enriched.policy_penalty = 0.0
            enriched.score = float("inf")
            diag_ctx = getattr(agg, "_last_quote_context", None)
            if isinstance(diag_ctx, dict):
                tokens_ctx = diag_ctx.get("tokens")
                if tokens_ctx == list(evaluation.candidate.route.tokens):
                    self._record_provider_diagnostics(
                        getattr(agg, "_last_quote_diagnostics", None),
                        local_blocklist=session_blocklist,
                    )
            if enriched.valid_quote():
                quoted_price = enriched.price()
                if quoted_price is not None:
                    valid, reason = validate_diem_route_price(
                        enriched.candidate.route, quoted_price
                    )
                    if not valid:
                        enriched.errors.append(f"bridge_validation_failed:{reason}")
                        enriched.quote = None
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
            return enriched

        def _worker(agg: DexAggregator) -> None:
            while True:
                evaluation = _next_candidate()
                if evaluation is None:
                    return
                enriched = _evaluate_route(agg, evaluation)
                if enriched is None:
                    continue
                with queue_lock:
                    evaluations.append(enriched)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for agg in aggregators[:worker_count]:
                executor.submit(_worker, agg)

        best_eval: RouteEvaluation | None = None
        for evaluation in evaluations:
            if not evaluation.valid_quote():
                continue
            if best_eval is None or (evaluation.score or float("inf")) < (
                best_eval.score or float("inf")
            ):
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
            if time_budget_exhausted and isinstance(result.metadata, dict):
                result.metadata.setdefault("time_budget_exhausted", True)
            self._record(request, result, evaluations)
            return result

        # Fallback: bridge path for DIEM price
        fallback_result: QuoteResult | None = None
        token_in_l = request.token_in.lower()
        token_out_l = request.token_out.lower()
        diem_norm = (config.diem_token or "").strip().lower()
        quote_norm = (config.quote_token or "").strip().lower()
        is_diem_pair = bool(diem_norm) and (
            token_in_l == diem_norm or token_out_l == diem_norm
        )

        if (
            is_diem_pair
            and quote_norm
            and token_in_l == diem_norm
            and token_out_l == quote_norm
        ):
            fallback_result = bridge_fallback(
                amount_in=request.amount_in_wei,
                config=config,
                guardrails=guardrails,
                policy=policy,
                mode=request.mode,
            )
            if fallback_result and isinstance(fallback_result.metadata, dict):
                fallback_result.metadata.setdefault(
                    "fallback_reason", "diem_bridge_quote"
                )

        if fallback_result is None and is_diem_pair and self._external_price_fetcher:
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
            if fallback_result and isinstance(fallback_result.metadata, dict):
                fallback_result.metadata.setdefault(
                    "fallback_reason", "no_onchain_liquidity"
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
        result: QuoteResult | None,
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
        sanitized_payload = _sanitize_for_json(payload)
        try:
            self._memory.record_cycle(sanitized_payload)
        except Exception:
            # Memory logging should never raise to callers
            pass


__all__ = ["PathQuoteEngine", "QuoteMode", "QuoteRequest", "QuoteResult"]
