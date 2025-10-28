# Unified Deployment Guide - Docker & Replit

## Overview

The Venice Capacity Broker uses a **layered configuration strategy** that works seamlessly across Docker and Replit environments:

- **`.env`** = Shared baseline configuration (git-ignored, created from template)
- **`docker/.env.local`** = Docker-specific overrides (DB URLs, Redis, etc.)
- **Replit Secrets** = Same variables as `.env` (platform provides infrastructure URLs)
- **`docker-compose.yml`** = Defines defaults with `${VAR:-default}` syntax

The startup script (`scripts/docker_start_broker.sh`) auto-detects the environment and applies the correct settings.

---

## Quick Start

### Docker Deployment

```bash
# 1. Copy the template and configure
cp config/broker-fixes.env.template .env
# Edit .env with your secrets (BROKER_ADMIN_TOKEN, VENICE_PARENT_KEY, ETH_PRIVATE_KEY)

# 2. Build and start
docker compose up -d --build

# 3. Verify quotes are working
curl "http://localhost:8000/v1/env" | jq '.features'
curl "http://localhost:8000/v1/quotes?units=0.1&asset=ETH"

# 4. Access the UI
# Open http://localhost:8000/buy.html
```

### Replit Deployment

```bash
# 1. Open Secrets panel (🔒 icon in left sidebar)

# 2. Add these REQUIRED secrets:
BROKER_ADMIN_TOKEN=<generate-strong-token>
VENICE_PARENT_KEY=vk_live_<your-venice-key>
ETH_PRIVATE_KEY=0x<your-wallet-private-key>
BASE_RPC_URL=https://mainnet.base.org

# 3. Add contract addresses (Base mainnet):
VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024
QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
TREASURY_ADDRESS=0xe6e24e8E6F3004D82F0C710f6Bb035af1bE730C1

# 4. Add DEX routers:
UNISWAP_V2_ROUTER_ADDRESS=0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24
UNISWAP_V3_ROUTER_ADDRESS=0x2626664c2603336e57b271c5c0b26f421741e481
UNISWAP_V3_QUOTER_ADDRESS=0x3d4e44eb1374240ce5f1b871ab261cd16335b76a
AERODROME_ROUTER_ADDRESS=0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43

# 5. Add pricing paths:
TRADE_PATH=0xf4d97f2da56e8c3098f3a8d538db630a2606a024@3000,0x4200000000000000000000000000000000000006@500,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
VVV_TRADE_PATH=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf@3000,0x4200000000000000000000000000000000000006@500,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

# 6. Enable quotes and performance settings:
QUOTES_ENABLED=1
PRICE_ENGINE=market
DEX_FIRST_QUOTE_LINGER_MS=60
DEX_AGGREGATE_TIMEOUT_SECONDS=1.2
DEX_PROVIDER_TIMEOUT_SECONDS=0.9
DEX_MAX_WORKERS=4
RPC_REQUEST_TIMEOUT_SECONDS=5

# 7. Set CORS for your deployment URL:
CORS_ENABLED=true
CORS_ALLOW_ORIGINS=https://<your-repl-id>.replit.dev

# 8. Configure Replit infrastructure:
# Use your Postgres Add-on URL:
SQL_DATABASE_URL=postgresql+psycopg2://<user>:<pass>@<host>:<port>/<db>
# Use Replit DB:
KV_URL=<your-replit-db-url>
REPLIT_DB_URL=<your-replit-db-url>
KV_API_TOKEN=<your-kv-token>

# 9. Click Run
```

---

## Configuration Reference

### Section 1: Core Services

#### Venice API (REQUIRED)
```bash
VENICE_API_BASE_URL=https://api.venice.ai/api/v1  # Must include /api/v1
VENICE_PARENT_KEY=vk_live_your_key_here          # For creating scoped sub-keys
```

#### Base Network (REQUIRED)
```bash
BASE_RPC_URL=https://mainnet.base.org
BASE_CHAIN_ID=8453
# Optional: Multiple RPCs for failover
# BASE_RPC_URLS=https://mainnet.base.org,https://base.llamarpc.com
```

