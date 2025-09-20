Deployment Guide (Simplified)

Audience: AI/ML engineer. No DevOps required.

Overview
- This repo is private and proprietary. Keep it private on GitHub and Replit.
- Recommended path: deploy the Broker API as a Replit Web Service using the provided `.replit` and `replit.nix`.

## Fresh Deployment Checklist

1. Install dependencies with `uv sync --extra dev` or run the pip fallback in a virtual environment.

   The CLI loads `.env` on startup, so complete this step before booting any agents.

2. Copy `.env.example` to `.env` and set the Broker, Venice, Base RPC, and DIEM variables that your environment requires.

   Confirm the values with `uv run python apps/cli/main.py env:status` before going live.

3. Run database migrations so counters, purchases, and token snapshots have the expected schema.

   `uv run alembic upgrade head` matches what `make db-migrate` wraps.

4. Launch the Broker API with `uv run uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port 8000`.

   Keep an eye on `/health`, `/metrics`, and `/v1/env` while the service warms up.

5. Warm the DEX discovery cache and verify the configured trade path.

   Invoke `uv run python apps/cli/main.py startup:probe` and review the printed hops, reserves, and cached entries.

6. Seed an operator tenant and confirm limiter buckets.

   `make rotate-probe TENANT=t1` rotates or creates the tenant, sends the admin chat probe, compacts counters, and prints the latest window.

7. Start the automation supervisor so helpers stay up with the API.

   Run `make run-stack` to boot the Broker API, orchestrator (dry-run by default), StakeMaster loop, and token watcher in one command.

   Export `AUTOSTART_ORCHESTRATOR_LIVE=1` or `AUTOSTART_STAKEMASTER_LIVE=1` before the command when you are ready for on-chain execution, or disable any component with `AUTOSTART_<NAME>=0`.

   Keep `make run-broker` around for API-only sessions.

8. When you are ready for on-chain transactions, switch the orchestrator to live mode and remove the dry-run guard on DIEM CLI verbs.

   Always run one dry-run cycle first, then monitor `vvv_agent_decisions_total` and `staking.heartbeat` events.

## Restart Checklist (Existing Deployment)

1. Load secrets from your host or secret manager, then run `make env-status` to verify the Broker sees the expected config.

2. Apply pending migrations with `uv run alembic upgrade head` before traffic resumes.

3. Start the Broker API using the same supervisor or process manager that you use in production.

   Confirm `/health`, `/metrics`, and `/v1/env` report ready status.

4. Restart the helpers that normally run in the background with `make run-stack` so they stand up beside the API, or launch them individually when you are debugging.

   - Orchestrator loop as above.
   - StakeMaster heartbeat as above.
   - Token watcher via `make watch-tokens` or `make watch-tokens-once` for a single refresh.

5. Exercise Venice connectivity and pricing once the services are up.

   `uv run python apps/cli/main.py venice:signals` and `uv run python apps/cli/main.py quotes:preview --units 100` are fast probes.

6. Resume mint or burn only when the heartbeat, limiter counters, and pricing probes look healthy.

   The CLI `diem:mint` and `diem:burn` commands accept `--dry-run` so you can double-check before committing transactions.

## Background Processes

- `make run-stack` launches the Broker API, orchestrator, StakeMaster, and token watcher together via `scripts/start_stack.py`; toggle components with `AUTOSTART_*` env vars and keep it in dry-run mode unless you set the `*_LIVE` flags. The token watcher stays off unless you export `ETHERSCAN_API_KEY`/`BASESCAN_API_KEY` or opt in with `AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY=1`.

- Orchestrator runs from `graph/workflows/orchestrator.py` and is exposed through the CLI `run:orchestrator` parser entry at `apps/cli/main.py:1255`.

- StakeMaster lives in `agents/stake_master/agent.py` and the CLI `run:stakemaster` entry wires in optional live claims.

- The token watcher service in `services/marketdata/token_watcher.py` stores price and supply snapshots when you use the `make watch-tokens` target defined near line 158 of the Makefile.

- Brokers that rely on Venice metrics should also keep `uv run python apps/cli/main.py venice:signals` in a periodic cron or task runner to surface transient network failures quickly.

