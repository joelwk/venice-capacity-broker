# Troubleshooting

## Venice API 404 Errors

- Ensure `VENICE_API_BASE_URL` includes `/api/v1` (required by Venice).
- Probe upstream:

```bash
uv run python apps/cli/main.py startup:probe
uv run python apps/cli/main.py venice:probe-openapi
```

- If mismatched, set the correct base URL and retry.

## DEX Route Gaps / Pricing Failures

- Suggest paths and validate liquidity:

```bash
uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC
uv run python apps/cli/main.py quotes:preview --units 1000000000000000000
```

- Adjust `TRADE_PATH`, router envs, or token addresses; confirm with `quotes:preview`.
- **Exact-in fallback for degraded DEX conditions**: When exact-out previews fail (e.g., due to RPC rate limits or DEX errors), enable exact-in fallback for small trades:
  - Set `DIEM_EXACT_IN_FALLBACK_ENABLE=1` to enable fallback.
  - Configure `DIEM_EXACT_IN_FALLBACK_MAX_USD` (default: 10.0) to limit fallback trade size.
  - The system will attempt exact-in quotes when exact-out fails, allowing trade signals even under degraded conditions.
  - Fallback trades are flagged in logs with `"venue": "exact_in_fallback"` for monitoring.

### Quotes + DIEM price fallbacks

- When DEX quotes keep failing we now respond with the latest DIEM snapshot instead of hiding prices. `/v1/market/diem` returns `priceUsd`, `health`, and the most recent diagnostics from `logs/dex_diagnostics.jsonl`. Use it to keep UI cards populated even while quotes error.
- Quote endpoints include a `fallbackPrice` object when there is no executable route. The frontend can surface `fallbackPrice.priceUsd` to show “Quote unavailable, but DIEM is $X.XX” and reference the attached diagnostics for debugging.
- Cache bust the endpoint via `_t`/`_r` query parameters when you need the freshest snapshot after a quote failure.

## Buy page: spot vs limit

- `/v1/env` `features.bids` must be true or Order type / Place Bid is off. `POST /v1/bids` is 404 when `BIDS_ENABLED` is false.
- Max unit price is the pay asset **per 1 DIEM**, not a dollar total. A cap below live `unitPrice` settles as 409 `price exceeds bid max` (or `bid out of band`).
- Place Bid opens the wallet for EIP-712 `PurchaseIntent`. That is a signature, not a transfer. Payment still happens after a quote appears.
- A filled limit shows the same amount / treasury card as spot. That is expected.
- `503` with `inventory failsafe hot` means CapacityBroker paused intake. Check `BROKER_INVENTORY_POLICY_PATH` and `BROKER_UTIL_SURGE_THRESHOLD`.

## Trade Efficiency Issues (Overpayment)

A 9x overpayment shows up as `trade_efficiency_ratio` near 9.0 in `execute_trade: execution diagnostics` logs.  

You may also see `trade_efficiency_alert` warnings with `expected_amount_in_slot0` far below `actual_amount_in`.  

If `route_type` is `bridge`, the buy likely used the bridge path instead of the direct pool.  

### Remediation

Set `DIEM_BUY_DIRECT_ONLY=1` to force the direct pool.  

Confirm `AERODROME_CL_ROUTER_ADDRESS`, `DIEM_USDC_POOL_ADDRESS`, and `DIEM_USDC_TICK_SPACING` are present.  

Tighten `DIEM_BUY_AMOUNT_IN_SANITY_THRESHOLD` to 1.5-2.0 during stabilization.  

Set `DIEM_TRADE_EFFICIENCY_ALERT_THRESHOLD` to your alert level.  

### Verify direct pool liquidity

Use these probes to validate the direct DIEM/USDC pool.  

```bash
uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC
uv run python apps/cli/main.py quotes:preview --units 1000000000000000000
```

Check that the suggested route is DIEM/USDC and that quotes are non-zero.  

## DIEM Price Guard Blocks

- Check price source and fallback reason:

```bash
# View price health metadata
uv run python apps/cli/main.py quotes:preview --units 1000000000000000000

# Check mint rate for fair value calculation
uv run python apps/cli/main.py diem:mint-rate --live

# Verify on-chain liquidity
uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC
```

- If DIEM uses `external_reference` with `fallback_reason=no_onchain_liquidity`, the price guard will accept it.
- If DIEM uses `bridge_vvv` (price inferred from VVV), trades can proceed without on-chain DIEM liquidity.
- If price guard blocks due to stale/clamped data, check `logs/runtime.log` for price sanity warnings.

