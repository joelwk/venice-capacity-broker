# VVV Agents Monorepo

Proprietary notice: This repository is private and not open source. All rights reserved. See LICENSE and NOTICE for details.

A scaffolded framework for a multi-agent system integrating LangGraph/LangChain orchestration with Coinbase AgentKit and Venice AI. It follows the implementation blueprint in `implementation-plan` and establishes a clean structure to build wallet, staking, DIEM, autonomous key issuance, and brokered capacity services.

## Structure

```
apps/
  broker-api/        # Capacity Broker API + static /admin panel
  control-plane/     # Admin UI (static, mounted at /admin)
  cli/               # Operator CLI (argparse-based)
services/
  wallet/            # Wallet providers (Smart Wallet + ETH account)
  staking/           # VVV staking client (on-chain via AgentKit)
  diem/              # DIEM mint/burn/trade (on-chain via AgentKit)
  venice_keys/       # Venice API key issuance manager
  marketdata/        # Price/quotes provider (DEX aggregator + Venice)
  risk/              # Risk policy (limits + exposure helpers)
agents/
  stake_master/      # Keeps staking optimal
  arbi_diem/         # DIEM arbitrage executor
  capacity_broker/   # Issues scoped keys and allocates capacity
  ai_treasurer/      # Treasury policies for VVV/DIEM
  quorum/            # Quorum voting + listen interval policy
graph/
  nodes/             # Node functions (observe/decide/execute)
  workflows/         # High-level workflows (composable)
libs/
  venice_sdk/        # Thin Venice client (autonomous key flow)
  agentkit_ext/      # AgentKit action wrappers (stubs)
  risk/              # Risk policy helpers
  pricing/           # DIEM fair value helpers
  telemetry/         # Logging and basic tracing
infra/
  docker/            # Dockerfile placeholders
  k8s/               # K8s placeholders
  terraform/         # IaC placeholders
config/
  default.yml        # Central app configuration template
tests/               # Minimal import sanity tests
```

## Quickstart

- Python 3.10+
- Copy `.env.example` to `.env` and fill values as you integrate real services.
- Dependency management uses `uv` with `pyproject.toml` (requirements.txt remains for compatibility).

Install with uv (recommended):

```
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
uv sync --extra dev
```

Or with pip (compat mode):

