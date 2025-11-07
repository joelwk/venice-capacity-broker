# Testing & QA

## Core Suites

- DEX paths & slippage: `tests/test_dex_exact_out.py`, `tests/test_dex_fot_fallback.py`, `tests/test_dex_exact_out_venues.py`.
- DIEM services: `tests/test_diem_service.py`, `tests/test_diem_buy_path.py`, `tests/test_diem_mint_burn_dryrun.py`.
- Risk policy: `tests/test_risk_policy.py`, `tests/test_arbi_diem_risk_integration.py`.
- Broker limits & idempotency: `tests/test_broker_limits.py`, `tests/test_cli_idempotency_purge.py`.
- Market-data normalization: `tests/test_marketdata_prices.py`.

## Orchestrator & Probes

```bash
uv run python apps/cli/main.py run:loop --dry-run --max-cycles 1
uv run python apps/cli/main.py startup:probe --check-live
uv run python apps/cli/main.py quotes:preview --units 1.0
uv run python apps/cli/main.py market:best-price:scan --start 1.0 --min 1e-12 --factor 10
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

## References

- Operations → `./OPERATIONS.md`
- Configuration → `./CONFIGURATION.md`
- Deployment → `./DEPLOYMENT.md`


