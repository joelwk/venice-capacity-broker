# Bridge Factory Registration Runbook

**Date**: 2025-11-28  
**Scope**: Base mainnet – DIEM/VVV (Aerodrome) and VVV/USDC (Uniswap V3)

---

## Purpose

Enable router-based execution by ensuring the bridge pools are discoverable via the Aerodrome volatile factory and the Uniswap V3 factory on Base.  
The runbook covers the health check, dry-run verification, live registration, and post-registration validation.

---

## Execution Pre-Check Integration

The ArbiDiem agent blocks live router execution when the Aerodrome DIEM/VVV pair or the Uniswap V3 VVV/USDC pool is not registered with its factory.  
Logs emit `factory_registration_missing` with: `Execution blocked: bridge pools not registered with factories. Run \`market:bridge-factory-check\` for details.`  
Run `uv run python apps/cli/main.py market:bridge-factory-check` before enabling live cycles or after any bridge-path redeployments.

---

## Preconditions

- Funded signer wallet (Base ETH) with `ETH_PRIVATE_KEY` exported.  
- `FACTORY_REGISTRATION_ALLOWED_ADDRESSES` contains the signer address (comma separated allow-list).  
- Production environment variables for bridge routing are already configured (see `docs/PRODUCTION_CONFIG_BRIDGE_PATH.md`).  
- Composite routing is operating normally (no outstanding bridge health incidents).

Optional but recommended: set `APP_ENV=production` on the host where registration will run.

---

## Step 1 – Health Check

```bash
uv run python apps/cli/main.py market:bridge-factory-check
```

Expected result: both factories report the configured pool addresses as `Factory get*`.  
If either factory returns `<none>`, continue to Step 2.  
If the command fails due to missing configuration, resolve the env mismatch before proceeding.

---

## Step 2 – Dry-Run Diagnostics

```bash
uv run python scripts/register_bridge_pools.py
```

What to confirm:
- Chain ID, RPC endpoint, and signer information look correct.  
- Notes show token ordering, `factory()` values, and whether bytecode is present at each pool address.  
- The script reports that no transactions were sent.

Do **not** proceed if bytecode already exists at the pool address but the script would require `--force-registration` unless you have explicit sign-off from protocol owners.

---

## Step 3 – Live Registration

1. Export safety gates:
   ```bash
   export FACTORY_REGISTRATION_ALLOWED_ADDRESSES=0xSignerAddr
   export CONFIRM_MAINNET=YES
   ```

2. Submit the registration transactions:
   ```bash
   uv run python scripts/register_bridge_pools.py \
     --enable-live \
     --confirm-mainnet \
     --wait-for-receipt
   ```

3. For environments where bytecode is absent and a new pool must be deployed, add `--force-registration`.  
   Only do this after double-checking that no canonical pool currently exists.

4. Monitor terminal output for transaction hashes and confirmation statuses.  
   If a transaction reverts, capture the revert reason from the script output and escalate before retrying.

---

## Step 4 – Post-Registration Validation

1. Re-run the CLI health check:
   ```bash
   uv run python apps/cli/main.py market:bridge-factory-check
   ```
   Expected: both factories now return the configured addresses and `Registered: yes`.

2. Re-run the probe script to record the updated status:
   ```bash
   uv run python scripts/probe_bridge_path_failures.py
   ```
   Confirm that the factory registration section reports success.

3. Observe runtime logs for the next orchestrator cycles.  
   Verify that composite routing continues to operate and no new slippage or quote failures appear.

---

## Failure Handling

- **Factory call reverts**: stop, capture the revert message, and open a protocol ticket.  
  Do not retry with `--force-registration` unless instructed by factory maintainers.

- **Signer not authorized**: update `FACTORY_REGISTRATION_ALLOWED_ADDRESSES` or use `--allow-unlisted-sender` after security sign-off.

- **Chain ID mismatch**: ensure RPC endpoints point to Base mainnet; never bypass the guard.  

- **Pool already exists but mapping missing**: escalate to protocol maintainers.  
  The script intentionally skips registration when bytecode is present unless `--force-registration` is supplied.

- **Composite routing disruptions**: leave `DIEM_VVV_DIRECT_SWAP_ENABLE` and `DIEM_ENABLE_PAIR_MATH_FALLBACK` enabled so quoting continues while registration issues are resolved.

---

## Rollback / Contingency

No on-chain rollback is available for factory registration.  
If a misconfigured pool was deployed, contact Aerodrome or Uniswap operators to evaluate removal, and update application config to keep using the canonical pool addresses.  
Until router execution is stable, keep composite routing and reserve fallbacks enabled.

---

## References

- `scripts/register_bridge_pools.py` – Diagnostics and registration helper.  
- `apps/cli/main.py market:bridge-factory-check` – Factory health check.  
- `docs/PRODUCTION_CONFIG_BRIDGE_PATH.md` – Environment and configuration details.

