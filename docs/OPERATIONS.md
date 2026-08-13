# Operations Guide

## Responsibilities

- Maintain orchestrator health, staking cadence, DIEM inventory, and tenant quotas.
- Keep live toggles safe; escalate via progressive-live before enable-live.
- Ensure Venice parent/sub-key hygiene and auditability.

## Dashboards & Logs

- HTTP: `/health`, `/metrics`, `/v1/env`, `/admin` (Broker UI).
- Logs: `logs/runtime.log` (quorum, reflex, price sanity, trades, summaries).
- Memory: `db/agent_memory.jsonl` (cycle records, portfolio, decisions).

### DIEM Premium Diagnostics

Enable DIEM premium diagnostics when investigating premium swings.

This exposes a broker endpoint that reports both premium ratios plus attribution.

```bash
DIEM_PREMIUM_DIAGNOSTICS_ENABLE=1
curl -s http://localhost:8000/v1/market/diem/premium?lookback=10 | jq
```

## Daily Checklist

1) Validate environment and Venice API config

```bash
uv run python apps/cli/main.py env:status
uv run python apps/cli/main.py startup:probe --check-live
```

`startup:probe` now fails fast when DIEM, VVV, or quote token addresses are missing or malformed.

It also sends a $2 DIEM quote probe and, with `DIEM_DEBUG_ROUTES=1`, logs `executable_quote_count` and provider status for the attempt.

2) Confirm StakeMaster heartbeat and rewards cadence

```bash
uv run python apps/cli/main.py run:stakemaster --enable-live --max-cycles 0
```

**Note:** Claim attempts are gated by reward USD value versus estimated gas USD cost.

Tune `STAKEMASTER_MIN_CLAIM_USD`, `STAKEMASTER_CLAIM_GAS_BUFFER_MULT`, and `STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS` to change cadence.

3) Review DIEM mint rate and fair value components

```bash
uv run python apps/cli/main.py diem:mint-rate --live
```

**Fair Value Calibration:**
- Expected range: $100-180 with VVV ~$1.36, 60% adoption, no on-chain liquidity
- If fair value consistently < $80 or > $250, review parameters:
  - `DIEM_ADOPTION_BASE` (default 0.60): Raise if utilization consistently > 70%
  - `DIEM_FAIR_VALUE_HORIZON_DAYS` (default 365): Extend to 540-730 for longer outlook
  - `DIEM_DISCOUNT_RATE_APY` (default 0.15): Lower to 0.10-0.12 for higher valuation
  - `DIEM_ILLIQUIDITY_DISCOUNT` (default 0.80): Set to 1.0 when DEX liquidity is added

4) Review utilization and DIEM buffer (target ~1.5× daily)

- Check `/metrics` and latest `agent_memory.jsonl` entries.

## Wallet Funding Requirements

### Minimum Balances for Trading

| Asset | Minimum | Recommended | Purpose |
| --- | --- | --- | --- |
| USDC | $1.00 | $10.00 | ArbiDiem buy/burn trades |
| ETH | 0.0005 | 0.001 | Gas for transactions |
| VVV | 0.25 | 1.0 | Gas buffer, staking dust |

### Diagnosing "insufficient_balance" Holds

When logs show `Buy/burn skipped: insufficient USDC balance for minimum trade`:

1) Check wallet balance

```
uv run python apps/cli/main.py wallet:balances
```

2) Verify USDC address

```
echo $USDC_TOKEN_ADDRESS
```

3) Fund wallet on Base network with USDC at address

```
uv run python - <<'PY'
from libs.agentkit_ext.agentkit_wallet import get_address
print("Deposit USDC to:", get_address())
PY
```

### Adjusting Trade Thresholds (testing only)

Lower only for test environments; keep production defaults unless risk-approved.

```
# Minimum trade size (default $1.00)
export ARBI_DIEM_MIN_TRADE_USD=0.50

# Low balance warning threshold (default $5.00)
export PORTFOLIO_USDC_LOW_BALANCE_USD=2.0
```