```
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optional dependencies (install via extras as you go):

```
# Broker API default stack (db + kv + tracing + migrations)
uv sync --extra broker
# Database + KV + tracing (legacy split)
uv sync --extra db --extra kv --extra tracing
# Web3 + AgentKit
uv sync --extra web3 --extra agentkit
# LangGraph / LangChain helpers
uv sync --extra graph
```

On-chain and wallets:
- Coinbase CDP Smart Wallet (recommended) and ETH account are supported via `coinbase-agentkit` + `cdp-sdk`.
- Configure one of:
  - Smart Wallet (no EOA needed):
    - `WALLET_PROVIDER=smart_wallet`
    - `NETWORK_ID=base-mainnet` (or `base-sepolia`)
    - `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`
    - Optional: `BASE_RPC_URL`, `PAYMASTER_URL`
    - Note: `OWNER` is only required if you call `sign_message` with a smart wallet (e.g., some web3 challenges).
  - Dev EOA:
    - `WALLET_PROVIDER=eth_account`
    - `ETH_PRIVATE_KEY`, `BASE_RPC_URL`, `BASE_CHAIN_ID`
- Base gating enforced: only base-mainnet or base-sepolia are allowed.
To enable on-chain calls: set `BASE_RPC_URL` and provide ABIs in `abi/`.

DIEM on-chain actions (live):
- Mint/burn via CLI using DIEMService and AgentKit actions. Requires `DIEM_TOKEN_ADDRESS` and `abi/diem.json`.
  - Mint: `python apps/cli/main.py diem:mint <amountBaseUnits> [--dry-run] [--idem-key K] [--corr-id ID]`
  - Burn: `python apps/cli/main.py diem:burn <amountBaseUnits> [--dry-run] [--idem-key K] [--corr-id ID]`
- Stake DIEM for daily API credits via the new `DIEMService.stake_for_api` helper. Configure `DIEM_STAKING_ADDRESS` (or allow it to fall back to `DIEM_TOKEN_ADDRESS`), optional `DIEM_STAKING_ABI`, and `DIEM_STAKE_FN` when the staking function name differs.
- Optional sVVV capacity gate and lock/unlock hooks can be enabled via env (see DIEM mint/burn gate below).

StakeMaster heartbeat (Venice allocation):
- The staking agent keeps an active-staker heartbeat by issuing small inference calls on a configurable interval.
  - Interval and controls: `STAKEMASTER_HEARTBEAT_INTERVAL_HOURS` (default 48), `STAKEMASTER_HEARTBEAT_DISABLE=1` to turn it off, `STAKEMASTER_HEARTBEAT_PROMPT` to customize the ping message.
  - Model selection: set `VENICE_HEARTBEAT_MODEL` (falls back to `VENICE_DEFAULT_MODEL`, default `venice-pro`). The heartbeat uses `VENICE_API_KEY` (or the default Venice client configuration) and emits `staking.heartbeat` events.
  - Active-staker thresholds: configure `VVV_ACTIVE_MIN_STAKE_UNITS` (base units) and optional `VVV_COOLDOWN_SECONDS` so the agent can surface cooldown countdowns in telemetry.

Buy/burn on discount:
- When market price falls sufficiently below fair value, ArbiDiem can buy DIEM on the reversed `TRADE_PATH` (e.g., `USDC -> WETH -> DIEM`) using exact‑out quotes and then burn those units, honoring the same risk and slippage caps.

Risk policy:
- Configure DIEM trade sizing and exposure limits via env:
  - `RISK_MAX_DIEM_TRADE_USD` (default 10000)
  - `RISK_MAX_DIEM_INVENTORY_USD` (default 100000)
  - `RISK_MAX_DIEM_TRADE_UNITS` (optional absolute unit cap)
  - `DIEM_DECIMALS` (optional override to avoid on-chain reads; default 18 if fetch fails)
- ArbiDiem sizing:
  - Desired units via `ARBI_DIEM_MINT_UNITS` (default 1000). Final units are min(desired, risk-allowed) at current DIEM price.
  - Slippage cap via `RISK_MAX_SLIPPAGE_BPS` (default 150 bps).
  - Optional portfolio cap wiring in orchestrator (env-gated):
    - `RISK_ENABLE_PORTFOLIO_CAP=true`
    - Inventory (base units): `DIEM_INVENTORY_UNITS`, `VVV_INVENTORY_UNITS`, `USDC_INVENTORY_UNITS`

DIEM mint/burn gate (optional):
- `DIEM_ENABLE_SVVV_GATE`: enable sVVV capacity pre-check before mint
- `DIEM_MINT_RATE_SVVV_PER_DIEM`: integer sVVV base units required per 1 DIEM base unit
- `DIEM_MINT_RATE`: float sVVV tokens per 1 DIEM token (decimals-aware)
- `DIEM_SVVV_AVAILABLE_UNITS`: override available sVVV (base units)
- `DIEM_DECIMALS`, `SVVV_DECIMALS` (or `VVV_DECIMALS`): defaults 18
- `DIEM_STAKING_ADDRESS`, `DIEM_STAKING_ABI`, `DIEM_STAKE_FN`: configure DIEM staking target when it differs from the token contract
- `DIEM_LOCK_ON_MINT`, `DIEM_UNLOCK_AFTER_BURN`, `DIEM_UNLOCK_COOLDOWN_SECONDS`: enable automatic lock/unlock hooks and cooldown metadata around mint/burn

Risk utilization/volatility (optional):
- `RISK_UTIL_ALPHA`: multiplier = `1 + alpha * utilization` (default 0.5)
- `RISK_MAX_VOLATILITY_BPS`: when >0, caps units proportionally when realized vol exceeds cap

Run the CLI help:

```
python apps/cli/main.py --help
```

Run the sample workflow (dry run) — minimal single-agent path:

```
python apps/cli/main.py run:quorum --dry-run
```

Primary v1 loop (orchestrator):

```
python apps/cli/main.py run:orchestrator --dry-run --interval 5.0 --max-cycles 10
```

Dry-run notes:
- Dry-run mode avoids initializing Web3/DEX and uses `DIEM_FAKE_PRICE` (or default 1.0) for decisions. Set `DIEM_FAKE_PRICE=1.5` to simulate premium conditions without RPC. Provide `DIEM_FAKE_MINT_RATE` when you also want to mock the mint-rate premium without hitting Venice metrics.

Broker API (requires FastAPI + Uvicorn):

Run with uv (recommended):

```
uv run uvicorn app:app --app-dir apps/broker-api --reload --host 0.0.0.0 --port 8000
```
See docs/control-plane-smoke-checklist.md for the manual smoke checklist.

Run with pip/venv:

```
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
python -m pip install --upgrade pip wheel setuptools
pip install -r requirements.txt
python -m uvicorn app:app --app-dir apps/broker-api --reload --host 0.0.0.0 --port 8000
```

API index & docs:
- Visit `/` for a small HTML index with links to `/docs` (Swagger UI), `/redoc`, `/health`, and `/metrics`.
- Admin endpoints require `Authorization: Bearer <BROKER_ADMIN_TOKEN>`.

Admin Control Panel:
- Browse `/admin/` for a minimal UI to manage tenants and limits, inspect health/env, and send a chat probe.
- The UI prompts once for `BROKER_ADMIN_TOKEN` and stores it in your browser `localStorage`.
- Chat probe requires a model: set `BROKER_DEFAULT_MODEL` (or provide a model field in the form).

Buyer Flow (flag‑gated):
  - Baseline: enable `QUOTES_ENABLED=true` and `PURCHASES_ENABLED=true`, set `TREASURY_ADDRESS`, `ACCEPT_ASSETS=ETH,USDC`, and (for USDC) `USDC_ADDRESS`. Open `/admin/buy.html` → Connect Wallet → pick “By Units” or “By Budget” → Get Quote → Pay → Paste Tx → Key issued. Purchases also support SSE streaming of status.
- Clearing Price (optional): set `CLEARING_ENABLED=true`. Endpoints: `GET /v1/pricing/clearing_price` and SSE `GET /v1/pricing/clearing_price/stream`. Response includes an optional `change24h` when token snapshots are available.
- Bids (optional): set `BIDS_ENABLED=true` and configure EIP‑712 domain: `SIGN_DOMAIN_NAME`, `SIGN_DOMAIN_VERSION`, `CHAIN_ID`. Endpoints: `POST /v1/bids`, `GET /v1/bids?buyer=0x...`, `GET /v1/bids/{bidId}`, and SSE `GET /v1/bids/{bidId}/stream`.
- Settlement v1 (optional): set `SETTLEMENT_ENABLED=true`. Endpoint: `POST /v1/bids/{bidId}/settle` (returns a server quote for Pay & Verify). DEX preview endpoint: `GET /v1/settlement/quote?fromToken=<addr>&toAsset=<ETH|USDC>&amountOut=<minor>[&path=...]` (UniswapV2 only; Aerodrome exact‑out is disabled; falls back to mid‑price with `approx=true`). Preview includes `slippageBps` when derivable and enforces risk caps.
- UI cards hide automatically when features are disabled in `/v1/env.features`.

## Implementation Status (Plan Alignment)

This repository tracks the implementation plan in `implementation-plan` and prioritizes core infrastructure and marketplace first.

- Done: Broker core and marketplace scaffolding
  - Broker API: multi-tenant `/v1` endpoints (tenants, chat, limits), idempotency middleware, optional KV-backed sliding-window limiter, basic metrics, SQL store (default) with JSON fallback.
  - Admin UI: static control panel mounted at `/admin` with auth prompt, health/env view, tenant and limits management, chat probe. Buyer page at `/admin/buy.html`.
  - Marketplace: feature-gated Quotes and Purchases endpoints with on-chain ETH/USDC payment verification on Base; issues scoped Venice subkeys on success.
  - Buyer upgrades: Clearing Price API + SSE (flag‑gated), EIP‑712 bids + SSE (flag‑gated), Settlement v1 via server quotes (flag‑gated), and DEX exact‑out preview (flag‑gated).
  - Venice SDK + Key Manager: autonomous root/subkey flows; CLI and Makefile helpers for rotation, probing, and compaction.

- Needs attention: hardening and productionization
  - Migrations and compaction flows: ensure docs and scripts are reliable for prod.
  - Observability: prefer `starlette-exporter` metrics; consider tracing integration for agent/graph workflows.
  - Security: enforce `BROKER_REQUIRE_ADMIN_TOKEN=true` in production; CORS allowlists for buyer/admin; secret hygiene; clear defaults for `BROKER_DEFAULT_MODEL`.
  - Pricing/risk: evolve static pricing to policy-driven engine; validate rate/decimals across assets; add receipts/audit trails.

- Next steps (in order)
  1) Core hardening: finalize metrics and env introspection; optional Redis-backed limiter where available.
  2) Marketplace to production: enable flags in deploys, admin tables for quotes/purchases/utilization, finalize receipts; polish buyer UX.
  3) Agent operations: wire AgentKit actions end-to-end, expand StakeMaster/ArbiDiem loops, add Quorum and AI Treasurer workflows via LangGraph.
 - Full guide: see `docs/ADMIN.md`.

## Makefile Shortcuts

- `make env-status`: prints `/v1/env` (if reachable) and a local snapshot (KV backend, limiter, SQL, metrics, tracing).
- `make run-broker`: starts `uvicorn app:app` with `--app-dir apps/broker-api` on `0.0.0.0:$(BROKER_API_PORT)`.
- `make health`: GET `$(BROKER_BASE_URL)/health`.
- `make create-tenant TENANT=t1 LABEL="Team A" [QUOTA=0 EXPIRES=...]`: admin create (uses `BROKER_ADMIN_TOKEN`).
- `make chat-admin TENANT=t1 [MESSAGE=Hello] [MODEL=<m>]`: admin act-as `/v1/chat` to generate traffic quickly.
- `make limits-get TENANT=t1` / `make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]`: view/set per-tenant broker limits.
- `make db-compact`: compact KV → SQL counters; `make db-counters TENANT=t1 [LIMIT=20]`: show recent counters.
- `make demo-e2e TENANT=t1 [LABEL=TeamA MESSAGE=Hello LIMIT=20 MODEL=<m>]`: seed tenant, probe chat, compact, and show counters.

## Docker Compose (Postgres + Redis)


### New/Updated Make Targets

- make setup-db: install SQL extras (sqlmodel + psycopg2-binary) via uv or pip.
- make rotate-probe TENANT=t1 [LABEL=TeamA MESSAGE=Hello]: rotate subkey, then probe /v1/chat.
- make server-db-compact [MINUTES=60 DELETE_AFTER=false]: compact KV ? SQL in-process (works with in-memory KV).
- make demo-e2e TENANT=t1 [FORCE_SQL=1 ...]: use FORCE_SQL=1 to seed a local SQL tenant instead of Venice.
- make chat-admin now sets a default Idempotency-Key automatically; override with IDK=<value>.
- make enable-buyer: append buyer feature flags to .env and print restart tips.

Spin up Postgres and Redis locally for full E2E validation (SQL store + Redis-backed limiter):

```
docker compose up -d

