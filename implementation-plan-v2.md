### ✅ Current State Summary (What’s Implemented)

| Area                                           | Evidence & Notes                                                                                                                                                                                                                                                                      |
| ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Wallet & Staking (services/staking)**        | `StakingService` wraps VVV approve, stake, claim, and unstake functions; `status()` uses on‑chain calls to report staked and reward balances. This satisfies the basic staking module from the original plan.                                                                         |
| **Market Data**                                | `MarketDataProvider` implements quoting, price fetching and DIEM/VVV signal retrieval, with caching and retry logic. It is functional for best‑price and unified signals.                                                                                                             |
| **Key Management (services/venice\_keys)**     | `KeyManager` issues root and scoped subkeys via `VeniceClient` and revokes them; it delegates to Venice API endpoints.  It respects environment overrides for API paths.                                                                                                              |
| **Venice SDK**                                 | `VeniceClient` sets the base URL and trims trailing slashes to avoid the double‑slash bug, and supports overrideable paths for root key, subkey creation, models and chat.  This solves environment pitfalls described in `VeniceProgress.txt`.                                       |
| **Stake‑Master & DIEM Controller (LangGraph)** | `graph/langgraph/nodes.py` defines nodes for wallet verification, staking status, DIEM premium calculation (based on `DIEM_FAIR_ALPHA` & `DIEM_PREMIUM_THRESHOLD`) and a broker router.  The `diem_controller_node` returns `mint_sell` or `hold` decisions and logs rationale spans. |
| **Broker API**                                 | `apps/broker-api/app.py` defines tenant creation, chat proxying with rate limits, per‑tenant limit configuration, and a debug counters endpoint (admin‑only).  It uses SQL storage by default, with JSON fallback.                                                                    |
| **Idempotency & Rate Limiting**                | Middleware to prevent duplicate chat requests is implemented; it generates a digest key and returns 409 on replay.  Rate limiting per tenant uses sliding windows with KV or Redis; tests cover base cases.                                                                           |
| **Tests & Observability**                      | There are tests for DIEM premium thresholds and rate‑limiting; `VeniceProgress.txt` notes tests for admin counters and broker limits have been added.  Metrics are exposed via Prometheus.                                                                                            |

---

### 🔍 Gaps and Pending Tasks

1. **DIEM Module Completion**

   * `services/diem/client.py` still has stubs for `mint` and `burn`.  Actual on‑chain minting (locking sVVV to mint DIEM) and burning (unlocking sVVV) must be implemented.  This includes constructing and sending Base L2 transactions, handling gas estimation, and updating state post‑transaction.

2. **Risk Module**

   * The `services/risk` folder only has an `__init__.py`.  The original plan proposed risk models (e.g., position sizing, volatility, utilization) to adjust staking or trading.  A basic risk service should at least compute portfolio exposure (VVV vs DIEM vs USD), apply configurable stop‑loss/limit parameters, and expose decisions to the quorum agent.

3. **Capacity Broker & AI Treasurer Agents**

   * The original plan described a `capacity_broker` agent to resell unused Diem capacity and a multi‑tenant broker API.  While the broker API exists, the agent logic to dynamically price and rent capacity is not implemented.  Similarly, the `ai_treasurer` agent for managing VVV/DIEM treasury allocations (e.g., budgeting, hedging, and reinvesting rewards) is missing.

4. **Quorum / Multi‑Agent Orchestrator**

   * The current graph only includes basic nodes (wallet, stake\_master, diem\_controller, broker\_router).  A full quorum orchestrator (listening to multiple signals, voting on actions, adjusting listen intervals) is not yet present.  This orchestrator should coordinate `stake_master`, `arbi_diem`, `capacity_broker`, and `ai_treasurer`.

5. **DIEM Arbitrage & Market Integration**

   * The plan included an `arbi_diem` agent to mint DIEM when the premium is above threshold and to burn or hold when below.  Although the decision logic exists, actual execution (minting, trading via DEX aggregator, monitoring slippage) needs to be wired up to `MarketDataProvider` and `DIEMService`.  Additional tests should cover these flows.

6. **Risk‑Aware Idle Capacity Use**

   * The project aims to sell unused Diem capacity to third parties via scoped keys.  This requires:

     * Pricing models (e.g., cost plus margin).
     * Quota management (ensuring own operations stay within staked capacity).
     * Dynamic issuance/expiration of subkeys via `VeniceClient`.
     * Documentation/guides for clients and an admin dashboard.

7. **Documentation & Developer Experience**

   * The README currently focuses on environment pitfalls and quick start instructions.  You requested a more development‑oriented technical reference: step‑by‑step workflows for pushing code to Replit, handling env variables (`VENICE_API_BASE_URL`, `VENICE_PARENT_KEY`, `VENICE_CREATE_SUBKEY_PATH`), managing dependencies (`uv` vs `pip`), and troubleshooting base URLs and double slashes.  A full overhaul is needed to capture lessons learned (e.g., verifying base URL via `/openapi.json`, difference between parent and API keys).

---

### 📦 Updated Implementation Plan to Reach v1

The goal is a minimal yet functional end‑to‑end system: stake VVV, monitor DIEM premiums, mint/sell DIEM, resell unused capacity via broker API, and coordinate decisions through a simple quorum.

1. **Complete DIEMService**

   * **Implement mint & burn**: Use Coinbase AgentKit or direct Web3 calls to interact with the DIEM smart contract on Base.  Write `mint(s_vvv_amount: int)` to lock sVVV and return minted DIEM; `burn(diem_amount: int)` to unlock sVVV.  Update `services/diem/client.py` accordingly and add tests.

