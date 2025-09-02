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
   - Optional SQL (Replit SQL): `SQL_DATABASE_URL` (or `DATABASE_URL`)
3. Click Run. The app binds to `0.0.0.0:$PORT` and serves `GET /health`.
4. Deployments panel → Create Web Service (auto-detects run command from `.replit`).
5. Verify health: open the webview → `/health` returns `{ "status": "ok" }`.

Validation (Limiter + Idempotency)
- Create a tenant (admin only): `POST /v1/tenants` with `{ tenant_id, label, quota }` and `Authorization: Bearer $BROKER_ADMIN_TOKEN`.
- Probe limiter: in Replit Shell → `python scripts/limit_probe.py --tenant <subkey> --rps 15 --duration 30`.
- Idempotency: repeated identical `POST /v1/chat` should return `409` with `X-Idempotency-Accepted: false`.

Optional: SQL-backed store (Replit SQL)
- Set `BROKER_STORE_BACKEND=sql` and `SQL_DATABASE_URL`.
- (Optional) `uv run alembic upgrade head` to apply migrations.
- Use CLI to compact KV → SQL counters:
  - `uv run python apps/cli/main.py data:compact-counters --force`
- Inspect counters:
  - API: `GET /v1/debug/counters?tenant_id=<id>&limit=20`
  - CLI: `uv run python apps/cli/main.py counters:show --tenant <id> --limit 20 --json`

2) Local — Quick Run
- Recommended Python: 3.10
- Install deps:
  - With uv: `curl -LsSf https://astral.sh/uv/install.sh | sh && uv venv && uv sync --extra dev`
  - Or pip: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
- Start API: `uv run uvicorn app:app --app-dir apps/broker-api --reload`
- Health: `GET http://127.0.0.1:8000/health`

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

Secrets — Recovery & Rotation
- Purpose: regain admin access if secrets are wiped or rotate on schedule.
- Scope: affects only admin‑gated endpoints; tenant subkeys continue to function.

1) Generate a new strong admin token
- Python: `python -c "import secrets; print(secrets.token_urlsafe(48))"`
- OpenSSL: `openssl rand -hex 32`
- Node.js: `node -e "console.log(require('crypto').randomBytes(32).toString('base64url'))"`
- PowerShell (Windows):
  `\$b = New-Object 'Byte[]' 32; [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes(\$b); [Convert]::ToBase64String(\$b)`

2) Set or restore the secret in your environment
- Replit: Deployments → Secrets → add `BROKER_ADMIN_TOKEN` (and set `BROKER_REQUIRE_ADMIN_TOKEN=true` for production).
- Local dev: add to `.env` (see `.env.example`), then export or use your runner (`uv run` reads env by default).
- GitHub Actions: Settings → Secrets and variables → Actions → New repository secret `BROKER_ADMIN_TOKEN`.
- Kubernetes: `kubectl create secret generic broker-secrets --from-literal=BROKER_ADMIN_TOKEN=<token> --dry-run=client -o yaml | kubectl apply -f -` and ensure the deployment mounts it as env.

3) Restart or redeploy to pick up the new value
- Replit: Stop/Run or Redeploy the Web Service.
- Local: restart the uvicorn process.
- Kubernetes: `kubectl rollout restart deployment/<broker-deployment>`.

4) Verify health and admin access
- Health (no auth): `curl -fsS http://<host>/health` → `{ "status": "ok" }`.
- Admin‑gated endpoint (requires token): for example, create a test tenant (replace values):
  `curl -X POST http://<host>/v1/tenants -H "Authorization: Bearer $BROKER_ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"tenant_id":"test1","label":"test","quota":0}'`
  - Expected: `200` with tenant payload; otherwise `401` indicates the token is not set or incorrect.

Emergency operation (if admin token is missing)
- Temporarily allow startup without admin token by setting `BROKER_REQUIRE_ADMIN_TOKEN=false`.
- Only use for development or to regain access, then immediately set a new `BROKER_ADMIN_TOKEN`, flip `BROKER_REQUIRE_ADMIN_TOKEN=true`, and restart.

Notes & impact
- Rotating `BROKER_ADMIN_TOKEN` does not affect existing tenant subkeys; it only protects admin endpoints.
- Keep the token out of source control; store in secret managers (Replit Secrets, GitHub Actions, K8s Secrets).
- Prefer long, random, URL‑safe tokens (≥32 bytes entropy).

5) Manual Tenant Flow (Admin)
1. Create tenant: `POST /v1/tenants` with `{ tenant_id, label, quota }`.
2. Get tenant: `GET /v1/tenants/{id}`.
3. Set per-tenant broker limits: `POST /v1/tenants/{id}/broker-limits` with `{ "windowSeconds": 60, "maxRequests": 120, "label": "premium" }`.
4. Revoke: `POST /v1/tenants/{id}/revoke`.

Notes
- Do not publish this package to public registries. CI guards forbid publish commands; enforce CI as a required status check.
- Keep LICENSE and NOTICE in the repo to make proprietary status explicit.
- For any doubts, prefer Replit Deployments over custom infra.