# Configure app env to point to services
export SQL_DATABASE_URL=postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/postgres
export REDIS_URL=redis://127.0.0.1:6379/0

# Run tests against services
uv run pytest -q

# Control-plane UI regression
uv run playwright install chromium
uv run pytest tests/ui/test_control_plane_buy_flow.py -q

# Or run the API
uv run uvicorn app:app --app-dir apps/broker-api --reload
```

Stop services:

```
docker compose down -v
```

Notes:
- The application still runs fine without Docker: tests fall back to SQLite; Redis-dependent tests skip if `REDIS_URL` is unset.
  

## DEX Startup Probe (Etherscan v2)

Before relying on DEX quotes, validate your `TRADE_PATH` pairs exist on Base using the built‑in startup probe. It calls Etherscan v2 `proxy/eth_call` (`chainid=8453`) against Uniswap V2 and Aerodrome factories to find pairs and reserves for each hop.

- Env required:
  - `ETHERSCAN_API_KEY`
  - `TRADE_PATH` (addresses in order, e.g., `DIEM,USDC` or `DIEM,WETH,USDC`)
- Run on demand:
  - `uv run python apps/cli/main.py startup:probe`
- Auto‑run on broker start (best‑effort):
  - `make run-broker` prints a one‑screen report before launching the API

Example output (abridged):

```
DEX verify (chain 8453)
Path: 0xf4d97f2...a024 -> 0x42000000...0006 -> 0x833589fC...2913

