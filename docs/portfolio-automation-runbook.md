# Portfolio-Aware Arbitrage, Auto-Staking, and Pricing Automation

## Overview

This document describes the implementation of portfolio-aware arbitrage, automated profit recycling to staking, dynamic broker pricing, and AI Treasurer automation with ReAct execution hooks.

## Environment Variables

### Portfolio Inventory

- `RISK_ENABLE_PORTFOLIO_CAP=1` - Enable portfolio-aware sizing for ArbiDiem
- `RISK_MAX_USDC_TRADE_PCT=0.35` - Maximum percentage of USDC inventory to trade per cycle
- `RISK_MIN_USDC_RESERVE_USD=100.0` - Minimum USDC reserve to maintain
- `RISK_MAX_VVV_UNLOCK_PCT=0.25` - Maximum percentage of VVV stake to unlock per cycle

### Profit Recycling

- `TREASURER_ENABLE_AUTOMATION=0|1` - Enable automated profit recycling (default: 0)
- `TREASURER_MIN_ACTION_USD=10.0` - Minimum USD value for automated actions
- `TREASURER_MAX_ACTIONS_PER_CYCLE=1` - Maximum automated actions per cycle

### Broker Pricing

- `BROKER_UTIL_TARGET=0.65` - Target utilization ratio for pricing adjustments
- `BROKER_PRICE_STEP_BPS=50` - Step size in basis points for price adjustments
- `BROKER_DISCOUNT_MAX_BPS=500` - Maximum discount in basis points
- `BROKER_HYSTERESIS_WINDOW=0.05` - Hysteresis window to prevent oscillation
- `BROKER_UTIL_SURGE_THRESHOLD=0.85` - Utilization threshold for surge pricing
- `BROKER_UTIL_RELAX_THRESHOLD=0.40` - Utilization threshold for discount pricing
- `BROKER_BASE_PRICE_USD=1.0` - Base price per DIEM in USD
- `BROKER_SURGE_MULTIPLIER=2.0` - Maximum surge multiplier

### StakeMaster

- `STAKEMASTER_MIN_STAKE_USD=10.0` - Minimum USD value for staking operations
- `STAKEMASTER_PRIORITY_FEE_WEI` - EIP-1559 priority fee override
- `STAKEMASTER_PRIORITY_FEE_BUMP_MULT` - Multiplier for priority fee bumps
- `STAKEMASTER_PRIORITY_FEE_MIN_WEI` - Minimum priority fee
- `STAKEMASTER_STAKE_GAS_LIMIT` - Gas limit override for stake transactions

### Venice API

- `VENICE_API_BASE_URL=https://api.venice.ai/api/v1` - **MUST include `/api/v1`**
- `VENICE_API_KEY` - Venice API key for authentication

## CLI Commands

### Startup Probe

```bash
# Validate environment and Venice API config
uv run python apps/cli/main.py startup:probe

# Also check live operation requirements
uv run python apps/cli/main.py startup:probe --check-live

# Treat issues as warnings
uv run python apps/cli/main.py startup:probe --warn-only
```

### Orchestrator Loop

```bash
# Dry-run (default)
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 2

# Progressive live escalation (dry-run → live after healthy heartbeats)
uv run python apps/cli/main.py run:loop --progressive-live --max-cycles 5

# Enable live actions immediately
uv run python apps/cli/main.py run:loop --enable-live --max-cycles 3 --sleep 15
```

### Portfolio Inventory

```bash
# Preview portfolio snapshot (via startup probe)
uv run python apps/cli/main.py startup:probe --check-live
```

### Profit Recycling

Profit recycling is automated when `TREASURER_ENABLE_AUTOMATION=1` and quorum/reflex gates pass. Manual testing:

```bash
# Test recycle logic (dry-run)
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 1
```

### Broker Pricing

Dynamic pricing adjusts automatically based on utilization. View pricing in broker summaries:

```bash
# Run broker cycle
uv run python apps/cli/main.py broker:tenants:list
```

## Runbook

### Initial Setup

1. **Validate Venice API Configuration**
   ```bash
   uv run python apps/cli/main.py startup:probe
   ```
   Ensure `VENICE_API_BASE_URL` includes `/api/v1`.

2. **Configure Portfolio Caps**
   ```bash
   export RISK_ENABLE_PORTFOLIO_CAP=1
   export RISK_MAX_USDC_TRADE_PCT=0.35
   export RISK_MIN_USDC_RESERVE_USD=100.0
   ```

3. **Enable Profit Recycling (Optional)**
   ```bash
   export TREASURER_ENABLE_AUTOMATION=1
   export TREASURER_MIN_ACTION_USD=10.0
   ```

