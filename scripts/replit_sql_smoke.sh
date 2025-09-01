#!/usr/bin/env bash
set -euo pipefail

# Replit SQL smoke test SOL
# Requirements:
# - API running on Replit with SQL configured
# - Secrets: BROKER_BASE_URL, BROKER_ADMIN_TOKEN, SQL_DATABASE_URL
# Optional: VENICE_PARENT_KEY if you plan to create a real tenant via admin API.

BASE_URL=${BROKER_BASE_URL:-"http://127.0.0.1:8000"}
ADMIN=${BROKER_ADMIN_TOKEN:-""}
TENANT_ID=${1:-"t-sql-smoke"}
RPS=${RPS:-15}
DURATION=${DURATION:-30}
CONCURRENCY=${CONCURRENCY:-20}

echo "=== SQL SMOKE: base_url=$BASE_URL tenant=$TENANT_ID rps=$RPS duration=$DURATIONs concurrency=$CONCURRENCY ==="

if [[ -z "$ADMIN" ]]; then
  echo "ERROR: BROKER_ADMIN_TOKEN is required" >&2
  exit 1
fi

echo "[1/5] Create tenant (idempotent)"
curl -sS -X POST "${BASE_URL%/}/v1/tenants" \
  -H "Authorization: Bearer $ADMIN" \
  -H 'Content-Type: application/json' \
  -d '{"tenant_id":"'"$TENANT_ID"'","label":"SQL Smoke","quota":0}' | tee /tmp/tenant.json >/dev/null || true

echo "[2/5] Generate chat traffic via admin act-as"
python scripts/limit_probe.py \
  --base-url "$BASE_URL" \
  --tenant-id "$TENANT_ID" \
  --admin-token "$ADMIN" \
  --rps "$RPS" --duration "$DURATION" --concurrency "$CONCURRENCY" | tee /tmp/probe.out

echo "[3/5] Compact KV -> SQL counters"
python apps/cli/main.py data:compact-counters --force || true

echo "[4/5] Fetch debug counters (first 10 rows)"
curl -sS "${BASE_URL%/}/v1/debug/counters?tenant_id=$TENANT_ID&limit=10" -H "Authorization: Bearer $ADMIN" | tee /tmp/counters.json

echo "[5/5] Summary"
echo "Probe summary (last line):"; tail -n 1 /tmp/probe.out
echo "Counters sample:"; cat /tmp/counters.json

echo "=== SQL SMOKE: DONE ==="

