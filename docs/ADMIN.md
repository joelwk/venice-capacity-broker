# Admin Control Panel

Minimal static admin UI mounted under the Broker API at `/admin`.

Overview
- Purpose: day-to-day ops + demo leverage without extra infra
- Served by: `apps/broker-api/app.py` mounts `apps/control-plane` via Starlette `StaticFiles`
- Tech: plain HTML + vanilla JS; token stored in browser `localStorage`

Features (MVP)
- Tenants
  - List: `GET /v1/tenants`
  - Create: `POST /v1/tenants` (id/label/quota/expiry)
  - Rotate: `POST /v1/tenants?rotate=true[&revoke_old=true]`
  - Revoke: `POST /v1/tenants/{id}/revoke`
  - Inspect: `GET /v1/tenants/{id}`
- Limits
  - View broker limits: `GET /v1/tenants/{id}/broker-limits`
  - Set broker limits: `POST /v1/tenants/{id}/broker-limits` (windowSeconds/maxRequests/label)
- Health/Env
  - Health: `GET /health`
  - Env snapshot: `GET /v1/env`
- Chat Probe
  - Admin act-as tenant: `POST /v1/chat` with `X-Tenant-Id: <id>`
  - Model: request `model` or global `BROKER_DEFAULT_MODEL`

Run Instructions
- Install (uv): `uv run uvicorn app:app --app-dir apps/broker-api --reload`
- Install (venv/pip): create venv, `pip install -r requirements.txt`, then `python -m uvicorn app:app --app-dir apps/broker-api --reload`
- Open UI: `http://127.0.0.1:8000/admin/`
- Paste token: `BROKER_ADMIN_TOKEN` into the Auth card (stored in localStorage)
- Shortcut: `make run-broker` starts the API; `make env-status` prints server/local env including KV detection.

Security
- In production set `BROKER_REQUIRE_ADMIN_TOKEN=true` and a strong `BROKER_ADMIN_TOKEN`
- UI stores token only in the browser; click Clear to remove it
- Admin actions use the same backend auth as the `/v1/*` endpoints
 - Set `AGENTS_PAUSED=true` to pause orchestrator decisions without redeploying

Environment & Backends
- Store backend: SQL is default (configure `SQL_DATABASE_URL` or `POSTGRES_*`). Set `BROKER_STORE_BACKEND=json` only for local file-based development.
- KV (limits/idempotency): autodetects Redis (`REDIS_URL`/`KV_REDIS_URL`) or Replit DB (`KV_URL`/`REPLIT_DB_URL`); otherwise in-memory. Namespacing via `KV_NAMESPACE` and `KV_PREFIX` is supported.
- Metrics: `/metrics` uses starlette-exporter if installed (`METRICS_BACKEND=starlette`), else builtin text metrics.
  - Agent counters (builtin): look for `vvv_agent_decisions_total{agent,action}`.
  - DEX metrics:
    - `vvv_dex_quotes_total{provider,status}` and `vvv_dex_trades_total{provider,path}`
    - `vvv_fot_fallback_total{provider}` when fee-on-transfer fallback is used
    - `vvv_dex_agg_selected_total{provider[,mode]}` chosen route; `vvv_dex_agg_no_quotes_total` when none
    - `vvv_dex_agg_trade_total{provider,mode}` and `vvv_dex_agg_trade_errors_total{provider,mode}`
    - `vvv_dex_quote_latency_bucket_total{provider,bucket}` and `vvv_dex_trade_latency_bucket_total{provider,bucket}` with buckets: `lt_50ms|lt_100ms|lt_200ms|lt_500ms|lt_1s|lt_2s|ge_2s`
    - Circuit: `vvv_dex_circuit_open_total{provider}`, `vvv_dex_circuit_skips_total{provider}`. Configure with `DEX_CIRCUIT_FAILURES` and `DEX_CIRCUIT_COOL_OFF_SECONDS`.
  - Structured events: ArbiDiem/DIEM actions emit `diem.mint`, `diem.burn`, `diem.trade` (now include optional `correlationId`).
  - Risk metrics: `vvv_risk_liquidity_checks_total{adjusted}` and `vvv_risk_liquidity_slippage_bucket_total{bucket}` capture liquidity-aware sizing decisions.
  - Signal bus: emits `signal.market.prices` and `signal.market.signals` to a lightweight in-process event queue for cross-agent wiring.