#### Wallet (REQUIRED for on-chain ops)
```bash
# Option 1: ETH Account (dev/testing)
ETH_PRIVATE_KEY=0xYourPrivateKeyHere

# Option 2: CDP Smart Wallet (production)
WALLET_PROVIDER=smart_wallet
NETWORK_ID=base-mainnet
CDP_API_KEY_ID=your_cdp_key_id
CDP_API_KEY_SECRET=your_cdp_secret
CDP_WALLET_SECRET=your_wallet_secret
```

### Section 2: Quote & Purchase Features

```bash
# Enable front-end quote functionality
QUOTES_ENABLED=1
PURCHASES_ENABLED=1
PRICE_ENGINE=market                              # Use live market pricing
ACCEPT_ASSETS=ETH,USDC                           # Supported payment assets
TREASURY_ADDRESS=0xe6e24e8E6F3004D82F0C710f6Bb035af1bE730C1

# Pricing bounds
PRICE_ACCEPTED_MIN_UNITS=0.01
PRICE_ACCEPTED_MAX_UNITS=100000
PRICE_QUOTE_TTL_SECONDS=120

# Discounts (basis points: 500 = 5%)
PRICE_DISCOUNT_ETH_BPS=500
PRICE_DISCOUNT_USDC_BPS=500
```

### Section 3: Performance Optimization (NEW - Quote Speed)

These settings reduce quote latency from seconds to milliseconds:

```bash
# Early-exit after first good quote (reduces p99 significantly)
DEX_FIRST_QUOTE_LINGER_MS=60

# Timeout settings (balance speed vs. route exploration)
DEX_AGGREGATE_TIMEOUT_SECONDS=1.2       # Global timeout across all providers
DEX_PROVIDER_TIMEOUT_SECONDS=0.9        # Per-provider timeout
DEX_MAX_WORKERS=4                       # Concurrent provider queries

# RPC client timeout (avoid slow nodes)
RPC_REQUEST_TIMEOUT_SECONDS=5

# Circuit breaker (auto-skip failing providers)
DEX_CIRCUIT_FAILURES=3
DEX_CIRCUIT_COOL_OFF_SECONDS=60
```

### Section 4: Caching (Quote Speed)

```bash
# Price caching reduces redundant DEX calls
MARKETDATA_PRICE_CACHE_TTL_SECONDS=60
MARKETDATA_PRICE_CACHE_TTL_DIEM_SECONDS=30
MARKETDATA_PRICE_CACHE_TTL_USDC_SECONDS=300

# Route quote caching (aggressive for quote endpoint)
MARKETDATA_ROUTE_QUOTE_TTL_SECONDS=2.0

# API response cache (front-end /v1/market/prices)
BROKER_PRICES_TTL_SECONDS=60
```

### Section 5: Docker-Specific Settings

```bash
# Startup testing (non-blocking by default)
BROKER_TESTS_BACKGROUND=1               # Run tests in background
BROKER_SKIP_TESTS=0                     # Set to 1 to skip entirely
BROKER_TESTS_TIMEOUT_SECONDS=900

# Pool catalog refresh on startup
MARKET_POOLS_REFRESH=1
```

---

## Environment Detection Logic

The `scripts/docker_start_broker.sh` script automatically detects the environment:

```bash
# Docker detection
if [ -f /.dockerenv ]; then
  # Use redis://redis:6379/0
  # Run tests with Redis backing
fi

# Replit detection
if [ -n "${REPLIT_ENVIRONMENT}" ] || [ -n "${REPLIT_WORKSPACE_ID}" ]; then
  # Skip Redis tests
  # Use Replit DB for KV
fi
```

---

## Validation & Testing

### After Deployment

```bash
# 1. Check environment loaded correctly
curl http://localhost:8000/v1/env

# Expected output (abbreviated):
# {
#   "features": {
#     "quotes": true,
#     "purchases": true
#   },
#   "payments": {
#     "treasury_address": "0xe6e24e8..."
#   }
# }

# 2. Test quote endpoint
curl "http://localhost:8000/v1/quotes?units=0.1&asset=ETH"

# Expected: JSON with quoteId, unitPrice, totalPrice, etc.

# 3. Check market data
curl "http://localhost:8000/v1/market/prices?symbols=DIEM,ETH,USDC"

# Expected: {"DIEM": 148.5, "ETH": 4120.0, "USDC": 1.0}

# 4. Test front-end
# Open http://localhost:8000/buy.html
# Click "Get Quote" button
# Should show quote details in <2 seconds
```