Hop 1: DIEM -> WETH
 - UniswapV2: (no pair)
 - Aerodrome Volatile: pair=0x... reserves=12345,67890 ts=1725660000
 - Aerodrome Stable: (no pair)

Hop 2: WETH -> USDC
 - UniswapV2: pair=0x... reserves=...
 - Aerodrome Volatile: pair=0x... reserves=...
 - Aerodrome Stable: (no pair)
```

If a hop shows “no pair” across venues, set a viable multi‑hop `TRADE_PATH` (e.g., via WETH) that the probe confirms, then re‑run quotes.

Tip: Keep Base WETH address `0x4200000000000000000000000000000000000006` handy for bridging.

## Liquidity-Aware Preview & Scan

Inspect expected execution and slippage without sending trades. The preview uses router quotes when available and falls back to a constant‑product (UniswapV2) approximation for thin pools; the log marks `approx=true` when the fallback is used.

- Preview slippage and sizing at your current `TRADE_PATH`:
  - `uv run python apps/cli/main.py quotes:preview [--units <base-units>] [--price <usd>]`
  - Prints reserve-cap, adjusted units, and `slippage_bps`. If price discovery fails, it derives DIEM price via `DIEM->WETH` mid × `WETH->QUOTE`.
- Scan smaller inputs to find a viable quote when pools are thin:
  - `uv run python apps/cli/main.py market:best-price:scan --start 1.0 --min 1e-12 --factor 10`

Environment tips:
- Prefer multi-hop `TRADE_PATH` on Base for DIEM pricing: `DIEM -> WETH -> USDC`.
- Cap input vs first-hop reserves with `RISK_MAX_POOL_TAKE_BPS` (e.g., 25 = 0.25%).

## Venice Alignment Runbook

Ensure Venice API is aligned and ready in your environment:

- Base URL must include `/api/v1` (e.g., `VENICE_API_BASE_URL=https://api.venice.ai/api/v1`).
- Prefer explicit VVV metrics endpoints and override when deployments differ. The orchestrator reads the DIEM mint rate from these endpoints when available:
  - `VENICE_VVV_CIRC_PATH=/vvv/circulatingsupply`
  - `VENICE_VVV_UTIL_PATH=/vvv/utilization`
  - `VENICE_VVV_YIELD_PATH=/vvv/staking_yield`
- DIEM balances/usage come from `GET /api_keys/rate_limits` (no DIEM signals endpoint).
- Probe OpenAPI and get recommended exports:
  - `uv run python apps/cli/main.py venice:probe-openapi --base-url https://api.venice.ai`
- Readiness checks:
  - `make env-status` → `server.venice.ready=true` and models/vvv `readyReason=ok`.

## CI Gate (prod sanity)

Gate deploys on sane prod defaults and Venice readiness:

- Add a CI step:
  - `uv run python apps/cli/main.py ci:gate`
- Fails when:
  - `venice.ready=false` (server `/v1/env`)
  - `signals.offline=true`
  - `BROKER_REQUIRE_ADMIN_TOKEN=false` or admin token missing when required
  - CORS enabled with wildcard origins


Idempotency TTL
- Canonical env is `IDEMPOTENCY_TTL_SECONDS` (default 300). For compatibility, the app also reads `IDEM_TTL_SECONDS`.
- Idempotency applies to `POST /v1/chat` requests using a hash of method+path+body+Idempotency-Key, with a per-tenant TTL window.
```

Notes:
- Entry points add the repo root to `sys.path` for simple local imports.
- On-chain calls now use AgentKit wallet providers underneath; configure env and ABIs:
  - `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`.
  - DEX: dual providers supported (Uniswap V2 and Aerodrome). Set:
    - `DEX_PROVIDERS=uniswap_v2,aerodrome`
    - `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, and optional `AERODROME_STABLE`
    - Legacy `ROUTER_ADDRESS` remains as a fallback for Uniswap V2
  - ABIs: `abi/erc20.json` (provided), plus project-specific `abi/staking.json`, `abi/diem.json`.
  - Trading supports:
    - Uniswap V2 router ABI (`abi/uniswap_v2_router.json`) with `TRADE_PATH`
    - Aerodrome router ABI (`abi/aerodrome_router.json`) with multi-hop support; pool type toggled via `AERODROME_STABLE` (the system auto-tries both stable/volatile)
  - DEX modes:
    - Exact-in (sell): picks best `getAmountsOut` and executes `swapExactTokensForTokens`; auto-fallback to `swapExactTokensForTokensSupportingFeeOnTransferTokens` for FOT tokens.
    - Exact-out (buy): supports `getAmountsIn` and executes `swapTokensForExactTokens` with max-input guard. Slippage set via `SLIPPAGE_BPS`. Note: Aerodrome is skipped for exact-out (ABI lacks `getAmountsIn`); Uniswap V2 handles buy-path exact-out.
  - DEX metrics:
    - `vvv_dex_quotes_total{provider,status}`, `vvv_dex_trades_total{provider,path}`, `vvv_fot_fallback_total{provider}`
    - Aggregator: `vvv_dex_agg_selected_total{provider[,mode]}`, `vvv_dex_agg_no_quotes_total`, `vvv_dex_agg_trade_total{provider,mode}`, `vvv_dex_agg_trade_errors_total{provider,mode}`
    - Latency buckets: `vvv_dex_quote_latency_bucket_total{provider,bucket}`, `vvv_dex_trade_latency_bucket_total{provider,bucket}` with buckets `lt_50ms|lt_100ms|lt_200ms|lt_500ms|lt_1s|lt_2s|ge_2s`
