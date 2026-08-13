#!/usr/bin/env sh
set -e

# Diagnostics
bash scripts/print_env_diagnostics.sh docker_entrypoint

# Run migrations and environment validation
scripts/prestart.sh

# Exec the passed command (default set in Dockerfile)
exec "$@"
