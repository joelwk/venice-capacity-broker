
---

## ✅ Done

**Configuration & Infrastructure**

* `.env.example` and `config/default.yml` define Base-chain addresses (VVV token, staking contract, DIEM token, USDC, WETH), DEX routers (Uniswap V2, Aerodrome), and trade paths; environment parsing and validation are implemented.
* Etherscan v2 usage documented (single endpoint with `chainid=8453`).

**Core Services**

* **Staking service**: provides approve, stake, claim, unstake and on-chain status queries.
* **Market data provider**: fetches VVV metrics (circulating supply, utilisation, staking yield) and DIEM balances via Venice API; quotes token prices via Uniswap V2 and Aerodrome with fallback paths.
* **Venice SDK & Key manager**: wraps API endpoints for model queries, rate limits, subkey generation and revocation.
* **DEX aggregator**: performs price discovery and trades across Uniswap V2 and Aerodrome, honouring slippage settings.
* **Broker API**: multi-tenant FastAPI service exposing `/v1/tenants`, `/v1/chat`, per-tenant rate limits, scoped key issuance and debug counters; idempotency middleware protects against duplicate requests.
* **CLI tools**: commands for staking actions, DIEM mint/trade proxies (stubbed), key management, price quoting, usage & limits, run orchestrator flows, admin utilities (rate-limit settings, idempotency purge).
* **Orchestrator & Graph**: simple workflow wires wallet verification, staking status, DIEM premium decision and broker routing.
* **Initial agents**:

  * **StakeMaster**: keeps staker active; claims rewards periodically.
  * **ArbiDiem**: monitors DIEM premium and would mint/sell when mint/burn is implemented.
  * **CapacityBroker**: issues scoped subkeys for tenants with static pricing/quotas.
  * **AITreasurer**: provides rudimentary buffer management for DIEM vs. expected usage.
* **Testing & Observability**: unit tests for staking, market data, idempotency, rate limits and DIEM premium logic; Prometheus metrics exposed; SQL compaction & rate-limit tests.

**Overall**: the repository provides a working scaffold capable of staking VVV, accessing Venice API, pricing tokens, issuing subkeys and serving chat requests under per-tenant quotas.  The basic agent workflows run, but advanced functionality is stubbed.

## 🛠 Next (Remaining to reach the original vision)

1. **Complete the DIEM service**
   Implement on-chain `mint` and `burn` in `services/diem/client.py`, locking sVVV via the staking contract and unlocking via burns; handle mint-rate calculation, unlock periods and error propagation.  Integrate these functions into the CLI and ArbiDiem agent.

2. **Implement a full risk module**
   Create `services/risk` with a policy engine that evaluates portfolio exposure (VVV vs. DIEM vs. USD), volatility, utilisation and stop‑loss thresholds.  Provide an API for agents to fetch recommended trade sizes, staking amounts and mint/burn limits.  Hook the risk module into ArbiDiem, AITreasurer and Quorum decisions.

3. **Upgrade agents**

   * **ArbiDiem v2**: size trades based on risk outputs and real-time liquidity; use Etherscan v2 to discover pool reserves (via `getPair` and `getReserves` on Uniswap V2 and Aerodrome) and incorporate slippage/risk.
   * **CapacityBroker v2**: introduce dynamic pricing and quotas for unused Diem credits; allow renting/selling surplus compute with configurable terms; optionally integrate DIEM lending/escrow on-chain.
   * **AITreasurer v2**: manage treasury actively—stake/unstake VVV, mint/sell DIEM, rebalance buffers based on forecasted usage and risk.
   * **Quorum coordinator**: implement a multi-agent voting layer to reconcile signals from StakeMaster, ArbiDiem, CapacityBroker and AI Treasurer, and to adjust the “listen interval” based on volatility.

4. **Add market and pool discovery**
   Use Etherscan v2 (with `chainid=8453`) to query Uniswap and Aerodrome factories (`getPair` or event logs) to discover token pair addresses and fetch reserves via `getReserves`.  Populate a cache of pool liquidity for DIEM/USDC, VVV/DIEM and other relevant pairs.  Integrate liquidity data into pricing and risk calculations.

5. **Introduce event watchers**
   Implement on‑chain watchers for large stake/unstake events, DIEM mints/burns and high‑volume trades.  Use these events to trigger risk adjustments, treasury rebalancing or alerts.

6. **Enhance broker and subkey management**
   Add tenant self-service with adjustable quotas and expiry; implement DIEM-denominated pricing; secure subkey rotation and auto-expiry warnings.  Provide a UI or API for monitoring tenant usage and costs.

7. **Expand tests and documentation**

   * Test new flows for DIEM mint/burn, risk-driven trade sizing, dynamic pricing and quorum decisions.
   * Update documentation (README, AGENTS.md) to reflect tokenomics, environment variables and new agent behaviours.
   * Provide runbooks for setting up Etherscan keys, running pool-discovery scripts, and starting the full multi-agent system.

8. **Optionally extend**

   * Add DeFi integrations (use DIEM as collateral).
   * Support multi-model routing and AI cost hedging via DIEM futures.
   * Build UI dashboards for treasury and capacity rental.m.