### No Liquidity Preview (reserve_cap_units < 1000)

When logs show `'reason': 'no_liquidity_preview'` and `'reserve_cap_units': 9`:
- System found no meaningful on-chain DIEM liquidity
- Reserve cap of < 1000 base units indicates non-existent or dust-level pools
- System now bypasses this cap and allows trades at risk-suggested sizes
- Requires either: (1) Add DIEM/USDC liquidity to DEX, or (2) Implement OTC trading

See `docs/DIEM_LIQUIDITY_ANALYSIS.md` for full analysis and solutions.

## Migrations / State Drift

- Apply latest migrations and compact KV counters:

```bash
uv run alembic upgrade head
make server-db-compact
```

## Rate Limits / Key Issues

- Verify sub-keys have `consumptionLimit` and `expiresAt`.
- Audit and cleanup keys:

```bash
uv run python apps/cli/main.py broker:tenants:list
uv run python apps/cli/main.py venice:keys:cleanup --prefix T1 --dry-run
```

## RPC / Network Instability

- The system now automatically tracks RPC endpoint health and rotates away from rate-limited or failing endpoints.
- Multiple RPC endpoints: Set `BASE_RPC_URLS` (comma-separated) for automatic failover. The system will prefer healthy endpoints and avoid rate-limited ones.
- Health tracking configuration (optional):
  - `RPC_RATE_LIMIT_COOLDOWN_SECONDS` (default: 60) - How long to avoid endpoints after 429 errors.
  - `RPC_BACKOFF_BASE_SECONDS` (default: 60) - Base backoff time for failed endpoints.
  - `RPC_BACKOFF_MAX_SECONDS` (default: 600) - Maximum backoff time.
  - `RPC_BACKOFF_MULTIPLIER` (default: 2.0) - Exponential backoff multiplier.
- Manual override: Switch `BASE_RPC_URL` to a healthy endpoint if automatic rotation isn't sufficient.
- Increase `RPC_REQUEST_TIMEOUT_SECONDS` if timeouts are frequent.

## Guardrails Blocked Actions

- Check `logs/runtime.log` for price guard streaks and Reflex reasons.
- Loosen temporarily only for debugging: `MARKETDATA_PRICE_SANITY_MAX_DRIFT`, `REFLEX_*` (revert after).

## References

- Configuration → `./CONFIGURATION.md`
- Deployment → `./DEPLOYMENT.md`
- Operations → `./OPERATIONS.md`

## Mint Rate Conversion Mismatch (fair value shows ~1e18)

When logs show an astronomically large DIEM fair value (e.g., `fair=1325364000000000000.0000`) and `mint_rate=1000000000000000000.00`, the mint rate is being treated as base units during fair value calculation.

### Symptoms

- Runtime logs like:

```
2025-11-10 00:32:05 | INFO | AGENT[arbi_diem] | Market px=120.9495, fair=1325364000000000000.0000 (vvv=1.3254, mint_rate=1000000000000000000.00, util=0.00%, conf=85%)
```

- Premium appears near 0 despite high market price.

### Diagnose

1) Inspect mint rate via CLI:

```bash
uv run python apps/cli/main.py diem:mint-rate --live
```

- Expect `tokens_per_diem: 1.0` when `DIEM_MINT_RATE_SVVV_PER_DIEM=1e18` (1 sVVV per 1 DIEM in base units).

2) If `tokens_per_diem` is `1e18` (wrong), verify envs and conversion logic:

- Env override (optional): `DIEM_MINT_RATE_SVVV_PER_DIEM=1000000000000000000`
- The conversion should normalize base units to token units.

### Resolution

- Ensure mint rate normalization divides by sVVV decimals:

```python
# services/marketdata/provider.py
# Correct conversion (example for 18-decimal tokens)
return float(units) / float(10 ** svvv_decimals)
```

- This yields `1.0` when `units = 1e18` and both tokens have 18 decimals.

### Verify

```bash
# 1) Mint rate returns tokens_per_diem: 1.0
uv run python apps/cli/main.py diem:mint-rate

# 2) Fair value returns reasonable numbers (e.g., $7–10)
tail -n 200 runtime.log | Select-String "Market px="
```

### Notes

- The fair value model depends on mint rate in token units.
- Large `fair` values will suppress trades by distorting premium calculations.


