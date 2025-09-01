# VVV Agents Monorepo

Proprietary notice: This repository is private and not open source. All rights reserved. See LICENSE and NOTICE for details.

A scaffolded framework for a multi-agent system integrating LangGraph/LangChain orchestration with Coinbase AgentKit and Venice AI. It follows the implementation blueprint in `implementation-plan` and establishes a clean structure to build wallet, staking, DIEM, autonomous key issuance, and brokered capacity services.

## Structure

```
apps/
  broker-api/        # Capacity Broker API (stub)
  control-plane/     # Admin UI placeholder
  cli/               # Operator CLI (argparse-based)
services/
  wallet/            # Wallet providers (Smart Wallet + ETH account)
  staking/           # VVV staking client (stubs)
  diem/              # DIEM mint/burn/trade (stubs)
  venice_keys/       # Venice API key issuance manager
  marketdata/        # Price/quotes provider (stub)
  risk/              # Simple risk budget checks
agents/
  stake_master/      # Keeps staking optimal
  arbi_diem/         # DIEM arbitrage executor
  capacity_broker/   # Issues scoped keys and allocates capacity
  ai_treasurer/      # Treasury policies for VVV/DIEM
  quorum/            # Quorum voting + listen interval policy
graph/
  nodes/             # Node functions (observe/decide/execute)
  workflows/         # High-level workflows (composable)
libs/
  venice_sdk/        # Thin Venice client (autonomous key flow)
  agentkit_ext/      # AgentKit action wrappers (stubs)
  pricing/           # DIEM fair value helpers
  telemetry/         # Logging and basic tracing
infra/
  docker/            # Dockerfile placeholders
  k8s/               # K8s placeholders
  terraform/         # IaC placeholders
config/
  default.yml        # Central app configuration template
tests/               # Minimal import sanity tests
```

## Quickstart

- Python 3.10+
- Copy `.env.example` to `.env` and fill values as you integrate real services.
- Dependency management uses `uv` with `pyproject.toml` (requirements.txt remains for compatibility).

Install with uv (recommended):

```
curl -LsSf https://astral.sh/uv/install.sh | sh
uv venv
uv sync --extra dev
```

Or with pip (compat mode):

```
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Optional dependencies (install via extras as you go):

```
# Database + KV + tracing
uv sync --extra db --extra kv --extra tracing
# Web3 + AgentKit
uv sync --extra web3 --extra agentkit
# LangGraph/LangChain
uv sync --extra graph
```
On-chain and wallets:
- `coinbase-agentkit` + `cdp-sdk` integrate CDP Smart Wallet (gasless) and Eth-account providers.
- Configure one of:
  - Dev EOA: set `WALLET_PROVIDER=eth_account`, `ETH_PRIVATE_KEY`, `BASE_RPC_URL`, `BASE_CHAIN_ID`.
  - Smart Wallet: set `WALLET_PROVIDER=smart_wallet`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`, `OWNER`, `NETWORK_ID` (base-mainnet|base-sepolia), optional `PAYMASTER_URL`.
  - Base gating enforced: only base-mainnet or base-sepolia are allowed.
To enable on-chain calls: set `BASE_RPC_URL` and provide ABIs in `abi/`.

Run the CLI help:

```
python apps/cli/main.py --help
```

Run the sample workflow (dry run):

```
python apps/cli/main.py run:quorum --dry-run
```

Broker API (requires FastAPI + Uvicorn):

```
uv run uvicorn app:app --app-dir apps/broker-api --reload
```

API index & docs:
- Visit `/` for a small HTML index with links to `/docs` (Swagger UI), `/redoc`, `/health`, and `/metrics`.
- Admin endpoints require `Authorization: Bearer <BROKER_ADMIN_TOKEN>`.

## Docker Compose (Postgres + Redis)

Spin up Postgres and Redis locally for full E2E validation (SQL store + Redis-backed limiter):

```
docker compose up -d

# Configure app env to point to services
export SQL_DATABASE_URL=postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/postgres
export REDIS_URL=redis://127.0.0.1:6379/0

# Run tests against services
uv run pytest -q

# Or run the API
uv run uvicorn app:app --app-dir apps/broker-api --reload
```