### DEX allowance + tiny sell verification

Use this sequence when validating UniswapV3 approvals and composite sell execution after a deploy.

1) Confirm VVV allowance to the UniswapV3 router.

If allowance is `0`, the first live trade will submit an approval automatically and then retry the UniswapV3 leg once.

```bash
uv run python - <<'PY'
import os
from web3 import Web3
from libs.agentkit_ext.agentkit_wallet import get_address
from libs.agentkit_ext.web3_utils import get_contract, get_web3

vvv = os.environ["VVV_TOKEN_ADDRESS"]
router = os.environ["UNISWAP_V3_ROUTER_ADDRESS"]
owner = get_address()
w3 = get_web3()
erc20 = get_contract(w3, Web3.to_checksum_address(vvv), "erc20.json")
allowance = erc20.functions.allowance(
    Web3.to_checksum_address(owner),
    Web3.to_checksum_address(router),
).call()
print("VVV allowance to UniswapV3 router:", int(allowance))
PY
```

2) Execute a tiny DIEM sell dry-run (quotes only).

```bash
# Example for 18-decimal DIEM: 0.001 DIEM
uv run python apps/cli/main.py quotes:compare --amount 1000000000000000
```

3) Execute a tiny DIEM sell live (when safe), then verify tx ordering on BaseScan.

You should see an approval transaction (if needed) before the swap transaction(s).

```bash
uv run python - <<'PY'
import uuid
from libs.dex.providers import build_aggregator_from_env
from services.diem.client import DIEMService
from services.marketdata.provider import MarketDataProvider

corr_id = str(uuid.uuid4())
amount_in = 1000000000000000  # 0.001 DIEM when DIEM_DECIMALS=18
svc = DIEMService(build_aggregator_from_env(), market_data=MarketDataProvider())
res = svc.trade(side="sell", amount=amount_in, slippage_bps=50, corr_id=corr_id)
print("correlation_id:", corr_id)
print(res)
PY
```

## Safety Controls

- `AGENTS_PAUSED=true` — immediate stop without API downtime.
- Quorum and Reflex guardrails: `QUORUM_ENABLE`, `REFLEX_MAX_VOL_BPS`, `REFLEX_MAX_PRICE_DRAWDOWN`, `REFLEX_REQUIRE_ACTIVE_STAKE`.
- Price sanity: `MARKETDATA_PRICE_SANITY_MAX_DRIFT` (use only when diagnosing drifts).

## Tenant & Key Management (CapacityBroker)

- Issue scoped sub-keys with `consumptionLimit` and `expiresAt`.
- Rotate/revoke on anomaly; store issuance audit.

```bash
uv run python apps/cli/main.py broker:tenants:list
uv run python apps/cli/main.py broker:venice:subkey --label "debug" --diem 100 --expires-at 2026-01-17T00:00:00Z
uv run python apps/cli/main.py venice:keys:cleanup --prefix T1 --dry-run
```

## Revenue Activation SOP

- Broker tenants (immediate): onboard tenants and monitor utilization-driven pricing.

```bash
uv run python apps/cli/main.py broker:tenants:create \
  --tenant production_client_001 \
  --quota 10000 \
  --tier premium
```

- Arbitrage (passive): system mints/sells when premium ≥ `DIEM_PREMIUM_THRESHOLD`; buys/burns at discount ≤ `DIEM_DISCOUNT_THRESHOLD`.
- Compounding: lower claim thresholds to increase staking cadence respecting costs.

## AI Treasurer Automation

Enable automated profit recycling and dynamic pricing:

```bash
# Set in environment
TREASURER_ENABLE_AUTOMATION=1
TREASURER_MIN_ACTION_USD=25.0

# Requires live mode with portfolio inventory
uv run python apps/cli/main.py run:loop --progressive-live
```

Monitor treasurer actions in logs:

```bash
docker logs venice-orchestrator-1 | grep -i treasurer
```

## Runbook (dry → progressive-live → live)

### Staged Test Matrix

**Stage (a): Pure dry-run validation**

