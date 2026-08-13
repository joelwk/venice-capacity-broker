# Docker Deployment Configuration Guide

Complete guide for configuring and validating Docker deployments.

## Quick Start

```bash
# Validate Docker configuration
python scripts/validate_docker_env.py

# Start services
docker compose --env-file docker/.env.local up
```

## File Structure

```
.env                    # Shared config (non-sensitive defaults)
docker/.env.local       # Docker-specific secrets and config (gitignored)
docker-compose.yml      # Service definitions
```

## Required Configuration

### 1. Database Configuration

**In `docker/.env.local`:**
```bash
# gitleaks:allow
SQL_DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/postgres
```

**In `.env`:**
```bash
# POSTGRES_* vars should be blank (Docker-specific)
POSTGRES_HOST=
POSTGRES_PORT=
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
```

**Why:** Docker Compose provides PostgreSQL service at `postgres:5432`. The full connection string in `docker/.env.local` contains all connection info, so individual `POSTGRES_*` vars are not needed.

### 2. Required Secrets

**In `docker/.env.local` (gitignored):**

```bash
# Admin authentication
BROKER_ADMIN_TOKEN=<strong-random-token>

# Venice API access
VENICE_API_BASE_URL=https://api.venice.ai/api/v1
VENICE_API_KEY=<venice-api-key>
VENICE_PARENT_KEY=<venice-parent-key>

# Redis KV store
REDIS_URL=redis://redis:6379/0

# Base network (can be in .env)
BASE_RPC_URL=https://mainnet.base.org
BASE_CHAIN_ID=8453
```

### 3. Docker Compose Services

**Required services in `docker-compose.yml`:**
- `postgres` - PostgreSQL database
- `redis` - Redis KV store
- `broker` - Broker API service
- `orchestrator` - Orchestrator service (recommended)

## Validation

Run the Docker-specific validator:

```bash
python scripts/validate_docker_env.py
```

### What It Checks

1. **Required Files**
   - `docker/.env.local` exists
   - `docker-compose.yml` exists with required services

2. **Database**
   - `SQL_DATABASE_URL` set in `docker/.env.local` (not placeholder)
   - Points to PostgreSQL (not SQLite)
   - `POSTGRES_*` vars blank in `.env`

3. **Secrets**
   - `BROKER_ADMIN_TOKEN` set
   - `VENICE_API_KEY` set
   - `VENICE_PARENT_KEY` set
   - `VENICE_API_BASE_URL` includes `/api/v1`
   - `REDIS_URL` set

4. **Network**
   - `BASE_RPC_URL` configured
   - `BASE_CHAIN_ID` is 8453 (Base mainnet)

### Exit Codes

- `0` - No critical or high-priority issues
- `1` - High-priority issues found
- `2` - Critical issues found

## Common Issues

### Issue: POSTGRES_* vars have values in .env

**Problem:** Docker-specific variables are set in shared `.env` file.

**Fix:**
```bash
# In .env, set to blank:
POSTGRES_HOST=
POSTGRES_PORT=
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=
```

### Issue: SQL_DATABASE_URL is placeholder

**Problem:** `SQL_DATABASE_URL` is marked as `"set-in-secrets"` in `docker/.env.local`.

**Fix:** Set actual PostgreSQL connection string in `docker/.env.local`:
```bash
# gitleaks:allow
SQL_DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/postgres
```

### Issue: VENICE_API_BASE_URL missing /api/v1

**Problem:** Venice API base URL doesn't include `/api/v1` suffix.

**Fix:**
```bash
VENICE_API_BASE_URL=https://api.venice.ai/api/v1
```

### Issue: Missing Docker Compose service

**Problem:** Required service not defined in `docker-compose.yml`.

**Fix:** Add service definition to `docker-compose.yml`:
```yaml
postgres:
  image: postgres:16
  # ... service config
```

## Deployment Steps

1. **Create `docker/.env.local`:**
   ```bash
   cp docker/.env.local.example docker/.env.local
   ```

2. **Configure secrets:**
   - Edit `docker/.env.local` with actual values
   - Never commit this file (it's gitignored)

3. **Validate configuration:**
   ```bash
   python scripts/validate_docker_env.py
   ```

4. **Start services:**
   ```bash
   docker compose --env-file docker/.env.local up -d
   ```

5. **Check logs:**
   ```bash
   docker compose logs -f broker
   ```

## Environment Variable Reference

| Variable | Location | Required | Description |
|----------|----------|----------|-------------|
| `SQL_DATABASE_URL` | `docker/.env.local` | ✅ | PostgreSQL connection string |
| `POSTGRES_HOST` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_PORT` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_DB` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_USER` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_PASSWORD` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `REDIS_URL` | `docker/.env.local` | ✅ | Redis connection string |
| `BROKER_ADMIN_TOKEN` | `docker/.env.local` | ✅ | Admin authentication token |
| `VENICE_API_BASE_URL` | `docker/.env.local` | ✅ | Must include `/api/v1` |
| `VENICE_API_KEY` | `docker/.env.local` | ✅ | Venice API key |
| `VENICE_PARENT_KEY` | `docker/.env.local` | ✅ | Venice parent key |
| `BASE_RPC_URL` | `.env` or `docker/.env.local` | ✅ | Base RPC endpoint |
| `BASE_CHAIN_ID` | `.env` or `docker/.env.local` | ✅ | Should be `8453` |

## Related Documentation

- `docs/DEPLOYMENT_VALIDATION.md` - Validation overview
- `docs/VALIDATION_QUICK_REFERENCE.md` - Quick reference matrix
- `scripts/validate_docker_env.py` - Docker validation script

