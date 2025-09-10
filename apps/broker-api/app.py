from __future__ import annotations

from apps._path import add_repo_root_to_sys_path

add_repo_root_to_sys_path()

# Ensure local .env is loaded for Replit/shell runs (without overriding Replit secrets)
try:
    from libs.env import load_dotenv_if_present  # type: ignore
    from pathlib import Path as _PathRoot

    _repo_root = _PathRoot(__file__).resolve().parents[2]
    load_dotenv_if_present(path=str(_repo_root / ".env"), override=False)
except Exception:
    pass

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
    # Fallback for direct execution where package context is absent
    try:
        import importlib.util as _ilu2
        from pathlib import Path as _PathSql
        import sys as _sys2

        _p_sql = _PathSql(__file__).with_name("tenant_store_sql.py").resolve()
        _mod_name_sql = "broker_tenant_store_sql_fallback"
        _spec_sql = _ilu2.spec_from_file_location(_mod_name_sql, str(_p_sql))
        if _spec_sql and _spec_sql.loader:
            _mod_sql = _ilu2.module_from_spec(_spec_sql)
            _sys2.modules[_mod_name_sql] = _mod_sql
            _spec_sql.loader.exec_module(_mod_sql)  # type: ignore[attr-defined]
            _SQLTenantStore = _mod_sql.SQLTenantStore  # type: ignore[attr-defined]
        else:
            _SQLTenantStore = None  # type: ignore[assignment]
    except Exception:
        _SQLTenantStore = None  # type: ignore[assignment]


logger = get_logger("broker.api")

