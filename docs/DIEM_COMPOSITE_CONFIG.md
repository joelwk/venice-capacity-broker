# DIEM Composite Route Configuration Guide

This guide covers the new configuration options introduced for composite exact-out routing and ArbiDiem buy/burn fallback behavior.

## New Environment Variables

### Core Fallback Configuration

**`DIEM_EXACT_IN_FALLBACK_ENABLE`** (default: `0`)
- Enable exact-in fallback when composite exact-out previews fail
- Set to `1`, `true`, `yes`, or `on` to enable
- **Recommended for production**: `1` (enables buy/burn when composite routing is flaky)

**`DIEM_EXACT_IN_FALLBACK_MAX_USD`** (default: `10.0`)
- Maximum USD value per fallback trade
- Only small trades use fallback to limit risk exposure
- **Recommended for production**: `10.0` - `25.0` (start conservative, increase after monitoring)
- **Recommended for staging**: `25.0` (allows more testing)

**`DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS`** (default: uses `RISK_MAX_SLIPPAGE_BPS`)
- Maximum slippage allowed for fallback trades (in basis points)
- If unset, inherits from `RISK_MAX_SLIPPAGE_BPS` (typically 50 bps)
- **Recommended**: Leave unset to inherit risk policy, or set explicitly to `50` - `100` for composite routes

### Auto-Fallback Mode

**`DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY`** (default: `0`)
- Automatically enable exact-in fallback when:
  - DIEM price source is `bridge_vvv` or `path_engine` (healthy)
  - DIEM/VVV reserves are sane
  - Requested notional is below `DIEM_EXACT_IN_FALLBACK_MAX_USD`
- Set to `1`, `true`, `yes`, or `on` to enable
- **Recommended for production**: `1` (allows buy/burn when bridge pricing is trusted but composite exact-out fails)
- **Recommended for staging**: `1` (enables more test coverage)

### Bridge Fallback Guardrails (Live Mode Safety)

**`DIEM_BRIDGE_LIVE_FALLBACK_ENABLE`** (default: `0`)
- Enable bridge_vvv price fallback in live mode when DEX quotes fail
- **⚠️ WARNING**: Only enable for small trades with tight monitoring
- When enabled, allows execution using bridge_vvv price when all DEX providers fail
- **Default**: `0` (disabled) - prevents execution without valid DEX quotes
- **Recommended for production**: `0` (keep disabled unless explicitly needed for small probes)
- **Recommended for staging**: `0` initially, enable only for controlled testing

**`DIEM_BRIDGE_LIVE_FALLBACK_MAX_USD`** (default: `5.0`)
- Maximum USD value per trade when using bridge fallback in live mode
- Only applies when `DIEM_BRIDGE_LIVE_FALLBACK_ENABLE=1`
- **Recommended**: `5.0` (minimum realistic trade size) or higher as needed
- **Note**: At current DIEM prices (~$128-130), $5 USD ≈ 0.04 DIEM units

**`DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS`** (default: `50.0`)
- Assumed slippage in basis points when using bridge fallback
- Used for policy checks when bridge price is used instead of DEX quotes
- **Recommended**: `50.0` (conservative assumption)

**Hard Guard**: The system automatically blocks execution when:
- All DEX providers return zero quotes (diagnostics show all failures)
- Bridge fallback is disabled (`DIEM_BRIDGE_LIVE_FALLBACK_ENABLE=0`) in live mode
- This prevents execution attempts that would revert on-chain

### Analytic Preview (Advanced)

**`DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE`** (default: `0`)
- Enable analytic reserve-based preview for DIEM/VVV leg when router quotes fail
- Uses Uniswap V2 formula on reserves for preview-only (never used for execution)
- Set to `1`, `true`, `yes`, or `on` to enable
- **Recommended for production**: `0` initially (enable only if composite exact-out remains flaky after other fixes)
- **Recommended for staging**: `1` (helps diagnose routing issues)

### Path Engine Timeout

**`MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS`** (default: `10.0`)
- Maximum seconds to wait for path engine quotes before falling back to bridge pricing
- Capped at 30.0 seconds maximum
- Standardizes to 10.0s when unset so dry runs and live runs share the same budget
- **Recommended for production**: `10.0` (default) with higher values only when RPC latency is extreme
- **Recommended for staging**: `10.0` (default) or lower if running on constrained hosts
- **`PATH_ENGINE_ROUTE_WORKERS`** (default: `2`) controls parallel route fan-out; keep small to avoid RPC thrash

## Docker Environment Configuration

Add to your `.env.docker` or `docker/.env.local`:

```bash
# DIEM Composite Route Fallback (Production)
DIEM_EXACT_IN_FALLBACK_ENABLE=1
DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0
DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS=50
DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE=0

# Path Engine Timeout (default: 10.0s, max: 30.0s)
# Use 10.0s for production; lower values (2.5-5.0s) only for constrained hosts
MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS=10.0

# Path Engine Provider Management (optional - defaults from config/default.yml)
# PATH_ENGINE_ROUTE_CACHE_TTL_SECONDS=60.0  # Route cache TTL (5-600s)
# PATH_ENGINE_PROVIDER_TIMEOUT_THRESHOLD=3  # Consecutive timeouts before blocklisting
# PATH_ENGINE_PROVIDER_BACKOFF_SECONDS=180.0  # Backoff duration for blocked providers (min 5s)
# PATH_ENGINE_MIN_ROUTE_BUDGET_SECONDS=0.35  # Min time budget per route (0.05-5.0s)
# PATH_ENGINE_SOFT_TIMEOUT_MARGIN_SECONDS=0.75  # Buffer subtracted from timeout (0.05-5.0s)

# Debug (disable in production unless troubleshooting)
DIEM_DEBUG_ROUTES=0
MARKETDATA_DEBUG_SANITY=0
```

