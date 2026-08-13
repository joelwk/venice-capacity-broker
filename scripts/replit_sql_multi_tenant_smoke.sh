#!/usr/bin/env bash
set -euo pipefail

# Replit multi-tenant SQL smoke test.
# Uses shared library for common functions.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/sql_smoke_lib.sh"

BASE_URL=${BROKER_BASE_URL:-"http://127.0.0.1:8000"}
ADMIN=${BROKER_ADMIN_TOKEN:-""}
RPS=${RPS:-10}
DURATION=${DURATION:-20}
CONCURRENCY=${CONCURRENCY:-20}
PROBE_TIMEOUT=${PROBE_TIMEOUT:-30}
TENANT_CREATE_TIMEOUT=${TENANT_CREATE_TIMEOUT:-45}
RESULTS_DIR=${SQL_SMOKE_RESULTS_DIR:-"/tmp/replit-sql-smoke"}
CHAT_DIR=${SQL_SMOKE_CHAT_DIR:-"$RESULTS_DIR/chat-samples"}
SQL_LIGHT_RPS=${SQL_SMOKE_SQL_LIGHT_RPS:-5}
SQL_LIGHT_DURATION=${SQL_SMOKE_SQL_LIGHT_DURATION:-10}
SQL_LIGHT_CONCURRENCY=${SQL_SMOKE_SQL_LIGHT_CONCURRENCY:-10}
SUCCESS_RATIO_THRESHOLD=${SQL_SMOKE_SUCCESS_RATIO_THRESHOLD:-0.4}
SOAK_MODE=${SQL_SMOKE_SOAK_MODE:-0}

# Tenant specs and limits are now defined in sql_smoke_lib.sh
# Can be overridden here if needed before calling run_sql_smoke_for_tenants

if [[ -z "$ADMIN" ]]; then
  echo "ERROR: BROKER_ADMIN_TOKEN is required" >&2
  exit 1
fi

# Verify Venice key is available
echo "[init] Verifying Venice API key availability..."
if [[ -z "${VENICE_PARENT_KEY:-}" && -z "${VENICE_API_KEY:-}" ]]; then
  echo "ERROR: VENICE_PARENT_KEY or VENICE_API_KEY must be set" >&2
  echo "  Check .env or Replit Secrets configuration" >&2
  exit 1
fi
echo "[init] Venice key found"

mkdir -p "$RESULTS_DIR"
mkdir -p "$CHAT_DIR"

echo "=== REPLIT SQL MULTI-TENANT SMOKE: base_url=$BASE_URL rps=$RPS duration=${DURATION}s concurrency=$CONCURRENCY ==="

export BROKER_BASE_URL="$BASE_URL"

# Run smoke tests for all tenants using shared runner
# Replit always requires low-tier RL validation (require_low_tier_rl=1)
if ! run_sql_smoke_for_tenants \
  "replit-sql" \
  "$SUCCESS_RATIO_THRESHOLD" \
  "$SOAK_MODE" \
  1 \
  "$RPS" \
  "$DURATION" \
  "$CONCURRENCY" \
  "$SQL_LIGHT_RPS" \
  "$SQL_LIGHT_DURATION" \
  "$SQL_LIGHT_CONCURRENCY" \
  "$PROBE_TIMEOUT" \
  ""; then
  echo "[smoke] ERROR: smoke test failed" >&2
  if ! is_truthy "$SOAK_MODE"; then
    exit 1
  fi
fi

# End-of-run operations
MAX_EXPIRY_MINUTES=$(compute_max_expiry_minutes)
compact_counters_once "replit-sql" ""
fetch_counters_for_tenants "replit-sql"
wait_for_expiries "$MAX_EXPIRY_MINUTES"
list_final_tenants "replit-sql" ""

echo "=== REPLIT SQL MULTI-TENANT SMOKE: DONE ==="
