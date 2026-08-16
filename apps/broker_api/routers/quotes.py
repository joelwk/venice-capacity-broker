from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from .. import marketdata
from ..models import QuoteResponse
from ..public_rate_limit import enforce_public_rate_limit

try:
    # Optional helper to bootstrap tables if migrations weren't run yet.
    from db.session import create_all_unconditional  # type: ignore
except Exception:  # pragma: no cover - helper may be unavailable in older builds
    create_all_unconditional = None  # type: ignore

router = APIRouter()

_pricing: object | None = None
_logger: logging.Logger
_quotes_enabled: bool = True
_quotes_persist_enabled: bool
_quotes_async_enabled: bool
_quote_results: dict[str, dict]
_quote_results_lock: Lock


@contextmanager
def _otel_span(name: str, attrs: dict[str, Any] | None = None):
    try:
        from opentelemetry import trace  # type: ignore

        tracer = trace.get_tracer(__name__)
        with tracer.start_as_current_span(name) as span:
            if attrs:
                for key, value in attrs.items():
                    try:
                        span.set_attribute(key, value)
                    except Exception:
                        pass
            yield span
            return
    except Exception:
        pass
    with nullcontext():
        yield None


def init_router(
    *,
    pricing_service: object,
    logger: logging.Logger,
    quotes_enabled: bool,
    quotes_persist_enabled: bool,
    quotes_async_enabled: bool,
) -> APIRouter:
    global \
        _pricing, \
        _logger, \
        _quotes_enabled, \
        _quotes_persist_enabled, \
        _quotes_async_enabled
    global _quote_results, _quote_results_lock

    _quotes_enabled = bool(quotes_enabled)
    if not _quotes_enabled:
        return router

    _pricing = pricing_service
    _logger = logger
    _quotes_persist_enabled = quotes_persist_enabled
    _quotes_async_enabled = quotes_async_enabled
    _quote_results = {}
    _quote_results_lock = Lock()

    QuoteResponse.model_rebuild()

    return router


_DEX_DIAGNOSTICS_PATH = Path(
    os.getenv("DEX_DIAGNOSTICS_LOG") or "logs/dex_diagnostics.jsonl"
)


def _recent_dex_diagnostics(limit: int = 3) -> list[dict[str, Any]]:
    if not _DEX_DIAGNOSTICS_PATH.exists():
        return []
    buffer: deque[dict[str, Any]] = deque(maxlen=max(1, limit))
    try:
        with _DEX_DIAGNOSTICS_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                buffer.append(entry)
    except Exception:
        return []
    return list(buffer)


def _diem_price_snap() -> dict[str, Any]:
    provider = marketdata.get_marketdata_provider()
    price_map = provider.prices(["DIEM"])
    price = float(price_map.get("DIEM", 0.0) or 0.0)
    health: dict[str, Any] = {}
    try:
        health = provider.price_health("DIEM", max_age=180.0)
    except Exception:
        health = {}
    diagnostics = _recent_dex_diagnostics()
    return {
        "symbol": "DIEM",
        "priceUsd": price,
        "health": health,
        "diagnostics": diagnostics,
        "timestampMs": int(time.time() * 1000),
    }


def _merge_with_fallback(detail: object, fallback: dict[str, Any]) -> dict[str, Any]:
    if isinstance(detail, dict):
        payload = dict(detail)
    else:
        payload = {"message": str(detail)}
    payload["fallbackPrice"] = fallback
    return payload


def _quote_error_detail(detail_value: object) -> dict[str, Any]:
    fallback = _diem_price_snap()
    try:
        price = float(fallback.get("priceUsd") or 0.0)
        _logger.info("quote fallback price known: DIEM=$%.2f", price)
    except Exception:
        _logger.info("quote fallback price recorded")
    return _merge_with_fallback(detail_value, fallback)


def _persist_quote_async(payload: dict) -> None:
    if _pricing is None:
        return
    try:
        getattr(_pricing, "persist_quote", lambda *_: None)(payload)
    except Exception as exc:
        # Best-effort bootstrap on missing tables, then retry once.
        msg = f"{type(exc).__name__}: {exc}"
        if (
            "UndefinedTable" in msg or 'relation "quote" does not exist' in msg.lower()
        ) and callable(create_all_unconditional):
            try:
                create_all_unconditional()  # type: ignore[misc]
                getattr(_pricing, "persist_quote", lambda *_: None)(payload)
                return
            except Exception:
                pass
        _logger.warning(
            "quote persist async failed quoteId=%s error=%s",
            payload.get("quoteId"),
            exc,
        )


