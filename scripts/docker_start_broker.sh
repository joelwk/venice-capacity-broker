#!/usr/bin/env bash
set -euo pipefail

log() {
  printf '[broker-startup] %s\n' "$*"
}

log "Validating capacity broker environment"
python scripts/validate_broker_env.py

log "Applying database migrations"
if ! python -m alembic upgrade head; then
  log "Alembic migration failed; continuing without blocking startup"
fi

if [ "${MARKET_POOLS_REFRESH:-1}" != "0" ]; then
  log "Refreshing DEX pool catalog (market:pools:watch --once)"
  if ! python apps/cli/main.py market:pools:watch --once; then
    log "Pool catalog refresh failed; continuing without blocking startup"
  fi
else
  log "Skipping pool catalog refresh (MARKET_POOLS_REFRESH=${MARKET_POOLS_REFRESH})"
fi

ORIGINAL_REDIS_URL="${REDIS_URL-}"
ORIGINAL_KV_REDIS_URL="${KV_REDIS_URL-}"
ORIGINAL_SKIP_REDIS_TESTS="${SKIP_REDIS_TESTS-}"
export BROKER_SKIP_DOTENV="1"

IS_DOCKER=0
if [ -f /.dockerenv ]; then
  IS_DOCKER=1
fi

IS_REPLIT=0
if [ -n "${REPLIT_ENVIRONMENT:-}" ] || [ -n "${REPLIT_WORKSPACE_ID:-}" ]; then
  IS_REPLIT=1
fi

if [ "$IS_DOCKER" -eq 1 ] && [ "$IS_REPLIT" -eq 0 ]; then
  if [ -z "${REDIS_URL:-}" ]; then
    export REDIS_URL="redis://redis:6379/0"
  fi
  if [ -z "${KV_REDIS_URL:-}" ] && [ -n "${REDIS_URL:-}" ]; then
    export KV_REDIS_URL="${REDIS_URL}"
  fi
  unset SKIP_REDIS_TESTS
  log "Running tests with Redis backing (REDIS_URL=${REDIS_URL})"
else
  export SKIP_REDIS_TESTS="1"
  export REDIS_URL=""
  export KV_REDIS_URL=""
  log "Redis tests skipped (SKIP_REDIS_TESTS=1)"
fi

log "Running test suite before launching broker"
pytest -q

if [ -n "${ORIGINAL_REDIS_URL}" ]; then
  export REDIS_URL="${ORIGINAL_REDIS_URL}"
else
  unset REDIS_URL
fi
if [ -n "${ORIGINAL_KV_REDIS_URL}" ]; then
  export KV_REDIS_URL="${ORIGINAL_KV_REDIS_URL}"
else
  unset KV_REDIS_URL
fi
if [ -n "${ORIGINAL_SKIP_REDIS_TESTS}" ]; then
  export SKIP_REDIS_TESTS="${ORIGINAL_SKIP_REDIS_TESTS}"
else
  unset SKIP_REDIS_TESTS
fi
unset BROKER_SKIP_DOTENV

log "Starting broker API"
exec uvicorn app:app --app-dir apps/broker-api --host 0.0.0.0 --port 8000
