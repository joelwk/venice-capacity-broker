from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from libs.dex.providers import DexAggregator


def _quote_executable(q: Any) -> bool:
    """Return True when a quote is executable on-chain."""
    try:
        provider = (
            q.get("provider") if isinstance(q, dict) else getattr(q, "provider", "")
        )
        if str(provider).strip().lower() == "composite_analytic":
            return False
        flag = (
            q.get("executable", True)
            if isinstance(q, dict)
            else getattr(q, "executable", True)
        )
        return bool(flag)
    except Exception:
        return True


def summarize_quotes(
    quotes: Iterable[Any],
    *,
    diagnostics: list[dict[str, Any]] | None = None,
    route_tokens: list[str] | None = None,
    aggregator: DexAggregator | None = None,
) -> dict[str, Any]:
    """Summarize quote attempt for logging/guardrails parity with broker UI."""

    quote_list = list(quotes) if quotes is not None else []
    exec_count = sum(1 for q in quote_list if _quote_executable(q))
    status_counts: dict[str, int] = {}
    provider_errors = 0
    revert_errors = 0

    diag_source = diagnostics
    if diag_source is None and aggregator is not None:
        try:
            diag_source = getattr(aggregator, "_last_quote_diagnostics", None)
        except Exception:
            diag_source = None

    if diag_source:
        for entry in diag_source:
            status = str(entry.get("status", "")).strip().lower()
            if not status:
                continue
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "error":
                provider_errors += 1
                if entry.get("revert_reason"):
                    revert_errors += 1

    # Fall back to aggregator context counts if present
    if provider_errors == 0 and aggregator is not None:
        try:
            ctx = getattr(aggregator, "_last_quote_context", {}) or {}
            provider_errors = int(ctx.get("provider_errors") or 0)
            status_ctx = ctx.get("status_counts")
            if isinstance(status_ctx, dict) and not status_counts:
                status_counts = {k: int(v) for k, v in status_ctx.items()}
        except Exception:
            pass

    return {
        "quote_count": len(quote_list),
        "executable_quote_count": exec_count,
        "provider_errors": provider_errors,
        "revert_errors": revert_errors,
        "status_counts": status_counts,
        "route": list(route_tokens) if route_tokens else None,
    }


def last_quote_summary(aggregator: DexAggregator) -> dict[str, Any]:
    """Return the most recent quote context snapshot when available."""
    try:
        ctx = getattr(aggregator, "_last_quote_context", {}) or {}
    except Exception:
        ctx = {}
    summary = {
        "quote_count": ctx.get("quotes_attempted"),
        "executable_quote_count": ctx.get("executable_quotes"),
        "provider_errors": ctx.get("provider_errors"),
        "status_counts": ctx.get("status_counts"),
        "route": ctx.get("route"),
    }
    return {k: v for k, v in summary.items() if v is not None}


__all__ = ["last_quote_summary", "summarize_quotes"]