### Dry-Run Smoke Test

```bash
# Run 2 cycles in dry-run mode
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 2

# Verify telemetry includes:
# - portfolio inventory (inventoryUsd, perAssetUsd)
# - broker utilization
# - ArbiDiem rationale (desired_units, suggested_units, portfolioAdjustedUnits, tradeRoute)
```

### Progressive Live Mode

```bash
# Start with progressive escalation
uv run python apps/cli/main.py run:loop --progressive-live --max-cycles 10 --sleep 15

# Monitor logs/runtime.log for:
# - Healthy heartbeats (5 cycles default)
# - Live mode activation
# - Portfolio-aware sizing
# - Profit recycling events
```

### Full Live Mode

```bash
# Enable live actions immediately
uv run python apps/cli/main.py run:loop --enable-live --max-cycles 0 --sleep 15

# Monitor:
# - db/agent_memory.jsonl for cycle records
# - logs/runtime.log for decisions and executions
# - On-chain transactions via BaseScan
```

## Observability

### Memory Persistence

Cycle records are persisted to `db/agent_memory.jsonl` with:
- `portfolio.inventoryUsd` - Total portfolio USD value
- `portfolio.perAssetUsd` - Per-asset USD valuations
- `brokerUtilization` - Current broker utilization ratio
- `arbi.why.desired_units` - Desired trade size
- `arbi.why.suggested_units` - Risk-adjusted suggested size
- `arbi.why.portfolioAdjustedUnits` - Final portfolio-capped size
- `arbi.why.tradeRoute` - Selected DEX trade route

### Runtime Logs

`logs/runtime.log` includes:
- Portfolio snapshot summaries
- ArbiDiem sizing decisions with telemetry
- Profit recycling events (USDC→VVV swaps, staking)
- Broker pricing adjustments with hysteresis
- AI Treasurer execution hooks (gated by quorum/reflex)

### Metrics

- `/metrics` endpoint exposes Prometheus metrics for:
  - Portfolio inventory USD value
  - Broker utilization ratio
  - Trade sizes (desired, suggested, adjusted)
  - Profit recycling events

## Testing

### Unit Tests

```bash
# Portfolio inventory
uv run pytest tests/test_portfolio_snapshot.py -v

# ArbiDiem portfolio caps
uv run pytest tests/test_arbi_diem_portfolio_cap.py -v

# Profit recycling
uv run pytest tests/test_profit_recycling.py -v

# Broker pricing
uv run pytest tests/test_broker_pricing_loop.py -v

# Treasurer automation
uv run pytest tests/test_treasurer_automation.py -v
```

### Integration Tests

```bash
# Full orchestrator cycle with portfolio injection
uv run pytest tests/test_single_loop_orchestrator.py -v

# End-to-end dry-run
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 1
```

## Troubleshooting

### Venice API 404 Errors

1. **Check Base URL Format**
   ```bash
   echo $VENICE_API_BASE_URL
   # Should include /api/v1, e.g., https://api.venice.ai/api/v1
   ```

2. **Validate with Startup Probe**
   ```bash
   uv run python apps/cli/main.py startup:probe
   ```

3. **VeniceClient auto-normalizes** - If missing `/api/v1`, it will be added with a warning

### Portfolio Inventory Errors

- Ensure `BASE_RPC_URL` is set and accessible
- Check wallet provider configuration (`OWNER` or AgentKit wallet)
- Verify token addresses (`VVV_TOKEN_ADDRESS`, `DIEM_TOKEN_ADDRESS`, `QUOTE_TOKEN_ADDRESS`)

### Profit Recycling Not Triggering

- Verify `TREASURER_ENABLE_AUTOMATION=1`
- Check quorum approval (quorum must vote approve)
- Ensure reflex guardian allows execution (`REFLEX_*` envs)
- Minimum USD threshold (`TREASURER_MIN_ACTION_USD`)

### Broker Pricing Not Adjusting

- Check utilization thresholds (`BROKER_UTIL_SURGE_THRESHOLD`, `BROKER_UTIL_RELAX_THRESHOLD`)
- Verify hysteresis window (`BROKER_HYSTERESIS_WINDOW`)
- Inspect price history in broker summaries

## References

- `AGENTS.md` - Agent catalog and contracts
- `.cursor/rules/venice-api-config.mdc` - Venice API configuration guide
- `services/portfolio/inventory.py` - Portfolio inventory service
- `services/treasury/recycle.py` - Profit recycling service
- `agents/ai_treasurer/agent.py` - AI Treasurer with ReAct hooks

