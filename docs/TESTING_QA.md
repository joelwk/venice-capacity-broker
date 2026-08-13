# Testing & QA

## Core Suites

- DEX paths & slippage: `tests/test_dex_exact_out.py`, `tests/test_dex_fot_fallback.py`, `tests/test_dex_exact_out_venues.py`.
- DIEM services: `tests/test_diem_service.py`, `tests/test_diem_buy_path.py`, `tests/test_diem_mint_burn_dryrun.py`.
- DIEM fair value & routing: `tests/test_diem_fair_value.py`, `tests/test_diem_fair_value_v2.py`, `tests/test_diem_routing_integration.py`.
- Risk policy: `tests/test_risk_policy.py`, `tests/test_arbi_diem_risk_integration.py`.
- Broker limits & idempotency: `tests/test_broker_limits.py`, `tests/test_cli_idempotency_purge.py`.
- Market-data normalization: `tests/test_marketdata_prices.py`.

## Orchestrator & Probes

```bash
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 1
uv run python apps/cli/main.py startup:probe --check-live
uv run python apps/cli/main.py quotes:preview --units 1000000000000000000
uv run python apps/cli/main.py market:best-price:scan --start 1.0 --min 1e-12 --factor 10
uv run python apps/cli/main.py diem:mint-rate --live
```

## Failure Injection

- API errors / 404s → set invalid `VENICE_API_BASE_URL` then run `startup:probe` (expect clear diagnostics).
- Rate limits → issue sub-keys without limits (expect violations surfaced; then fix and retest).
- Route gaps → break `TRADE_PATH` and run `market:routes:suggest` (expect suggested hops).

## Gate to Live

- Require green test run and healthy StakeMaster heartbeats before enabling live.
- Use progressive-live first; only use enable-live with operator sign-off.

```bash
uv run pytest -q
uv run python apps/cli/main.py run:loop --progressive-live --sleep 15
```

## Artifacts to Check

- `logs/runtime.log` — price guard streaks, reflex halts, decisions.
- `db/agent_memory.jsonl` — portfolio telemetry, broker utilization, rationale fields.

## SQL Smoke Tests

- `scripts/docker_sql_multi_tenant_smoke.sh` and `scripts/replit_sql_multi_tenant_smoke.sh` are the canonical probes.

  They spin up three short-lived tenants with distinct DIEM quotas, record a chat sample for each, hammer `/v1/chat` via `limit_probe.py`, and snapshot counters plus tenant listings after expiries.

  Adjust the `TENANT_SPECS` array (e.g., keep only one entry) if you need a faster single-tenant smoke in CI.

- Review the per-tenant probe summaries, chat samples (`logs/sql-smoke/chat-samples/` or `/tmp/replit-sql-smoke/chat-samples/`), and counter snapshots to observe DIEM exhaustion, rate limits, and cleanup behavior.

## References

- Operations → `./OPERATIONS.md`
- Configuration → `./CONFIGURATION.md`
- Deployment → `./DEPLOYMENT.md`


