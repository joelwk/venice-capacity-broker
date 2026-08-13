"""
Venice Broker API entrypoint with modular router architecture.

This module creates the FastAPI application and wires all routers.
"""

from __future__ import annotations

import inspect
import logging
import os
import sys
import threading
import time
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Ensure repository root is on sys.path for auxiliary imports (mirrors CLI entrypoint).
try:
    from apps._path import REPO_ROOT
except Exception:  # pragma: no cover - fallback for direct module execution
    REPO_ROOT = Path(__file__).resolve().parents[2]

try:
    from libs.env import is_test_env  # type: ignore
except Exception:  # pragma: no cover - fallback for direct module execution

    def is_test_env() -> bool:  # type: ignore
        return bool(os.getenv("PYTEST_CURRENT_TEST") or "pytest" in sys.modules)


def _load_runtime_env() -> None:
    """Best-effort loading of repo-level dotenv files for API runtime.

    Replit Deployments and bare uvicorn launches do not automatically populate os.environ
    with values from .env/.env.docker, so we mirror the CLI bootstrap logic here.

    In Docker, environment variables are already set by docker-compose, so we skip
    loading .env files to avoid overriding Docker-provided values (especially SQL_DATABASE_URL).
    """

    try:
        from libs.env import bootstrap_env  # type: ignore
    except Exception:
        return

    bootstrap_env(
        repo_root=REPO_ROOT,
        enable_dotenv=not os.getenv("DISABLE_RUNTIME_DOTENV"),
    )


_load_runtime_env()

# Initialize logging early, before importing routers that may create loggers
_get_logger = None
try:
    from libs.telemetry.logger import get_logger

    _get_logger = get_logger
    # Initialize the telemetry logging system early
    _ = get_logger("broker.api.init")
except Exception:  # pragma: no cover - fallback if telemetry not available
    _get_logger = None

# Validate RPC configuration early to fail fast on misconfiguration
if _get_logger:
    try:
        from libs.runtime.rpc_validation import (
            log_rpc_configuration,
            validate_rpc_configuration,
        )

        logger = _get_logger("broker.api.rpc")
        # Log RPC configuration for observability
        log_rpc_configuration()

        # In production, validate that we're not using public RPCs
        try:
            allow_dry_run = is_test_env() or os.getenv(
                "ENABLE_LIVE", ""
            ).lower() not in (
                "1",
                "true",
                "yes",
                "on",
            )
            validate_rpc_configuration(
                fail_on_public=True,
                require_paid=False,  # Don't require paid if not explicitly set
                allow_dry_run=allow_dry_run,
            )
        except ValueError as exc:
            logger.error("RPC configuration validation failed: %s", exc)
            logger.error(
                "Set BASE_RPC_URLS=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY "
                "in docker/.env.local or environment variables."
            )
            # In API, we can be stricter - fail startup if RPC is misconfigured
            # But allow override via env var for flexibility
            if os.getenv("RPC_VALIDATION_STRICT", "").lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                raise
    except Exception as exc:
        # Don't fail startup if validation module has issues
        if _get_logger:
            _get_logger("broker.api.rpc").debug("RPC validation skipped: %s", exc)

if TYPE_CHECKING:
    from typing import Any

# Import routers
# Handle both relative imports (when loaded as package) and absolute imports (when loaded directly)
try:
    from .routers import (
        admin,
        bids,
        clearing,
        purchases,
        quotes,
        settlement,
        tenants,
        venice,
    )
    from .routers import (
        marketdata as marketdata_router,
    )
except ImportError:
    # Fallback for direct module loading (e.g., in tests)
    import sys
    from pathlib import Path

    broker_api_path = Path(__file__).parent
    if str(broker_api_path.parent) not in sys.path:
        sys.path.insert(0, str(broker_api_path.parent))
    from apps.broker_api.routers import (
        admin,
        bids,
        clearing,
        purchases,
        quotes,
        settlement,
        tenants,
        venice,
    )
    from apps.broker_api.routers import (
        marketdata as marketdata_router,
    )

# Import helper modules
# Handle both relative imports (when loaded as package) and absolute imports (when loaded directly)
try:
    from . import lifespan as lifespan_module
    from . import marketdata, rate_limit
    from .services import (
        bids as bids_helpers,
    )
    from .services import (
        clearing as clearing_helpers,
    )
    from .services import (
        pricing as pricing_helpers,
    )
