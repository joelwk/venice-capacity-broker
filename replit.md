# Venice Capacity Broker

A Python/FastAPI service that sells scoped Venice AI inference credits for crypto on the Base L2 chain. It prices DIEM credits, verifies Base-chain treasury payments, and issues scoped Venice API subkeys.

## Stack

- **Runtime:** Python 3.10+, managed with `uv`
- **API:** FastAPI / Uvicorn on port 8000
- **Agents:** StakeMaster, ArbiDiem, CapacityBroker, quorum/reflex, treasury orchestrator
- **DB:** PostgreSQL via SQLModel + Alembic migrations
- **KV:** Redis (or Replit KV in Replit environment)
- **Blockchain:** Base L2 via Web3/eth-account; Coinbase AgentKit/CDP SDK

## How to Run on Replit

Two modes are configured as workflows:

| Workflow | Command | When to use |
|---|---|---|
| **Run Venice Stack (dry)** | `bash scripts/replit_run.sh dry` | Development / testing — no real crypto transactions |
| **Run Venice Stack (live)** | `bash scripts/replit_run.sh live` | Production — real Base L2 and Venice API |

All secrets are managed via Replit Secrets (see `.env.example` for the full list).

## Deployment

Deployment target: **Reserved VM (GCE)**

Build command unsets `UV_PROJECT_ENVIRONMENT` and `VIRTUAL_ENV` before `uv sync` so that `uv` creates a proper `.venv` in the deployment container (rather than attempting to reuse `.pythonlibs`, which has no Python executable in the GCE environment).

Run command: `bash scripts/replit_run.sh live`

## Key Entry Points

- **API app:** `apps/broker_api/app.py` (ASGI object: `app`)
- **CLI:** `apps/cli/main.py`
- **Orchestrator / agent stack:** `graph/`, `agents/`, `services/`
- **Run script:** `scripts/replit_run.sh`

## User Preferences

- Keep project's existing Python/uv structure; do not migrate to other package managers.
