# Venice Capacity Broker

This repository hosts the production agent orchestrator, Capacity Broker API, and supporting services for the Venice VVV and DIEM stack.

It provides a single loop automation path for staking, DIEM arbitrage, and tenant key issuance together with operator tooling.

## Architecture Snapshot

StakeMaster, ArbiDiem, and CapacityBroker run inside `graph/workflows/orchestrator.py` with quorum voting, Reflex guardrails, and AI Treasurer guidance.

The Broker API (`apps/broker_api/app.py`) exposes tenant management, quotes, purchases, clearing, bids, and SSE streams while serving the `/admin` control panel.

Market data aggregates DEX quotes, Venice signals, and Etherscan discovery through `services/marketdata/provider.py`.

Services rely on SQLModel for durable data, KV stores for live limits, and structured telemetry from `libs/telemetry`.

## Repository Layout

- `apps/` contains the Broker API, control plane static assets, and CLI entry points.

- `agents/` implements StakeMaster, ArbiDiem, CapacityBroker, Quorum models, Reflex, and AI Treasurer.

- `services/` includes staking, DIEM, market data, keys, memory, and risk helpers.

- `graph/` stores orchestrator workflows and optional LangGraph integration.

- `libs/` holds shared runtime utilities such as telemetry, KV adapters, pricing, and AgentKit extensions.

- `tests/` exercises orchestrator, DIEM, DEX, broker, and risk behaviour.

## Environment Setup

Use Python 3.10+ and install dependencies with `uv sync --extra dev` or the pip fallback listed in `pyproject.toml`.

Copy `.env.example` to `.env`, then layer Docker overrides from `.env.docker` or `docker/.env.local` as needed.

Populate Venice credentials, Base RPC endpoints, token addresses, DEX routers, and broker settings referenced in `docs/AGENTS.md`.

Configure SQL (`SQL_DATABASE_URL`) and KV (`KV_URL`, `KV_API_TOKEN`) before enabling rate limits in shared environments.

## Common Commands

`uv run python apps/cli/main.py env:status` compares server configuration with local env files.

`uv run python apps/cli/main.py run:loop --sleep 15 --max-cycles 3` exercises the single loop orchestrator.

`uv run python apps/cli/main.py startup:probe` validates DEX routes and Venice metrics before live trading.

`make run-broker` starts the Broker API locally, while `make run-stack` adds the orchestrator and token watcher.

`make rotate-probe TENANT=t1` rotates or creates a tenant, probes chat, and compacts counters.

## Testing

Run `uv run pytest -q` to execute orchestrator, DIEM, market data, broker, and risk tests.

Targeted suites include `tests/test_single_loop_orchestrator.py`, `tests/test_diem_service.py`, `tests/test_dex_exact_out.py`, `tests/test_broker_limits.py`, and `tests/test_risk_policy.py`.

`make ci-gate` performs env validation, migrations, and smoke checks prior to deployment.

## Observability

`logs/runtime.log` collects orchestrator summaries, price guard activity, and broker events.

`/metrics`, `/health`, and `/v1/env` provide readiness indicators for the Broker API.

Set `DIEM_DEBUG_ROUTES`, `MARKETDATA_DEBUG_SANITY`, or `DIEM_FAKE_PRICE` when troubleshooting offline scenarios.

Enable `RISK_VOL_PERSIST` to persist price ticks into SQL for longer drift analysis.

## Documentation

**Start Here:**
- `ARCHITECTURE.md` - System architecture and component overview
- `README.md` - This file (quick start and commands)

**Core Documentation:**
- `AGENTS.md` - Agent catalog with contracts and run surfaces

**Implementation Plans:**
- `docs/implementation-plan-agents.md` - Agent implementation details
- `docs/implementation-plan-broker.md` - Broker implementation specifics

**Operational Guides:**
- `docs/DEPLOYMENT.md` - Deployment for Docker and Replit (includes Base addresses)
- `docs/agent-management.md` - Agent operation runbooks
- `docs/ADMIN.md` - Admin control panel guide

**Technical References:**
- `docs/venice-diem-tokenomics.md` - VVV/DIEM tokenomics
