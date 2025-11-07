#!/usr/bin/env bash
set -euo pipefail

mode="${1:-dry}"

# Ensure prestart tasks on Replit (migrations + env validation)
if [ -f "scripts/replit_prestart.sh" ]; then
	bash scripts/replit_prestart.sh
fi

# Derive APP_ENV=production for Replit Deployments
export APP_ENV="production"

# Decide stack process set
if [ "$mode" = "live" ]; then
	# Start broker API and orchestrator live
	exec bash -lc "uvicorn apps.broker_api.app:app --host 0.0.0.0 --port \\${AUTOSTART_BROKER_PORT:-8000} & python apps/cli/main.py run:loop --sleep 15 --max-cycles 0 --enable-live & wait"
else
	# Dry-run (no on-chain actions)
	exec bash -lc "uvicorn apps.broker_api.app:app --host 0.0.0.0 --port \\${AUTOSTART_BROKER_PORT:-8000} & python apps/cli/main.py run:loop --sleep 15 --max-cycles 0 --dry-run & wait"
fi
