# Deployment Guide

This single guide covers environment preparation, layered configuration, and live operations for both Docker Compose and Replit deployments.

## Prerequisites

Use Python 3.10 or newer with `uv` installed. Keep Docker handy for local containers. Secure a Base RPC endpoint, Venice parent key, staking contract addresses, and DEX router addresses. Provision a SQL database for broker state plus a KV store (Redis or Replit DB) for live rate limits.

## Layered Configuration

Copy `.env.example` to `.env` for shared secrets. Add Docker overrides in `.env.docker` or `docker/.env.local`; Compose reads them in order. On Replit, mirror the same keys through the Secrets panel. `docker-compose.yml` falls back to defaults via `${VAR:-default}` so unset values never break the stack.

## Base Setup Checklist

1. Install dependencies with `uv sync --extra dev` (or the pip fallback).
2. Populate `.env` with Venice, Base, token, DEX, broker, SQL, and KV settings.
3. Apply migrations through `uv run alembic upgrade head` or `make db-migrate`.
4. Seed market data using `uv run python apps/cli/main.py market:pools:watch --once`.
5. Confirm wiring with `uv run python apps/cli/main.py env:status` and `uv run python apps/cli/main.py startup:probe`.

## Environment Quick Starts

### Docker Compose

1. `cp config/broker-fixes.env.template .env` (or merge into the existing file) and fill required values.
2. Run `docker compose up -d --build` to launch the API, orchestrator, and watcher.
3. Tail logs with `docker compose logs -f broker` until `/health`, `/metrics`, and `/v1/env` report ready.
4. Execute `docker compose exec broker uv run python apps/cli/main.py startup:probe` before turning on live trades.
5. Use additional env files (for example `docker/dev.env`) via `docker compose --env-file dev.env up` when you split staging and production.

### Replit Web Service

1. Add secrets for `BROKER_ADMIN_TOKEN`, `VENICE_PARENT_KEY`, `ETH_PRIVATE_KEY`, `BASE_RPC_URL`, token addresses, routers, and `TREASURY_ADDRESS`.
2. Supply SQL (Replit Postgres) and KV (Replit DB) URLs with access tokens.
3. Enable features through `QUOTES_ENABLED=1`, `PURCHASES_ENABLED=1`, `PRICE_ENGINE=market`, and `CORS_ENABLED=true`.
4. Click Run to trigger `scripts/docker_start_broker.sh`; the script validates envs, runs migrations, and starts Uvicorn.
5. Group secrets by category so future rotations stay manageable.

## Starting Services Locally

`make run-broker` starts only the API with hot reload. `make run-stack` adds the single loop orchestrator and token watcher. Export `AUTOSTART_ORCHESTRATOR_LIVE=1` when you intend to execute on-chain immediately; otherwise leave it dry-run until checks pass.

## Post Deployment Checks

Open `/health`, `/metrics`, `/v1/env`, and the `/admin` dashboard after each deploy. Run `make rotate-probe TENANT=t1` to confirm tenant rotation, chat health, and limiter persistence. Watch `logs/runtime.log` for orchestrator summaries, price guard streaks, and Reflex decisions.

## Going Live Safely

Dry-run the orchestrator with `uv run python apps/cli/main.py run:loop --sleep 15 --max-cycles 0`. Switch to progressive live via `uv run python apps/cli/main.py run:loop --sleep 15 --progressive-live` so StakeMaster heartbeats gate real trades. Toggle `AGENTS_PAUSED=true` when you need an immediate safety stop without bringing the API down.

## Maintenance and Troubleshooting

`uv run python apps/cli/main.py venice:probe-openapi` confirms Venice API paths and prints recommended env exports. `uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC` validates routing whenever pools change. Run `make server-db-compact` regularly in multi-tenant environments to roll KV counters into SQL. `uv run python apps/cli/main.py env:status` remains the quickest way to compare live settings with local files.

## Environment Reference

Critical broker flags: `BROKER_ADMIN_TOKEN`, `BROKER_REQUIRE_ADMIN_TOKEN`, `BROKER_DEFAULT_MODEL`. Venice integration: `VENICE_API_BASE_URL`, `VENICE_PARENT_KEY`, plus optional `VENICE_VVV_*` overrides. Quotes and payment knobs: `QUOTES_ENABLED`, `PURCHASES_ENABLED`, `PRICE_ENGINE`, `ACCEPT_ASSETS`, `TREASURY_ADDRESS`. DEX tuning: `DEX_PROVIDERS`, `DEX_MAX_WORKERS`, `DEX_AGGREGATE_TIMEOUT_SECONDS`, `RPC_REQUEST_TIMEOUT_SECONDS`. Optional modules: `CLEARING_ENABLED`, `BIDS_ENABLED`, `SETTLEMENT_ENABLED`, `CORS_ALLOW_ORIGINS`.

## Base Mainnet Addresses

### DEX Routers (Chain ID: 8453)

**Aerodrome Router:** `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`
**Uniswap V2 Router:** `0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24`
**Uniswap V3 Router:** `0xE592427A0AEce92De3Edee1F18E0157C05861564`
**Uniswap V3 Quoter:** `0x61fFE014bA17989E743c5F6cB21bF9697530B21e`

### Core Tokens

**WETH:** `0x4200000000000000000000000000000000000006`
**USDC:** `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
**DIEM:** `0xF4d97F2da56e8c3098f3a8D538DB630A2606a024`
**VVV:** `0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf`

### Example Configuration

```bash
# DEX Routers
UNISWAP_V2_ROUTER_ADDRESS=0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24
AERODROME_ROUTER_ADDRESS=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
UNISWAP_V3_ROUTER_ADDRESS=0xE592427A0AEce92De3Edee1F18E0157C05861564
UNISWAP_V3_QUOTER_ADDRESS=0x61fFE014bA17989E743c5F6cB21bF9697530B21e

# Quote and bridge tokens
QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913  # USDC
BRIDGE_TOKEN_ADDRESS=0x4200000000000000000000000000000000000006  # WETH

# Project tokens
VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
DIEM_TOKEN_ADDRESS=0xF4d97F2da56e8c3098f3a8D538DB630A2606a024
```

## References

Environment variable defaults live in `config/broker-fixes.env.template`. For operational policy, read `docs/implementation-plan-agents.md` and `docs/implementation-plan-broker.md`. Tokenomics context remains in `docs/venice-diem-tokenomics.md`.
