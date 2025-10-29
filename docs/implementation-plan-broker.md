# Implementation Plan: Broker

This plan documents the Capacity Broker API and supporting services that currently live in the repository.

It replaces legacy orchestration notes and reflects `apps/broker-api/app.py`, the CLI helpers, and the market data stack.

## Scope

Cover tenant administration, quotes and purchases, payment verification, clearing features, and Venice key issuance.

Outline storage, telemetry, and configuration so engineers can extend the broker without guesswork.

Agent automation specifics remain in `docs/implementation-plan-agents.md`.

## System Snapshot

The Broker API is a Starlette application served from `apps/broker-api/app.py`.

It mounts the static admin UI under `/admin`, exposes JSON endpoints, and authenticates with `BROKER_ADMIN_TOKEN`.

Persistent data uses SQLModel tables (`db/models.py`) while rate limits rely on KV backends with SQL compaction.

Make targets invoke the API, orchestrator, and market watchers together for local runs.

## Tenant and Admin Surfaces

Tenants are persisted via SQL and mirrored into KV for rate limit enforcement.

Admin endpoints include `GET/POST /v1/tenants`, rotation helpers, and broker limit management.

Tenant self service mirrors the admin contract with the `/v1/me/*` endpoints guarded by scoped keys.

`make rotate-probe` demonstrates the full create, rotate, probe flow and should stay green in CI.

## Quotes and Pricing

`/v1/quotes` supports DIEM pricing in ETH or USDC with optional `units` or `budget` parameters.

Pricing is configurable through static rates or the live `market` engine fed by `services/marketdata/provider.py`.

Routes default to `TRADE_PATH` but can be overridden per request when exact out support exists.

Cache behaviour follows `BROKER_PRICES_TTL_SECONDS` and DEX timeouts such as `DEX_AGGREGATE_TIMEOUT_SECONDS`.

`services/marketdata/token_watcher.py` backfills pool discovery and should run periodically (`make watch-tokens`).

## Purchases and Verification

`POST /v1/purchases/verify` checks Base transactions, enforces accepted assets, and emits receipts.

Verification flows use `ACCEPT_ASSETS`, treasury routing, and ERC-20 metadata from the environment.

`GET /v1/purchases/{id}` and the SSE variant stream status updates for front end consumers.

Purchases persist payload, quote snapshot, tx hash, and Venice issuance metadata in SQL.

## Venice Key Issuance

`services/venice_keys/manager.py` issues scoped sub keys with required `consumptionLimit` and `expiresAt`.

The broker verifies Venice readiness at startup and refuses to fulfill purchases when the parent key is missing.

`venice:keys:cleanup` in the CLI provides a periodic hygiene sweep to revoke unused sub keys.

## Clearing, Bids, and Settlement (Optional)

`CLEARING_ENABLED` exposes `/v1/pricing/clearing_price` plus an SSE stream for dashboards.

`BIDS_ENABLED` adds EIP-712 bid submission and polling, and `SETTLEMENT_ENABLED` attaches the settlement quote flow.

Exact out previews currently support Uniswap V2 pools; Aerodrome remains opt in through manual paths.

## Storage and Instrumentation

SQLModel tables capture purchases, counters, price ticks, and reflections when configured.

KV backends (Redis or Replit DB) handle live rate limits and rolling windows.

`libs.telemetry.logger` records structured events, and optional metrics exporters surface Prometheus counters.

`logs/runtime.log` includes annotated orchestrator and broker events for operator review.

## Configuration Checklist

Core server settings:

- `BROKER_ADMIN_TOKEN`, `BROKER_REQUIRE_ADMIN_TOKEN`, and `BROKER_DEFAULT_MODEL`.

- `VENICE_API_BASE_URL`, `VENICE_PARENT_KEY`, and optional path overrides.

- Database URLs (`SQL_DATABASE_URL`) and KV connectors (`KV_URL`, `KV_API_TOKEN`).

Quotes and payments:

- `QUOTES_ENABLED`, `PURCHASES_ENABLED`, `PRICE_ENGINE`, and `ACCEPT_ASSETS`.

- `TREASURY_ADDRESS`, `PRICE_UNIT_USDC`, `PRICE_UNIT_ETH_WEI`, and `PRICE_ACCEPTED_MIN_UNITS`.

- DEX parameters such as `DEX_PROVIDERS`, `DEX_MAX_WORKERS`, and `RPC_REQUEST_TIMEOUT_SECONDS`.

Advanced features:

- `CLEARING_ENABLED`, `CLEARING_BAND_BPS`, and `CLEARING_SSE_INTERVAL_SECONDS`.

- `BIDS_ENABLED`, `SETTLEMENT_ENABLED`, `SIGN_DOMAIN_NAME`, and `SIGN_DOMAIN_VERSION`.

- `CORS_ENABLED` and `CORS_ALLOW_ORIGINS` for hosted buyer flows.

## Implementation Roadmap

Phase 1 validates core tenant CRUD, rate limits, and scoped key issuance against Venice.

Phase 2 finalizes quotes and purchases with live DEX pricing and Base transaction verification.

Phase 3 enables optional clearing bands, bids, and settlement once demand exists.

Phase 4 introduces analytics hardening: SQL compaction, telemetry dashboards, and abuse heuristics.

## Testing and Validation

`uv run pytest -q` covers broker endpoints, DIEM services, market data, and rate limit behaviour.

Key suites include `tests/test_broker_limits.py`, `tests/test_cli_idempotency_purge.py`, `tests/test_diem_service.py`, and `tests/test_marketdata_prices.py`.

`make ci-gate` runs the environment gate, migrations, and smoke checks before deployments.

Use `uv run python apps/cli/main.py market:pools:watch --once` after migrations to backfill DEX data.

## Operational Practices

`make run-broker` starts the API for local work; `make run-stack` adds the orchestrator and token watcher.

`uv run python apps/cli/main.py env:status` compares server reported env with local `.env` files.

`make server-db-compact` moves KV counters into SQL for long term retention.

Monitor `/metrics`, `/health`, and `/v1/env` after each deploy to confirm readiness.

## References

Deployment steps are detailed in `docs/DEPLOYMENT.md`.

Token and routing context lives in `docs/venice-diem-tokenomics.md` and `docs/EtherScan.md`.

Agent coordination details remain in `docs/implementation-plan-agents.md`.
