# Platform-Agnostic Architecture Guide

**Version:** 1.0  
**Date:** 2025-11-11  
**Purpose:** Unified deployment strategy across Docker and Replit environments

---

## Executive Summary

Venice Capacity Broker operates in two distinct deployment environments:

1. **Docker** - Multi-container orchestration with self-hosted infrastructure
2. **Replit** - Serverless, managed services with integrated tooling

This document establishes a **platform-agnostic abstraction layer** that ensures consistent behavior, seamless migrations, and elegant deployments across both platforms.

---

## Current State Analysis

### Docker Deployment Model

**Architecture:**
```
┌─────────────────────────────────────────────────┐
│  docker-compose.yml (Orchestrator)              │
├─────────────────────────────────────────────────┤
│  ├─ postgres:15        (self-hosted)            │
│  ├─ redis:7            (self-hosted)            │
│  ├─ broker             (schema: broker)         │
│  ├─ orchestrator       (schema: orchestrator)   │
│  └─ token-watcher      (schema: token_watcher)  │
└─────────────────────────────────────────────────┘
```

**Startup Chain:**
```
docker_entrypoint.sh
  └─> prestart.sh
      └─> docker_start_broker.sh
          ├─> validate_broker_env.py
          ├─> alembic upgrade head
          ├─> market:pools:watch --once
          ├─> pytest -q (background)
          └─> uvicorn (broker API)
```

**Environment Cascade:**
```
.env → .env.docker → docker/.env.local → container environment
```

**Storage:**
- **Structured Data:** PostgreSQL (volume-backed)
- **KV Store:** Redis
- **Object Storage:** Local filesystem (bind mounts)

**Schema Isolation:**
Each service uses `DATABASE_SCHEMA` environment variable to create isolated namespaces within a single PostgreSQL database, preventing Alembic version table collisions.

---

### Replit Deployment Model

**Architecture:**
```
┌─────────────────────────────────────────────────┐
│  Replit VM (Single Process Model)               │
├─────────────────────────────────────────────────┤
│  ├─ Replit Database    (PostgreSQL 16 on Neon)  │
│  ├─ Replit DB          (Native KV store)        │
│  ├─ App Storage        (S3-compatible)          │
│  └─ Main Process       (broker + orchestrator)  │
│      ├─ Background: token-watcher (optional)    │
│      └─ Background: orchestrator loop (optional) │
└─────────────────────────────────────────────────┘
```

**Startup Chain:**
```
.replit → replit_run.sh [dry|live]
  └─> run_stack_entry.sh
      ├─> validate_broker_env.py
      ├─> alembic upgrade head
      ├─> python apps/cli/main.py start:stack
      │   ├─ Broker API (foreground)
      │   ├─ Orchestrator (background, optional)
      │   └─ Token Watcher (background, optional)
      └─> Port binding ($REPLIT_SERVER_PORT)
```

**Environment:**
- **Secrets:** REPLIT_DB_URL, DATABASE_URL, API keys
- **Dynamic:** REPLIT_SERVER_PORT, REPLIT_ENVIRONMENT
- **Config:** .env files (same cascade as Docker)

**Storage:**
- **Structured Data:** PostgreSQL 16 on Neon (managed, serverless)
- **KV Store:** Replit DB (native HTTP API)
- **Object Storage:** Replit App Storage (Python SDK)

**Key Differences:**
- No schema isolation needed (single tenant)
- Production vs Development database separation (Replit managed)
- Agent cannot modify production database (safety feature)

---

## Platform Abstraction Layers

### 1. Storage Abstraction

#### Structured Data (SQL)

**Current Implementation:** ✅ **Platform-Agnostic**

```python
# db/session.py & db/migrations/env.py
def get_url() -> str:
    return os.getenv("SQL_DATABASE_URL") or os.getenv("DATABASE_URL") or ""

def get_schema() -> str | None:
    return os.getenv("DATABASE_SCHEMA") or None
```