```bash
# Short dry-run to validate configuration
uv run python apps/cli/main.py run:loop --dry-run --sleep 15 --max-cycles 3

# Verify logs show:
# - StakeMaster: status=ok, live=False, heartbeat.sent=True
# - ArbiDiem: dry_run=True, action signals but no execution
# - Progressive: requested=False (or True if STAKEMASTER_PROGRESSIVE_ENABLE=1), live=False
```

**Stage (b): Progressive-live with low threshold (testing)**

```bash
# Set low threshold for faster validation (default is 5 cycles)
export STAKEMASTER_PROGRESSIVE_CYCLES=2

# Run with progressive-live enabled
uv run python apps/cli/main.py run:loop --progressive-live --enable-live --sleep 15 --max-cycles 0

# Monitor logs for progressive state transitions:
# - Look for "progressive state: counter incremented" messages
# - After threshold cycles, expect "progressive live enabled"
# - Subsequent cycles should show StakeMaster with live=True and actual claim attempts

# Inspect recent cycles:
uv run python apps/cli/main.py orchestrator:cycles --limit 10
```

**Stage (c): Full live mode with production thresholds**

```bash
# Reset to production threshold (or use default 5)
export STAKEMASTER_PROGRESSIVE_CYCLES=5

# Run indefinitely with progressive-live
uv run python apps/cli/main.py run:loop --progressive-live --enable-live --sleep 15 --max-cycles 0

# Or enable immediate live (bypasses progressive gating)
uv run python apps/cli/main.py run:loop --enable-live --sleep 15 --max-cycles 0
```

### Progressive-Live Behavior

**How it works:**

1. Progressive-live requires consecutive healthy StakeMaster heartbeats (default: 5 cycles).
2. Each cycle increments the counter if:
   - StakeMaster status is "ok"
   - Heartbeat was sent successfully OR heartbeat was not forced
   - Special bypass: forced heartbeat with `venice_client_unavailable` + `STAKEMASTER_PROGRESSIVE_ALLOW_NO_HEARTBEAT=1`
3. Counter resets to 0 if any cycle fails the above conditions.
4. Once counter >= threshold, progressive state flips `live=True` and StakeMaster begins claiming rewards.

**Expected log progression:**

```
Cycle 1: progressive state: counter incremented counter=1 threshold=5
Cycle 2: progressive state: counter incremented counter=2 threshold=5
Cycle 3: progressive state: counter incremented counter=3 threshold=5
Cycle 4: progressive state: counter incremented counter=4 threshold=5
Cycle 5: progressive state: counter incremented counter=5 threshold=5
Cycle 5: progressive live enabled threshold=5 counter=5 enabled_at=<timestamp>
Cycle 6: StakeMaster: live=True, claim.attempted=True, claim.executed=True (if rewards available)
```

**Troubleshooting progressive-live:**

- If counter never increments: Check `stake.heartbeat.sent` and `stake.heartbeat.error` in logs
- If counter resets: Review heartbeat errors; may indicate Venice API connectivity issues
- To inspect progressive state: `uv run python apps/cli/main.py orchestrator:cycles --limit 20`

**Environment variables:**

- `STAKEMASTER_PROGRESSIVE_ENABLE=1` (default: enabled) - Controls whether progressive mode is active
- `STAKEMASTER_PROGRESSIVE_CYCLES=5` (default: 5) - Number of healthy cycles required
- `STAKEMASTER_PROGRESSIVE_ALLOW_NO_HEARTBEAT=0` (default: disabled) - Allow bypass for venice_client_unavailable errors

## Incident Runbook

- Venice API 404
  - Ensure `VENICE_API_BASE_URL` includes `/api/v1`.
  - `uv run python apps/cli/main.py startup:probe` and `venice:probe-openapi`.
  - Check heartbeat error categories in logs: `venice_404`, `venice_auth`, `venice_server`