Decision Record (operators)
- Fields: `agent`, `action`, `price`, `inventoryUsd`, `dry_run`, `correlationId`, `limits` (slippage_bps_cap, max_trade_usd, max_inventory_usd, max_trade_units), `why` (market_price, fair_per_day, threshold_mult, premium, desired_units, suggested_units, exec_price_preview, slippage_bps, slippage_ok, decision, reason), `outcome`.
- Example (JSON, abbreviated):
  `{ "agent": "arbi_diem", "action": "mint_sell", "price": 2.15, "inventoryUsd": 3.0, "correlationId": "...", "limits": {"slippage_bps_cap":150}, "why": {"premium":1.12, "desired_units":1000, "suggested_units":800, "slippage_bps":45, "decision":"mint_sell"}, "outcome": true }`

Troubleshooting (what we hit)
- Nix read-only store errors with pip: install into a venv or use `--user`
- uvicorn import string must be `module:attribute` (use `app:app` with `--app-dir apps/broker-api`)
- 401 Unauthorized on admin actions: set/paste `BROKER_ADMIN_TOKEN`
- 400 on chat probe: set `BROKER_DEFAULT_MODEL` or specify a model in the form

Reflection: What worked vs. didn’t
- Worked
  - Minimal static mount: simple to ship and no extra infra
  - Token prompt + localStorage: fast operator UX
  - Reusing existing `/v1/*` endpoints kept scope small and reliable
- Didn’t (or gotchas)
  - Pip install to Nix store failed; venv fixed it
  - Missed `app:app` import string at first; documented explicit command
  - Chat probe needs a default model; surfaced clearly in the UI and docs

Next Steps (from implementation-plan)
- SQL Store
  - Default backend is SQL; configure `SQL_DATABASE_URL`.
  - Drive traffic; compact counters: `make demo-e2e TENANT=t1` or run `python apps/cli/main.py data:compact-counters --force` after some `/v1/chat` calls.
  - Verify counters: `GET /v1/debug/counters?tenant_id=<id>&limit=20` or `make db-counters TENANT=<id>`.
- Observability
  - Add `starlette-exporter` dependency; set `METRICS_BACKEND=starlette`
  - Optionally surface select metrics in `/admin`
- Security/Config
  - Flip `BROKER_REQUIRE_ADMIN_TOKEN=true` in prod and store token via secure secret
  - Ensure `BROKER_DEFAULT_MODEL` is set via secrets
- Docs & Helpers
  - Keep `/v1/env` updated; `make env-status` prints a concise view for operators.

Makefile Shortcuts (handy in Replit Shell)
- `make create-tenant TENANT=t1 LABEL="Team A"`
- `make chat-admin TENANT=t1 [MESSAGE=Hello]` (admin act-as chat)
- `make limits-get TENANT=t1` / `make limits-set TENANT=t1 WINDOW=60 MAX=60 [LABEL=premium]`
- `make db-compact` and `make db-counters TENANT=t1 [LIMIT=20]`

Buyer Page
- Navigate to `/admin/buy.html` for a minimal Buyer flow: connect wallet, request quote, pay to the treasury address, paste tx hash, and retrieve the issued key.
- For ETH quotes, the page offers a one-click `eth_sendTransaction` and an EIP-681 deeplink. For USDC, copy address/amount helpers and a token link are provided.
 - Audit receipts: Each verified purchase stores a JSON `receipt` (SQL) with tx details, amount, quote summary, and verification metadata. Admin listings include status; detailed receipts can be queried via the DB.

Updates
- Chat idempotency: duplicate payloads within TTL return 409. The `make chat-admin` helper sets a default Idempotency-Key automatically; override with `IDK=<value>`.
- Rotate + probe from shell: `make rotate-probe TENANT=t1 [LABEL=TeamA MESSAGE=Hello]`.
- Server compaction: `make server-db-compact [MINUTES=60 DELETE_AFTER=false]` (works with in-memory KV); then `make db-counters TENANT=t1 [LIMIT=20]`.
