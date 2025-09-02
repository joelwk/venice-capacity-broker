# Admin Control Panel

Minimal static admin UI mounted under the Broker API at `/admin`.

Overview
- Purpose: day‑to‑day ops + demo leverage without extra infra
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
  - Admin act‑as tenant: `POST /v1/chat` with `X-Tenant-Id: <id>`
  - Model: request `model` or global `BROKER_DEFAULT_MODEL`

Run Instructions
- Install (uv): `uv run uvicorn app:app --app-dir apps/broker-api --reload`
- Install (venv/pip): create venv, `pip install -r requirements.txt`, then `python -m uvicorn app:app --app-dir apps/broker-api --reload`
- Open UI: `http://127.0.0.1:8000/admin/`
- Paste token: `BROKER_ADMIN_TOKEN` into the Auth card (stored in localStorage)

Security
- Production: set `BROKER_REQUIRE_ADMIN_TOKEN=true` and a strong `BROKER_ADMIN_TOKEN`
- UI stores token only in the browser; click Clear to remove it
- Admin actions use the same backend auth as the `/v1/*` endpoints

Environment & Backends
- Store backend: `BROKER_STORE_BACKEND=sql|json` (SQL requires SQLModel + configured `SQL_DATABASE_URL`)
- KV (limits/idempotency): autodetects Redis (`REDIS_URL`/`KV_REDIS_URL`) or Replit DB (`KV_URL`/`REPLIT_DB_URL`); otherwise in‑memory
- Metrics: `/metrics` uses starlette-exporter if installed (`METRICS_BACKEND=starlette`), else builtin text metrics

Troubleshooting (what we hit)
- Nix read‑only store errors with pip: install into a venv or use `--user`
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
  - Set `BROKER_STORE_BACKEND=sql`, configure `SQL_DATABASE_URL`
  - Drive traffic; compact counters: `python apps/cli/main.py data:compact-counters --force`
  - Verify counters: `GET /v1/debug/counters?tenant_id=<id>&limit=20`
- Observability
  - Add `starlette-exporter` dependency; set `METRICS_BACKEND=starlette`
  - Optionally surface select metrics in `/admin`
- Security/Config
  - Flip `BROKER_REQUIRE_ADMIN_TOKEN=true` in prod and store token via secure secret
  - Ensure `BROKER_DEFAULT_MODEL` is set via secrets
- Docs & Helpers
  - Keep `/v1/env` updated; add CLI `env:status` and Makefile shortcuts as needed