Stop services:

```
docker compose down -v
```

Notes:
- The application still runs fine without Docker: tests fall back to SQLite; Redis-dependent tests skip if `REDIS_URL` is unset.
- Use `BROKER_STORE_BACKEND=sql` to force the SQL tenant store if validating DB paths.

Idempotency TTL
- Canonical env is `IDEMPOTENCY_TTL_SECONDS` (default 300). For compatibility, the app also reads `IDEM_TTL_SECONDS`.
- Idempotency applies to `POST /v1/chat` requests using a hash of method+path+body+Idempotency-Key, with a per-tenant TTL window.
```

Notes:
- Entry points add the repo root to `sys.path` for simple local imports.
- On-chain calls now use AgentKit wallet providers underneath; configure env and ABIs:
  - `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`.
  - DEX: dual providers supported (Uniswap V2 and Aerodrome). Set:
    - `DEX_PROVIDERS=uniswap_v2,aerodrome`
    - `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, and optional `AERODROME_STABLE`
    - Legacy `ROUTER_ADDRESS` remains as a fallback for Uniswap V2
  - ABIs: `abi/erc20.json` (provided), plus project-specific `abi/staking.json`, `abi/diem.json`.
  - Trading supports:
    - Uniswap V2 router ABI (`abi/uniswap_v2_router.json`) with `TRADE_PATH`
    - Aerodrome router ABI (`abi/aerodrome_router.json`) single-hop with `AERODROME_STABLE`
- Venice client now performs real HTTP requests; endpoints are configurable via env.
- Broker store backend:
  - Default stores tenants in JSON at `apps/broker-api/tenants.json`.
  - To use SQL, set `BROKER_STORE_BACKEND=sql` and configure `SQL_DATABASE_URL` (or POSTGRES_* envs). Install `sqlmodel` and a DB driver like `psycopg2-binary`.
- Broker limits (admin):
  - View: `GET /v1/tenants/{tenantId}/broker-limits` (requires admin bearer).
  - Set: `POST /v1/tenants/{tenantId}/broker-limits` with `{ "windowSeconds": 60, "maxRequests": 120, "label": "premium" }`.
  - Enforced by `/v1/chat` in addition to global `RATE_LIMIT_*` defaults.

CLI admin helpers:
- List tenants: `python apps/cli/main.py broker:tenants:list`
- Get tenant limits: `python apps/cli/main.py broker:limits:get --tenant <id>`
- Set tenant limits: `python apps/cli/main.py broker:limits:set --tenant <id> [--window N] [--max N] [--label name]`
  - Uses `BROKER_BASE_URL` or `BROKER_API_HOST`/`BROKER_API_PORT` and `BROKER_ADMIN_TOKEN` for auth.
- Keep secrets in `.env` and never commit them.
 - Revoke tenant key: `python apps/cli/main.py broker:tenants:revoke --tenant <id>`
 - Clean up Venice keys by description prefix: `python apps/cli/main.py venice:keys:cleanup --prefix "T1" [--dry-run]`

Rate limits and KV:
- KV client supports Replit DB style HTTP (`KV_URL`) and optional Redis (`REDIS_URL`).
- Limiter uses a fixed-window counter with atomic `INCR` when Redis is configured; otherwise best‑effort.

LangSmith tracing:
- Enable by setting `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_API_KEY`.
- Optional `LANGCHAIN_PROJECT` for grouping runs. The LangGraph pipeline run is wrapped for tracing when enabled.

## Configuration

See `config/default.yml` and `.env.example` for environment variables and defaults.

URLs & env vars:
- `BASE_URL`: shell convenience used in examples to hold your API base URL (not read by the app).
- `BROKER_BASE_URL`: full base URL used by the CLI to reach the Broker API. If set, the CLI uses this directly (recommended for Replit).
- `BROKER_API_HOST` / `BROKER_API_PORT`: CLI fallback to construct a URL when `BROKER_BASE_URL` is not set (e.g., `127.0.0.1:8000`). The server does not read these.
- `BASE_RPC_URL`: on-chain Base RPC endpoint for Web3/AgentKit (e.g., `https://mainnet.base.org` or `https://sepolia.base.org`). Unrelated to the Broker API URL.