- Events: `diem.mint`, `diem.burn`, `diem.trade` events now include optional `correlationId` when orchestrated.
  - Risk: `vvv_risk_liquidity_checks_total{adjusted}` and `vvv_risk_liquidity_slippage_bucket_total{bucket}` increment when sizing is liquidity-aware.
  - Signals: emits `signal.market.prices` and `signal.market.signals` to the in-process event bus for simple cross-agent consumption.
  - Purchases: emits `purchase.verified` upon successful verification and subkey issuance.
- Venice client now performs real HTTP requests; endpoints are configurable via env.
 - Risk policy integrated with ArbiDiem to gate mint/sell sizes by USD or unit caps.
 - Broker store backend:
  - Default uses SQL (configure `SQL_DATABASE_URL` or `POSTGRES_*`). Install `sqlmodel` and a DB driver like `psycopg2-binary`.
  - For file-based dev only, set `BROKER_STORE_BACKEND=json` (stores `apps/broker-api/tenants.json`).
- Broker limits (admin):
  - View: `GET /v1/tenants/{tenantId}/broker-limits` (requires admin bearer).
  - Set: `POST /v1/tenants/{tenantId}/broker-limits` with `{ "windowSeconds": 60, "maxRequests": 120, "label": "premium" }`.
  - Enforced by `/v1/chat` in addition to global `RATE_LIMIT_*` defaults.

- Broker limits (tenant self-service, new):
  - View my limits: `GET /v1/me/broker-limits` (bearer must be a tenant subkey)
  - Update my limits: `POST /v1/me/broker-limits` with any subset of fields:
    - `windowSeconds`: tenants may increase only (more restrictive). Decreases require admin.
    - `maxRequests`: tenants may decrease only. Increases require admin.
    - `label`: tenants may annotate with labels prefixed `self:` (e.g., `self:throttled`).
  - Stored in KV alongside admin-set defaults and enforced by the limiter.

CLI admin helpers:
- List tenants: `python apps/cli/main.py broker:tenants:list`
- Get tenant limits: `python apps/cli/main.py broker:limits:get --tenant <id>`
- Set tenant limits: `python apps/cli/main.py broker:limits:set --tenant <id> [--window N] [--max N] [--label name]`
  - Uses `BROKER_BASE_URL` or `BROKER_API_HOST`/`BROKER_API_PORT` and `BROKER_ADMIN_TOKEN` for auth.
- Keep secrets in `.env` and never commit them.
 - Revoke tenant key: `python apps/cli/main.py broker:tenants:revoke --tenant <id>`
 - Clean up Venice keys by description prefix: `python apps/cli/main.py venice:keys:cleanup --prefix "T1" [--dry-run]`

Rate limits and KV:
- KV client supports Replit DB style HTTP (`KV_URL`) and optional Redis (`REDIS_URL`).
- Limiter uses a fixed-window counter with atomic `INCR` when Redis is configured; otherwise best‑effort.

LangSmith tracing:
- Enable by setting `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY`.
- Optional `LANGCHAIN_PROJECT` for grouping runs. The LangGraph pipeline run is wrapped for tracing when enabled.

## Configuration

See `config/default.yml` and `.env.example` for environment variables and defaults.

DEX aggregator (Base mainnet example)
- Set providers: `DEX_PROVIDERS=uniswap_v2,aerodrome`
- Routers: `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, optional `AERODROME_STABLE`
- Quote token: `QUOTE_TOKEN_ADDRESS` (e.g., Base USDC)
- Bridge token: `DEX_BRIDGE_TOKEN_ADDRESS` (e.g., Base WETH) for multi-hop
- Pricing path cache (in-memory, token→quote):
  - `PRICE_PATH_CACHE_TTL_SECONDS` (default 1800)
  - `PRICE_PATH_CACHE_MAX` (default 256)

URLs & env vars:
- `BASE_URL`: shell convenience used in examples to hold your API base URL (not read by the app).
- `BROKER_BASE_URL`: full base URL used by the CLI to reach the Broker API. If set, the CLI uses this directly (recommended for Replit).
- `BROKER_API_HOST` / `BROKER_API_PORT`: CLI fallback to construct a URL when `BROKER_BASE_URL` is not set (e.g., `127.0.0.1:8000`). The server does not read these.
- `BASE_RPC_URL`: on-chain Base RPC endpoint for Web3/AgentKit (e.g., `https://mainnet.base.org` or `https://sepolia.base.org`). Unrelated to the Broker API URL.

### Venice API (env pitfalls)

- **`VENICE_API_BASE_URL` selection**: choose with or without `/api` using the OpenAPI spec.
  - Try `GET ${BASE_URL%/}/openapi.json`. If this returns 200, start with `VENICE_API_BASE_URL=${BASE_URL%/}` unless the spec's `servers[0].url` contains `/api`, in which case use `${BASE_URL%/}/api`.
  - If `GET ${BASE_URL%/}/openapi.json` is 404 but `GET ${BASE_URL%/}/api/openapi.json` works, use `VENICE_API_BASE_URL=${BASE_URL%/}/api`.
