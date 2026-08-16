# Production Configuration for Bridge Path Routing

**Date**: 2025-11-28  
**Status**: Production Ready (with composite routing)

## Summary

Composite routing for DIEM→VVV→USDC bridge path is now working end-to-end. This document outlines the production configuration required to maintain reliable bridge path routing.

## Required Environment Variables

### Core Bridge Path Configuration

```bash
# Token addresses
DIEM_TOKEN_ADDRESS=0xf4d97f2da56e8c3098f3a8d538db630a2606a024
VVV_TOKEN_ADDRESS=0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913

# Pool addresses (bypass factory discovery)
DIEM_VVV_PAIR_ADDRESS=0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d
VVV_USDC_POOL_ADDRESS=0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530
VVV_USDC_POOL_FEE=3000

# Bridge path provider preferences
DIEM_VVV_BRIDGE_PROVIDER=aerodrome
```

### Critical: Enable Reserve Fallback for Leg1

**Required for production** - Enables reserve math fallback for DIEM/VVV leg, bypassing router factory discovery:

```bash
# Enable direct reserve-based quoting for DIEM/VVV leg
DIEM_VVV_DIRECT_SWAP_ENABLE=1
DIEM_ENABLE_PAIR_MATH_FALLBACK=1
```

**Why**: The DIEM/VVV pair is not registered with Aerodrome factories, so router `getAmountsOut()` calls fail. The reserve fallback uses direct pool reserves to compute quotes, bypassing factory discovery.

### Factory Registration Safety Flags

Set these environment variables before running live registration:

```bash
# Allow-listed signers that can submit registration transactions
FACTORY_REGISTRATION_ALLOWED_ADDRESSES=0xYourSigner,0xBackupSigner

# Explicit confirmation required when --enable-live is used
CONFIRM_MAINNET=YES
```

The `scripts/register_bridge_pools.py` script enforces the allow-list and confirmation gate unless `--allow-unlisted-sender` or `--allow-nonprod` overrides are supplied.

### Composite Routing Configuration

Composite routing is enabled by default and automatically triggers when:
- Route has composite metadata attached (via `get_bridge_trade_path_with_metadata()`)
- Route spans multiple providers (Aerodrome Leg1 + V3 Leg2)

No additional configuration needed - composite routing is handled automatically by `best_quote()`.

## How It Works

### Composite Routing Flow

1. **Route Construction**: Bridge path route is constructed with fees `[None, 3000]`:
   - Leg1 (DIEM→VVV): No fee (Aerodrome V2)
   - Leg2 (VVV→USDC): Fee 3000 (Uniswap V3)

2. **Metadata Attachment**: Bridge path metadata is attached via `attach_composite_metadata()`:
   - Leg1: `provider="aerodrome"`, `pool_address=<DIEM_VVV_PAIR_ADDRESS>`, `fee=None`
   - Leg2: `provider="uniswap_v3"`, `pool_address=<VVV_USDC_POOL_ADDRESS>`, `fee=3000`