2. **Add Risk Module**

   * Create `services/risk/policy.py` with functions to compute portfolio exposure (current VVV, DIEM, USD), and recommend safe position sizes.  Allow configurable parameters for max exposure, minimum liquidity, and stop‑loss thresholds.  Integrate this into the quorum so decisions respect risk constraints.

3. **Finish Market & Arbitrage Logic**

   * Extend `MarketDataProvider` to compute DIEM fair value and premium using available signals and on‑chain data.  Integrate `arbi_diem` agent logic: call `mint` when premium ≥ threshold and `burn` when below, factoring in risk and rate limits.  Ensure trades use the aggregator for best price and handle slippage.  Add tests to verify that decisions result in the correct on‑chain transactions.

4. **Capacity Broker and Tenant Management**

   * Flesh out a `capacity_broker` agent to:

     * Calculate spare capacity (unused DIEM credits).
     * Price and sell capacity to tenants via the broker API.
     * Use `VeniceClient` to issue scoped subkeys with quotas and expirations.
     * Adjust prices dynamically based on utilization and market demand.
   * Build a simple web UI or CLI to view tenants, capacity usage and pricing.

5. **Treasury Management**

   * Implement `ai_treasurer` agent to oversee treasury functions:

     * Stake/unstake VVV based on risk and premium.
     * Allocate minted DIEM between internal usage, resale, and burning.
     * Track emissions rewards and restake as appropriate.

6. **Quorum & Agent Orchestration**

   * Build a `quorum` agent using LangGraph to:

     * Collect signals from `stake_master`, `arbi_diem`, `capacity_broker`, `ai_treasurer`, and `risk`.
     * Vote or apply weighted rules to decide the final action (e.g., “mint and sell DIEM”, “increase price on broker API”).
     * Adjust `listen interval` based on volatility (as per original plan) to react faster during high demand.

7. **Testing and Validation**

   * Expand the test suite:

     * Cover DIEM mint/burn flows and risk constraints.
     * Test broker API subkey issuance and quotas.
     * Validate quorum decisions under different market scenarios.
     * Include integration tests that stake VVV, mint DIEM, sell DIEM, and check balances.

8. **Documentation & Developer Workflow**

   * Overhaul the README to serve as a technical reference:

     * Step‑by‑step setup for Replit: environment variables, `uv` installation, path trimming (`BASE_URL%/`), verifying Venice base URL via `openapi.json`, and subkey path overrides.
     * Differences between parent key and standard API key, how to acquire each, and when to use them.
     * Guide for using the broker CLI, issuing subkeys, and calling chat completions.
     * Troubleshooting section: common errors (e.g., 404 on subkey creation due to wrong base path) and how to fix them.
   * Document contribution workflows: branch naming, testing (`pytest`), environment compaction, deployment on Replit, and how to update the SQL schema when adding new services.

9. **Optional Enhancements** (future sprints after v1)

   * Add risk‑adjusted dynamic pricing models.
   * Integrate on‑chain governance for parameter tuning.
   * Support other AI models via multi‑provider routing.
   * Implement a marketplace for DIEM rentals using escrow contracts.

---

### 🚀 Next Steps Toward Final System (Focused on Agents + System Features)

1) Marketdata + Watchers (stabilize, then freeze)
- Confirm multi-hop Aerodrome quoting works in prod (Base) and cache hit-rates are healthy.
- Add metrics: cache hits/misses, quote failures by provider, and average price latency.
- Finalize env defaults in `.env.example` (routers, quote/bridge tokens, cache TTL/max) and document in README/DEPLOYMENT.
- Freeze interfaces for `MarketDataProvider`, `TokenMetrics`, and DEX aggregator inputs.

2) DIEM Module (on-chain actions)
- Implement `services/diem` mint/burn with Web3 + AgentKit wallet; wire gas estimation and error surfacing.
- Add a dry-run mode and idempotency guard for mint/burn operations.
- Emit domain events (minted, burned, failed) for the orchestrator.

3) Risk Service (MVP)
- Add `services/risk` with simple budget/exposure checks and tunable thresholds (ENV-backed).
- Provide a stateless API the orchestrator can call before taking actions (stake, mint, trade).

4) Orchestrator (Quorum) Build-Out
- Compose `stake_master`, `arbi_diem`, `capacity_broker`, and `ai_treasurer` under a unified graph.
- Add listen-interval policy and backoff; centralize signal ingestion (DIEM/VVV signals + token watcher snapshots).
- Persist decisions and outcomes for observability.

5) ArbiDiem Agent
- Implement premium detection loop (already in plan) to trigger mint/sell (or hold) based on thresholds and risk limits.
- Integrate with DEX prices for execution preview; validate expected slippage via aggregator quotes.

6) Capacity Broker Agent
- Add dynamic pricing logic for reselling unused capacity; use `libs/pricing` and `MarketPricingEngine` as baseline.
- Implement allocation logic using Broker API (quote → purchase → key issuance lifecycle).

7) AI Treasurer Agent
- Define treasury constraints and objectives; allocate between VVV/DIEM/USDC given risk budget.
- Schedule periodic rebalancing; produce proposals logged with explanations.

8) Observability + Ops
- Extend `/metrics` to include agent loop metrics, DIEM transactions, and error classes.
- Add structured logs for all agent decisions with correlation IDs.
- Provide admin toggles to pause/resume agents and adjust thresholds live.

9) Hardening + Tests
- Add integration tests for: multi-hop quotes, DIEM mint/burn happy paths (mock chain), and orchestrator decision branches.
- Gate agent actions behind feature flags and environment readiness checks.

10) Documentation (sources of truth)
- Keep `README.md` (developer quickstart), `docs/DEPLOYMENT.md` (ops), and `implementation-plan-v2.md` (architecture + roadmap) updated as features land.