- **Parent key for subkeys**: set `VENICE_PARENT_KEY` to a parent/root inference key that has permission to create sub-keys. A regular `VENICE_API_KEY` without parent privileges will fail when calling `/v1/keys/sub` (typically 401/403). Obtain a parent key via the wallet challenge flow or your Venice admin UI. The broker will fall back to `VENICE_API_KEY` if `VENICE_PARENT_KEY` is unset, but it must be parent-capable.
- **Avoid double slashes**:
  - Shell: use `${BASE_URL%/}` to trim a trailing `/` before appending paths.
  - Code: trim trailing slashes, e.g., Python .rstrip('/'); JS .replace(/\/+$/, '').

Additional notes for official API base (`https://api.venice.ai/api/v1`):
- Set `VENICE_API_BASE_URL=https://api.venice.ai/api/v1`.
- Subkey/key creation defaults to `POST /api_keys` with `Authorization: Bearer <VENICE_PARENT_KEY>` and body including `apiKeyType` and `consumptionLimit`. If your deployment differs, override `VENICE_CREATE_SUBKEY_PATH`.
- Use `VENICE_API_KEY_TYPE=INFERENCE` (or `ADMIN` if required). Some deployments reject `READ_ONLY` with a 400 invalid enum error.

Quick probes and examples:

```
# Pick your host, then probe OpenAPI location
BASE_URL="https://api.venice.ai"   # or your self-hosted domain
curl -fsSL "${BASE_URL%/}/openapi.json" || curl -fsSL "${BASE_URL%/}/api/openapi.json"

# Example sub-key creation (requires parent key)
curl -sS -H "Authorization: Bearer $VENICE_PARENT_KEY" \
  -H "Content-Type: application/json" \
  -X POST "${BASE_URL%/}/api_keys" \
  -d '{"apiKeyType":"INFERENCE","consumptionLimit":{"diem":10},"description":"tenant-1"}'

# Example web3 root key (wallet-signed)
# 1) Request a challenge (some deployments support POST; others may use GET)
ADDRESS=0xYourWallet
curl -sS -X POST "${BASE_URL%/}/api_keys/generate_web3_key" \
  -H "Content-Type: application/json" \
  -d '{"wallet":"'"$ADDRESS"'"}'

# 2) Sign the returned message/challenge off-chain -> SIG=0x...
# 3) Exchange signature for a root inference key with optional limits
curl -sS -X POST "${BASE_URL%/}/api_keys/generate_web3_key" \
  -H "Content-Type: application/json" \
  -d '{"address":"'"$ADDRESS"'","signature":"'"$SIG"'","apiKeyType":"INFERENCE","consumptionLimit":{"diem":10}}'
```

Broker wrappers (admin):
- `POST /v1/venice/web3/challenge` with `{ "wallet": "0x..." }` returns a signable challenge payload.
- `POST /v1/venice/web3/create-root-key` with `{ "address": "0x...", "signature": "0x...", "apiKeyType": "INFERENCE", "consumptionLimit": {"diem": 10} }` creates a root key.
- `POST /v1/venice/subkey` with `{ "label": "tenant-1", "consumptionLimit": {"diem": 10}, "expiresAt": "2025-12-31T23:59:00Z" }` creates a scoped subkey using a parent key from env (or `parentKey` if provided).
Tenants & keys
- Create a tenant (idempotent): `POST /v1/tenants` (requires `BROKER_ADMIN_TOKEN` and `VENICE_PARENT_KEY`). If the tenant exists, it is returned without minting a new key.
- Rotate key: `POST /v1/tenants?rotate=true` to mint a fresh subkey and update the store. Preserve existing quota/expiry unless you pass overrides in the body.
- Rotate + revoke old: `POST /v1/tenants?rotate=true&revoke_old=true` will, after a successful rotate, attempt to delete the old key via Venice if `key_id` is recorded.
- Revoke tenant key: `POST /v1/tenants/{tenantId}/revoke` attempts a Venice `DELETE /api_keys/{id}` if available, and marks the tenant as revoked.

SQL backend quick smoke (local or Replit SQL)
- Set `SQL_DATABASE_URL` or `DATABASE_URL` to a Postgres connection string.
- Optionally run migrations: `uv run alembic upgrade head` (tables will auto-create on first use otherwise).
- Start API.
- Create a tenant via `POST /v1/tenants` (requires `BROKER_ADMIN_TOKEN` and `VENICE_PARENT_KEY`).
- Compact counters (if using the limiter): `uv run python apps/cli/main.py data:compact-counters --force`.
- Inspect via admin endpoint: `GET /v1/debug/counters?tenant_id=<id>&limit=20` or CLI `counters:show`.

## Cheat Sheet (Broker + CLI)

Create a tenant (admin-only):

```
curl -sS -X POST "${BROKER_BASE_URL%/}/v1/tenants" \
  -H "Authorization: Bearer $BROKER_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "tenant_id": "t1",
        "label": "Team A",
        "quota": null,
        "expires_at": "2025-12-31T23:59:00Z"
      }'
```

List tenants and limits (CLI):

```
python apps/cli/main.py broker:tenants:list
python apps/cli/main.py broker:limits:get --tenant t1
```

Proxy chat via the broker (using tenant subkey):

```
export SUBKEY=<tenant-subkey>
curl -sS -H "Authorization: Bearer $SUBKEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}' \
  "${BROKER_BASE_URL%/}/v1/chat"
```

Counters and compaction:

```
# Admin-only debug counters
curl -sS -H "Authorization: Bearer $BROKER_ADMIN_TOKEN" \
  "${BROKER_BASE_URL%/}/v1/debug/counters?tenant_id=t1&limit=20"

# CLI equivalents
python apps/cli/main.py counters:show --tenant t1 --scope chat --limit 20 --json
python apps/cli/main.py data:compact-counters --force
```

