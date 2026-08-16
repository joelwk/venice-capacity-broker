# Replit Deployment Configuration Guide

Complete guide for configuring and validating Replit deployments.

## Quick Start

```bash
# Validate Replit configuration
python scripts/validate_replit_env.py

# Replit automatically runs replit_prestart.sh on deploy
```

## File Structure

```
.env                    # Shared config with "set-in-secrets" markers
Replit Secrets Manager  # Actual secret values (set in Replit UI)
```

## Required Configuration

### 1. Database Configuration

**In `.env`:**
```bash
# Mark as "set-in-secrets" - actual value goes in Replit Secrets Manager
SQL_DATABASE_URL="set-in-secrets"

# POSTGRES_* vars should be blank (Docker-specific)
POSTGRES_HOST=
POSTGRES_PORT=
POSTGRES_DB=
POSTGRES_USER=
POSTGRES_PASSWORD=

# DATABASE_URL is blank - Replit provides automatically
DATABASE_URL=

# Optional: Read replica (if used)
SQL_DATABASE_URL_READONLY=
```

**In Replit Secrets Manager:**
- `SQL_DATABASE_URL` = Full PostgreSQL connection string from Replit Database tool
- Format: `postgresql+psycopg2://user:password@host:port/database` <!-- gitleaks:allow example connection string -->