### Performance Metrics

With the optimizations, you should see:

- **Quote p50**: 200-600ms (was 2-10s)
- **Quote p99**: <2s (was >30s)
- **API startup**: <10s (was >60s with blocking tests)
- **Front-end load**: <3s for market data

---

## Troubleshooting

### Quotes Return 400/500

```bash
# Check if market pricing dependencies are available
docker logs venice-broker-1 | grep -i "pricing\|quote\|features"

# Common issues:
# - QUOTE_TOKEN_ADDRESS not set → defaults break
# - DIEM_TOKEN_ADDRESS not set → pricing engine fails
# - No DEX providers configured → aggregator throws
# - PRICE_ENGINE=static but no PRICE_UNIT_ETH_WEI → error
```

**Fix**: Ensure all contract addresses and routers are set (see Section 1 of template)

### Quotes Slow (>5s)

```bash
# Check DEX timeouts and RPC latency
docker logs venice-broker-1 | grep -i "timeout\|latency\|circuit"

# Common issues:
# - RPC_REQUEST_TIMEOUT_SECONDS too high (10s+) → slow RPC nodes stall quotes
# - DEX_PROVIDER_TIMEOUT_SECONDS too high → waits for failing providers
# - DEX_FIRST_QUOTE_LINGER_MS too high → waits unnecessarily after good quote
```

**Fix**: Use the recommended values from Section 3

### Front-End Shows "Quotes Disabled"

```bash
# Check features flag
curl http://localhost:8000/v1/env | jq '.features.quotes'

# Should return: true
```

**Fix**: Set `QUOTES_ENABLED=1` in .env or Replit Secrets

### CORS Errors in Browser Console

```bash
# Check CORS settings
curl http://localhost:8000/v1/env | jq '.cors'
```

**Fix**: Set `CORS_ENABLED=true` and add your domain to `CORS_ALLOW_ORIGINS`

---

## Migration from Old Config

If you have an existing `local.json` or scattered env vars:

```bash
# Convert JSON to env format
jq -r 'to_entries|.[]|"\(.key)=\(.value)"' local.json > .env

# Then add performance settings from Section 3 above
cat config/broker-fixes.env.template >> .env
```

---

## Production Checklist

Before deploying to production:

- [ ] Strong `BROKER_ADMIN_TOKEN` (32+ chars, random)
- [ ] `BROKER_REQUIRE_ADMIN_TOKEN=true`
- [ ] CDP Smart Wallet configured (not ETH_PRIVATE_KEY)
- [ ] Multiple RPC URLs for resilience (`BASE_RPC_URLS`)
- [ ] SQL database with backups (`SQL_DATABASE_URL`)
- [ ] Redis for KV with persistence (`REDIS_URL`)
- [ ] CORS limited to known origins (not `*`)
- [ ] Rate limits tuned for expected traffic
- [ ] Monitoring enabled (LangSmith, OpenTelemetry)
- [ ] Test quote endpoint responds in <2s: `time curl "https://your-domain/v1/quotes?units=0.1&asset=ETH"`

---

## Performance Tuning Guide

### Quote Latency Budget

Target budget for `/v1/quotes?units=0.1&asset=ETH`:

| Component | Target | Config |
|-----------|--------|--------|
| Market data fetch | <100ms | `MARKETDATA_PRICE_CACHE_TTL_*` |
| DEX aggregation | <500ms | `DEX_AGGREGATE_TIMEOUT_SECONDS=1.2` |
| Pricing calculation | <50ms | (internal) |
| Response serialization | <50ms | (internal) |
| **Total p50** | **<700ms** | - |
| **Total p99** | **<2s** | - |

### Tuning Knobs

**For faster quotes (trade route quality for speed)**:
```bash
DEX_FIRST_QUOTE_LINGER_MS=40            # Exit immediately after first quote
DEX_AGGREGATE_TIMEOUT_SECONDS=0.8       # Hard cap at 800ms
DEX_PROVIDER_TIMEOUT_SECONDS=0.6        # Give up on slow providers quickly
```

**For better routes (trade speed for quality)**:
```bash
DEX_FIRST_QUOTE_LINGER_MS=120           # Wait longer for better quotes
DEX_AGGREGATE_TIMEOUT_SECONDS=2.0       # Allow more exploration time
DEX_PROVIDER_TIMEOUT_SECONDS=1.5        # Tolerate slower RPC responses
```

