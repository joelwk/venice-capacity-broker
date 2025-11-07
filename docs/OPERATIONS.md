# Operations Guide

## Responsibilities

- Maintain orchestrator health, staking cadence, DIEM inventory, and tenant quotas.
- Keep live toggles safe; escalate via progressive-live before enable-live.
- Ensure Venice parent/sub-key hygiene and auditability.

## Dashboards & Logs

- HTTP: `/health`, `/metrics`, `/v1/env`, `/admin` (Broker UI).
- Logs: `logs/runtime.log` (quorum, reflex, price sanity, trades, summaries).
- Memory: `db/agent_memory.jsonl` (cycle records, portfolio, decisions).

## Daily Checklist

1) Validate environment and Venice API config

```bash
uv run python apps/cli/main.py env:status
uv run python apps/cli/main.py startup:probe --check-live
```

2) Confirm StakeMaster heartbeat and rewards cadence

```bash
uv run python apps/cli/main.py run:stakemaster --enable-live --max-cycles 0
```

3) Review utilization and DIEM buffer (target ~1.5× daily)

- Check `/metrics` and latest `agent_memory.jsonl` entries.

## Safety Controls

- `AGENTS_PAUSED=true` — immediate stop without API downtime.
- Quorum and Reflex guardrails: `QUORUM_ENABLE`, `REFLEX_MAX_VOL_BPS`, `REFLEX_MAX_PRICE_DRAWDOWN`, `REFLEX_REQUIRE_ACTIVE_STAKE`.
- Price sanity: `MARKETDATA_PRICE_SANITY_MAX_DRIFT` (use only when diagnosing drifts).

## Tenant & Key Management (CapacityBroker)

- Issue scoped sub-keys with `consumptionLimit` and `expiresAt`.
- Rotate/revoke on anomaly; store issuance audit.

```bash
uv run python apps/cli/main.py broker:tenants:list
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

## Runbook (dry → progressive-live → live)

```bash
# Dry-run (default)
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 2

# Progressive-live (recommended)
uv run python apps/cli/main.py run:loop --progressive-live --sleep 15

# Enable-live (immediate)
uv run python apps/cli/main.py run:loop --enable-live --sleep 15
```

## Incident Runbook

- Venice API 404
  - Ensure `VENICE_API_BASE_URL` includes `/api/v1`.
  - `uv run python apps/cli/main.py startup:probe` and `venice:probe-openapi`.

- DEX route gaps / liquidity
  - `uv run python apps/cli/main.py market:routes:suggest --base DIEM --quote USDC`.
  - Adjust `TRADE_PATH`/routers; verify with quotes preview.

- RPC instability
  - Switch `BASE_RPC_URL`; increase `RPC_REQUEST_TIMEOUT_SECONDS`.

- Key abuse or anomaly
  - Revoke sub-keys; rotate parent; raise limits only after root-cause.

## References

- Configuration → `./CONFIGURATION.md`
- Deployment → `./DEPLOYMENT.md`
- Troubleshooting → `./TROUBLESHOOTING.md`
- Security & Keys → `./SECURITY_KEYS.md`


