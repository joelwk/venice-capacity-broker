# Control Plane (Admin UI)

Minimal static admin panel served by the Broker API at `/admin`.

Scope (MVP)
- Tenants: list, create (id/label/quota/expiry), revoke, rotate
- Limits: view/set per-tenant broker limits
- Health/Env: show `/health` + `/v1/env` snapshot
- Chat probe: send a one‑off message as admin act‑as

Implementation
- Static assets in `apps/control-plane/` mounted by FastAPI (`StaticFiles`) at `/admin`
- Plain HTML + vanilla JS (`fetch`) with a token prompt stored in `localStorage`
- No build step required

Files
- `apps/control-plane/index.html` — dashboard with cards for actions
- `apps/control-plane/app.js` — fetch helpers, bearer header, small forms
- `apps/control-plane/style.css` — tiny styling

Usage
- Start the API (see README for uvicorn command)
- Open `http://127.0.0.1:8000/admin/`
- Paste `BROKER_ADMIN_TOKEN` in the Auth card (stored locally in your browser)
- Use the cards to manage tenants, adjust limits, check health/env, and run chat probes

Security
- In production set `BROKER_REQUIRE_ADMIN_TOKEN=true` and a strong `BROKER_ADMIN_TOKEN`
- The UI stores the token in browser `localStorage` only; clear via the Auth card

Troubleshooting
- 401 on admin calls: ensure a valid `BROKER_ADMIN_TOKEN` is set in the Auth card
- 400 on chat probe: set `BROKER_DEFAULT_MODEL` (or provide a model in the form)
- 503 limits KV errors: per‑tenant limits require a KV backend; otherwise limits card may be read‑only

Next
- Optional: surface key counters (`/metrics`) and selected gauges
- Optional: add SQL counters table using `/v1/debug/counters`
- See `docs/ADMIN.md` for end‑to‑end details and follow‑ups from the implementation plan