try:
    from fastapi import FastAPI, Header, HTTPException, Request, Query
    from fastapi.responses import PlainTextResponse
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.staticfiles import StaticFiles
    from pathlib import Path as _Path2
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

    # Mount /admin static control panel if present
    try:
        _admin_dir = _Path2(__file__).resolve().parent.parent / "control-plane"
        if _admin_dir.exists():
            app.mount("/admin", StaticFiles(directory=str(_admin_dir), html=True), name="admin")
            logger.info("admin ui: mounted at /admin from %s", _admin_dir)
        else:
            logger.info("admin ui: directory not found at %s (skipping mount)", _admin_dir)
    except Exception as _e:
        logger.warning("admin ui: failed to mount /admin: %s", _e)

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

    # Broker limits schema used by both admin and tenant self-service endpoints
    from pydantic import Field

    class BrokerLimits(BaseModel):
        windowSeconds: int | None = Field(default=None, ge=1)
        maxRequests: int | None = Field(default=None, ge=0)
        label: str | None = None  # classification label (e.g., premium, basic)

    # --- Optional: CORS (flag-gated) ---
    try:
        import os as _cors_os

        if (_cors_os.getenv("CORS_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"}:
            _origins = [o.strip() for o in (_cors_os.getenv("CORS_ALLOW_ORIGINS") or "").split(",") if o.strip()]
            if not _origins:
                _origins = ["*"]
            app.add_middleware(
                CORSMiddleware,
                allow_origins=_origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
    except Exception:
        pass

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

    _backend = (_os.getenv("BROKER_STORE_BACKEND") or "sql").strip().lower()
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

    # --- Public, non-secret environment snapshot (for logs and /v1/env) ---
    def _env_snapshot_public() -> dict:
        try:
            # Web3/RPC
            rpc = (_os.getenv("RPC_URL") or _os.getenv("BASE_RPC_URL") or "").strip()
            chain_id = (_os.getenv("BASE_CHAIN_ID") or "").strip()

            # DEX providers and router addresses
            dex_providers = [p.strip().lower() for p in (_os.getenv("DEX_PROVIDERS") or "uniswap_v2,aerodrome").split(",") if p.strip()]
            uni_router = (_os.getenv("UNISWAP_V2_ROUTER_ADDRESS") or _os.getenv("ROUTER_ADDRESS") or "").strip()
            aero_router = (_os.getenv("AERODROME_ROUTER_ADDRESS") or "").strip()
            aero_stable_raw = (_os.getenv("AERODROME_STABLE") or "true").strip().lower()
            aero_stable = aero_stable_raw in {"1", "true", "yes", "on"}

            # Pricing config
            quote_token = (_os.getenv("QUOTE_TOKEN_ADDRESS") or "").strip()
            diem_token = (_os.getenv("DIEM_TOKEN_ADDRESS") or "").strip()
            vvv_token = (_os.getenv("VVV_TOKEN_ADDRESS") or "").strip()
            trade_path = (_os.getenv("TRADE_PATH") or "").strip()

            # ABI presence
            try:
                from pathlib import Path as _P

                repo_root = _P(__file__).resolve().parents[2]
                abi_dir = repo_root / "abi"
                def _has(name: str) -> bool:
                    try:
                        return (abi_dir / name).exists()
                    except Exception:
                        return False
                abi = {
                    "erc20": _has("erc20.json"),
                    "uniswap_v2_router": _has("uniswap_v2_router.json"),
                    "aerodrome_router": _has("aerodrome_router.json"),
                    "diem": _has("diem.json"),
                }
            except Exception:
                abi = {"erc20": False, "uniswap_v2_router": False, "aerodrome_router": False, "diem": False}

            return {
                "web3": {"rpc_configured": bool(rpc), "chain_id_set": bool(chain_id)},
                "dex": {
                    "providers": dex_providers,
                    "uniswap_v2": {"configured": bool(uni_router), "router": (uni_router or None)},
                    "aerodrome": {"configured": bool(aero_router), "router": (aero_router or None), "stable": bool(aero_stable)},
                },
                "pricing": {
                    "quote_token": (quote_token or None),
                    "diem_token": (diem_token or None),
                    "vvv_token": (vvv_token or None),
                    "trade_path": (trade_path or None),
                },
                "abi": abi,
            }
        except Exception:
            return {
                "web3": {"rpc_configured": False, "chain_id_set": False},
                "dex": {"providers": []},
                "pricing": {},
                "abi": {},
            }

    # Log a concise snapshot at startup to reduce env confusion (no secrets)
    _snap = _env_snapshot_public()
    try:
        logger.info(
            "env.web3 rpc_configured=%s chain_id_set=%s",
            _snap.get("web3", {}).get("rpc_configured"),
            _snap.get("web3", {}).get("chain_id_set"),
        )
        logger.info(
            "env.dex providers=%s uniswap.router=%s aerodrome.router=%s stable=%s",
            ",".join(_snap.get("dex", {}).get("providers", []) or []),
            (_snap.get("dex", {}).get("uniswap_v2", {}) or {}).get("router"),
            (_snap.get("dex", {}).get("aerodrome", {}) or {}).get("router"),
            (_snap.get("dex", {}).get("aerodrome", {}) or {}).get("stable"),
        )
        logger.info(
            "env.pricing quote_token=%s diem_token=%s vvv_token=%s trade_path=%s",
            (_snap.get("pricing", {}) or {}).get("quote_token"),
            (_snap.get("pricing", {}) or {}).get("diem_token"),
            (_snap.get("pricing", {}) or {}).get("vvv_token"),
            (_snap.get("pricing", {}) or {}).get("trade_path"),
        )
        logger.info(
            "env.abi erc20=%s uniswap_v2_router=%s aerodrome_router=%s diem=%s",
            (_snap.get("abi", {}) or {}).get("erc20"),
            (_snap.get("abi", {}) or {}).get("uniswap_v2_router"),
            (_snap.get("abi", {}) or {}).get("aerodrome_router"),
            (_snap.get("abi", {}) or {}).get("diem"),
        )
    except Exception:
        pass

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

    @app.get("/v1/env")
    def env_status() -> dict:
        """Lightweight environment status without secrets.

        Returns booleans/labels only. No tokens or URLs are exposed.
        """
        import os as __os

        # Store backend label
        try:
            _store_backend = "sql" if (_SQLTenantStore is not None and isinstance(store, _SQLTenantStore)) else "json"  # type: ignore[arg-type]
        except Exception:
            _store_backend = "json"

        # KV backend label
        kv_kind = "memory"
        try:
            if (__os.getenv("REDIS_URL") or __os.getenv("KV_REDIS_URL")):
                kv_kind = "redis"
            elif (__os.getenv("KV_URL") or __os.getenv("REPLIT_DB_URL")):
                kv_kind = "replit_db"
        except Exception:
            pass

        # SQL configured/installed (without connecting)
        sql_env = bool(__os.getenv("SQL_DATABASE_URL") or __os.getenv("DATABASE_URL") or __os.getenv("POSTGRES_HOST"))
        sql_pkgs = _SQLTenantStore is not None

        # Metrics backend label
        metrics_kind = "off" if METRICS_BACKEND == "off" else ("starlette" if _using_starlette_exporter else "builtin")

        # Gather recent signal events (best-effort)
        recent_signals: list[dict] = []
        try:
            from libs.telemetry.events import recent as _recent

            ev = _recent(limit=50)
            sig = [e for e in ev if str(e.get("kind", "")).startswith("signal.market.")]
            # Keep only last 5 for readability
            recent_signals = sig[-5:]
        except Exception:
            recent_signals = []

        # Orchestrator dry-run fake price (if provided in environment)
        try:
            dr_fp_env = __os.getenv("DIEM_FAKE_PRICE") or __os.getenv("TEST_DIEM_PRICE")
            dr_fake_price = float(dr_fp_env) if dr_fp_env and str(dr_fp_env).strip() != "" else None
        except Exception:
            dr_fake_price = None

        # Determine if signals are currently using an offline stub
        def _signals_offline(recents: list[dict]) -> bool:
            try:
                for e in reversed(recents):
                    # Prefer signal.market.signals events for offline detection
                    if str(e.get("kind", "")) == "signal.market.signals":
                        vvv = e.get("vvv") or {}
                        diem = e.get("diem") or {}
                        if bool(vvv.get("offline")) or bool(diem.get("offline")):
                            return True
                        # if explicit offline flags are absent, assume online
                        return False
                return False
            except Exception:
                return False

        signals_offline = _signals_offline(recent_signals)

        # Venice config snapshot + readiness (no secrets)
        ven_base = (_os.getenv("VENICE_API_BASE_URL") or "").strip()
        ven_paths = {
            "models": (_os.getenv("VENICE_MODELS_PATH") or "/models"),
            "vvv": (_os.getenv("VENICE_VVV_PATH") or "/vvv"),
            "vvv_circ": (_os.getenv("VENICE_VVV_CIRC_PATH") or "/vvv/circulatingsupply"),
            "vvv_util": (_os.getenv("VENICE_VVV_UTIL_PATH") or "/vvv/utilization"),
            "vvv_yield": (_os.getenv("VENICE_VVV_YIELD_PATH") or "/vvv/staking_yield"),
        }
        ven_key_paths = {
            "create_subkey": (_os.getenv("VENICE_CREATE_SUBKEY_PATH") or "/api_keys"),
            "create_root": (_os.getenv("VENICE_CREATE_ROOT_PATH") or "/api_keys/generate_web3_key"),
            "challenge": (_os.getenv("VENICE_CHALLENGE_PATH") or "/api_keys/generate_web3_key"),
            "revoke": (_os.getenv("VENICE_REVOKE_KEY_PATH") or "/api_keys/{key_id}"),
            "rate_limits": (_os.getenv("VENICE_RATE_LIMITS_PATH") or "/api_keys/rate_limits"),
            "usage": (_os.getenv("VENICE_USAGE_PATH") or "/api_keys/rate_limits/log"),
        }
        ven_key = (_os.getenv("VENICE_API_KEY") or "").strip()
        ven_headers = {"Content-Type": "application/json"}
        if ven_key:
            ven_headers["Authorization"] = f"Bearer {ven_key}"
        models_ok = False
        vvv_ok = False
        models_code: int | None = None
        vvv_code: int | None = None
        vvv_circ_code: int | None = None
        vvv_util_code: int | None = None
        vvv_yield_code: int | None = None
        if ven_base:
            try:
                import requests as _rq

                r = _rq.get(ven_base.rstrip("/") + ven_paths["models"], headers=ven_headers, timeout=3)
                models_ok = bool(r.ok)
                models_code = int(r.status_code)
            except _rq.exceptions.Timeout:
                models_code = 0
                models_ok = False
            except Exception:
                models_code = None
                models_ok = False
            try:
                import requests as _rq2

                r2 = _rq2.get(ven_base.rstrip("/") + ven_paths["vvv"], headers=ven_headers, timeout=3)
                vvv_ok = bool(r2.ok)
                vvv_code = int(r2.status_code)
            except _rq2.exceptions.Timeout:
                vvv_code = 0
                vvv_ok = vvv_ok or False
            except Exception:
                vvv_code = None
                vvv_ok = vvv_ok or False
            # Explicit VVV metrics (any one success implies metrics availability)
            try:
                import requests as _rq4
                r4 = _rq4.get(ven_base.rstrip("/") + ven_paths["vvv_circ"], headers=ven_headers, timeout=3)
                vvv_circ_code = int(r4.status_code)
                vvv_ok = vvv_ok or bool(r4.ok)
            except _rq4.exceptions.Timeout:
                vvv_circ_code = 0
            except Exception:
                vvv_circ_code = None
            try:
                import requests as _rq5
                r5 = _rq5.get(ven_base.rstrip("/") + ven_paths["vvv_util"], headers=ven_headers, timeout=3)
                vvv_util_code = int(r5.status_code)
                vvv_ok = vvv_ok or bool(r5.ok)
            except _rq5.exceptions.Timeout:
                vvv_util_code = 0
            except Exception:
                vvv_util_code = None
            try:
                import requests as _rq6
                r6 = _rq6.get(ven_base.rstrip("/") + ven_paths["vvv_yield"], headers=ven_headers, timeout=3)
                vvv_yield_code = int(r6.status_code)
                vvv_ok = vvv_ok or bool(r6.ok)
            except _rq6.exceptions.Timeout:
                vvv_yield_code = 0
            except Exception:
                vvv_yield_code = None
        ven_ready = bool(models_ok and vvv_ok)
        # Build ready reasons per check
        def _reason(ok: bool, code: int | None) -> str:
            if ok:
                return "ok"
            if not ven_base:
                return "baseUnset"
            if not ven_key:
                # Some deployments may allow anonymous; keep hint minimal
                return "apiKeyMissing"
            if code == 0:
                return "timeout"
            if code is None:
                return "error"
            return f"http:{code}"

        venice_cfg = {
            "baseUrl": ven_base or None,
            "modelsPath": ven_paths["models"],
            "vvvPath": ven_paths["vvv"],
            "offlineSignals": ((_os.getenv("VENICE_OFFLINE_SIGNALS") or "false").strip().lower() in {"1", "true", "yes", "on"}),
            "ready": bool(ven_ready),
            "modelsOk": bool(models_ok),
            "vvvSignalsOk": bool(vvv_ok),
            "readyReason": {
                "models": _reason(models_ok, models_code),
                "vvv": _reason(vvv_ok, vvv_code if vvv_ok else (vvv_circ_code or vvv_util_code or vvv_yield_code)),
            },
            "keyPaths": ven_key_paths,
        }

        # Public env snapshot (web3/dex/pricing/abi)
        snap = _env_snapshot_public()

        return {
            "version": "0.1.0",
            "admin": {
                "token_present": bool(ADMIN_TOKEN),
                "required_at_startup": bool(REQUIRE_ADMIN),
            },
            "store": {
                "backend": _store_backend,
            },
            "kv": {
                "backend": kv_kind,
                "namespace_set": bool(__os.getenv("KV_NAMESPACE")),
                "prefix_set": bool(__os.getenv("KV_PREFIX")),
                "redis_configured": bool(__os.getenv("REDIS_URL") or __os.getenv("KV_REDIS_URL")),
                "replit_db_configured": bool(__os.getenv("KV_URL") or __os.getenv("REPLIT_DB_URL")),
            },
            "limiter": {
                "enabled": bool(RATE_LIMITS_ENABLED),
                "windowSeconds": int(RATE_LIMIT_WINDOW_SECONDS),
                "maxRequests": int(RATE_LIMIT_MAX_REQUESTS),
            },
            "idempotency": {
                "ttlSeconds": int(IDEM_TTL_SECONDS),
                "kv_available": _kv_admin is not None,
            },
            "sql": {
                "env_configured": bool(sql_env),
                "packages_installed": bool(sql_pkgs),
            },
            "metrics": {
                "backend": metrics_kind,
                "path": METRICS_PATH,
            },
            "tracing": {
                "enabled": (_os.getenv("LANGCHAIN_TRACING_V2") or "false").strip().lower() in {"1", "true", "yes"},
            },
            "payments": {
                "enabled": (_os.getenv("PURCHASES_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"},
                "accepted_assets": [a.strip().upper() for a in (_os.getenv("ACCEPT_ASSETS") or "ETH,USDC").split(",") if a.strip()],
                "treasury_address": (_os.getenv("TREASURY_ADDRESS") or "").strip() or None,
                "usdc_address": (_os.getenv("USDC_ADDRESS") or "").strip() or None,
            },
            "features": {
                "quotes": (_os.getenv("QUOTES_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"},
                "purchases": (_os.getenv("PURCHASES_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"},
            },
            "orchestrator": {
                "dryRunFakePrice": dr_fake_price,
            },
            "signals": {
                "recent": recent_signals,
                "offline": bool(signals_offline),
            },
            "venice": venice_cfg,
            "web3": snap.get("web3", {}),
            "dex": snap.get("dex", {}),
            "pricing": snap.get("pricing", {}),
            "abi": snap.get("abi", {}),
        }

    # --- Admin: Venice OpenAPI probe (server-side) ---
    @app.get("/v1/admin/venice/probe")
    @_traceable("broker.venice_probe")
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
        spec = None
        spec_loc = None
        for path in ("/openapi.json", "/api/openapi.json"):
            try:
                r = _rq.get(base_url + path, timeout=timeout)
                if r.ok:
                    spec = r.json()
                    spec_loc = path
                    break
            except Exception:
                continue
        if spec is None:
            raise HTTPException(status_code=502, detail=f"Failed to fetch OpenAPI from {base_url}")

        servers = spec.get("servers") or []
        server_url = None
        if servers and isinstance(servers, list) and isinstance(servers[0], dict):
            server_url = servers[0].get("url")
        if server_url and isinstance(server_url, str):
            if server_url.startswith("http://") or server_url.startswith("https://"):
                recommended_base = server_url.rstrip("/")
            else:
                recommended_base = base_url + ("/" + server_url.lstrip("/"))
        else:
            recommended_base = base_url if spec_loc == "/openapi.json" else base_url + "/api"

        paths = spec.get("paths") or {}
        def _first_present(cands: list[str]) -> str | None:
            for c in cands:
                if c in paths:
                    return c
            return None

        subkey_path = _first_present(["/api_keys", "/v1/keys/sub", "/v1/keys/subkey"]) or "/api_keys"
        root_path = _first_present(["/api_keys/generate_web3_key", "/v1/keys/generate_web3_key"]) or "/api_keys/generate_web3_key"
        # Signals endpoints vary; prefer explicit VVV metrics
        vvv_path = "/vvv" if "/vvv" in paths else ("/signals/vvv" if "/signals/vvv" in paths else "/vvv")
        vvv_circ = "/vvv/circulatingsupply" if "/vvv/circulatingsupply" in paths else None
        vvv_util = "/vvv/utilization" if "/vvv/utilization" in paths else None
        vvv_yield = "/vvv/staking_yield" if "/vvv/staking_yield" in paths else None

        return {
            "inputBase": base_url,
            "specLocation": spec_loc,
            "recommended": {
                "VENICE_API_BASE_URL": recommended_base,
                "VENICE_CREATE_SUBKEY_PATH": subkey_path,
                "VENICE_CREATE_ROOT_PATH": root_path,
                "VENICE_VVV_PATH": vvv_path,
                **({"VENICE_VVV_CIRC_PATH": vvv_circ} if vvv_circ else {}),
                **({"VENICE_VVV_UTIL_PATH": vvv_util} if vvv_util else {}),
                **({"VENICE_VVV_YIELD_PATH": vvv_yield} if vvv_yield else {}),
            },
        }

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

    @app.post("/v1/admin/compact-counters")
    @_traceable("broker.admin_compact_counters")
    def admin_compact_counters(
        minutes: int = Query(default=60, ge=1, le=1440, description="How many minutes back to scan for buckets when prefix listing isn't available"),
        delete_after: bool = Query(default=False, description="Delete KV buckets after successful upsert"),
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        """Admin-only: compact KV limiter buckets into SQL Counter rows.

        Runs in-process, ensuring KV visibility even when using in-memory or
        Replit DB without prefix listing. Intended for Replit/Web Service ops.
        """
        _require_admin(authorization)
        # Ensure SQL dependencies are available
        try:
            from sqlmodel import Session, select  # type: ignore
            from db.session import get_engine, create_db_and_tables
            from db.models import Counter, Tenant as DbTenant
            import json as _json2
            import time as _time2
            import re as _re2
            from datetime import datetime as _dt2
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"SQL dependencies unavailable: {e}")

        # Ensure tables exist
        try:
            create_db_and_tables()
        except Exception:
            pass

        # Prefer existing KV instance; otherwise construct one
        kv = _kv_admin
        if kv is None:
            try:
                from libs.kv import KVStore as _KV2

                kv = _KV2()
            except Exception as e:  # noqa: BLE001
                raise HTTPException(status_code=503, detail=f"KV unavailable: {e}")

        engine = get_engine()

        # Try prefix listing first
        keys = []  # type: ignore[var-annotated]
        try:
            keys = kv.keys("rl:tenant:")  # type: ignore[attr-defined]
        except Exception:
            keys = []

        examined = 0
        inserted = 0
        updated = 0

        # If listing failed/empty, scan recent windows for known tenants
        if not keys:
            try:
                tenant_ids = list(store.all().keys())
            except Exception:
                tenant_ids = []
            # Fallback: load from SQL tenant table if present
            if not tenant_ids:
                try:
                    with Session(engine) as _s:  # type: ignore[call-arg]
                        tenant_ids = [row.id for row in _s.exec(select(DbTenant)).all()]
                except Exception:
                    tenant_ids = []
            win_s = int(RATE_LIMIT_WINDOW_SECONDS or 60)
            now = int(_time2.time())
            start = now - int(minutes) * 60
            start = (start // win_s) * win_s
            cand: list[str] = []
            for tid in tenant_ids:
                tcur = start
                while tcur <= now:
                    cand.append(f"rl:tenant:{tid}:chat:{tcur}")
                    tcur += win_s
            keys = cand

        # Upsert counters
        pat = _re2.compile(r"^rl:tenant:(?P<tenant>[^:]+):chat:(?P<bucket>\d+)$")
        with Session(engine) as s:  # type: ignore[call-arg]
            for k in keys:
                examined += 1
                m = pat.match(k)
                if not m:
                    continue
                tenant_id = m.group("tenant")
                bucket_s = int(m.group("bucket"))
                try:
                    raw = kv.get(k)  # type: ignore[attr-defined]
                except Exception:
                    raw = None
                try:
                    count = int(raw) if raw is not None else 0
                except Exception:
                    count = 0
                if count <= 0:
                    continue
                # Determine windowSeconds from per-tenant override if set
                win_s = int(RATE_LIMIT_WINDOW_SECONDS or 60)
                try:
                    limits_raw = kv.get(f"broker:tenant:{tenant_id}:limits")  # type: ignore[attr-defined]
                    if limits_raw:
                        obj = _json2.loads(limits_raw)
                        win_s = int(obj.get("windowSeconds", win_s))
                except Exception:
                    pass
                bucket_dt = _dt2.utcfromtimestamp(bucket_s)
                existing = s.exec(
                    select(Counter).where(
                        Counter.tenant_id == tenant_id,  # type: ignore[comparison-overlap]
                        Counter.scope == "chat",
                        Counter.bucket_start == bucket_dt,
                        Counter.bucket_seconds == int(win_s),
                        Counter.model == None,  # noqa: E711
                    )
                ).first()
                if existing is None:
                    rec = Counter(
                        tenant_id=tenant_id,
                        scope="chat",
                        model=None,
                        bucket_start=bucket_dt,
                        bucket_seconds=int(win_s),
                        count=int(count),
                    )
                    s.add(rec)
                    inserted += 1
                else:
                    if int(count) > int(existing.count):
                        existing.count = int(count)
                        updated += 1
                try:
                    s.commit()
                except Exception as _e:  # noqa: BLE001
                    s.rollback()
                    logger.warning("compact: upsert failed for %s: %s", k, _e)
                    continue
                if delete_after:
                    try:
                        kv.delete(k)  # type: ignore[attr-defined]
                    except Exception:
                        pass

        return {
            "examined": examined,
            "inserted": inserted,
            "updated": updated,
            "scanMinutes": int(minutes),
            "windowSeconds": int(RATE_LIMIT_WINDOW_SECONDS or 60),
        }

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

        # Determine model: request > env defaults. If still unset, allow
        # calling the Venice client without specifying a model to support
        # deployments/tests where the SDK/server has its own default.
        _def_model = (_os.getenv("BROKER_DEFAULT_MODEL") or _os.getenv("VENICE_DEFAULT_MODEL") or "").strip()
        _model = payload.model or (_def_model if _def_model else None)

        sub_client = VeniceClient(api_key=t.subkey, base_url=client.config.base_url)
        try:
            if _model:
                res = sub_client.chat_completions(messages=payload.messages, model=_model)
            else:
                res = sub_client.chat_completions(messages=payload.messages)
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

    # --- Tenant self-service: view/update broker limits (restrictive only) ---
    @app.get("/v1/me/broker-limits")
    @_traceable("broker.me_broker_limits")
    def my_broker_limits(
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        role, tenant_ctx = _auth_context(authorization)
        if role != "tenant" or tenant_ctx is None:
            raise HTTPException(status_code=403, detail="tenant auth required")
        return _get_broker_limits_obj(tenant_ctx.id)

    @app.post("/v1/me/broker-limits")
    @_traceable("broker.me_set_broker_limits")
    def my_set_broker_limits(
        limits: BrokerLimits,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ) -> dict:
        role, tenant_ctx = _auth_context(authorization)
        if role != "tenant" or tenant_ctx is None:
            raise HTTPException(status_code=403, detail="tenant auth required")
        # KV is required for persistence
        if _kv_admin is None:
            raise HTTPException(status_code=503, detail="KV store unavailable")
        current = _get_broker_limits_obj(tenant_ctx.id)
        new_obj = dict(current)
        # Allow only more restrictive settings by tenant:
        # - windowSeconds: can increase (longer window reduces rate) but not decrease below current
        # - maxRequests: can decrease but not increase above current
        # - label: only allow self-annotation with prefix 'self:'
        if limits.windowSeconds is not None:
            try:
                ws = int(limits.windowSeconds)
                if ws < 1:
                    raise ValueError
            except Exception:
                raise HTTPException(status_code=422, detail="invalid windowSeconds")
            if ws < int(current.get("windowSeconds", ws)):
                raise HTTPException(status_code=403, detail="cannot decrease windowSeconds (admin only)")
            new_obj["windowSeconds"] = ws
        if limits.maxRequests is not None:
            try:
                mr = int(limits.maxRequests)
                if mr < 0:
                    raise ValueError
            except Exception:
                raise HTTPException(status_code=422, detail="invalid maxRequests")
            if mr > int(current.get("maxRequests", mr)):
                raise HTTPException(status_code=403, detail="cannot increase maxRequests (admin only)")
            new_obj["maxRequests"] = mr
        if limits.label is not None:
            # Guardrails: tenants may only write self-annotation labels prefixed with 'self:'
            lb = str(limits.label).strip()
            if not lb.startswith("self:"):
                raise HTTPException(status_code=403, detail="label must start with 'self:' for tenant updates")
            new_obj["label"] = lb
        try:
            import json as _json

            _kv_admin.set(f"broker:tenant:{tenant_ctx.id}:limits", _json.dumps(new_obj))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"failed to update limits: {e}")
        return new_obj

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
            # Builtin HTTP metrics
            text = _metrics.render_prom()
            # Append agent metrics if available
            try:
                from libs.telemetry.metrics import render_prom as _render_agents

                text = text + _render_agents()
            except Exception:
                pass
            return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    # --- Admin: per-tenant broker limits (caps/labels) ---

    # --- Market data (prices/signals) ---
    @app.get("/v1/market/prices")
    @_traceable("broker.market_prices")
    def market_prices(
        symbols: str = Query(default="VVV,DIEM,ETH,USDC", description="Comma-separated symbols"),
    ) -> dict:
        """Return simple price feed and common ratios.

        Prices are sourced via services.marketdata.provider (DEX aggregator + Venice signals).
        Ratios are convenience fields, e.g., VVV_DIEM = VVV/DIEM when both are present.
        """
        try:
            from services.marketdata.provider import MarketDataProvider  # type: ignore

            md = MarketDataProvider()
            syms = [s.strip() for s in (symbols or "").split(",") if s.strip()]
            prices = md.prices(syms)
            # Common ratios to DIEM if available
            ratios: dict[str, float] = {}
            diem = prices.get("DIEM")
            if diem and float(diem) != 0:
                for s, p in prices.items():
                    if s.upper() != "DIEM":
                        try:
                            ratios[f"{s.upper()}_DIEM"] = float(p) / float(diem)
                        except Exception:
                            pass
            return {"symbols": syms, "prices": prices, "ratios": ratios}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/v1/market/signals")
    @_traceable("broker.market_signals")
    def market_signals(ttl_s: int = Query(default=30, ge=1, le=600)) -> dict:
        """Return unified VVV + DIEM signals from Venice-backed provider."""
        try:
            from services.marketdata.provider import MarketDataProvider  # type: ignore

            md = MarketDataProvider()
            return md.unified_signals(ttl_s=int(ttl_s))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(e))

    # --- Token snapshots (BaseScan/Etherscan watcher) ---
    from pydantic import BaseModel as _BM

    class TokenSummary(_BM):
        address: str
        chain: str | None = None
        symbol: str | None = None
        name: str | None = None
        decimals: int | None = None
        lastTs: str | None = None
        priceUsd: float | None = None
        holders: int | None = None
        transfers24h: int | None = None
        marketcapUsd: float | None = None

    @app.get("/v1/market/tokens", response_model=list[TokenSummary])
    @_traceable("broker.market_tokens")
    def market_tokens() -> list[TokenSummary]:
        try:
            from sqlmodel import Session, select
            from db.session import get_engine
            from db.models import AssetToken, TokenSnapshot
        except Exception as _e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"SQL dependencies unavailable: {_e}")

        engine = get_engine()
        out: list[TokenSummary] = []
        with Session(engine) as s:  # type: ignore[call-arg]
            tokens = s.exec(select(AssetToken)).all()
            for t in tokens:
                snap = s.exec(
                    select(TokenSnapshot)
                    .where(TokenSnapshot.token_address == t.address)
                    .order_by(TokenSnapshot.ts.desc())
                    .limit(1)
                ).first()
                out.append(
                    TokenSummary(
                        address=t.address,
                        chain=getattr(t, "chain", None),
                        symbol=getattr(t, "symbol", None),
                        name=getattr(t, "name", None),
                        decimals=getattr(t, "decimals", None),
                        lastTs=(snap.ts.isoformat() + "Z") if snap and snap.ts else None,
                        priceUsd=getattr(snap, "price_usd", None) if snap else None,
                        holders=getattr(snap, "holders", None) if snap else None,
                        transfers24h=getattr(snap, "transfers_24h", None) if snap else None,
                        marketcapUsd=getattr(snap, "marketcap_usd", None) if snap else None,
                    )
                )
        return out

    class TokenHistoryPoint(_BM):
        ts: str
        priceUsd: float | None = None
        holders: int | None = None
        transfers24h: int | None = None
        marketcapUsd: float | None = None

    @app.get("/v1/market/token/{address}/history", response_model=list[TokenHistoryPoint])
    @_traceable("broker.market_token_history")
    def market_token_history(
        address: str,
        since: str | None = Query(default=None, description="ISO8601 or epoch seconds"),
        until: str | None = Query(default=None, description="ISO8601 or epoch seconds"),
        limit: int = Query(default=500, ge=1, le=5000),
        asc: bool = Query(default=True),
    ) -> list[TokenHistoryPoint]:
        try:
            from sqlmodel import Session, select
            from sqlalchemy import desc as _desc
            from datetime import datetime as _dt, timedelta as _td
            from db.session import get_engine
            from db.models import TokenSnapshot
        except Exception as _e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"SQL dependencies unavailable: {_e}")

        # Default window: last 24h if since is not provided
        if not since:
            try:
                since_dt = _dt.utcnow() - _td(hours=24)
                since = since_dt.isoformat() + "Z"
            except Exception:
                pass

        def _as_dt(val: str | None):
            if not val:
                return None
            try:
                return _parse_dt(val)  # type: ignore[name-defined]
            except Exception:
                return None

        since_dt = _as_dt(since)
        until_dt = _as_dt(until)

        engine = get_engine()
        with Session(engine) as s:  # type: ignore[call-arg]
            q = select(TokenSnapshot).where(TokenSnapshot.token_address == address)
            if since_dt is not None:
                q = q.where(TokenSnapshot.ts >= since_dt)
            if until_dt is not None:
                q = q.where(TokenSnapshot.ts <= until_dt)
            q = q.order_by(TokenSnapshot.ts if asc else _desc(TokenSnapshot.ts)).limit(int(limit))
            rows = s.exec(q).all()

        out: list[TokenHistoryPoint] = []
        for r in rows:
            out.append(
                TokenHistoryPoint(
                    ts=(r.ts.isoformat() + "Z"),
                    priceUsd=r.price_usd,
                    holders=r.holders,
                    transfers24h=r.transfers_24h,
                    marketcapUsd=r.marketcap_usd,
                )
            )
        return out

    # --- Quotes & Purchases (flag-gated; non-admin) ---
    try:
        _features = {
            "quotes": (_os.getenv("QUOTES_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"},
            "purchases": (_os.getenv("PURCHASES_ENABLED") or "false").strip().lower() in {"1", "true", "yes", "on"},
        }
        if _features["quotes"]:
            from services.pricing.service import PricingService  # type: ignore
            _pricing = PricingService()

            class QuoteResponse(BaseModel):
                quoteId: str
                units: int
                asset: str
                unitPrice: int
                totalPrice: int
                acceptedMin: int | None = None
                acceptedMax: int | None = None
                expiresAt: int

            @app.get("/v1/quotes", response_model=QuoteResponse)
            def get_quote(
                units: int = Query(..., ge=1),
                asset: str = Query(..., description="ETH or USDC"),
            ) -> dict:
                try:
                    return _pricing.get_quote(units=units, asset=asset)
                except Exception as e:  # noqa: BLE001
                    raise HTTPException(status_code=400, detail=str(e))

        if _features["purchases"]:
            from db.session import get_session
            from db.models import Purchase, Quote as DbQuote
            # sqlmodel is optional at runtime; guard import to avoid startup failure
            try:
                from sqlmodel import select as _select  # type: ignore
                _has_sqlmodel_p = True
            except Exception:
                _select = None  # type: ignore
                _has_sqlmodel_p = False
            from datetime import datetime as _dt
            import hashlib as _hh

            class PurchaseVerifyRequest(BaseModel):
                quoteId: str
                txHash: str
                buyerAddress: str
                tenantId: str | None = None
                model: str | None = None

            class PurchaseStatus(BaseModel):
                purchaseId: str
                status: str
                tenantId: str | None = None
                subkey: str | None = None
                expiresAt: str | None = None

            def _normalize_hex(x: str) -> str:
                x = str(x).strip()
                return x if x.startswith("0x") else ("0x" + x)

            def _wei(hex_str: str) -> int:
                return int(hex_str, 16)

            def _rpc_call(url: str, method: str, params: list) -> dict:
                import requests as _rq

                r = _rq.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
                r.raise_for_status()
                j = r.json()
                if "error" in j:
                    raise RuntimeError(str(j["error"]))
                return j["result"]

            def _verify_tx(quote: DbQuote, tx_hash: str, treasury: str, usdc_addr: str | None, base_rpc: str) -> tuple[int, dict]:
                tx_hash = _normalize_hex(tx_hash)
                rec = _rpc_call(base_rpc, "eth_getTransactionReceipt", [tx_hash])
                if rec is None:
                    raise RuntimeError("transaction not found")
                status_hex = rec.get("status") or "0x0"
                if int(status_hex, 16) != 1:
                    raise RuntimeError("transaction failed")
                to_addr = (rec.get("to") or "").lower()
                # ETH path: check 'to' and value
                if quote.asset.upper() == "ETH":
                    tx = _rpc_call(base_rpc, "eth_getTransactionByHash", [tx_hash])
                    if (tx.get("to") or "").lower() != treasury.lower():
                        raise RuntimeError("ETH payment to wrong address")
                    val = _wei(tx.get("value", "0x0"))
                    if val < int(quote.total_price):
                        raise RuntimeError("insufficient ETH amount")
                    details = {
                        "method": "ETH",
                        "to": tx.get("to"),
                        "from": tx.get("from"),
                        "blockNumber": rec.get("blockNumber"),
                        "value": str(val),
                    }
                    return val, details
                # USDC path: find Transfer(to=treasury)
                if quote.asset.upper() == "USDC":
                    if not usdc_addr:
                        raise RuntimeError("USDC_ADDRESS not configured")
                    logs = rec.get("logs", [])
                    sig = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"  # keccak(Transfer(address,address,uint256))
                    paid = 0
                    matches: list[dict] = []
                    for lg in logs:
                        if (lg.get("address") or "").lower() != usdc_addr.lower():
                            continue
                        topics = lg.get("topics") or []
                        if not topics or topics[0].lower() != sig:
                            continue
                        # topics[2] is to address (indexed)
                        if len(topics) < 3:
                            continue
                        to_topic = topics[2]
                        # last 20 bytes
                        to_addr_log = "0x" + to_topic[-40:]
                        if to_addr_log.lower() != treasury.lower():
                            continue
                        amount = _wei(lg.get("data", "0x0"))
                        paid += amount
                        matches.append({"amount": str(amount), "index": lg.get("logIndex")})
                    if paid < int(quote.total_price):
                        raise RuntimeError("insufficient USDC amount")
                    details = {
                        "method": "USDC",
                        "to": treasury,
                        "blockNumber": rec.get("blockNumber"),
                        "matches": matches,
                        "value": str(paid),
                    }
                    return paid, details
                raise RuntimeError("unsupported asset")

            @app.post("/v1/purchases/verify", response_model=PurchaseStatus)
            def verify_purchase(req: PurchaseVerifyRequest) -> dict:
                if not _has_sqlmodel_p:
                    raise HTTPException(status_code=503, detail="SQL dependencies unavailable")
                base_rpc = (_os.getenv("BASE_RPC_URL") or "").strip()
                if not base_rpc:
                    raise HTTPException(status_code=400, detail="BASE_RPC_URL not set")
                treasury = (_os.getenv("TREASURY_ADDRESS") or "").strip()
                if not treasury:
                    raise HTTPException(status_code=400, detail="TREASURY_ADDRESS not set")
                usdc_addr = (_os.getenv("USDC_ADDRESS") or "").strip() or None

                # Load quote
                with next(get_session()) as s:  # type: ignore[call-arg]
                    q = s.exec(_select(DbQuote).where(DbQuote.quote_id == req.quoteId)).first()  # type: ignore[misc]
                    if q is None:
                        raise HTTPException(status_code=404, detail="quote not found")
                    # Verify payment
                    try:
                        paid_val, receipt = _verify_tx(q, req.txHash, treasury, usdc_addr, base_rpc)
                    except Exception as e:  # noqa: BLE001
                        raise HTTPException(status_code=400, detail=str(e))
                    # Record/Upsert purchase
                    pur_id = _hh.sha256(f"{req.txHash}:{req.buyerAddress}".encode()).hexdigest()[:16]
                    p = s.exec(_select(Purchase).where(Purchase.purchase_id == pur_id)).first()
                    if p is None:
                        p = Purchase(
                            purchase_id=pur_id,
                            quote_id=q.quote_id,
                            buyer_address=req.buyerAddress,
                            asset=q.asset,
                            amount_paid=int(paid_val or q.total_price),
                            tx_hash=req.txHash,
                            status="confirmed",
                        )
                        s.add(p)
                    else:
                        p.status = "confirmed"
                        p.amount_paid = int(paid_val or q.total_price)
                    # Attach audit receipt JSON (best-effort)
                    try:
                        import json as _json2
                        p.receipt = _json2.dumps(
                            {
                                "txHash": req.txHash,
                                "network": (_os.getenv("NETWORK_ID") or "base-mainnet"),
                                "asset": q.asset,
                                "amountPaid": int(paid_val or q.total_price),
                                "quote": {
                                    "quoteId": q.quote_id,
                                    "units": int(q.units),
                                    "unitPrice": int(q.unit_price),
                                    "totalPrice": int(q.total_price),
                                },
                                "verification": receipt,
                                "verifiedAt": int(time.time()),
                            }
                        )
                    except Exception:
                        pass
                    # Mint subkey and upsert tenant
                    try:
                        # Map units → consumption limit and expiry (24h default)
                        limit_kind = (_os.getenv("PURCHASE_UNITS_KIND") or "diem").strip().lower()
                        cons = {"diem": int(q.units)} if limit_kind == "diem" else {limit_kind: int(q.units)}
                        expires_at = _dt.utcfromtimestamp(int(time.time()) + 24 * 3600)
                        sub = keys.issue_scoped_key(
                            (_os.getenv("VENICE_PARENT_KEY") or _os.getenv("VENICE_API_KEY") or ""),
                            label=f"Buyer {req.buyerAddress[:6]}...",
                            consumption_limit=cons,
                            expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        )
                        # Extract values
                        def _extract(obj: dict, keys: list[str]) -> str:
                            for k in keys:
                                v = obj.get(k)
                                if isinstance(v, str) and v:
                                    return v
                            return ""

                        subkey = _extract(sub, ["apiKey", "api_key", "key", "token", "api_key_value"]) or ""
                        kid = _extract(sub, ["id", "keyId", "apiKeyId", "api_key_id"]) or None
                        if not subkey:
                            raise RuntimeError("failed to mint subkey")
                        # Choose tenant id: reuse provided or derive from wallet address
                        tenant_id = req.tenantId or ("w:" + req.buyerAddress.lower())
                        store.upsert(Tenant(id=tenant_id, label="Buyer", subkey=subkey, quota=q.units, expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ")))
                        p.status = "fulfilled"
                        p.tenant_id = tenant_id
                        p.subkey = subkey
                        p.key_id = kid
                        p.expires_at = expires_at
                        p.fulfilled_at = _dt.utcnow()
                    except Exception as e:  # noqa: BLE001
                        # Keep as confirmed; fulfill later
                        p.status = "confirmed"
                    s.add(p)
                    s.commit()
                    out = {
                        "purchaseId": p.purchase_id,
                        "status": p.status,
                        "tenantId": p.tenant_id,
                        "subkey": p.subkey,
                        "expiresAt": p.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if p.expires_at else None,
                    }
                    # Emit audit event (best-effort)
                    try:
                        from libs.telemetry.events import emit as _emit

                        _emit("purchase.verified", {**out, "txHash": req.txHash, "asset": q.asset, "units": int(q.units)})
                    except Exception:
                        pass
                    return out

            @app.get("/v1/purchases/{purchase_id}", response_model=PurchaseStatus)
            def get_purchase(purchase_id: str) -> dict:
                if not _has_sqlmodel_p:
                    raise HTTPException(status_code=503, detail="SQL dependencies unavailable")
                with next(get_session()) as s:  # type: ignore[call-arg]
                    p = s.exec(_select(Purchase).where(Purchase.purchase_id == purchase_id)).first()
                    if p is None:
                        raise HTTPException(status_code=404, detail="purchase not found")
                    return {
                        "purchaseId": p.purchase_id,
                        "status": p.status,
                        "tenantId": p.tenant_id,
                        "subkey": p.subkey,
                        "expiresAt": p.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if p.expires_at else None,
                    }

        # Admin listings (quotes, purchases, utilization)
        try:
            from sqlmodel import select as _select_all  # type: ignore
            _has_sqlmodel = True
        except Exception:
            _select_all = None  # type: ignore
            _has_sqlmodel = False

        if _features["quotes"] or _features["purchases"]:
            if _has_sqlmodel:
                from db.session import get_session as _get_sess
                from db.models import Quote as _Q, Purchase as _P

                @app.get("/v1/admin/quotes")
                def admin_quotes(
                    limit: int = Query(default=50, ge=1, le=500),
                    status: str | None = Query(default=None),
                    authorization: str | None = Header(default=None, alias="Authorization"),
                ) -> list[dict]:
                    _require_admin(authorization)
                    with next(_get_sess()) as s:  # type: ignore[call-arg]
                        stmt = _select_all(_Q).order_by(_Q.created_at.desc()).limit(int(limit))  # type: ignore[misc]
                        rows = s.exec(stmt).all()
                        out = []
                        for r in rows:
                            if status and r.status != status:
                                continue
                            out.append({
                                "quoteId": r.quote_id,
                                "units": int(r.units),
                                "asset": r.asset,
                                "unitPrice": int(r.unit_price),
                                "totalPrice": int(r.total_price),
                                "status": r.status,
                                "expiresAt": r.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.expires_at else None,
                                "createdAt": r.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if r.created_at else None,
                            })
                        return out

                @app.get("/v1/admin/purchases")
                def admin_purchases(
                    limit: int = Query(default=50, ge=1, le=500),
                    status: str | None = Query(default=None),
                    authorization: str | None = Header(default=None, alias="Authorization"),
                ) -> list[dict]:
                    _require_admin(authorization)
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

                @app.get("/v1/admin/utilization")
                def admin_utilization(
                    minutes: int = Query(default=1440, ge=1, le=10080),
                    authorization: str | None = Header(default=None, alias="Authorization"),
                ) -> dict:
                    _require_admin(authorization)
                    try:
                        from db.models import Counter as _C
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
            else:
                # Define no-op admin endpoints so the UI doesn't 404 when SQLModel is unavailable.
                @app.get("/v1/admin/quotes")
                def admin_quotes_stub(
                    limit: int = Query(default=50, ge=1, le=500),
                    status: str | None = Query(default=None),
                    authorization: str | None = Header(default=None, alias="Authorization"),
                ) -> list[dict]:
                    _require_admin(authorization)
                    return []

                @app.get("/v1/admin/purchases")
                def admin_purchases_stub(
                    limit: int = Query(default=50, ge=1, le=500),
                    status: str | None = Query(default=None),
                    authorization: str | None = Header(default=None, alias="Authorization"),
                ) -> list[dict]:
                    _require_admin(authorization)
                    return []

                @app.get("/v1/admin/utilization")
                def admin_utilization_stub(
                    minutes: int = Query(default=1440, ge=1, le=10080),
                    authorization: str | None = Header(default=None, alias="Authorization"),
                ) -> dict:
                    _require_admin(authorization)
                    return {"minutes": int(minutes), "total": 0}
    except Exception as _e_features:  # noqa: BLE001
        logger.warning("features init error: %s", _e_features)

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
        "FastAPI not available. Install 'fastapi pydantic uvicorn' to run Broker API. (%s)", e)

    def main() -> None:  # noqa: D401
        """Run info for environments without FastAPI installed."""
        print("Install fastapi, pydantic, uvicorn; then run:")
        print("  uvicorn app:app --app-dir apps/broker-api --reload")
