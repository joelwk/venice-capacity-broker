from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Header, HTTPException, Query

router = APIRouter(prefix="/v1/admin", tags=["admin"])
debug_router = APIRouter(prefix="/v1/debug", tags=["debug"])

_require_admin: Callable[[Optional[str]], None]
_has_sqlmodel: bool
_get_sess: Any
_select_all: Any
_Q: Any  # Quote model
_P: Any  # Purchase model
_C: Any  # Counter model
_logger: Any


def init_router(
    *,
    require_admin: Callable[[Optional[str]], None],
    has_sqlmodel: bool,
    get_sess: Any,
    select_all: Any,
    quote_model: Any,
    purchase_model: Any,
    counter_model: Any,
    logger: Any,
) -> tuple[APIRouter, APIRouter]:
    global _require_admin, _has_sqlmodel, _get_sess, _select_all, _Q, _P, _C, _logger
    _require_admin = require_admin
    _has_sqlmodel = has_sqlmodel
    _get_sess = get_sess
    _select_all = select_all
    _Q = quote_model
    _P = purchase_model
    _C = counter_model
    _logger = logger
    return router, debug_router


@router.get("/quotes")
def admin_quotes(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> list[dict]:
    """List quotes with optional status filter."""
    _require_admin(authorization)
    if not _has_sqlmodel:
        return []
    with next(_get_sess()) as s:  # type: ignore[call-arg]
        stmt = _select_all(_Q).order_by(_Q.created_at.desc()).limit(int(limit))  # type: ignore[misc]
        rows = s.exec(stmt).all()
        out = []
        for r in rows:
            if status and r.status != status:
                continue
            out.append({
                "quoteId": r.quote_id,
                "units": float(r.units),
                "asset": r.asset,
                "unitPrice": int(r.unit_price),
                "totalPrice": int(r.total_price),
                "status": r.status,
                "expiresAt": r.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.expires_at else None,
                "createdAt": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
            })
        return out


@router.get("/purchases")
def admin_purchases(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> list[dict]:
    """List purchases with optional status filter."""
    _require_admin(authorization)
    if not _has_sqlmodel:
        return []
    with next(_get_sess()) as s:  # type: ignore[call-arg]
        stmt = _select_all(_P).order_by(_P.created_at.desc()).limit(int(limit))  # type: ignore[misc]
        rows = s.exec(stmt).all()
        out = []
        for r in rows:
            if status and r.status != status:
                continue
            out.append({
                "purchaseId": r.purchase_id,
                "quoteId": r.quote_id,
                "buyer": r.buyer_address,
                "asset": r.asset,
                "amountPaid": int(r.amount_paid),
                "txHash": r.tx_hash,
                "status": r.status,
                "tenantId": r.tenant_id,
                "expiresAt": r.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.expires_at else None,
                "createdAt": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
                "fulfilledAt": r.fulfilled_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.fulfilled_at else None,
            })
        return out


@router.get("/utilization")
def admin_utilization(
    minutes: int = Query(default=1440, ge=1, le=10080),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    """Get utilization stats for recent time window."""
    _require_admin(authorization)
    if not _has_sqlmodel:
        return {"minutes": int(minutes), "total": 0}
    try:
        from sqlmodel import select as _sel
        from datetime import datetime as __dt, timedelta as __td
        start = __dt.utcnow() - __td(minutes=int(minutes))
        used = 0
        with next(_get_sess()) as s:  # type: ignore[call-arg]
            rows = s.exec(_sel(_C).where(_C.bucket_start >= start)).all()
            used = sum(int(r.count or 0) for r in rows)
        return {"minutes": int(minutes), "total": int(used)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/counters")
def admin_counters(
    tenant_id: str = Query(..., description="Tenant ID to filter counters"),
    scope: str | None = Query(default=None, description="Optional scope filter (e.g., 'chat', 'signals')"),
    bucket_seconds: str | None = Query(default=None, description="Optional bucket size filter"),
    limit: int = Query(default=100, ge=1, le=1000),
    asc: bool = Query(default=False, description="Sort ascending by bucket_start"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> list[dict]:
    """List counter records for a tenant with optional filters."""
    _require_admin(authorization)
    if not _has_sqlmodel:
        return []
    bucket_seconds_value: int | None
    try:
        bucket_seconds_value = None if bucket_seconds is None else int(str(bucket_seconds).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bucket_seconds must be an integer")
    try:
        from sqlmodel import select as _sel
        from sqlalchemy import desc as _desc
        stmt = _sel(_C).where(_C.tenant_id == tenant_id)  # type: ignore[misc]
        if scope:
            stmt = stmt.where(_C.scope == scope)  # type: ignore[misc]
        if bucket_seconds_value is not None:
            stmt = stmt.where(_C.bucket_seconds == bucket_seconds_value)  # type: ignore[misc]
        if asc:
            stmt = stmt.order_by(_C.bucket_start)  # type: ignore[misc]
        else:
            stmt = stmt.order_by(_desc(_C.bucket_start))  # type: ignore[misc]
        stmt = stmt.limit(int(limit))
        with next(_get_sess()) as s:  # type: ignore[call-arg]
            rows = s.exec(stmt).all()
            return [
                {
                    "tenant_id": r.tenant_id,
                    "scope": r.scope,
                    "model": r.model,
                    "bucket_start": r.bucket_start.isoformat() if r.bucket_start else None,
                    "bucket_seconds": r.bucket_seconds,
                    "count": r.count,
                }
                for r in rows
            ]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@debug_router.get("/counters")
def debug_counters(
    tenant_id: str = Query(..., description="Tenant ID to filter counters"),
    scope: str | None = Query(default=None, description="Optional scope filter (e.g., 'chat', 'signals')"),
    bucket_seconds: str | None = Query(default=None, description="Optional bucket size filter"),
    limit: int = Query(default=100, ge=1, le=1000),
    asc: bool = Query(default=False, description="Sort ascending by bucket_start"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> list[dict]:
    """List counter records for a tenant with optional filters (debug endpoint)."""
    _require_admin(authorization)
    if not _has_sqlmodel:
        return []
    bucket_seconds_value: int | None
    try:
        bucket_seconds_value = None if bucket_seconds is None else int(str(bucket_seconds).strip())
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="bucket_seconds must be an integer")
    try:
        from sqlmodel import select as _sel
        from sqlalchemy import desc as _desc
        stmt = _sel(_C).where(_C.tenant_id == tenant_id)  # type: ignore[misc]
        if scope:
            stmt = stmt.where(_C.scope == scope)  # type: ignore[misc]
        if bucket_seconds_value is not None:
            stmt = stmt.where(_C.bucket_seconds == bucket_seconds_value)  # type: ignore[misc]
        if asc:
            stmt = stmt.order_by(_C.bucket_start)  # type: ignore[misc]
        else:
            stmt = stmt.order_by(_desc(_C.bucket_start))  # type: ignore[misc]
        stmt = stmt.limit(int(limit))
        with next(_get_sess()) as s:  # type: ignore[call-arg]
            rows = s.exec(stmt).all()
            return [
                {
                    "tenant_id": r.tenant_id,
                    "scope": r.scope,
                    "model": r.model,
                    "bucket_start": r.bucket_start.isoformat() if r.bucket_start else None,
                    "bucket_seconds": r.bucket_seconds,
                    "count": r.count,
                }
                for r in rows
            ]
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/venice/probe")
def venice_probe(
    base: str | None = Query(default=None, description="Base host, e.g., https://api.venice.ai"),
    timeout: float = Query(default=10.0, ge=1.0, le=60.0),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> dict:
    """Admin-only: Fetch OpenAPI and recommend VENICE_* exports.

    Tries /openapi.json then /api/openapi.json at the provided base.
    """
    _require_admin(authorization)
    import requests as _rq

    base_url = (base or "https://api.venice.ai").rstrip("/")
    paths_to_try = ["/openapi.json", "/api/openapi.json"]
    spec = None
    for p in paths_to_try:
        try:
            r = _rq.get(f"{base_url}{p}", timeout=float(timeout))
            r.raise_for_status()
            spec = r.json()
            break
        except Exception:
            continue
    if spec is None:
        raise HTTPException(status_code=502, detail="OpenAPI spec not found")

    # Extract path suggestions
    paths = spec.get("paths", {})
    suggestions: dict[str, str] = {}
    
    # Check for common Venice endpoints
    if "/vvv/circulatingsupply" in paths:
        suggestions["VENICE_VVV_CIRC_PATH"] = "/vvv/circulatingsupply"
    if "/vvv/utilization" in paths:
        suggestions["VENICE_VVV_UTIL_PATH"] = "/vvv/utilization"
    if "/vvv/staking_yield" in paths:
        suggestions["VENICE_VVV_YIELD_PATH"] = "/vvv/staking_yield"

    return {
        "base": base_url,
        "openapi_version": spec.get("openapi") or spec.get("swagger"),
        "info": spec.get("info", {}),
        "paths_count": len(paths),
        "suggestions": suggestions,
    }


__all__ = ["router", "debug_router", "init_router"]

