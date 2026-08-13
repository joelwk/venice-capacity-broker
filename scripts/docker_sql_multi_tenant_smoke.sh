#!/usr/bin/env bash
set -euo pipefail

# Docker-based multi-tenant SQL smoke test.
# Uses shared library for common functions.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/sql_smoke_lib.sh"

RPS_FROM_ENV=0
if [[ -n "${RPS+x}" ]]; then
  RPS_FROM_ENV=1
fi
DURATION_FROM_ENV=0
if [[ -n "${DURATION+x}" ]]; then
  DURATION_FROM_ENV=1
fi
CONCURRENCY_FROM_ENV=0
if [[ -n "${CONCURRENCY+x}" ]]; then
  CONCURRENCY_FROM_ENV=1
fi
SQL_LIGHT_RPS_FROM_ENV=0
if [[ -n "${SQL_SMOKE_SQL_LIGHT_RPS+x}" ]]; then
  SQL_LIGHT_RPS_FROM_ENV=1
fi
SQL_LIGHT_DURATION_FROM_ENV=0
if [[ -n "${SQL_SMOKE_SQL_LIGHT_DURATION+x}" ]]; then
  SQL_LIGHT_DURATION_FROM_ENV=1
fi
SQL_LIGHT_CONCURRENCY_FROM_ENV=0
if [[ -n "${SQL_SMOKE_SQL_LIGHT_CONCURRENCY+x}" ]]; then
  SQL_LIGHT_CONCURRENCY_FROM_ENV=1
fi
REQUIRE_LOW_TIER_RL_FROM_ENV=0
if [[ -n "${SQL_SMOKE_REQUIRE_LOW_TIER_RL+x}" ]]; then
  REQUIRE_LOW_TIER_RL_FROM_ENV=1
fi
USAGE_MODE_FROM_ENV=0
if [[ -n "${SQL_SMOKE_USAGE_MODE+x}" ]]; then
  USAGE_MODE_FROM_ENV=1
fi
ENFORCE_USAGE_DELTA_FROM_ENV=0
if [[ -n "${SQL_SMOKE_ENFORCE_USAGE_DELTA+x}" ]]; then
  ENFORCE_USAGE_DELTA_FROM_ENV=1
fi
MIN_USAGE_DELTA_FROM_ENV=0
if [[ -n "${SQL_SMOKE_MIN_USAGE_DELTA+x}" ]]; then
  MIN_USAGE_DELTA_FROM_ENV=1
fi
REQUIRE_DIEM_429_FROM_ENV=0
if [[ -n "${SQL_SMOKE_REQUIRE_DIEM_429+x}" ]]; then
  REQUIRE_DIEM_429_FROM_ENV=1
fi
UNCENSORED_MAX_PASSES_FROM_ENV=0
if [[ -n "${SQL_SMOKE_UNCENSORED_MAX_PASSES+x}" ]]; then
  UNCENSORED_MAX_PASSES_FROM_ENV=1
fi

BASE_URL=${BROKER_BASE_URL:-"http://127.0.0.1:8000"}
RPS=${RPS:-10}
DURATION=${DURATION:-20}
CONCURRENCY=${CONCURRENCY:-20}
PROBE_TIMEOUT=${PROBE_TIMEOUT:-30}
TENANT_CREATE_TIMEOUT=${TENANT_CREATE_TIMEOUT:-45}
RESULTS_DIR=${SQL_SMOKE_RESULTS_DIR:-"logs/sql-smoke"}
CHAT_DIR=${SQL_SMOKE_CHAT_DIR:-"$RESULTS_DIR/chat-samples"}
COMPOSE_CMD=${DOCKER_COMPOSE:-"docker compose"}
SQL_LIGHT_RPS=${SQL_SMOKE_SQL_LIGHT_RPS:-5}
SQL_LIGHT_DURATION=${SQL_SMOKE_SQL_LIGHT_DURATION:-10}
SQL_LIGHT_CONCURRENCY=${SQL_SMOKE_SQL_LIGHT_CONCURRENCY:-10}
SUCCESS_RATIO_THRESHOLD=${SQL_SMOKE_SUCCESS_RATIO_THRESHOLD:-0.4}
SOAK_MODE=${SQL_SMOKE_SOAK_MODE:-0}
REQUIRE_LOW_TIER_RL=${SQL_SMOKE_REQUIRE_LOW_TIER_RL:-1}
SQL_SMOKE_UNCENSORED_QUOTA_MIN=${SQL_SMOKE_UNCENSORED_QUOTA_MIN:-1}
SQL_SMOKE_UNCENSORED_QUOTA_MAX=${SQL_SMOKE_UNCENSORED_QUOTA_MAX:-1}
SQL_SMOKE_UNCENSORED_MAX_PASSES=${SQL_SMOKE_UNCENSORED_MAX_PASSES:-1}
SQL_SMOKE_USAGE_MODE=${SQL_SMOKE_USAGE_MODE:-broker}
SQL_SMOKE_ENFORCE_USAGE_DELTA=${SQL_SMOKE_ENFORCE_USAGE_DELTA:-0}
SQL_SMOKE_MIN_USAGE_DELTA=${SQL_SMOKE_MIN_USAGE_DELTA:-0}
SQL_SMOKE_REQUIRE_DIEM_429=${SQL_SMOKE_REQUIRE_DIEM_429:-0}