### Docker Compose Example

Update `docker-compose.yml` environment section or use `.env.docker`:

```yaml
services:
  broker:
    environment:
      # ... existing vars ...
      # DIEM Composite Fallback
      - DIEM_EXACT_IN_FALLBACK_ENABLE=1
      - DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0
      - DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
      # Path Engine Timeout (default: 10.0s from config/default.yml)
      # Override only if needed for constrained hosts
      # - MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS=10.0
```

## Replit Environment Configuration

Add to your Replit Secrets (`.replit` or Secrets tab):

```bash
# DIEM Composite Route Fallback (Production)
DIEM_EXACT_IN_FALLBACK_ENABLE=1
DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0
DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS=50
DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE=0

# Path Engine Timeout (default: 10.0s from config/default.yml)
# Replit may benefit from lower values (2.5-5.0s) due to resource constraints
# Override only if experiencing timeout issues
MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS=10.0

# Path Engine Provider Management (optional - defaults from config/default.yml)
# PATH_ENGINE_ROUTE_CACHE_TTL_SECONDS=60.0  # Route cache TTL (5-600s)
# PATH_ENGINE_PROVIDER_TIMEOUT_THRESHOLD=3  # Consecutive timeouts before blocklisting
# PATH_ENGINE_PROVIDER_BACKOFF_SECONDS=180.0  # Backoff duration for blocked providers (min 5s)
# PATH_ENGINE_MIN_ROUTE_BUDGET_SECONDS=0.35  # Min time budget per route (0.05-5.0s)
# PATH_ENGINE_SOFT_TIMEOUT_MARGIN_SECONDS=0.75  # Buffer subtracted from timeout (0.05-5.0s)
```

### Replit `.replit` File Example

```toml
[env]
DIEM_EXACT_IN_FALLBACK_ENABLE=1
DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0
DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
# Path Engine Timeout (default: 10.0s from config/default.yml)
# Override only if needed for constrained Replit resources
# MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS=10.0
```

## Staging/Development Configuration

For staging environments where you want more aggressive testing:

```bash
# Staging - More permissive for testing
DIEM_EXACT_IN_FALLBACK_ENABLE=1
DIEM_EXACT_IN_FALLBACK_MAX_USD=25.0
DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS=200
DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1
DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE=1  # Enable for diagnostics

# Debug enabled for staging
DIEM_DEBUG_ROUTES=1
MARKETDATA_DEBUG_SANITY=1
```

## Configuration Validation

After setting these variables, validate your configuration:

```bash
# Check DIEM pricing health
uv run python apps/cli/main.py market:diem-bridge-check

# Full startup probe (includes DIEM route health)
uv run python apps/cli/main.py startup:probe --check-live

# Test buy/burn path with fallback
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 1
```

## Monitoring & Rollout Strategy

### Phase 1: Diagnostics Only (Week 1)
- Set `DIEM_EXACT_IN_FALLBACK_ENABLE=0` (disabled)
- Set `DIEM_DEBUG_ROUTES=1` (enabled)
- Monitor logs for composite leg failures and DIEM/VVV bridge leg diagnostics
- Review `dex_composite_diem_bridge_fail_total` metrics

### Phase 2: Conservative Fallback (Week 2)
- Set `DIEM_EXACT_IN_FALLBACK_ENABLE=1`
- Set `DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0` (conservative)
- Set `DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY=1`
- Monitor fallback usage via `agent_decisions_total` with `action=buy_burn` and `venue=exact_in_fallback`

### Phase 3: Production Tuning (Week 3+)
- If fallback is working well, consider increasing `DIEM_EXACT_IN_FALLBACK_MAX_USD` to `15.0` - `25.0`
- Only enable `DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE=1` if composite exact-out remains problematic
- Disable debug flags (`DIEM_DEBUG_ROUTES=0`, `MARKETDATA_DEBUG_SANITY=0`)

## Troubleshooting

If buy/burn is still blocked:

1. **Check price health**:
   ```bash
   uv run python apps/cli/main.py market:diem-bridge-check
   ```
   Ensure `source` is `bridge_vvv` or `path_engine` and `valid` is `true`

2. **Check composite route status**:
   ```bash
   DIEM_DEBUG_ROUTES=1 uv run python apps/cli/main.py run:loop --dry-run
   ```
   Look for "Composite exact-out leg failure details" in logs

3. **Verify fallback is enabled**:
   ```bash
   uv run python apps/cli/main.py env:status | grep DIEM_EXACT_IN_FALLBACK
   ```

4. **Check reserves**:
   Ensure DIEM/VVV pair has healthy reserves (check via Etherscan or `services.marketdata.etherscan_verify`)

## Related Configuration

These settings work together with existing risk and DEX configuration:

- `RISK_MAX_SLIPPAGE_BPS` - Global slippage cap (fallback inherits if `DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS` unset)
- `RISK_MAX_POOL_TAKE_BPS` - Pool reserve cap (applies to fallback trades)
- `DEX_COMPOSITE_ENABLE` - Must be `1` (default) for composite routing to work
- `TRADE_PATH` - Should include DIEM bridge path (DIEM->VVV->USDC) for composite routing

## See Also

- `docs/CONFIGURATION.md` - Full configuration reference
- `docs/TROUBLESHOOTING.md` - Troubleshooting guide
- `docs/DIEM_TECHNICAL_GUIDE.md` - DIEM routing technical details

