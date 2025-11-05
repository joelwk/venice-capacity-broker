#!/usr/bin/env bash
set -euo pipefail

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

EXTRA_FLAGS="--extra broker --extra web3 --extra agentkit --extra db"
if [ -n "${UV_EXTRAS:-}" ]; then
  for e in $UV_EXTRAS; do
    EXTRA_FLAGS="$EXTRA_FLAGS --extra $e"
  done
fi

export UV_PYTHON="${UV_PYTHON:-python3}"
uv sync $EXTRA_FLAGS

export AUTOSTART_BROKER_API=${AUTOSTART_BROKER_API:-1}
HTTP_PORT="${REPLIT_SERVER_PORT:-${PORT:-8000}}"
HTTP_HOST="${REPLIT_SERVER_HOST:-0.0.0.0}"
export AUTOSTART_BROKER_HOST=${AUTOSTART_BROKER_HOST:-$HTTP_HOST}
export AUTOSTART_BROKER_PORT=${AUTOSTART_BROKER_PORT:-$HTTP_PORT}
export BROKER_API_HOST=${BROKER_API_HOST:-$AUTOSTART_BROKER_HOST}
export BROKER_API_PORT=${BROKER_API_PORT:-$AUTOSTART_BROKER_PORT}

MODE=${RUN_STACK_MODE:-dry}
if [ "$MODE" = "live" ]; then
  export AUTOSTART_ORCHESTRATOR_LIVE=${AUTOSTART_ORCHESTRATOR_LIVE:-1}
  export AUTOSTART_STAKEMASTER_LIVE=${AUTOSTART_STAKEMASTER_LIVE:-1}
else
  export AUTOSTART_ORCHESTRATOR_LIVE=${AUTOSTART_ORCHESTRATOR_LIVE:-0}
  export AUTOSTART_STAKEMASTER_LIVE=${AUTOSTART_STAKEMASTER_LIVE:-0}
fi

export AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY=${AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY:-1}

exec make run-stack