**How Replit Database Works:**
1. Add SQL database in Replit Database tool
2. Replit automatically provides `DATABASE_URL` and Postgres helpers (`PGHOST`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`, `PGPORT`) as environment variables
3. `replit_prestart.sh` copies `DATABASE_URL` to `SQL_DATABASE_URL` if not set  
4. For production, copy connection string to Secrets Manager as `SQL_DATABASE_URL`                                                                              

### 2. Required Secrets (Replit Secrets Manager)

**Critical:**
- `SQL_DATABASE_URL` - PostgreSQL connection string
- `VENICE_API_KEY` - Venice API key for broker operations
- `VENICE_PARENT_KEY` - Venice parent key for sub-key creation
- `BROKER_ADMIN_TOKEN` - Admin authentication token

**Optional but Recommended:**
- `SQL_DATABASE_URL_READONLY` - Read-only connection string (if using read replicas)
- `ETH_PRIVATE_KEY` - Private key for live trading (if running orchestrator)
- `CDP_API_KEY_ID` - Coinbase Cloud API key ID (if using smart wallet)
- `CDP_API_KEY_SECRET` - Coinbase Cloud API key secret
- `CDP_WALLET_SECRET` - Coinbase Cloud wallet secret

### 3. Required Config (in `.env`)

```bash
# Venice API (must include /api/v1)
VENICE_API_BASE_URL=https://api.venice.ai/api/v1

# Base network
BASE_RPC_URL=https://mainnet.base.org
BASE_CHAIN_ID=8453

# Buyer pricing
# - market: DIEM-aware pricing using live market data (recommended for / and /buy.html)
# - static: requires PRICE_UNIT_ETH_WEI and/or PRICE_UNIT_USDC
PRICE_ENGINE=market
# BIDS_ENABLED=true  # optional; spot quotes work without this
```

**Replit tuning for Base RPC**

- `BASE_RPC_URL` defaults to `https://base.drpc.org` inside `scripts/replit_run.sh` to avoid rate-limit spikes on the default public RPC.
- `RPC_REQUEST_TIMEOUT_SECONDS` is set to `20` seconds in Replit so DEX previews have enough time to succeed when liquidity is thin.

### 4. KV Store (Optional)

**Option 1: Use Replit KV Store**
- Replit automatically provides `REPLIT_DB_URL` when KV store is added
- Set in Replit Secrets Manager if needed
- Format: `https://kv.replit.com/v0/<token>`

**Option 2: Use Redis**
- Set `REDIS_URL` in `.env` or Secrets Manager
- Format: `redis://host:port/db`

### 4.1 Logging Configuration

The logging system supports both human-readable and structured JSON formats. Logs are written to both stdout and file by default.

**Environment Variables:**
```bash
# Log level: DEBUG, INFO, WARNING, ERROR (default: INFO)
LOG_LEVEL=INFO

# Log format: empty for human-readable, "json" for structured JSON
# Use JSON for better parsing in Replit's log viewer
LOG_FORMAT=json

# Log directory (default: logs)
LOG_DIR=logs

# Log file basename (default: runtime.log)
LOG_BASENAME=runtime.log

# Console capture: 1 to tee stdout/stderr to log file (default: 1)
LOG_CAPTURE_CONSOLE=1

# Optional: explicit log file path (overrides LOG_DIR/LOG_BASENAME)
# LOG_FILE=/path/to/custom.log
```

**Recommendations:**

| Environment | LOG_FORMAT | LOG_LEVEL | Notes |
|-------------|------------|-----------|-------|
| Development | (empty) | DEBUG | Human-readable for local debugging |
| Staging | json | INFO | Structured for testing log parsing |
| Production | json | INFO | Structured for Replit log viewer |

**Viewing Logs:**

1. **Replit Console:** Logs appear in real-time during workflow runs
2. **Log Files:** Check `logs/runtime.log` for persistent logs
3. **JSON Parsing:** When `LOG_FORMAT=json`, each line is valid JSON for easy filtering

**Troubleshooting Logging Issues:**

- If logs don't appear: Check `LOG_CAPTURE_CONSOLE=1` is set
- If log file is empty: Ensure `LOG_DIR` directory exists and is writable
- For structured parsing: Set `LOG_FORMAT=json` in Secrets or `.env`

### 5. DIEM Composite Route Configuration (New)

**For Production:**
```bash
# In Replit Secrets Manager or .env:
DIEM_EXACT_IN_FALLBACK_ENABLE=1
DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0
DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
# Path Engine Timeout (default: 10.0s from config/default.yml)
# Override only if experiencing timeout issues on constrained Replit resources
# MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS=10.0

# Path Engine Provider Management (optional - defaults from config/default.yml)
# PATH_ENGINE_ROUTE_CACHE_TTL_SECONDS=60.0
# PATH_ENGINE_PROVIDER_TIMEOUT_THRESHOLD=3
# PATH_ENGINE_PROVIDER_BACKOFF_SECONDS=180.0
# PATH_ENGINE_MIN_ROUTE_BUDGET_SECONDS=0.35
# PATH_ENGINE_SOFT_TIMEOUT_MARGIN_SECONDS=0.75
```

**For Staging/Testing:**
```bash
DIEM_EXACT_IN_FALLBACK_ENABLE=1
DIEM_EXACT_IN_FALLBACK_MAX_USD=25.0
DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE=1  # Enable for diagnostics
DIEM_DEBUG_ROUTES=1  # Enable for troubleshooting
```

See `docs/DIEM_COMPOSITE_CONFIG.md` for detailed configuration guide.

### 6. Risk Policy Configuration

**Risk limits (in `.env` or Secrets Manager):**
```bash
# Slippage and pool impact caps
RISK_MAX_SLIPPAGE_BPS=50  # 0.5% default slippage cap
RISK_MAX_POOL_TAKE_BPS=10  # 0.1% max pool impact (proportionally reduced with slippage cap)

# Trade sizing limits
RISK_MAX_DIEM_TRADE_USD=10000.0  # Max notional per trade in USD
RISK_MAX_DIEM_INVENTORY_USD=100000.0  # Max inventory cap in USD
RISK_MAX_DIEM_TRADE_UNITS=0  # Absolute max units per trade (overrides USD, supports scientific notation like 5e17)

# Staking limits
RISK_MAX_STAKE_USD=0.0  # Max stake in USD (0 = disabled, uses inventory cap)

# Utilization and volatility adjustments
RISK_UTIL_ALPHA=0.5  # Utilization multiplier: 1 + alpha * utilization
RISK_MAX_VOLATILITY_BPS=0  # Max volatility cap in bps (0 = disabled)

# DIEM arbitrage thresholds
DIEM_PREMIUM_THRESHOLD=1.05  # Premium multiple to trigger mint/sell
DIEM_DISCOUNT_THRESHOLD=0.0  # Discount multiple to trigger buy/burn (0 = uses premium_threshold)
```

**Note:** These values can be set in `.env` for non-sensitive defaults, or in Replit Secrets Manager if you need different values per environment.

## Validation

Run the Replit-specific validator:

```bash
python scripts/validate_replit_env.py
```

### What It Checks

1. **Database**
   - `SQL_DATABASE_URL` marked as `"set-in-secrets"` in `.env`
   - `POSTGRES_*` vars blank in `.env`
   - `DATABASE_URL` blank (Replit provides automatically)

2. **Secrets**
   - Required secrets documented for Secrets Manager
   - `REPLIT_DB_URL` properly configured (if using KV store)

3. **Config**
   - `VENICE_API_BASE_URL` includes `/api/v1`
   - `BASE_RPC_URL` configured
   - `BASE_CHAIN_ID` is 8453
   - No Docker-specific values in `.env`

4. **Best Practices**
   - `SQL_CREATE_ALL_ON_START` not used (should use Alembic)
   - No duplicate variables

### Exit Codes

- `0` - No critical or high-priority issues
- `1` - High-priority issues found
- `2` - Critical issues found

## Setting Secrets in Replit

1. **Open Replit workspace**
2. **Click Secrets icon (🔒)** in left sidebar
3. **Add secret:**
   - Key: `SQL_DATABASE_URL`
   - Value: Copy from Replit Database tool connection string
4. **Repeat for all required secrets**
5. **Click "Add secret"** for each

## Common Issues

### Issue: Market Snapshot missing ETH / prices slow or disappearing on `/buy.html`

**Symptom**: The `/buy.html` Market Snapshot intermittently shows no ETH (or no assets), or prices take a long time to appear and then disappear.

This usually means `/v1/env-and-prices` and `/v1/market/prices` are returning **partial results** due to marketdata batch timeouts during cold-start DEX/RPC initialization.

**Fix**:

- Set `BROKER_WARMUP_MARKETDATA_TIMEOUT_SECONDS=30` so the startup warmers can fully populate caches even when DEX/RPC init exceeds UI-oriented timeouts.
- Set `MARKETDATA_PRICES_TIMEOUT_SECONDS=20` so the first interactive request has enough time to fetch ETH/USDC/WBTC/DIEM on Replit.

These are safe to set in Replit Secrets or `.replit` `[env]` for deployments.

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

### Issue: SQL_DATABASE_URL is actual value in .env

**Problem:** `SQL_DATABASE_URL` contains actual connection string instead of `"set-in-secrets"`.

**Fix:**
```bash
# In .env:
SQL_DATABASE_URL="set-in-secrets"

# Then set actual value in Replit Secrets Manager
```

### Issue: VENICE_API_BASE_URL missing /api/v1

**Problem:** Venice API base URL doesn't include `/api/v1` suffix.

**Fix:**
```bash
VENICE_API_BASE_URL=https://api.venice.ai/api/v1
```

### Issue: REPLIT_DB_URL is placeholder

**Problem:** `REPLIT_DB_URL` is missing the `/v0/<token>` segment.

**Fix:** Set full value in Replit Secrets Manager:
```
https://kv.replit.com/v0/<your-token-here>
```

### Issue: SQL_CREATE_ALL_ON_START enabled

**Problem:** Using `SQL_CREATE_ALL_ON_START=1` instead of Alembic migrations.

**Fix:**
```bash
# Remove from .env:
SQL_CREATE_ALL_ON_START=false

# Use Alembic instead (already in replit_prestart.sh):
alembic upgrade head
```

## Deployment Steps

1. **Configure `.env`:**
   - Mark secrets as `"set-in-secrets"`
   - Set required config vars
   - Blank out Docker-specific vars

2. **Add SQL Database:**
   - Open Replit Database tool
   - Click "Add database"
   - Replit provides `DATABASE_URL` automatically

3. **Set Secrets:**
   - Open Replit Secrets Manager
   - Add all required secrets
   - Copy connection string from Database tool

4. **Validate configuration:**
   ```bash
   python scripts/validate_replit_env.py
   ```

5. **Deploy:**
   - Replit automatically runs `replit_prestart.sh`
   - Migrations run via `alembic upgrade head`
   - Services start automatically

## Environment Variable Reference

| Variable | Location | Required | Description |
|----------|----------|----------|-------------|
| `SQL_DATABASE_URL` | `.env` = `"set-in-secrets"`<br>Secrets = actual value | ✅ | PostgreSQL connection string |
| `POSTGRES_HOST` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_PORT` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_DB` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_USER` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `POSTGRES_PASSWORD` | `.env` (blank) | ❌ | Docker-specific, not needed |
| `DATABASE_URL` | `.env` (blank) | ❌ | Replit provides automatically |
| `PGHOST` / `PGUSER` / `PGPASSWORD` / `PGDATABASE` / `PGPORT` | (not in `.env`) | ❌ | Replit provides automatically with SQL database |
| `REPLIT_DB_URL` | Secrets Manager | ⚠️ | KV store URL (if using) |
| `BROKER_ADMIN_TOKEN` | Secrets Manager | ✅ | Admin authentication token |
| `VENICE_API_BASE_URL` | `.env` | ✅ | Must include `/api/v1` |
| `VENICE_API_KEY` | Secrets Manager | ✅ | Venice API key |
| `VENICE_PARENT_KEY` | Secrets Manager | ✅ | Venice parent key |
| `BASE_RPC_URL` | `.env` | ✅ | Base RPC endpoint |
| `BASE_CHAIN_ID` | `.env` | ✅ | Should be `8453` |
| `DIEM_EXACT_IN_FALLBACK_ENABLE` | `.env` or Secrets | ⚠️ | Enable exact-in fallback (default: `0`, recommended: `1` for production) |
| `DIEM_EXACT_IN_FALLBACK_MAX_USD` | `.env` or Secrets | ⚠️ | Max USD per fallback trade (default: `10.0`) |
| `DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY` | `.env` or Secrets | ⚠️ | Auto-enable fallback when bridge healthy (default: `0`, recommended: `1`) |
| `MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS` | `.env` | ⚠️ | Path engine timeout (default: `10.0`, max: `30.0`) |
| `RISK_MAX_SLIPPAGE_BPS` | `.env` or Secrets | ⚠️ | Max slippage in basis points (default: `50` = 0.5%) |
| `RISK_MAX_POOL_TAKE_BPS` | `.env` or Secrets | ⚠️ | Max pool impact in basis points (default: `10` = 0.1%) |
| `RISK_MAX_DIEM_TRADE_USD` | `.env` or Secrets | ⚠️ | Max notional per trade in USD (default: `10000.0`) |
| `RISK_MAX_DIEM_INVENTORY_USD` | `.env` or Secrets | ⚠️ | Max inventory cap in USD (default: `100000.0`) |
| `RISK_MAX_DIEM_TRADE_UNITS` | `.env` or Secrets | ⚠️ | Absolute max units per trade (default: `0` = disabled, supports scientific notation) |
| `RISK_MAX_STAKE_USD` | `.env` or Secrets | ⚠️ | Max stake in USD (default: `0.0` = disabled) |
| `RISK_UTIL_ALPHA` | `.env` or Secrets | ⚠️ | Utilization multiplier alpha (default: `0.5`) |
| `RISK_MAX_VOLATILITY_BPS` | `.env` or Secrets | ⚠️ | Max volatility cap in bps (default: `0` = disabled) |
| `DIEM_PREMIUM_THRESHOLD` | `.env` or Secrets | ⚠️ | Premium multiple to trigger mint/sell (default: `1.05`) |
| `DIEM_DISCOUNT_THRESHOLD` | `.env` or Secrets | ⚠️ | Discount multiple to trigger buy/burn (default: `0.0` = uses premium_threshold) |
| `LOG_LEVEL` | `.env` | ⚠️ | Log level: DEBUG, INFO, WARNING, ERROR (default: `INFO`) |
| `LOG_FORMAT` | `.env` or Secrets | ⚠️ | Log format: empty for text, `json` for structured (default: empty) |
| `LOG_DIR` | `.env` | ⚠️ | Log directory (default: `logs`) |
| `LOG_BASENAME` | `.env` | ⚠️ | Log file name (default: `runtime.log`) |
| `LOG_CAPTURE_CONSOLE` | `.env` | ⚠️ | Tee stdout/stderr to log file (default: `1`) |

## Pre-Deployment Checklist

- [ ] `.env` has `SQL_DATABASE_URL="set-in-secrets"`
- [ ] All `POSTGRES_*` vars are blank in `.env`
- [ ] `DATABASE_URL` is blank in `.env`
- [ ] `VENICE_API_BASE_URL` includes `/api/v1`
- [ ] `BASE_RPC_URL` and `BASE_CHAIN_ID` are set
- [ ] All required secrets set in Replit Secrets Manager
- [ ] SQL database added in Replit Database tool
- [ ] `validate_replit_env.py` passes with no critical issues
- [ ] Logging configured: `LOG_LEVEL=INFO` (or as needed)
- [ ] For production: consider `LOG_FORMAT=json` for structured logs

## Related Documentation

- [Replit SQL Database Docs](https://docs.replit.com/cloud-services/storage-and-databases/sql-database.md)
- [Replit Secrets Manager](https://docs.replit.com/replit-workspace/workspace-features/secrets.md)
- `docs/DEPLOYMENT_VALIDATION.md` - Validation overview
- `docs/VALIDATION_QUICK_REFERENCE.md` - Quick reference matrix
- `scripts/validate_replit_env.py` - Replit validation script
- `scripts/replit_prestart.sh` - Pre-deployment script