**For thin liquidity markets**:
```bash
DEX_AGGREGATE_TIMEOUT_SECONDS=3.0       # More time to find routes
MARKETDATA_ROUTE_QUOTE_TTL_SECONDS=5.0  # Cache routes longer
```

---

## Environment Variable Index

### Critical (Required for Quotes)

| Variable | Example | Purpose |
|----------|---------|---------|
| `QUOTES_ENABLED` | `1` | Enable `/v1/quotes` endpoint |
| `PRICE_ENGINE` | `market` | Use live DEX pricing |
| `DIEM_TOKEN_ADDRESS` | `0xf4d9...` | DIEM contract |
| `QUOTE_TOKEN_ADDRESS` | `0x8335...` | USDC contract |
| `TRADE_PATH` | `0xf4d9@3000,0x4200@500,0x8335` | DIEM→WETH→USDC route |
| `TREASURY_ADDRESS` | `0xe6e2...` | Payment destination |

### Performance (High Impact)

| Variable | Default | Impact |
|----------|---------|--------|
| `DEX_FIRST_QUOTE_LINGER_MS` | `60` | Early-exit latency |
| `DEX_AGGREGATE_TIMEOUT_SECONDS` | `1.2` | Total quote budget |
| `DEX_PROVIDER_TIMEOUT_SECONDS` | `0.9` | Per-provider limit |
| `RPC_REQUEST_TIMEOUT_SECONDS` | `5` | RPC client timeout |
| `BROKER_PRICES_TTL_SECONDS` | `60` | API cache duration |

### Startup (Docker Only)

| Variable | Default | Purpose |
|----------|---------|---------|
| `BROKER_TESTS_BACKGROUND` | `1` | Non-blocking tests |
| `BROKER_SKIP_TESTS` | `0` | Skip tests entirely |
| `MARKET_POOLS_REFRESH` | `1` | Backfill pool catalog |

---

## File Structure

```
venice/
├── .env                       # Shared baseline (git-ignored, copy from template)
├── .env.example               # Template with all variables
├── config/
│   └── broker-fixes.env.template  # Migration template (legacy→unified)
├── docker/
│   └── .env.local             # Docker-specific overrides (optional, git-ignored)
├── docker-compose.yml         # Loads: .env, .env.docker, docker/.env.local
└── scripts/
    └── docker_start_broker.sh # Auto-detects Docker vs Replit
```

---

## Common Patterns

### Docker Multi-Environment

```bash
# dev.env
QUOTES_ENABLED=1
PRICE_ENGINE=market
BROKER_SKIP_TESTS=1  # Fast iteration

# prod.env
QUOTES_ENABLED=1
PRICE_ENGINE=market
BROKER_TESTS_BACKGROUND=1
BROKER_REQUIRE_ADMIN_TOKEN=true

# Run:
docker compose --env-file dev.env up -d
# or
docker compose --env-file prod.env up -d --build
```

### Replit Secrets Organization

Group secrets by category in the Secrets panel UI:

```
[Core]
BROKER_ADMIN_TOKEN
VENICE_PARENT_KEY
ETH_PRIVATE_KEY

[Network]
BASE_RPC_URL
BASE_CHAIN_ID

[Contracts]
VVV_TOKEN_ADDRESS
DIEM_TOKEN_ADDRESS
...

[Performance]
DEX_FIRST_QUOTE_LINGER_MS
DEX_AGGREGATE_TIMEOUT_SECONDS
...
```

---

## Advanced: Multi-Region Deployment

For globally distributed deployments:

```bash
# Use region-specific RPC pools
# US East
BASE_RPC_URLS=https://base-us-east.example.com,https://mainnet.base.org

# EU West
BASE_RPC_URLS=https://base-eu-west.example.com,https://mainnet.base.org

# Tune RPC timeouts by region
# US: Lower latency
RPC_REQUEST_TIMEOUT_SECONDS=3
# EU/Asia: Higher latency tolerance
RPC_REQUEST_TIMEOUT_SECONDS=8
```

---

## References

- Main deployment guide: `docs/DEPLOYMENT.md`
- Environment setup: `docs/ENV_SETUP.md`
- Template with all fixes: `config/broker-fixes.env.template`
- Agent orchestrator config: `AGENTS.md`
