from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from ..models import BidRequest, BidResponse

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
    global _recover_buyer, _price_usdc_per_unit_from_asset, _classify_bid_status, _logger
    
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


@router.post("", response_model=BidResponse)
def bids_create(req: BidRequest) -> dict:
    """Create a new bid with signature verification and idempotency."""
    if not _bids_enabled:
        raise HTTPException(status_code=404, detail="bids disabled")
    
    # Verify signature and fields
    buyer_rx = _recover_buyer(req)
    if buyer_rx.lower() != str(req.buyer or "").lower():
        raise HTTPException(status_code=400, detail="buyer/signature mismatch")
    
    # Idempotency on (buyer, nonce)
    if not _has_sql_bids:
        raise HTTPException(status_code=503, detail="SQL dependencies unavailable")
    
    import hashlib as _hh2
    import json as _json2
    from datetime import datetime as _dt
    
    now_s = int(time.time())
    with next(_get_sess()) as s:  # type: ignore[call-arg]
        exists = s.exec(
            _sel(_DbBid).where((_DbBid.buyer_address == buyer_rx) & (_DbBid.nonce == int(req.nonce)))  # type: ignore[misc]
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
                    and int(exists.expiry.timestamp()) == int(req.expiry)
                )
            except Exception:
                same = False
            if not same:
                raise HTTPException(status_code=409, detail="nonce replay with different payload")
            return {"bidId": exists.bid_id, "status": exists.status}
        
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
            expiry=_dt.utcfromtimestamp(int(req.expiry)),
            slippage_bps=int(req.slippageBps),
            nonce=int(req.nonce),
            status=status,
            context=ctx_json,
        )
        s.add(b)
        s.commit()
        return {"bidId": bid_id, "status": status}


@router.get("", response_model=list[dict])
def bids_list(buyer: str = Query(..., description="buyer wallet address")) -> list[dict]:
    """List bids for a given buyer."""
    if not _bids_enabled:
        raise HTTPException(status_code=404, detail="bids disabled")
    if not _has_sql_bids:
        raise HTTPException(status_code=503, detail="SQL dependencies unavailable")
    
    with next(_get_sess()) as s:  # type: ignore[call-arg]
        rows = s.exec(_sel(_DbBid).where(_DbBid.buyer_address == buyer).order_by(_DbBid.created_at.desc()).limit(50)).all()  # type: ignore[misc]
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    "bidId": r.bid_id,
                    "status": r.status,
                    "units": float(r.units),
                    "maxPrice": int(r.max_price),
                    "asset": r.asset,
                    "expiry": int(r.expiry.timestamp()) if r.expiry else None,
                    "slippageBps": int(r.slippage_bps),
                    "nonce": int(r.nonce),
                    "createdAt": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
                }
            )
        return out


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
        return {
            "bidId": r.bid_id,
            "status": r.status,
            "units": float(r.units),
            "maxPrice": int(r.max_price),
            "asset": r.asset,
            "expiry": int(r.expiry.timestamp()) if r.expiry else None,
            "slippageBps": int(r.slippage_bps),
            "nonce": int(r.nonce),
            "createdAt": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
            "updatedAt": r.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.updated_at else None,
            "context": r.context,
        }


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
                            yield "event: error\n" + "data: bid not found after multiple attempts, terminating stream\n\n"
                            break
                        time.sleep(3)
                        continue
                    # Reset counter when bid is found
                    not_found_count = 0
                    max_usdc = _price_usdc_per_unit_from_asset(int(r.max_price), str(r.asset))
                    status, ctx = _classify_bid_status(max_usdc, now_s, int(r.expiry.timestamp()) if r.expiry else now_s)
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
                        yield f"data: {_json4.dumps({'bidId': r.bid_id, 'status': r.status, 'context': ctx})}\n\n"
                        last_status = status
            except Exception as _e:  # noqa: BLE001
                yield f"event: error\n" f"data: {str(_e)}\n\n"
            time.sleep(5)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


__all__ = ["router", "init_router"]