**Platform Mapping:**
| Platform | Variable | Value Example | Schema Isolation |
|----------|----------|---------------|------------------|
| Docker | `SQL_DATABASE_URL` | `postgresql://postgres:postgres@postgres:5432/postgres` <!-- gitleaks:allow example --> | ✅ Per-service |
| Replit Dev | `DATABASE_URL` | `postgresql://neon.tech:5432/replit_dev` | ❌ Single schema |
| Replit Prod | `DATABASE_URL` | `postgresql://neon.tech:5432/replit_prod` | ❌ Single schema |

**Migration Strategy:**

**❌ CURRENT ISSUE:** Migrations don't commit properly in `db/migrations/env.py`

```python
# Problem: Transaction commits without explicit flush
with context.begin_transaction():
    context.run_migrations()
    # Missing: connection.commit() or proper transaction handling
```

**Resolution Required:** See "Critical Fixes" section below.

---

#### Key-Value Store

**Current Implementation:** ✅ **Platform-Agnostic** (with fallback)

```python
# libs/kv/client.py
class KVStore:
    def __init__(self):
        self.redis_url = os.getenv("REDIS_URL") or os.getenv("KV_REDIS_URL")
        self.base_url = os.getenv("REPLIT_DB_URL") or os.getenv("KV_URL")
        # In-memory fallback (disabled in production)
        self._mem: Dict[str, Tuple[Any, Optional[float]]] = {}
```

**Platform Mapping:**
| Platform | Primary | Fallback | In-Memory Allowed |
|----------|---------|----------|-------------------|
| Docker | `REDIS_URL=redis://redis:6379/0` | In-memory | ✅ Dev only |
| Replit | `REPLIT_DB_URL` | In-memory | ✅ Dev only |
| Production | Either required | ❌ Disabled | ❌ Never |

**Safety Guardrails:**
```bash
# Live mode validation (docker_start_broker.sh, replit_run.sh)
if [ "${AUTOSTART_ORCHESTRATOR_LIVE:-0}" = "1" ]; then
  if [ -z "${REDIS_URL:-}" ] && [ -z "${REPLIT_DB_URL:-}" ]; then
    echo "ERROR: Live mode requires durable KV store"
    exit 1
  fi
  export ALLOW_INMEMORY_KV_FALLBACK=0
fi
```

---

#### Object Storage

**Current Status:** ⚠️ **Partially Implemented**

**Platform Mapping:**
| Platform | Implementation | Use Case |
|----------|----------------|----------|
| Docker | Local filesystem | Logs, state files, SQLite backups |
| Replit | App Storage SDK | Persistent files across deployments |

**Replit App Storage Integration:** (Recommended)

```python
# libs/storage/client.py (NEW)
from replit.object_storage import Client

class ObjectStore:
    def __init__(self):
        self.is_replit = bool(os.getenv("REPLIT_DB_URL"))
        if self.is_replit:
            self.client = Client()  # Uses default bucket
        else:
            self.base_path = Path(os.getenv("STORAGE_PATH", "/app/storage"))
    
    def upload_from_text(self, key: str, data: str) -> None:
        if self.is_replit:
            self.client.upload_from_text(key, data)
        else:
            (self.base_path / key).write_text(data)
    
    def download_as_text(self, key: str) -> str:
        if self.is_replit:
            return self.client.download_as_text(key)
        else:
            return (self.base_path / key).read_text()
```

---

### 2. Service Orchestration

#### Docker: Multi-Container Pattern

**Strengths:**
- ✅ True service isolation
- ✅ Independent scaling
- ✅ Health checks and dependencies
- ✅ Network isolation

**Challenges:**
- ❌ Higher resource overhead
- ❌ Complex local development
- ❌ State management across containers

#### Replit: Single-Process with Background Tasks

**Strengths:**
- ✅ Simplified deployment
- ✅ Serverless scaling
- ✅ Integrated observability
- ✅ Zero infrastructure management

**Challenges:**
- ❌ Process coupling
- ❌ Shared resource limits
- ❌ Graceful shutdown complexity

