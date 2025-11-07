# Configuration

This is the single source of truth for environment variables and run-mode flags.

## Venice API

- `VENICE_API_BASE_URL` — must be `https://api.venice.ai/api/v1` (must include `/api/v1`).
- `VENICE_API_KEY` / `VENICE_PARENT_KEY` — parent key for sub-key creation.
- Optional path overrides: `VENICE_VVV_CIRC_PATH`, `VENICE_VVV_UTIL_PATH`, `VENICE_VVV_YIELD_PATH`, `VENICE_CREATE_SUBKEY_PATH`, `VENICE_CREATE_ROOT_PATH`, `VENICE_CHALLENGE_PATH`, `VENICE_REVOKE_KEY_PATH`.

## Base / On-chain

- `BASE_RPC_URL`, `BASE_CHAIN_ID`.
- Token/contract addresses: `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`.

## DEX Configuration

- `DEX_PROVIDERS` (e.g., `uniswap_v2,aerodrome`).
- Routers: `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, `AERODROME_STABLE`.
- Pricing: `QUOTE_TOKEN_ADDRESS`, optional `TRADE_PATH` for DIEM pricing.
- Timeouts: `DEX_AGGREGATE_TIMEOUT_SECONDS`, `RPC_REQUEST_TIMEOUT_SECONDS`.

## Run Modes & Toggles

CLI flags:

```bash
# Dry-run (default)
uv run python apps/cli/main.py run:loop --dry-run
# Progressive-live (recommended)
uv run python apps/cli/main.py run:loop --progressive-live
# Enable-live (immediate)
uv run python apps/cli/main.py run:loop --enable-live
```

Environment toggles:

- `AGENTS_PAUSED=true` — emergency stop without bringing API down.
- `STAKEMASTER_PROGRESSIVE_ENABLE`, `STAKEMASTER_PROGRESSIVE_CYCLES`.

## Persistence & fallbacks (production policy)

- `APP_ENV` — set to `production` (Docker/Replit deployments), `staging`, `development`, or `test`.
- Production requires Postgres. `SQL_DATABASE_URL` must point to Postgres (not SQLite or placeholder). Fallbacks to SQLite are disabled in production.
- In non‑production, fallbacks are gated by explicit flags:
  - `ALLOW_SQLITE_FALLBACK=1` — allows SQLite engine when Postgres is absent.
  - `ALLOW_JSON_FALLBACK=1` — allows JSON tenant store and memory logs.
  - `ALLOW_INMEMORY_KV_FALLBACK=1` — allows in‑process KV for rate limiting.
- Agent memory logs persist to SQL table `AgentMemory`. Retention is controlled by `MEMORY_RETENTION_DAYS` (default 30).
- Decision inserts fail fast in production when persistence errors occur.

Metrics and visibility:

- The Broker exposes Prometheus counters at `/metrics` (e.g., `vvv_fallback_sqlite_total`, `vvv_fallback_json_store_total`, `vvv_fallback_inmemory_kv_total`, `vvv_sql_connect_errors_total`).

## Quorum & Reflex

- Quorum: `QUORUM_ENABLE`, `QUORUM_THRESHOLD`, `QUORUM_WEIGHT_*`, model thresholds.

## Replit & Docker prestart

- Migrations must be applied before serving:
  - Docker: call `scripts/prestart.sh` (runs `alembic upgrade head` then environment validation).
  - Replit: call `scripts/replit_prestart.sh` in the deployment prestart hook.
- Replit production databases are PostgreSQL 16 on Neon (managed). Use the provided DSN and apply migrations before serving. See:
  - Replit SQL Database: https://docs.replit.com/cloud-services/storage-and-databases/sql-database.md
  - Replit Production Databases: https://docs.replit.com/cloud-services/storage-and-databases/production-databases.md
  - Replit KV/Database (fallback for KV): https://docs.replit.com/cloud-services/storage-and-databases/replit-database.md

## Risk & DIEM

- Mint/burn gates: `DIEM_ENABLE_SVVV_GATE`, `DIEM_MINT_RATE_SVVV_PER_DIEM`, `DIEM_MINT_RATE`, `DIEM_DECIMALS`, `SVVV_DECIMALS`.
- Thresholds: `DIEM_PREMIUM_THRESHOLD`, `DIEM_DISCOUNT_THRESHOLD`.
- Sizing & guards: `RISK_MAX_SLIPPAGE_BPS`, `RISK_MAX_POOL_TAKE_BPS`, `RISK_MAX_VOLATILITY_BPS`, `RISK_UTIL_ALPHA`.
- DIEM staking helpers: `DIEM_STAKING_ADDRESS`, `DIEM_STAKING_ABI`, `DIEM_STAKE_FN`, `DIEM_LOCK_ON_MINT`, `DIEM_UNLOCK_AFTER_BURN`, `DIEM_UNLOCK_COOLDOWN_SECONDS`.

## Broker

- Core: `BROKER_ADMIN_TOKEN`, `BROKER_REQUIRE_ADMIN_TOKEN`, `BROKER_DEFAULT_MODEL`.
- Features: `QUOTES_ENABLED`, `PURCHASES_ENABLED`, `PRICE_ENGINE`, `ACCEPT_ASSETS`, `TREASURY_ADDRESS`.
- Pricing knobs: `BROKER_UTIL_TARGET`, `BROKER_PRICE_STEP_BPS`, `BROKER_DISCOUNT_MAX_BPS`, `BROKER_HYSTERESIS_WINDOW`, `BROKER_UTIL_SURGE_THRESHOLD`, `BROKER_UTIL_RELAX_THRESHOLD`, `BROKER_BASE_PRICE_USD`, `BROKER_SURGE_MULTIPLIER`.
- CORS: `CORS_ENABLED`, `CORS_ALLOW_ORIGINS`.

## Debug & Instrumentation

- `DIEM_DEBUG_ROUTES=1` — logs normalized routes and aggregator diagnostics.
- `MARKETDATA_DEBUG_SANITY=1` — emits price sanity clamp context (avoid in prod).
- Sanity drift: `MARKETDATA_PRICE_SANITY_MAX_DRIFT`, `MARKETDATA_SANITY_THRESHOLD`.

## Examples

Minimal local `.env` example:

```bash
VENICE_API_BASE_URL=https://api.venice.ai/api/v1
VENICE_PARENT_KEY=sk-...
BASE_RPC_URL=...
VVV_TOKEN_ADDRESS=0x...
DIEM_TOKEN_ADDRESS=0x...
BROKER_ADMIN_TOKEN=...
QUOTES_ENABLED=1
PURCHASES_ENABLED=1
```

See also: `./DEPLOYMENT.md` for preflight and run-mode commands.