def _snapshot_quote(payload: dict) -> dict:
    snap = dict(payload)
    qid = snap.get("quoteId")
    if qid:
        with _quote_results_lock:
            _quote_results[qid] = snap
    return snap


@router.get(
    "/v1/quotes",
    response_model=QuoteResponse,
    dependencies=[Depends(enforce_public_rate_limit)],
)
def get_quote(
    background_tasks: BackgroundTasks,
    units: float | None = Query(default=None, gt=0),
    asset: str = Query(..., description="ETH, USDC, or WBTC"),
    budget: float | None = Query(default=None, gt=0, description="Budget in USD"),
) -> JSONResponse:
    if not _quotes_enabled:
        raise HTTPException(status_code=404, detail="quotes disabled")
    if _pricing is None:
        raise HTTPException(status_code=503, detail="pricing unavailable")
    if units is None and budget is None:
        raise HTTPException(status_code=400, detail="specify units or budget")
    if units is not None and budget is not None:
        raise HTTPException(
            status_code=400, detail="provide either units or budget, not both"
        )

    # Never quote an asset whose payment cannot be verified on-chain later.
    from .purchases import payment_asset_supported

    if not payment_asset_supported(asset):
        raise HTTPException(
            status_code=400,
            detail=f"asset {asset} not accepted: payment token address not configured",
        )

    from services.broker.inventory import IntakePausedError, assert_intake_open

    try:
        assert_intake_open()
    except IntakePausedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    with _otel_span(
        "broker.quote.generate",
        {"units": units or 0, "asset": asset, "budget": budget or 0},
    ) as span:
        # Retry logic for warmup errors
        max_retries = 2
        retry_delay = 0.5  # seconds
        last_exception = None

        for attempt in range(max_retries + 1):
            try:
                quote_payload = _pricing.get_quote(
                    units=units, asset=asset, budget_usd=budget
                )
                # Success - break out of retry loop
                break
            except RuntimeError as exc:
                error_msg = str(exc).lower()
                # Check if this is a warm-up error that might benefit from retry
                if (
                    "warming up" in error_msg or "market data unavailable" in error_msg
                ) and attempt < max_retries:
                    last_exception = exc
                    _logger.info(
                        "Quote request warmup retry %d/%d: %s",
                        attempt + 1,
                        max_retries + 1,
                        exc,
                    )
                    import time

                    time.sleep(retry_delay * (attempt + 1))  # Exponential backoff
                    continue
                # Not retryable or out of retries - raise immediately
                if "warming up" in error_msg or "market data unavailable" in error_msg:
                    raise HTTPException(
                        status_code=503,
                        detail=_quote_error_detail(str(exc)),
                    ) from exc
                raise HTTPException(
                    status_code=400,
                    detail=_quote_error_detail(str(exc)),
                ) from exc
            except Exception as exc:
                # Non-retryable errors
                raise HTTPException(
                    status_code=400,
                    detail=_quote_error_detail(str(exc)),
                ) from exc
        else:
            # All retries exhausted
            if last_exception:
                raise HTTPException(
                    status_code=503,
                    detail=_quote_error_detail(
                        f"market data unavailable after {max_retries + 1} attempts: {last_exception}"
                    ),
                ) from last_exception
            raise HTTPException(
                status_code=503,
                detail=_quote_error_detail("quote generation failed"),
            )

        if span is not None:
            try:
                span.set_attribute("quote_id", quote_payload.get("quoteId"))
            except Exception:
                pass

        if not _quotes_persist_enabled:
            return _create_no_cache_response(quote_payload)

        snapshot = _snapshot_quote(quote_payload)
        if _quotes_async_enabled and background_tasks is not None:
            background_tasks.add_task(_persist_quote_async, snapshot)
            return _create_no_cache_response(snapshot)

        try:
            _pricing.persist_quote(quote_payload)
        except Exception as exc:
            _logger.warning(
                "quote persist sync failed quoteId=%s error=%s",
                quote_payload.get("quoteId"),
                exc,
            )
        return _create_no_cache_response(quote_payload)


def _create_no_cache_response(content: dict) -> JSONResponse:
    """Create a JSONResponse with aggressive no-cache headers."""
    return JSONResponse(
        content=content,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


__all__ = ["init_router", "router"]
