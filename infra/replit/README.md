Replit Deployment

Overview

- Purpose: make this repo runnable and deployable on Replit with zero manual wiring.
- Defaults to launching the Broker API (FastAPI) on Replit’s provided port.

What’s included

- `.replit`: prefers `uv` to install deps via `pyproject.toml` (`uv sync`) and runs `uv run uvicorn` bound to `0.0.0.0:$PORT`. Falls back to `pip install -r requirements.txt` when `uv` is unavailable.
- `replit.nix`: pins a Nix environment with Python 3.10 and common build deps (`pip`, `gcc`, `pkg-config`, `openssl`, `libffi`, `cacert`).
- `.env.example`: example of environment variables you’ll typically set as Replit Secrets.

Run on Replit (Workspace)

1) Import this repo into Replit (Python template).
2) Open “Secrets” and add any required env vars (see `.env.example` and `config/default.yml`). Common ones:
   - `VENICE_API_KEY`
   - `BASE_RPC_URL`, `BASE_CHAIN_ID`
   - Contract/router addresses if you plan to exercise on-chain routes
3) Click Run. Replit will:
   - Use `replit.nix` to set up a Python 3.10 environment
   - Execute `.replit` which will try `uv sync && uv run uvicorn …`; if `uv` is not available it will fallback to `pip install -r requirements.txt && uvicorn …`.
4) Open the webview. Health check is `GET /health`.

Quick scripts and Makefile

- Create a test tenant (admin-only):
  - Ensure `BROKER_ADMIN_TOKEN` and `VENICE_PARENT_KEY` are set in Secrets.
  - Set `BROKER_BASE_URL` to your public Replit URL (recommended for CLI/scripts).
  - Run: `python scripts/create_test_tenant.py --tenant-id t1 --label "Team A"`
  - Optional: probe chat as admin act-as: `python scripts/create_test_tenant.py --tenant-id t1 --label "Team A" --probe-chat`

- Makefile helpers (override `BROKER_BASE_URL` if not set):
  - `make health`
  - `make create-tenant TENANT=t1 LABEL="Team A"`
  - `make chat-admin TENANT=t1 [MESSAGE=Hello]`
  - `make limits-get TENANT=t1`
  - `make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]`

Deploy on Replit (Deployments)

- From the running Repl, open the Deployments panel and create a Web Service.
- The run command is auto-detected from `.replit`; if prompted, use:
  `pip install -r requirements.txt && uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port $PORT`
- Ensure environment variables (secrets) are configured in the Deployment settings.

SQL Database (optional)

- If you enable Replit Cloud Services: SQL Database, set either `SQL_DATABASE_URL` or `DATABASE_URL` with the Postgres connection string provided by Replit.
- To exercise the SQL-backed features:
  - Set `BROKER_STORE_BACKEND=sql` and install db extras (already in requirements.txt; with uv: `uv sync --extra db`).
  - Run migrations if desired via Alembic (optional; tables auto-create on start):
    `uv run alembic upgrade head`
  - Create a tenant via the admin API (requires `BROKER_ADMIN_TOKEN` and a valid `VENICE_PARENT_KEY`):
    `POST /v1/tenants { tenant_id, label, quota, expires_at? }`
  - Compact KV rate-limit buckets into SQL counters:
    `uv run python apps/cli/main.py data:compact-counters --force`
  - Inspect counters:
    - Admin API: `GET /v1/debug/counters?tenant_id=<id>&limit=20`
    - CLI: `uv run python apps/cli/main.py counters:show --tenant <id> --limit 20 --json`

Notes

- The app must bind to `0.0.0.0` and respect Replit’s `$PORT` environment variable; the included `.replit` does this.
- Optional dependencies (`fastapi`, `uvicorn`) are listed in `requirements.txt` so they install automatically.
- For CLI usage inside Replit, open the Shell and run:
  `python apps/cli/main.py --help`
  `python apps/cli/main.py run:quorum --dry-run`

Metrics and Observability

