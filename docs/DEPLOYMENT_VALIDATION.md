# Deployment Environment Validation

Overview of deployment-specific environment validation scripts.

## Quick Commands

```bash
# Validate Docker deployment
python scripts/validate_docker_env.py

# Validate Replit deployment
python scripts/validate_replit_env.py

# Comprehensive validation (all contexts)
python scripts/validate_broker_env.py
```

## Validation Scripts

### Docker: `scripts/validate_docker_env.py`

**Purpose:** Validates Docker-specific configuration in `docker/.env.local` and ensures `.env` doesn't contain Docker-specific values.

**Checks:**
- Required files (`docker/.env.local`, `docker-compose.yml`)
- Database configuration (`SQL_DATABASE_URL` in `docker/.env.local`)
- Required secrets (BROKER_ADMIN_TOKEN, VENICE_API_KEY, etc.)
- Docker Compose services (postgres, redis, broker, orchestrator)
- Network configuration (BASE_RPC_URL, BASE_CHAIN_ID)

**See:** `docs/DOCKER_DEPLOYMENT.md` for complete guide

### Replit: `scripts/validate_replit_env.py`

**Purpose:** Validates Replit-specific configuration in `.env` and documents required secrets for Replit Secrets Manager.

**Checks:**
- Database variables marked as `"set-in-secrets"` in `.env`
- Docker-specific vars blank in `.env`
- Required config vars (VENICE_API_BASE_URL, BASE_RPC_URL, etc.)
- Replit Secrets requirements documented
- Best practices (no SQL_CREATE_ALL_ON_START, etc.)

**See:** `docs/REPLIT_DEPLOYMENT.md` for complete guide

### Comprehensive: `scripts/validate_broker_env.py`

**Purpose:** Validates broker configuration across all deployment contexts (Docker, Replit, local).

**Use:** For comprehensive validation when you need to check all contexts at once.

**Note:** For deployment-specific validation, use the dedicated scripts above.

## Configuration Matrix

| Variable | Docker | Replit |
|----------|--------|--------|
| `SQL_DATABASE_URL` | Set in `docker/.env.local` | `"set-in-secrets"` in `.env`, actual value in Secrets |
| `POSTGRES_HOST` | Set in `docker/.env.local` if needed | Blank in `.env` |
| `POSTGRES_PORT` | Set in `docker/.env.local` if needed | Blank in `.env` |
| `POSTGRES_DB` | Set in `docker/.env.local` if needed | Blank in `.env` |
| `POSTGRES_USER` | Set in `docker/.env.local` if needed | Blank in `.env` |
| `POSTGRES_PASSWORD` | Set in `docker/.env.local` if needed | Blank in `.env` |
| `DATABASE_URL` | Can be set | Blank (Replit provides automatically) |
| `REDIS_URL` | Set in `docker/.env.local` | Optional (use `REPLIT_DB_URL` instead) |
| `REPLIT_DB_URL` | N/A | Set in Replit Secrets Manager |
| `BROKER_ADMIN_TOKEN` | Set in `docker/.env.local` | Set in Replit Secrets Manager |
| `VENICE_API_KEY` | Set in `docker/.env.local` | Set in Replit Secrets Manager |
| `VENICE_PARENT_KEY` | Set in `docker/.env.local` | Set in Replit Secrets Manager |
| `VENICE_API_BASE_URL` | Set in `docker/.env.local` | Set in `.env` (must include `/api/v1`) |
| `BASE_RPC_URL` | Set in `.env` or `docker/.env.local` | Set in `.env` |
| `BASE_CHAIN_ID` | Set in `.env` or `docker/.env.local` | Set in `.env` (should be `8453`) |

## Exit Codes

All validation scripts use consistent exit codes:

- `0` - Success (no critical/high issues)
- `1` - High-priority issues found
- `2` - Critical issues found

## CI/CD Integration

```bash
# Docker validation
python scripts/validate_docker_env.py || exit 1

# Replit validation
python scripts/validate_replit_env.py || exit 1
```

## Documentation

- **`docs/DOCKER_DEPLOYMENT.md`** - Complete Docker deployment guide
- **`docs/REPLIT_DEPLOYMENT.md`** - Complete Replit deployment guide
- **`docs/VALIDATION_QUICK_REFERENCE.md`** - Quick reference matrix

## Common Issues

### POSTGRES_* vars in .env

**Problem:** Docker-specific variables set in shared `.env` file.

**Fix:** Blank them out in `.env`, set in `docker/.env.local` for Docker.

### SQL_DATABASE_URL location

**Docker:** Set actual value in `docker/.env.local`  
**Replit:** Mark as `"set-in-secrets"` in `.env`, set actual value in Secrets Manager

### VENICE_API_BASE_URL format

**Both:** Must include `/api/v1` suffix  
**Example:** `https://api.venice.ai/api/v1`

## Getting Help

1. Run the appropriate validation script
2. Review the detailed error messages
3. Check the deployment-specific guide:
   - Docker → `docs/DOCKER_DEPLOYMENT.md`
   - Replit → `docs/REPLIT_DEPLOYMENT.md`
4. See `docs/VALIDATION_QUICK_REFERENCE.md` for quick fixes
