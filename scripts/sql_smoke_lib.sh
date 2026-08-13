#!/usr/bin/env bash
# Shared library for SQL multi-tenant smoke tests
# Source this file from docker or replit smoke scripts

# Default tenant configuration (can be overridden by caller scripts)
if [[ -z "${TENANT_SPECS+x}" ]]; then
  # Tenants:
  # - t-sql-low: low DIEM quota, explicit low-tier rate limits.
  # - t-sql-high: former mid tier (same quota/limits), renamed for clarity.
  # - t-sql-uncensored: tiny random DIEM quota, uses the venice-uncensored model.
  TENANT_SPECS=(
    "t-sql-low,SQL Smoke Low,5,5"
    "t-sql-high,SQL Smoke High,25,8"
    # Default DIEM for the uncensored tenant is intentionally clamped to a single unit with a short TTL.
    "t-sql-uncensored,SQL Smoke Uncensored,random,3"
  )
fi

if [[ -z "${TENANT_LIMIT_MAX+x}" ]]; then
  declare -A TENANT_LIMIT_MAX=(
    ["t-sql-low"]=40
  )
fi

if [[ -z "${TENANT_LIMIT_WINDOW+x}" ]]; then
  declare -A TENANT_LIMIT_WINDOW=(
    ["t-sql-low"]=60
  )
fi

