## ✅ Done (MVP Foundations)

1. **Environment & Configuration**

   * `.env` and `default.yml` define correct Base addresses (VVV token, DIEM token, staking contract, USDC), routers (Uniswap V2 and Aerodrome) and default trade paths.
   * Slippage, rate limits and per-tenant quotas are configurable via environment variables.

2. **Core Services**

   * **Staking service**: provides `approve`, `stake`, `claim`, `unstake` and status queries; uses AgentKit to interact with the VVV staking contract.
   * **Market data service**: retrieves VVV metrics (circulating supply, network utilization, staking yield) and DIEM balances from Venice API.  Quotes prices using DEX aggregator across Uniswap V2 and Aerodrome.
   * **Venice SDK & Key manager**: supports model listing, rate-limit queries, API key and subkey creation/revocation.
   * **DEX aggregator**: abstracts trading/price quoting across Uniswap V2 and Aerodrome with slippage control.
   * **DIEM service (scaffold)**: defines interfaces for mint and burn, though on-chain logic is not yet implemented.

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

   * Use Etherscan v2 (`chainid=8453`) to query Uniswap and Aerodrome factories (`getPair`) and read reserves (`getReserves`); build a local cache of DIEM/USDC and VVV/DIEM pool liquidity.
   * Integrate liquidity data into pricing and risk calculations.

5. **On-chain Event Watchers**

   * Add listeners for large stake/unstake, DIEM mint/burn and high‑volume trades to trigger risk/treasury adjustments and agent actions.

6. **Broker API Enhancements**

   * Implement dynamic quotas and DIEM‑denominated pricing; support tenant self‑service adjustments; add key rotation and expiry notifications.

7. **Additional Tests & Documentation**

   * Extend test suite to cover DIEM mint/burn flows, risk-driven trade sizing, dynamic pricing and quorum logic.
   * Update README, AGENTS.md and runbooks with tokenomics details, Etherscan v2 setup instructions and usage of new risk/pool discovery features.