1) Replit — Quick Deploy
1. Import the repo into Replit (Python template). Ensure the Repl is private.
2. Open Secrets and add required env vars (see `.env.example`). Minimal set:
   - `BROKER_ADMIN_TOKEN`: a strong random string
   - `VENICE_PARENT_KEY` (or `VENICE_API_KEY`) for creating tenants
   - KV store: `REPLIT_DB_URL` (or `KV_URL`); optional `KV_NAMESPACE=vvv`, `KV_PREFIX=vvv:`
   - Optional: `BROKER_BASE_URL` = your public Replit URL (enables Makefile/CLI to reach the service)
   - Optional SQL (Replit SQL): `SQL_DATABASE_URL` (or `DATABASE_URL`)
 - Optional CDP Smart Wallet: `WALLET_PROVIDER=smart_wallet`, `NETWORK_ID=base-mainnet`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`
 - DEX aggregator (for on-chain pricing in token watcher):
     - `QUOTE_TOKEN_ADDRESS` (e.g., Base USDC)
     - `DEX_PROVIDERS=uniswap_v2,aerodrome`
     - `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, optional `AERODROME_STABLE`
     - Optional: `BRIDGE_TOKEN_ADDRESS` (e.g., Base WETH)
     - Optional: price path cache tuning `PRICE_PATH_CACHE_TTL_SECONDS` (default 1800), `PRICE_PATH_CACHE_MAX` (default 256)
  - Risk policy (for ArbiDiem sizing and exposure limits):
    - `RISK_MAX_DIEM_TRADE_USD` (default 10000)
    - `RISK_MAX_DIEM_INVENTORY_USD` (default 100000)
    - `RISK_MAX_DIEM_TRADE_UNITS` (optional hard cap per trade)
    - `DIEM_DECIMALS` (optional override to avoid on-chain reads)
  - DIEM staking/mint hooks (optional):
    - `DIEM_STAKING_ADDRESS`, `DIEM_STAKING_ABI`, `DIEM_STAKE_FN`
    - `DIEM_LOCK_ON_MINT`, `DIEM_UNLOCK_AFTER_BURN`, `DIEM_UNLOCK_COOLDOWN_SECONDS`
  - StakeMaster heartbeat:
    - `STAKEMASTER_HEARTBEAT_INTERVAL_HOURS` (default 48)
    - `STAKEMASTER_HEARTBEAT_DISABLE` to turn it off
    - `STAKEMASTER_HEARTBEAT_PROMPT`
    - `VENICE_HEARTBEAT_MODEL` or fallback `VENICE_DEFAULT_MODEL`
    - `VVV_ACTIVE_MIN_STAKE_UNITS`, `VVV_COOLDOWN_SECONDS`
3. Click **Run**. The default runnable (**Stack (dry-run)**) calls `scripts/run_stack_entry.sh` with `RUN_STACK_MODE=dry`, which installs deps with `uv sync`, starts the Broker API, orchestrator, StakeMaster, and token watcher, and keeps the orchestrator in dry-run mode.
4. When you are ready for real trades or staking heartbeats, pick the **Stack (live)** runnable (or export `RUN_STACK_MODE=live`) so the same script enables `AUTOSTART_ORCHESTRATOR_LIVE` and `AUTOSTART_STAKEMASTER_LIVE`.
5. Deployments panel -> Create Web Service (auto-detects the run command from `.replit`).
6. Verify health: open the webview -> `/health` returns `{ "status": "ok" }`.
7. Use the Replit Workflows panel when you need to rerun the stack: **Run Venice Stack (dry)** drives the dry-run mode and **Run Venice Stack (live)** enables the live flags.

Validation (Limiter + Idempotency)
- Create a tenant (admin only): `POST /v1/tenants` with `{ tenant_id, label, quota }` and `Authorization: Bearer $BROKER_ADMIN_TOKEN`.
- Probe limiter (CLI): `make chat-admin TENANT=<id> [MESSAGE=Hello]` for a quick write, or run `python scripts/limit_probe.py --tenant <subkey> --rps 15 --duration 30`.
- Idempotency: repeated identical `POST /v1/chat` returns `409` with `X-Idempotency-Accepted: false`.
 - Tenant self-service: authenticate with a tenant subkey and call:
   - `GET /v1/me/broker-limits` to view current limiter settings
   - `POST /v1/me/broker-limits` to tighten limits (increase `windowSeconds`, decrease `maxRequests`)

Makefile shortcuts (operator quality-of-life)
- `make env-status` prints `/v1/env` (if reachable) plus a local snapshot including KV detection (Redis vs Replit DB vs memory).
- `make demo-e2e TENANT=t1 [LABEL=TeamA]` seeds a tenant (SQL if no Venice parent key), sends a chat, compacts counters, and prints recent counters.
- `make db-compact` compacts KV → SQL counters; `make db-counters TENANT=t1 [LIMIT=20]` shows recent counters.

