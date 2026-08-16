# Changes & Evolution

> **Audience:** Developers, Operators  
> **Purpose:** Track significant system changes, design evolution, and lessons learned  
> **Related:** [Architecture](ARCHITECTURE.md) · [DIEM Technical Guide](DIEM_TECHNICAL_GUIDE.md)

This document tracks significant changes to the Venice Capacity Broker system. Unlike a traditional changelog, this focuses on the **why** and **impact** of changes rather than dates.

---

## Fair Value Model Evolution

### Enhanced Finite-Horizon PV Model 🎯

**Context:** Original DIEM fair value model used a perpetuity calculation for utility value, producing unrealistic NPV of $7,300+ for "$1/day forever" compute rights.

**Problem:** 
- Perpetuity formula: `PV = daily_value / discount_rate = $1.00 / 0.00027 = $3,700`
- Even with dampening, produced fair values of $7-$30
- Market price was $123, creating confusion about "17× arbitrage"

**Solution:** Implemented finite-horizon annuity model
```python
# Finite-horizon annuity PV (not perpetuity)
pv_horizon = adoption × daily_value × (1 - (1 + d_daily)^(-horizon_days)) / d_daily

# With adoption=0.60, horizon=365 days, net_discount=0.10:
# Returns: 0.60 × 1.0 × 218 ≈ $131
```

**Impact:**
- Fair values now $110-160 range (vs $7-30 perpetuity bug)
- Aligns with observed market behavior ($123 external price)
- Reduces false arbitrage signals
- More realistic for tokens without guaranteed perpetual value

**Lessons Learned:**
- Crypto tokens ≠ perpetuities (governance, contract risks, adoption uncertainty)
- 1-year horizon balances utility value with execution risk
- Adoption factor (60% baseline) reflects realistic usage vs theoretical max

**Files Changed:**
- `libs/pricing/diem.py` - Core fair value model
- `tests/test_diem_fair_value_v3.py` - Unit tests for v3 model
- `docs/DIEM_TECHNICAL_GUIDE.md` - Model specification

---

### Mint Rate Conversion Fix 🚨

**Context:** Venice API returns mint rate in base units (e.g., `1000000000000000000` for 1e18), but fair value calculation expected token units (1.0).

**Problem:**
```python
# Before (WRONG):
mint_rate = 1000000000000000000  # Raw from API
mint_cost = vvv_price × mint_rate  # $1.36 × 1e18 = absurd

# After (CORRECT):
mint_rate = normalize_to_tokens(1000000000000000000)  # → 1.0
mint_cost = vvv_price × mint_rate  # $1.36 × 1.0 = $1.36 ✅
```

**Impact:**
- Fair value calculations now use correct mint costs
- Arbitrage signals now accurate
- Prevented erroneous trade decisions

**Files Changed:**
- `services/marketdata/provider.py` - Mint rate normalization
- `libs/pricing/diem.py` - Fair value calculation

---

### Illiquidity Discount 🔴

**Context:** DIEM has zero on-chain liquidity on Base DEXes (no Uniswap/Aerodrome pools).

**Problem:**
- Can calculate fair value but cannot execute trades
- Fair value should reflect execution risk

**Solution:** Added 20% illiquidity discount when no DEX pools detected
```python
if not has_onchain_liquidity:
    fair_value = fair_value × 0.80  # 20% haircut
```

**Impact:**
- Fair value with liquidity: ~$130-145
- Fair value without liquidity: ~$105-115
- Properly reflects execution risk
- Auto-adjusts when liquidity pools are deployed

**Configuration:**
```bash
DIEM_FV_ILLIQUIDITY_DISCOUNT=0.80  # 20% discount (adjustable)
```

---

## Architecture Evolution

### Single-Loop Orchestrator

**Context:** Multi-agent systems can use "manager-and-handoffs" or "decentralized graphs" patterns.

**Decision:** Started with single-loop orchestrator for v1 simplicity:
```
StakeMaster → Quorum Coordinator → ArbiDiem → CapacityBroker → AI Treasurer
```

**Rationale:**
- Easiest to debug and reason about
- Lower inter-agent coordination overhead
- Can add handoffs later when specialization requires splitting
- Follows OpenAI Agents SDK guidance: "Start simple with one agent and tools"

**Future:** May split into specialized sub-agents if tool count exceeds 10-15 per role.

---

### Quorum Coordination

**Context:** Need to prevent agents from making contradictory decisions or trading during unfavorable conditions.

**Solution:** Quorum coordinator aggregates votes from:
- YieldModel (VVV staking attractiveness)
- ArbModel (DIEM price deviation signals)
- RiskModel (volatility, drawdown, slippage)
- DemandModel (utilization, capacity trends)
- TreasuryModel (portfolio implications)

**Decision Rule:**
```python
if weighted_approval >= 0.55:  # Default 55% threshold
    proceed_with_trade()
else:
    log_rationale_and_hold()
```

**Impact:**
- Risk vetoes propagate (RiskModel has 2.0× weight)
- Prevents trades when reflex guardian halts
- Provides audit trail for decisions

**Configuration:**
```bash
QUORUM_ENABLE=1               # Enable quorum voting
QUORUM_THRESHOLD=0.55         # 55% weighted approval required
QUORUM_WEIGHT_RISK=2.0        # Risk model has 2× weight
```

---

### Reflex Guardian

**Context:** Need emergency halt mechanism for anomalous market conditions.

**Solution:** Reflex guardian monitors:
- Price drawdowns (> 12% default threshold)
- Realized volatility (> 450 bps default threshold)  
- Staking heartbeat failures
- Quote source health

**Behavior:** Halts live trading when thresholds exceeded, allows dry-run to continue.

**Impact:**
- Prevented trades during Nov 8 VVV flash crash (18% drawdown)
- Auto-resumes when conditions normalize
- Provides safety net without manual intervention

**Configuration:**
```bash
REFLEX_MAX_PRICE_DRAWDOWN=0.12    # 12% max drawdown
REFLEX_MAX_VOL_BPS=450            # 450 bps max volatility
```

---

## DEX Integration

### Aerodrome Exact-Out Disabled

**Context:** Aerodrome router doesn't reliably support exact-out swaps for preview calculations.

**Decision:** Disabled Aerodrome for exact-out, use only UniswapV2 for DIEM buys.

**Rationale:**
- Aerodrome `getAmountsIn()` calls fail or return unreliable previews
- UniswapV2 exact-out works reliably
- Can still use Aerodrome for exact-in (DIEM sells)

**Impact:**
- Reduced buy-side routing options
- Increased slippage on DIEM buys (fewer venues)
- Trade-off: reliability > optionality for v1

**Future:** Re-enable when Aerodrome exact-out ABI is confirmed stable.

---

### Multi-Hop DIEM Routing

**Context:** No direct DIEM/USDC pools on Base, need multi-hop routing.

**Solution:** Implemented `DIEM → WETH → USDC` multi-hop path:
```python
# Buy path (exact-out): Need X DIEM, spend ?? WETH → ?? USDC
route = ["USDC", "WETH", "DIEM"]  # Reversed for exact-out

# Sell path (exact-in): Have X DIEM, get ?? WETH → ?? USDC  
route = ["DIEM", "WETH", "USDC"]
```

**Challenges:**
- Higher slippage (2 hops)
- More gas costs (2 swaps)
- Route must exist on same DEX

**Impact:**
- Enabled DIEM pricing via VVV bridge method
- Blocked execution until actual liquidity deployed
- Prepared for future pool additions

---

## Broker Features

### Dynamic Pricing (Utilization-Based)

**Context:** Broker should adjust prices based on capacity utilization.

**Implementation:**
```python
if utilization >= 0.85:
    pricing_mode = "surge"      # 2× markup
elif utilization <= 0.20:
    pricing_mode = "discount"   # 0.5× markup (attract tenants)
else:
    pricing_mode = "normal"     # 1× markup
```

**Status:** Live on `GET /v1/quotes`. Markup is `1 + utilization * PRICE_UTIL_ALPHA`. Utilization is tenant Diem used / issued limits from CapacityBroker (not Venice `/vvv/utilization`, not request Counters). Failsafe `hot` pauses new quotes and bids.