### Venice API (env pitfalls)

- **`VENICE_API_BASE_URL` selection**: choose with or without `/api` using the OpenAPI spec.
  - Try `GET ${BASE_URL%/}/openapi.json`. If this returns 200, start with `VENICE_API_BASE_URL=${BASE_URL%/}` unless the spec's `servers[0].url` contains `/api`, in which case use `${BASE_URL%/}/api`.
  - If `GET ${BASE_URL%/}/openapi.json` is 404 but `GET ${BASE_URL%/}/api/openapi.json` works, use `VENICE_API_BASE_URL=${BASE_URL%/}/api`.
- **Parent key for subkeys**: set `VENICE_PARENT_KEY` to a parent/root inference key that has permission to create sub-keys. A regular `VENICE_API_KEY` without parent privileges will fail when calling `/v1/keys/sub` (typically 401/403). Obtain a parent key via the wallet challenge flow or your Venice admin UI. The broker will fall back to `VENICE_API_KEY` if `VENICE_PARENT_KEY` is unset, but it must be parent-capable.
- **Avoid double slashes**:
  - Shell: use `${BASE_URL%/}` to trim a trailing `/` before appending paths.
  - Code: trim trailing slashes, e.g., Python .rstrip('/'); JS .replace(/\/+$/, '').

Additional notes for official API base (`https://api.venice.ai/api/v1`):
- Set `VENICE_API_BASE_URL=https://api.venice.ai/api/v1`.
- Subkey/key creation defaults to `POST /api_keys` with `Authorization: Bearer <VENICE_PARENT_KEY>` and body including `apiKeyType` and `consumptionLimit`. If your deployment differs, override `VENICE_CREATE_SUBKEY_PATH`.
- Use `VENICE_API_KEY_TYPE=INFERENCE` (or `ADMIN` if required). Some deployments reject `READ_ONLY` with a 400 invalid enum error.

Quick probes and examples:

```
# Pick your host, then probe OpenAPI location
BASE_URL="https://api.venice.ai"   # or your self-hosted domain
curl -fsSL "${BASE_URL%/}/openapi.json" || curl -fsSL "${BASE_URL%/}/api/openapi.json"

# Example sub-key creation (requires parent key)
curl -sS -H "Authorization: Bearer $VENICE_PARENT_KEY" \
  -H "Content-Type: application/json" \
  -X POST "${BASE_URL%/}/api_keys" \
  -d '{"apiKeyType":"INFERENCE","consumptionLimit":{"diem":10},"description":"tenant-1"}'

# Example web3 root key (wallet-signed)
# 1) Request a challenge (some deployments support POST; others may use GET)
ADDRESS=0xYourWallet
curl -sS -X POST "${BASE_URL%/}/api_keys/generate_web3_key" \
  -H "Content-Type: application/json" \
  -d '{"wallet":"'"$ADDRESS"'"}'

# 2) Sign the returned message/challenge off-chain -> SIG=0x...
# 3) Exchange signature for a root inference key with optional limits
curl -sS -X POST "${BASE_URL%/}/api_keys/generate_web3_key" \
  -H "Content-Type: application/json" \
  -d '{"address":"'"$ADDRESS"'","signature":"'"$SIG"'","apiKeyType":"INFERENCE","consumptionLimit":{"diem":10}}'
```

