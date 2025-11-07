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
uv run python apps/cli/main.py quotes:preview --units 1.0
```

- Adjust `TRADE_PATH`, router envs, or token addresses; confirm with `quotes:preview`.

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

- Switch `BASE_RPC_URL` to a healthy endpoint.
- Increase `RPC_REQUEST_TIMEOUT_SECONDS`.

## Guardrails Blocked Actions

- Check `logs/runtime.log` for price guard streaks and Reflex reasons.
- Loosen temporarily only for debugging: `MARKETDATA_PRICE_SANITY_MAX_DRIFT`, `REFLEX_*` (revert after).

## References

- Configuration → `./CONFIGURATION.md`
- Deployment → `./DEPLOYMENT.md`
- Operations → `./OPERATIONS.md`


