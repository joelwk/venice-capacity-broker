from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from libs.telemetry.tracing import annotate_span
from libs.venice_sdk.client import VeniceClient
from services.venice_keys.manager import KeyManager

from ..models import (
    BrokerLimits,
    ChatRequest,
    TenantCreateRequest,
    TenantResponse,
    UsageResponse,
)
from ..tenant_store import Tenant, TenantStore

router = APIRouter()

_store: TenantStore
_keys: KeyManager
_client: VeniceClient
_logger: Any
_require_admin: Callable[[Optional[str]], None]
_auth_context: Callable[[Optional[str]], Tuple[str, Optional[Tenant]]]
_compute_expires_at: Callable[[Optional[float]], Optional[str]]
_extract_field: Callable[[Any, Iterable[str]], str]
_default_quota: int
_kv_admin: Any
_rate_limits_enabled: bool
_rate_limit_window_seconds: int
_rate_limit_max_requests: int
_limiter: Any
_get_rate_limit_headers: Callable[[Optional[str]], Dict[str, str]]


def init_router(
    *,
    store: TenantStore,
    keys: KeyManager,
    client: VeniceClient,
    logger: Any,
    require_admin: Callable[[Optional[str]], None],
    auth_context: Callable[[Optional[str]], Tuple[str, Optional[Tenant]]],
    compute_expires_at: Callable[[Optional[float]], Optional[str]],
    extract_field: Callable[[Any, Iterable[str]], str],
    default_quota: int,
    kv_admin: Any,
    rate_limits_enabled: bool,
    rate_limit_window_seconds: int,
    rate_limit_max_requests: int,
    limiter: Any,
    get_rate_limit_headers: Callable[[Optional[str]], Dict[str, str]],
) -> APIRouter:
    global _store, _keys, _client, _logger, _require_admin, _auth_context
    global _compute_expires_at, _extract_field, _default_quota, _kv_admin
    global _rate_limits_enabled, _rate_limit_window_seconds, _rate_limit_max_requests
    global _limiter, _get_rate_limit_headers

    _store = store
    _keys = keys
    _client = client
    _logger = logger
    _require_admin = require_admin
    _auth_context = auth_context
    _compute_expires_at = compute_expires_at
    _extract_field = extract_field
    _default_quota = default_quota
    _kv_admin = kv_admin
    _rate_limits_enabled = rate_limits_enabled
    _rate_limit_window_seconds = rate_limit_window_seconds
    _rate_limit_max_requests = rate_limit_max_requests
    _limiter = limiter
    _get_rate_limit_headers = get_rate_limit_headers

    return router


def _get_broker_limits_obj(tenant_id: str) -> Dict[str, Any]:
    win_s = _rate_limit_window_seconds
    max_req = _rate_limit_max_requests
    label: Optional[str] = None
    if _kv_admin is not None:
        try:
            raw = _kv_admin.get(f"broker:tenant:{tenant_id}:limits")
            if raw:
                obj = json.loads(raw)
                win_s = int(obj.get("windowSeconds", win_s))
                max_req = int(obj.get("maxRequests", max_req))
                label = obj.get("label", label)
        except Exception:
            pass
    return {"windowSeconds": win_s, "maxRequests": max_req, "label": label}


