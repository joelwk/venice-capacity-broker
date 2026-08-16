from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..models import BidRequest, BidResponse
from ..services.bids import expiry_as_utc, expiry_epoch

router = APIRouter(prefix="/v1/bids", tags=["bids"])

_bids_enabled: bool
_has_sql_bids: bool
_get_sess: Any
_sel: Any
_DbBid: Any
_recover_buyer: Callable[[BidRequest], str]
_price_usdc_per_unit_from_asset: Callable[[int, str], float]
_classify_bid_status: Callable[[float, int, int], tuple[str, dict]]
_logger: Any


def init_router(
    *,
    bids_enabled: bool,
    has_sql_bids: bool,
    get_sess: Any,
    select_func: Any,
    bid_model: Any,
    recover_buyer: Callable[[BidRequest], str],
    price_usdc_per_unit_from_asset: Callable[[int, str], float],
    classify_bid_status: Callable[[float, int, int], tuple[str, dict]],
    logger: Any,
) -> APIRouter:
    global _bids_enabled, _has_sql_bids, _get_sess, _sel, _DbBid
    global \
        _recover_buyer, \
        _price_usdc_per_unit_from_asset, \
        _classify_bid_status, \
        _logger

    _bids_enabled = bids_enabled
    _has_sql_bids = has_sql_bids
    _get_sess = get_sess
    _sel = select_func
    _DbBid = bid_model
    _recover_buyer = recover_buyer
    _price_usdc_per_unit_from_asset = price_usdc_per_unit_from_asset
    _classify_bid_status = classify_bid_status
    _logger = logger
    return router


def _bid_public_dict(row: Any, *, include_context: bool = False) -> dict:
    payload = {
        "bidId": row.bid_id,
        "status": row.status,
        "units": float(row.units),
        "maxPrice": int(row.max_price),
        "asset": row.asset,
        "expiry": expiry_epoch(row.expiry) if row.expiry else None,
        "slippageBps": int(row.slippage_bps),
        "nonce": int(row.nonce),
        "quoteId": row.quote_id,
        "createdAt": (
            row.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if row.created_at else None
        ),
    }
    if include_context:
        payload["updatedAt"] = (
            row.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if row.updated_at else None
        )
        payload["context"] = row.context
    return payload


@router.post("", response_model=BidResponse)
def bids_create(req: BidRequest) -> dict:
    """Create a new bid with signature verification and idempotency."""
    if not _bids_enabled:
        raise HTTPException(status_code=404, detail="bids disabled")

    from services.broker.inventory import IntakePausedError, assert_intake_open

    try:
        assert_intake_open()
    except IntakePausedError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # Verify signature and fields
    buyer_rx = _recover_buyer(req)
    if buyer_rx.lower() != str(req.buyer or "").lower():
        raise HTTPException(status_code=400, detail="buyer/signature mismatch")

    # Idempotency on (buyer, nonce)
    if not _has_sql_bids:
        raise HTTPException(status_code=503, detail="SQL dependencies unavailable")

    import hashlib as _hh2
    import json as _json2

    now_s = int(time.time())
    with next(_get_sess()) as s:  # type: ignore[call-arg]
        exists = s.exec(
            _sel(_DbBid).where(
                (_DbBid.buyer_address == buyer_rx) & (_DbBid.nonce == int(req.nonce))
            )  # type: ignore[misc]
        ).first()

        # Consistent bidId across retries
        base = f"{buyer_rx}:{int(req.nonce)}:{str(req.asset).upper()}:{int(req.maxPrice)}:{int(req.units)}:{int(req.expiry)}"
        bid_id = _hh2.sha256(base.encode()).hexdigest()[:16]

        if exists is not None:
            # If payload matches, return existing (idempotent). Otherwise, conflict.
            try:
                same = (
                    exists.max_price == int(req.maxPrice)
                    and str(exists.asset).upper() == str(req.asset).upper()
                    and int(round(float(exists.units) * 1_000_000)) == int(req.units)
                    and expiry_epoch(exists.expiry) == int(req.expiry)
                )
            except Exception:
                same = False
            if not same:
                raise HTTPException(
                    status_code=409, detail="nonce replay with different payload"
                )
            return {
                "bidId": exists.bid_id,
                "status": exists.status,
                "quoteId": exists.quote_id,
            }

        # Compute initial status
        max_usdc = _price_usdc_per_unit_from_asset(int(req.maxPrice), str(req.asset))
        status, ctx = _classify_bid_status(max_usdc, now_s, int(req.expiry))
        ctx_json = None
        try:
            ctx_json = _json2.dumps(ctx)
        except Exception:
            ctx_json = None

        b = _DbBid(
            bid_id=bid_id,
            buyer_address=buyer_rx,
            units=float(int(req.units)) / 1_000_000.0,
            max_price=int(req.maxPrice),
            asset=str(req.asset).upper(),
            expiry=expiry_as_utc(int(req.expiry)),
            slippage_bps=int(req.slippageBps),
            nonce=int(req.nonce),
            status=status,
            context=ctx_json,
        )
        s.add(b)
        s.commit()
        return {"bidId": bid_id, "status": status}