Default: SQL-backed store (Replit SQL or Postgres)
- Ensure `SQL_DATABASE_URL` (or `POSTGRES_*`) is configured.
- (Optional) `uv run alembic upgrade head` to apply migrations.
- For a file-based dev fallback, set `BROKER_STORE_BACKEND=json`.
- Use CLI to compact KV + SQL counters:
  - `uv run python apps/cli/main.py data:compact-counters --force` or `make db-compact`
- Inspect counters:
  - API: `GET /v1/debug/counters?tenant_id=<id>&limit=20`
  - CLI: `uv run python apps/cli/main.py counters:show --tenant <id> --limit 20 --json` or `make db-counters TENANT=<id>`

2) Local — Quick Run
- Recommended Python: 3.10+
- Install deps:
  - With uv: `curl -LsSf https://astral.sh/uv/install.sh | sh && uv venv && uv sync --extra dev`
  - Or pip: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Start API: `uv run uvicorn app:app --app-dir apps/broker-api --reload`
- Health: `GET http://127.0.0.1:8000/health`

Risk-aware ArbiDiem (optional operators)
- Set `ARBI_DIEM_MINT_UNITS` to your desired mint lot size (in token units). The risk policy will reduce it as needed using the live DIEM price.
- Slippage cap via `RISK_MAX_SLIPPAGE_BPS` (default 150 bps).
- Portfolio-cap wiring in orchestrator (env-gated): `RISK_ENABLE_PORTFOLIO_CAP=true` and set `DIEM_INVENTORY_UNITS`, `VVV_INVENTORY_UNITS`, `USDC_INVENTORY_UNITS`.
- DIEM on-chain actions (live): ensure `DIEM_TOKEN_ADDRESS` and `abi/diem.json` are present; use CLI `diem:mint` and `diem:burn` for direct actions (honors sVVV capacity gate when enabled).
- DIEM staking (optional): configure `DIEM_STAKING_ADDRESS` (and `DIEM_STAKE_FN`/`DIEM_STAKING_ABI` when needed) so workflows can call `DIEMService.stake_for_api` to park DIEM for API credits.
- Dry runs: set `DIEM_FAKE_PRICE` and `DIEM_FAKE_MINT_RATE` to simulate market/mint conditions without hitting Venice or Web3.

DEX trading modes (Base)
- Providers: set `DEX_PROVIDERS=uniswap_v2,aerodrome`, router envs (`UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, optional `AERODROME_STABLE`).
- Exact-in (sell): aggregator uses `getAmountsOut` and executes `swapExactTokensForTokens`. Fee-on-transfer tokens trigger automatic fallback to `swapExactTokensForTokensSupportingFeeOnTransferTokens`.
- Exact-out (buy): aggregator uses `getAmountsIn` and executes `swapTokensForExactTokens` with max-input guard. Slippage via `SLIPPAGE_BPS`.
- Operator note: Aerodrome router ABI in this repo lacks `getAmountsIn` (no exact-out). The aggregator intentionally skips Aerodrome for exact-out; Uniswap V2 handles buy-path exact-out.

Observability (operators)
- Metrics at `${METRICS_PATH:/metrics}`. Use `METRICS_BACKEND=starlette` for Prometheus middleware if installed.
- DEX telemetry: `vvv_dex_quotes_total`, `vvv_dex_trades_total`, `vvv_fot_fallback_total`, aggregator selections/errors, and latency buckets `vvv_dex_*_latency_bucket_total`.
- Decisions: `vvv_agent_decisions_total{agent,action}`. Events `diem.mint|burn|trade` include optional `correlationId` when called by the orchestrator.

3) GitHub — Safe CI Only (no publish)
- The repo includes `.github/workflows/ci.yml` which:
  - Blocks publish commands in workflows/scripts (twine upload, npm publish, etc.).
  - Runs `pytest` on push/PR.
  - Provides a no-op “Deployment (manual via Replit)” job with a pointer to this guide.
- Recommended repository settings:
  1. Keep the repository Private.
  2. Settings → Actions → General:
     - Allow GitHub Actions from GitHub only (or “Allow select actions”).
     - Disable or restrict reusable workflows from public sources.
  3. Settings → Branches → Add rule for `main`:
     - Require a pull request before merging (with 1 reviewer).
     - Require status checks to pass → select “CI” workflow jobs.
     - Block force pushes and deletions.
  4. Settings → Secrets and variables → Actions: store any secrets here (not in code).

4) Minimal Operational Checklist
- Admin token set and required (`BROKER_REQUIRE_ADMIN_TOKEN=true` in production).
- KV configured (`REPLIT_DB_URL` or Redis via `REDIS_URL`).
- Rate limits tuned (`RATE_LIMIT_WINDOW_SECONDS`, `RATE_LIMIT_MAX_REQUESTS`).
- Observability:
  - Metrics at `${METRICS_PATH:/metrics}` (starlette_exporter or builtin).
  - Optional LangSmith tracing: set `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY`.
- Sanity check env: `make env-status` shows detected KV backend (redis|replit_db|memory), limiter and SQL flags.
 - Buyer receipts: SQL must be enabled to persist purchase `receipt` JSON. Run `uv run alembic upgrade head` to apply migrations.

Secrets — Recovery & Rotation
- Purpose: regain admin access if secrets are wiped or rotate on schedule.
- Scope: affects only admin-gated endpoints; tenant subkeys continue to function.

1) Generate a new strong admin token
- Python: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- OpenSSL: `openssl rand -hex 32`
- Node.js: `node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"`
- PowerShell (Windows):
  `$b = New-Object 'Byte[]' 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b); [Convert]::ToBase64String($b)`

2) Set or restore the secret in your environment
- Replit: Deployments → Secrets → add `BROKER_ADMIN_TOKEN` (and set `BROKER_REQUIRE_ADMIN_TOKEN=true` for production).
- Local dev: add to your shell env or `.env`.