@router.get("/v1/tenants", response_model=list[TenantResponse])
def list_tenants(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> list[TenantResponse]:
    _require_admin(authorization)
    out: list[TenantResponse] = []
    for t in _store.all().values():
        out.append(TenantResponse(id=t.id, label=t.label, quota=t.quota, expires_at=t.expires_at, status=t.status))
    out.sort(key=lambda x: x.id)
    return out


@router.post("/v1/tenants", response_model=TenantResponse)
async def create_tenant(
    req: TenantCreateRequest,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    rotate: bool = Query(default=False, description="If true and tenant exists, mint a new subkey and update store"),
    revoke_old: bool = Query(default=False, description="When rotating and an old key_id exists, revoke it after successful rotate"),
) -> TenantResponse:
    _require_admin(authorization)

    parent_key = os.getenv("VENICE_PARENT_KEY") or os.getenv("VENICE_API_KEY")
    if not parent_key:
        raise HTTPException(status_code=400, detail="VENICE_PARENT_KEY or VENICE_API_KEY must be set")

    existing_t = _store.get(req.tenant_id)
    if existing_t and not rotate:
        return TenantResponse(
            id=existing_t.id,
            label=existing_t.label,
            quota=existing_t.quota,
            expires_at=existing_t.expires_at,
            status=existing_t.status,
        )

    if existing_t and rotate:
        quota = int(req.quota) if req.quota is not None else int(existing_t.quota)
        expires_at = req.expires_at or existing_t.expires_at or _compute_expires_at(None)
        label = req.label or existing_t.label
        old_key_id = getattr(existing_t, "key_id", None)
    else:
        label = req.label
        quota = int(req.quota) if req.quota is not None else _default_quota
        expires_at = req.expires_at or _compute_expires_at(None)
        old_key_id = None

    try:
        sub = _keys.issue_scoped_key(parent_key, label, quota, expires_at)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Failed to create subkey: {exc}") from exc

    subkey = _extract_field(sub, ("apiKey", "api_key", "key", "token", "api_key_value"))
    if not subkey:
        keys_present = list(sub.keys()) if isinstance(sub, dict) else []
        raise HTTPException(status_code=502, detail=f"Subkey not returned by Venice (fields={keys_present})")
    key_id = _extract_field(sub, ("id", "keyId", "apiKeyId", "api_key_id"))

    tenant = Tenant(id=req.tenant_id, label=label, subkey=subkey, quota=quota, expires_at=expires_at, key_id=key_id or None)
    _store.upsert(tenant)
    if rotate and revoke_old and old_key_id:
        try:
            _keys.revoke_key(str(old_key_id))
        except Exception as exc:  # noqa: BLE001
            _logger.warning("rotate: revoke_old failed for key_id=%s: %s", old_key_id, exc)
    return TenantResponse(id=tenant.id, label=tenant.label, quota=tenant.quota, expires_at=tenant.expires_at, status=tenant.status)


@router.post("/v1/tenants/{tenant_id}/revoke")
def revoke_tenant_key(
    tenant_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Dict[str, Any]:
    _require_admin(authorization)
    t = _store.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    try:
        kid = getattr(t, "key_id", None)
        if kid:
            _keys.revoke_key(str(kid))
    except Exception as exc:  # noqa: BLE001
        _logger.warning("revoke failed or unsupported: %s", exc)
    t.status = "revoked"
    _store.upsert(t)
    return {"status": "revoked", "tenant": tenant_id}


@router.get("/v1/tenants/{tenant_id}", response_model=TenantResponse)
def get_tenant(
    tenant_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> TenantResponse:
    _require_admin(authorization)
    t = _store.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    return TenantResponse(id=t.id, label=t.label, quota=t.quota, expires_at=t.expires_at, status=t.status)


@router.get("/v1/tenants/{tenant_id}/broker-limits")
def get_broker_limits(
    tenant_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Dict[str, Any]:
    _require_admin(authorization)
    t = _store.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _get_broker_limits_obj(tenant_id)


@router.post("/v1/tenants/{tenant_id}/broker-limits")
def set_broker_limits(
    tenant_id: str,
    limits: BrokerLimits,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
) -> Dict[str, Any]:
    _require_admin(authorization)
    t = _store.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    if _kv_admin is None:
        raise HTTPException(status_code=503, detail="KV store unavailable")
    try:
        current = _get_broker_limits_obj(tenant_id)
        if limits.windowSeconds is not None:
            current["windowSeconds"] = int(limits.windowSeconds)
        if limits.maxRequests is not None:
            current["maxRequests"] = int(limits.maxRequests)
        if limits.label is not None:
            current["label"] = limits.label
        _kv_admin.set(f"broker:tenant:{tenant_id}:limits", json.dumps(current))
        return current
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"failed to update limits: {exc}") from exc


@router.post("/v1/chat")
async def chat_proxy(
    payload: ChatRequest,
    x_tenant_id: Optional[str] = Header(default=None, alias="X-Tenant-Id"),
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Dict[str, Any]:
    role, tenant_ctx = _auth_context(authorization)
    if role == "tenant":
        t = tenant_ctx
        if t is None or t.status != "active":
            raise HTTPException(status_code=401, detail="invalid tenant")
        if x_tenant_id and x_tenant_id != t.id:
            raise HTTPException(status_code=403, detail="tenant mismatch")
    else:
        if not x_tenant_id:
            raise HTTPException(status_code=400, detail="X-Tenant-Id required for admin calls")
        t = _store.get(x_tenant_id)
        if not t or t.status != "active":
            raise HTTPException(status_code=404, detail="tenant not found or inactive")

    win_s = _rate_limit_window_seconds
    max_req = _rate_limit_max_requests
    if _kv_admin is not None and _rate_limits_enabled and _limiter is not None:
        try:
            raw = _kv_admin.get(f"broker:tenant:{t.id}:limits")
            if raw:
                obj = json.loads(raw)
                win_s = int(obj.get("windowSeconds", win_s))
                max_req = int(obj.get("maxRequests", max_req))
        except Exception:
            pass

    try:
        annotate_span(
            {
                "tenantId": t.id,
                "windowSeconds": int(win_s),
                "maxRequests": int(max_req),
                "model": payload.model,
            },
            name="broker.chat.attrs",
        )
    except Exception:
        pass

    if _rate_limits_enabled and _limiter is not None and max_req > 0 and win_s > 0:
        key = f"tenant:{t.id}:chat"
        allowed, hdrs = _limiter.allow(key, max_req, win_s)
        if not allowed:
            try:
                reset_at = int(hdrs.get("X-RateLimit-Reset", "0"))
                retry_after = max(0, reset_at - int(time.time()))
            except Exception:
                retry_after = win_s
            raise HTTPException(
                status_code=429,
                detail="rate limit exceeded",
                headers={**hdrs, "Retry-After": str(retry_after)},
            )

    _def_model = (os.getenv("BROKER_DEFAULT_MODEL") or os.getenv("VENICE_DEFAULT_MODEL") or "").strip()
    _model = payload.model or (_def_model if _def_model else None)

    # Idempotency check: compute key from tenant_id + scope + payload hash
    idem_storage_key = None
    idem_accepted = False
    if _kv_admin is not None:
        try:
            # Compute hash of payload (messages + model + max_tokens)
            payload_str = json.dumps(
                {
                    "messages": payload.messages,
                    "model": _model,
                    "max_tokens": payload.max_tokens,
                },
                sort_keys=True,
            )
            payload_hash = hashlib.sha256(payload_str.encode()).hexdigest()[:16]
            if idempotency_key:
                idem_storage_key = f"idem:chat:{t.id}:header:{idempotency_key.strip()}"
            else:
                idem_storage_key = f"idem:chat:{t.id}:payload:{payload_hash}"
            
            # Check if key exists
            existing = _kv_admin.get(idem_storage_key)
            if existing is not None:
                # Idempotency replay detected
                return JSONResponse(
                    status_code=409,
                    content={"code": "idempotency_replay", "detail": "idempotency replay"},
                    headers={"X-Idempotency-Accepted": "false"},
                )
            
            # Store key with TTL
            idem_ttl_s = int(os.getenv("IDEM_TTL_SECONDS") or "60")
            _kv_admin.set(idem_storage_key, payload_hash, ttl_s=idem_ttl_s)
            idem_accepted = True
        except HTTPException:
            raise
        except Exception:
            # If idempotency check fails, continue without it
            pass

    sub_client = VeniceClient(api_key=t.subkey, base_url=_client.config.base_url)
    extra_args: Dict[str, Any] = {}
    if payload.max_tokens is not None:
        extra_args["max_tokens"] = int(payload.max_tokens)
    try:
        result = None
        if _model:
            result = sub_client.chat_completions(messages=payload.messages, model=_model, **extra_args)
        else:
            result = sub_client.chat_completions(messages=payload.messages, **extra_args)
        
        # Return result with idempotency header if applicable
        if idem_accepted:
            return JSONResponse(
                status_code=200,
                content=result if isinstance(result, dict) else result,
                headers={"X-Idempotency-Accepted": "true"},
            )
        return result
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"venice error: {exc}") from exc


@router.get("/v1/tenants/{tenant_id}/usage", response_model=UsageResponse)
def tenant_usage(
    tenant_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    response: Response = Response(),
) -> UsageResponse:
    role, tenant_ctx = _auth_context(authorization)
    t = _store.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    if role == "tenant":
        if not tenant_ctx or tenant_ctx.id != tenant_id:
            raise HTTPException(status_code=403, detail="forbidden")

    for key, value in _get_rate_limit_headers(tenant_id).items():
        response.headers[key] = value

    sub_client = VeniceClient(api_key=t.subkey, base_url=_client.config.base_url)
    try:
        usage = sub_client.get_usage()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"usage fetch failed: {exc}") from exc
    try:
        limits = sub_client.get_rate_limits()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("limits fetch failed: %s", exc)
        limits = None
    return UsageResponse(usage=usage, limits=limits)


@router.get("/v1/tenants/{tenant_id}/limits")
def tenant_limits(
    tenant_id: str,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    response: Response = Response(),
) -> Dict[str, Any]:
    role, tenant_ctx = _auth_context(authorization)
    t = _store.get(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="tenant not found")
    if role == "tenant":
        if not tenant_ctx or tenant_ctx.id != tenant_id:
            raise HTTPException(status_code=403, detail="forbidden")

    for key, value in _get_rate_limit_headers(tenant_id).items():
        response.headers[key] = value

    sub_client = VeniceClient(api_key=t.subkey, base_url=_client.config.base_url)
    try:
        return sub_client.get_rate_limits()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"limits fetch failed: {exc}") from exc


@router.get("/v1/me")
def whoami(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    response: Response = Response(),
) -> Dict[str, Any]:
    role, tenant_ctx = _auth_context(authorization)
    if role == "tenant" and tenant_ctx:
        for key, value in _get_rate_limit_headers(tenant_ctx.id).items():
            response.headers[key] = value
    if role == "admin":
        return {"role": "admin"}
    assert tenant_ctx is not None
    return {"role": "tenant", "tenant": {"id": tenant_ctx.id, "label": tenant_ctx.label}}


@router.get("/v1/me/usage", response_model=UsageResponse)
def my_usage(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    response: Response = Response(),
) -> UsageResponse:
    role, tenant_ctx = _auth_context(authorization)
    if role != "tenant" or tenant_ctx is None:
        raise HTTPException(status_code=403, detail="tenant auth required")

    for key, value in _get_rate_limit_headers(tenant_ctx.id).items():
        response.headers[key] = value

    sub_client = VeniceClient(api_key=tenant_ctx.subkey, base_url=_client.config.base_url)
    try:
        usage = sub_client.get_usage()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"usage fetch failed: {exc}") from exc
    try:
        limits = sub_client.get_rate_limits()
    except Exception as exc:  # noqa: BLE001
        _logger.warning("limits fetch failed: %s", exc)
        limits = None
    return UsageResponse(usage=usage, limits=limits)


