from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Sequence

from libs.dex.routes import RoutePlan


class QuoteMode(str, Enum):
    """Execution mode for the quoting engine."""

    DRY_RUN = "dry-run"
    PROGRESSIVE = "progressive-live"
    LIVE = "live"

    @classmethod
    def from_flags(cls, *, enable_live: bool, progressive_live: bool) -> "QuoteMode":
        if enable_live and not progressive_live:
            return cls.LIVE
        if progressive_live:
            return cls.PROGRESSIVE
        return cls.DRY_RUN


@dataclass
class QuoteRequest:
    """Normalized quote request."""

    token_in: str
    token_out: str
    amount_in_wei: int
    mode: QuoteMode = QuoteMode.DRY_RUN
    symbol_label: Optional[str] = None
    tenant_tier: Optional[str] = None
    progressive_cycle: Optional[int] = None


@dataclass
class HopTelemetry:
    """Metadata captured for a single hop in a route."""

    token_in: str
    token_out: str
    pool: Optional[str]
    status: str = "unknown"
    metrics: Dict[str, Any] = field(default_factory=dict)
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "token_in": self.token_in,
            "token_out": self.token_out,
            "pool": self.pool,
            "status": self.status,
        }
        if self.metrics:
            payload["metrics"] = self.metrics
        if self.diagnostics:
            payload["diagnostics"] = self.diagnostics
        if self.errors:
            payload["errors"] = list(self.errors)
        return payload


@dataclass
class RouteCandidate:
    """Route seed prior to enrichment."""

    route: Optional[RoutePlan]
    source: str
    reason: Optional[str] = None

    def tokens(self) -> Sequence[str]:
        return tuple(self.route.tokens)

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "tokens": list(self.route.tokens),
            "source": self.source,
        }
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class RouteEvaluation:
    """Route after enrichment/quoting."""

    candidate: RouteCandidate
    quote: Optional[Dict[str, Any]] = None
    hops: List[HopTelemetry] = field(default_factory=list)
    guardrail_penalty: float = 0.0
    policy_penalty: float = 0.0
    score: Optional[float] = None
    errors: List[str] = field(default_factory=list)

    def valid_quote(self) -> bool:
        if not isinstance(self.quote, dict):
            return False
        try:
            amount_in = float(self.quote.get("amount_in") or 0.0)
            amount_out = float(self.quote.get("amount_out") or 0.0)
            return amount_in > 0 and amount_out > 0
        except Exception:
            return False

    def amount_out(self) -> Optional[int]:
        if not self.valid_quote():
            return None
        try:
            return int(self.quote["amount_out"])
        except Exception:
            return None

    def amount_in(self) -> Optional[int]:
        if not self.valid_quote():
            return None
        try:
            return int(self.quote["amount_in"])
        except Exception:
            return None

    def provider(self) -> Optional[str]:
        if not isinstance(self.quote, dict):
            return None
        provider = self.quote.get("provider")
        return str(provider) if provider is not None else None

    def price(self) -> Optional[float]:
        if not isinstance(self.quote, dict):
            return None
        try:
            value = self.quote.get("price")
            return float(value) if value is not None else None
        except Exception:
            return None

    def path_tokens(self) -> Iterable[str]:
        if isinstance(self.quote, dict) and isinstance(self.quote.get("path"), (list, tuple)):
            return [str(tok) for tok in self.quote["path"]]
        return self.candidate.route.tokens

    def as_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "candidate": self.candidate.as_dict(),
            "guardrail_penalty": self.guardrail_penalty,
            "policy_penalty": self.policy_penalty,
            "score": self.score,
            "valid": self.valid_quote(),
        }
        if self.quote:
            data["quote"] = self.quote
        if self.hops:
            data["hops"] = [hop.as_dict() for hop in self.hops]
        if self.errors:
            data["errors"] = list(self.errors)
        return data


@dataclass
class GuardrailContext:
    fetch_mode: str = "auto"
    max_pool_take_bps: Optional[float] = None
    volatility_bps: Optional[float] = None
    utilization: Optional[float] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "fetch_mode": self.fetch_mode,
            "max_pool_take_bps": self.max_pool_take_bps,
            "volatility_bps": self.volatility_bps,
            "utilization": self.utilization,
        }


@dataclass
class PolicyContext:
    tenant_tier: Optional[str] = None
    progressive_mode: bool = False
    progressive_cycle: Optional[int] = None
    progressive_min_cycles: Optional[int] = None
    liquidity_floor_usd: Optional[float] = None
    stale_route_penalty_bps: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tenant_tier": self.tenant_tier,
            "progressive_mode": self.progressive_mode,
            "progressive_cycle": self.progressive_cycle,
            "progressive_min_cycles": self.progressive_min_cycles,
            "liquidity_floor_usd": self.liquidity_floor_usd,
            "stale_route_penalty_bps": self.stale_route_penalty_bps,
        }


@dataclass
class QuoteResult:
    """Final quote with context and scoring metadata."""

    amount_in: int
    amount_out: int
    price: float
    provider: str
    route: RoutePlan
    score: float
    guardrails: GuardrailContext
    policy: PolicyContext
    mode: QuoteMode
    source: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload = {
            "amount_in": self.amount_in,
            "amount_out": self.amount_out,
            "price": self.price,
            "provider": self.provider,
            "route": list(self.route.tokens) if self.route else None,
            "score": self.score,
            "mode": self.mode.value,
            "source": self.source,
            "guardrails": self.guardrails.snapshot(),
            "policy": self.policy.as_dict(),
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload
