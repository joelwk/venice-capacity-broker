# DIEM Technical Guide

> **Audience:** Developers, Operators  
> **Related:** [Tokenomics Overview](venice-diem-tokenomics.md) · [Configuration](CONFIGURATION.md#diem-fair-value-model) · [Troubleshooting](TROUBLESHOOTING.md#diem-price-guard-blocks)

**Version:** 3.0 (Finite-Horizon PV with Adoption)  
**Status:** ✅ PRODUCTION

## Table of Contents

1. [Fair Value Model](#fair-value-model)
2. [Liquidity Considerations](#liquidity-considerations)
3. [Configuration](#configuration)
4. [Monitoring & Troubleshooting](#monitoring--troubleshooting)
5. [References](#references)

---

## Fair Value Model

### Executive Summary

This section specifies the production fair value model for DIEM tokens, using a finite-horizon present value calculation with adoption-based scaling, illiquidity discounts, and market-calibrated parameters. The model produces fair values in the $110-160 range that align with observed market behavior and DIEM's tokenomics.

### Ground-Truth Assumptions

From Venice tokenomics documentation:

1. **DIEM Utility:** $1/day of AI compute credit when staked; resets daily
2. **Minting:** Only VVV stakers can mint DIEM by locking sVVV
3. **Emissions:** While locked, stakers earn 80% of normal VVV emissions (20% opportunity cost)
4. **Supply Control:** Target float ≈ 38,000 DIEM; mint rate rises with supply
5. **Execution:** Price anchored by mint-cost floor and constrained by liquidity

### Fair Value Formula (Production-Ready)

#### Parameters

**Required:**
- `vvv_price`: Live VVV market price in USD
- `mint_rate`: sVVV tokens per DIEM (normalized to token units, typically 1.0)

**Optional (with defaults):**
- `emissions_penalty`: 0.20 (20% opportunity cost)
- `utilization_current`: Observed utilization ratio (0..1)
- `utilization_trend`: Utilization trend signal (0..1)
- `circulating_supply`: Current DIEM supply
- `target_supply`: 38,000 (target float)
- `discount_rate_apy`: 0.15 (15% annual discount rate)
- `growth_rate_apy`: 0.05 (5% expected growth)
- `historical_ratio`: Historical DIEM/VVV price ratio
- `horizon_days`: 365 (PV calculation horizon)
- `adoption_base`: 0.60 (baseline adoption when util unknown)
- `has_onchain_liquidity`: True if DEX pools exist
- `illiquidity_discount`: 0.80 (20% discount when no pools)

#### Calculation Steps

##### 1. Mint Cost Floor

```python
mint_cost = vvv_price × mint_rate
emissions_cost = mint_cost × emissions_penalty × EMISSIONS_APY
base_cost = mint_cost + emissions_cost
```

**Example:** VVV=$1.36, mint_rate=1.0
- mint_cost = $1.36
- emissions_cost = $1.36 × 0.20 × 0.10 = $0.027
- base_cost = **$1.39**

##### 2. Adoption/Utilization

```python
# Use observed utilization if available, else adoption baseline
if utilization_current or utilization_trend:
    adoption = average(utilization_current, utilization_trend)
else:
    adoption = adoption_base  # Default 0.60

adoption = clamp(adoption, MIN_ADOPTION, MAX_ADOPTION)  # 0.25-0.90
```

##### 3. Finite-Horizon PV

```python
# Calculate net discount rate
net_discount_apy = max(discount_rate_apy - growth_rate_apy, 0.05)

# Convert APY to daily rate
d_daily = (1 + net_discount_apy)^(1/365) - 1

# Finite-horizon annuity PV formula
daily_value = 1.0  # $1/day per tokenomics
pv_horizon = adoption × daily_value × (1 - (1 + d_daily)^(-horizon_days)) / d_daily
```

**Example:** adoption=0.60, horizon=365, net_discount=0.10
- d_daily ≈ 0.000261
- pv_horizon ≈ 0.60 × 1.0 × 218 = **$131**

##### 4. Scarcity Multiplier

```python
if circulating_supply and target_supply:
    supply_ratio = circulating_supply / target_supply
    if supply_ratio < 1.0:
        scarcity_multiplier = 1.0 + (1.0 - supply_ratio) × SCARCITY_WEIGHT
    else:
        scarcity_multiplier = 1.0 - (supply_ratio - 1.0) × (SCARCITY_WEIGHT / 2.0)
    scarcity_multiplier = clamp(scarcity_multiplier, SCARCITY_MIN, SCARCITY_MAX)
else:
    scarcity_multiplier = 1.0
```

**Example:** supply=19,000, target=38,000
- supply_ratio = 0.5
- scarcity_multiplier ≈ **1.15** (15% premium for scarcity)

##### 5. Demand Multiplier

```python
if historical_ratio > 0:
    demand_signal = (historical_ratio / mint_cost) - 1.0
    demand_multiplier = 1.0 + demand_signal × DEMAND_WEIGHT
    demand_multiplier = clamp(demand_multiplier, DEMAND_MIN, DEMAND_MAX)
else:
    demand_multiplier = 1.0
```

##### 6. Sentiment Adjustment

```python
sentiment_mult = 1.0 + (sentiment_score - 0.5) × SENTIMENT_WEIGHT
sentiment_mult = clamp(sentiment_mult, SENTIMENT_MIN, SENTIMENT_MAX)
```

##### 7. Blend and Apply Multipliers

```python
# Blend base cost and utility PV
if base_cost > pv_horizon:
    blended = base_cost  # Cost floor dominates
else:
    blended = (base_cost × COST_WEIGHT) + (pv_horizon × (1.0 - COST_WEIGHT))

# Apply multipliers
fair_value = blended × scarcity_multiplier × demand_multiplier × sentiment_mult
```

##### 8. Illiquidity Discount

```python
if not has_onchain_liquidity:
    fair_value = fair_value × illiquidity_discount  # 20% haircut when no DEX pools
```

### Default Parameters (Calibrated)

```python
# Constants
EMISSIONS_APY = 0.10         # VVV emissions rate
EMISSIONS_PENALTY = 0.20     # 20% opportunity cost while locked
TARGET_SUPPLY = 38000.0      # Target DIEM float
DISCOUNT_RATE_APY = 0.15     # 15% discount rate
GROWTH_RATE_APY = 0.05       # 5% expected growth
HORIZON_DAYS = 365           # 1-year horizon for PV
ADOPTION_BASE = 0.60         # Baseline 60% adoption

# Defaults
adoption_base = 0.60
horizon_days = 365
illiquidity_discount = 0.80   # 20% discount when no pools
```

### Expected Fair Value Ranges

#### Scenario 1: Current Market (No DEX Liquidity)

- VVV: $1.36
- Mint rate: 1.0
- Adoption: 60%
- Horizon: 365 days
- **Fair value: ~$105-115** (with illiquidity discount)

#### Scenario 2: With DEX Liquidity

- Same parameters but `has_onchain_liquidity=True`
- **Fair value: ~$130-145** (no illiquidity discount)

#### Scenario 3: High Adoption (70%+)

- Adoption: 75%
- **Fair value: ~$150-170**

#### Scenario 4: Longer Horizon (2 years)

- Horizon: 730 days
- **Fair value: ~$180-220**

#### Scenario 5: Supply Scarcity (50% of target)

- Supply: 19,000 / 38,000
- Scarcity multiplier: 1.15
- **Fair value: ~$150-180**

### Calibration Guide

#### When Fair Value is Too Low (< $80)

Check these parameters:

```bash
# Increase adoption baseline
DIEM_FV_ADOPTION_BASE=0.70

# Increase horizon
DIEM_FV_HORIZON_DAYS=540

# Reduce discount rate
DIEM_FV_DISCOUNT_APY=0.12
```

#### When Fair Value is Too High (> $250)

Check these parameters:

```bash
# Decrease adoption
DIEM_FV_ADOPTION_BASE=0.50

# Shorten horizon
DIEM_FV_HORIZON_DAYS=270

# Increase discount rate
DIEM_FV_DISCOUNT_APY=0.18
```

#### When Utilization is Consistently High (> 70%)

Model will auto-adjust via adoption factor:

```python
# Model naturally increases fair value when utilization rises
adoption = max(utilization_current, utilization_trend)  # Uses actual data
fair_value increases proportionally
```

#### When DEX Liquidity is Added

```bash
# Remove illiquidity discount
has_onchain_liquidity = True  # Auto-detected when reserve_cap > 1000
# Fair value increases by ~25% (1.0 / 0.80 discount removal)
```

---

## Liquidity Considerations

### Current Liquidity Status

**🔴 CRITICAL: Zero On-Chain DIEM Liquidity on Base DEXes**

As of deployment, DIEM has **no active liquidity pools** on Uniswap V2, Uniswap V3, or Aerodrome on Base. This creates several operational constraints:

### What's Working ✅

1. **Mint Rate Conversion** - Correctly normalizes 1e18 base units → 1.0 token units
2. **Fair Value Calculation** - Multi-factor model working correctly
3. **Premium Detection** - Identifies price deviations from fair value
4. **Price Sourcing** - Uses `bridge_vvv` method (infers DIEM price from VVV) when no DEX pools exist
5. **Quorum Approval** - Risk checks and voting working properly
6. **Guard Rails** - Volatility, slippage, and utilization guards all operational

### What's Blocked ❌

**NO EXECUTABLE TRADES**

```bash
# Direct DIEM→USDC pool
verify_trade_path(['DIEM', 'USDC'])
→ {'uniswap_v2': {'pair': None}, 'aerodrome_vol': {'pair': None}, 'uniswap_v3': {'pool': None}}

# Multi-hop DIEM→WETH→USDC
verify_trade_path(['DIEM', 'WETH', 'USDC'], [3000, 500])
→ All hops return None for pair/pool
```

**Impact:**
- `reserve_cap_units` returns near-zero (< 1000 base units)
- Aggregator's `best_quote()` returns `None` for any amount
- Trades blocked with `'reason': 'no_liquidity_preview'`

### Why Price Sources Differ

#### Token Watcher Shows $53 Price

External APIs (Etherscan/Basescan token stats) provide this price, not from DEX pools.

#### ArbiDiem Calculates $123 Price

This price is calculated via the **VVV bridge method**:
1. Get VVV price from DEX: $1.36
2. Apply fair value model multipliers
3. Calculate DIEM price: ~$123

**Both prices are from external sources or models, not from DIEM DEX pools!**

### Solutions & Workarounds

#### Option 1: Create On-Chain Liquidity (Best Long-Term) 🎯

**Action Required:**
```bash
# Deploy DIEM/USDC pool on Uniswap V2 or Aerodrome
# Recommended initial depth: $10k-50k each side

# Expected outcomes:
# - reserve_cap_units > 10,000,000,000,000,000,000 (10e18)
# - best_quote() returns valid swap routes
# - Arbitrage trades can execute
# - Fair value illiquidity discount removed (+25% fair value)
```

**Prerequisites:**
- DIEM tokens for liquidity provision
- Matching USDC or WETH liquidity
- Smart contract deployment (if needed)

#### Option 2: Use Finite-Horizon PV Model (✅ Implemented)

**Already Deployed:**
```python
# Finite-horizon annuity PV (not perpetuity)
pv_horizon = adoption × daily_value × (1 - (1 + d_daily)^(-horizon_days)) / d_daily

# Returns: 0.60 × 1.0 × 218 ≈ $131
```

This prevents extreme fair value estimates and provides reasonable pricing even without on-chain liquidity.

**Configuration:**
```bash
DIEM_FAIR_VALUE_HORIZON_DAYS=365  # PV horizon (days)
DIEM_FV_ADOPTION_BASE=0.60        # Adoption baseline (alias: DIEM_ADOPTION_BASE)
```

#### Option 3: Bypass Liquidity Guard for Bridge Pricing

**Current Behavior:**
```python
# Check if we have a valid price source even without on-chain liquidity
if price_health["source"] in ["aggregator", "bridge_vvv"] and price_health["confidence"] >= 0.80:
    # Allow pricing without liquidity for display/analysis
    # But still block trades until reserve_cap > threshold
```

**This enables:**
- Fair value calculations
- Premium/discount detection
- Decision rationale logging
- Dry-run simulations

**This blocks:**
- Live trade execution (requires actual DEX liquidity)

#### Option 4: Implement OTC/Limit Order System

**Future Enhancement:**
```
# Build off-chain order book for DIEM trades
# Match buyers/sellers outside DEX
# Settle via smart contract escrow

# Advantages:
# - Works without DEX liquidity
# - Better pricing for large trades
# - Revenue opportunity from order matching
```

### Recommended Action Plan

#### Immediate (Next 24 Hours)

1. **Monitor bridge pricing** - Current VVV-based DIEM pricing is operational
2. **Log arbitrage opportunities** - System will identify but not execute trades
3. **Validate fair value model** - Ensure reasonable estimates ($110-160 range)

#### Short-Term (Next Week)

1. **Deploy initial DIEM liquidity** - Target $10k-20k pool on Uniswap V2 or Aerodrome
2. **Test trade execution** - Verify `reserve_cap > 1000` and `best_quote()` returns valid routes
3. **Enable live arbitrage** - Once liquidity confirmed, allow `--enable-live` mode

#### Medium-Term (Next Month)

1. **Scale liquidity depth** - Increase to $50k-100k for lower slippage
2. **Add multiple venues** - Deploy to Uniswap V3 with concentrated liquidity
3. **Implement dynamic pricing** - Adjust fair value model based on actual trade data

---

## Configuration

### Environment Variables

```bash
# Fair value model parameters
DIEM_FV_ADOPTION_BASE=0.60        # Baseline adoption (0-1) (alias: DIEM_ADOPTION_BASE)
DIEM_FAIR_VALUE_HORIZON_DAYS=365  # PV calculation horizon (days)
DIEM_ILLIQUIDITY_DISCOUNT=0.80    # Discount when no DEX pools (20% haircut)

# Note: cost/scarcity weights are code constants in libs/pricing/diem.py.
```

### Implementation Status

**✅ Completed:**
- Finite-horizon PV model
- Adoption-based scaling
- Illiquidity discount
- Multi-factor blending
- Mint cost floor
- Scarcity multiplier

**⚠️ Pending:**
- On-chain liquidity deployment
- Historical price ratio tracking
- Sentiment integration
- Dynamic parameter tuning

**🔄 Ongoing:**
- Model calibration based on market feedback
- Parameter optimization
- Performance monitoring

---

## Monitoring & Troubleshooting

### Check Fair Value in Logs

```bash
# Watch for fair value calculations
tail -f runtime.log | grep "fair value"

# Should show format:
# Market px=123.49, fair=130.00 (vvv=1.36, mint_rate=1.0, util=0.00%, conf=85%)
```

### Utilization Signal Missing or Stuck at 0%

If `util=0.00%` persists across cycles, treat it as suspect until proven otherwise.


First confirm the Venice API config is correct.


`VENICE_API_BASE_URL` must include `/api/v1`.


`VENICE_VVV_UTIL_PATH` should be `/vvv/utilization` unless your deployment differs.


When the Venice utilization fetch fails, the Venice client logs the HTTP status and a short response snippet.


When the orchestrator cannot obtain live utilization, it uses the adoption baseline and logs `utilization_source=fallback`.


The fallback value comes from `DIEM_FV_ADOPTION_BASE` or `DIEM_ADOPTION_BASE`.


### Premium Metrics (Operator-Facing)

DIEM uses two premium ratios so spikes are interpretable.

`premiumFair` measures price versus the full fair value model.

`premiumMint` measures price versus the mint-cost floor.

Definitions:

- `premiumFair = priceUsd / fairValueUsd`
- `premiumMint = priceUsd / mintCostFloorUsd`

Enable diagnostics:

```bash
DIEM_PREMIUM_DIAGNOSTICS_ENABLE=1
```

Broker API snapshot:

```bash
# Returns current + attribution + history (best-effort).
curl -s http://localhost:8000/v1/market/diem/premium?lookback=10 | jq
```

Trust semantics:

`trustedPrice=true` means the DIEM price was valid and not clamped.

If the source is `external_reference`, then `trustedPrice` is only true when the orchestrator marks it `trusted_external=true`.

### Verify Components

```bash
# Test fair value calculation locally
uv run python -c "
from libs.pricing.diem import fair_value_per_diem
result = fair_value_per_diem(
    vvv_price=1.36,
    mint_rate=1.0,
    utilization_current=None,
    has_onchain_liquidity=False,
)
components = result.get('components') or {}
print(f'Fair value: ${result[\"fair_value\"]:.2f}')
print(f'Base cost: ${components.get(\"base_cost\", 0.0):.2f}')
print(f'PV horizon: ${components.get(\"pv_horizon\", 0.0):.2f}')
print(f'Adoption: {components.get(\"adoption\", 0.0):.1%}')
"

# Expected output: Fair value ~$105-115 (with illiquidity discount)
```

### Verify Liquidity Status

```bash
# Check DIEM liquidity on Base DEXes
uv run python apps/cli/main.py quotes:preview --units 100.0

# Should show:
# - reserve_cap_units: > 1000 (if liquidity exists)
# - best_route: DIEM -> ... -> USDC (if pools exist)
# - "no_liquidity_preview" (if no pools)
```

### Common Issues

#### Issue 1: Fair Value Too High (> $250)

**Diagnosis:**
```bash
# Check parameters
echo $DIEM_FAIR_VALUE_HORIZON_DAYS  # Should be 365, not 3650
echo $DIEM_FV_ADOPTION_BASE         # Alias: DIEM_ADOPTION_BASE
```

**Fix:** Adjust parameters per [Calibration Guide](#calibration-guide)

#### Issue 2: Fair Value Too Low (< $50)

**Diagnosis:**
```bash
# Check for illiquidity discount
# With no DEX pools, 20% discount is expected

# Verify VVV price is correct
uv run python apps/cli/main.py market:best-price:scan --start 1.0
```

**Fix:** Deploy DEX liquidity or adjust `DIEM_ILLIQUIDITY_DISCOUNT`

#### Issue 3: Trades Blocked Despite Fair Price

**Diagnosis:**
```bash
# Check liquidity preview
uv run python apps/cli/main.py quotes:preview --units 10.0

# Look for:
# - reserve_cap_units < 1000 → No liquidity
# - best_route: None → No DEX pools
```

**Fix:** Deploy DIEM liquidity per [Solutions](#solutions--workarounds)

---

## References

### Documentation
- [Tokenomics Overview](venice-diem-tokenomics.md) - High-level DIEM concepts
- [Configuration Guide](CONFIGURATION.md) - Environment setup
- [Troubleshooting](TROUBLESHOOTING.md) - Common issues
- [Architecture](ARCHITECTURE.md) - System design

### Key Files
- `libs/pricing/diem.py` - Fair value model implementation
- `services/marketdata/provider.py` - Price sourcing and aggregation
- `libs/dex/aggregator.py` - DEX routing and liquidity checks
- `agents/arbi_diem/agent.py` - Arbitrage decision logic

### External Resources
- Venice Blog: [VVV and DIEM Tokenomics](https://venice.ai/blog)
- Base DEXes: [Uniswap V2](https://uniswap.org), [Aerodrome](https://aerodrome.finance)

---

## Version History

### v3.0 (November 10, 2025) - ✅ Current

- **Major:** Implemented finite-horizon PV model (replaces perpetuity)
- **Major:** Added adoption-based scaling
- **Major:** Added illiquidity discount (20% when no DEX pools)
- **Fix:** Mint rate conversion (1e18 base units → 1.0 tokens)
- **Enhancement:** Multi-factor blending (cost floor + utility PV)
- **Result:** Fair values now $110-160 range (vs $7-30 perpetuity bug)

### v2.0 (November 9, 2025) - Deprecated

- Used perpetuity NPV ($7,300) with sqrt dampening
- Produced fair values $7-45 (too low)
- Mint rate conversion bug (not normalized)

### v1.0 (October 2025) - Deprecated

- Simple cost-plus model
- No utility PV component
- Fair values ~$2-5

---

## Key Insights

1. **Finite-horizon PV is critical** - Perpetuity models over-estimate long-term value
2. **Adoption matters** - 60% baseline reflects realistic usage vs 100% theoretical max
3. **Liquidity discount needed** - 20% haircut when no DEX pools reflects execution risk
4. **Cost floor is safety net** - Ensures fair value never drops below mint cost
5. **Model is composable** - Easy to add/remove factors for calibration

---

## See Also

- [Operations Guide](OPERATIONS.md) - Daily monitoring checklist
- [DIEM Service](../services/diem/manager.py) - Mint/burn operations
- [ArbiDiem Agent](../agents/arbi_diem/agent.py) - Arbitrage logic
- [Risk Policy](../services/risk/policy.py) - Risk parameters

---

**Summary:** This technical guide consolidates the DIEM fair value model specification with liquidity analysis and operational guidance. Use the fair value model for pricing, follow the liquidity solutions for enabling trades, and monitor via logs and CLI commands.
