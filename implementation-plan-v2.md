## ✅ Done (MVP Foundations)

1. **Environment & Configuration**

   * `.env` and `default.yml` define correct Base addresses (VVV token, DIEM token, staking contract, USDC), routers (Uniswap V2 and Aerodrome) and default trade paths.
   * Slippage, rate limits and per-tenant quotas are configurable via environment variables.

2. **Core Services**

   * **Staking service**: provides `approve`, `stake`, `claim`, `unstake` and status queries; uses AgentKit to interact with the VVV staking contract.
   * **Market data service**: fetches VVV metrics via explicit endpoints (`/vvv/circulatingsupply`, `/vvv/utilization`, `/vvv/staking_yield`) and DIEM balances/rate limits via `GET /api_keys/rate_limits` (no `/diem` signals). Prices/quotes come from the DEX aggregator.
   * **Venice SDK & Key manager**: supports model listing, rate-limit queries, API key and subkey creation/revocation; paths are env‑overridable.
   * **DEX aggregator**: UniswapV2 supports exact‑in and exact‑out; Aerodrome exact‑out is intentionally disabled. FOT fallback and slippage guards enforced.
   * **DIEM service**: on‑chain mint/burn wired via AgentKit actions, with optional sVVV capacity gate; emits telemetry; integrates with risk sizing.

3. **Broker API & CLI**

   * Multi-tenant FastAPI server: create/list tenants, issue/revoke scoped API keys, proxy chat completions with per-tenant quotas and idempotency, expose usage counters and metrics.
   * CLI tools: commands to stake VVV, fetch metrics, quote prices, (stub) trade DIEM, manage API keys and tenants, and run orchestrator flows.

4. **Agents & Orchestrator**

   * **StakeMaster**: keeps staker active by making periodic dummy API calls and claims rewards.
   * **ArbiDiem (v1)**: monitors DIEM premium and logs decisions (mint/sell calls are stubbed until DIEM mint is complete).
   * **CapacityBroker (v1)**: issues scoped API keys with fixed quotas; no dynamic pricing yet.
   * **AITreasurer (v1)**: maintains a simple DIEM buffer target.
   * **Orchestrator**: wires wallet check, staking status, DIEM premium decision and broker routing.

5. **Risk Module (initial)**

   * Basic functions for volatility and utilization multipliers and risk-based sizing are implemented and fed into ArbiDiem.

6. **Testing & Observability**

   * Unit tests cover staking, market data fetching, DIEM premium logic, idempotency, and rate-limit enforcement.
   * Prometheus metrics and SQL counters are wired in; CLI includes counter compaction.

---

## 🛠 Next (Tasks to Complete MVP)

1. **Finalize DIEM Mint/Burn**

   * Implement on-chain mint and burn in `services/diem/client.py` to lock sVVV, calculate the mint rate, handle unlock periods, and return transaction receipts.
   * Integrate with CLI commands and enable ArbiDiem to execute mint and sell actions.

2. **Complete Risk Engine**

   * Expand `services/risk` to provide exposure sizing, volatility analysis and utilization‑based caps.
   * Add methods such as `suggest_trade_units` and `max_stake` and inject them into ArbiDiem and AITreasurer for risk‑aware decisioning.

3. **Agent Upgrades & Quorum Coordinator**

   * **ArbiDiem v2**: size trades using the new risk engine and live pool liquidity; call mint/sell once DIEM service is ready.
   * **CapacityBroker v2**: introduce dynamic pricing and quotas for unused DIEM credits; implement resale/rental logic.
   * **AITreasurer v2**: actively stake/unstake VVV, mint/burn DIEM and rebalance buffers based on usage forecasts and risk.
   * **Quorum agent**: aggregate signals from all agents (StakeMaster, ArbiDiem, CapacityBroker, AITreasurer), coordinate actions and adjust listen intervals.

4. **Pool & Liquidity Discovery**

   * Implemented via Etherscan v2 (`chainid=8453`) helper using `proxy/eth_call` for `getPair`/`getReserves`; local cache populated for DIEM↔WETH and WETH↔USDC.
   * CLI `startup:probe` warms the cache and prints a compact report; `quotes:preview` shows reserve‑cap and slippage reasoning.

5. **On-chain Event Watchers**

   * Add listeners for large stake/unstake, DIEM mint/burn and high‑volume trades to trigger risk/treasury adjustments and agent actions.

6. **Broker API Enhancements**

   * Ensure `.replit` installs required extras (`--extra broker --extra web3 --extra agentkit`) so DEX/web3 stay installed across restarts.
   * Implement dynamic quotas and DIEM‑denominated pricing; support tenant self‑service adjustments; add key rotation and expiry notifications.

7. **Additional Tests & Documentation**

   * Extend test suite for: UniswapV2 exact‑out, Aerodrome exact‑in fallback, DIEM mint/burn sVVV gate, reserve‑cap sizing and risk integration, orchestrator persistence.
   * Update README/AGENTS.md with: Venice path rules (no `/diem`), Base Etherscan v2 setup, DEX constraints (Aerodrome exact‑out disabled), Replit runbook, and recommended `RISK_MAX_POOL_TAKE_BPS` defaults.

---

## 🔧 Environment Defaults (Updated)

- `TRADE_PATH` should be multi‑hop on Base for DIEM pricing: `DIEM -> WETH -> USDC`.
- Set `RISK_MAX_POOL_TAKE_BPS` (e.g., 25) to cap input to a conservative fraction of first‑hop reserves when pools are shallow.
- Keep `.replit` uv sync extras as `--extra broker --extra web3 --extra agentkit` to persist DEX/web3 across restarts.

---

## ▶️ Operational Notes (v1)

- Aerodrome exact‑out is skipped by design; UniswapV2 handles exact‑out (buy). Exact‑in sells supported on both; FOT fallback enabled.
- CLI probes:
  - `venice:signals` → VVV metrics + DIEM balances via `rate_limits`.
  - `startup:probe` → warms liquidity cache (Etherscan v2) and prints pairs/reserves.
  - `quotes:preview` → prints reserve‑cap and slippage reasoning; Aerodrome exact‑out skip noted.