- Prometheus metrics exposed at `GET ${METRICS_PATH:/metrics}`.
  - If `starlette_exporter` is installed (default), metrics include `starlette_requests_total` and latency histograms.
  - If not installed, builtin counters are exposed: `vvv_requests_total`, `vvv_errors_total`, `vvv_request_latency_seconds_sum`, and `vvv_requests_by_path_total{path="..."}`.
- Example PromQL (starlette_exporter):
  - 429 rate-limit responses on chat: `sum(rate(starlette_requests_total{path="/v1/chat", status="429"}[5m]))`
  - Request rate by path: `sum(rate(starlette_requests_total[5m])) by (path)`
- Example PromQL (builtin fallback):
  - Requests by path: `vvv_requests_by_path_total` (counter; use `increase(vvv_requests_by_path_total[5m])` for deltas)
  - Errors total: `vvv_errors_total`

Rate Limiting

- Enable limiter with:
  - `RATE_LIMITS_ENABLED=true`
  - `RATE_LIMIT_WINDOW_SECONDS=60`
  - `RATE_LIMIT_MAX_REQUESTS=60`
- Optional Redis for cross-process atomicity: set `REDIS_URL`.
- Per-tenant overrides stored in KV at `broker:tenant:{tenantId}:limits` with shape `{ "windowSeconds": N, "maxRequests": M, "label": "premium" }`.
- Admin CLI helpers:
  - `python apps/cli/main.py broker:limits:get --tenant <id>`
  - `python apps/cli/main.py broker:limits:set --tenant <id> --window 60 --max 120 --label premium`

KV→SQL Compaction

- Nightly job recommended to compact KV counters into SQL `counter` table:
  - Command: `python apps/cli/main.py data:compact-counters --force`
  - Gate with `KV_SQL_COMPACTION_ENABLED=true` to allow without `--force`.
  - Environment:
    - `KV_COMPACTION_PREFIX` (default `rl:tenant:`)
    - `KV_COMPACTION_DELETE` (set `true` to delete keys after compaction)
- Scheduling options:
  - Replit: use an external scheduler (e.g., GitHub Actions nightly) to invoke the command via a separate runner.
  - Kubernetes: add a CronJob that runs the above command against the same database/KV.

Tracing (LangSmith)

- Enable LangSmith traces for graph and broker spans:
  - `LANGCHAIN_TRACING_V2=true`
  - `LANGCHAIN_API_KEY=<secret>` (and optional `LANGCHAIN_PROJECT`)

Security

- Admin endpoints require a bearer token if configured:
  - Set `BROKER_ADMIN_TOKEN=<secret>`.
  - To enforce presence at startup, set `BROKER_REQUIRE_ADMIN_TOKEN=true` (app will fail to start if `BROKER_ADMIN_TOKEN` is missing).

Limiter Validation

- Use the included probe to validate RPS ceilings with and without Redis:
  - Tenant-subkey mode: `python scripts/limit_probe.py --auth-bearer <tenant-subkey> --rps 15 --duration 30`
  - Admin act-as mode: `BROKER_ADMIN_TOKEN=<token> python scripts/limit_probe.py --tenant-id <tenantId> --rps 15 --duration 30`
  - Optional: set `REDIS_URL` to exercise cross-process atomicity
- Output includes a JSON summary and Prom-style counters to paste into logs:
  - `probe_requests_total`, `probe_success_total`, `probe_rate_limited_total`, `probe_other_errors_total`, plus latency percentiles
  - Example JSON: `{ "attempted":450,"ok":390,"rate_limited":60,"ok_rps":13.0, "latency_ms_p50":85.2 }`
- Recommended starting defaults (tune with the probe):
  - KV-only (Replit Database / in-memory): `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_MAX_REQUESTS=60` ≈ 1 req/sec average per tenant
  - Redis-backed (`REDIS_URL` set): start with `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_MAX_REQUESTS=120` and adjust per probe results
  - For burstier traffic, shorter windows (1–5s) with proportionally smaller `maxRequests` yield faster backoff

Replit Database (KV) and Redis

