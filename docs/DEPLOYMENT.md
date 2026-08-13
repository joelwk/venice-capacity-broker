# Deployment Guide

This guide covers environment preparation, layered configuration, and live operations for Docker Compose and Replit deployments, with a single canonical section for run modes.

## Prerequisites

Use Python 3.10+ with `uv` installed. Have Docker available. Secure a Base RPC endpoint, Venice parent key, token/contract addresses, and DEX router addresses. Provision SQL (broker state) and KV (rate limits) stores.

## Layered Configuration

Copy `.env.example` → `.env`. Add Docker overrides in `.env.docker` or `docker/.env.local`. On Replit, mirror via Secrets. Compose uses `${VAR:-default}` fallbacks so unset values won't break the stack.

## Preflight

```bash
uv sync --extra dev
uv run python apps/cli/main.py env:status
uv run python apps/cli/main.py startup:probe --check-live
uv run alembic upgrade head
uv run python apps/cli/main.py market:pools:watch --once

# Validate DIEM pricing and composite routing
uv run python apps/cli/main.py market:diem-bridge-check

# Validate portfolio inventory (required for Treasurer automation)
uv run python apps/cli/main.py startup:probe --check-live
```

## Run Modes (canonical)

```bash
# Dry-run (default)
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 2

# Progressive-live (recommended)
uv run python apps/cli/main.py run:loop --progressive-live --sleep 15

# Enable-live (immediate)
uv run python apps/cli/main.py run:loop --enable-live --sleep 15
```

Emergency stop without downtime: `AGENTS_PAUSED=true`.

## Docker Compose

1) `cp .env.example .env` and fill required values.
2) `docker compose up -d --build` to launch API, orchestrator, watcher.
3) `docker compose logs -f broker` until `/health`, `/metrics`, `/v1/env` report ready.
4) `docker compose exec broker uv run python apps/cli/main.py startup:probe` before live trades.
5) Use `docker compose --env-file dev.env up` when splitting staging vs prod.

## Replit Web Service

1) Add secrets for `BROKER_ADMIN_TOKEN`, `VENICE_PARENT_KEY`, `ETH_PRIVATE_KEY`, `BASE_RPC_URL`, token addresses, routers, `TREASURY_ADDRESS`.
2) Supply SQL (Replit Postgres) and KV (Replit DB) URLs.
3) Enable: `QUOTES_ENABLED=1`, `PURCHASES_ENABLED=1`, `PRICE_ENGINE=market`, `CORS_ENABLED=true`.
4) Click Run (script validates envs, runs migrations, starts Uvicorn).

## Starting Locally

- `make run-broker` starts only the API with hot reload.
- `make run-stack` adds the orchestrator and token watcher.
- `AUTOSTART_ORCHESTRATOR_LIVE=1` to execute on-chain immediately (otherwise dry-run).

## Post-Deploy Checks

- Open `/health`, `/metrics`, `/v1/env`, and `/admin`.
- `make rotate-probe TENANT=t1` verifies rotation, chat health, limiter persistence.
- Tail `logs/runtime.log` for summaries, price guards, reflex decisions.

## Maintenance & Troubleshooting

- `uv run python apps/cli/main.py venice:probe-openapi` (Venice API paths and env hints).
- `uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC` (routing validation).
- `make server-db-compact` regularly in multi-tenant environments.
- `uv run python apps/cli/main.py env:status` to compare live vs local.

## Base Mainnet Addresses (reference)

Routers (8453): Aerodrome `0xcF77…E43`, Uniswap V2 `0x4752…D24`, Uniswap V3 `0xE592…564`, Quoter `0x61fF…21e`.

Core tokens: WETH `0x4200…006`, USDC `0x8335…913`, DIEM `0xF4d9…024`, VVV `0xacfE…1bf`.

## References

- Configuration → `./CONFIGURATION.md`
- Operations → `./OPERATIONS.md`
- Tokenomics → `./venice-diem-tokenomics.md`