SQL_SMOKE_PROFILE_NAME=${SQL_SMOKE_PROFILE:-""}
if [[ -n "$SQL_SMOKE_PROFILE_NAME" ]]; then
  profile_known=1
  profile_rps=""
  profile_duration=""
  profile_concurrency=""
  profile_sql_light_rps=""
  profile_sql_light_duration=""
  profile_sql_light_concurrency=""
  profile_usage_mode=""
  profile_enforce_usage_delta=""
  profile_min_usage_delta=""
  profile_uncensored_min_usage_delta=""
  profile_require_low_tier_rl=""
  profile_uncensored_max_passes=""
  profile_require_diem_429=""
  case "$SQL_SMOKE_PROFILE_NAME" in
    local-dev)
      profile_rps=6
      profile_duration=20
      profile_concurrency=10
      profile_sql_light_rps=3
      profile_sql_light_duration=12
      profile_sql_light_concurrency=6
      if (( REQUIRE_LOW_TIER_RL_FROM_ENV == 0 )); then
        REQUIRE_LOW_TIER_RL=0
        echo "[profile] REQUIRE_LOW_TIER_RL disabled for local-dev"
      fi
      ;;
    local-dev-diem)
      profile_rps=3
      profile_duration=18
      profile_concurrency=6
      profile_sql_light_rps=4
      profile_sql_light_duration=30
      profile_sql_light_concurrency=6
      profile_usage_mode="venice-admin"
      profile_enforce_usage_delta=1
      profile_min_usage_delta=0
      profile_uncensored_min_usage_delta=0.75
      profile_require_low_tier_rl=0
      profile_uncensored_max_passes=4
      profile_require_diem_429=1
      ;;
    ci)
      profile_rps=6
      profile_duration=20
      profile_concurrency=12
      profile_sql_light_rps=3
      profile_sql_light_duration=10
      profile_sql_light_concurrency=6
      ;;
    staging)
      profile_rps=10
      profile_duration=30
      profile_concurrency=20
      profile_sql_light_rps=5
      profile_sql_light_duration=12
      profile_sql_light_concurrency=10
      ;;
    *)
      profile_known=0
      echo "[profile] WARN: unknown SQL_SMOKE_PROFILE=${SQL_SMOKE_PROFILE_NAME}; skipping profile" >&2
      ;;
  esac

  if (( profile_known )); then
    echo "[profile] SQL_SMOKE_PROFILE=${SQL_SMOKE_PROFILE_NAME}"
    if [[ -n "$profile_rps" ]]; then
      if (( RPS_FROM_ENV == 0 )); then
        RPS=$profile_rps
        echo "[profile] RPS set to ${RPS}"
      else
        echo "[profile] RPS preserved from env (${RPS})"
      fi
    fi
    if [[ -n "$profile_duration" ]]; then
      if (( DURATION_FROM_ENV == 0 )); then
        DURATION=$profile_duration
        echo "[profile] DURATION set to ${DURATION}"
      else
        echo "[profile] DURATION preserved from env (${DURATION})"
      fi
    fi
    if [[ -n "$profile_concurrency" ]]; then
      if (( CONCURRENCY_FROM_ENV == 0 )); then
        CONCURRENCY=$profile_concurrency
        echo "[profile] CONCURRENCY set to ${CONCURRENCY}"
      else
        echo "[profile] CONCURRENCY preserved from env (${CONCURRENCY})"
      fi
    fi
    if [[ -n "$profile_sql_light_rps" ]]; then
      if (( SQL_LIGHT_RPS_FROM_ENV == 0 )); then
        SQL_LIGHT_RPS=$profile_sql_light_rps
        echo "[profile] SQL_LIGHT_RPS set to ${SQL_LIGHT_RPS}"
      else
        echo "[profile] SQL_LIGHT_RPS preserved from env (${SQL_LIGHT_RPS})"
      fi
    fi
    if [[ -n "$profile_sql_light_duration" ]]; then
      if (( SQL_LIGHT_DURATION_FROM_ENV == 0 )); then
        SQL_LIGHT_DURATION=$profile_sql_light_duration
        echo "[profile] SQL_LIGHT_DURATION set to ${SQL_LIGHT_DURATION}"
      else
        echo "[profile] SQL_LIGHT_DURATION preserved from env (${SQL_LIGHT_DURATION})"
      fi
    fi
    if [[ -n "$profile_sql_light_concurrency" ]]; then
      if (( SQL_LIGHT_CONCURRENCY_FROM_ENV == 0 )); then
        SQL_LIGHT_CONCURRENCY=$profile_sql_light_concurrency
        echo "[profile] SQL_LIGHT_CONCURRENCY set to ${SQL_LIGHT_CONCURRENCY}"
      else
        echo "[profile] SQL_LIGHT_CONCURRENCY preserved from env (${SQL_LIGHT_CONCURRENCY})"
      fi
    fi
    if [[ -n "$profile_usage_mode" ]]; then
      if (( USAGE_MODE_FROM_ENV == 0 )); then
        SQL_SMOKE_USAGE_MODE=$profile_usage_mode
        echo "[profile] SQL_SMOKE_USAGE_MODE set to ${SQL_SMOKE_USAGE_MODE}"
      else
        echo "[profile] SQL_SMOKE_USAGE_MODE preserved from env (${SQL_SMOKE_USAGE_MODE})"
      fi
    fi
    if [[ -n "$profile_enforce_usage_delta" ]]; then
      if (( ENFORCE_USAGE_DELTA_FROM_ENV == 0 )); then
        SQL_SMOKE_ENFORCE_USAGE_DELTA=$profile_enforce_usage_delta
        echo "[profile] SQL_SMOKE_ENFORCE_USAGE_DELTA set to ${SQL_SMOKE_ENFORCE_USAGE_DELTA}"
      else
        echo "[profile] SQL_SMOKE_ENFORCE_USAGE_DELTA preserved from env (${SQL_SMOKE_ENFORCE_USAGE_DELTA})"
      fi
    fi
    if [[ -n "$profile_min_usage_delta" ]]; then
      if (( MIN_USAGE_DELTA_FROM_ENV == 0 )); then
        SQL_SMOKE_MIN_USAGE_DELTA=$profile_min_usage_delta
        echo "[profile] SQL_SMOKE_MIN_USAGE_DELTA set to ${SQL_SMOKE_MIN_USAGE_DELTA}"
      else
        echo "[profile] SQL_SMOKE_MIN_USAGE_DELTA preserved from env (${SQL_SMOKE_MIN_USAGE_DELTA})"
      fi
    fi
    if [[ -n "$profile_uncensored_min_usage_delta" ]]; then
      tenant_key="T_SQL_UNCENSORED"
      var_name="SQL_SMOKE_MIN_USAGE_DELTA_${tenant_key}"
      if [[ -z "${!var_name+x}" ]]; then
        printf -v "$var_name" '%s' "$profile_uncensored_min_usage_delta"
        echo "[profile] ${var_name} set to ${!var_name}"
      else
        echo "[profile] ${var_name} preserved from env (${!var_name})"
      fi
    fi
    if [[ -n "$profile_require_low_tier_rl" ]]; then
      if (( REQUIRE_LOW_TIER_RL_FROM_ENV == 0 )); then
        REQUIRE_LOW_TIER_RL=$profile_require_low_tier_rl
        echo "[profile] REQUIRE_LOW_TIER_RL set to ${REQUIRE_LOW_TIER_RL}"
      else
        echo "[profile] REQUIRE_LOW_TIER_RL preserved from env (${REQUIRE_LOW_TIER_RL})"
      fi
    fi
    if [[ -n "$profile_uncensored_max_passes" ]]; then
      if (( UNCENSORED_MAX_PASSES_FROM_ENV == 0 )); then
        SQL_SMOKE_UNCENSORED_MAX_PASSES=$profile_uncensored_max_passes
        echo "[profile] SQL_SMOKE_UNCENSORED_MAX_PASSES set to ${SQL_SMOKE_UNCENSORED_MAX_PASSES}"
      else
        echo "[profile] SQL_SMOKE_UNCENSORED_MAX_PASSES preserved from env (${SQL_SMOKE_UNCENSORED_MAX_PASSES})"
      fi
    fi
    if [[ -n "$profile_require_diem_429" ]]; then
      if (( REQUIRE_DIEM_429_FROM_ENV == 0 )); then
        SQL_SMOKE_REQUIRE_DIEM_429=$profile_require_diem_429
        echo "[profile] SQL_SMOKE_REQUIRE_DIEM_429 set to ${SQL_SMOKE_REQUIRE_DIEM_429}"
      else
        echo "[profile] SQL_SMOKE_REQUIRE_DIEM_429 preserved from env (${SQL_SMOKE_REQUIRE_DIEM_429})"
      fi
    fi
  fi
