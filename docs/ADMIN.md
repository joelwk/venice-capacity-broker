# Admin Control Panel

Minimal static admin UI mounted under the Broker API at `/admin`.

Overview
- Purpose: day-to-day ops + demo leverage without extra infra
- Served by: `apps/broker-api/app.py` mounts `apps/control-plane` via Starlette `StaticFiles`
- Tech: plain HTML + vanilla JS; token stored in browser `localStorage`

## Operations Runbook

- Use `/admin` to confirm the Broker is reachable, then open the Health and Venice cards first.

- Check the `Signals` badge on the Venice card; it reflects the same `signal.market.*` events that StakeMaster and ArbiDiem emit.

- Run `make env-status` or click the Env Snapshot card after every deploy to compare live settings with your `.env` file.

- Use `make run-stack` when you need to restart the Broker API and helper loops together; set `AUTOSTART_*` toggles before running.

- For manual spot checks you can still launch helpers individually from the shell beside the admin UI:

  - `uv run python apps/cli/main.py run:stakemaster --enable-live` ensures the heartbeat described in `agents/stake_master/agent.py:26` stays active.
  - `uv run python apps/cli/main.py run:orchestrator --dry-run --interval 5.0 --max-cycles 0` evaluates DIEM trades through `graph/workflows/orchestrator.py:27`.
  - `make watch-tokens` runs `services/marketdata/token_watcher.py:654` so the Buyer page can show up-to-date supply metrics.

- When Venice connectivity becomes unstable, open the Venice card and run the inline probe or call `uv run python apps/cli/main.py venice:signals`.

- The Admin UI surfaces limiter counters and receipts on demand; use the Makefile helpers (`make rotate-probe`, `make limits-set`) when you need shell access to the same data.

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
 - Tenant self-service (no admin token)
   - Who am I: `GET /v1/me` (returns `{ role, tenant }` for tenant tokens)
   - My usage: `GET /v1/me/usage`
   - My broker limits: `GET /v1/me/broker-limits`
   - Update my limits: `POST /v1/me/broker-limits`
     - `windowSeconds`: tenants may increase only (more restrictive). Decreasing requires admin.
     - `maxRequests`: tenants may decrease only. Increasing requires admin.
     - `label`: must be prefixed `self:` when set by tenants (purely descriptive).
- Health/Env
  - Health: `GET /health`
  - Env snapshot: `GET /v1/env`
    - Includes `orchestrator.dryRunFakePrice` when set (used by offline dry-runs)
    - Includes `signals.recent` with the last few `signal.market.*` events for operator visibility
    - Includes `venice` config snapshot (baseUrl, vvvPath, offlineSignals) without secrets
    - Includes Venice readiness fields: `ready`, `modelsOk`, `vvvSignalsOk`
  - Venice probe: `GET /v1/admin/venice/probe?base=https://api.venice.ai` (admin) fetches OpenAPI and suggests env exports (base URL and paths)
  - Admin UI card: “Venice Config & Signals” shows config snapshot, recent signals, and inline Path Probe (enter base URL and click “Probe Paths”)
    - Shows a banner-style status; highlights “Venice: NOT READY” when readiness checks fail
  - Offline indicator: status line shows “Signals: OFFLINE” when `VENICE_OFFLINE_SIGNALS=true` or recent signals indicate offline stubs
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
- KV (limits/idempotency): autodetects Redis (`REDIS_URL`/`KV_REDIS_URL`) or Replit DB (`KV_URL`/`REPLIT_DB_URL`). Set `KV_URL` **and** `KV_API_TOKEN` when using Replit DB so broker limits persist after refresh; otherwise the in-memory fallback is used only for local smoke tests. Namespacing via `KV_NAMESPACE` and `KV_PREFIX` is supported.
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
  - Offline fallback (dev): set `VENICE_OFFLINE_SIGNALS=true` to return stubbed signals from `/v1/market/signals` when Venice endpoints are unavailable.

Decision Record (operators)
- Fields: `agent`, `action`, `price`, `inventoryUsd`, `dry_run`, `correlationId`, `ts`, `limits` (slippage_bps_cap, max_trade_usd, max_inventory_usd, max_trade_units), `why` (market_price, fair_per_day, threshold_mult, premium, desired_units, suggested_units, exec_price_preview, slippage_bps, slippage_ok, decision, reason), `outcome`.
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

Validated Front-End Flows
- 2025-09-21 QA sweep confirmed `/admin`, `/admin/buy.html`, and `/docs` load on the live stack.
- `GET /v1/market/prices?symbols=DIEM,VVV,ETH,USDC` now returns non-zero prices (DIEM ~219.3, VVV ~1.2e-5, ETH ~4452.5, USDC = 1.0) and surfaces DIEM/VVV ratios in the dashboard cards.
- Admin broker limits persist after `POST /v1/tenants/{id}/broker-limits` when `KV_URL`/`KV_API_TOKEN` are configured. Refresh the Limits card to verify the new window/max pair.

Buyer Page
- Navigate to `/admin/buy.html` for the Buyer flow. Cards appear once `/v1/env.features` reports `quotes=true` (Step 1) and `purchases=true` (Step 2/3).
- Step 1 (�Get Payment Details�) requests a live quote, shows the amount/address with copy helpers, renders a USD estimate, and starts a visible expiry countdown beside the Market Snapshot sidebar.
- Step 2 (�Confirm Transaction�) stays hidden until a quote is active, then unlocks the wallet + tx hash inputs, enables Connect Wallet, and shows inline alerts/spinner while verification runs. Expired quotes re-disable the step and prompt for a refresh.
- Step 3 (�Receive API Key�) appears only after verification succeeds, presents a success checkmark, exposes the scoped key with a copy button, and shows the formatted expiry.
- All alerts now use friendly copy instead of raw JSON; copy-to-clipboard buttons acknowledge success, and verification errors keep Step 2 visible (disabled) so the message remains readable.
Updates
- Chat idempotency: duplicate payloads within TTL return 409. The `make chat-admin` helper sets a default Idempotency-Key automatically; override with `IDK=<value>`.
- Rotate + probe from shell: `make rotate-probe TENANT=t1 [LABEL=TeamA MESSAGE=Hello]`.
- Server compaction: `make server-db-compact [MINUTES=60 DELETE_AFTER=false]` (works with in-memory KV); then `make db-counters TENANT=t1 [LIMIT=20]`.
- Buyer Upgrades: Clearing Price API + SSE, EIP‑712 Bids + SSE, Settlement (v1) via server quotes, and DEX exact‑out preview are now available behind flags (`CLEARING_ENABLED`, `BIDS_ENABLED`, `SETTLEMENT_ENABLED`).
