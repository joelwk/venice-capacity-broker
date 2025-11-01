from __future__ import annotations

import logging
from threading import Lock
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import JSONResponse

from ..models import QuoteResponse

router = APIRouter()

_pricing: Optional[object] = None
_logger: logging.Logger
_quotes_persist_enabled: bool
_quotes_async_enabled: bool
_quote_results: dict[str, dict]
_quote_results_lock: Lock


def init_router(
    *,
    pricing_service: object,
    logger: logging.Logger,
    quotes_enabled: bool,
    quotes_persist_enabled: bool,
    quotes_async_enabled: bool,
) -> APIRouter:
    global _pricing, _logger, _quotes_persist_enabled, _quotes_async_enabled
    global _quote_results, _quote_results_lock

    if not quotes_enabled:
        return router

    _pricing = pricing_service
    _logger = logger
    _quotes_persist_enabled = quotes_persist_enabled
    _quotes_async_enabled = quotes_async_enabled
    _quote_results = {}
    _quote_results_lock = Lock()

    QuoteResponse.model_rebuild()

    return router


def _persist_quote_async(payload: dict) -> None:
    if _pricing is None:
        return
    try:
        getattr(_pricing, "persist_quote", lambda *_: None)(payload)
    except Exception as exc:  # noqa: BLE001
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


@router.get("/v1/quotes", response_model=QuoteResponse)
def get_quote(
    background_tasks: BackgroundTasks,
    units: Optional[float] = Query(default=None, gt=0),
    asset: str = Query(..., description="ETH, USDC, or WBTC"),
    budget: Optional[float] = Query(default=None, gt=0, description="Budget in USD"),
) -> JSONResponse:
    if _pricing is None:
        raise HTTPException(status_code=503, detail="pricing unavailable")
    if units is None and budget is None:
        raise HTTPException(status_code=400, detail="specify units or budget")
    if units is not None and budget is not None:
        raise HTTPException(status_code=400, detail="provide either units or budget, not both")
    try:
        quote_payload = getattr(_pricing, "get_quote")(units=units, asset=asset, budget_usd=budget)
    except RuntimeError as exc:
        # Check if this is a warm-up error
        if "warming up" in str(exc).lower():
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not _quotes_persist_enabled:
        return _create_no_cache_response(quote_payload)

    snapshot = _snapshot_quote(quote_payload)
    if _quotes_async_enabled and background_tasks is not None:
        background_tasks.add_task(_persist_quote_async, snapshot)
        return _create_no_cache_response(snapshot)

    try:
        getattr(_pricing, "persist_quote")(quote_payload)
    except Exception as exc:  # noqa: BLE001
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
        }
    )


__all__ = ["router", "init_router"]