#### Unified Pattern: "Stack Manager"

**Concept:** Abstract service lifecycle regardless of deployment model

```python
# services/stack/manager.py (PROPOSED)
class StackManager:
    """Platform-agnostic service orchestration"""
    
    def __init__(self):
        self.is_docker = os.path.exists("/.dockerenv")
        self.is_replit = bool(os.getenv("REPLIT_ENVIRONMENT"))
    
    async def start_broker_api(self):
        """Start broker API (foreground in Replit, container in Docker)"""
        pass
    
    async def start_orchestrator(self):
        """Start orchestrator loop (background in both)"""
        pass
    
    async def start_token_watcher(self):
        """Start token watcher (background in both)"""
        pass
    
    async def health_check(self) -> Dict[str, str]:
        """Unified health check across all services"""
        pass
```

---

### 3. Environment Configuration

#### Current Cascade (Both Platforms)

```
.env                    # Base configuration, placeholders
  ↓
.env.docker            # Docker-specific defaults (Docker only)
  ↓
docker/.env.local      # Local overrides (gitignored, Docker only)
  ↓
Replit Secrets         # Secure credentials (Replit only)
  ↓
Container/Process ENV  # Runtime overrides
```

#### Unified Resolution Strategy

**Proposed: `config/loader.py`**

```python
class ConfigLoader:
    """Platform-agnostic configuration resolver"""
    
    PRECEDENCE = [
        ".env",                     # Base (both)
        ".env.docker",              # Docker-specific
        "docker/.env.local",        # Docker local
        "replit_secrets",           # Replit Secrets API
        "environment",              # OS environment (final)
    ]
    
    def load(self) -> Dict[str, str]:
        """Load and merge configuration with platform awareness"""
        config = {}
        for source in self.PRECEDENCE:
            if self._should_load(source):
                config.update(self._load_source(source))
        return config
    
    def _should_load(self, source: str) -> bool:
        """Check if source applies to current platform"""
        if source.startswith("docker/") and not self.is_docker:
            return False
        if source == "replit_secrets" and not self.is_replit:
            return False
        return True
```

---

### 4. Migration & Schema Management

#### Current Implementation

**Schema Isolation (Docker):**
```yaml
# docker-compose.yml
broker:
  environment:
    DATABASE_SCHEMA: broker
orchestrator:
  environment:
    DATABASE_SCHEMA: orchestrator
token-watcher:
  environment:
    DATABASE_SCHEMA: token_watcher
```

**Benefits:**
- ✅ Prevents Alembic version table collisions
- ✅ Logical separation of concerns
- ✅ Independent migration states

**Replit Approach:**
- Single schema (no isolation needed)
- Development vs Production databases (Replit managed)
- Agent-safe: Cannot modify production database

#### ❌ **CRITICAL BUG: Migration Transaction Handling**

**Current Code (`db/migrations/env.py:87-112`):**

```python
with connectable.connect() as connection:
    if schema:
        connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        connection.commit()  # ✅ Schema creation commits
    
    # ...
    
    with context.begin_transaction():
        if schema:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
        context.run_migrations()  # ✅ Runs successfully
        # ❌ NO COMMIT - Transaction rolls back when context exits!
```

**Evidence from logs/runtime.log:**
- Lines 19-49: Migrations execute ✅
- Lines 50-247: `alembic_version` table missing ❌
- Database verification: Only schemas exist, no tables ❌

**Root Cause:**
The `context.begin_transaction()` context manager doesn't automatically commit. When it exits, the transaction rolls back, destroying all migration work.

---

## Critical Fixes Required

### Fix #1: Migration Transaction Commit

**File:** `db/migrations/env.py`

**Current (Broken):**
```python
with context.begin_transaction():
    if schema:
        connection.execute(text(f'SET search_path TO "{schema}", public'))
    context.run_migrations()
```

**Fixed:**
```python
with context.begin_transaction():
    if schema:
        connection.execute(text(f'SET search_path TO "{schema}", public'))
    context.run_migrations()

# Ensure transaction commits
if connection.in_transaction():
    connection.commit()
```