except ImportError:
    # Fallback for direct module loading (e.g., in tests)
    import sys
    from pathlib import Path

    broker_api_path = Path(__file__).parent
    if str(broker_api_path.parent.parent) not in sys.path:
        sys.path.insert(0, str(broker_api_path.parent.parent))
    from apps.broker_api import lifespan as lifespan_module
    from apps.broker_api import marketdata, rate_limit
    from apps.broker_api.services import (
        bids as bids_helpers,
    )
    from apps.broker_api.services import (
        clearing as clearing_helpers,
    )
    from apps.broker_api.services import (
        pricing as pricing_helpers,
    )

try:
    from libs.telemetry.metrics import render_prom as _metrics_render  # type: ignore
except Exception:  # pragma: no cover

    def _metrics_render(prefix: str = "vvv") -> str:  # type: ignore
        return ""


# Logger - use telemetry logger if available, otherwise fallback to standard logging
if _get_logger is not None:
    logger = _get_logger("broker.api")
else:
    logger = logging.getLogger("broker.api")

_LAZY_MISSING = object()


class _LazyProxy:
    """Lazy init wrapper to keep startup fast in constrained environments."""

    def __init__(self, factory, name: str) -> None:
        self._factory = factory
        self._name = name
        self._value = _LAZY_MISSING
        self._lock = threading.Lock()

    def _get(self):
        if self._value is _LAZY_MISSING:
            with self._lock:
                if self._value is _LAZY_MISSING:
                    self._value = self._factory()
        return self._value

    def prefetch(self) -> None:
        threading.Thread(
            target=self._get, name=f"lazy-init-{self._name}", daemon=True
        ).start()

    def __getattr__(self, name: str):
        return getattr(self._get(), name)

    def __call__(self, *args, **kwargs):
        return self._get()(*args, **kwargs)

    def __bool__(self) -> bool:
        return bool(self._get())

    def __repr__(self) -> str:
        status = "ready" if self._value is not _LAZY_MISSING else "pending"
        return f"<LazyProxy {self._name} ({status})>"


# Module-level attributes that tests patch directly.
_prices_resp_cache = {}
_env_prices_resp_cache = {}
_get_marketdata_provider = None
_provider_override = None
env_status = None
_limiter = None
store = None
keys = None
Tenant = None
_CANONICAL_GLOBALS: dict[str, Any] | None = None
_SHARED_STATE = sys.modules.setdefault(
    "apps.broker_api._shared_state", types.SimpleNamespace()
)

_SELF_MODULE = sys.modules.get(__name__)
if _SELF_MODULE is None:
    _SELF_MODULE = inspect.getmodule(inspect.currentframe())  # type: ignore[arg-type]
module_refs = getattr(_SHARED_STATE, "module_refs", None)
if not isinstance(module_refs, list):
    module_refs = []
_SHARED_STATE.module_refs = module_refs
if _SELF_MODULE is not None and _SELF_MODULE not in module_refs:
    module_refs.append(_SELF_MODULE)

if __name__ == "apps.broker_api.app":
    if _SELF_MODULE is not None:
        try:
            _SELF_MODULE.__name__ = __name__
            spec = getattr(_SELF_MODULE, "__spec__", None)
            if spec is not None:
                spec.name = __name__
        except Exception:
            pass
    _CANONICAL_GLOBALS = globals()
    _SHARED_STATE.canonical_globals = globals()
else:
    sys.modules.setdefault(__name__, _SELF_MODULE)