3. **Composite Quoting**: `best_quote()` detects composite route and calls `quote_composite_exact_in()`:
   - Quotes Leg1 via Aerodrome (uses reserve fallback when `DIEM_VVV_DIRECT_SWAP_ENABLE=1`)
   - Quotes Leg2 via Uniswap V3 (uses quoter, doesn't need factory)
   - Chains results: `amount_out[Leg1]` becomes `amount_in[Leg2]`

4. **Execution**: `DexAggregator.trade_best` / `trade_best_exact_out` run `_execute_composite_*` (sequential txs, pre-approvals, wait-for-balance). Partial-leg failure is logged as stranded inventory; it does not auto-unwind.

## Current Limitations

### Factory Registration

**Status**: Pools may still be missing factory entries in environments that have not executed the new registration workflow.

- Run `uv run python apps/cli/main.py market:bridge-factory-check` to validate discoverability.
- Use `uv run python scripts/register_bridge_pools.py` (dry-run first) to perform registration when the factory reports zero addresses.

**Impact**:
- Router `getAmountsOut()` calls fail while factories return zero addresses.
- Router-based execution would fail unless pools are registered or direct pool calls are implemented.

**Workaround**:
- ✅ **Quoting**: Uses reserve fallback (Leg1) + quoter (Leg2) - **works**
- ⚠️ **Execution**: Router calls will fail until pools are registered or alternative execution paths are added.

### Direct DIEM→USDC Path

**Status**: No direct path exists (expected - no direct pool)

**Impact**: Must use bridge path (DIEM→VVV→USDC) for all DIEM/USDC trades

## Production Checklist

- [x] ✅ Enable `DIEM_VVV_DIRECT_SWAP_ENABLE=1`
- [x] ✅ Enable `DIEM_ENABLE_PAIR_MATH_FALLBACK=1`
- [x] ✅ Configure pool addresses (`DIEM_VVV_PAIR_ADDRESS`, `VVV_USDC_POOL_ADDRESS`)
- [x] ✅ Configure fee tier (`VVV_USDC_POOL_FEE=3000`)
- [ ] ⏳ Register pools with factories (run `market:bridge-factory-check`, then `scripts/register_bridge_pools.py --enable-live` when ready)
- [ ] ⏳ Monitor composite routing success rate
- [ ] ⏳ Set up alerts for composite routing failures

## Monitoring

### Key Metrics

- `dex_composite_quote_success_total` - Composite quotes that succeeded
- `dex_composite_leg_failed_total` - Individual leg failures
- `dex_composite_fallback_used_total` - Reserve fallback usage

### Expected Behavior

- **Quoting**: Should succeed via composite routing
- **Leg1 (DIEM→VVV)**: Uses reserve fallback (bypasses factory)
- **Leg2 (VVV→USDC)**: Uses V3 quoter (bypasses factory)
- **Router calls**: Will fail until pools are registered (expected)

## Troubleshooting

### Composite Routing Not Triggering

**Symptoms**: `best_quote()` returns single-provider quotes instead of composite

**Check**:
1. Route has composite metadata: `getattr(route, "_is_composite", False)` should be `True`
2. Bridge legs are attached: `getattr(route, "_bridge_legs", None)` should not be `None`
3. Route structure matches legs: `len(route.tokens) - 1 == len(bridge_legs)`

**Fix**: Ensure `get_bridge_trade_path_with_metadata()` is called and metadata is attached via `attach_composite_metadata()`

### Leg1 Quote Failing

**Symptoms**: DIEM→VVV leg returns `None`

**Check**:
1. `DIEM_VVV_DIRECT_SWAP_ENABLE=1` is set
2. `DIEM_VVV_PAIR_ADDRESS` is configured correctly
3. Pool has liquidity (check reserves)

**Fix**: Enable reserve fallback flags and verify pool address

### Leg2 Quote Failing

**Symptoms**: VVV→USDC leg returns `None`

**Check**:
1. `VVV_USDC_POOL_ADDRESS` is configured correctly
2. `VVV_USDC_POOL_FEE=3000` matches actual pool fee tier
3. V3 quoter address is configured

**Fix**: Verify pool address and fee tier match on-chain pool

## Future Improvements

1. **Factory Registration**: Register pools with factories to enable router-based execution
2. **Direct Pool Execution**: Implement direct pool calls for execution (bypass routers)
3. **Pool Discovery**: Automatically discover pool addresses from factory events
4. **Fallback Chain**: Add more fallback layers (reserve math → quoter → router)

## References

- **Composite Routing**: `libs/dex/composite.py` - Implementation details
- **Bridge Path Metadata**: `services/marketdata/pathing/fallbacks.py` - Route construction
- **Factory Registration Script**: `scripts/register_bridge_pools.py` - Diagnostics and registration workflow
- **Factory Registration Runbook**: `docs/BRIDGE_FACTORY_REGISTRATION_RUNBOOK.md` - Operational guide

