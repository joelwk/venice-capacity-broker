#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-dry}"

is_replit_context() {
	if [[ -n "${REPLIT_DEPLOYMENT:-}" || -n "${REPLIT_ENVIRONMENT:-}" || -n "${REPLIT_ENV:-}" || -n "${REPL_ID:-}" || -n "${REPL_SLUG:-}" || -n "${REPLIT_DB_URL:-}" ]]; then
		return 0
	fi
	return 1
}

# ------------------------------------------------------------------------------
# Load .env file for Replit deployments
# Replit does not auto-load .env files, so we inject them here.
# Order: .env first (base config), then Replit Secrets override (already in env)
# ------------------------------------------------------------------------------
load_env_file() {
	local env_file="$1"
	if [[ ! -f "$env_file" ]]; then
		return 0
	fi
	echo "[replit_run] Loading environment from $env_file"
	local line key value
	while IFS= read -r line || [[ -n "$line" ]]; do
		# Skip empty lines and comments
		[[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
		# Skip lines that don't look like KEY=VALUE
		[[ ! "$line" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] && continue
		# Extract key and value
		key="${line%%=*}"
		value="${line#*=}"
		# Only set if not already defined (Replit Secrets take precedence)
		if [[ -z "${!key:-}" ]]; then
			export "$key"="$value"
		fi
	done < "$env_file"
}

# Load .env file (base configuration copied from env.md template)
# Replit Secrets are already in the environment and take precedence
if [[ -f ".env" ]]; then
	load_env_file ".env"
else
	echo "[replit_run] No .env file found. Create one from the env.md template:"
	echo "[replit_run]   cp env.md .env"
	echo "[replit_run] Continuing with Replit Secrets and [env] section only."
fi

export PATH="$PWD/.local/bin:$HOME/.local/bin:$PATH"

# Set core defaults before diagnostics so they are not reported as missing.
export RUN_STACK_MODE="$MODE"
export APP_ENV="${APP_ENV:-production}"

# Reserved VM deploys: skip slow steps unless explicitly overridden.
if is_replit_context; then
	export SKIP_UV_SYNC="${SKIP_UV_SYNC:-1}"
	export SKIP_REPLIT_PRESTART="${SKIP_REPLIT_PRESTART:-1}"
	export SKIP_ENV_VALIDATION="${SKIP_ENV_VALIDATION:-1}"
	export SKIP_STARTUP_TASKS="${SKIP_STARTUP_TASKS:-1}"
	# Skip slow diagnostics in deployments by default - they block server startup
	export SKIP_ENV_DIAGNOSTICS="${SKIP_ENV_DIAGNOSTICS:-1}"
	# Defer heavy broker deps to keep startup under Replit health check windows
	export BROKER_FAST_STARTUP="${BROKER_FAST_STARTUP:-1}"
	# Extend price cache TTL to reduce cold-start latency on user requests
	# Default 60s is too short when warmup takes 10+ seconds
	export BROKER_PRICES_TTL_SECONDS="${BROKER_PRICES_TTL_SECONDS:-300}"
	# Use faster RPC timeout for price fetches
	export RPC_REQUEST_TIMEOUT_SECONDS="${RPC_REQUEST_TIMEOUT_SECONDS:-15}"
fi

# Run diagnostics unless skipped (slow in Replit deployments)
if [ "${SKIP_ENV_DIAGNOSTICS:-0}" != "1" ]; then
	bash scripts/print_env_diagnostics.sh "replit_run mode=${MODE}"
else
	echo "[replit_run] SKIP_ENV_DIAGNOSTICS=1; skipping slow diagnostics"
	echo "[replit_run] Set SKIP_ENV_DIAGNOSTICS=0 in Secrets to enable"
fi
echo "[replit_run] Run stack mode: ${MODE}"
if [[ "$MODE" != "dry" && "$MODE" != "live" ]]; then
        echo "Usage: $0 [dry|live]" >&2
        exit 1
fi

# Shared runtime defaults; allow external overrides when present.
export CORS_ENABLED="${CORS_ENABLED:-true}"
export CORS_ALLOW_ORIGINS="${CORS_ALLOW_ORIGINS:-https://capacity-broker.replit.app}"
export QUOTES_ENABLED="${QUOTES_ENABLED:-true}"
export PURCHASES_ENABLED="${PURCHASES_ENABLED:-true}"
export AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY="${AUTOSTART_TOKEN_WATCHER_ALLOW_NO_KEY:-1}"
# Buyer flow defaults to DIEM-aware pricing. Static pricing requires PRICE_UNIT_* to be set,
# so default to market pricing on Replit unless explicitly overridden.
export PRICE_ENGINE="${PRICE_ENGINE:-market}"

# Logging configuration for production
export LOG_LEVEL="${LOG_LEVEL:-INFO}"
export LOG_DIR="${LOG_DIR:-logs}"
export LOG_BASENAME="${LOG_BASENAME:-runtime.log}"
export LOG_CAPTURE_CONSOLE="${LOG_CAPTURE_CONSOLE:-1}"
export LOG_FORMAT="${LOG_FORMAT:-}"

# Ensure log directory exists before any Python process starts
# This is critical for Replit deployments where the directory may not exist
if [ -n "$LOG_DIR" ] && [ "$LOG_DIR" != "stdout" ]; then
	mkdir -p "$LOG_DIR" 2>/dev/null || echo "[replit_run] Warning: could not create log directory $LOG_DIR" >&2
fi

# Print logging configuration for diagnostics
echo "[replit_run] Logging: level=${LOG_LEVEL} format=${LOG_FORMAT:-text} dir=${LOG_DIR} file=${LOG_BASENAME}"
echo "[replit_run] Console capture: ${LOG_CAPTURE_CONSOLE}"

# Prefer a resilient Base RPC on Replit to avoid 429s
export BASE_RPC_URL="${BASE_RPC_URL:-https://base.drpc.org}"
export RPC_REQUEST_TIMEOUT_SECONDS="${RPC_REQUEST_TIMEOUT_SECONDS:-20}"

# DIEM trading configuration - enable direct routes and bridge fallback
export DIEM_BUY_DIRECT_ONLY="${DIEM_BUY_DIRECT_ONLY:-1}"
export DIEM_BRIDGE_LIVE_FALLBACK_ENABLE="${DIEM_BRIDGE_LIVE_FALLBACK_ENABLE:-1}"
export DIEM_BRIDGE_LIVE_FALLBACK_MAX_USD="${DIEM_BRIDGE_LIVE_FALLBACK_MAX_USD:-10.0}"
export DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS="${DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS:-100.0}"

REPLIT_HTTP_PORT="${REPLIT_SERVER_PORT:-${PORT:-8000}}"
REPLIT_HTTP_HOST="${REPLIT_SERVER_HOST:-0.0.0.0}"
export AUTOSTART_BROKER_PORT="${AUTOSTART_BROKER_PORT:-$REPLIT_HTTP_PORT}"
export AUTOSTART_BROKER_HOST="${AUTOSTART_BROKER_HOST:-$REPLIT_HTTP_HOST}"

if [ "${SKIP_STARTUP_TASKS:-0}" = "1" ]; then
	echo "[replit_run] SKIP_STARTUP_TASKS=1 set; starting broker API only" >&2
	if [[ "$MODE" == "live" ]]; then
		HAS_KV_STORE=0
		if [ -n "${REDIS_URL:-}" ]; then
			HAS_KV_STORE=1
		elif [ -n "${REPLIT_DB_URL:-}" ]; then
			HAS_KV_STORE=1
		elif [ -f "/tmp/replitdb" ]; then
			export REPLIT_DB_URL="$(cat /tmp/replitdb)"
			HAS_KV_STORE=1
			echo "[replit_run] Loaded REPLIT_DB_URL from /tmp/replitdb (deployment mode)" >&2
		fi

		if [ "$HAS_KV_STORE" -eq 0 ]; then
			echo "[replit_run] ERROR: Live mode requires durable KV store (REDIS_URL or REPLIT_DB_URL)" >&2
			exit 1
		fi
	fi

	if [ "${SPAWN_AGENT_STACK:-0}" = "1" ]; then
		echo "[replit_run] Spawning background agent stack (broker disabled)..." >&2
		# Include logging env vars so background stack writes to same log files
		LOG_FORMAT_ESCAPED=$(printf '%q' "${LOG_FORMAT:-}")
		AGENT_ENV="AUTOSTART_BROKER_API=0 AUTOSTART_ORCHESTRATOR=${AUTOSTART_ORCHESTRATOR:-1} AUTOSTART_STAKEMASTER=${AUTOSTART_STAKEMASTER:-0} AUTOSTART_TOKEN_WATCHER=${AUTOSTART_TOKEN_WATCHER:-1} RUN_STACK_MODE=${MODE} SKIP_UV_SYNC=1 SKIP_REPLIT_PRESTART=1 SKIP_ENV_VALIDATION=1 LOG_LEVEL=${LOG_LEVEL} LOG_DIR=${LOG_DIR} LOG_BASENAME=${LOG_BASENAME} LOG_CAPTURE_CONSOLE=${LOG_CAPTURE_CONSOLE} LOG_FORMAT=${LOG_FORMAT_ESCAPED}"
		if command -v uv >/dev/null 2>&1; then
			bash -lc "$AGENT_ENV uv run python scripts/start_stack.py" &
		else
			bash -lc "$AGENT_ENV python scripts/start_stack.py" &
		fi
	fi

	if command -v uv >/dev/null 2>&1; then
		exec uv run uvicorn apps.broker_api.app:app --host "$AUTOSTART_BROKER_HOST" --port "$AUTOSTART_BROKER_PORT"
	else
		exec uvicorn apps.broker_api.app:app --host "$AUTOSTART_BROKER_HOST" --port "$AUTOSTART_BROKER_PORT"
	fi
fi

if [[ "$MODE" == "live" ]]; then
        export AUTOSTART_ORCHESTRATOR_LIVE=1
        export AUTOSTART_STAKEMASTER_LIVE=1
        
        # Disable in-memory KV fallback for live mode - require durable store
        export ALLOW_INMEMORY_KV_FALLBACK=0
        
        # Validate that a durable KV store is configured
        # Note: REPLIT_DB_URL is not available in deployments; check /tmp/replitdb file instead
        HAS_KV_STORE=0
        if [ -n "${REDIS_URL:-}" ]; then
                HAS_KV_STORE=1
        elif [ -n "${REPLIT_DB_URL:-}" ]; then
                HAS_KV_STORE=1
        elif [ -f "/tmp/replitdb" ]; then
                # In deployments, REPLIT_DB_URL is stored in /tmp/replitdb
                export REPLIT_DB_URL="$(cat /tmp/replitdb)"
                HAS_KV_STORE=1
                echo "[replit_run] Loaded REPLIT_DB_URL from /tmp/replitdb (deployment mode)" >&2
        fi
        
        if [ "$HAS_KV_STORE" -eq 0 ]; then
                echo "[replit_run] ERROR: Live mode requires durable KV store (REDIS_URL or REPLIT_DB_URL)" >&2
                echo "[replit_run] Configure REPLIT_DB_URL in Secrets or provide REDIS_URL" >&2
                echo "[replit_run] In deployments, REPLIT_DB_URL should be in /tmp/replitdb" >&2
                exit 1
        fi
else
        export AUTOSTART_ORCHESTRATOR_LIVE=0
        export AUTOSTART_STAKEMASTER_LIVE=0
fi

exec bash scripts/run_stack_entry.sh
