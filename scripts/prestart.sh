#!/usr/bin/env sh
set -e

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
