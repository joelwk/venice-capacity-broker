#!/usr/bin/env sh
set -e

# Run migrations and environment validation
scripts/prestart.sh

# Exec the passed command (default set in Dockerfile)
exec "$@"

