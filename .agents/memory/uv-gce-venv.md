---
name: uv GCE deployment venv conflict
description: UV_PROJECT_ENVIRONMENT=.pythonlibs is injected by Replit in both build and runtime GCE containers, causing uv to fail in both phases
---

# uv GCE Deployment Venv Conflict

## The Rule
Both the `build` and `run` commands in `[deployment]` must unset `UV_PROJECT_ENVIRONMENT` and `VIRTUAL_ENV` before invoking any `uv` command.

**Why:** Replit sets `UV_PROJECT_ENVIRONMENT=/home/runner/workspace/.pythonlibs` in the shell environment — both during the GCE build container and at runtime. `.pythonlibs` is Replit's flat package directory; it contains installed packages but no Python executable, so `uv` rejects it as an invalid venv. Without the unset, `uv sync` fails the build phase (no Python executable), and `uv run` fails the runtime phase (crashes before uvicorn binds → health check timeout → promote step failure with no logs).

**How to apply:** Any `.replit` `[deployment]` build or run command that uses `uv` needs:
```
unset UV_PROJECT_ENVIRONMENT && unset VIRTUAL_ENV && <uv command>
```
The unset causes `uv` to create/use `.venv` in the project root instead.