fi

# Tenant specs and limits are now defined in sql_smoke_lib.sh
# Can be overridden here if needed before calling run_sql_smoke_for_tenants

mkdir -p "$RESULTS_DIR"
mkdir -p "$CHAT_DIR"

if ! ${COMPOSE_CMD} ps broker >/dev/null 2>&1; then
  echo "ERROR: broker service not found or docker compose not initialized" >&2
  exit 1
fi

# Discover broker admin token (prefer host env, fall back to broker container).
ADMIN=${BROKER_ADMIN_TOKEN:-""}
if [[ -z "$ADMIN" ]]; then
  echo "[multi-tenant-sql] BROKER_ADMIN_TOKEN not set in host env, reading from broker container env"
  set +e
  ADMIN=$(${COMPOSE_CMD} exec -T broker sh -lc 'printf "%s" "${BROKER_ADMIN_TOKEN:-}"' 2>/dev/null)
  status=$?
  set -e
  if [[ $status -ne 0 || -z "$ADMIN" ]]; then
    echo "ERROR: BROKER_ADMIN_TOKEN is required (set in host env or inside broker container)" >&2
    exit 1
  fi
fi

# Verify Venice key is available in broker container
echo "[init] Verifying Venice API key availability..."
if ! ${COMPOSE_CMD} exec -T broker sh -c 'test -n "$VENICE_PARENT_KEY" || test -n "$VENICE_API_KEY"' 2>/dev/null; then
  echo "ERROR: broker container must have VENICE_PARENT_KEY or VENICE_API_KEY set" >&2
  echo "  Check .env.docker or docker/.env.local configuration" >&2
  exit 1
