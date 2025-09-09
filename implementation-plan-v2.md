## What's Done

1. Repository & Architecture Setup

   - Frameworks: LangGraph orchestrates agent flows; AgentKit handles on-chain calls; Venice API client is wired.
   - Environment & configuration: Base-chain addresses (VVV, DIEM, staking, USDC), router addresses (Uniswap V2, Aerodrome) and slippage/trade paths are defined in `.env` and `config/default.yml`.
   - Documentation: High-level overviews of services, agents, and tokenomics (VVV & DIEM) in README and AGENTS.md.

2. Core Services & Infrastructure

   - Staking client: approve/stake/claim/unstake plus on-chain status for VVV and sVVV.
   - Market data: VVV metrics (circulating supply, utilization, staking yield) and DIEM balances via Venice API; DEX quotes via Uniswap V2 and Aerodrome with fallback pathing and slippage controls.
   - Venice SDK & KeyManager: model listing, rate-limit queries, API key/subkey lifecycle (create/list/revoke). CLI and broker integrations in place.
   - DEX aggregator: Uniswap V2 (exact-in and exact-out) and Aerodrome (exact-in only) with unified slippage guards and FOT fallback for Uniswap V2.
   - Broker API & CLI:
     - Multi-tenant FastAPI: `/v1/tenants`, `/v1/chat`, idempotency middleware, per-tenant rate limits, scoped key issuance, usage counters.
     - Tenant self-service (minimal): `/v1/me`, `/v1/me/usage`, `/v1/me/broker-limits` (GET/POST). Tenants may tighten limits only (increase `windowSeconds`, decrease `maxRequests`, labels prefixed `self:`). Admin retains full control via `/v1/tenants/{id}/broker-limits`.
     - CLI: staking, market data, quotes, DIEM mint/burn (live), key management, tenant creation and admin ops.
   - Initial agents & Orchestrator:
     - StakeMaster: Keeps “active staker” status and claims rewards.
     - ArbiDiem (v1): Detects DIEM premium vs fair value, sizes with risk/slippage guards, and executes mint/sell when not simulating.
     - CapacityBroker (v1): Issues scoped API keys with fixed quotas.
     - AITreasurer (v1): Simple DIEM buffer target.
     - Orchestrator: Single-loop flow connecting wallet checks, staking status, premium signal and broker calls; persists decision records.
   - Risk module (initial): Utilization and volatility caps; attaches to ArbiDiem sizing. Portfolio exposure computation available; orchestrator can pass inventory USD (env-gated portfolio cap wiring).
   - Etherscan v2 probe: Helpers to query Uniswap/Aerodrome factory `getPair` and `getReserves` on Base (chain id 8453); CLI probe seeds a liquidity cache for DIEM/USDC and VVV/DIEM pairs.
   - Testing & instrumentation: Unit tests cover staking flows, market data retrieval, premium calculations, DEX behaviors (exact-in/out + FOT), idempotency and rate-limit enforcement. Prometheus metrics and SQL counters are exposed.

3. Recent improvements

   - DIEM service: On-chain `mint` and `burn` implemented in `services/diem/client.py` via AgentKit actions; optional sVVV capacity gate, pre-mint lock, and post-burn unlock (env-gated). CLI verbs `diem:mint` and `diem:burn` available.
   - Risk integration: `size_with_risk` combines base caps, utilization multiplier, volatility cap, and optional reserve cap. Orchestrator can pass portfolio exposure (env `RISK_ENABLE_PORTFOLIO_CAP=true`).
   - Orchestrator: Persists decision records with detailed `why` rationale and `ts` timestamp; correlation ids are propagated to DIEM events when provided.
   - Startup probes: Ensure environment paths are correct and signals are fetched from `/vvv` endpoints and `rate_limits`.

---

## What's Next (MVP Completion & Beyond)

1. Risk Engine (Deepening)

   - Expand beyond initial hooks: portfolio exposure limits across VVV/DIEM/USD with persistence, dynamic sizing based on real-time liquidity and realized volatility windows, and stop-loss/kill-switch rules. Harden APIs (`suggest_trade_units`, `max_stake`) and wire into ArbiDiem sizing and AI Treasurer buffer/hedging.

2. Agent Upgrades & Quorum Coordination

   - ArbiDiem v2: Use enhanced risk outputs and on-chain liquidity to size trades; emit structured “why” (premium, reserves, utilization, volatility, slippage).
   - CapacityBroker v2: Dynamic pricing and quota adjustments; optional rental/resale of unused DIEM credits.
   - AITreasurer v2: Manage staking/unstaking, DIEM minting/selling, and buffer rebalancing based on risk and usage forecasts.
   - Quorum coordinator: Aggregate decisions from StakeMaster, ArbiDiem, CapacityBroker and AITreasurer; resolve conflicts; adjust listen intervals.

3. Robust Liquidity Discovery

   - Use Etherscan v2 (logs + proxy/eth_call) to discover pairs (factory `getPair` or `PairCreated` logs) and read reserves (`getReserves`).
   - Periodically query factory logs and on-demand `getPair`/`getReserves`; maintain a cache keyed by token pairs and AMM; use liquidity data to cap trade size and adjust risk.

4. Event Watchers

   - Deploy on-chain listeners for large stake/unstake events, DIEM mint/burn events and high-volume trades. Emit internal triggers to update risk parameters and treasury allocations.

5. Broker Enhancements

   - Beyond minimal self-service, add dynamic pricing/allocation and optional DIEM-denominated pricing; surface key rotation/expiry warnings; enforce prod defaults (admin token, CORS allowlist) and receipt trails.

6. Testing & Documentation Expansion

   - Increase test coverage for advanced risk sizing, liquidity discovery scheduling, dynamic pricing and quorum logic.
   - Keep docs updated: Venice API paths (no `/diem` signals), Etherscan v2 (chain id 8453), DEX constraints and agent behaviours. Provide a Replit deployment/runbook.

7. Optional Post-MVP enhancements

   - Consider DeFi integrations (lending/collateral), building futures/options for AI compute, and a dashboard for capacity rental and treasury monitoring.

---

For any gap between this plan and the running notes in `implementation-plan.md`, prefer the functional boundaries and priorities in `implementation-plan.md` (source of truth for v1 boundaries). This guards v1 stability while allowing iterative enhancement post‑v1.

