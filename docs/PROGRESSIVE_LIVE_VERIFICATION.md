# Progressive-Live Re-enablement Verification Guide

## Summary

All code changes have been completed to stabilize DIEM arbitrage execution:

1. ✅ **Slippage guards hardened** - Execution blocked when slippage >= 1000 bps or exceeds policy cap
2. ✅ **Quote validation added** - Execution blocked when no valid execution price preview available
3. ✅ **TRADE_PATH updated** - Default route changed to `DIEM,VVV,USDC` for better liquidity
4. ✅ **Tests passing** - All arbitrage and DIEM integration tests pass

## What Changed

### Code Changes

**`agents/arbi_diem/agent.py`**:
- Added execution guards for both `mint_sell` and `buy_burn` paths
- Blocks live execution when:
  - `slippage_ok=False` (slippage exceeds policy cap)
  - Slippage >= 1000 bps (extreme slippage threshold)
  - No valid execution price preview available (`preview_px <= 0` or `final_preview <= 0`)
- Returns `hold` decision with appropriate reason codes instead of executing
- Guards only apply to live execution (`simulate=False`); dry-run simulation still works

**`config/default.yml`**:
- Updated default `TRADE_PATH` from addresses to symbols: `DIEM,VVV,USDC`
- Uses bridge route (DIEM→VVV→USDC) for better liquidity access

## Verification Steps

When re-enabling progressive-live, verify the following:

### 1. Start with Progressive-Live Only

```bash
python -m apps.cli.main run:loop --progressive-live --max-cycles 5
```

**Do NOT** use `--enable-live` initially.

### 2. Monitor Runtime Logs

Watch `logs/runtime-*.log` for:

#### ✅ Success Indicators:
- `arbi.dry_run=True` for initial cycles
- `execution.status='dry_run'` for dry-run cycles
- `execution.status='executed'` only when quotes are valid and slippage is acceptable
- `execution.status='blocked'` or `decision='hold'` when guards trigger
- No `TRANSFER_FROM_FAILED` errors

#### ⚠️ Warning Signs:
- `execution.status='error'` with `TRANSFER_FROM_FAILED`
- `decision='mint_sell'` with `slippage_bps` >= 1000
- `decision='mint_sell'` with `reason='no_execution_preview'` but still executing

### 3. Check Guard Behavior

Look for these log messages indicating guards are working:

```
Mint and sell blocked: no valid execution price preview available
Mint and sell blocked: extreme_slippage (slippage_bps=XXXX, cap=XXX)
Mint and sell blocked: slippage_exceeded_policy (slippage_bps=XXX, cap=XXX)
```

### 4. Verify Rationale Fields

Check that `arbi.why` contains:
- `decision='hold'` when guards block execution
- `reason` field indicating why execution was blocked:
  - `no_execution_preview` - No quotes available
  - `extreme_slippage` - Slippage >= 1000 bps
  - `slippage_exceeded_policy` - Slippage exceeds policy cap
- `policy_checks.slippage_ok=False` when slippage is too high

### 5. Progressive Transition

After several clean dry-run cycles:
- Progressive-live should transition to live mode automatically
- Watch for the first live cycle to ensure guards still work
- If guards block execution correctly, continue monitoring
- Only after multiple successful cycles, consider adding `--enable-live` for longer sessions

## Expected Behavior

### Dry-Run Mode
- Guards do NOT block execution (simulation only)
- `decision` can be `mint_sell` even without quotes
- `execution.status='dry_run'`

### Live Mode (Progressive-Live Enabled)
- Guards BLOCK execution when conditions aren't met
- `decision='hold'` with appropriate `reason`
- `execution.status='blocked'` or `execution.status='no_action'`
- No on-chain transactions attempted when blocked

### Successful Live Execution
- Only occurs when:
  - Valid quotes/preview available (`preview_px > 0`)
  - Slippage within policy cap (`slippage_bps <= slippage_bps_cap`)
  - Slippage < 1000 bps (not extreme)
- `execution.status='executed'`
- `execution.executed=True`

## Troubleshooting

### If `TRANSFER_FROM_FAILED` errors still occur:

1. Check that guards are actually blocking:
   - Look for `Mint and sell blocked:` log messages
   - Verify `decision='hold'` in rationale

2. Verify `simulate` parameter:
   - Ensure `simulate=False` is only passed in live mode
   - Check orchestrator code at `graph/workflows/orchestrator.py:1561`

3. Check quote availability:
   - Run `python -m apps.cli.main quotes:preview --units 1`
   - Verify non-zero quotes are returned

### If guards block too aggressively:

1. Check slippage cap:
   - Verify `RISK_SLIPPAGE_BPS_CAP` environment variable
   - Default is 150 bps

2. Check extreme slippage threshold:
   - Currently hardcoded at 1000 bps in `agents/arbi_diem/agent.py:806`
   - Can be adjusted if needed

3. Verify quote sources:
   - Check `logs/dex_diagnostics.jsonl` for quote availability
   - Ensure `TRADE_PATH=DIEM,VVV,USDC` is set correctly

## Next Steps

After successful verification:

1. Run longer progressive-live sessions (10-20 cycles)
2. Monitor for any edge cases or unexpected behavior
3. Once stable, consider enabling full live mode with `--enable-live`
4. Continue monitoring logs for any issues

## Related Files

- `agents/arbi_diem/agent.py` - Guard implementation
- `config/default.yml` - TRADE_PATH configuration
- `graph/workflows/orchestrator.py` - Orchestrator execution flow
- `fix.plan.md` - Original implementation plan