Probe Venice OpenAPI and print recommended env exports:

```
python apps/cli/main.py venice:probe-openapi --base-url https://api.venice.ai
```

## Troubleshooting

- uv not found: add `~/.local/bin` to PATH, e.g., `export PATH="$HOME/.local/bin:$PATH"`.
- Dev dependencies: if `uv sync` fails, include `--extra dev` to pull test/stub deps.
- Replit PATH quirks: use `export PATH` each session; ignore `.bashrc` warnings.
- Uvicorn import string: use `app:app` with `--app-dir apps/broker-api`; `app` alone will fail.
- Nix/pip permission denied: install in a virtualenv or use `pip install --user`.
- SQL URL precedence: app and Alembic read `SQL_DATABASE_URL` first, then `DATABASE_URL`, then `POSTGRES_*`. On Replit, secrets propagate to both server and CLI.
- Alembic URL: `alembic.ini` leaves `sqlalchemy.url` blank; `db/migrations/env.py` injects the URL from env. If you see interpolation errors, ensure you have pulled latest repo.
- Test import errors (`sqlmodel`): ensure stubs load before `db.models` (see `tests/test_admin_counters_endpoint.py`).
- 404/401 creating tenants: verify `VENICE_API_BASE_URL` and `VENICE_PARENT_KEY` (must have sub-key privileges).
- Double slashes in URLs: use `${BASE_URL%/}` in shell and strip trailing slashes in code.
- Debug-only: you can seed `apps/broker-api/tenants.json` with a known key to bypass sub-key creation temporarily (never in production).

## License

Proprietary. Do not distribute.
Addresses on Base:
- Defaults target Base mainnet (`NETWORK_ID=base-mainnet`). Prefilled values in `.env.example` and `config/addresses.base-mainnet.yml`:
  - `VVV_TOKEN_ADDRESS`: 0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
  - `VVV_STAKING_ADDRESS`: 0x321b7ff75154472B18EDb199033fF4D116F340Ff
  - `DIEM_TOKEN_ADDRESS`: 0xF4d97F2da56e8c3098f3a8D538DB630A2606a024
  - `UNISWAP_V2_ROUTER_ADDRESS`: 0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24
  - `AERODROME_ROUTER_ADDRESS`: 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
  - `AERODROME_STABLE`: false (DIEM/USDC volatile pool)

Offline signing test:
- Run a local end-to-end signing test with no network calls:

```
python apps/cli/main.py test:challenge-offline
```

It generates an ephemeral wallet, signs a dummy Venice challenge, and echoes the payload as the Venice client would receive it.

Compare DEX quotes (live, uses configured providers and `TRADE_PATH`):

```
python apps/cli/main.py quotes:compare --amount 1000000
```

This prints quotes from Uniswap V2 and Aerodrome (if both routers are set) and highlights the best output. Set `TRADE_PATH` to comma-separated addresses (e.g., `DIEM,USDC`).

## Replit

- Fully uses `uv` (pip fallback removed). The runner auto-installs `uv` if missing.
- Steps: import to Replit, add Secrets from `.env.example`, click Run.
  - Use the public Replit URL (e.g., `https://<id>.worf.replit.dev`) in your browser and for curl.
  - `/` shows the HTML index; `/docs` opens Swagger UI; `/health` returns `{"status":"ok"}`.
