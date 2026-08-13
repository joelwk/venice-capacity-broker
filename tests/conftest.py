import os
from pathlib import Path

import pytest


def _load_dotenv_values(keys):
    """Load selected keys from .env without polluting the full environment."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return {}
    values = {}
    try:
        with env_path.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip().rstrip("\r")
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :]
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key not in keys:
                    continue
                val = val.strip()
                if (val.startswith('"') and val.endswith('"')) or (
                    val.startswith("'") and val.endswith("'")
                ):
                    val = val[1:-1]
                if key:
                    values[key] = val
    except Exception:
        return {}
    return values


_DOTENV_VALUES = _load_dotenv_values(
    {
        "BASE_RPC_URL",
        "DIEM_USDC_POOL_ADDRESS",
        "DIEM_TOKEN_ADDRESS",
        "QUOTE_TOKEN_ADDRESS",
        "VVV_TOKEN_ADDRESS",
        "AERODROME_CL_ROUTER_ADDRESS",
    }
)

DEFAULT_TOKEN_ENV = {
    "DIEM_TOKEN_ADDRESS": _DOTENV_VALUES.get(
        "DIEM_TOKEN_ADDRESS",
        "0xF4d97F2Da56e8c3098f3a8D538DB630A2606a024",  # gitleaks:allow Base mainnet DIEM (public)
    ),
    "VVV_TOKEN_ADDRESS": _DOTENV_VALUES.get(
        "VVV_TOKEN_ADDRESS",
        "0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf",  # gitleaks:allow Base mainnet VVV (public)
    ),
    "QUOTE_TOKEN_ADDRESS": _DOTENV_VALUES.get(
        "QUOTE_TOKEN_ADDRESS",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # gitleaks:allow Base mainnet USDC (public)
    ),
    "WETH_ADDRESS": "0x4200000000000000000000000000000000000006",
    # Default DIEM sell-direction path with V3 fee tier on VVV/USDC hop
    "TRADE_PATH": "0xF4d97F2Da56e8c3098f3a8D538DB630A2606a024,0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913@3000",
    # Default RPC URL for Web3 initialization (public Base RPC - validation skipped in tests)
    "BASE_RPC_URL": os.getenv("BASE_RPC_URL")
    or _DOTENV_VALUES.get("BASE_RPC_URL")
    or "https://mainnet.base.org",
    # Direct DIEM/USDC SlipStream pool (if available via .env)
    "DIEM_USDC_POOL_ADDRESS": _DOTENV_VALUES.get(
        "DIEM_USDC_POOL_ADDRESS", "0xBc3231036Ee1ECa03E5F67FEceDC640D21610823"
    ),
    "AERODROME_CL_ROUTER_ADDRESS": _DOTENV_VALUES.get(
        "AERODROME_CL_ROUTER_ADDRESS", ""
    ),
}

# Keep core env defaults available outside monkeypatch lifecycle (e.g. background tasks).
os.environ.setdefault("BASE_RPC_URL", DEFAULT_TOKEN_ENV["BASE_RPC_URL"])
os.environ.setdefault(
    "DIEM_USDC_POOL_ADDRESS", DEFAULT_TOKEN_ENV["DIEM_USDC_POOL_ADDRESS"]
)
os.environ.setdefault("DIEM_TOKEN_ADDRESS", DEFAULT_TOKEN_ENV["DIEM_TOKEN_ADDRESS"])
os.environ.setdefault("VVV_TOKEN_ADDRESS", DEFAULT_TOKEN_ENV["VVV_TOKEN_ADDRESS"])
os.environ.setdefault("QUOTE_TOKEN_ADDRESS", DEFAULT_TOKEN_ENV["QUOTE_TOKEN_ADDRESS"])
if DEFAULT_TOKEN_ENV["AERODROME_CL_ROUTER_ADDRESS"]:
    os.environ.setdefault(
        "AERODROME_CL_ROUTER_ADDRESS", DEFAULT_TOKEN_ENV["AERODROME_CL_ROUTER_ADDRESS"]
    )

# Disable console capture during tests to avoid conflicts with pytest's capture mechanism
os.environ.setdefault("LOG_CAPTURE_CONSOLE", "0")

os.environ.setdefault("BROKER_REQUIRE_ADMIN_TOKEN", "false")
os.environ.setdefault("BROKER_ADMIN_TOKEN", "test-admin")  # gitleaks:allow test value
os.environ.setdefault("VENICE_PARENT_KEY", "parent-test")  # gitleaks:allow test value
# The public-endpoint limiter is process-local and shared across tests; keep it
# off by default so unrelated tests don't consume each other's budget.
os.environ.setdefault("BUY_RATE_LIMITS_ENABLED", "false")
os.environ.setdefault("ALLOW_JSON_FALLBACK", "1")
os.environ.setdefault("ALLOW_SQLITE_FALLBACK", "1")
os.environ.setdefault("ALLOW_INMEMORY_KV_FALLBACK", "1")
os.environ.setdefault("VENICE_OFFLINE_SIGNALS", "1")
os.environ.setdefault("MARKETDATA_EXTERNAL_PRICE_TTL_SECONDS", "0")
os.environ.setdefault("MARKETDATA_VALIDATE_TRADE_PATHS", "0")
os.environ.setdefault("BROKER_WARMUP_PING_DISABLE", "1")

# Ensure developer-specific database/KV env vars don't leak into tests.
# Note: BASE_RPC_URL is intentionally NOT cleared here - it's set in DEFAULT_TOKEN_ENV
# so that Web3 initialization can succeed in tests (validation is skipped via PYTEST_CURRENT_TEST).
for _unset in (
    "SQL_DATABASE_URL",
    "DATABASE_URL",
    "POSTGRES_HOST",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "KV_URL",
    "REPLIT_DB_URL",
    "REDIS_URL",
    "KV_REDIS_URL",
    "RPC_URL",
    "RPC_URLS",
    "RPC_URL_FALLBACK",
    "BASE_RPC_URLS",
    "BASE_RPC_URL_FALLBACK",
):
    os.environ.pop(_unset, None)


@pytest.fixture(autouse=True)
def _reset_env_vars(monkeypatch):
    # Clear developer-specific env vars but preserve BASE_RPC_URL (set via DEFAULT_TOKEN_ENV)
    for name in (
        "SQL_DATABASE_URL",
        "DATABASE_URL",
        "POSTGRES_HOST",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
        "KV_URL",
        "REPLIT_DB_URL",
        "REDIS_URL",
        "KV_REDIS_URL",
        "RPC_URL",
        "RPC_URLS",
        "RPC_URL_FALLBACK",
        "BASE_RPC_URLS",
        "BASE_RPC_URL_FALLBACK",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in DEFAULT_TOKEN_ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture(autouse=True)
def _reset_marketdata_caches():
    from services.marketdata.provider import MarketDataProvider

    MarketDataProvider._price_cache.clear()
    MarketDataProvider._external_price_cache.clear()
    if hasattr(MarketDataProvider, "_external_price_backoff"):
        MarketDataProvider._external_price_backoff.clear()
    yield
    MarketDataProvider._price_cache.clear()
    MarketDataProvider._external_price_cache.clear()
    if hasattr(MarketDataProvider, "_external_price_backoff"):
        MarketDataProvider._external_price_backoff.clear()
