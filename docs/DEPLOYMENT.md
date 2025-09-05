Deployment Guide (Simplified)

Audience: AI/ML engineer. No DevOps required.

Overview
- This repo is private and proprietary. Keep it private on GitHub and Replit.
- Recommended path: deploy the Broker API as a Replit Web Service using the provided `.replit` and `replit.nix`.

1) Replit — Quick Deploy
1. Import the repo into Replit (Python template). Ensure the Repl is private.
2. Open “Secrets” and add required env vars (see `.env.example`). Minimal set:
   - `BROKER_ADMIN_TOKEN`: a strong random string
   - `VENICE_PARENT_KEY` (or `VENICE_API_KEY`) for creating tenants
   - KV store: `REPLIT_DB_URL` (or `KV_URL`); optional `KV_NAMESPACE=vvv`, `KV_PREFIX=vvv:`
   - Optional: `BROKER_BASE_URL` = your public Replit URL (enables Makefile/CLI to reach the service)
   - Optional SQL (Replit SQL): `SQL_DATABASE_URL` (or `DATABASE_URL`)
 - DEX aggregator (for on-chain pricing in token watcher):
     - `QUOTE_TOKEN_ADDRESS` (e.g., Base USDC)
     - `DEX_PROVIDERS=uniswap_v2,aerodrome`
     - `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, optional `AERODROME_STABLE`
     - Optional: `DEX_BRIDGE_TOKEN_ADDRESS` (e.g., Base WETH)
     - Optional: price path cache tuning `PRICE_PATH_CACHE_TTL_SECONDS` (default 1800), `PRICE_PATH_CACHE_MAX` (default 256)
  - Risk policy (for ArbiDiem sizing and exposure limits):
    - `RISK_MAX_DIEM_TRADE_USD` (default 10000)
    - `RISK_MAX_DIEM_INVENTORY_USD` (default 100000)
    - `RISK_MAX_DIEM_TRADE_UNITS` (optional hard cap per trade)
    - `DIEM_DECIMALS` (optional override to avoid on-chain reads)
3. Click Run. The app binds to `0.0.0.0:$PORT` and serves `GET /health`.
4. Deployments panel → Create Web Service (auto-detects run command from `.replit`).
5. Verify health: open the webview → `/health` returns `{ "status": "ok" }`.

Validation (Limiter + Idempotency)
- Create a tenant (admin only): `POST /v1/tenants` with `{ tenant_id, label, quota }` and `Authorization: Bearer $BROKER_ADMIN_TOKEN`.
- Probe limiter (CLI): `make chat-admin TENANT=<id> [MESSAGE=Hello]` for a quick write, or run the probe `python scripts/limit_probe.py --tenant <subkey> --rps 15 --duration 30`.
- Idempotency: repeated identical `POST /v1/chat` should return `409` with `X-Idempotency-Accepted: false`.

Makefile shortcuts (operator quality-of-life)
- `make env-status` prints `/v1/env` (if reachable) plus a local snapshot including KV detection (Redis vs Replit DB vs memory).
- `make demo-e2e TENANT=t1 [LABEL=TeamA]` seeds a tenant (SQL if no Venice parent key), sends a chat, compacts counters, and prints recent counters.
- `make db-compact` compacts KV -> SQL counters; `make db-counters TENANT=t1 [LIMIT=20]` shows recent counters.

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
- Recommended Python: 3.10
- Install deps:
  - With uv: `curl -LsSf https://astral.sh/uv/install.sh | sh && uv venv && uv sync --extra dev`
  - Or pip: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Start API: `uv run uvicorn app:app --app-dir apps/broker-api --reload`
- Health: `GET http://127.0.0.1:8000/health`

Risk-aware ArbiDiem (optional operators):
- Set `ARBI_DIEM_MINT_UNITS` to your desired mint lot size (in token units). The risk policy will reduce it as needed using the live DIEM price.

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
- API endpoints:
 - `GET /v1/quotes?units=<n>&asset=<ETH|USDC>`
 - `POST /v1/purchases/verify` with `{ quoteId, txHash, buyerAddress }`
 - `GET /v1/purchases/{purchaseId}`
