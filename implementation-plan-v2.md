Below is the consolidated **implementation‑plan‑v2** with only two sections—**What’s Done** and **What’s Remaining**.  It reflects an audit of the `replit` branch against the original vision in *implementation‑plan.md* and serves as the roadmap to finalize the core (v1) functionality so we can build advanced agent and feature enhancements afterward.

---

## ✅ What’s Done (MVP Foundations)

* **Architecture & Repository Setup**
  LangGraph orchestrator, AgentKit for on‑chain transactions, and a clearly defined monorepo (`services/`, `agents/`, `libs/`, `apps/`, `graph/`, `infra/`, `tests/`).  Environment files specify Base addresses for VVV/DIEM/staking, USDC, WETH, Uniswap V2 and Aerodrome routers, trade paths, slippage, and Etherscan v2 settings.

* **Core Services**

  * **Staking client:** Handles approve/stake/claim/unstake flows and monitors sVVV status; StakeMaster agent keeps the wallet “active” and claims rewards.
  * **Market data service:** Fetches VVV circulating supply, network utilization and staking yield; retrieves DIEM balances from the Venice API via the `/api_keys/rate_limits` endpoint; quotes token prices via Uniswap V2 and Aerodrome with fallback routing and slippage controls.
  * **Venice SDK & KeyManager:** Supports model/rate-limit queries and full lifecycle of root and scoped API keys.
  * **DEX aggregator:** Wraps Uniswap V2 and Aerodrome trading (exact-out on Uniswap only) with slippage guards.
  * **DIEM service scaffold:** Defines interfaces and CLI commands for mint/burn (but on‑chain execution is not yet implemented).

* **Broker & Operator Interfaces**
  Multi‑tenant FastAPI broker exposes endpoints to create tenants, issue scoped API keys with quotas, proxy chat completions with per-tenant limits, and provide usage counters and metrics.  The CLI offers commands for staking, pricing, key management, tenant management, startup probes and administrative tasks.

* **Initial Agents & Orchestrator**
  Implemented agents include:

  * StakeMaster (keeps staking active);
  * ArbiDiem (monitors DIEM premium and logs mint/sell intent);
  * CapacityBroker (issues scoped keys with fixed quotas);
  * AITreasurer (simple buffer heuristic).
    A single-loop orchestrator coordinates wallet checks, staking status, premium detection and broker routing.

* **Risk Module (Initial)**
  Basic utilization and volatility caps are implemented; these risk knobs feed into ArbiDiem’s sizing logic.

* **Liquidity Discovery**
  Startup probe uses Etherscan v2 (`chainid=8453`) to query Uniswap/Aerodrome factories (`getPair`, `getReserves`) and warm a basic cache for DIEM/USDC and VVV/DIEM pools, informing fallback pricing and risk.

* **Testing & Metrics**
  Unit tests cover staking flows, market data retrieval, premium detection, idempotency and rate-limiting. Prometheus metrics and SQL usage counters are in place.  Documentation includes a pocket guide on VVV/DIEM tokenomics and a high-level system overview.

---

## 🛠 What’s Remaining (to Finalize MVP and Meet Full Plan)

1. **Complete DIEM Mint/Burn**
   Implement on-chain mint/burn logic in `services/diem/client.py`, including sVVV locking/unlocking, mint-rate calculation, and error handling.  Connect these methods to the CLI and enable ArbiDiem to execute mint→sell and rebuy→burn operations when premium thresholds and risk parameters permit.

2. **Build a Comprehensive Risk Engine**
   Extend `services/risk` to handle portfolio exposure limits (VVV/DIEM/USD), liquidity-aware trade sizing, volatility-based adjustments and stop‑loss rules.  Provide APIs like `suggest_trade_units()` and `max_stake()`; wire them into ArbiDiem for trade sizing and AITreasurer for buffer management.

3. **Upgrade Agents & Introduce Quorum**

   * **ArbiDiem v2:** Use risk-engine outputs and live liquidity data for sizing; execute mint/sell actions; record structured rationale (premium, utilization, reserves, volatility, slippage).
   * **CapacityBroker v2:** Add dynamic pricing and quota management; enable resale/rental of unused DIEM credits.
   * **AITreasurer v2:** Manage stake/unstake operations and DIEM mint/sell, maintaining a buffer based on risk and demand.
   * **Quorum coordinator:** Aggregate signals from StakeMaster, ArbiDiem, CapacityBroker and AITreasurer, resolve conflicts and adjust action intervals.

4. **Robust Liquidity & Event Monitoring**
   Expand Etherscan v2 integration to periodically scan `PairCreated` logs and call `getPair` / `getReserves` on demand.  Maintain a local pool cache across DEXes, feeding risk sizing and pricing.  Build watchers for large stake/unstake, DIEM mint/burn and high-volume trades; trigger risk and treasury adjustments based on events.

5. **Enhance Broker for Self-Service**
   Add tenant self-service features: adjustable quotas, optional DIEM-denominated pricing, and key rotation/expiry notifications.  Lock down production defaults (admin token, CORS restrictions) and ensure proper logging and auditing.

6. **Augment Tests & Documentation**
   Expand coverage to include DIEM mint/burn flows, risk-driven sizing, liquidity discovery, dynamic pricing and quorum decision logic.  Revise README and AGENTS.md with Venice API path rules (explicit VVV metrics, DIEM balances via rate-limits), Etherscan v2 setup, DEX constraints on Base and step-by-step runbooks for Replit deployment.

7. **Optional Post-MVP Enhancements**
   Once the MVP is stable, explore multi-agent quorum at larger scale, sophisticated pricing models, DeFi integrations (e.g., DIEM lending), AI compute futures/derivatives and user-facing dashboards.  These are explicitly deferred until core functionality is complete.

By finishing these remaining tasks, the system will achieve the robust, self‑service capacity broker envisioned in the original plan—capable of minting and burning DIEM, running advanced risk management, coordinating multiple agents and exposing dynamic quotas and pricing—while providing a solid foundation for future enhancements.
