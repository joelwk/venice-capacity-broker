#!/usr/bin/env sh
set -e

# Replit production DBs are Postgres 16 on Neon (see docs). Ensure migrations applied.
# https://docs.replit.com/cloud-services/storage-and-databases/sql-database.md
# https://docs.replit.com/cloud-services/storage-and-databases/production-databases.md

if command -v alembic >/dev/null 2>&1; then
	alembic upgrade head
else
	echo "[replit-prestart] alembic not found in PATH" >&2
	exit 1
fi

python scripts/validate_broker_env.py || exit $?

exit 0