@router.get("", response_model=list[dict])
def bids_list(
    buyer: str = Query(..., description="buyer wallet address"),
) -> list[dict]:
    """List bids for a given buyer."""
    if not _bids_enabled:
        raise HTTPException(status_code=404, detail="bids disabled")
    if not _has_sql_bids:
        raise HTTPException(status_code=503, detail="SQL dependencies unavailable")

    with next(_get_sess()) as s:  # type: ignore[call-arg]
        rows = s.exec(
            _sel(_DbBid)
            .where(_DbBid.buyer_address == buyer)
            .order_by(_DbBid.created_at.desc())
            .limit(50)
        ).all()  # type: ignore[misc]
        return [_bid_public_dict(r) for r in rows]


@router.get("/{bid_id}")
def bids_get(bid_id: str) -> dict:
    """Get details for a specific bid."""
    if not _bids_enabled:
        raise HTTPException(status_code=404, detail="bids disabled")
    if not _has_sql_bids:
        raise HTTPException(status_code=503, detail="SQL dependencies unavailable")

    with next(_get_sess()) as s:  # type: ignore[call-arg]
        r = s.exec(_sel(_DbBid).where(_DbBid.bid_id == bid_id)).first()  # type: ignore[misc]
        if r is None:
            raise HTTPException(status_code=404, detail="bid not found")
        return _bid_public_dict(r, include_context=True)


@router.get("/{bid_id}/stream")
def bids_stream(bid_id: str) -> StreamingResponse:
    """Stream bid status updates via SSE."""
    if not _bids_enabled:
        raise HTTPException(status_code=404, detail="bids disabled")

    def _gen():
        from datetime import datetime as _dt

        last_status = None
        last_heartbeat = time.time()
        not_found_count = 0
        MAX_NOT_FOUND_RETRIES = 3
        while True:
            try:
                # Send heartbeat comment every 30 seconds
                now = time.time()
                if now - last_heartbeat >= 30.0:
                    yield ": heartbeat\n\n"
                    last_heartbeat = now

                now_s = int(time.time())
                if not _has_sql_bids:
                    yield "event: error\n" + "data: SQL unavailable\n\n"
                    time.sleep(3)
                    continue
                with next(_get_sess()) as s:  # type: ignore[call-arg]
                    r = s.exec(_sel(_DbBid).where(_DbBid.bid_id == bid_id)).first()  # type: ignore[misc]
                    if r is None:
                        not_found_count += 1
                        yield "event: error\n" + "data: not found\n\n"
                        if not_found_count >= MAX_NOT_FOUND_RETRIES:
                            yield (
                                "event: error\n"
                                "data: bid not found after multiple attempts, terminating stream\n\n"
                            )
                            break
                        time.sleep(3)
                        continue
                    # Reset counter when bid is found
                    not_found_count = 0
                    max_usdc = _price_usdc_per_unit_from_asset(
                        int(r.max_price), str(r.asset)
                    )
                    status, ctx = _classify_bid_status(
                        max_usdc,
                        now_s,
                        expiry_epoch(r.expiry) if r.expiry else now_s,
                    )
                    if status != r.status:
                        r.status = status
                        r.updated_at = _dt.utcnow()
                        try:
                            import json as _json3

                            r.context = _json3.dumps(ctx)
                        except Exception:
                            pass
                        s.add(r)
                        s.commit()
                    if status != last_status:
                        import json as _json4

                        # Avoid backslashes inside f-string expression by using single-quoted keys
                        yield f"data: {_json4.dumps({'bidId': r.bid_id, 'status': r.status, 'quoteId': r.quote_id, 'context': ctx})}\n\n"
                        last_status = status
            except Exception as _e:
                yield f"event: error\ndata: {_e!s}\n\n"
            time.sleep(5)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


__all__ = ["init_router", "router"]
