#!/usr/bin/env bash
set -euo pipefail

if [ "${SKIP_REPLIT_PRESTART:-0}" = "1" ]; then
  echo "[replit-prestart] skipping (SKIP_REPLIT_PRESTART=1)"
  exit 0
fi

echo "[replit-prestart] starting"

bash scripts/print_env_diagnostics.sh replit_prestart

# Helper function to detect placeholder URLs
is_placeholder() {
  # Heuristic match for tutorial/placeholder URLs
  echo "$1" | grep -Eq 'placeholder|example\.com|@host:|://host[:/]|/database$' 
}

DB_SOURCE="none"
if [ -n "${SQL_DATABASE_URL:-}" ]; then
  DB_SOURCE="SQL_DATABASE_URL"
elif [ -n "${DATABASE_URL:-}" ]; then
  DB_SOURCE="DATABASE_URL"
fi
echo "[replit-prestart] Database source: $DB_SOURCE"

if [ -n "${SQL_DATABASE_URL:-}" ]; then
  if is_placeholder "${SQL_DATABASE_URL}"; then
    echo "[replit-prestart] SQL_DATABASE_URL is marked as placeholder (set-in-secrets)"                                                                         
  else
    echo "[replit-prestart] SQL_DATABASE_URL contains an explicit value"        
  fi
fi

# Prefer Replit-provided DATABASE_URL over SQL_DATABASE_URL from .env

if [[ -n "${DATABASE_URL:-}" ]]; then
  if [[ -z "${SQL_DATABASE_URL:-}" || $(is_placeholder "${SQL_DATABASE_URL:-}") ]]; then
    export SQL_DATABASE_URL="${DATABASE_URL}"
    echo "[replit-prestart] SQL_DATABASE_URL set from DATABASE_URL"
  fi
fi

# Basic driver check (uv sync should have installed psycopg2-binary)
if ! uv run python -c 'import psycopg2' >/dev/null 2>&1; then
  echo "[replit-prestart] installing psycopg2-binary..."
  uv pip install --quiet 'psycopg2-binary>=2.9'
fi

# Run alembic migrations via uv environment
echo "[replit-prestart] applying migrations"
uv run alembic upgrade head

# Validate runtime env (non-fatal)
uv run python scripts/validate_broker_env.py || true

echo "[replit-prestart] done"
exit 0
