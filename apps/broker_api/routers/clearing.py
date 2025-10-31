from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..models import ClearingPriceResponse

router = APIRouter(prefix="/v1/pricing", tags=["pricing"])

_compute_clearing_price: Callable[[], dict]
_clearing_enabled: bool
_clearing_sse_interval: float
_logger: Any


def init_router(
    *,
    compute_clearing_price: Callable[[], dict],
    clearing_enabled: bool,
    clearing_sse_interval: float,
    logger: Any,
) -> APIRouter:
    global _compute_clearing_price, _clearing_enabled, _clearing_sse_interval, _logger
    _compute_clearing_price = compute_clearing_price
    _clearing_enabled = clearing_enabled
    _clearing_sse_interval = clearing_sse_interval
    _logger = logger
    return router


@router.get("/clearing_price", response_model=ClearingPriceResponse)
def clearing_price() -> dict:
    """Get current clearing price snapshot."""
    if not _clearing_enabled:
        raise HTTPException(status_code=404, detail="clearing price disabled")
    return _compute_clearing_price()


@router.get("/clearing_price/stream")
def clearing_price_stream() -> StreamingResponse:
    """Stream clearing price updates via SSE."""
    if not _clearing_enabled:
        raise HTTPException(status_code=404, detail="clearing price disabled")
    
    def _gen():
        last_heartbeat = time.time()
        while True:
            try:
                payload = _compute_clearing_price()
                import json as _json
                yield f"data: {_json.dumps(payload)}\n\n"
                
                # Send heartbeat comment every 30 seconds
                now = time.time()
                if now - last_heartbeat >= 30.0:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now
            except Exception as _e:  # noqa: BLE001
                yield f"event: error\n" f"data: {str(_e)}\n\n"
            time.sleep(max(1.0, float(_clearing_sse_interval)))

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


__all__ = ["router", "init_router"]