- Recommended Secrets for Replit:
  - `BROKER_BASE_URL` = your public Replit URL
  - `BROKER_ADMIN_TOKEN` = strong random
  - `VENICE_PARENT_KEY` (or `VENICE_API_KEY`)
  - On-chain (optional): `BASE_RPC_URL`, `NETWORK_ID` (base-sepolia|base-mainnet); for Smart Wallet also set `WALLET_PROVIDER=smart_wallet`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`, `OWNER`.
  - Optional extras: set `UV_EXTRAS` to space-separated extras before first run, e.g. `UV_EXTRAS="broker"`, `UV_EXTRAS="broker dev"`, or `UV_EXTRAS="web3 agentkit"`.
    - Under the hood, the runner executes `uv sync --extra <each>` then starts Uvicorn via `uv run`.
- Deployments: create a Web Service from the Deployments panel (details in `infra/replit/README.md`).
  - For Replit Cloud Services: SQL Database, set `SQL_DATABASE_URL` or `DATABASE_URL` from the service credentials. For KV, set `REPLIT_DB_URL`.
 
## Operational Notes

Rate limiting
- Validate ceilings with the built-in probe: `python apps/cli/main.py probe:limits [--auth-bearer <subkey> | --tenant <id>] [--rps N] [--duration S] [--concurrency M]`.
- Recommended starting defaults:
  - KV-only (Replit DB / in-memory): `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_MAX_REQUESTS=60` (~1 req/sec per tenant)
  - Redis-backed (`REDIS_URL` set): `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_MAX_REQUESTS=120` (tune via probe).

Idempotency
- Key format: `idem:{scope}:{tenant_id}:{digest}:{epoch_min}` with TTL window.
- TTL env: `IDEMPOTENCY_TTL_SECONDS` (alias `IDEM_TTL_SECONDS` supported), default 300s.
- Duplicate POST `/v1/chat` within TTL returns 409 and `X-Idempotency-Accepted: false`.
- Admin cleanup: `python apps/cli/main.py idem:purge --prefix idem:chat:<tenantId>` prints a summary and deletes matching keys.

### Target SLOs

- p90 latency: under 200 ms at target `ok_rps` measured by `scripts/limit_probe.py`.
- 429 fraction: below 10% during sustained probe at target `ok_rps` (tune `RATE_LIMIT_*` and per-tenant limits accordingly).
- Error rate: < 1% non-429 errors during probe.
- Metrics coverage: `/metrics` exposes request counters and latency; add alerts if p90 > 200 ms or 429 fraction > 10% for 5m.

## New/Updated Make Targets

- `make setup-db`: install SQL extras (sqlmodel + psycopg2-binary) via uv or pip.
- `make rotate-probe TENANT=t1 [LABEL=TeamA MESSAGE=Hello]`: rotate subkey, then probe `/v1/chat`.
- `make server-db-compact [MINUTES=60 DELETE_AFTER=false]`: compact KV → SQL in-process (works with in-memory KV).
- `make demo-e2e TENANT=t1 [FORCE_SQL=1 ...]`: use `FORCE_SQL=1` to seed a local SQL tenant instead of Venice.
- `make chat-admin` now sets a default Idempotency-Key automatically; override with `IDK=<value>`.

## Buyer Flow (Quotes + Purchases)

- Feature flags (off by default): set in env and restart broker
    - `QUOTES_ENABLED=true` to enable `GET /v1/quotes`
    - `PURCHASES_ENABLED=true` to enable `POST /v1/purchases/verify` and `GET /v1/purchases/{id}`
    - `CORS_ENABLED=true` and `CORS_ALLOW_ORIGINS=https://your-buyer.app,https://your-admin.app`
  - Quotes API
    - `GET /v1/quotes?units=<n>&asset=<ETH|USDC>` → returns `{ quoteId, units, unitPrice, totalPrice, expiresAt }`
    - `GET /v1/quotes?budget=<usd>&asset=<ETH|USDC>` → same shape as above; requires `PRICE_ENGINE=market` so the server can derive DIEM/USD and ETH/USD; send either `units` or `budget`, not both
    - Budgets must cover the minimum quote size (`PRICE_ACCEPTED_MIN_UNITS`, default `0.01` DIEM). Smaller budgets receive a 400 with a helpful error.
  - Pricing (Static engine)
    - `PRICE_UNIT_USDC` (minor units) and/or `PRICE_UNIT_ETH_WEI` (wei) per unit
    - Optional: `PRICE_ACCEPTED_MIN_UNITS`, `PRICE_ACCEPTED_MAX_UNITS`, `PRICE_QUOTE_TTL_SECONDS`
    - Notes on units/multipliers:
      - USDC uses 6 decimals. 1 USDC = 1,000,000 minor units (`10^6`).
     - ETH uses wei. 1 ETH = 1,000,000,000,000,000,000 wei (`10^18`).
     - For proportions, DeFi conventions use 18‑decimals fixed point (aka WAD = `10^18`); basis points (`10^4`) are also common for simple slippage/fees.
- Payments
  - `BASE_RPC_URL` (Base mainnet RPC)
  - `TREASURY_ADDRESS` (receiver)
  - `ACCEPT_ASSETS=ETH,USDC`
  - For USDC: `USDC_ADDRESS` (Base mainnet) and `USDC_DECIMALS=6` (informational)
- Venice key issuance
  - `VENICE_PARENT_KEY` (or `VENICE_API_KEY`) is required to mint subkeys on successful verify

Endpoints
- `POST /v1/purchases/verify` with `{ quoteId, txHash, buyerAddress }` → verifies on Base and issues a subkey
- `GET /v1/purchases/{purchaseId}` → returns status and (if fulfilled) key metadata
 - `GET /v1/purchases/{purchaseId}/stream` → SSE stream of purchase status transitions (e.g., confirmed → fulfilled)
 - `POST /v1/settlement/confirm` → alias to purchase verification (shape‑compatible with `/v1/purchases/verify`)

Buyer UI
- Navigate to `/admin/buy.html`: connect wallet (Metamask), fetch quote, send payment to the treasury address, paste tx hash, retrieve key.
- For ETH, the page offers a one-click “Pay with wallet (ETH)” using `eth_sendTransaction`, and an EIP‑681 deeplink. For USDC, copy address/amount helpers are shown.
- Receipts & audit: verification attaches a JSON receipt to each purchase (stored in SQL) with tx details, quote summary, and verification metadata.

## End-to-End Demo (MVP)

1) Install SQL extras and set env
- `uv sync --extra db` (or `make setup-db`)
- Set `SQL_DATABASE_URL` (or `DATABASE_URL`/`POSTGRES_*`) and `BROKER_ADMIN_TOKEN`.
- Optional: set `BROKER_DEFAULT_MODEL=venice-uncensored` (or your default model).

2) Start API and verify
- `uv run uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port 8000`
- `make env-status`

3) Rotate + probe (recommended)
- `make rotate-probe TENANT=t1 LABEL="Team A" MESSAGE="Hello $(date +%s)"`

4) Compact + view counters
- `make server-db-compact DELETE_AFTER=1`
- `make db-counters TENANT=t1 LIMIT=20`

5) Tune limits and re-probe
- `make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]`
- `make rotate-probe TENANT=t1 MESSAGE="after limits"`

Notes
- CLI compaction (`make db-compact`) may find no keys if KV is in-memory; use `make server-db-compact` in that case.
- Some Venice deployments do not expose `/openapi.json`; if `venice:probe-openapi` fails, set paths directly if needed (e.g., `VENICE_CREATE_SUBKEY_PATH=/api_keys`).

