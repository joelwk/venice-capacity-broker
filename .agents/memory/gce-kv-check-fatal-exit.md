---
name: GCE venv path — must use non-gitignored directory for deployment venv
description: Root cause of all 44-minute health-check timeouts in GCE Reserved VM deployments.
---

## Root Cause

`.venv/` is in `.gitignore`. In GCE builds, `uv sync` creates `.venv/` inside the ephemeral build container, but when the Repl layer snapshot is created it **excludes gitignored files**. The runtime container receives the Repl layer without `.venv/`, so `uv run uvicorn` silently fails (no venv → no output → no binding), and the GCE health-check probe times out for 44 minutes.

Evidence: the successful build (Aug 14) had its Repl layer take **28 seconds** to create (`.venv/` was present in the workspace on that day and included). All failing builds (Aug 15+) had the Repl layer complete in **16 seconds** — smaller because `.venv/` was excluded.

## Rule

**Build command**: set `UV_PROJECT_ENVIRONMENT=.venv-deploy` (or any path not matched by `.gitignore`) before running `uv sync`. The venv is created at `.venv-deploy/`.

**Run command**: set `UV_PROJECT_ENVIRONMENT=.venv-deploy` before calling `bash scripts/replit_run.sh live`. This propagates to every `uv run` call inside the script.

Do NOT use `unset UV_PROJECT_ENVIRONMENT` alone. That relied on uv auto-discovering `.venv/` in the current directory, which fails when `.venv/` is absent from the runtime container.

**Why `.venv-deploy`**: the gitignore pattern is `.venv/` (matches a directory literally named `.venv`). `.venv-deploy` does not match, so git/Repl-layer includes it.

## Secondary issue (fixed separately)

The `SKIP_STARTUP_TASKS=1` + `MODE=live` block in `scripts/replit_run.sh` called `exit 1` when no KV URL was found (`REDIS_URL`/`REPLIT_DB_URL`/`/tmp/replitdb`). This killed the process before uvicorn bound, also causing the 44-min timeout. Fixed by converting `exit 1` to a warning + `ALLOW_INMEMORY_KV_FALLBACK=1`.

## Do NOT

- Do not add `uv sync` to the run command — package-firewall.replit.local (used during builds) may not be reachable at runtime and would hang.
- Do not use `unset UV_PROJECT_ENVIRONMENT` without also setting a correct value — leaves uv looking for a missing `.venv/`.
