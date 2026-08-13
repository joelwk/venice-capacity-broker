# SQL Multi-Tenant Smoke Test Suite

## Overview

The SQL smoke test suite validates multi-tenant broker behavior under load, focusing on:
- Rate limiting and quota enforcement
- Per-tenant usage tracking
- Counter persistence and compaction
- SQL workload handling
- Venice API integration

## Architecture

### Unified Design (New)

```
sql_smoke_lib.sh (shared library)
    ├── docker_sql_multi_tenant_smoke.sh (Docker Compose)
    └── replit_sql_multi_tenant_smoke.sh (Replit/bare metal)
```

**Key Improvement**: All common logic (tenant creation, usage tracking, validation, probe execution) is now in `sql_smoke_lib.sh`, eliminating ~300 lines of duplication.

### Files

- **`sql_smoke_lib.sh`**: Shared library with all common functions
- **`docker_sql_multi_tenant_smoke.sh`**: Docker Compose wrapper
- **`replit_sql_multi_tenant_smoke.sh`**: Replit/bare metal wrapper
- **`limit_probe.py`**: HTTP load generator with status classification

## What the Test Does

### 1. Tenant Creation (3 tiers)
- **t-sql-low**: quota=5, maxRequests=40/60s (forces 429s)
- **t-sql-high**: quota=25, default limits
- **t-sql-uncensored**: quota=1-3 (random DIEM), default limits, `model=venice-uncensored`, drains until quota is exhausted

### 2. Probe Phases (per tenant)
- **Hello workload**: Simple greeting (baseline latency)
- **SQL-light workload**: SQL query prompt (validates toolchain path)

### 3. Validations
- ✓ Success ratio ≥ threshold (default 40%)
- ✓ Usage delta logging (enforce via `SQL_SMOKE_ENFORCE_USAGE_DELTA`)
- ✓ DIEM tenant usage delta + optional 429 quota exhaustion checks (`SQL_SMOKE_REQUIRE_DIEM_429`)
- ✓ Rate-limited count > 0 for t-sql-low
- ✓ Counters persisted to SQL after compaction
- ✓ Status histogram captured (429/4xx/5xx/timeout/connect_error)

Usage delta checks always log the Venetian snapshots; set `SQL_SMOKE_ENFORCE_USAGE_DELTA=1` (with `SQL_SMOKE_MIN_USAGE_DELTA` or per-tenant overrides) to fail fast when deltas are missing or too small.

### 4. Artifacts
```
logs/sql-smoke/
├── chat-samples/
│   ├── t-sql-low-chat-sample.json
│   ├── t-sql-high-chat-sample.json
│   └── t-sql-uncensored-chat-sample.json
├── docker-sql-probe-{tenant}-{phase}.log
├── docker-sql-probe-summary-{tenant}-{phase}.json
├── docker-sql-status-histogram-{tenant}-{phase}.json
├── docker-sql-usage-{tenant}-{pre|post|drain-passN}.json
├── docker-sql-counters-{tenant}.json
└── docker-sql-tenants-after.json
```
When `SQL_SMOKE_USAGE_MODE=venice-admin`, the usage snapshots also include `_summary.diem` with the inferred DIEM consumption for quick delta checks.

## Usage

### Docker Compose

```bash
# Default run
bash scripts/docker_sql_multi_tenant_smoke.sh

# Custom parameters
RPS=20 DURATION=30 SUCCESS_RATIO_THRESHOLD=0.5 \
  bash scripts/docker_sql_multi_tenant_smoke.sh

# Soak mode (warnings only, no early exit)
SQL_SMOKE_SOAK_MODE=1 bash scripts/docker_sql_multi_tenant_smoke.sh

# Apply a preset for local laptops
SQL_SMOKE_PROFILE=local-dev bash scripts/docker_sql_multi_tenant_smoke.sh

# Focus on DIEM accounting & quota enforcement
SQL_SMOKE_PROFILE=local-dev-diem bash scripts/docker_sql_multi_tenant_smoke.sh

# Enforce Venice usage delta ≥ 1.0 units
SQL_SMOKE_ENFORCE_USAGE_DELTA=1 SQL_SMOKE_MIN_USAGE_DELTA=1.0 \
  bash scripts/docker_sql_multi_tenant_smoke.sh

# Pull usage snapshots via the broker container's Venice key
SQL_SMOKE_USAGE_MODE=venice-direct SQL_SMOKE_PROFILE=local-dev \
  bash scripts/docker_sql_multi_tenant_smoke.sh

# Force the venice-admin collector and expect DIEM 429s
SQL_SMOKE_PROFILE=local-dev-diem SQL_SMOKE_REQUIRE_DIEM_429=1 \
  bash scripts/docker_sql_multi_tenant_smoke.sh
```

