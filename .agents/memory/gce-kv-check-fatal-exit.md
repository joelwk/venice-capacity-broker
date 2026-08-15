---
name: GCE KV store check — fatal exit blocks uvicorn
description: The SKIP_STARTUP_TASKS+live path in replit_run.sh exits 1 when no KV URL is found, preventing uvicorn from binding and causing the 44-minute health-check timeout.
---

## Rule

In `scripts/replit_run.sh`, the `SKIP_STARTUP_TASKS=1` + `MODE=live` block checks for `REDIS_URL`, `REPLIT_DB_URL`, or `/tmp/replitdb`. If none are found it must NOT `exit 1` — that kills the process before uvicorn binds, causing the GCE startup probe to time out for the full 44-minute window with zero runtime logs captured.

**Why:** In GCE Reserved VM containers the KV store may be accessed via `KV_API_TOKEN` (HTTP API) rather than `REPLIT_DB_URL` (websocket URL). The script didn't check `KV_API_TOKEN`, so it incorrectly concluded no KV was available and exited. The successful build on 2026-08-14 presumably had `REPLIT_DB_URL` injected by the container; subsequent builds did not.

**How to apply:** When no KV URL is found, log a warning and set `ALLOW_INMEMORY_KV_FALLBACK=1`, then continue. Let the app's runtime KV layer handle the actual error (it has its own fallback/error handling). The health check only needs uvicorn to bind and `GET /` to return 200 — KV availability is irrelevant for that.

**Do NOT add `uv sync` to the deployment run command.** The build-phase `uv sync` installs packages. At runtime in GCE, the package-firewall.replit.local used during builds may not be reachable; a `uv sync` in the run command could hang and prevent uvicorn from starting.