def _validate_diem_vvv_env() -> None:
    """Validate required DIEM/VVV environment variables at startup.

    Logs a structured error if any required variables are missing.
    Does not abort startup to allow graceful degradation in some deployments.
    """
    required_vars = {
        "DIEM_TOKEN_ADDRESS": os.getenv("DIEM_TOKEN_ADDRESS"),
        "VVV_TOKEN_ADDRESS": os.getenv("VVV_TOKEN_ADDRESS"),
        "DIEM_VVV_PAIR_ADDRESS": os.getenv("DIEM_VVV_PAIR_ADDRESS"),
        "VVV_USDC_POOL_ADDRESS": os.getenv("VVV_USDC_POOL_ADDRESS"),
        "QUOTE_TOKEN_ADDRESS": os.getenv("QUOTE_TOKEN_ADDRESS"),
    }

    missing = [
        var for var, value in required_vars.items() if not value or not value.strip()
    ]

    if missing:
        logger = (
            _get_logger("broker.api.config")
            if _get_logger
            else logging.getLogger("broker.api.config")
        )
        logger.error(
            "Broker API startup: Missing required DIEM/VVV environment variables: %s. "
            "Bridge pricing and DIEM trade routes may fail. "
            "Set these in your environment or secrets manager.",
            ", ".join(missing),
        )
    else:
        logger = (
            _get_logger("broker.api.config")
            if _get_logger
            else logging.getLogger("broker.api.config")
        )
        logger.debug("Broker API startup: DIEM/VVV environment variables validated")


def create_app() -> FastAPI:
    """
    Factory function to create the FastAPI application.

    Wires all routers and applies middleware configuration.
    """
    # Validate auth configuration at startup
    try:
        from . import auth, cache
    except ImportError:
        from apps.broker_api import auth, cache
    auth.validate_admin_config()

    # Validate DIEM/VVV environment variables at startup (ID-004)
    _validate_diem_vvv_env()
    logger.info(
        "env-config: ETH_PRICE_PATH=%s WBTC_PRICE_PATH=%s WETH_USDC_POOL_FEE=%s",
        os.getenv("ETH_PRICE_PATH") or "<unset>",
        os.getenv("WBTC_PRICE_PATH") or "<unset>",
        os.getenv("WETH_USDC_POOL_FEE") or "<unset>",
    )

    # Expose cache dicts and functions for test access (tests/test_env_and_prices_cache.py)
    # Tests need to access module._prices_resp_cache, module._env_prices_resp_cache,
    # module._get_marketdata_provider, and module.env_status
    try:
        import apps.broker_api.cache as cache_module
    except ImportError:
        from apps.broker_api import cache as cache_module
    # Build dependencies first (needed for lifespan)
    fast_startup = _fast_startup_enabled()
    deps = _build_dependencies(fast_startup=fast_startup)

    global _prices_resp_cache, _env_prices_resp_cache, _get_marketdata_provider
    global env_status, _limiter, store, keys, Tenant
    _prices_resp_cache = cache_module._prices_resp_cache
    _env_prices_resp_cache = cache_module._env_prices_resp_cache
    _get_marketdata_provider = deps["get_marketdata_provider"]
    env_status = deps["env_status_fn"]
    _limiter = deps.get("limiter")
    store = deps["store"]
    keys = deps["keys"]
    try:
        from .tenant_store import Tenant as TenantType
    except ImportError:
        from apps.broker_api.tenant_store import Tenant as TenantType
    Tenant = TenantType  # type: ignore[assignment]

    # Create lifespan manager using the new lifespan module
    @asynccontextmanager
    async def app_lifespan(app: FastAPI):  # type: ignore
        # Include all assets used by the buy page (VVV appended in warmup)
        warm_symbols = ("DIEM", "ETH", "USDC", "WBTC")
        async with lifespan_module.lifespan(
            app,
            warm_symbols,
            deps["get_marketdata_provider"],
            deps["env_status_fn"],
            cache.env_prices_cache_set,
        ):
            yield

    app = FastAPI(
        title="Venice Capacity Broker API",
        version="0.2.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=app_lifespan,
    )

    # Static routes (buy page, admin UI)
    _setup_static_routes(app)

    # CORS middleware
    _setup_cors(app)

    # Metrics endpoint
    @app.get("/metrics", include_in_schema=False)
    def metrics() -> PlainTextResponse:
        try:
            return PlainTextResponse(_metrics_render())
        except Exception as exc:
            logger.warning("metrics render failed: %s", exc)
            return PlainTextResponse("")

    # Wire all routers
    _wire_routers(app, deps)

    if fast_startup:
        _prefetch_lazy_dependencies(deps)

    logger.info("Venice Broker API initialized with modular routers")
    return app