Broker wrappers (admin):
- `POST /v1/venice/web3/challenge` with `{ "wallet": "0x..." }` returns a signable challenge payload.
- `POST /v1/venice/web3/create-root-key` with `{ "address": "0x...", "signature": "0x...", "apiKeyType": "INFERENCE", "consumptionLimit": {"diem": 10} }` creates a root key.
- `POST /v1/venice/subkey` with `{ "label": "tenant-1", "consumptionLimit": {"diem": 10}, "expiresAt": "2025-12-31T23:59:00Z" }` creates a scoped subkey using a parent key from env (or `parentKey` if provided).
Tenants & keys
- Create a tenant (idempotent): `POST /v1/tenants` (requires `BROKER_ADMIN_TOKEN` and `VENICE_PARENT_KEY`). If the tenant exists, it is returned without minting a new key.
- Rotate key: `POST /v1/tenants?rotate=true` to mint a fresh subkey and update the store. Preserve existing quota/expiry unless you pass overrides in the body.
- Rotate + revoke old: `POST /v1/tenants?rotate=true&revoke_old=true` will, after a successful rotate, attempt to delete the old key via Venice if `key_id` is recorded.
- Revoke tenant key: `POST /v1/tenants/{tenantId}/revoke` attempts a Venice `DELETE /api_keys/{id}` if available, and marks the tenant as revoked.

SQL backend quick smoke (local or Replit SQL)
- Set `SQL_DATABASE_URL` or `DATABASE_URL` to a Postgres connection string.
- Optionally run migrations: `uv run alembic upgrade head` (tables will auto-create on first use otherwise).
- Start API with `BROKER_STORE_BACKEND=sql`.
- Create a tenant via `POST /v1/tenants` (requires `BROKER_ADMIN_TOKEN` and `VENICE_PARENT_KEY`).
- Compact counters (if using the limiter): `uv run python apps/cli/main.py data:compact-counters --force`.
- Inspect via admin endpoint: `GET /v1/debug/counters?tenant_id=<id>&limit=20` or CLI `counters:show`.

## Cheat Sheet (Broker + CLI)

Create a tenant (admin-only):

```
curl -sS -X POST "${BROKER_BASE_URL%/}/v1/tenants" \
  -H "Authorization: Bearer $BROKER_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "tenant_id": "t1",
        "label": "Team A",
        "quota": null,
        "expires_at": "2025-12-31T23:59:00Z"
      }'
```

List tenants and limits (CLI):

```
python apps/cli/main.py broker:tenants:list
python apps/cli/main.py broker:limits:get --tenant t1
```

Proxy chat via the broker (using tenant subkey):

```
export SUBKEY=<tenant-subkey>
curl -sS -H "Authorization: Bearer $SUBKEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}]}' \
  "${BROKER_BASE_URL%/}/v1/chat"
```

Counters and compaction:

```
# Admin-only debug counters
curl -sS -H "Authorization: Bearer $BROKER_ADMIN_TOKEN" \
  "${BROKER_BASE_URL%/}/v1/debug/counters?tenant_id=t1&limit=20"

# CLI equivalents
python apps/cli/main.py counters:show --tenant t1 --scope chat --limit 20 --json
python apps/cli/main.py data:compact-counters --force
```

Probe Venice OpenAPI and print recommended env exports:

```
python apps/cli/main.py venice:probe-openapi --base-url https://api.venice.ai
```

## Troubleshooting

- uv not found: add `~/.local/bin` to PATH, e.g., `export PATH="$HOME/.local/bin:$PATH"`.
- Dev dependencies: if `uv sync` fails, include `--extra dev` to pull test/stub deps.
- Replit PATH quirks: use `export PATH` each session; ignore `.bashrc` warnings.
- Test import errors (`sqlmodel`): ensure stubs load before `db.models` (see `tests/test_admin_counters_endpoint.py`).
- 404/401 creating tenants: verify `VENICE_API_BASE_URL` and `VENICE_PARENT_KEY` (must have sub-key privileges).
- Double slashes in URLs: use `${BASE_URL%/}` in shell and strip trailing slashes in code.
- Debug-only: you can seed `apps/broker-api/tenants.json` with a known key to bypass sub-key creation temporarily (never in production).

## License

Proprietary. Do not distribute.
Addresses on Base:
- Defaults target Base mainnet (`NETWORK_ID=base-mainnet`). Prefilled values in `.env.example` and `config/addresses.base-mainnet.yml`:
  - `VVV_TOKEN_ADDRESS`: 0xacfE6019Ed1A7Dc6f7B508C02d1b04ec88cC21bf
  - `VVV_STAKING_ADDRESS`: 0x321b7ff75154472B18EDb199033fF4D116F340Ff
  - `DIEM_TOKEN_ADDRESS`: 0xF4d861575ecc9493420A3f5a14F85B13f0b50EB3
  - `UNISWAP_V2_ROUTER_ADDRESS`: 0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24
  - `AERODROME_ROUTER_ADDRESS`: 0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5
  - `AERODROME_STABLE`: false (DIEM/USDC volatile pool)

