# Admin Control Panel

The Broker API serves a static operations console under `/admin`.

It complements CLI helpers and should be the first stop after every deploy.

## Routine Operator Flow

1. Load `/admin`, paste `BROKER_ADMIN_TOKEN`, and verify the Health, Venice, and Env cards.

2. Use the Chat Probe card or `make rotate-probe TENANT=t1` to confirm tenant rotation and limiter persistence.

3. Review Venice signals, price snapshots, and orchestrator status in the dashboard before enabling live trades.

4. Check `/metrics` and `logs/runtime.log` when anomalies surface.

## Features

Tenants: list, create, rotate, revoke, inspect, and edit broker limits through the UI or `/v1/tenants` endpoints.

Self service: scoped keys can call `/v1/me`, `/v1/me/usage`, and `/v1/me/broker-limits` to manage within policy bounds.

Venice: the card surfaces readiness, recent signals, and includes an inline OpenAPI probe.

Buyer flow: `/admin/buy.html` walks through quote retrieval, payment details, verification, and key delivery with SSE updates.

Telemetry: cards display recent `signal.market.*` events and price sanity notes mirrored from the orchestrator.

## CLI Complements

`make run-stack` restarts the Broker, orchestrator, and watcher together.

`uv run python apps/cli/main.py run:loop --sleep 15 --max-cycles 0` simulates a full cycle for demos.

`make db-counters TENANT=t1` and `make server-db-compact` examine limiter history from the shell.

## Security Notes

Set `BROKER_REQUIRE_ADMIN_TOKEN=true` in all non development environments.

The UI stores the token in `localStorage`; clear it after each session.

Use `AGENTS_PAUSED=true` to hold live actions while preserving API availability.

## References

Broker endpoint contracts live in `docs/implementation-plan-broker.md`.

Operational guardrails are covered in `docs/implementation-plan-agents.md`.

Deployment specifics are documented in `docs/DEPLOYMENT.md`.
