MVP Release Checklist

Scope: Broker API, limiter + idempotency, JSON/SQL tenant store, CLI admin, DIEM premium logic, observability, and Replit Deployments.

- Tag and version
  - Bump version in `pyproject.toml` if needed
  - Create Git tag `v0.1.0-mvp`

- Configuration templates
  - Copy `.env.example` to your secrets manager; fill required fields:
    - `BROKER_ADMIN_TOKEN` and (optional) `BROKER_REQUIRE_ADMIN_TOKEN=true`
    - `VENICE_API_KEY` or `VENICE_PARENT_KEY` (for tenant creation)
    - KV: `REPLIT_DB_URL` (or `KV_URL`), optional `REDIS_URL`
    - SQL: `SQL_DATABASE_URL` or `DATABASE_URL` if using Postgres
    - Limiter: `RATE_LIMIT_WINDOW_SECONDS`, `RATE_LIMIT_MAX_REQUESTS`
    - Idempotency: `IDEMPOTENCY_TTL_SECONDS` (canonical; alias `IDEM_TTL_SECONDS` also supported)

- Local smoke (Docker Compose optional)
  - `docker compose up -d` to start Postgres/Redis
  - `export SQL_DATABASE_URL=postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/postgres`
  - `export REDIS_URL=redis://127.0.0.1:6379/0`
  - `uv run pytest -q` (Redis-dependent tests will skip if `REDIS_URL` is unset)

- Broker API
  - Start: `uv run uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port 8000`
  - Health: `GET /health`
  - Admin tenants: set `BROKER_ADMIN_TOKEN`, then `POST /v1/tenants` to create a tenant (requires `VENICE_PARENT_KEY`)
  - Limits: `GET/POST /v1/tenants/{id}/broker-limits`
  - Metrics: `GET ${METRICS_PATH:/metrics}` (starlette_exporter auto if installed; builtin fallback otherwise)

- Limiter and idempotency
  - Enable limiter: `RATE_LIMITS_ENABLED=true` + window/max
  - Probe: `python scripts/limit_probe.py --tenant <subkey> --rps 15 --duration 30`
  - Idempotency TTL: confirm `IDEMPOTENCY_TTL_SECONDS` active via 409 responses on duplicate payloads

- SQL store and counters
  - Run with `BROKER_STORE_BACKEND=sql`; ensure `SQL_DATABASE_URL` set
  - (Optional) `uv run alembic upgrade head`
  - Compact KV → SQL: `python apps/cli/main.py data:compact-counters --force`
  - Inspect counters: API `/v1/debug/counters?tenant_id=<id>` or CLI `counters:show`

- Replit Deployments
  - Follow `infra/replit/README.md`
  - Ensure `REPLIT_DB_URL` (KV) and `SQL_DATABASE_URL`/`DATABASE_URL` (if using Replit SQL) are configured in the Deployment secrets
  - Run limiter probe from the Replit Shell and record observed RPS; adjust defaults accordingly

- Docs
  - Verify README, infra/replit/README, and `.env.example` reflect canonical env vars (notably `IDEMPOTENCY_TTL_SECONDS`).

