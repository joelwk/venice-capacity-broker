from __future__ import annotations

from apps._path import add_repo_root_to_sys_path

add_repo_root_to_sys_path()

from libs.telemetry.logger import get_logger
from libs.telemetry.tracing import annotate_span
from libs.venice_sdk.client import VeniceClient
from services.venice_keys.manager import KeyManager
try:
    from .tenant_store import TenantStore, Tenant
except Exception:
    # Fallback for direct file execution/import where package context is absent
    import importlib.util as _ilu
    from pathlib import Path as _Path

    _p = _Path(__file__).with_name("tenant_store.py").resolve()
    import sys as _sys
    _mod_name = "broker_tenant_store_fallback"
    _spec = _ilu.spec_from_file_location(_mod_name, str(_p))
    assert _spec and _spec.loader
    _mod = _ilu.module_from_spec(_spec)
    _sys.modules[_mod_name] = _mod
    _spec.loader.exec_module(_mod)  # type: ignore[attr-defined]
    TenantStore = _mod.TenantStore  # type: ignore[assignment]
    Tenant = _mod.Tenant  # type: ignore[assignment]
try:
    from .tenant_store_sql import SQLTenantStore as _SQLTenantStore  # type: ignore
except Exception:
    _SQLTenantStore = None  # type: ignore


logger = get_logger("broker.api")

