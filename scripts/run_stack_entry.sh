#!/usr/bin/env bash
set -euo pipefail

is_replit_context() {
	if [[ -n "${REPLIT_DEPLOYMENT:-}" || -n "${REPLIT_ENVIRONMENT:-}" || -n "${REPLIT_ENV:-}" || -n "${REPL_ID:-}" || -n "${REPL_SLUG:-}" || -n "${REPLIT_DB_URL:-}" ]]; then
		return 0
	fi
	return 1
}

# Detect uv
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

export PATH="$PWD/.local/bin:$HOME/.local/bin:$PATH"

# Ensure docker env overrides exist for stack validation when running in ephemeral environments.
# Create an empty stub so `validate-env` passes without clobbering secrets supplied via Replit.
if [ -f ".env.docker" ] && [ -f ".env.docker.example" ]; then
  if cmp -s ".env.docker" ".env.docker.example"; then
    rm ".env.docker"
  fi
fi

if [ ! -f ".env.docker" ]; then
  : > ".env.docker"
fi

if is_replit_context; then
  export SKIP_UV_SYNC="${SKIP_UV_SYNC:-1}"
  export SKIP_REPLIT_PRESTART="${SKIP_REPLIT_PRESTART:-1}"
  export SKIP_ENV_VALIDATION="${SKIP_ENV_VALIDATION:-1}"
  # Skip slow diagnostics in deployments by default
  export SKIP_ENV_DIAGNOSTICS="${SKIP_ENV_DIAGNOSTICS:-1}"
fi

# Avoid running the one-shot stakemaster alongside the orchestrator loop.
# The loop already runs StakeMaster each cycle; a standalone run would exit immediately.
if [ "${AUTOSTART_ORCHESTRATOR:-1}" = "1" ]; then
  if [ "${AUTOSTART_STAKEMASTER:-0}" != "0" ]; then
    echo "[run_stack_entry] Disabling standalone stakemaster (handled by agent-loop)" >&2
  fi
  export AUTOSTART_STAKEMASTER=0
fi

EXTRA_FLAGS="--extra broker --extra web3 --extra agentkit --extra db"
if [ -n "${UV_EXTRAS:-}" ]; then
  for e in $UV_EXTRAS; do
    EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"
  done
fi

export UV_PYTHON="${UV_PYTHON:-python3}"

# Ensure log directory exists early for any child processes
LOG_DIR="${LOG_DIR:-logs}"
if [ -n "$LOG_DIR" ] && [ "$LOG_DIR" != "stdout" ]; then
  mkdir -p "$LOG_DIR" 2>/dev/null || echo "[run_stack_entry] Warning: could not create log directory $LOG_DIR" >&2
fi

# Run diagnostics unless skipped (slow in Replit deployments)
if [ "${SKIP_ENV_DIAGNOSTICS:-0}" != "1" ]; then
  bash scripts/print_env_diagnostics.sh run_stack_entry
else
  echo "[run_stack_entry] SKIP_ENV_DIAGNOSTICS=1; skipping slow diagnostics"
fi
echo "[run_stack_entry] UV extras: ${EXTRA_FLAGS}"
if [ "${SKIP_UV_SYNC:-0}" = "1" ]; then
  echo "[run_stack_entry] SKIP_UV_SYNC=1 set; skipping uv sync"
else
  uv sync $EXTRA_FLAGS
fi

# After dependencies are installed, run Replit prestart tasks (migrations, env checks)
if [ "${SKIP_REPLIT_PRESTART:-0}" = "1" ]; then
  echo "[run_stack_entry] SKIP_REPLIT_PRESTART=1 set; skipping prestart tasks"
elif [ -f "scripts/replit_prestart.sh" ]; then
  bash scripts/replit_prestart.sh || {
    echo "[run_stack_entry] prestart failed" >&2
    exit 1
  }
fi

export AUTOSTART_BROKER_API=${AUTOSTART_BROKER_API:-1}
HTTP_PORT="${REPLIT_SERVER_PORT:-${PORT:-8000}}"
HTTP_HOST="${REPLIT_SERVER_HOST:-0.0.0.0}"
export AUTOSTART_BROKER_HOST=${AUTOSTART_BROKER_HOST:-$HTTP_HOST}
export AUTOSTART_BROKER_PORT=${AUTOSTART_BROKER_PORT:-$HTTP_PORT}
export BROKER_API_HOST=${BROKER_API_HOST:-$AUTOSTART_BROKER_HOST}
export BROKER_API_PORT=${BROKER_API_PORT:-$AUTOSTART_BROKER_PORT}

MODE=${RUN_STACK_MODE:-dry}
echo "[run_stack_entry] Computed MODE=${MODE}"
if [ "$MODE" = "live" ]; then
  export AUTOSTART_ORCHESTRATOR_LIVE=${AUTOSTART_ORCHESTRATOR_LIVE:-1}
  export AUTOSTART_STAKEMASTER_LIVE=${AUTOSTART_STAKEMASTER_LIVE:-1}
  
  # Disable in-memory KV fallback for live mode - require durable store
  export ALLOW_INMEMORY_KV_FALLBACK=0
  
  # Validate that a durable KV store is configured
  # Note: REPLIT_DB_URL is not available in deployments; check /tmp/replitdb file instead
  HAS_KV_STORE=0
  if [ -n "${REDIS_URL:-}" ] || [ -n "${KV_REDIS_URL:-}" ]; then
    HAS_KV_STORE=1
  elif [ -n "${REPLIT_DB_URL:-}" ]; then
    HAS_KV_STORE=1
  elif [ -f "/tmp/replitdb" ]; then
    # In deployments, REPLIT_DB_URL is stored in /tmp/replitdb
    export REPLIT_DB_URL="$(cat /tmp/replitdb)"
    HAS_KV_STORE=1
    echo "[run_stack_entry] Loaded REPLIT_DB_URL from /tmp/replitdb (deployment mode)" >&2
  fi
  
  if [ "$HAS_KV_STORE" -eq 0 ]; then
    echo "[run_stack_entry] ERROR: Live mode requires durable KV store" >&2
    echo "[run_stack_entry] Configure REDIS_URL, KV_REDIS_URL, or REPLIT_DB_URL" >&2
    echo "[run_stack_entry] In deployments, REPLIT_DB_URL should be in /tmp/replitdb" >&2
    exit 1
  fi
else
  export AUTOSTART_ORCHESTRATOR_LIVE=${AUTOSTART_ORCHESTRATOR_LIVE:-0}
  export AUTOSTART_STAKEMASTER_LIVE=${AUTOSTART_STAKEMASTER_LIVE:-0}
fi

export AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY=${AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY:-1}

exec make run-stack