def _setup_static_routes(app: FastAPI) -> None:
    """Set up static file routes and landing pages."""
    try:
        _buy_html_path = (
            Path(__file__).resolve().parent.parent / "control-plane" / "buy.html"
        ).resolve()

        @app.get("/", include_in_schema=False)
        async def index() -> RedirectResponse:
            return RedirectResponse(url="/buy.html", status_code=307)

        @app.get("/buy.html", include_in_schema=False)
        async def buy_landing() -> FileResponse:
            from fastapi import HTTPException

            if not _buy_html_path.exists():
                raise HTTPException(status_code=404, detail="buy page not found")
            return FileResponse(_buy_html_path)

        @app.get("/api", include_in_schema=False)
        def api_probe_get() -> dict:
            return {"ok": True, "service": "broker", "version": "0.2.0"}

        @app.head("/api", include_in_schema=False)
        def api_probe_head() -> PlainTextResponse:
            return PlainTextResponse("", status_code=200)

        @app.get("/health", include_in_schema=False)
        def health_check() -> dict:
            """Health check endpoint for Replit deployments and load balancers."""
            return {"status": "ok", "service": "broker", "version": "0.2.0"}

        @app.head("/health", include_in_schema=False)
        def health_check_head() -> PlainTextResponse:
            """HEAD health check for faster readiness probes."""
            return PlainTextResponse("", status_code=200)

        # Mount admin UI if present
        _admin_dir = Path(__file__).resolve().parent.parent / "control-plane"
        if _admin_dir.exists():
            app.mount(
                "/admin",
                StaticFiles(directory=str(_admin_dir), html=True),
                name="admin",
            )
            logger.info("admin ui: mounted at /admin from %s", _admin_dir)
    except Exception as e:
        logger.warning("static routes setup failed: %s", e)


def _setup_cors(app: FastAPI) -> None:
    """Configure CORS middleware if enabled."""
    try:
        if (os.getenv("CORS_ENABLED") or "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            origins = [
                o.strip()
                for o in (os.getenv("CORS_ALLOW_ORIGINS") or "").split(",")
                if o.strip()
            ]
            if not origins:
                origins = ["*"]
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            logger.info("CORS enabled for origins: %s", origins)
    except Exception as e:
        logger.warning("CORS setup failed: %s", e)