# Utility functions
is_truthy() {
  case "${1,,}" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

expiry_iso() {
  local minutes=$1
  EXP_MINUTES="$minutes" python - <<'PY'
from datetime import datetime, timedelta, timezone
import os

delta = timedelta(minutes=int(os.environ["EXP_MINUTES"]))
print((datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

# Tenant management
create_tenant_with_retry() {
  local tenant_id=$1
  local tenant_label=$2
  local tenant_quota=$3
  local expires_at=$4
  local output_file=$5
  local cmd_prefix=${6:-""}  # Optional: "docker compose exec -T broker " or ""
  local attempt=0
  local tmp_err=$(mktemp)
  while (( attempt < 2 )); do
    attempt=$((attempt + 1))
    if ${cmd_prefix} python scripts/create_test_tenant.py \
      --tenant-id "$tenant_id" \
      --label "$tenant_label" \
      --quota "$tenant_quota" \
      --expires-at "$expires_at" \
      --timeout "${TENANT_CREATE_TIMEOUT:-45}" \
      --probe-chat 2>"$tmp_err" | tee "$output_file"; then
      rm -f "$tmp_err"
      return 0
    fi
    if (( attempt < 2 )); then
      echo "[create] WARN: tenant create failed for $tenant_id (attempt $attempt), retrying in 5s..." >&2
      if [[ -s "$tmp_err" ]]; then
        echo "[create] stderr: $(head -n 3 "$tmp_err")" >&2
      fi
      sleep 5
    fi
  done
  echo "[create] ERROR: tenant create failed for $tenant_id after $attempt attempts" >&2
  if [[ -s "$tmp_err" ]]; then
    echo "[create] Last error output:" >&2
    cat "$tmp_err" >&2
  fi
  rm -f "$tmp_err"
  return 1
}

check_probe_chat_failure() {
  local tenant_id=$1
  local chat_sample_file=$2
  if grep -q '"chat_status".*502' "$chat_sample_file" 2>/dev/null; then
    echo "[create] WARN: tenant $tenant_id created but probe-chat failed with 502" >&2
    echo "[create] WARN: This usually means Venice subkey creation succeeded but key is invalid" >&2
    echo "[create] WARN: Continuing with rate-limit probes (they use admin impersonation)" >&2
  fi
}

# Broker limits configuration
set_broker_limits() {
  local tenant_id=$1
  local window_seconds=$2
  local max_requests=$3
  local payload
  read -r -d '' payload <<EOF || true
{"windowSeconds":${window_seconds:-60},"maxRequests":${max_requests:-0},"label":"sql-smoke"}
EOF
  curl -sS -X POST "${BASE_URL%/}/v1/tenants/${tenant_id}/broker-limits" \
    -H "Authorization: Bearer ${ADMIN}" \
    -H "Content-Type: application/json" \
    -d "$payload" >/dev/null
}

# Usage tracking
fetch_usage() {
  local tenant_id=$1
  local suffix=$2
  local prefix=${3:-""}  # e.g., "docker-sql" or "replit-sql"
  local mode=${SQL_SMOKE_USAGE_MODE:-broker}

  if [[ "${mode}" == "venice-direct" || "${mode}" == "venice-admin" ]]; then
    local direct_path
    if direct_path=$(fetch_usage_via_venice "$tenant_id" "$suffix" "$prefix" "$mode"); then
      if [[ -n "$direct_path" ]]; then
        echo "$direct_path"
        return 0
      fi
    fi
    echo "[usage] WARN: ${mode} usage mode failed, falling back to broker endpoint" >&2
  fi

  local out="${RESULTS_DIR}/${prefix}-usage-${tenant_id}-${suffix}.json"
  curl -sS "${BASE_URL%/}/v1/tenants/${tenant_id}/usage" \
    -H "Authorization: Bearer ${ADMIN}" > "$out"
  echo "$out"
}

fetch_usage_via_venice() {
  local tenant_id=$1
  local suffix=$2
  local prefix=${3:-""}  # e.g., "docker-sql" or "replit-sql"
  local mode=${4:-"venice-direct"}
  local out="${RESULTS_DIR}/${prefix}-usage-${tenant_id}-${suffix}.json"
  local tmp=$(mktemp "/tmp/venice-usage-${tenant_id}-${suffix}.XXXXXX")
  local status=1

  if [[ "$prefix" == docker-* ]]; then
    local compose_cmd="${COMPOSE_CMD:-docker compose}"
    ${compose_cmd} exec -T broker python - "$tenant_id" "$mode" <<'PY' > "$tmp"
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from apps.broker_api.tenant_store_sql import SQLTenantStore
except Exception:  # noqa: BLE001
    SQLTenantStore = None  # type: ignore[assignment]


def landlord(limit: int) -> int:
    return limit if limit > 0 else 200


def build_attempt(label: str, api_key: Optional[str], params: Optional[Dict[str, Any]] = None, extra_headers: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
    if not api_key:
        return None
    attempt: Dict[str, Any] = {"label": label, "api_key": api_key}
    if params:
        attempt["params"] = params
    if extra_headers:
        attempt["extra_headers"] = extra_headers
    return attempt


def perform(url: str, attempt: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    headers: Dict[str, str] = {"Authorization": f"Bearer {attempt['api_key']}"}
    extra_headers = attempt.get("extra_headers") or {}
    if extra_headers:
        headers.update(extra_headers)
    params = attempt.get("params") or {}
    timeout_s = float(os.getenv("SQL_SMOKE_USAGE_TIMEOUT", "30"))
    try:
        resp = httpx.get(url, headers=headers, params=params, timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        return None, {"attempt": attempt["label"], "error": str(exc)}
    meta: Dict[str, Any] = {"attempt": attempt["label"], "status": resp.status_code}
    if resp.status_code == 200:
        try:
            return resp.json(), meta
        except Exception as exc:  # noqa: BLE001
            return {"usage": None, "error": f"invalid_json: {exc}", "raw": resp.text[:200]}, meta
    meta["body"] = resp.text[:200]
    return None, meta


def extract_diem_usage(usage_payload: Any, limits_payload: Any) -> float:
    """
    Extract per-tenant DIEM usage from Venice usage payloads.

    First attempts to sum explicit usage fields (dailyAverageDiem, consumptionDaily, etc.).
    If no explicit usage fields are found (total remains 0.0), falls back to deriving
    consumption from DIEM balance changes.

    For balance-only deployments: returns the negative of the DIEM balance so that
    usage_delta() computes pre_balance - post_balance (i.e., DIEM consumed) correctly.
    """

    def _collect_from_entry(entry: Any) -> float:
        if not isinstance(entry, dict):
            return 0.0
        accum = 0.0
        for key in (
            "dailyAverageDiem",
            "daily_average_diem",
            "avgDailyDiem",
            "consumptionDaily",
            "consumption",
        ):
            if key in entry and entry[key] is not None:
                try:
                    accum += float(entry[key])
                except Exception:
                    pass
        return accum

    total = 0.0

    if isinstance(usage_payload, dict):
        # Top-level fields
        total += _collect_from_entry(usage_payload)

        data = usage_payload.get("data")
        if isinstance(data, list):
            for entry in data:
                total += _collect_from_entry(entry)

        aggregate = usage_payload.get("aggregate")
        if isinstance(aggregate, dict):
            total += _collect_from_entry(aggregate)

    # If no explicit usage fields found, fall back to balance-based derivation
    if total == 0.0 and isinstance(limits_payload, dict):
        data = limits_payload.get("data")
        if isinstance(data, dict):
            balances = data.get("balances")
            if isinstance(balances, dict) and "DIEM" in balances:
                try:
                    balance = float(balances["DIEM"])
                    # Return negative balance so usage_delta computes consumption correctly:
                    # delta = max(0, post - pre) = max(0, -post_balance - (-pre_balance))
                    #      = max(0, pre_balance - post_balance) = DIEM consumed
                    return -balance
                except Exception:
                    pass

    return total


def resolve_path(base: str, path: str) -> str:
    scoped = path or ""
    if scoped.startswith("http://") or scoped.startswith("https://"):
        return scoped
    return f"{base}{scoped}"


def main() -> None:
    tenant_id = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "venice-direct"
    base = os.getenv("VENICE_API_BASE_URL", "").rstrip("/")
    usage_path = os.getenv("VENICE_USAGE_PATH", "/api_keys/rate_limits/log")
    rate_limits_path = os.getenv("VENICE_RATE_LIMITS_PATH", "/api_keys/rate_limits")
    quota_path = os.getenv("VENICE_QUOTA_PATH", "/api_keys/rate_limits")

    url = resolve_path(base, usage_path)
    limits_url = resolve_path(base, rate_limits_path)
    quota_url = resolve_path(base, quota_path)

    try:
        limit = landlord(int(os.getenv("SQL_SMOKE_USAGE_LOG_LIMIT", "200")))
    except ValueError:
        limit = 200

    store_tenant = None
    if SQLTenantStore is not None:
        try:
            store_tenant = SQLTenantStore().get(tenant_id)
        except Exception:  # noqa: BLE001
            store_tenant = None

    parent_key = os.getenv("VENICE_PARENT_KEY") or os.getenv("VENICE_API_KEY")
    sub_key = getattr(store_tenant, "subkey", None) if store_tenant else None
    key_id = getattr(store_tenant, "key_id", None) if store_tenant else None

    attempts: List[Dict[str, Any]] = []
    if sub_key:
        maybe = build_attempt("tenant_subkey", sub_key, {"limit": limit})
        if maybe:
            attempts.append(maybe)
    maybe = build_attempt("parent_default", parent_key, {"limit": limit})
    if maybe:
        attempts.append(maybe)
    if parent_key and key_id:
        maybe = build_attempt(
            "parent_apiKeyId",
            parent_key,
            {"limit": limit, "apiKeyId": key_id},
        )
        if maybe:
            attempts.append(maybe)
    if parent_key and sub_key:
        for label, params, headers in (
            ("parent_subKey_param", {"limit": limit, "subKey": sub_key}, None),
            ("parent_header_subKey", {"limit": limit}, {"X-Venice-Sub-Key": sub_key}),
            ("parent_apiKey_param", {"limit": limit, "apiKey": sub_key}, None),
        ):
            maybe = build_attempt(label, parent_key, params, headers)
            if maybe:
                attempts.append(maybe)

    if not attempts:
        payload = {
            "error": "usage_fetch_failed",
            "reason": "no_api_key_available",
            "tenant": tenant_id,
            "mode": mode,
        }
        print(json.dumps(payload))
        return

    usage_result: Optional[Any] = None
    usage_traces: List[Dict[str, Any]] = []

    for attempt in attempts:
        result, meta = perform(url, attempt)
        usage_traces.append(meta)
        if result is not None and usage_result is None:
            usage_result = result
            if mode != "venice-admin":
                break

    limits_attempts: List[Dict[str, Any]] = []
    limits_result: Optional[Any] = None

    if mode == "venice-admin":
        limit_attempt_specs: List[Dict[str, Any]] = []
        # Venice requires admin key (parent) for /api_keys/rate_limits endpoint
        # Pass subkey via X-Venice-Sub-Key header to get tenant-specific limits
        if parent_key and sub_key:
            # Try parent key with subkey header first (most accurate for tenant limits)
            maybe = build_attempt(
                "limits_parent_header_subKey",
                parent_key,
                None,
                {"X-Venice-Sub-Key": sub_key},
            )
            if maybe:
                limit_attempt_specs.append(maybe)
        if parent_key:
            limit_attempt_specs.append(build_attempt("limits_parent_default", parent_key) or {})
        if parent_key and key_id:
            limit_attempt_specs.append(
                build_attempt(
                    "limits_parent_apiKeyId",
                    parent_key,
                    {"apiKeyId": key_id},
                )
                or {}
            )
        limit_attempt_specs = [entry for entry in limit_attempt_specs if entry]
        for attempt in limit_attempt_specs:
            result, meta = perform(limits_url, attempt)
            limits_attempts.append(meta)
            if result is not None:
                limits_result = result
                break
        if limits_result is None and limit_attempt_specs:
            for attempt in limit_attempt_specs:
                result, meta = perform(quota_url, attempt)
                meta["fallback"] = True
                limits_attempts.append(meta)
                if result is not None:
                    limits_result = result
                    break

    payload: Dict[str, Any] = {
        "mode": mode,
        "tenant": tenant_id,
        "usage": usage_result or {},
        "logs": usage_result,
        "limits": limits_result,
        "_fetchMeta": {
            "usageAttempts": usage_traces,
            "limitsAttempts": limits_attempts,
        },
    }

    usage_value = extract_diem_usage(payload.get("usage"), payload.get("limits"))
    payload["_summary"] = {"diem": float(usage_value)}

    print(json.dumps(payload))


if __name__ == "__main__":
    main()
PY
    status=$?
  else
    cat <<'JSON' > "$tmp"
{"error":"venice_direct_requires_docker","detail":"venice-direct usage mode is only supported when prefix begins with 'docker-'"}
JSON
    status=1
  fi

  mv "$tmp" "$out"
  rm -f "$tmp"

  if (( status != 0 )); then
    echo ""
    return 1
  fi

  echo "$out"
}

usage_metric() {
  local file=$1
  python - "$file" <<'PY'
import json
import sys
from typing import Any, Optional


def to_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        for key in (
            "diem",
            "daily",
            "dailyAverageDiem",
            "daily_average_diem",
            "avgDailyDiem",
            "consumption",
            "consumptionDaily",
            "value",
            "used",
            "total",
        ):
            if key in value:
                nested = to_float(value[key])
                if nested is not None:
                    return nested
        for container_key in ("data", "items", "entries", "aggregate"):
            if container_key in value:
                nested = to_float(value[container_key])
                if nested is not None:
                    return nested
        totals = []
        for key, sub in value.items():
            if key in ("consumptionLimit", "consumption_limit"):
                continue
            nested = to_float(sub)
            if nested is not None:
                totals.append(nested)
        if totals:
            return sum(totals)
        return None
    if isinstance(value, list):
        total = 0.0
        found = False
        for entry in value:
            nested = to_float(entry)
            if nested is not None:
                total += nested
                found = True
        return total if found else None
    return None


def extract(data: Any) -> float:
    if not isinstance(data, dict):
        return 0.0
    summary = data.get("_summary")
    if summary is not None:
        maybe = to_float(summary)
        if maybe is not None:
            return maybe
    for key in ("usage", "limits", "logs"):
        maybe = to_float(data.get(key))
        if maybe is not None:
            return maybe
    return 0.0


with open(sys.argv[1], "r", encoding="utf-8") as handle:
    payload = json.load(handle)
print(extract(payload))
PY
}

usage_delta() {
  local before=$1
  local after=$2
  python - "$before" "$after" <<'PY'
import sys

pre = float(sys.argv[1])
post = float(sys.argv[2])
# Generic usage delta semantics:
#   - Metrics that grow with usage (e.g., cumulative consumption):
#       delta = max(0, post - pre)
#   - Metrics that are the negative of a balance (e.g., -DIEM_balance):
#       pre = -pre_balance, post = -post_balance
#       delta = max(0, post - pre)
#             = max(0, -post_balance - (-pre_balance))
#             = max(0, pre_balance - post_balance)  # DIEM consumed
# This keeps DIEM consumption non-negative and monotonic with actual spend.

delta = post - pre
if delta < 0:
    delta = 0.0

print(delta)
PY
}

float_ge() {
  python - "$1" "$2" <<'PY'
import sys

current = float(sys.argv[1] or 0)
target = float(sys.argv[2] or 0)
raise SystemExit(0 if current + 1e-6 >= target else 1)
PY
}

float_progressed() {
  python - "$1" "$2" <<'PY'
import sys

current = float(sys.argv[1] or 0)
previous = float(sys.argv[2] or 0)
raise SystemExit(0 if current > previous + 1e-6 else 1)
PY
}

usage_payload_status() {
  local file=$1
  python - "$file" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
except Exception as exc:
    print(f"invalid_json: {exc}")
    raise SystemExit(2)

if isinstance(data, dict):
    if "error" in data and not isinstance(data.get("usage"), (dict, list)):
        # propagate explicit error payloads
        message = data.get("detail") or data.get("error") or "usage error"
        print(message)
        raise SystemExit(1)
    if any(
        key in data
        for key in ("usage", "data", "limits", "payload")
    ):
        raise SystemExit(0)
elif isinstance(data, list):
    raise SystemExit(0)

message = None
if isinstance(data, dict):
    for key in ("detail", "error", "message"):
        value = data.get(key)
        if isinstance(value, (str, int, float)):
            message = str(value)
            break
    else:
        message = "usage data missing"
else:
    message = "unexpected usage payload"
print(message)
raise SystemExit(1)
PY
}

validate_usage_delta() {
  local delta=$1
  local tenant_id=$2
  local min_delta=${3:-0}
  python - "$delta" "$tenant_id" "$min_delta" <<'PY'
import sys

delta = float(sys.argv[1])
min_delta = float(sys.argv[3])
if delta + 1e-9 < min_delta:
    raise SystemExit(
        f"usage delta for {sys.argv[2]} below minimum {min_delta}: {delta}"
    )
print(delta)
PY
}

# Counter validation
validate_counters() {
  local file=$1
  python - "$file" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
if not isinstance(data, list):
    raise SystemExit("counters endpoint did not return a list")
if not data:
    raise SystemExit("counters endpoint returned no rows")
print(len(data))
PY
}

# Probe result analysis
read_summary_value() {
  local file=$1
  local key=$2
  python - "$file" "$key" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
value = data.get(sys.argv[2], 0)
print(value if value is not None else 0)
PY
}

assert_success_ratio() {
  local summary=$1
  local threshold=$2
  python - "$summary" "$threshold" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1]))
attempted = float(data.get("attempted") or 0)
ok = float(data.get("ok") or 0)
ratio = (ok / attempted) if attempted else 0.0
threshold = float(sys.argv[2])
if ratio + 1e-9 < threshold:
    raise SystemExit(f"success_ratio {ratio:.4f} < {threshold}")
print(f"{ratio:.4f}")
PY
}

write_status_histogram() {
  local summary=$1
  local dest=$2
  python - "$summary" "$dest" <<'PY'
import json, sys

data = json.load(open(sys.argv[1]))
hist = data.get("status_histogram", {})
with open(sys.argv[2], "w") as f:
    json.dump(hist, f, indent=2)
PY
}

# Probe execution
run_probe_phase() {
  local tenant_id=$1
  local suffix=$2
  local prefix=${3:-""}  # e.g., "docker-sql" or "replit-sql"
  shift 3
  local cmd_prefix=""
  if [[ "$prefix" == docker-* ]]; then
    cmd_prefix="${COMPOSE_CMD:-docker compose} exec -T broker "
  fi
  
  local log_file="${RESULTS_DIR}/${prefix}-probe-${tenant_id}-${suffix}.log"
  local summary_file="${RESULTS_DIR}/${prefix}-probe-summary-${tenant_id}-${suffix}.json"
  local histogram_file="${RESULTS_DIR}/${prefix}-status-histogram-${tenant_id}-${suffix}.json"
  local tmp=$(mktemp "/tmp/${prefix}-probe-${tenant_id}-${suffix}.XXXXXX")
  
  # Capture verbose probe output into a temp file and log only;
  # do not leak it to stdout so callers receive just the summary path.
  ${cmd_prefix} python scripts/limit_probe.py \
    --base-url "$BASE_URL" \
    --tenant-id "$tenant_id" \
    --admin-token "$ADMIN" \
    "$@" | tee "$tmp" >/dev/null
  
  cp "$tmp" "$log_file"
  local summary_line
  summary_line=$(tail -n 1 "$tmp" || true)
  if [[ "$summary_line" == \{* ]]; then
    printf "%s\n" "$summary_line" > "$summary_file"
    write_status_histogram "$summary_file" "$histogram_file"
  else
    echo "WARNING: probe output for ${tenant_id}-${suffix} missing JSON summary line" >&2
  fi
  rm -f "$tmp"
  echo "$summary_file"
}

# Tenant quota assignment helpers
assign_uncensored_quota_if_needed() {
  local tenant_id=$1
  local -n quota_ref=$2  # Use nameref to modify the quota variable
  if [[ "$tenant_id" == "t-sql-uncensored" ]]; then
    local min_quota=${SQL_SMOKE_UNCENSORED_QUOTA_MIN:-1}
    local max_quota=${SQL_SMOKE_UNCENSORED_QUOTA_MAX:-1}
    if (( max_quota < min_quota )); then
      max_quota=$min_quota
    fi
    local range=$((max_quota - min_quota + 1))
    quota_ref=$((RANDOM % range + min_quota))
    echo "[create] t-sql-uncensored using random DIEM quota=${quota_ref} (range ${min_quota}-${max_quota})"
  fi
}

# Low-tier rate limit validation
validate_low_tier_rate_limits() {
  local tenant_id=$1
  local summary_file=$2
  local require_low_tier_rl=$3
  local soak_mode=${4:-0}
  
  if [[ "$tenant_id" != "t-sql-low" ]]; then
    return 0
  fi
  
  local rl_count
  rl_count=$(read_summary_value "$summary_file" "rate_limited")
  if [[ "$rl_count" == "0" ]]; then
    if (( require_low_tier_rl )); then
      echo "[validate] ERROR: t-sql-low should have rate_limited > 0, got $rl_count" >&2
      if ! is_truthy "$soak_mode"; then
        return 1
      fi
    else
      echo "[validate] WARN: t-sql-low produced no 429s (REQUIRE_LOW_TIER_RL=0)" >&2
    fi
  else
    echo "[validate] t-sql-low: rate_limited=$rl_count (expected > 0) ✓"
  fi
  return 0
}

validate_diem_quota_exhaustion() {
  local tenant_id=$1
  local summary_file=$2
  local require_diem_rl=$3
  local soak_mode=${4:-0}

  if [[ "$tenant_id" != "t-sql-uncensored" ]]; then
    return 0
  fi
  if ! is_truthy "$require_diem_rl"; then
    return 0
  fi

  local rl_count
  rl_count=$(read_summary_value "$summary_file" "rate_limited")
  if [[ "$rl_count" == "0" ]]; then
    echo "[validate] ERROR: t-sql-uncensored should have rate_limited > 0 when DIEM quota enforced" >&2
    if ! is_truthy "$soak_mode"; then
      return 1
    fi
  else
    echo "[validate] t-sql-uncensored: rate_limited=$rl_count (expected > 0) ✓"
  fi
  return 0
}

# Validation and assertions
run_validations() {
  local tenant_id=$1
  local summary_file=$2
  local counters_file=$3
  local pre_usage_file=$4
  local post_usage_file=$5
  local threshold=${6:-0.4}
  local soak_mode=${7:-0}
  
  # Validate success ratio unless in soak mode
  if ! is_truthy "$soak_mode"; then
    echo "[validate] ${tenant_id}: checking success ratio >= ${threshold}"
    if ! assert_success_ratio "$summary_file" "$threshold"; then
      echo "[validate] ERROR: ${tenant_id} failed success ratio check" >&2
      return 1
    fi
  fi
  
  local enforce_usage_delta_flag=0
  if is_truthy "${SQL_SMOKE_ENFORCE_USAGE_DELTA:-0}"; then
    enforce_usage_delta_flag=1
  fi
  local min_usage_delta="${SQL_SMOKE_MIN_USAGE_DELTA:-0}"
  local tenant_env_key
  tenant_env_key=$(printf "%s" "$tenant_id" | tr '[:lower:]' '[:upper:]')
  tenant_env_key=${tenant_env_key//-/_}
  local tenant_min_var="SQL_SMOKE_MIN_USAGE_DELTA_${tenant_env_key}"
  if [[ -n "${!tenant_min_var:-}" ]]; then
    min_usage_delta="${!tenant_min_var}"
  fi
  local tenant_enforce_var="SQL_SMOKE_ENFORCE_USAGE_DELTA_${tenant_env_key}"
  if [[ -n "${!tenant_enforce_var:-}" ]]; then
    if is_truthy "${!tenant_enforce_var}"; then
      enforce_usage_delta_flag=1
    else
      enforce_usage_delta_flag=0
    fi
  fi
  
  if [[ -f "$pre_usage_file" && -f "$post_usage_file" ]]; then
    local usage_inputs_ok=1
    local pre_usage_error=""
    local post_usage_error=""
    if ! pre_usage_error=$(usage_payload_status "$pre_usage_file" 2>&1); then
      usage_inputs_ok=0
      if [[ -n "$pre_usage_error" ]]; then
        echo "[validate] WARN: ${tenant_id} pre-usage payload error: ${pre_usage_error}" >&2
      else
        echo "[validate] WARN: ${tenant_id} pre-usage payload missing usage data" >&2
      fi
    fi
    if ! post_usage_error=$(usage_payload_status "$post_usage_file" 2>&1); then
      usage_inputs_ok=0
      if [[ -n "$post_usage_error" ]]; then
        echo "[validate] WARN: ${tenant_id} post-usage payload error: ${post_usage_error}" >&2
      else
        echo "[validate] WARN: ${tenant_id} post-usage payload missing usage data" >&2
      fi
    fi

    if (( usage_inputs_ok )); then
      echo "[validate] ${tenant_id}: checking usage delta (min=${min_usage_delta})"
      local pre_metric post_metric delta delta_output
      pre_metric=$(usage_metric "$pre_usage_file")
      post_metric=$(usage_metric "$post_usage_file")
      delta=$(usage_delta "$pre_metric" "$post_metric")
      local skip_usage_enforcement=0
      if (( enforce_usage_delta_flag )) && [[ "$delta" == "0.0" ]]; then
        local pre_summary_diem post_summary_diem
        pre_summary_diem=$(python - "$pre_usage_file" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    print("0.0")
    raise SystemExit(0)
summary = data.get("_summary") or {}
value = summary.get("diem", 0.0)
try:
    print(float(value))
except Exception:
    print("0.0")
PY
)
        post_summary_diem=$(python - "$post_usage_file" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], "r", encoding="utf-8"))
except Exception:
    print("0.0")
    raise SystemExit(0)
summary = data.get("_summary") or {}
value = summary.get("diem", 0.0)
try:
    print(float(value))
except Exception:
    print("0.0")
PY
)
        if [[ "$pre_summary_diem" == "0.0" && "$post_summary_diem" == "0.0" ]]; then
          echo "[validate] WARN: ${tenant_id} DIEM usage tracking unsupported (pre/post summaries are 0.0); skipping enforcement" >&2
          skip_usage_enforcement=1
        fi
      fi

      if (( skip_usage_enforcement )); then
        echo "[validate] ${tenant_id}: usage delta = ${delta} (not enforced)"
      elif ! delta_output=$(validate_usage_delta "$delta" "$tenant_id" "$min_usage_delta" 2>&1); then
        echo "[validate] WARN: ${tenant_id} usage delta validation failed: ${delta_output}" >&2
        if (( enforce_usage_delta_flag )); then
          echo "[validate] ERROR: ${tenant_id} usage delta enforcement failed" >&2
          return 1
        fi
      else
        if (( enforce_usage_delta_flag )); then
          echo "[validate] ${tenant_id}: usage delta = ${delta_output} (pre=${pre_metric}, post=${post_metric})"
        else
          echo "[validate] ${tenant_id}: usage delta = ${delta_output}"
        fi
      fi
    else
      if (( enforce_usage_delta_flag )); then
        echo "[validate] ERROR: ${tenant_id} usage delta enforcement failed due to usage payload errors" >&2
        return 1
      fi
    fi
  else
    echo "[validate] WARN: ${tenant_id} usage snapshots missing; skipping usage delta check" >&2
    if (( enforce_usage_delta_flag )); then
      echo "[validate] ERROR: ${tenant_id} usage delta enforcement requires usage snapshots" >&2
      return 1
    fi
  fi
  
  # Validate counters if file exists
  if [[ -f "$counters_file" ]]; then
    echo "[validate] ${tenant_id}: checking counters"
    if ! validate_counters "$counters_file"; then
      echo "[validate] WARN: ${tenant_id} counters validation failed" >&2
    fi
  fi
  
  return 0
}

# Main smoke test runner for all tenants
run_sql_smoke_for_tenants() {
  local prefix=$1  # e.g., "docker-sql" or "replit-sql"
  local success_ratio_threshold=${2:-0.4}
  local soak_mode=${3:-0}
  local require_low_tier_rl=${4:-1}
  local rps=${5:-10}
  local duration=${6:-20}
  local concurrency=${7:-20}
  local sql_light_rps=${8:-5}
  local sql_light_duration=${9:-10}
  local sql_light_concurrency=${10:-10}
  local probe_timeout=${11:-30}
  local cmd_prefix=${12:-""}  # Optional: "docker compose exec -T broker " or ""
  
  local max_expiry_minutes=0
  local require_diem_rl=${SQL_SMOKE_REQUIRE_DIEM_429:-0}
  
  for spec in "${TENANT_SPECS[@]}"; do
    IFS=',' read -r TENANT_ID TENANT_LABEL TENANT_QUOTA TENANT_TTL <<< "$spec"
    if [[ -z "$TENANT_ID" || -z "$TENANT_QUOTA" || -z "$TENANT_TTL" ]]; then
      echo "WARN: skipping invalid tenant spec: $spec" >&2
      continue
    fi

    # Special handling for the uncensored tenant: assign a tiny random DIEM quota.
    assign_uncensored_quota_if_needed "$TENANT_ID" "TENANT_QUOTA"

    if (( TENANT_TTL > max_expiry_minutes )); then
      max_expiry_minutes=$TENANT_TTL
    fi

    local expires_at
    expires_at=$(expiry_iso "$TENANT_TTL")

    echo "[create] $TENANT_ID (quota=$TENANT_QUOTA expire=$expires_at)"
    if ! create_tenant_with_retry \
      "$TENANT_ID" \
      "$TENANT_LABEL" \
      "$TENANT_QUOTA" \
      "$expires_at" \
      "$CHAT_DIR/${TENANT_ID}-chat-sample.json" \
      "$cmd_prefix"; then
      echo "[create] ERROR: skipping probes for $TENANT_ID because tenant creation failed" >&2
      continue
    fi
    
    check_probe_chat_failure "$TENANT_ID" "$CHAT_DIR/${TENANT_ID}-chat-sample.json"

    if [[ -n "${TENANT_LIMIT_MAX[$TENANT_ID]:-}" ]]; then
      echo "[limits] ${TENANT_ID} window=${TENANT_LIMIT_WINDOW[$TENANT_ID]:-60} maxRequests=${TENANT_LIMIT_MAX[$TENANT_ID]}"
      set_broker_limits \
        "$TENANT_ID" \
        "${TENANT_LIMIT_WINDOW[$TENANT_ID]:-60}" \
        "${TENANT_LIMIT_MAX[$TENANT_ID]}"
    fi

    local pre_usage post_usage
    pre_usage=$(fetch_usage "$TENANT_ID" "pre" "$prefix")
    echo "[usage] ${TENANT_ID} pre-probe: $pre_usage"

    echo "[probe] ${TENANT_ID} (hello workload)"
    local summary_hello
    if [[ "$TENANT_ID" == "t-sql-uncensored" ]]; then
      summary_hello=$(run_probe_phase "$TENANT_ID" "hello" "$prefix" \
        --rps "$rps" \
        --duration "$duration" \
        --concurrency "$concurrency" \
        --timeout "$probe_timeout" \
        --workload hello \
        --model "venice-uncensored" \
        --think false)
    else
      summary_hello=$(run_probe_phase "$TENANT_ID" "hello" "$prefix" \
        --rps "$rps" \
        --duration "$duration" \
        --concurrency "$concurrency" \
        --timeout "$probe_timeout" \
        --workload hello \
        --think false)
    fi

    echo "[probe] ${TENANT_ID} (sql_light workload)"
    local summary_sql
    if [[ "$TENANT_ID" == "t-sql-uncensored" ]]; then
      summary_sql=$(run_probe_phase "$TENANT_ID" "sql-light" "$prefix" \
        --rps "$sql_light_rps" \
        --duration "$sql_light_duration" \
        --concurrency "$sql_light_concurrency" \
        --timeout "$probe_timeout" \
        --workload sql_light \
        --model "venice-uncensored" \
        --think false)
    else
      summary_sql=$(run_probe_phase "$TENANT_ID" "sql-light" "$prefix" \
        --rps "$sql_light_rps" \
        --duration "$sql_light_duration" \
        --concurrency "$sql_light_concurrency" \
        --timeout "$probe_timeout" \
        --workload sql_light \
        --think false)
    fi

    if [[ "$TENANT_ID" == "t-sql-uncensored" ]]; then
      local pre_metric post_metric drained target_quota
      pre_metric=$(usage_metric "$pre_usage")
      target_quota="$TENANT_QUOTA"
      local drain_pass=0
      post_usage=$(fetch_usage "$TENANT_ID" "drain-pass${drain_pass}" "$prefix")
      echo "[usage] ${TENANT_ID} drain-pass${drain_pass}: $post_usage"
      post_metric=$(usage_metric "$post_usage")
      drained=$(usage_delta "$pre_metric" "$post_metric")
      echo "[drain] ${TENANT_ID}: DIEM consumed=${drained}/${target_quota}"
      local max_passes=${SQL_SMOKE_UNCENSORED_MAX_PASSES:-5}
      while true; do
        if float_ge "$drained" "$target_quota"; then
          break
        fi
        if (( drain_pass >= max_passes )); then
          echo "[drain] ${TENANT_ID}: reached max passes (${max_passes}) without exhausting quota" >&2
          break
        fi
        drain_pass=$((drain_pass + 1))
        echo "[drain] ${TENANT_ID}: venice-uncensored additional pass ${drain_pass}"
        summary_sql=$(run_probe_phase "$TENANT_ID" "sql-light" "$prefix" \
          --rps "$sql_light_rps" \
          --duration "$sql_light_duration" \
          --concurrency "$sql_light_concurrency" \
          --timeout "$probe_timeout" \
          --workload sql_light \
          --model "venice-uncensored" \
          --think false)
        post_usage=$(fetch_usage "$TENANT_ID" "drain-pass${drain_pass}" "$prefix")
        echo "[usage] ${TENANT_ID} drain-pass${drain_pass}: $post_usage"
        post_metric=$(usage_metric "$post_usage")
        local previous_drained="$drained"
        drained=$(usage_delta "$pre_metric" "$post_metric")
        echo "[drain] ${TENANT_ID}: DIEM consumed=${drained}/${target_quota}"
        if ! float_progressed "$drained" "$previous_drained"; then
          echo "[drain] ${TENANT_ID}: no additional DIEM usage detected; stopping" >&2
          break
        fi
      done
    else
      post_usage=$(fetch_usage "$TENANT_ID" "post" "$prefix")
      echo "[usage] ${TENANT_ID} post-probe: $post_usage"
    fi

    local counters_file="${RESULTS_DIR}/${prefix}-counters-${TENANT_ID}.json"
    
    if ! run_validations \
      "$TENANT_ID" \
      "$summary_hello" \
      "$counters_file" \
      "$pre_usage" \
      "$post_usage" \
      "$success_ratio_threshold" \
      "$soak_mode"; then
      echo "[validate] ERROR: ${TENANT_ID} failed validation" >&2
      if ! is_truthy "$soak_mode"; then
        return 1
      fi
    fi

    if ! validate_low_tier_rate_limits "$TENANT_ID" "$summary_hello" "$require_low_tier_rl" "$soak_mode"; then
      if ! is_truthy "$soak_mode"; then
        return 1
      fi
    fi
    if ! validate_diem_quota_exhaustion "$TENANT_ID" "$summary_sql" "$require_diem_rl" "$soak_mode"; then
      if ! is_truthy "$soak_mode"; then
        return 1
      fi
    fi
  done
  
  return 0
}

# Helper to compute max expiry minutes from tenant specs
compute_max_expiry_minutes() {
  local max_expiry=0
  for spec in "${TENANT_SPECS[@]}"; do
    IFS=',' read -r _ _ _ tenant_ttl <<< "$spec"
    if [[ -n "$tenant_ttl" ]] && (( tenant_ttl > max_expiry )); then
      max_expiry=$tenant_ttl
    fi
  done
  echo "$max_expiry"
}

# End-of-run helpers
compact_counters_once() {
  local prefix=$1
  local cmd_prefix=${2:-""}
  
  echo "[counter compact] running once across tenants"
  if [[ -n "$cmd_prefix" ]]; then
    ${cmd_prefix} python apps/cli/main.py data:compact-counters --force || true
  else
    python apps/cli/main.py data:compact-counters --force || true
  fi
}

fetch_counters_for_tenants() {
  local prefix=$1
  
  for spec in "${TENANT_SPECS[@]}"; do
    IFS=',' read -r TENANT_ID _ _ _ <<< "$spec"
    if [[ -z "$TENANT_ID" ]]; then
      continue
    fi
    local counters_file="${RESULTS_DIR}/${prefix}-counters-${TENANT_ID}.json"
    curl -sS "${BASE_URL%/}/v1/debug/counters?tenant_id=$TENANT_ID&limit=10" \
      -H "Authorization: Bearer $ADMIN" | tee "$counters_file"
  done
}

wait_for_expiries() {
  local max_expiry_minutes=$1
  local wait_seconds=$((max_expiry_minutes * 60 + 30))
  echo "[cleanup] waiting $wait_seconds seconds for expiries"
  sleep "$wait_seconds"
}

list_final_tenants() {
  local prefix=$1
  local cmd_prefix=${2:-""}
  local tenant_list_file="${RESULTS_DIR}/${prefix}-tenants-after.json"
  
  if [[ -n "$cmd_prefix" ]]; then
    ${cmd_prefix} python apps/cli/main.py broker:tenants:list | tee "$tenant_list_file"
  else
    python apps/cli/main.py broker:tenants:list | tee "$tenant_list_file"
  fi
}

