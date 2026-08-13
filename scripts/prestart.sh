#!/usr/bin/env sh
set -e

# Detect execution context
if [ -f "/.dockerenv" ]; then
	EXEC_CONTEXT="docker"
elif [ -n "${REPLIT_DB_URL:-}" ] || [ -n "${REPLIT_ENVIRONMENT:-}" ] || [ -n "${REPLIT_ENV:-}" ]; then
	EXEC_CONTEXT="replit"
else
	EXEC_CONTEXT="local"
fi

bash scripts/print_env_diagnostics.sh prestart "context=${EXEC_CONTEXT}"
if [ -n "${SQL_DATABASE_URL:-}" ]; then
	echo "[prestart] SQL_DATABASE_URL is configured"
else
	echo "[prestart] SQL_DATABASE_URL is missing"
fi

# Warn on local runs without proper Postgres configuration
if [ "$EXEC_CONTEXT" = "local" ]; then
	if [ -z "${SQL_DATABASE_URL:-}" ]; then
		if [ "${ALLOW_SQLITE_FALLBACK:-0}" != "1" ]; then
			echo "[prestart] WARNING: Local run detected without SQL_DATABASE_URL" >&2
			echo "[prestart] Configure: SQL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres" >&2
			echo "[prestart] Or set ALLOW_SQLITE_FALLBACK=1 for dev/test only" >&2
		fi
	elif echo "${SQL_DATABASE_URL}" | grep -qi "sqlite"; then
		if [ "${ALLOW_SQLITE_FALLBACK:-0}" != "1" ]; then
			echo "[prestart] WARNING: Local run with SQLite database detected" >&2
			echo "[prestart] Host-side runs require Postgres for broker persistence" >&2
			echo "[prestart] Configure: SQL_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres" >&2
		fi
	fi
fi

# Run Alembic migrations to head
if command -v alembic >/dev/null 2>&1; then
	alembic upgrade head
else
	echo "[prestart] alembic not found in PATH" >&2
	exit 1
fi

# Validate environment and DB connectivity (exits non-zero on critical/high issues)
python scripts/validate_broker_env.py || exit $?

# Success
exit 0