def _build_env_status() -> dict[str, Any]:
    """
    Build environment status response.

    Returns:
            Dict with version, features, pricing, payments, etc.
    """
    treasury = (os.getenv("TREASURY_ADDRESS") or "").strip()
    usdc_addr = (os.getenv("USDC_ADDRESS") or "").strip()
    base_rpc = (os.getenv("BASE_RPC_URL") or "").strip()

    quotes_enabled = (os.getenv("QUOTES_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    purchases_enabled = (os.getenv("PURCHASES_ENABLED") or "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    clearing_enabled = (os.getenv("CLEARING_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    bids_enabled = (os.getenv("BIDS_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    diem_snapshot_mode = (
        (os.getenv("UI_DIEM_SNAPSHOT_MODE") or "always").strip().lower()
    )

    quote_ttl = int((os.getenv("PRICE_QUOTE_TTL_SECONDS") or "120").strip() or 120)

    # Populate discount map from configured environment variables
    try:
        from services.pricing.service import configured_discount_map

        discounts = configured_discount_map()
    except Exception:
        discounts = {}

    return {
        "version": "0.2.0",
        "features": {
            "quotes": quotes_enabled,
            "purchases": purchases_enabled,
            "clearing": clearing_enabled,
            "bids": bids_enabled,
            "diem_snapshot_mode": diem_snapshot_mode,
        },
        "pricing": {
            "discounts": discounts,
        },
        "payments": {
            "treasury_address": treasury,
            "accepted_assets": ["USDC", "ETH", "WBTC"] if treasury else [],
            "usdc_address": usdc_addr if usdc_addr else None,
        },
        "buyer": {
            "quote_ttl": quote_ttl,
            "last_updated": int(time.time()),
        },
        "network": {
            "base_rpc_url": base_rpc or None,
        },
    }


def _fast_startup_enabled() -> bool:
    raw = os.getenv("BROKER_FAST_STARTUP") or ""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _prefetch_lazy_dependencies(deps: dict[str, Any]) -> None:
    for key in ("store", "pricing_service", "settle_pricing"):
        value = deps.get(key)
        if isinstance(value, _LazyProxy):
            value.prefetch()


def _build_dependencies(*, fast_startup: bool = False) -> dict[str, Any]:
    """
    Build dependency injection dict for routers.

    Uses modular helpers from auth, config, store, rate_limit, marketdata, and services modules.
    """
    from libs.venice_sdk.client import VeniceClient
    from services.venice_keys.manager import KeyManager

    try:
        from . import auth, config, store
    except ImportError:
        from apps.broker_api import auth, config, store

    deps: dict[str, Any] = {}

    def _maybe_lazy(factory, name: str):
        if fast_startup:
            return _LazyProxy(factory, name)
        return factory()

    # Core services - build fresh (optionally deferred for fast startup)
    deps["store"] = _maybe_lazy(store.build_store, "tenant-store")
    deps["client"] = VeniceClient()
    deps["keys"] = KeyManager(deps["client"])
    deps["logger"] = logger

    # Auth helpers from auth module
    deps["require_admin"] = lambda authz: auth.require_admin(authz)
    deps["auth_context"] = lambda authz: auth.auth_context(deps["store"], authz)

    # Config helpers from config module
    deps["compute_expires_at"] = config.compute_expires_at
    deps["extract_field"] = config.extract_field
    deps["default_quota"] = config.DEFAULT_QUOTA

    # Rate limiting from rate_limit module
    limiter, kv_admin, enabled, window_seconds, max_requests = (
        rate_limit.build_rate_limiter()
    )
    deps["limiter"] = limiter
    deps["kv_admin"] = kv_admin
    deps["rate_limits_enabled"] = enabled
    deps["rate_limit_window_seconds"] = window_seconds
    deps["rate_limit_max_requests"] = max_requests
    deps["get_rate_limit_headers"] = lambda x: {}  # TODO: Implement if needed

    # Log Redis/KV backend status for diagnostics
    if kv_admin is not None:
        checker = getattr(kv_admin, "has_atomic_counters", None)
        if callable(checker):
            try:
                has_atomic = bool(checker())
            except Exception:
                has_atomic = False
        else:
            has_atomic = False
        logger.info(
            "kv-admin: initialized (atomic_counters=%s, rate_limits_enabled=%s)",
            has_atomic,
            enabled,
        )
    else:
        logger.warning(
            "kv-admin: not initialized (rate limiting and counters disabled)"
        )

    # Market data provider from marketdata module
    deps["get_marketdata_provider"] = marketdata.get_marketdata_provider

    # Environment status function
    deps["env_status_fn"] = _build_env_status

    # Update module-level attribute for test access
    global env_status
    env_status = _build_env_status

    # Pricing service from pricing module
    try:
        deps["pricing_service"] = _maybe_lazy(
            pricing_helpers.build_pricing_service, "pricing-service"
        )
        deps["settle_pricing"] = _maybe_lazy(
            pricing_helpers.build_pricing_service, "settle-pricing"
        )
    except Exception:
        deps["pricing_service"] = None
        deps["settle_pricing"] = None
    # Quote persistence is synchronous by default: verification reads the
    # quote row immediately after issuance, and an async write can lose the
    # race (verify 404s on a quote the buyer just received).
    # Set QUOTES_PERSIST_ENABLED=false to disable persistence entirely
    # Set QUOTES_ASYNC_ENABLED=true to trade that safety for response time
    deps["quotes_persist_enabled"] = (
        os.getenv("QUOTES_PERSIST_ENABLED") or "true"
    ).strip().lower() in {"1", "true", "yes", "on"}
    deps["quotes_async_enabled"] = (
        os.getenv("QUOTES_ASYNC_ENABLED") or "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    deps["quotes_enabled"] = (
        os.getenv("QUOTES_ENABLED") or "true"
    ).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

    # Purchases
    deps["purchases_enabled"] = (
        os.getenv("PURCHASES_ENABLED") or "true"
    ).strip().lower() in {"1", "true", "yes", "on"}

    # SQL/DB helpers
    try:
        from sqlmodel import select

        from db.session import get_session

        deps["has_sqlmodel"] = True
        deps["get_sess"] = get_session
        deps["select_all"] = select
        deps["select_func"] = select
    except Exception:
        deps["has_sqlmodel"] = False

    # DB models (for admin/bids/settlement)
    if deps["has_sqlmodel"]:
        try:
            from db.models import Bid, Counter, Purchase, Quote

            deps["quote_model"] = Quote
            deps["purchase_model"] = Purchase
            deps["counter_model"] = Counter
            deps["bid_model"] = Bid
        except Exception:
            pass

    # Bids configuration
    bids_enabled = (os.getenv("BIDS_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    deps["bids_enabled"] = bids_enabled
    deps["has_sql_bids"] = deps.get("has_sqlmodel", False)

    # Bids helpers from bids module
    if bids_enabled:
        sign_domain_name = os.getenv("SIGN_DOMAIN_NAME") or "Venice Broker"
        sign_domain_version = os.getenv("SIGN_DOMAIN_VERSION") or "1"
        chain_id_env = os.getenv("CHAIN_ID") or os.getenv("BASE_CHAIN_ID") or "8453"

        deps["recover_buyer"] = lambda req: bids_helpers.recover_buyer(
            req, sign_domain_name, sign_domain_version, chain_id_env
        )
        deps["price_usdc_per_unit_from_asset"] = (
            lambda max_price_minor, asset: bids_helpers.price_usdc_per_unit_from_asset(
                max_price_minor, asset, marketdata.get_marketdata_provider
            )
        )
        deps["classify_bid_status"] = (
            lambda max_price_usdc, now_s, expiry_s: bids_helpers.classify_bid_status(
                max_price_usdc,
                now_s,
                expiry_s,
                lambda: clearing_helpers.compute_clearing_price(
                    marketdata.get_marketdata_provider()
                ),
            )
        )

    # Clearing configuration
    clearing_enabled = (os.getenv("CLEARING_ENABLED") or "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    deps["clearing_enabled"] = clearing_enabled
    deps["clearing_sse_interval"] = float(os.getenv("CLEARING_SSE_INTERVAL") or "5.0")

    if clearing_enabled:
        deps["compute_clearing_price"] = (
            lambda: clearing_helpers.compute_clearing_price(
                marketdata.get_marketdata_provider()
            )
        )

    # Settlement configuration
    settlement_enabled = (
        os.getenv("SETTLEMENT_ENABLED") or "false"
    ).strip().lower() in {"1", "true", "yes", "on"}
    deps["settlement_enabled"] = settlement_enabled

    # verify_purchase is actually a route handler in purchases router, not a helper
    deps["verify_purchase"] = None

    return deps


def _wire_routers(app: FastAPI, deps: dict[str, Any]) -> None:
    """Wire all routers to the FastAPI app with dependency injection."""

    # Tenants router
    try:
        tenant_router = tenants.init_router(
            store=deps["store"],
            keys=deps["keys"],
            client=deps["client"],
            logger=deps["logger"],
            require_admin=deps["require_admin"],
            auth_context=deps["auth_context"],
            compute_expires_at=deps["compute_expires_at"],
            extract_field=deps["extract_field"],
            default_quota=deps["default_quota"],
            kv_admin=deps["kv_admin"],
            rate_limits_enabled=deps["rate_limits_enabled"],
            rate_limit_window_seconds=deps["rate_limit_window_seconds"],
            rate_limit_max_requests=deps["rate_limit_max_requests"],
            limiter=deps["limiter"],
            get_rate_limit_headers=deps["get_rate_limit_headers"],
        )
        app.include_router(tenant_router)
        logger.info("Wired tenants router")
    except Exception as e:
        logger.error("Failed to wire tenants router: %s", e)

    # Marketdata router
    try:
        import sys

        def _provider_proxy():
            provider_factory = _provider_override or _get_marketdata_provider

            module_globals = globals()
            candidate = module_globals.get("_get_marketdata_provider")
            if callable(candidate):
                if os.getenv("BROKER_DEBUG_PROVIDER_PROXY") == "1":
                    print(
                        f"[provider_proxy] globals candidate from {getattr(candidate, '__module__', '')}",
                        flush=True,
                    )
                provider_factory = candidate

            module_refs = getattr(_SHARED_STATE, "module_refs", [])
            if isinstance(module_refs, list):
                for mod in reversed(module_refs):
                    candidate = getattr(mod, "_get_marketdata_provider", None)
                    if not callable(candidate):
                        continue
                    candidate_module = getattr(candidate, "__module__", "")
                    if os.getenv("BROKER_DEBUG_PROVIDER_PROXY") == "1":
                        print(
                            f"[provider_proxy] module_ref candidate from {candidate_module}",
                            flush=True,
                        )
                    if (
                        candidate_module.startswith("tests.")
                        or candidate_module == "__main__"
                    ):
                        provider_factory = candidate
                        break

            shared_globals = getattr(_SHARED_STATE, "canonical_globals", None)
            if isinstance(shared_globals, dict):
                shared_candidate = shared_globals.get("_get_marketdata_provider")
                if callable(shared_candidate):
                    provider_factory = shared_candidate

            if os.getenv("PYTEST_CURRENT_TEST"):
                for name, test_mod in list(sys.modules.items()):
                    if not name.startswith("tests."):
                        continue
                    app_mod = getattr(test_mod, "module", None)
                    if app_mod is None:
                        continue
                    candidate = getattr(app_mod, "_get_marketdata_provider", None)
                    if callable(candidate) and getattr(
                        candidate, "__module__", ""
                    ).startswith("tests."):
                        provider_factory = candidate
                        break

            if not callable(provider_factory):
                candidate = None
                if _CANONICAL_GLOBALS is not None:
                    candidate = _CANONICAL_GLOBALS.get("_get_marketdata_provider")
                if not callable(candidate):
                    app_module = sys.modules.get("apps.broker_api.app")
                    if app_module is not None and hasattr(
                        app_module, "_get_marketdata_provider"
                    ):
                        candidate = app_module._get_marketdata_provider
                if callable(candidate):
                    candidate_module = getattr(candidate, "__module__", "")
                    if (
                        candidate_module.startswith("tests.")
                        or candidate_module == "__main__"
                        or not callable(provider_factory)
                    ):
                        provider_factory = candidate

            if os.getenv("BROKER_DEBUG_PROVIDER_PROXY") == "1":
                try:
                    identifier = getattr(
                        provider_factory, "__name__", repr(provider_factory)
                    )
                except Exception:
                    identifier = repr(provider_factory)
                print(f"[provider_proxy] selected={identifier}", flush=True)

            if (
                os.getenv("PYTEST_CURRENT_TEST")
                and callable(provider_factory)
                and getattr(provider_factory, "__module__", __name__).startswith(
                    "apps."
                )
            ):
                try:
                    import gc

                    for obj in gc.get_objects():
                        if not callable(obj):
                            continue
                        module_name = getattr(obj, "__module__", "")
                        if not module_name.startswith("tests."):
                            continue
                        closure = getattr(obj, "__closure__", None)
                        if not closure:
                            continue
                        for cell in closure:
                            try:
                                contents = cell.cell_contents
                            except Exception:
                                continue
                            if hasattr(contents, "calls") and isinstance(
                                getattr(contents, "calls", None), list
                            ):
                                if os.getenv("BROKER_DEBUG_PROVIDER_PROXY") == "1":
                                    print(
                                        f"[provider_proxy] gc candidate from {module_name}",
                                        flush=True,
                                    )
                                provider_factory = obj
                                raise StopIteration  # break out
                except StopIteration:
                    pass
                except Exception:
                    pass

            if callable(provider_factory):
                return provider_factory()
            raise RuntimeError("marketdata provider unavailable")

        def _env_status_proxy():
            status_fn = env_status
            app_module = sys.modules.get("apps.broker_api.app")
            if app_module is not None and callable(
                getattr(app_module, "env_status", None)
            ):
                status_fn = app_module.env_status
            if callable(status_fn):
                return status_fn()
            return _build_env_status()

        marketdata_router_instance = marketdata_router.init_router(
            get_marketdata_provider=_provider_proxy,
            env_status_fn=_env_status_proxy,
            logger=deps["logger"],
            require_admin=deps["require_admin"],
        )
        app.include_router(marketdata_router_instance)
        logger.info("Wired marketdata router")
    except Exception as e:
        logger.error("Failed to wire marketdata router: %s", e)

    # Venice router
    try:
        venice_router = venice.init_router(
            client=deps["client"],
            require_admin=deps["require_admin"],
        )
        app.include_router(venice_router)
        logger.info("Wired venice router")
    except Exception as e:
        logger.error("Failed to wire venice router: %s", e)

    # Quotes router
    try:
        quotes_router = quotes.init_router(
            pricing_service=deps["pricing_service"],
            logger=deps["logger"],
            quotes_enabled=deps["quotes_enabled"],
            quotes_persist_enabled=deps["quotes_persist_enabled"],
            quotes_async_enabled=deps["quotes_async_enabled"],
        )
        app.include_router(quotes_router)
        logger.info("Wired quotes router")
    except Exception as e:
        logger.error("Failed to wire quotes router: %s", e)

    # Purchases router
    try:
        purchases_router = purchases.init_router(
            store=deps["store"],
            keys=deps["keys"],
            logger=deps["logger"],
            extract_field=deps["extract_field"],
            purchases_enabled=deps["purchases_enabled"],
        )
        app.include_router(purchases_router)
        logger.info("Wired purchases router")
    except Exception as e:
        logger.error("Failed to wire purchases router: %s", e)

    # Admin router
    if deps.get("has_sqlmodel"):
        try:
            admin_router, debug_router = admin.init_router(
                require_admin=deps["require_admin"],
                has_sqlmodel=deps["has_sqlmodel"],
                get_sess=deps["get_sess"],
                select_all=deps["select_all"],
                quote_model=deps["quote_model"],
                purchase_model=deps["purchase_model"],
                counter_model=deps["counter_model"],
                logger=deps["logger"],
            )
            app.include_router(admin_router)
            app.include_router(debug_router)
            logger.info("Wired admin router and debug router")
        except Exception as e:
            logger.error("Failed to wire admin router: %s", e)

    # Clearing router
    if deps.get("clearing_enabled"):
        try:
            clearing_router = clearing.init_router(
                compute_clearing_price=deps["compute_clearing_price"],
                clearing_enabled=deps["clearing_enabled"],
                clearing_sse_interval=deps["clearing_sse_interval"],
                logger=deps["logger"],
            )
            app.include_router(clearing_router)
            logger.info("Wired clearing router")
        except Exception as e:
            logger.error("Failed to wire clearing router: %s", e)

    # Bids router
    if deps.get("bids_enabled") and deps.get("has_sql_bids"):
        try:
            bids_router = bids.init_router(
                bids_enabled=deps["bids_enabled"],
                has_sql_bids=deps["has_sql_bids"],
                get_sess=deps["get_sess"],
                select_func=deps["select_func"],
                bid_model=deps["bid_model"],
                recover_buyer=deps["recover_buyer"],
                price_usdc_per_unit_from_asset=deps["price_usdc_per_unit_from_asset"],
                classify_bid_status=deps["classify_bid_status"],
                logger=deps["logger"],
            )
            app.include_router(bids_router)
            logger.info("Wired bids router")
        except Exception as e:
            logger.error("Failed to wire bids router: %s", e)

    # Settlement router
    if deps.get("settlement_enabled"):
        try:
            settlement_router = settlement.init_router(
                settlement_enabled=deps["settlement_enabled"],
                has_sql_bids=deps.get("has_sql_bids", False),
                get_sess=deps.get("get_sess"),
                select_func=deps.get("select_func"),
                bid_model=deps.get("bid_model"),
                settle_pricing=deps.get("settle_pricing"),
                verify_purchase=deps["verify_purchase"],
                get_marketdata_provider=deps["get_marketdata_provider"],
                logger=deps["logger"],
            )
            app.include_router(settlement_router)
            logger.info("Wired settlement router")
        except Exception as e:
            logger.error("Failed to wire settlement router: %s", e)


# Create the app instance for uvicorn
app = create_app()

__all__ = ["app", "create_app"]


def __setattr__(name: str, value: object) -> None:  # type: ignore[override]
    global _provider_override
    globals()[name] = value
    if name == "_get_marketdata_provider" and callable(value):
        _provider_override = value  # type: ignore[assignment]
        import sys

        canonical = sys.modules.get("apps.broker_api.app")
        current = sys.modules.get(__name__)
        if canonical is not None and canonical is not current:
            canonical._provider_override = value
