## Agents Catalog (v1)

This document describes the production v1 agents, their responsibilities, dependencies, environment requirements, and how to run/test them. It reflects the final v1 scope defined in `implementation-plan-v2.md`. If any detail conflicts with the original plan, resolve in favor of `implementation-plan.md` (source of truth for boundaries).

### Source of truth and scope
- Final v1 scope: see `implementation-plan-v2.md` → “v1 Scope (Stop Line)”
- Planning baseline and tie-breaker: `implementation-plan.md`
- Venice API configuration rules: `.cursor/rules/venice-api-config.mdc`

## WRITING STYLE
- Each long sentence should be followed by **two newline characters**.
- Avoid long bullet lists.
- Write in **natural, plain English**. Be conversational.
- Avoid overly complex language and super-long sentences.
- Use **simple & easy-to-understand language**. Be concise.

## Shared prerequisites

### Environment
- Venice API
  - `VENICE_API_BASE_URL=https://api.venice.ai/api/v1` (must include `/api/v1`)
  - `VENICE_API_KEY` (inference key for models/signals)
  - Optional overrides when your deployment differs (see `.cursor/rules/venice-api-config.mdc`):
    - Legacy aggregate: `VENICE_VVV_PATH=/vvv`
    - Preferred metrics: `VENICE_VVV_CIRC_PATH=/vvv/circulatingsupply`, `VENICE_VVV_UTIL_PATH=/vvv/utilization`, `VENICE_VVV_YIELD_PATH=/vvv/staking_yield`
    - Key endpoints: `VENICE_CREATE_SUBKEY_PATH`, `VENICE_CREATE_ROOT_PATH`, `VENICE_CHALLENGE_PATH`, `VENICE_REVOKE_KEY_PATH`
- Base / on-chain
  - `BASE_RPC_URL`, `BASE_CHAIN_ID`
  - Contract addresses: `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`