fi
echo "[init] Venice key found in broker container"

echo "=== DOCKER SQL MULTI-TENANT SMOKE: base_url=$BASE_URL rps=$RPS duration=${DURATION}s concurrency=$CONCURRENCY ==="

# Run smoke tests for all tenants using shared runner
if ! run_sql_smoke_for_tenants \
  "docker-sql" \
  "$SUCCESS_RATIO_THRESHOLD" \
  "$SOAK_MODE" \
  "$REQUIRE_LOW_TIER_RL" \
  "$RPS" \
  "$DURATION" \
  "$CONCURRENCY" \
  "$SQL_LIGHT_RPS" \
  "$SQL_LIGHT_DURATION" \
  "$SQL_LIGHT_CONCURRENCY" \
  "$PROBE_TIMEOUT" \
  "${COMPOSE_CMD} exec -T broker "; then
  echo "[smoke] ERROR: smoke test failed" >&2
  if ! is_truthy "$SOAK_MODE"; then
    exit 1
  fi
fi

# End-of-run operations
MAX_EXPIRY_MINUTES=$(compute_max_expiry_minutes)
compact_counters_once "docker-sql" "${COMPOSE_CMD} exec -T broker "
fetch_counters_for_tenants "docker-sql"
wait_for_expiries "$MAX_EXPIRY_MINUTES"
list_final_tenants "docker-sql" "${COMPOSE_CMD} exec -T broker "

echo "=== DOCKER SQL MULTI-TENANT SMOKE: DONE ==="
