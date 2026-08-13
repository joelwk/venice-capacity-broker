# DIEM Dynamic Route Discovery Guide

## Overview

The `discover_trade_paths` function in `services/marketdata/dynamic_paths.py` discovers trade routes by querying DexScreener API for token pairs. It can discover both direct (2-token) and multi-hop (3+ token) routes.

## How Multi-Hop Discovery Works

1. **Bridge Token Configuration**: The system uses `WETH_ADDRESS` and `VVV_TOKEN_ADDRESS` as default bridge tokens
2. **Discovery Process**:
   - Fetches pairs from DexScreener for DIEM, bridge tokens (VVV, WETH), and quote token (USDC)
   - Tries direct routes first (DIEM→USDC)
   - Then tries bridge routes (DIEM→VVV→USDC, DIEM→WETH→USDC)
3. **Route Limit**: Default `_MAX_ROUTE_COUNT = 4` routes total

## Why Multi-Hop Routes Might Not Be Discovered

### 1. DexScreener API Missing Pairs

**Check if pairs exist**:
```bash
# Check DIEM pairs (using CLI command - works on Windows/Linux/Mac)
uv run python apps/cli/main.py market:dexscreener:pairs 0xf4d97f2da56e8c3098f3a8d538db630a2606a024

# JSON output format
uv run python apps/cli/main.py market:dexscreener:pairs 0xf4d97f2da56e8c3098f3a8d538db630a2606a024 --json

# Check VVV pairs
uv run python apps/cli/main.py market:dexscreener:pairs 0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf

# Alternative: using curl + jq (requires jq installed)
curl "https://api.dexscreener.com/latest/dex/tokens/0xf4d97f2da56e8c3098f3a8d538db630a2606a024" | jq '.pairs[] | select(.chainId == "base") | {pairAddress, dexId, liquidity: .liquidity.usd, baseToken: .baseToken.address, quoteToken: .quoteToken.address}'
```

**If pairs are missing**:
- DexScreener may not have indexed the pairs yet
- Pairs might exist on-chain but not be tracked by DexScreener
- Use configured routes (env/canonical/bridge) as fallback

### 2. Liquidity Thresholds Too High

**Current thresholds**:
- `TRADE_PATH_DIRECT_MIN_LIQ_USD` (default: 5000) - for direct DIEM→USDC routes
- `TRADE_PATH_HOP_MIN_LIQ_USD` (default: 1500) - for bridge hop routes

**If liquidity is below thresholds**:
```bash
# Lower thresholds to discover routes with less liquidity
export TRADE_PATH_HOP_MIN_LIQ_USD=500  # Lower from 1500
export TRADE_PATH_DIRECT_MIN_LIQ_USD=2000  # Lower from 5000
```

### 3. Route Limit Reached

**Current limit**: `TRADE_PATH_DYNAMIC_MAX_ROUTES` (default: 4)

**If direct routes fill the limit first**:
```bash
# Increase route limit to allow more multi-hop routes
export TRADE_PATH_DYNAMIC_MAX_ROUTES=8
```

### 4. V3 Route Fee Requirements

**V3 routes require fees for both hops**:
- `_discover_v3_routes` requires both DIEM→VVV and VVV→USDC to have Uniswap V3 fee tiers
- If one hop is V2/Aerodrome (no fee), the V3 route won't be discovered

**Solution**: V2 routes (`_discover_v2_routes`) don't require fees and should discover DIEM→VVV→USDC if:
- DIEM→VVV pair exists (Aerodrome/UniswapV2)
- VVV→USDC pair exists (any DEX)
- Both meet liquidity thresholds

### 5. Bridge Token Not Configured

**Ensure VVV is in bridge tokens**:
```bash
# Check current config
echo $VVV_TOKEN_ADDRESS  # Should be 0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf

# Or explicitly set bridge addresses
export TRADE_PATH_BRIDGE_ADDRESSES=0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf,0x4200000000000000000000000000000000000006
```

## Debugging Discovery

### Enable Debug Logging

```bash
export DIEM_DEBUG_ROUTES=1
uv run python apps/cli/main.py market:trade-paths:validate --amount 1.0
```

Look for logs showing:
- `dynamic trade path: tokens=... fees=...` - shows discovered routes
- `dexscreener fetch failed` - indicates API issues
- Pairs cache hits/misses

### Check What Pairs Are Found

Add temporary debug logging to `services/marketdata/dynamic_paths.py`:

```python
# In discover_trade_paths, after pairs_cache is populated:
log.info("Pairs cache summary: DIEM=%d pairs, VVV=%d pairs, USDC=%d pairs",
    len(pairs_cache.get(diem.lower(), [])),
    len(pairs_cache.get(vvv.lower(), [])),
    len(pairs_cache.get(quote.lower(), []))
)
```

### Verify Bridge Route Discovery

The discovery functions check:
1. **DIEM→VVV pair exists** with liquidity >= `_MIN_HOP_LIQ_USD`
2. **VVV→USDC pair exists** with liquidity >= `_MIN_HOP_LIQ_USD`
3. Both pairs are on allowed DEXes (V2: uniswap_v2, aerodrome; V3: uniswap_v3)

If either check fails, the multi-hop route won't be discovered.

## Recommended Configuration

For reliable DIEM→VVV→USDC discovery:

```bash
# Ensure VVV is configured
export VVV_TOKEN_ADDRESS=0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf

# Lower liquidity thresholds if needed (DIEM→VVV pair may have lower liquidity)
export TRADE_PATH_HOP_MIN_LIQ_USD=500

# Increase route limit to allow multi-hop routes (default is 4)
export TRADE_PATH_DYNAMIC_MAX_ROUTES=8

# Enable debug logging to see discovery process
export DIEM_DEBUG_ROUTES=1
```

**Note**: The discovery now prioritizes multi-hop routes over direct routes, so DIEM→VVV→USDC routes will be included even if the route limit is low. However, increasing the limit is still recommended to allow multiple multi-hop route variants.

## Fallback: Use Configured Routes

If dynamic discovery doesn't find multi-hop routes, the system falls back to:
1. **Env routes** (`TRADE_PATH`, `TRADE_PATHS`)
2. **Bridge routes** (injected from `get_bridge_trade_path_with_metadata`)
3. **Canonical routes** (from `CANONICAL_PRICE_PATHS`)

These are validated by `market:trade-paths:validate` even without dynamic discovery metadata.

## Expected Behavior

When discovery works correctly, you should see:
```
INFO | marketdata.provider | dynamic trade path: tokens=['0xf4d97f2d...', '0xacfe6019...', '0x833589fc...'] fees=None
```

This indicates a 3-token DIEM→VVV→USDC route was discovered.

If you only see:
```
INFO | marketdata.provider | dynamic trade path: tokens=['0xf4d97f2d...', '0x833589fc...'] fees=None
```

Then only the direct route was found, and multi-hop discovery failed (likely due to missing pairs, low liquidity, or route limit).

