#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-dry}"
if [[ "$MODE" != "dry" && "$MODE" != "live" ]]; then
  echo "Usage: $0 [dry|live]" >&2
  exit 1
fi

export RUN_STACK_MODE="$MODE"
export AUTOSTART_BROKER_PORT="${AUTOSTART_BROKER_PORT:-${PORT:-8000}}"

# Shared runtime defaults; allow external overrides when present.
export CORS_ENABLED="${CORS_ENABLED:-true}"
export CORS_ALLOW_ORIGINS="${CORS_ALLOW_ORIGINS:-https://capacity-broker.replit.app}"
export QUOTES_ENABLED="${QUOTES_ENABLED:-true}"
export PURCHASES_ENABLED="${PURCHASES_ENABLED:-true}"
export REFLEX_ALLOW_INACTIVE_STAKE="${REFLEX_ALLOW_INACTIVE_STAKE:-1}"
export AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY="${AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY:-1}"

if [[ "$MODE" == "live" ]]; then
  export AUTOSTART_ORCHESTRATOR_LIVE=1
  export AUTOSTART_STAKEMASTER_LIVE=1
else
  export AUTOSTART_ORCHESTRATOR_LIVE=0
  export AUTOSTART_STAKEMASTER_LIVE=0
fi

exec bash scripts/run_stack_entry.sh