**Alternative (Use Alembic's auto-commit mode):**
```python
configure_kwargs = dict(
    connection=connection,
    target_metadata=target_metadata,
    version_table_schema=schema,
    include_schemas=bool(schema),
    transaction_per_migration=True,  # ← Add this
)
```

---

### Fix #2: Platform Detection Utilities

**File:** `libs/platform/detector.py` (NEW)

```python
"""Platform detection and capability reporting"""
import os
from dataclasses import dataclass
from typing import Literal

PlatformType = Literal["docker", "replit", "local"]

@dataclass
class PlatformCapabilities:
    platform: PlatformType
    has_redis: bool
    has_replit_db: bool
    has_postgres: bool
    has_object_storage: bool
    supports_background_tasks: bool
    supports_multi_process: bool

def detect_platform() -> PlatformType:
    """Detect current deployment platform"""
    if os.path.exists("/.dockerenv"):
        return "docker"
    elif os.getenv("REPLIT_ENVIRONMENT") or os.getenv("REPLIT_DB_URL"):
        return "replit"
    else:
        return "local"

def get_capabilities() -> PlatformCapabilities:
    """Get platform capabilities"""
    platform = detect_platform()
    
    if platform == "docker":
        return PlatformCapabilities(
            platform="docker",
            has_redis=True,
            has_replit_db=False,
            has_postgres=True,
            has_object_storage=False,  # Local filesystem only
            supports_background_tasks=True,
            supports_multi_process=True,
        )
    elif platform == "replit":
        return PlatformCapabilities(
            platform="replit",
            has_redis=bool(os.getenv("REDIS_URL")),
            has_replit_db=bool(os.getenv("REPLIT_DB_URL")),
            has_postgres=bool(os.getenv("DATABASE_URL")),
            has_object_storage=True,  # Replit App Storage
            supports_background_tasks=True,
            supports_multi_process=False,  # Single VM
        )
    else:  # local
        return PlatformCapabilities(
            platform="local",
            has_redis=bool(os.getenv("REDIS_URL")),
            has_replit_db=False,
            has_postgres=bool(os.getenv("SQL_DATABASE_URL")),
            has_object_storage=False,
            supports_background_tasks=True,
            supports_multi_process=True,
        )
```

---

### Fix #3: Unified Startup Script

**File:** `scripts/unified_start.sh` (NEW)

```bash
#!/usr/bin/env bash
set -euo pipefail

# Platform-agnostic startup script
# Works in Docker, Replit, and local development

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

log() {
    printf '[unified-start] %s\n' "$*" >&2
}

detect_platform() {
    if [ -f /.dockerenv ]; then
        echo "docker"
    elif [ -n "${REPLIT_ENVIRONMENT:-}" ] || [ -n "${REPLIT_DB_URL:-}" ]; then
        echo "replit"
    else
        echo "local"
    fi
}

validate_environment() {
    log "Validating environment configuration"
    python scripts/validate_broker_env.py || {
        log "WARNING: Environment validation failed"
        return 1
    }
}

apply_migrations() {
    log "Applying database migrations"
    python -m alembic upgrade head || {
        log "ERROR: Migration failed"
        return 1
    }
}

refresh_market_data() {
    if [ "${MARKET_POOLS_REFRESH:-1}" != "0" ]; then
        log "Refreshing DEX pool catalog"
        python apps/cli/main.py market:pools:watch --once || {
            log "WARNING: Pool refresh failed"
        }
    fi
}

run_tests() {
    if [ "${BROKER_SKIP_TESTS:-0}" = "1" ]; then
        log "Tests skipped (BROKER_SKIP_TESTS=1)"
        return 0
    fi
    
    if [ "${BROKER_TESTS_BACKGROUND:-1}" = "1" ]; then
        log "Running tests in background"
        (timeout 900 pytest -q || true) &
    else
        log "Running tests (blocking)"
        pytest -q
    fi
}

start_services() {
    local platform="$1"
    local mode="${2:-dry}"  # dry or live
    
    case "$platform" in
        docker)
            log "Docker detected - starting broker API"
            exec uvicorn apps.broker_api.app:app --host 0.0.0.0 --port 8000
            ;;
        replit)
            log "Replit detected - starting stack"
            if [ "$mode" = "live" ]; then
                exec python apps/cli/main.py start:stack --enable-live
            else
                exec python apps/cli/main.py start:stack
            fi
            ;;
        local)
            log "Local development - starting broker API"
            exec uvicorn apps.broker_api.app:app --host 127.0.0.1 --port 8000 --reload
            ;;
    esac
}

main() {
    local platform
    local mode="${1:-dry}"
    
    platform="$(detect_platform)"
    log "Platform: $platform, Mode: $mode"
    
    cd "$PROJECT_ROOT"
    
    # Startup sequence
    validate_environment || true
    apply_migrations || exit 1
    refresh_market_data || true
    run_tests || true
    
    # Start services
    start_services "$platform" "$mode"
}

main "$@"
```

---

## Implementation Roadmap

### Phase 1: Critical Fixes (Immediate)
- [x] **Diagnose migration transaction issue** ✅ COMPLETE
- [ ] **Fix `db/migrations/env.py` transaction handling**
- [ ] **Verify migrations persist correctly**
- [ ] **Test in both Docker and Replit**

### Phase 2: Platform Abstraction (Week 1)
- [ ] Create `libs/platform/detector.py`
- [ ] Create `libs/storage/client.py` (Object storage)
- [ ] Update `libs/kv/client.py` with better error messages
- [ ] Create `scripts/unified_start.sh`

### Phase 3: Service Orchestration (Week 2)
- [ ] Create `services/stack/manager.py`
- [ ] Refactor `apps/cli/main.py` to use StackManager
- [ ] Update Docker entrypoint to use unified_start.sh
- [ ] Update Replit .replit to use unified_start.sh

### Phase 4: Testing & Validation (Week 3)
- [ ] Create platform-specific test suites
- [ ] Add `tests/test_platform_abstraction.py`
- [ ] Add `tests/test_migrations_docker.py`
- [ ] Add `tests/test_migrations_replit.py`
- [ ] Integration tests for object storage
- [ ] Integration tests for KV store fallback

### Phase 5: Documentation (Week 4)
- [ ] Update `docs/CONFIGURATION.md` with platform differences
- [ ] Update `docs/DEPLOYMENT.md` with unified guidance
- [ ] Create `docs/TROUBLESHOOTING.md` per-platform
- [ ] Update `README.md` with quick-start for both platforms

---

## Testing Strategy

### Platform-Specific Tests

**Docker Tests:**
```bash
# Full stack with all services
docker-compose up -d
docker-compose exec broker pytest tests/test_migrations_docker.py
docker-compose exec broker pytest tests/test_kv_redis.py
docker-compose down
```

**Replit Tests:**
```python
# tests/test_platform_replit.py
def test_replit_db_connection():
    """Verify Replit DB KV store works"""
    assert os.getenv("REPLIT_DB_URL")
    from libs.kv.client import KVStore
    kv = KVStore()
    kv.set("test_key", "test_value")
    assert kv.get("test_key") == "test_value"

def test_replit_app_storage():
    """Verify Replit App Storage works"""
    from libs.storage.client import ObjectStore
    store = ObjectStore()
    store.upload_from_text("test.txt", "Hello Replit")
    assert store.download_as_text("test.txt") == "Hello Replit"
```

### Cross-Platform Tests

**Unified Test Suite:**
```python
# tests/test_platform_agnostic.py
@pytest.mark.parametrize("platform", ["docker", "replit", "local"])
def test_database_connection(platform, monkeypatch):
    """Verify database connection works on all platforms"""
    setup_platform_env(platform, monkeypatch)
    from db.session import get_engine
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1"))
        assert result.scalar() == 1

def test_kv_store_platform_detection():
    """Verify KV store selects correct backend"""
    from libs.kv.client import KVStore
    kv = KVStore()
    if os.getenv("REPLIT_DB_URL"):
        assert kv.base_url is not None
    elif os.getenv("REDIS_URL"):
        assert kv.redis_url is not None
```

---

## Best Practices

### 1. Environment Variables

**✅ DO:**
- Use consistent variable names across platforms
- Provide sensible defaults
- Validate required variables at startup
- Document platform-specific overrides

**❌ DON'T:**
- Hard-code platform assumptions
- Use platform-specific variables in business logic
- Skip validation in production

### 2. Database Migrations

**✅ DO:**
- Test migrations in both platforms before deployment
- Use schema isolation in multi-tenant Docker setups
- Commit transactions explicitly
- Handle rollback scenarios gracefully

**❌ DON'T:**
- Assume auto-commit behavior
- Mix DDL and DML without transaction boundaries
- Ignore migration errors

### 3. Service Lifecycle

**✅ DO:**
- Abstract service startup behind unified interfaces
- Support graceful shutdown on all platforms
- Implement health checks consistently
- Log platform detection for debugging

**❌ DON'T:**
- Couple service logic to orchestration method
- Assume background tasks always succeed
- Block startup on non-critical failures

### 4. Storage Abstraction

**✅ DO:**
- Use platform-appropriate storage backends
- Implement consistent interfaces across backends
- Handle missing backends gracefully
- Provide fallback mechanisms for development

**❌ DON'T:**
- Assume filesystem persistence (Replit VMs are ephemeral)
- Mix storage concerns with business logic
- Ignore object storage limits (size, bandwidth)

---

## Monitoring & Observability

### Platform-Specific Metrics

**Docker:**
- Container health checks (docker-compose.yml)
- Resource usage (docker stats)
- Service logs (docker-compose logs)
- Network traffic (docker network inspect)

**Replit:**
- VM health (Replit dashboard)
- Database connection pool (Neon metrics)
- Replit DB requests (native metrics)
- App Storage usage (SDK metrics)

### Unified Telemetry

**Proposed:** OpenTelemetry integration

```python
# libs/telemetry/platform.py
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter

def setup_telemetry():
    """Configure platform-agnostic telemetry"""
    platform = detect_platform()
    
    tracer = trace.get_tracer(__name__)
    tracer.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter())
    )
    
    # Add platform as resource attribute
    tracer.set_attribute("platform", platform)
```

---

## Security Considerations

### Docker Security

- **Secrets:** Use Docker secrets or encrypted .env files
- **Network:** Isolate services on private networks
- **Volumes:** Restrict bind mount permissions
- **User:** Run containers as non-root

### Replit Security

- **Secrets:** Use Replit Secrets (encrypted at rest)
- **Database:** Production DB restricted from Agent edits
- **API Keys:** Scope permissions minimally
- **CORS:** Configure allowed origins explicitly

### Shared Security Practices

- **Validate inputs:** All environment variables
- **Encrypt connections:** TLS for all database connections
- **Rotate credentials:** Automated rotation where possible
- **Audit logs:** Track all configuration changes

---

## Appendix A: Environment Variable Reference

### Core Variables (Both Platforms)

| Variable | Purpose | Docker Default | Replit Default |
|----------|---------|----------------|----------------|
| `SQL_DATABASE_URL` | PostgreSQL connection | `postgresql://postgres:postgres@postgres:5432/postgres` <!-- gitleaks:allow example --> | From Replit DB |
| `DATABASE_URL` | Alt PostgreSQL | - | From Replit Secrets |
| `REDIS_URL` | Redis KV store | `redis://redis:6379/0` | Optional |
| `REPLIT_DB_URL` | Replit native KV | N/A | Auto-provided |
| `DATABASE_SCHEMA` | Schema isolation | `broker`, `orchestrator`, `token_watcher` | N/A |

### Platform-Specific Variables

**Docker Only:**
| Variable | Purpose | Default |
|----------|---------|---------|
| `BROKER_API_HOST` | API bind address | `0.0.0.0` |
| `BROKER_API_PORT` | API port | `8000` |

**Replit Only:**
| Variable | Purpose | Default |
|----------|---------|---------|
| `REPLIT_ENVIRONMENT` | Environment marker | Auto-set |
| `REPLIT_SERVER_PORT` | Dynamic port | Auto-set |
| `REPLIT_WORKSPACE_ID` | Workspace ID | Auto-set |

---

## Appendix B: Migration Command Reference

### Docker

```bash
# Apply migrations for all services
docker-compose exec broker alembic upgrade head
docker-compose exec orchestrator alembic upgrade head
docker-compose exec token-watcher alembic upgrade head

# Check migration status
docker-compose exec broker alembic current
docker-compose exec broker alembic history

# Reset database (DANGER)
docker-compose down -v  # Destroys data!
docker-compose up -d postgres redis
docker-compose run --rm broker alembic upgrade head
```

### Replit

```bash
# Apply migrations (single schema)
python -m alembic upgrade head

# Check status
python -m alembic current

# Production database migrations
# (Automatic during deployment, or manual via Replit console)
```

---

## Appendix C: Troubleshooting

### Issue: Migrations Don't Persist

**Symptoms:**
- Migrations execute successfully in logs
- `alembic_version` table missing
- Application tables missing

**Diagnosis:**
```sql
-- Check if schemas exist
SELECT nspname FROM pg_namespace WHERE nspname IN ('broker', 'orchestrator', 'token_watcher');

-- Check for alembic_version table
SELECT schemaname, tablename FROM pg_tables WHERE tablename = 'alembic_version';

-- Check for application tables
SELECT schemaname, tablename FROM pg_tables WHERE schemaname = 'broker';
```

**Solution:**
1. Fix transaction handling in `db/migrations/env.py` (see Critical Fixes)
2. Drop and recreate schemas
3. Re-run migrations
4. Verify tables exist

### Issue: KV Store Fallback to In-Memory

**Symptoms:**
- Warning: "Using in-memory KV fallback"
- Data doesn't persist across restarts
- Metrics show `fallback_inmemory_kv_total`

**Diagnosis:**
```bash
# Check KV configuration
echo "REDIS_URL=$REDIS_URL"
echo "REPLIT_DB_URL=$REPLIT_DB_URL"
echo "ALLOW_INMEMORY_KV_FALLBACK=$ALLOW_INMEMORY_KV_FALLBACK"
```

**Solution:**
- **Docker:** Set `REDIS_URL=redis://redis:6379/0`
- **Replit:** Ensure `REPLIT_DB_URL` is in Secrets
- **Production:** Set `ALLOW_INMEMORY_KV_FALLBACK=0` to fail fast

### Issue: Port Binding Conflicts (Replit)

**Symptoms:**
- `Address already in use` error
- Service doesn't respond on expected port

**Solution:**
```bash
# Use Replit-provided port
export BROKER_API_PORT="${REPLIT_SERVER_PORT:-8000}"
python apps/cli/main.py start:stack
```

---

## Conclusion

This architecture provides a **robust, platform-agnostic foundation** for Venice Capacity Broker that:

1. ✅ **Works seamlessly** across Docker and Replit
2. ✅ **Abstracts platform differences** into clean interfaces
3. ✅ **Maintains consistency** in behavior and configuration
4. ✅ **Supports future platforms** (AWS, GCP, Azure, etc.)
5. ✅ **Enables confident deployments** with comprehensive testing

**Next Steps:**
1. Apply Critical Fix #1 (migration transactions)
2. Implement Phase 2 (platform abstraction layer)
3. Test thoroughly on both platforms
4. Document platform-specific gotchas

**Questions?** See `docs/DEPLOYMENT.md` or open an issue.

---

**Maintained by:** Venice Engineering  
**Last Updated:** 2025-11-11  
**Review Cycle:** Quarterly or on platform changes
