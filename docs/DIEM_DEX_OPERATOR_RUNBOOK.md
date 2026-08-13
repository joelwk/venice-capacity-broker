# DIEM/DEX Operator Runbook

This runbook provides step-by-step procedures for diagnosing and resolving DIEM/DEX execution issues.

## Quick Diagnostics

### Verify DIEM Approvals and Balances

```bash
# Check DIEM token allowance for Aerodrome router
python scripts/check_diem_allowance.py \
  --token 0xf4d97f2da56e8c3098f3a8d538db630a2606a024 \
  --spender 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43

# Approve if needed
python scripts/approve_spender.py \
  --token 0xf4d97f2da56e8c3098f3a8d538db630a2606a024 \
  --spender 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43
```

### Verify DEX Configuration

```bash
# Validate router and factory addresses against Base on-chain
python scripts/validate_dex_config.py
```

### Check DIEM/VVV and VVV/USDC Pools

```bash
# Diagnose DIEM liquidity
python scripts/diagnose_diem_liquidity.py

# Preview quotes
python apps/cli/main.py quotes:preview --units 1000000000000000000
```

## Common Error Codes

### `no_liquidity_diem_vvv`
- **Meaning**: DIEM/VVV pair has insufficient liquidity for the requested trade size
- **Action**: Check reserves via `diagnose_diem_liquidity.py`, reduce trade size, or wait for liquidity

### `router_revert_spl`
- **Meaning**: Router call reverted due to insufficient liquidity (SPL = Swap Pool Liquidity)
- **Action**: Verify pool reserves, check if trade size exceeds available liquidity

### `router_revert_underflow`
- **Meaning**: Math underflow in router calculation (usually insufficient reserves)
- **Action**: Check pool reserves, verify token addresses are correct

### `no_quotes_available`
- **Meaning**: No DEX providers returned valid quotes
- **Action**: 
  1. Check `logs/dex_diagnostics.jsonl` for per-hop failures
  2. Verify router addresses are correct for Base
  3. Check RPC connectivity
  4. Enable `DIEM_ENABLE_PAIR_MATH_FALLBACK=1` for reserve-based fallback

### `diem_vvv_leg_failed`
- **Meaning**: The DIEM/VVV leg of a composite route failed
- **Action**: Check DIEM/VVV pair reserves and router configuration

## DIEM Price Issues

### Price Divergence from bridge_vvv

If logs show `marketdata.sanity | DIEM price divergence`:

1. **Check bridge_vvv price**:
   ```bash
   python apps/cli/main.py quotes:preview --units 1000000000000000000
   ```

2. **Verify DIEM/VVV and VVV/USDC pools**:
   ```bash
   python scripts/diagnose_diem_liquidity.py
   ```

3. **Adjust divergence threshold** (if needed):
   ```bash
   export MARKETDATA_DIEM_PRICE_MAX_DRIFT=0.30  # 30% instead of default 20%
   ```

### Price Source Priority

DIEM price is sourced in this order:
1. `bridge_vvv` (on-chain DIEM/VVV × VVV/USDC)
2. External reference (Dexscreener, etc.)
3. Path engine quotes

If `bridge_vvv` fails, check:
- `DIEM_VVV_PAIR_ADDRESS` is correct
- `VVV_USDC_POOL_ADDRESS` is correct
- RPC connectivity to Base

## Route Selection

### Canonical DIEM Routes

The system prioritizes canonical routes:
- **DIEM → USDC**: `DIEM -> VVV -> USDC`
- **USDC → DIEM**: `USDC -> VVV -> DIEM`

These routes use:
- DIEM/VVV pair (V2/Aerodrome)
- VVV/USDC pool (V3)

### Fallback Routes

If canonical routes fail, the system falls back to:
- Generic WETH-based routes (if liquidity exists)
- Direct DIEM/USDC pairs (if available)

Check route selection in logs:
```bash
grep "diem_canonical" logs/runtime.log
grep "dex.routes" logs/runtime.log
```

## Pool Discovery

### Verify Pools in Registry

```bash
# List discovered pools
python apps/cli/main.py market:pools:list --token DIEM

# Check pool watcher status
python apps/cli/main.py market:pools:watch --once
```

### Factory Configuration

Ensure these factories are configured for Base:
- `AERODROME_FACTORY_VOLATILE` (for DIEM/VVV pair)
- `UNISWAP_V3_FACTORY_ADDRESS` (for VVV/USDC pool)

Check in `config/default.yml` or via:
```bash
python scripts/validate_dex_config.py
```

## Execution Health

### Check Last Successful Trade

```bash
# Check orchestrator logs for successful trades
grep "trade.*success" logs/runtime.log | tail -5

# Check DEX diagnostics
tail -20 logs/dex_diagnostics.jsonl | jq '.event, .reason, .mode'
```

### Enable DIEM Execution Health Metrics

The system tracks:
- Last successful DIEM quote (per route/provider)
- Last successful DIEM trade (hash, timestamp)

View in logs:
```bash
grep "diem_exec_health" logs/runtime.log
```

## Troubleshooting Workflow

1. **Check approvals**: Ensure router is approved to spend DIEM
2. **Validate config**: Run `validate_dex_config.py`
3. **Check pools**: Verify DIEM/VVV and VVV/USDC pools exist and have liquidity
4. **Review diagnostics**: Check `logs/dex_diagnostics.jsonl` for per-hop errors
5. **Test quotes**: Run `quotes:preview` to verify routing works
6. **Check price**: Ensure DIEM price is reasonable (not $0.0001)
7. **Review logs**: Check orchestrator logs for decision-making context

## Emergency Procedures

### Disable Live Trading

If execution is failing repeatedly:

```bash
# Set dry-run mode
export STAKEMASTER_PROGRESSIVE_ENABLE=false
# Or remove --enable-live flag from orchestrator
```

### Enable Fallbacks

```bash
# Enable pair math fallback
export DIEM_ENABLE_PAIR_MATH_FALLBACK=1

# Enable exact-in fallback for small trades
export DIEM_EXACT_IN_FALLBACK_ENABLE=1
export DIEM_EXACT_IN_FALLBACK_MAX_USD=10.0
```

### Reset Circuit Breakers

If circuit breakers are blocking execution:

```bash
# Check circuit state in diagnostics
grep "circuit_open" logs/dex_diagnostics.jsonl

# Circuit breakers reset automatically after cooldown
# Or restart the orchestrator
```

## References

- **DIEM Token**: `0xf4d97f2da56e8c3098f3a8d538db630a2606a024`
- **VVV Token**: `0xacfe6019ed1a7dc6f7b508c02d1b04ec88cc21bf`
- **USDC Token**: `0x833589fcd6edb6e08f4c7c32d4f71b54bda02913`
- **DIEM/VVV Pair**: `0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d`
- **VVV/USDC Pool**: `0x67a11022b7b6ed66f81233f6c8ed6e48f7826530`
- **Aerodrome Router**: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`

## Base Explorer Links

- [DIEM Token](https://basescan.org/token/0xf4d97f2da56e8c3098f3a8d538db630a2606a024)
- [DIEM/VVV Pair](https://basescan.org/address/0xbb345d35450bf9ee76f3d2ce214e8e7ac5e1071d)
- [VVV/USDC Pool](https://basescan.org/address/0x67a11022b7b6ed66f81233f6c8ed6e48f7826530)