### Replit

```bash
# Ensure env vars are set
export BROKER_ADMIN_TOKEN="..."
export VENICE_PARENT_KEY="..."

bash scripts/replit_sql_multi_tenant_smoke.sh
```

## Environment Variables

### Required
- `BROKER_ADMIN_TOKEN`: Admin token for tenant management
- `VENICE_PARENT_KEY` or `VENICE_API_KEY`: Venice API key

### Optional
- `BASE_URL` / `BROKER_BASE_URL`: Broker endpoint (default: http://127.0.0.1:8000)
- `RPS`: Requests per second (default: 10)
- `DURATION`: Probe duration in seconds (default: 20)
- `CONCURRENCY`: Max in-flight requests (default: 20)
- `SUCCESS_RATIO_THRESHOLD`: Min success ratio (default: 0.4)
- `SQL_SMOKE_SOAK_MODE`: 0=fail fast, 1=warnings only (default: 0)
- `SQL_SMOKE_PROFILE`: Preset parameters (`local-dev`, `local-dev-diem`, `ci`, `staging`); explicit env vars still win
- `SQL_SMOKE_ENFORCE_USAGE_DELTA`: Set to 1 to fail when Venice usage delta is missing or below minimum
- `SQL_SMOKE_ENFORCE_USAGE_DELTA_<TENANT>`: Per-tenant override (`TENANT` uppercased with `-` → `_`), e.g. `SQL_SMOKE_ENFORCE_USAGE_DELTA_T_SQL_UNCENSORED=1`
- `SQL_SMOKE_MIN_USAGE_DELTA`: Required Venice usage delta when enforcement is on (default: 0)
- `SQL_SMOKE_MIN_USAGE_DELTA_<TENANT>`: Per-tenant minimum delta, e.g. `SQL_SMOKE_MIN_USAGE_DELTA_T_SQL_UNCENSORED=0.75`
- `SQL_SMOKE_USAGE_MODE`: `broker` (default); `venice-direct` pulls via the broker container's Venice key; `venice-admin` collects usage + limits with the parent key
- `SQL_SMOKE_USAGE_LOG_LIMIT`: Max Venice log entries to request when `venice-direct` is enabled (default: 200)
- `SQL_SMOKE_USAGE_TIMEOUT`: HTTP timeout in seconds for `venice-direct` requests (default: 30)
- `SQL_SMOKE_REQUIRE_LOW_TIER_RL`: Require t-sql-low to emit 429s (default: 1; auto-disabled by `local-dev` profile)
- `SQL_SMOKE_REQUIRE_DIEM_429`: Require t-sql-uncensored to emit 429s once DIEM quota is exhausted (default: 0; auto-enabled by `local-dev-diem`)
- `SQL_SMOKE_SQL_LIGHT_RPS`: SQL workload RPS (default: 5)
- `SQL_SMOKE_SQL_LIGHT_DURATION`: SQL workload duration (default: 10)
- `SQL_SMOKE_SQL_LIGHT_CONCURRENCY`: SQL workload concurrency (default: 10)
- `SQL_SMOKE_UNCENSORED_QUOTA_MIN`: Minimum DIEM units for the venice-uncensored tenant (default: 1)
- `SQL_SMOKE_UNCENSORED_QUOTA_MAX`: Maximum DIEM units for the venice-uncensored tenant (default: 3)
- `SQL_SMOKE_UNCENSORED_MAX_PASSES`: Additional venice-uncensored drain passes before stopping (default: 5)

## Tuning Profiles

Set `SQL_SMOKE_PROFILE` to apply guardrail presets while keeping manual overrides authoritative.
- `local-dev`: RPS 6, duration 20s, concurrency 10; SQL-light 3 RPS, 12s, concurrency 6 (disables low-tier 429 assertion)
- `local-dev-diem`: RPS 3, duration 18s, concurrency 6; SQL-light 4 RPS, 30s, concurrency 6; enables venice-admin usage mode, additional DIEM drain passes, usage delta enforcement, and DIEM 429 validation.
- `ci`: RPS 6, duration 20s, concurrency 12; SQL-light 3 RPS, 10s, concurrency 6
- `staging`: RPS 10, duration 30s, concurrency 20; SQL-light 5 RPS, 12s, concurrency 10

## Usage Snapshot Modes

`SQL_SMOKE_USAGE_MODE` controls how pre/post usage files are collected:
- `broker` (default): call `/v1/tenants/{id}/usage` via the broker API using `BROKER_ADMIN_TOKEN`.
- `venice-direct`: execs inside the broker container and queries the Venice API with the container's parent key, retrying across several impersonation strategies. Results include `_fetchMeta.attempts` for debugging.
- `venice-admin`: execs inside the broker container and collects both usage logs and rate-limit snapshots with the parent key, annotating `_summary.diem` for DIEM accounting.

The `venice-direct` mode requires Docker execution (prefix `docker-*`). When it cannot retrieve usage it writes a structured error payload that the smoke harness logs as a warning unless strict usage enforcement is enabled.

## Optimization Highlights

### Before (Duplication)
- **2 separate scripts**: ~450 lines each
- **Identical logic**: tenant creation, usage tracking, validation
- **Maintenance burden**: bug fixes required in 2 places
- **Inconsistent behavior**: Docker vs Replit could drift

### After (Unified)
- **1 shared library**: ~280 lines of reusable functions
- **2 thin wrappers**: ~170 lines each (environment-specific only)
- **Single source of truth**: bug fixes in one place
- **Consistent behavior**: same validation logic everywhere
- **40% reduction**: ~900 lines → ~620 lines total

### Key Abstractions

1. **`create_tenant_with_retry()`**: Handles Docker vs bare metal via `cmd_prefix` parameter
2. **`run_probe_phase()`**: Auto-detects Docker vs Replit from prefix string
3. **`run_validations()`**: Unified validation logic with configurable thresholds
4. **Python helpers**: Inline heredocs for JSON parsing (no temp files)

## Design Principles

### 1. Fail Fast with Context
- Venice key check before tenant creation
- Detailed error capture (stderr → logs)
- Probe-chat failure detection with actionable warnings

### 2. Graceful Degradation
- Probe-chat can fail (502) but rate-limit probes continue (admin impersonation)
- Soak mode allows warnings without exit
- Counter compaction failures don't block cleanup

### 3. Observable Artifacts
- Every probe phase → 3 files (log, summary, histogram)
- Pre/post usage snapshots for delta validation
- Status histogram for error classification

### 4. Composable Validation
- `run_validations()` accepts file paths, not in-memory state
- Each validator is a pure function (file → exit code)
- Easy to add new validators without refactoring

## Common Issues

### Venice Auth Error (401)
**Symptom**: `"venice error: Venice auth error 401"` in probe-chat

**Cause**: Venice parent key invalid or subkey creation failed

**Fix**:
```bash
# Docker
docker compose exec broker env | grep VENICE

# Replit
echo $VENICE_PARENT_KEY
```

If `SQL_SMOKE_ENFORCE_USAGE_DELTA=1`, this error halts the run; resolve the key before rerunning with enforcement.

### No Rate Limiting (429s)
**Symptom**: `rate_limited=0` for all tenants

**Cause**: `RATE_LIMITS_ENABLED=false` in broker

**Fix**: Set `RATE_LIMITS_ENABLED=true` in `.env.docker` or `docker/.env.local`

### Empty Counters
**Symptom**: `/v1/debug/counters` returns `[]`

**Cause**: Compaction disabled or no KV keys written

**Fix**:
```bash
# Check if rate limiter is writing keys
docker compose exec broker redis-cli --scan --pattern "rl:tenant:*"

# Force compaction
docker compose exec broker python apps/cli/main.py data:compact-counters --force
```

### High Error Rate
**Symptom**: `success_ratio < threshold`

**Cause**: Venice API latency, broker overload, or network issues

**Fix**:
- Reduce RPS: `RPS=5 bash scripts/docker_sql_multi_tenant_smoke.sh`
- Increase timeout: `PROBE_TIMEOUT=60 ...`
- Enable soak mode: `SQL_SMOKE_SOAK_MODE=1 ...`

## Future Enhancements

### Potential Improvements
1. **Parallel tenant probes**: Run all 3 tenants concurrently
2. **Streaming results**: Emit metrics to stdout as JSON lines
3. **Grafana integration**: Push histograms to Prometheus
4. **SQL query execution**: Actually run SQL queries (not just prompts)
5. **Chaos injection**: Random tenant revocations, network delays

### Non-Goals
- **Load testing**: Use dedicated tools (k6, Locust) for capacity planning
- **Security testing**: Use OWASP ZAP or similar for vuln scanning
- **End-to-end UI**: This is API-level smoke, not browser automation

## Contributing

When adding new validations:
1. Add pure function to `sql_smoke_lib.sh`
2. Call from `run_validations()` or inline in wrappers
3. Update this README with new env vars or artifacts
4. Test both Docker and Replit paths

When fixing bugs:
1. Fix in `sql_smoke_lib.sh` if common logic
2. Fix in wrapper if environment-specific
3. Add test case or assertion to prevent regression