- Progressive-live not flipping to live
  - Verify orchestrator is running long enough: use `--max-cycles 0` for indefinite runs
  - Check progressive counter: `uv run python apps/cli/main.py orchestrator:cycles --limit 10`
  - Review heartbeat logs: look for "progressive state: counter reset" messages
  - Temporarily lower threshold: `export STAKEMASTER_PROGRESSIVE_CYCLES=1` for testing

- DEX route gaps / liquidity
  - `uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC`.
  - Adjust `TRADE_PATH`/routers; verify with quotes preview.
  - Check `logs/dex_diagnostics.jsonl` for quote failure patterns
  - Review ArbiDiem logs for `slippage_source`: `quote_failure` vs `market_depth`

- RPC instability / rate limiting
  - Switch `BASE_RPC_URL`; increase `RPC_REQUEST_TIMEOUT_SECONDS`.
  - Monitor `logs/dex_diagnostics.jsonl` for 429 errors
  - Consider using private RPC endpoint or more generous rate limits

- Database connection failures
  - Verify `SQL_DATABASE_URL` or `DATABASE_URL` is set correctly
  - Run migrations: `uv run alembic upgrade head`
  - Check startup probe: `uv run python apps/cli/main.py startup:probe`

- Key abuse or anomaly
  - Revoke sub-keys; rotate parent; raise limits only after root-cause.

## Reflex and Quorum Observability

### Verifying Reflex Configuration

ReflexGuardian logs effective configuration at startup. Look for:

```
ReflexGuardian initialized: max_vol_bps=100000.0, max_utilization=0.99, ...
```

**Quick validation**: Compare the first `single-loop cycle` log entry's `reflex.limits` block against your `REFLEX_*` env vars:

```bash
# View latest cycle
uv run python apps/cli/main.py orchestrator:cycles --limit 1

# Check reflex limits match env
grep -A 5 '"reflex"' logs/runtime.log | grep '"limits"'
```

If `reflex.limits.max_vol_bps` doesn't match `REFLEX_MAX_VOL_BPS`, check:
- Environment variables are set before process start
- No conflicting constructor overrides in code
- Process restarted after env changes

### Interpreting Quorum Status

Every cycle log includes `arbi.quorum.status`. Common values:

- **`approved`** / **`blocked`**: Quorum voted (requires trade signal + live mode). Check `breakdown` for per-model votes.
- **`not_invoked`**: Normal when ArbiDiem finds no profitable trade. Check `arbi.why.reason` (often `no_exact_out_preview`).
- **`skipped`** with `reason: reflex_guard`: Reflex halted before quorum. Check `reflex.reasons`.
- **`skipped`** with `reason: price_guard`: Price anomaly detected. Check `arbi.priceGuard`.
- **`disabled`**: `QUORUM_ENABLE=0` or quorum not configured.

**CLI inspection**:

```bash
# View recent cycles with quorum status
uv run python apps/cli/main.py orchestrator:cycles --limit 5

# Inspect latest quorum decision
uv run python apps/cli/main.py quorum:inspect

# Automated verification + alert (cron friendly)
uv run python apps/cli/main.py ops:verify:reflex-quorum --limit 3 --alert-threshold 3
```

**Troubleshooting missing quorum activity**:

If `quorum.status` is always `not_invoked`:
1. Check `arbi.signalDecision`: Should be `true` for quorum to vote
2. Check `arbi.why.reason`: Common reasons include `no_exact_out_preview`, `fair_value_below_threshold`
3. Verify DEX quotes are available: Check `logs/dex_diagnostics.jsonl`
4. Ensure `live_mode=True` (not dry-run) for quorum to execute

If `quorum.status` is always `skipped`:
1. Check `reflex.halt`: If `true`, review `reflex.reasons` and `reflex.limits`
2. Check `arbi.priceGuard.status`: Price guard may be blocking
3. Verify `AGENTS_PAUSED=false`

## References

- Configuration → `./CONFIGURATION.md`
- Deployment → `./DEPLOYMENT.md`
- Troubleshooting → `./TROUBLESHOOTING.md`
- Security & Keys → `./SECURITY_KEYS.md`