@router.get("/v1/me/broker-limits")
def my_broker_limits(
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    response: Response = Response(),
) -> Dict[str, Any]:
    role, tenant_ctx = _auth_context(authorization)
    if role != "tenant" or tenant_ctx is None:
        raise HTTPException(status_code=403, detail="tenant auth required")

    for key, value in _get_rate_limit_headers(tenant_ctx.id).items():
        response.headers[key] = value

    return _get_broker_limits_obj(tenant_ctx.id)


@router.post("/v1/me/broker-limits")
def my_set_broker_limits(
    limits: BrokerLimits,
    authorization: Optional[str] = Header(default=None, alias="Authorization"),
    response: Response = Response(),
) -> Dict[str, Any]:
    role, tenant_ctx = _auth_context(authorization)
    if role != "tenant" or tenant_ctx is None:
        raise HTTPException(status_code=403, detail="tenant auth required")
    if _kv_admin is None:
        raise HTTPException(status_code=503, detail="KV store unavailable")

    for key, value in _get_rate_limit_headers(tenant_ctx.id).items():
        response.headers[key] = value

    current = _get_broker_limits_obj(tenant_ctx.id)
    if limits.windowSeconds is not None:
        try:
            ws = int(limits.windowSeconds)
            if ws < 1:
                raise ValueError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="invalid windowSeconds") from exc
        if ws < int(current.get("windowSeconds", ws)):
            raise HTTPException(status_code=403, detail="cannot decrease windowSeconds (admin only)")
        current["windowSeconds"] = ws
    if limits.maxRequests is not None:
        try:
            mr = int(limits.maxRequests)
            if mr < 0:
                raise ValueError
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail="invalid maxRequests") from exc
        if mr > int(current.get("maxRequests", mr)):
            raise HTTPException(status_code=403, detail="cannot increase maxRequests (admin only)")
        current["maxRequests"] = mr
    if limits.label is not None:
        if not str(limits.label).startswith("self:"):
            raise HTTPException(status_code=403, detail="label must be prefixed with 'self:'")
        current["label"] = limits.label

    _kv_admin.set(f"broker:tenant:{tenant_ctx.id}:limits", json.dumps(current))
    return current


__all__ = ["router", "init_router"]