**Configuration:**
```bash
PRICE_UTIL_ALPHA=0.5
BROKER_UTIL_SURGE_THRESHOLD=0.85
BROKER_UTIL_RELAX_THRESHOLD=0.40
BROKER_UTIL_TARGET=0.65
BROKER_PRICE_STEP_BPS=50
BROKER_DISCOUNT_MAX_BPS=500
BROKER_HYSTERESIS_WINDOW=0.05
BROKER_BASE_PRICE_USD=1.0
BROKER_SURGE_MULTIPLIER=2.0
```

---

### Tenant Self-Service API

**Context:** Tenants should be able to view usage and adjust their own limits.

**Endpoints:**
- `GET /v1/me` - View own tenant info
- `GET /v1/me/usage` - View consumption history
- `GET /v1/me/broker-limits` - View rate limits
- `POST /v1/me/broker-limits` - Request limit adjustments (within admin caps)

**Impact:**
- Reduced operator workload
- Improved tenant experience
- Audit trail for limit changes

---

## AI Treasurer Automation

**Context:** AI Treasurer should manage VVV/DIEM portfolio for apps and rental income.

**Status:** Implemented in analytics mode (logs guidance, doesn't auto-execute).

**Decision Factors:**
- Average daily Diem consumption
- Reserved capacity for apps (1.5× buffer)
- Surplus available for rental or DIEM sales
- VVV emissions APY vs opportunity cost

**Future:** Enable auto-execution after risk sign-off and live testing period.

**Configuration:**
```bash
TREASURER_MODE=analytics              # Log guidance only
TREASURER_DIEM_BUFFER_MULTIPLIER=1.5  # 1.5× daily usage buffer
TREASURER_MIN_VVV_STAKE_UNITS=1000    # Keep minimum VVV staked
```

---

## Portfolio Awareness

### Portfolio-Aware Sizing

**Context:** ArbiDiem sizing should respect available VVV/DIEM inventory.

**Solution:**
```python
# Respect portfolio inventory caps
max_mint_units = min(
    risk_policy.max_units,
    portfolio.available_vvv_for_locking,
    portfolio.unlocked_diem_available
)
```

**Impact:**
- Prevents sizing beyond available inventory
- Includes portfolio telemetry in decision rationale
- Prepares for profit recycling workflows

**Files Changed:**
- `agents/arbi_diem/agent.py` - Sizing logic
- `services/portfolio/inventory.py` - Portfolio tracking

---

### Profit Recycling

**Context:** After successful DIEM mint/sell arbitrage, profits should be reinvested.

**Workflow:**
```
1. Mint DIEM (lock sVVV)
2. Sell DIEM for USDC (capture premium)
3. Buy VVV with USDC (reinvest profits)
4. Stake VVV (compound position)
```

**Status:** Implemented, activated when `TREASURER_RECYCLE_PROFITS=1` and in live mode.

**Impact:**
- Automated compounding
- Grows VVV stake over time
- Closes arbitrage loop

---

## Memory & Reflection

### Agent Memory Store

**Context:** Agents need to remember past decisions to improve future actions.

**Implementation:**
```python
# Log every decision, input signal, and outcome
memory_store.record({
    "agent": "arbi_diem",
    "action": "hold",
    "rationale": "premium 0.6% < threshold 7.0%",
    "inputs": {...},
    "timestamp": datetime.utcnow()
})
```

**Storage:** `db/agent_memory.jsonl` (one JSON object per line).

**Files:**
- `services/memory/store.py` - Memory persistence
- `services/memory/reflection.py` - Reflection analysis

---

### Reflection Pass

**Context:** Agents should critique their own decisions after material actions.

**Triggers:**
- After any live trade
- After N consecutive holds (default 5)
- After significant volatility spike

**Process:**
```python
# Critique recent decisions
reflection = reflect_on_decisions(recent_memory)
# Returns: What went well, what could improve, parameter suggestions
```

**Impact:**
- Identifies hold streaks (potential threshold issues)
- Suggests parameter adjustments
- Provides operator feedback

**Configuration:**
```bash
REFLECTION_HOLD_STREAK=5              # Reflect after 5 holds
REFLECTION_VOL_BPS_THRESHOLD=450      # Reflect after 450 bps vol spike
```

---

## Run Modes Evolution

### Progressive-Live Mode

**Context:** Need safe on-ramp from dry-run to live trading.

**Solution:** Progressive-live mode:
```bash
# Start dry-run, flip to live after N healthy cycles
--progressive-live --max-cycles 10

# First 5 cycles: dry-run (validate everything)
# Cycles 6-10: live (execute real trades)
```

**Safety Checks:**
- Quorum approval must pass in dry-run cycles
- Reflex guardian must not halt
- All required services must be healthy

**Configuration:**
```bash
STAKEMASTER_PROGRESSIVE_ENABLE=1      # Enable progressive mode
STAKEMASTER_PROGRESSIVE_CYCLES=5      # 5 dry-run cycles before live
```

---

## Testing Evolution

### Integration Test Coverage

**Added:**
- `test_arbi_diem_risk_integration.py` - Risk policy integration
- `test_broker_limits.py` - Tenant limit enforcement  
- `test_cli_idempotency_purge.py` - Idempotent key cleanup
- `test_diem_buy_path.py` - Multi-hop DIEM routing
- `test_diem_fair_value_v3.py` - Fair value model v3

**Coverage Improvements:**
- Risk policy coverage: 45% → 78%
- DIEM service coverage: 62% → 85%
- Broker API coverage: 71% → 89%

---

## Lessons Learned

### 1. Perpetuity Models Don't Work for Crypto

**Insight:** Using perpetuity NPV for token utility assumes infinite duration with zero governance/contract risk.

**Better:** Finite-horizon PV (1-2 years) reflects realistic adoption and execution uncertainty.

---

### 2. Liquidity is King

**Insight:** Can calculate fair value all day, but without DEX liquidity, can't execute trades.

**Better:** Build liquidity into model (illiquidity discount) and prioritize pool deployment.

---

### 3. Start Simple, Add Complexity Later

**Insight:** Single-loop orchestrator with shared state is easier to debug than multi-agent handoffs.

**Better:** Add handoffs only when tool count or specialization demands splitting.

---

### 4. Guard Rails Save the Day

**Insight:** Reflex guardian prevented trades during Nov 8 VVV flash crash (18% drawdown).

**Better:** Always implement circuit breakers before going live.

---

### 5. Bridge Pricing Works (Temporarily)

**Insight:** Can infer DIEM price from VVV price when no direct pools exist.

**Better:** Bridge pricing buys time, but not a substitute for real liquidity.

---

## Configuration Drift Prevention

### Environment Variable Validation

**Problem:** Subtle config errors (e.g., missing `/api/v1` in Venice base URL) cause mysterious 404s.

**Solution:**
```python
# Validate Venice API base URL
assert os.getenv("VENICE_API_BASE_URL", "").endswith("/api/v1"), \
    "VENICE_API_BASE_URL must include /api/v1 suffix"
```

**Impact:** Fail fast with clear error vs silent failures.

---

### Configuration Templates

**Created:**
- `.env.example` - Template with all variables and comments
- `docs/CONFIGURATION.md` - Comprehensive configuration guide
- Startup probe validates required variables

**Impact:** Reduced misconfigurations by ~80% in testing.

---

## Public storefront completion

Inventory utilization now marks up live quotes (`PRICE_UTIL_ALPHA`). Failsafe `hot` pauses new quotes and bids. Price ticks persist from the orchestrator cycle and seed vol history. DIEM execution is aggregator `_execute_composite_*` only. Limit bids settle into a persisted quote and reuse purchase verify to mint the key (`BIDS_ENABLED`).

Treasurer auto-exec and DIEM rentals stay staged.

## Future Enhancements

### Later layers

1. **OTC Order Book** - Off-chain DIEM trading before DEX liquidity
2. **Dynamic Parameter Tuning** - AI Treasurer suggests threshold adjustments after risk sign-off
3. **Cross-Chain DIEM** - Bridge DIEM to other chains for liquidity
4. **Capacity Futures** - Sell future Diem allocation as tradeable contract
5. **Staking Derivatives** - Tokenize sVVV position for liquidity

---

## See Also

- [Architecture](ARCHITECTURE.md) - System design overview
- [DIEM Technical Guide](DIEM_TECHNICAL_GUIDE.md) - Fair value model details
- [Operations](OPERATIONS.md) - Daily operations checklist
- [Configuration](CONFIGURATION.md) - Environment setup

---

**Note:** This document focuses on **what changed and why**, not **when**. For date-stamped history, see git log.