- DEX configuration
  - `DEX_PROVIDERS=uniswap_v2,aerodrome`
  - Router addresses: `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, `AERODROME_STABLE`
  - Pricing: `QUOTE_TOKEN_ADDRESS`, optional `TRADE_PATH` for DIEM pricing

### DIEM & Risk configuration (new)
- DIEM mint/burn capacity gate (optional):
  - `DIEM_ENABLE_SVVV_GATE` (bool): enable sVVV capacity pre-check before mint
  - `DIEM_MINT_RATE_SVVV_PER_DIEM` (int): sVVV base units per 1 DIEM base unit
  - `DIEM_MINT_RATE` (float): sVVV tokens per 1 DIEM token (decimals-aware)
  - `DIEM_SVVV_AVAILABLE_UNITS` (int): override available sVVV units for locking
  - `DIEM_DECIMALS`, `SVVV_DECIMALS` (or `VVV_DECIMALS`) defaults 18
- Risk sizing modifiers (optional):
  - `RISK_UTIL_ALPHA` (float, default 0.5): multiplier = `1 + alpha * utilization`
  - `RISK_MAX_VOLATILITY_BPS` (float, default disabled): caps units when realized vol exceeds cap

### Libraries and services
- Venice SDK client: `libs/venice_sdk/client.py`
- Key manager: `services/venice_keys/manager.py`
- Market data: `services/marketdata/provider.py`
- DEX aggregator: `libs/dex/providers.py`
- CLI entrypoint: `apps/cli/main.py`

## Agent overview (v1)

- StakeMaster
  - Purpose: Maintain “active staker” status and claim rewards; ensure periodic heartbeat.
  - Key files: `agents/stake_master/agent.py`, `services/staking/client.py`, `libs/agentkit_ext/actions.py`
  - Dependencies: Base RPC, staking/vvv contract ABIs, Venice heartbeat (light inference usage acceptable)
  - Run:
    ```bash
    uv run python apps/cli/main.py run:stakemaster --enable-live   # live on-chain claims
    uv run python apps/cli/main.py run:loop --enable-live --sleep 15 --max-cycles 3
    ```

- ArbiDiem
  - Purpose: Risk-gated DIEM mint/sell workflow using DEX quotes and slippage guards.
  - Key files: `agents/arbi_diem/agent.py`, `services/diem/client.py`, `libs/dex/providers.py`
  - Dependencies: DEX routers, DIEM token address, pricing via `MarketDataProvider`
  - Run (single decision):
    ```bash
    uv run python apps/cli/main.py run:quorum --dry-run   # uses minimal workflow for mint/sell decision
    ```
  - Live DIEM actions (on-chain mint/burn):
    ```bash
    uv run python apps/cli/main.py diem:mint <amountBaseUnits> [--dry-run]
    uv run python apps/cli/main.py diem:burn <amountBaseUnits> [--dry-run]
    ```
    Requires `DIEM_TOKEN_ADDRESS` and `abi/diem.json`. Optional sVVV capacity gate and lock/unlock hooks via env.
  - Discount handling: When price is sufficiently below fair value, ArbiDiem can buy DIEM (exact‑out on the reversed TRADE_PATH) and burn it, with the same risk and slippage guards applied.

- CapacityBroker (minimal issuance)
  - Purpose: Issue scoped sub-keys with `consumptionLimit` and `expiresAt` for tenants; supports multi-tenant resale through Broker API.
  - Key files: `apps/broker-api/app.py`, `services/venice_keys/manager.py`, `libs/venice_sdk/client.py`
  - Dependencies: `VENICE_PARENT_KEY` for creating sub-keys, Broker admin token for tenant ops
  - Admin helpers:
    ```bash
    # list tenants / limits
    uv run python apps/cli/main.py broker:tenants:list
    uv run python apps/cli/main.py broker:limits:get --tenant T1
    uv run python apps/cli/main.py broker:limits:set --tenant T1 --window 60 --max 60 --label basic

    # venice keys cleanup (parent key recommended)
    uv run python apps/cli/main.py venice:keys:cleanup --prefix T1 --dry-run
    ```
  - Tenant self-service (new):
    - `GET /v1/me` returns `{ role, tenant }` for the authenticated tenant subkey.
    - `GET /v1/me/usage` returns Venice usage and limits for the authenticated tenant.
    - `GET /v1/me/broker-limits` returns current broker limiter settings.
    - `POST /v1/me/broker-limits` allows tenants to tighten their own limits only:
      - `windowSeconds`: may increase only (more restrictive). Decreases require admin.
      - `maxRequests`: may decrease only. Increases require admin.
      - `label`: must start with `self:` when set by tenants.

- Orchestrator (loop)
  - Purpose: Single-agent loop coordinating market observation and ArbiDiem decisions with persistence and backoff.
  - Key files: `graph/workflows/orchestrator.py`, `apps/cli/main.py`
  - Run:
    ```bash
    uv run python apps/cli/main.py run:orchestrator --dry-run --interval 5.0 --max-cycles 0
    ```
  - Portfolio cap wiring (env-gated): set `RISK_ENABLE_PORTFOLIO_CAP=true` and provide `DIEM_INVENTORY_UNITS`, `VVV_INVENTORY_UNITS`, `USDC_INVENTORY_UNITS` in base units; orchestrator passes the USD exposure to ArbiDiem sizing.

Notes:
- Quorum multi-agent orchestration and full AI Treasurer are post‑v1, except a minimal Treasurer heuristic may exist. Any advancement beyond v1 must not regress the tested v1 behaviors.

## Venice API usage in v1

- Models and chat
  - `GET ${VENICE_API_BASE_URL}/models`
  - `POST ${VENICE_API_BASE_URL}/chat/completions`
- VVV metrics
  - `GET ${VENICE_API_BASE_URL}/vvv/circulatingsupply`
  - `GET ${VENICE_API_BASE_URL}/vvv/utilization`
  - `GET ${VENICE_API_BASE_URL}/vvv/staking_yield`
- DIEM balances/usage
  - `GET ${VENICE_API_BASE_URL}/api_keys/rate_limits`
- Keys (for CapacityBroker and admin tooling)
  - `POST ${VENICE_API_BASE_URL}/api_keys` (scoped sub-keys, parent key as bearer)
  - `POST|GET ${VENICE_API_BASE_URL}/api_keys/generate_web3_key` (challenge + root key exchange)

Quick probes:
```bash
uv run python apps/cli/main.py venice:models
uv run python apps/cli/main.py venice:signals
uv run python apps/cli/main.py venice:probe-openapi --base-url https://api.venice.ai
```

## Inputs and outputs

- Inputs
  - On-chain state (staking, DIEM token, DEX quotes)
  - Venice signals (VVV/DIEM) and rate limits/usage when relevant
  - Config: env variables noted above; Broker per-tenant limits if using the proxy
- Outputs
  - Trades (dry-run by default), staking claims (when live), sub-key issuance, telemetry events/metrics
  - Decision records persisted by orchestrator; events via `libs/telemetry/*`

## Testing (v1)

- Unit/integration tests (selected):
  - Trading paths and slippage: `tests/test_dex_exact_out.py`, `tests/test_dex_fot_fallback.py`, `tests/test_dex_exact_out_venues.py`
  - DIEM service paths: `tests/test_diem_service.py`, `tests/test_diem_buy_path.py`, `tests/test_diem_mint_burn_dryrun.py`
  - Risk policy sizing: `tests/test_risk_policy.py`, `tests/test_arbi_diem_risk_integration.py`
  - Broker limits & idempotency: `tests/test_broker_limits.py`, `tests/test_cli_idempotency_purge.py`
- Orchestrator wiring: `tests/test_orchestrator_portfolio_cap.py`

Run examples:
```bash
uv run pytest -q
```

## Observability

- Metrics available at `/metrics` (when Broker API is running)
- Centralized events emitted by market data and decisions
- Optional tracing toggles via env (`LANGCHAIN_TRACING_V2`, etc.)

## Operational Notes

- DEX behaviors
  - UniswapV2 supports exact-out (buy) and exact-in (sell). Aerodrome exact-out stays disabled by design.
  - Fee-on-transfer fallback is enabled for UniswapV2 exact-in trades.
- Liquidity & quotes
  - Use `uv run python apps/cli/main.py startup:probe` to warm caches and print pair/reserves for the current `TRADE_PATH`.
  - Preview sizing and slippage without trading:
    - `uv run python apps/cli/main.py quotes:preview [--units N] [--price PX]`
      - Shows reserve-cap, adjusted units, and slippage_bps.
      - If router preview fails, an approximate constant-product estimate is used and marked `approx=true`.
  - Scan for best price over smaller inputs (guards thin pools):
    - `uv run python apps/cli/main.py market:best-price:scan --start 1.0 --min 1e-12 --factor 10`
  - Default DIEM pricing path on Base should be multi-hop: `DIEM -> WETH -> USDC`.
- Risk & sizing defaults
  - Cap input against first-hop reserves with `RISK_MAX_POOL_TAKE_BPS` (e.g., 25 = 0.25%).
  - Slippage cap defaults to 150 bps; adjust via `RISK_MAX_SLIPPAGE_BPS`.
  
## OUTPUT STYLE
- Write in complete, clear sentences. Like a **Senior Developer** mentoring a **Junior Engineer**.
- Always provide enough context for the User to understand — in a **simple & short way**.
- Clearly explain your **assumptions** and your **conclusions**.

## Known ambiguities and follow-ups

- Quorum vs. single-loop orchestrator
  - v1 uses a single orchestrator loop. The multi-agent quorum design remains post‑v1.
- CapacityBroker pricing and DIEM rentals
  - v1 provides minimal issuance. Dynamic pricing/allocation and rentals are post‑v1.
- Aerodrome exact‑out support
  - Documented limitation remains; monitor ABI for future support before enabling exact‑out.
- Venice endpoint variants
  - Some deployments use `/signals/*` or legacy `/v1/keys/*` routes. Use env overrides per `.cursor/rules/venice-api-config.mdc`.

For any gap between this catalog and the running notes in `implementation-plan-v2.md`, prefer the functional boundaries and priorities in `implementation-plan.md`. This guards v1 stability while allowing iterative enhancement post‑v1.