- The limiter and idempotency features use a simple KV store:
  - Replit Database: set `REPLIT_DB_URL` (or `KV_URL`), optionally `KV_NAMESPACE` and `KV_PREFIX` (defaults to `vvv:`).
  - Redis (preferred for atomic counters): set `REDIS_URL`.
- Idempotency TTL:
  - Canonical env: `IDEMPOTENCY_TTL_SECONDS` (default 300). Backwards-compatible alias `IDEM_TTL_SECONDS` is also read by the app.

Deployments Validation (runbook)

1) Prepare a tenant and limits
   - Set `BROKER_ADMIN_TOKEN`, `VENICE_PARENT_KEY`, and (optionally) `RATE_LIMIT_*`.
   - Create a tenant: `POST /v1/tenants` with `{ tenant_id, label, quota }`.
   - Verify: `GET /v1/tenants` (admin bearer).
2) Run probe
   - In the Replit Shell (tenant mode): `python scripts/limit_probe.py --auth-bearer <tenant-subkey> --rps 15 --duration 30 --concurrency 20`.
   - Or admin act-as mode: `python scripts/limit_probe.py --tenant-id <tenantId> --admin-token $BROKER_ADMIN_TOKEN --rps 15 --duration 30`.
   - Capture the JSON summary (last line) and Prom counters printed to stdout. Record at least: `ok_rps`, `rate_limited`, `latency_ms_p50/p90/p99`.
3) Adjust defaults
   - Tune `RATE_LIMIT_WINDOW_SECONDS` and `RATE_LIMIT_MAX_REQUESTS` per observed capacity and target SLOs (e.g., p90 < 200ms at ok_rps target).
   - Optionally set per-tenant overrides via admin endpoint:
     `POST /v1/tenants/{id}/broker-limits` with `{ "windowSeconds": 60, "maxRequests": 120, "label": "premium" }`.

OpenAPI probe (Venice)

- Detect the correct `VENICE_API_BASE_URL` and key paths for your deployment:
  - `python apps/cli/main.py venice:probe-openapi --base-url https://<your-venice-host>`
  - This prints recommended exports for `VENICE_API_BASE_URL`, `VENICE_CREATE_SUBKEY_PATH`, and (if available) Web3 root key paths.

Web3 root key wrapper (admin)

- The Broker exposes admin-only helpers to standardize onboarding when needed:
  - `POST /v1/venice/web3/challenge` with `{ "wallet": "0x..." }` → returns a signable challenge payload.
  - `POST /v1/venice/web3/create-root-key` with `{ "address": "0x...", "signature": "0x...", "apiKeyType": "INFERENCE", "consumptionLimit": {"diem": 10} }` → returns a root key object.

SQL Smoke Test (SOL)

- Purpose: validate end-to-end SQL path (tenant, traffic, compaction, counters).
- Preconditions: API deployed with `BROKER_STORE_BACKEND=sql` and `SQL_DATABASE_URL` configured.
- One-liner script:
  - `bash scripts/replit_sql_smoke.sh <tenantId>` (defaults to `t-sql-smoke`)
  - Required env: `BROKER_BASE_URL`, `BROKER_ADMIN_TOKEN` (and SQL configured on the server).
- Manual steps if preferred:
  1) Create a tenant (idempotent):
     - `POST ${BROKER_BASE_URL%/}/v1/tenants` with admin bearer.
  2) Generate traffic to `/v1/chat` using admin act-as mode:
     - `python scripts/limit_probe.py --base-url $BROKER_BASE_URL --tenant-id <id> --admin-token $BROKER_ADMIN_TOKEN --rps 15 --duration 30 --concurrency 20`
  3) Compact counters into SQL:
     - `python apps/cli/main.py data:compact-counters --force`
  4) Verify counters:
     - `GET ${BROKER_BASE_URL%/}/v1/debug/counters?tenant_id=<id>&limit=10` with admin bearer.
  5) Archive outputs per “SQL smoke checklist” in `MVP-RELEASE.md`.