Offline signing test:
- Run a local end-to-end signing test with no network calls:

```
python apps/cli/main.py test:challenge-offline
```

It generates an ephemeral wallet, signs a dummy Venice challenge, and echoes the payload as the Venice client would receive it.

Compare DEX quotes (live, uses configured providers and `TRADE_PATH`):

```
python apps/cli/main.py quotes:compare --amount 1000000
```

This prints quotes from Uniswap V2 and Aerodrome (if both routers are set) and highlights the best output. Set `TRADE_PATH` to comma-separated addresses (e.g., `DIEM,USDC`).

## Replit

- Fully uses `uv` (pip fallback removed). The runner auto-installs `uv` if missing.
- Steps: import to Replit, add Secrets from `.env.example`, click Run.
  - Use the public Replit URL (e.g., `https://<id>.worf.replit.dev`) in your browser and for curl.
  - `/` shows the HTML index; `/docs` opens Swagger UI; `/health` returns `{"status":"ok"}`.
- Recommended Secrets for Replit:
  - `BROKER_BASE_URL` = your public Replit URL
  - `BROKER_ADMIN_TOKEN` = strong random
  - `VENICE_PARENT_KEY` (or `VENICE_API_KEY`)
  - On-chain (optional): `BASE_RPC_URL`, `NETWORK_ID` (base-sepolia|base-mainnet); for Smart Wallet also set `WALLET_PROVIDER=smart_wallet`, `CDP_API_KEY_ID`, `CDP_API_KEY_SECRET`, `CDP_WALLET_SECRET`, `OWNER`.
- Optional extras: set `UV_EXTRAS` to space-separated extras before first run, e.g. `UV_EXTRAS="dev db kv"` or `UV_EXTRAS="web3 agentkit"`.
  - Under the hood, the runner executes `uv sync --extra <each>` then starts Uvicorn via `uv run`.
- Deployments: create a Web Service from the Deployments panel (details in `infra/replit/README.md`).
  - For Replit Cloud Services: SQL Database, set `SQL_DATABASE_URL` or `DATABASE_URL` from the service credentials. For KV, set `REPLIT_DB_URL`.
 
## Operational Notes

Rate limiting
- Validate ceilings with the built-in probe: `python apps/cli/main.py probe:limits [--auth-bearer <subkey> | --tenant <id>] [--rps N] [--duration S] [--concurrency M]`.
- Recommended starting defaults:
  - KV-only (Replit DB / in-memory): `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_MAX_REQUESTS=60` (~1 req/sec per tenant)
  - Redis-backed (`REDIS_URL` set): `RATE_LIMIT_WINDOW_SECONDS=60`, `RATE_LIMIT_MAX_REQUESTS=120` (tune via probe).

Idempotency
- Key format: `idem:{scope}:{tenant_id}:{digest}:{epoch_min}` with TTL window.
- TTL env: `IDEMPOTENCY_TTL_SECONDS` (alias `IDEM_TTL_SECONDS` supported), default 300s.
- Duplicate POST `/v1/chat` within TTL returns 409 and `X-Idempotency-Accepted: false`.
- Admin cleanup: `python apps/cli/main.py idem:purge --prefix idem:chat:<tenantId>` prints a summary and deletes matching keys.

### Target SLOs

- p90 latency: under 200 ms at target `ok_rps` measured by `scripts/limit_probe.py`.
- 429 fraction: below 10% during sustained probe at target `ok_rps` (tune `RATE_LIMIT_*` and per-tenant limits accordingly).
- Error rate: < 1% non-429 errors during probe.
- Metrics coverage: `/metrics` exposes request counters and latency; add alerts if p90 > 200 ms or 429 fraction > 10% for 5m.