Database: Postgres URL formats (Replit SQL)
- `postgresql://USER:PASSWORD@HOST:PORT/DBNAME` (recommended)
- `postgresql+psycopg2://USER:PASSWORD@HOST:PORT/DBNAME`
- If it starts with `postgres://`, change to `postgresql://`
- If TLS is enforced, append `?sslmode=require`
- Alternatively, set: `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`.
4) Run migrations (one-time): `uv run alembic upgrade head` (or `python -m alembic upgrade head`).
5) Drive traffic (create a tenant, send `/v1/chat`). Quick path: `make demo-e2e TENANT=t1`.
6) Compact counters: `uv run python apps/cli/main.py data:compact-counters --force` or `make db-compact`.
7) Verify: open `/v1/debug/counters?tenant_id=<id>&limit=20` or `make db-counters TENANT=t1`.

Automation notes
 - With the SQL backend (default), the API ensures tables exist at startup via `SQLModel.metadata.create_all(...)`.
 - The CLI auto-creates tables before `data:compact-counters` and `counters:show` if needed.
 - KV autodetection: Redis if `REDIS_URL`/`KV_REDIS_URL` is set; Replit DB if `REPLIT_DB_URL`/`KV_URL` is set; in-memory otherwise. `/v1/env` and `make env-status` reflect this.

Updated shortcuts and operations
- Rotate + probe in one step: `make rotate-probe TENANT=<id> [LABEL=TeamA MESSAGE=Hello]`.
- Server-side compaction (works with in-memory KV): `make server-db-compact [MINUTES=60 DELETE_AFTER=false]`.
- CLI compaction: `make db-compact` (may not see in-memory KV; prefer server compaction in that case).
- Tune limits then re-probe: `make limits-set TENANT=<id> WINDOW=60 MAX=60` followed by `make rotate-probe TENANT=<id>`.
- Model override per request: `make chat-admin TENANT=<id> MESSAGE="hi" MODEL="venice-uncensored"` or set `BROKER_DEFAULT_MODEL`.

Buyer Flow (flag-gated)
- Shortcut: run `make enable-buyer` to append the common flags to `.env`, then restart the broker.
- Enable features via env and restart broker:
  - `QUOTES_ENABLED=true` and `PURCHASES_ENABLED=true`
  - `CORS_ENABLED=true` with `CORS_ALLOW_ORIGINS=https://your-buyer.app,https://your-admin.app`
  - Pricing: `PRICE_UNIT_USDC` and/or `PRICE_UNIT_ETH_WEI`; optional `PRICE_QUOTE_TTL_SECONDS`
  - Payments: `BASE_RPC_URL`, `TREASURY_ADDRESS`, `ACCEPT_ASSETS=ETH,USDC`, and `USDC_ADDRESS` for USDC
- UI path: `/admin/buy.html` → connect wallet → get quote → pay → paste tx hash → receive subkey
 - Receipts: Successful verification persists a JSON `receipt` on the `Purchase` row (tx, quote summary, verifiedAt). Emit `purchase.verified` on the event bus.
- API endpoints:
 - `GET /v1/quotes?units=<n>&asset=<ETH|USDC>`
 - `POST /v1/purchases/verify` with `{ quoteId, txHash, buyerAddress }`
 - `GET /v1/purchases/{purchaseId}`

