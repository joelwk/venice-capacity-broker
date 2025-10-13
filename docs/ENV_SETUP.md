# Compose Environment Setup

Use layered env files instead of `local.json`.  
Keep shared configuration in `.env`, Docker overrides in `.env.docker`, and rely on Replit’s secrets store when running in Replit.

## Convert `local.json`

One-time conversion from the legacy Replit export:

```powershell
(Get-Content local.json | ConvertFrom-Json).psobject.Properties |
  ForEach-Object { "$($_.Name)=$($_.Value)" } | Set-Content -Encoding ascii .env
```

The equivalent Bash command is:

```bash
jq -r 'to_entries|.[]|"\(.key)=\(.value)"' local.json > .env
```

Open the new `.env` and remove `KV_URL`, `KV_API_TOKEN`, and `REPLIT_DB_URL`.  
Keep Docker-friendly values and add `BROKER_ADMIN_TOKEN`, `VENICE_API_BASE_URL`, `VENICE_PARENT_KEY`, `REDIS_URL`, and `SQL_DATABASE_URL`.

Create `.env.docker` from this minimal template:  
Only put Docker-specific tweaks or secrets that are not already covered by Replit’s secrets store.  
Compose now loads both files (`.env` first, then `.env.docker`) so overrides in `.env.docker` win inside containers.

## Recommended Compose defaults

Create `.env.docker` and adjust secrets needed for Docker runs.  
Compose declares `env_file: [.env, .env.docker]`, so both files are injected into the services’ environment.

```
BROKER_ADMIN_TOKEN=change-me
VENICE_API_BASE_URL=https://api.venice.ai/api/v1
VENICE_PARENT_KEY=
SQL_DATABASE_URL=postgresql+psycopg2://postgres:postgres@postgres:5432/postgres
REDIS_URL=redis://redis:6379/0
```

Optional helpers:

```
VENICE_API_KEY=
ETHERSCAN_API_KEY=
BASE_RPC_URL=https://mainnet.base.org
BASE_CHAIN_ID=8453
```

If you run orchestrator or wallet helpers locally, add the live-only secrets block (wallet keys, CDP credentials, session secret).  
Third-party API keys (OpenAI, Twitter, etc.) should also live in `.env.docker` when you need them outside Replit.

After editing `.env`, run:

```
docker compose --env-file .env up -d --build
```
