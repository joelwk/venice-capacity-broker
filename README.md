# Venice Capacity Broker

Buy scoped [Venice AI](https://venice.ai) inference credits with crypto, and run the agents that stake VVV, mint or burn DIEM, and meter tenant API keys.

Live buyer page: [venice-capacity-broker.replit.app/buy.html](https://venice-capacity-broker.replit.app/buy.html)

## Frontend preview

<video src="assets/FrontEndPreview.mp4" controls width="100%" playsinline>
  <a href="assets/FrontEndPreview.mp4">Watch the frontend preview</a>
</video>

## What it does

A FastAPI broker prices DIEM credits, verifies Base-chain payments to a treasury address, and issues a scoped Venice sub-key with `consumptionLimit` and `expiresAt`.

A single-loop orchestrator runs StakeMaster, a quorum-gated ArbiDiem trader, CapacityBroker, and AI Treasurer guidance.

## Layout

- `apps/broker_api` — public HTTP API and purchase flow
- `apps/control-plane` — static buy page and admin UI
- `apps/cli` — operator CLI (`vvv-agents`)
- `agents/` — StakeMaster, ArbiDiem, CapacityBroker, quorum, reflex
- `services/` — staking, DIEM, market data, keys, risk
- `graph/` — orchestrator loop
- `docs/` — configuration, deployment, operations

## Quick start

Python 3.10+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --extra dev --extra broker
cp .env.example .env
```

Set at least `VENICE_API_KEY` (or `VENICE_PARENT_KEY`), `TREASURY_ADDRESS`, `BASE_RPC_URL`, and `SQL_DATABASE_URL` in `.env` or your host secrets. Never commit `.env` or `.env.local`.

```bash
uv run uvicorn apps.broker_api.app:app --reload --port 8000
uv run pytest tests/test_purchase_security.py tests/test_buyer_e2e.py -q
```

`VENICE_API_BASE_URL` must include `/api/v1` (example: `https://api.venice.ai/api/v1`).

## Deploy

Docker: `docker compose up` (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [docs/DOCKER_DEPLOYMENT.md](docs/DOCKER_DEPLOYMENT.md)).

Replit: [docs/REPLIT_DEPLOYMENT.md](docs/REPLIT_DEPLOYMENT.md). Store keys in Replit Secrets, not files.

## Buy flow

1. `GET /v1/quotes` returns a priced quote (persisted synchronously).
2. The buyer pays the treasury on Base.
3. `GET /v1/purchases/challenge` plus a wallet signature prove ownership.
4. `POST /v1/purchases/verify` checks chain id, confirmations, token, and amount, then mints a scoped key.

Unauthenticated status endpoints never return the API key.

## License

MIT. See [LICENSE](LICENSE).