Buyer Flow — Extended (clearing price, bids, settlement)
- Flags (enable as needed):
  - `CLEARING_ENABLED=true` (Clearing Price API + SSE)
  - `BIDS_ENABLED=true` (EIP‑712 bids + SSE)
  - `SETTLEMENT_ENABLED=true` (Bid settlement + DEX preview)
  - EIP‑712 domain: `SIGN_DOMAIN_NAME=Venice Broker`, `SIGN_DOMAIN_VERSION=1`, `CHAIN_ID=8453`
- Clearing Price:
  - `GET /v1/pricing/clearing_price` returns `{ price, bandMin, bandMax, band: { min, max }, change24h|null, components, ts }`.
  - `GET /v1/pricing/clearing_price/stream` (SSE) streams the same structure periodically.
  - Tuning: `CLEARING_BAND_BPS` (default 200), `CLEARING_SSE_INTERVAL_SECONDS` (default 5)
- Bids (wallet signs EIP‑712 ‘PurchaseIntent’):
  - `POST /v1/bids` with `{ buyer, units(uint256, micro-units), maxPrice(uint256), asset, expiry, slippageBps, nonce, chainId, signature }`
  - `GET /v1/bids?buyer=0x...` lists recent bids for the wallet
  - `GET /v1/bids/{bidId}` returns bid details
  - `GET /v1/bids/{bidId}/stream` (SSE) streams status (`out_of_band|in_band|accepted_window|expired`)
- Settlement (v1: ETH/USDC, server quote + Pay/Verify):
  - `POST /v1/bids/{bidId}/settle` returns a fresh server quote; enforces `unitPrice <= maxPrice`.
  - Confirm payment via `POST /v1/purchases/verify` or its alias `POST /v1/settlement/confirm` (same body/response) to issue the key.
  - Monitor issuance in real time via SSE `GET /v1/purchases/{purchaseId}/stream`, which emits status updates until fulfilled. If SSE is unavailable, fall back to polling `GET /v1/purchases/{purchaseId}`.
- DEX exact‑out preview (buy‑side; UniswapV2 only):
  - `GET /v1/settlement/quote?fromToken=<addr>&toAsset=<ETH|USDC>&amountOut=<minor>&[path=addr0,addr1,...]`
  - Returns `{ provider|null, path, amountIn, amountOut, approx }` — `approx=true` when mid‑price fallback is used
  - Aerodrome exact‑out is intentionally disabled; provide route overrides via `path` when needed

Notes on pricing and units
- Quotes accept fractional `units` (e.g., `0.10`) for small purchases. Two-decimal precision is supported by default.
- Configure min/max via `PRICE_ACCEPTED_MIN_UNITS` (default `0.01`) and `PRICE_ACCEPTED_MAX_UNITS`.
- Responses include integer `unitPrice`/`totalPrice` in smallest units of the chosen asset (wei for ETH, 6‑decimals for USDC).

Venice alignment (runbook)
- Ensure `VENICE_API_BASE_URL` includes `/api/v1` (example: `https://api.venice.ai/api/v1`).
- Prefer explicit VVV metrics endpoints; override paths if your deployment differs:
  - `VENICE_VVV_CIRC_PATH=/vvv/circulatingsupply`, `VENICE_VVV_UTIL_PATH=/vvv/utilization`, `VENICE_VVV_YIELD_PATH=/vvv/staking_yield`
  - Legacy aggregate if needed: `VENICE_VVV_PATH=/vvv`
- The orchestrator consumes the mint rate reported by these endpoints when available; ensure Venice metrics remain reachable in production.
- Probe and print recommended env via CLI:
  - `uv run python apps/cli/main.py venice:probe-openapi --base-url https://api.venice.ai`
- DIEM balances/usage come from `GET /api_keys/rate_limits` (no DIEM signals endpoint).

CI/Health gate
- Use `make ci-gate` (or `uv run python apps/cli/main.py ci:gate`) to fail builds when:
  - Server mode: `/v1/env` reports `venice.ready=false`, `signals.offline=true`, admin token missing, or not required at startup.
  - Local mode: `BROKER_REQUIRE_ADMIN_TOKEN=false`, missing `BROKER_ADMIN_TOKEN` when required, `VENICE_OFFLINE_SIGNALS=true`, `VENICE_API_BASE_URL` missing `/api/v1`, or CORS wildcard when enabled.
- Optional smoke: `make smoke-quotes-preview` exercises aggregator preview without trades.