try:
    from fastapi import FastAPI, Header, HTTPException, Request, Query
    from fastapi.responses import PlainTextResponse
    from starlette.middleware.base import BaseHTTPMiddleware
    import threading, time
    import hashlib, json as _json
    from pydantic import BaseModel

    app = FastAPI(title="VVV Capacity Broker API", version="0.1.0")

    # Minimal root index with links to docs and health
    from fastapi.responses import HTMLResponse as _HTML

    @app.get("/", include_in_schema=False)
    def index():
        return _HTML(
            """
            <!doctype html>
            <html lang=\"en\">
              <head>
                <meta charset=\"utf-8\" />
                <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
                <title>VVV Capacity Broker API</title>
                <style>
                  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif;line-height:1.4;margin:2rem;color:#1a1a1a}
                  a{color:#0b66ff;text-decoration:none}
                  a:hover{text-decoration:underline}
                  .links{display:flex;gap:1rem;flex-wrap:wrap;margin-top:1rem}
                  code{background:#f4f6f8;padding:0.15rem 0.35rem;border-radius:4px}
                </style>
              </head>
              <body>
                <h1>VVV Capacity Broker API</h1>
                <p>Quick links:</p>
                <div class=\"links\">
                  <a href=\"/docs\">Swagger UI</a>
                  <a href=\"/redoc\">ReDoc</a>
                  <a href=\"/health\">Health</a>
                  <a href=\"/metrics\">Metrics</a>
                </div>
                <p>Admin endpoints require <code>Authorization: Bearer &lt;BROKER_ADMIN_TOKEN&gt;</code>.</p>
              </body>
            </html>
            """
        )

    class TenantCreateRequest(BaseModel):
        tenant_id: str
        label: str
        quota: int | None = None
        expires_at: str | None = None

    class TenantResponse(BaseModel):
        id: str
        label: str
        quota: int
        expires_at: str | None = None
        status: str

    class ChatRequest(BaseModel):
        messages: list[dict]
        model: str | None = None

    class UsageResponse(BaseModel):
        usage: dict
        limits: dict | None = None

    # Venice Web3 key flow models
    class Web3ChallengeRequest(BaseModel):
        wallet: str

    class Web3CreateRootRequest(BaseModel):
        address: str
        signature: str
        # Optional pass-throughs if supported by deployment
        challenge: str | None = None
        challengeId: str | None = None
        apiKeyType: str | None = None
        consumptionLimit: dict | int | None = None
        expiresAt: str | None = None
    class CreateSubkeyRequest(BaseModel):
        label: str
        consumptionLimit: dict | int
        expiresAt: str | None = None
        parentKey: str | None = None  # Optional override; otherwise env is used

    # Choose store backend: SQL if requested and available, else JSON file
    import os as _os

    _backend = (_os.getenv("BROKER_STORE_BACKEND") or "json").strip().lower()
    if _backend == "sql" and _SQLTenantStore is not None:
        try:
            store = _SQLTenantStore()  # type: ignore[call-arg]
            logger.info("broker.store: using SQL backend")
        except Exception as _store_err:  # noqa: BLE001
            logger.warning("broker.store: SQL backend requested but failed: %s; falling back to JSON", _store_err)
            store = TenantStore()
    else:
        store = TenantStore()
    client = VeniceClient()
    keys = KeyManager(client)

    # --- Defaults from environment ---
    ADMIN_TOKEN = _os.getenv("BROKER_ADMIN_TOKEN")
    REQUIRE_ADMIN = (_os.getenv("BROKER_REQUIRE_ADMIN_TOKEN") or "false").strip().lower() in {"1", "true", "yes", "on"}
    DEFAULT_QUOTA = int((_os.getenv("BROKER_DEFAULT_QUOTA") or "0").strip() or 0)
    DEFAULT_EXPIRY_DAYS = int((_os.getenv("BROKER_DEFAULT_EXPIRY_DAYS") or "0").strip() or 0)

    # Startup security checks/logs
    if REQUIRE_ADMIN and not ADMIN_TOKEN:
        logger.error("security: BROKER_REQUIRE_ADMIN_TOKEN=true but BROKER_ADMIN_TOKEN is unset; refusing to start")
        raise RuntimeError("BROKER_ADMIN_TOKEN required for startup")
    if ADMIN_TOKEN:
        logger.info("security: admin token configured; admin endpoints require bearer token")
    else:
        logger.warning("security: BROKER_ADMIN_TOKEN not set; admin endpoints allowed for development only")

    def _compute_expires_at(now_s: float | None = None) -> str | None:
        if DEFAULT_EXPIRY_DAYS <= 0:
            return None
        t = int((now_s or time.time()) + DEFAULT_EXPIRY_DAYS * 24 * 3600)
        # ISO8601 Zulu
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    # --- Optional KV-backed rate limiter (scaffold) ---
    RATE_LIMITS_ENABLED = (_os.getenv("RATE_LIMITS_ENABLED") or "false").strip().lower() == "true"
    RATE_LIMIT_WINDOW_SECONDS = int((_os.getenv("RATE_LIMIT_WINDOW_SECONDS") or "60").strip() or 60)
    RATE_LIMIT_MAX_REQUESTS = int((_os.getenv("RATE_LIMIT_MAX_REQUESTS") or "60").strip() or 60)
    _limiter = None
    _kv_admin = None
    if RATE_LIMITS_ENABLED:
        try:
            from libs.kv import KVStore
            from libs.ratelimit import KVSlidingWindowLimiter

            _kv = KVStore()
            _limiter = KVSlidingWindowLimiter(_kv)
            _kv_admin = _kv
            logger.info(
                "rate-limiter: enabled (window=%ss, max=%s)",
                RATE_LIMIT_WINDOW_SECONDS,
                RATE_LIMIT_MAX_REQUESTS,
            )
        except Exception as _e:  # noqa: BLE001
            logger.warning("rate-limiter: failed to initialize; continuing without (%s)", _e)
            _limiter = None
            _kv_admin = None
    else:
        try:
            from libs.kv import KVStore
            _kv_admin = KVStore()
        except Exception:
            _kv_admin = None
    # Idempotency config
    IDEM_TTL_SECONDS = int((_os.getenv("IDEM_TTL_SECONDS") or _os.getenv("IDEMPOTENCY_TTL_SECONDS") or "300").strip() or 300)

    # Idempotency key format (documented for tooling/CLI):
    IDEM_KEY_FORMAT = "idem:{scope}:{tenant_id}:{digest}:{epoch_min}"

    class IdempotencyMiddleware(BaseHTTPMiddleware):
        def __init__(self, app: FastAPI):
            super().__init__(app)

        async def dispatch(self, request: Request, call_next):  # type: ignore[override]
            # Only enforce for mutating chat requests
            try:
                path = request.url.path
                if request.method.upper() not in {"POST", "PUT", "PATCH"}:
                    return await call_next(request)
                if not path.startswith("/v1/chat"):
                    return await call_next(request)
                # KV required
                if _kv_admin is None or IDEM_TTL_SECONDS <= 0:
                    return await call_next(request)

                # Read small body safely (Starlette requires receive buffering)
                body_bytes = await request.body()
                # Reconstruct request with cached body for downstream
                async def receive() -> dict:  # type: ignore[override]
                    return {"type": "http.request", "body": body_bytes, "more_body": False}

                request._receive = receive  # type: ignore[attr-defined]

                # Compute a lightweight hash
                idem_header = request.headers.get("Idempotency-Key") or ""
                base = f"{request.method}:{path}:{body_bytes.decode('utf-8', 'ignore')}:{idem_header}"
                digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:16]
                # Tenant scope best-effort
                scope = "chat"
                # Try to map tenant id by Authorization bearer
                tenant_id = "anon"
                try:
                    auth = request.headers.get("Authorization")
                    tok = _bearer_token(auth)
                    # Match subkey to tenant
                    if tok:
                        for t in store.all().values():
                            if t.subkey == tok:
                                tenant_id = t.id
                                break
                    # Admin path: allow explicit X-Tenant-Id
                    xt = request.headers.get("X-Tenant-Id")
                    if xt:
                        tenant_id = xt
                except Exception:
                    pass

                epoch_min = int(time.time() // 60)
                # Keys follow IDEM_KEY_FORMAT
                key = f"idem:{scope}:{tenant_id}:{digest}:{epoch_min}"

                # First writer wins
                try:
                    new_v = int(_kv_admin.incrby(key, 1, ttl_s=int(IDEM_TTL_SECONDS)))
                except Exception:
                    return await call_next(request)
                if new_v > 1:
                    # Duplicate detected
                    detail = {
                        "code": "idempotency_replay",
                        "message": "Duplicate request within TTL window",
                        "details": {"scope": scope, "tenant_id": tenant_id, "hash": digest, "epoch_min": epoch_min},
                    }
                    from fastapi.responses import JSONResponse

                    resp = JSONResponse(status_code=409, content=detail)
                    resp.headers["X-Idempotency-Accepted"] = "false"
                    try:
                        annotate_span({"duplicate": True, **detail.get("details", {})}, name="broker.idem.reject")
                    except Exception:
                        pass
                    return resp

                response = await call_next(request)
                try:
                    response.headers["X-Idempotency-Accepted"] = "true"
                    annotate_span({"duplicate": False, "scope": scope, "tenant_id": tenant_id}, name="broker.idem.accept")
                except Exception:
                    pass
                return response
            except Exception:
                # Fail-open
                return await call_next(request)

    # Install middleware near the top for observability
    app.add_middleware(IdempotencyMiddleware)

    # --- LangSmith trace helper ---
    def _traceable(name: str):
        try:
            enabled = (_os.getenv("LANGCHAIN_TRACING_V2") or "false").strip().lower() in {"1", "true", "yes"}
            if not enabled:
                return lambda f: f
            from langsmith import traceable  # type: ignore
            return traceable(name=name)
        except Exception:
            return lambda f: f

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # --- Auth helpers ---

    def _bearer_token(authorization: str | None) -> str | None:
        if not authorization:
            return None
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1]
        return None

    def _require_admin(authorization: str | None) -> None:
        token = _bearer_token(authorization)
        if ADMIN_TOKEN:
            if token != ADMIN_TOKEN:
                raise HTTPException(status_code=401, detail="admin auth required")
        else:
            logger.warning("BROKER_ADMIN_TOKEN not set; allowing admin endpoints for development")

    def _tenant_by_subkey(tok: str | None) -> Tenant | None:
        if not tok:
            return None
        for t in store.all().values():
            if t.subkey == tok:
                return t
        return None

    def _auth_context(authorization: str | None) -> tuple[str, Tenant | None]:
        token = _bearer_token(authorization)
        # Admin takes precedence
        if ADMIN_TOKEN and token == ADMIN_TOKEN:
            return ("admin", None)
        # Tenant by subkey
        t = _tenant_by_subkey(token)
        if t:
            return ("tenant", t)
        # If no ADMIN_TOKEN set, we do not fall back to open access here.
        raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/v1/tenants", response_model=list[TenantResponse])
    def list_tenants(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> list[TenantResponse]:
        _require_admin(authorization)
        out: list[TenantResponse] = []
        for t in store.all().values():
            out.append(TenantResponse(id=t.id, label=t.label, quota=t.quota, expires_at=t.expires_at, status=t.status))
        # Sort by id for stable output
        out.sort(key=lambda x: x.id)
        return out

    # --- Venice Web3 root key helper endpoints (admin-only) ---

    @app.post("/v1/venice/web3/challenge")
    @_traceable("broker.venice_web3_challenge")
    def venice_web3_challenge(req: Web3ChallengeRequest, authorization: str | None = Header(default=None, alias="Authorization")) -> dict:
        _require_admin(authorization)
        try:
            return client.get_challenge(req.wallet)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"challenge fetch failed: {e}")

    @app.post("/v1/venice/web3/create-root-key")
    @_traceable("broker.venice_web3_create_root")
    def venice_web3_create_root(req: Web3CreateRootRequest, authorization: str | None = Header(default=None, alias="Authorization")) -> dict:
        _require_admin(authorization)
        try:
            return client.create_root_inference_key(
                wallet_address=req.address,
                signature=req.signature,
                challenge=req.challenge,
                challenge_id=req.challengeId,
                api_key_type=req.apiKeyType,
                consumption_limit=req.consumptionLimit,
                expires_at=req.expiresAt,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"root key creation failed: {e}")
    @app.post("/v1/venice/subkey")
    @_traceable("broker.venice_create_subkey")
    def venice_create_subkey(req: CreateSubkeyRequest, authorization: str | None = Header(default=None, alias="Authorization")) -> dict:
        """Admin-only: create a scoped subkey using a parent key.

        Uses `req.parentKey` if provided, else falls back to `VENICE_PARENT_KEY` or `VENICE_API_KEY` from env.
        """
        _require_admin(authorization)
        import os as __os
        parent = req.parentKey or __os.getenv("VENICE_PARENT_KEY") or __os.getenv("VENICE_API_KEY")
        if not parent:
            raise HTTPException(status_code=400, detail="no parent key provided and VENICE_PARENT_KEY/VENICE_API_KEY not set")
        try:
            return client.create_scoped_subkey(
                parent_key=parent,
                label=req.label,
                consumption_limit=req.consumptionLimit,
                expires_at=req.expiresAt,
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"subkey creation failed: {e}")

    @app.post("/v1/tenants", response_model=TenantResponse)
    @_traceable("broker.create_tenant")
    def create_tenant(
        req: TenantCreateRequest,
        authorization: str | None = Header(default=None, alias="Authorization"),
        rotate: bool = Query(default=False, description="If true and tenant exists, mint a new subkey and update store"),
        revoke_old: bool = Query(default=False, description="When rotating and an old key_id exists, revoke it after successful rotate"),
    ) -> TenantResponse:
        _require_admin(authorization)
        import os

        parent_key = os.getenv("VENICE_PARENT_KEY") or os.getenv("VENICE_API_KEY")
        if not parent_key:
            raise HTTPException(status_code=400, detail="VENICE_PARENT_KEY or VENICE_API_KEY must be set")
        # Idempotent by default; support rotation via query param
        existing_t = store.get(req.tenant_id)
        if existing_t and not rotate:
            return TenantResponse(id=existing_t.id, label=existing_t.label, quota=existing_t.quota, expires_at=existing_t.expires_at, status=existing_t.status)

        # Apply env-configured defaults if not provided
        if existing_t and rotate:
            # Rotate: preserve or override values from request
            quota = int(req.quota) if req.quota is not None else int(existing_t.quota)
            expires_at = req.expires_at or existing_t.expires_at or _compute_expires_at()
            label = req.label or existing_t.label
            old_key_id = getattr(existing_t, "key_id", None)
        else:
            label = req.label
            quota = int(req.quota) if req.quota is not None else DEFAULT_QUOTA
            expires_at = req.expires_at or _compute_expires_at()
            old_key_id = None
        try:
            sub = keys.issue_scoped_key(parent_key, label, quota, expires_at)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Failed to create subkey: {e}")
        # Extract the api key token from common response shapes
        def _extract_api_key(obj: dict) -> str:
            try:
                # Direct fields first
                for k in ("apiKey", "api_key", "key", "token", "api_key_value"):
                    v = obj.get(k)
                    if isinstance(v, str) and len(v) >= 16:
                        return v
                # Nested objects
                for v in obj.values():
                    if isinstance(v, dict):
                        s = _extract_api_key(v)
                        if s:
                            return s
            except Exception:
                pass
            return ""

        subkey = _extract_api_key(sub)
        if not subkey:
            # Do not leak response content; include top-level keys to aid debugging
            try:
                keys_present = list(sub.keys())
            except Exception:
                keys_present = []
            raise HTTPException(status_code=502, detail=f"Subkey not returned by Venice (fields={keys_present})")
        # Attempt to capture key id for later revoke
        key_id = ""
        try:
            for k in ("id", "keyId", "apiKeyId", "api_key_id"):
                v = sub.get(k)
                if v:
                    key_id = str(v)
                    break
        except Exception:
            key_id = ""

        tenant = Tenant(id=req.tenant_id, label=label, subkey=subkey, quota=quota, expires_at=expires_at, key_id=key_id or None)
        store.upsert(tenant)
        # Best-effort revoke of the old key only after successful rotate and store update
        if rotate and revoke_old and old_key_id:
            try:
                keys.revoke_key(str(old_key_id))
            except Exception as e:  # noqa: BLE001
                logger.warning(f"rotate: revoke_old failed for key_id={old_key_id}: {e}")
        return TenantResponse(id=tenant.id, label=tenant.label, quota=tenant.quota, expires_at=tenant.expires_at, status=tenant.status)

    @app.post("/v1/tenants/{tenant_id}/revoke")
    @_traceable("broker.revoke_tenant")
    def revoke_tenant_key(
        tenant_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        _require_admin(authorization)
        t = store.get(tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="tenant not found")
        try:
            # Attempt to revoke via official DELETE /api_keys/{id}
            kid = getattr(t, "key_id", None)
            if kid:
                keys.revoke_key(str(kid))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"revoke failed or unsupported: {e}")
        t.status = "revoked"
        store.upsert(t)
        return {"status": "revoked", "tenant": tenant_id}

    @app.get("/v1/tenants/{tenant_id}", response_model=TenantResponse)
    @_traceable("broker.get_tenant")
    def get_tenant(
        tenant_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> TenantResponse:
        _require_admin(authorization)
        t = store.get(tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="tenant not found")
        return TenantResponse(id=t.id, label=t.label, quota=t.quota, expires_at=t.expires_at, status=t.status)

    @app.post("/v1/chat")
    @_traceable("broker.chat_proxy")
    def chat_proxy(
        payload: ChatRequest,
        x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        """Proxy chat to Venice using tenant subkey or admin-specified tenant.

        Auth:
        - Authorization: Bearer <tenant-subkey> -> acts as that tenant
        - Authorization: Bearer <BROKER_ADMIN_TOKEN> + X-Tenant-Id -> acts as specified tenant
        """
        role, tenant_ctx = _auth_context(authorization)
        if role == "tenant":
            t = tenant_ctx
            if t is None or t.status != "active":  # defensive
                raise HTTPException(status_code=401, detail="invalid tenant")
            # If X-Tenant-Id is provided, ensure it matches
            if x_tenant_id and x_tenant_id != t.id:
                raise HTTPException(status_code=403, detail="tenant mismatch")
        else:
            # admin path: require X-Tenant-Id
            if not x_tenant_id:
                raise HTTPException(status_code=400, detail="X-Tenant-Id required for admin calls")
            t = store.get(x_tenant_id)
            if not t or t.status != "active":
                raise HTTPException(status_code=404, detail="tenant not found or inactive")

        # Optional: per-tenant KV-configured limits override env defaults
        win_s = RATE_LIMIT_WINDOW_SECONDS
        max_req = RATE_LIMIT_MAX_REQUESTS
        if _kv_admin is not None:
            try:
                import json as _json
                raw = _kv_admin.get(f"broker:tenant:{t.id}:limits")
                if raw:
                    obj = _json.loads(raw)
                    win_s = int(obj.get("windowSeconds", win_s))
                    max_req = int(obj.get("maxRequests", max_req))
            except Exception:
                pass

        # Add span attributes for tracing (LangSmith) prior to enforcement
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

        # Optional: enforce KV-based sliding-window limit per tenant
        if _limiter is not None and max_req > 0 and win_s > 0:
            key = f"tenant:{t.id}:chat"
            allowed, hdrs = _limiter.allow(key, max_req, win_s)
            if not allowed:
                # Compute Retry-After as seconds until reset
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

        sub_client = VeniceClient(api_key=t.subkey, base_url=client.config.base_url)
        try:
            res = sub_client.chat_completions(messages=payload.messages, model=payload.model)
            return res
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"venice error: {e}")

    @app.get("/v1/tenants/{tenant_id}/usage", response_model=UsageResponse)
    @_traceable("broker.tenant_usage")
    def tenant_usage(
        tenant_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> UsageResponse:
        role, tenant_ctx = _auth_context(authorization)
        t = store.get(tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="tenant not found")
        if role == "tenant":
            if not tenant_ctx or tenant_ctx.id != tenant_id:
                raise HTTPException(status_code=403, detail="forbidden")
        sub = VeniceClient(api_key=t.subkey, base_url=client.config.base_url)
        try:
            usage = sub.get_usage()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"usage fetch failed: {e}")
        limits: dict | None
        try:
            limits = sub.get_rate_limits()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"limits fetch failed: {e}")
            limits = None
        return UsageResponse(usage=usage, limits=limits)

    @app.get("/v1/tenants/{tenant_id}/limits")
    @_traceable("broker.tenant_limits")
    def tenant_limits(
        tenant_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        role, tenant_ctx = _auth_context(authorization)
        t = store.get(tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="tenant not found")
        if role == "tenant":
            if not tenant_ctx or tenant_ctx.id != tenant_id:
                raise HTTPException(status_code=403, detail="forbidden")
        sub = VeniceClient(api_key=t.subkey, base_url=client.config.base_url)
        try:
            return sub.get_rate_limits()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"limits fetch failed: {e}")

    @app.get("/v1/me")
    @_traceable("broker.me")
    def whoami(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        role, tenant_ctx = _auth_context(authorization)
        if role == "admin":
            return {"role": "admin"}
        assert tenant_ctx is not None
        return {"role": "tenant", "tenant": {"id": tenant_ctx.id, "label": tenant_ctx.label}}

    @app.get("/v1/me/usage", response_model=UsageResponse)
    @_traceable("broker.me_usage")
    def my_usage(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> UsageResponse:
        role, tenant_ctx = _auth_context(authorization)
        if role != "tenant" or tenant_ctx is None:
            raise HTTPException(status_code=403, detail="tenant auth required")
        sub = VeniceClient(api_key=tenant_ctx.subkey, base_url=client.config.base_url)
        try:
            usage = sub.get_usage()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"usage fetch failed: {e}")
        limits: dict | None
        try:
            limits = sub.get_rate_limits()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"limits fetch failed: {e}")
            limits = None
        return UsageResponse(usage=usage, limits=limits)

    # --- Metrics via starlette_exporter (with builtin fallback) ---
    METRICS_BACKEND = (_os.getenv("METRICS_BACKEND") or "auto").strip().lower()
    METRICS_PATH = (_os.getenv("METRICS_PATH") or "/metrics").strip() or "/metrics"

    _using_starlette_exporter = False
    if METRICS_BACKEND in ("auto", "starlette"):
        try:
            from starlette_exporter import PrometheusMiddleware, handle_metrics  # type: ignore

            app.add_middleware(PrometheusMiddleware)
            app.add_route(METRICS_PATH, handle_metrics)
            _using_starlette_exporter = True
            logger.info("metrics: using starlette_exporter at %s", METRICS_PATH)
        except Exception as _imp_err:  # noqa: BLE001
            if METRICS_BACKEND == "starlette":
                logger.warning("metrics: starlette_exporter requested but unavailable: %s", _imp_err)
            else:
                logger.info("metrics: starlette_exporter not installed; falling back to builtin")

    if not _using_starlette_exporter and METRICS_BACKEND != "off":
        class _Metrics:
            def __init__(self) -> None:
                self._lock = threading.Lock()
                self.requests_total = 0
                self.errors_total = 0
                self.latency_seconds_sum = 0.0
                self.by_path: dict[str, int] = {}

            def record(self, path: str, status_code: int, latency_s: float) -> None:
                with self._lock:
                    self.requests_total += 1
                    if status_code >= 500:
                        self.errors_total += 1
                    self.latency_seconds_sum += float(latency_s)
                    self.by_path[path] = self.by_path.get(path, 0) + 1

            def render_prom(self) -> str:
                lines: list[str] = []
                lines.append("# HELP vvv_requests_total Total HTTP requests.")
                lines.append("# TYPE vvv_requests_total counter")
                lines.append(f"vvv_requests_total {self.requests_total}")
                lines.append("# HELP vvv_errors_total 5xx HTTP responses.")
                lines.append("# TYPE vvv_errors_total counter")
                lines.append(f"vvv_errors_total {self.errors_total}")
                lines.append("# HELP vvv_request_latency_seconds_sum Cumulative request latency in seconds.")
                lines.append("# TYPE vvv_request_latency_seconds_sum counter")
                lines.append(f"vvv_request_latency_seconds_sum {self.latency_seconds_sum:.6f}")
                lines.append("# HELP vvv_requests_by_path_total Requests by path.")
                lines.append("# TYPE vvv_requests_by_path_total counter")
                for p, c in sorted(self.by_path.items()):
                    lines.append(f"vvv_requests_by_path_total{{path=\"{p}\"}} {c}")
                return "\n".join(lines) + "\n"

        _metrics = _Metrics()

        class MetricsMiddleware(BaseHTTPMiddleware):
            def __init__(self, app: FastAPI):
                super().__init__(app)

            async def dispatch(self, request, call_next):  # type: ignore[override]
                start = time.perf_counter()
                try:
                    response = await call_next(request)
                    return response
                finally:
                    dur = time.perf_counter() - start
                    status = getattr(locals().get("response", None), "status_code", 500)
                    path = request.url.path
                    try:
                        _metrics.record(path, int(status), float(dur))
                    except Exception:  # noqa: BLE001
                        pass

        app.add_middleware(MetricsMiddleware)

        @app.get(METRICS_PATH)
        @_traceable("broker.metrics")
        def metrics() -> PlainTextResponse:
            return PlainTextResponse(_metrics.render_prom(), media_type="text/plain; version=0.0.4; charset=utf-8")

    # --- Admin: per-tenant broker limits (caps/labels) ---
    from pydantic import Field

    class BrokerLimits(BaseModel):
        windowSeconds: int | None = Field(default=None, ge=1)
        maxRequests: int | None = Field(default=None, ge=0)
        label: str | None = None  # classification label (e.g., premium, basic)

    def _get_broker_limits_obj(tenant_id: str) -> dict:
        win_s = RATE_LIMIT_WINDOW_SECONDS
        max_req = RATE_LIMIT_MAX_REQUESTS
        label = None
        if _kv_admin is not None:
            try:
                import json as _json
                raw = _kv_admin.get(f"broker:tenant:{tenant_id}:limits")
                if raw:
                    obj = _json.loads(raw)
                    win_s = int(obj.get("windowSeconds", win_s))
                    max_req = int(obj.get("maxRequests", max_req))
                    label = obj.get("label", label)
            except Exception:
                pass
        return {"windowSeconds": win_s, "maxRequests": max_req, "label": label}

    @app.get("/v1/tenants/{tenant_id}/broker-limits")
    @_traceable("broker.get_broker_limits")
    def get_broker_limits(
        tenant_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        _require_admin(authorization)
        t = store.get(tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="tenant not found")
        return _get_broker_limits_obj(tenant_id)

    @app.post("/v1/tenants/{tenant_id}/broker-limits")
    @_traceable("broker.set_broker_limits")
    def set_broker_limits(
        tenant_id: str,
        limits: BrokerLimits,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        _require_admin(authorization)
        t = store.get(tenant_id)
        if not t:
            raise HTTPException(status_code=404, detail="tenant not found")
        if _kv_admin is None:
            raise HTTPException(status_code=503, detail="KV store unavailable")
        try:
            import json as _json
            current = _get_broker_limits_obj(tenant_id)
            if limits.windowSeconds is not None:
                current["windowSeconds"] = int(limits.windowSeconds)
            if limits.maxRequests is not None:
                current["maxRequests"] = int(limits.maxRequests)
            if limits.label is not None:
                current["label"] = limits.label
            _kv_admin.set(f"broker:tenant:{tenant_id}:limits", _json.dumps(current))
            return current
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"failed to update limits: {e}")

    # --- Debug: read aggregated counters from SQL ---
    @_traceable("broker.debug_counters")
    def _parse_dt(_val: str):
        from datetime import datetime as _dt

        try:
            if _val.isdigit():
                return _dt.utcfromtimestamp(int(_val))
        except Exception:
            pass
        try:
            v = _val.rstrip("Z").replace("T", " ")
            return _dt.fromisoformat(v)
        except Exception as _e:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"invalid datetime '{_val}': {_e}")

    @app.get("/v1/debug/counters")
    @_traceable("broker.debug_counters")
    def debug_counters(
        tenant_id: str | None = None,
        scope: str | None = None,
        model: str | None = None,
        bucket_seconds: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
        asc: bool = False,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> list[dict]:
        """Admin-only: return aggregated usage counters for a tenant from SQL.

        Query params:
        - tenant_id: required
        - scope, model, bucket_seconds: optional filters
        - since/until: ISO8601 or epoch seconds (inclusive)
        - limit: max rows (default 50)
        - asc: sort ascending by bucket_start (default desc)
        """
        _require_admin(authorization)
        if not tenant_id:
            raise HTTPException(status_code=400, detail="tenant_id is required")
        # Optional: ensure tenant exists
        if not store.get(tenant_id):
            raise HTTPException(status_code=404, detail="tenant not found")
        try:
            from sqlmodel import Session, select
            from db.session import get_engine
            from db.models import Counter
        except Exception as _e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"SQL dependencies unavailable: {_e}")

        engine = get_engine()
        q = select(Counter).where(Counter.tenant_id == tenant_id)
        if scope:
            q = q.where(Counter.scope == scope)
        if model:
            q = q.where(Counter.model == model)
        if bucket_seconds is not None:
            try:
                bs = int(bucket_seconds)
            except Exception as _e:  # noqa: BLE001
                raise HTTPException(status_code=400, detail=f"invalid bucket_seconds: {bucket_seconds}")
            q = q.where(Counter.bucket_seconds == int(bs))
        if since:
            q = q.where(Counter.bucket_start >= _parse_dt(since))
        if until:
            q = q.where(Counter.bucket_start <= _parse_dt(until))
        if asc:
            q = q.order_by(Counter.bucket_start)
        else:
            from sqlalchemy import desc as _desc

            q = q.order_by(_desc(Counter.bucket_start))
        limit = int(limit or 50)
        with Session(engine) as s:  # type: ignore[call-arg]
            rows = s.exec(q.limit(limit)).all()
        out: list[dict] = []
        for r in rows:
            out.append(
                {
                    "tenant_id": r.tenant_id,
                    "scope": r.scope,
                    "model": r.model,
                    "bucket_start": r.bucket_start.isoformat() + "Z",
                    "bucket_seconds": r.bucket_seconds,
                    "count": int(r.count),
                }
            )
        return out

except Exception as e:  # noqa: BLE001
    app = None  # type: ignore
    logger.warning(
        "FastAPI not available. Install 'fastapi pydantic uvicorn' to run Broker API.")

    def main() -> None:  # noqa: D401
        """Run info for environments without FastAPI installed."""
        print("Install fastapi, pydantic, uvicorn; then run:")
        print("  uvicorn app:app --app-dir apps/broker-api --reload")
