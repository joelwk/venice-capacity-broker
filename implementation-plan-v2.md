## ✅ Done (MVP Foundations)

1. **Environment & Configuration**

   * `.env` and `default.yml` define correct Base addresses (VVV token, DIEM token, staking contract, USDC), routers (Uniswap V2 and Aerodrome) and default trade paths.
   * Slippage, rate limits and per-tenant quotas are configurable via environment variables.

2. **Core Services**

   * **Staking service**: provides `approve`, `stake`, `claim`, `unstake` and status queries; uses AgentKit to interact with the VVV staking contract.
   * **Market data service**: fetches VVV metrics via explicit endpoints (`/vvv/circulatingsupply`, `/vvv/utilization`, `/vvv/staking_yield`) and DIEM balances/rate limits via `GET /api_keys/rate_limits` (no `/diem` signals). Prices/quotes come from the DEX aggregator (with reserve‑cap sizing and price fallbacks when pools are thin).
   * **Venice SDK & Key manager**: supports model listing, rate-limit queries, API key and subkey creation/revocation; paths are env‑overridable.
   * **DEX aggregator**: UniswapV2 supports exact‑in and exact‑out; Aerodrome exact‑out is intentionally disabled. FOT fallback and slippage guards enforced. Circuit breaker prevents thrashing on repeated errors.
   * **DIEM service**: on‑chain mint/burn wired via AgentKit actions, with optional sVVV capacity gate; emits telemetry; integrates with risk sizing.

3. **Broker API & CLI**

   * Multi-tenant FastAPI server: create/list tenants, issue/revoke scoped API keys, proxy chat completions with per-tenant quotas and idempotency, expose usage counters and metrics.
   * CLI tools: commands to stake VVV, fetch metrics, quote prices, (stub) trade DIEM, manage API keys and tenants, and run orchestrator flows.

4. **Agents & Orchestrator**

   * **StakeMaster**: keeps staker active and claims rewards (live gated), heartbeat wired.
   * **ArbiDiem (v1)**: monitors DIEM premium and sizes with risk (utilization multiplier, volatility cap, and pool reserve‑cap). Liquidity‑aware preview adjusts units; logs rationale.
   * **CapacityBroker (v1)**: issues scoped API keys with fixed quotas; idempotency + metrics; no dynamic pricing yet.
   * **AITreasurer (v1)**: simple buffer target (heuristic), post‑v1 for expansion.
   * **Orchestrator**: single‑loop; persists decisions; optional portfolio cap; backoff on errors. Dry‑run avoids web3.

5. **Risk Module (initial)**

   * USD/unit limits, utilization multiplier, realized volatility cap, reserve‑cap sizing (based on first‑hop reserves), slippage guard.

6. **Testing & Observability**

   * Tests cover: DEX exact‑out (UniswapV2) / exact‑in with FOT fallback, DIEM mint/burn dry‑run + capacity gate, risk integration, orchestrator portfolio cap, Etherscan discovery.
   * Metrics and events: DEX latency buckets, circuit events, agent decisions, signals; SQL counters + KV compaction.

7. **Liquidity & Docs (recent)**

   * Added constant‑product “approx out” fallback (UniswapV2 curve with fee) in MarketDataProvider to estimate execution when router previews fail; `quotes:preview` surfaces approximate slippage when used.
   * DIEM price fallback implemented: derives via mid‑price DIEM→WETH × WETH→QUOTE when direct quotes are unavailable.
   * CLI probes refreshed: `startup:probe` warms cache and prints pairs/reserves; `quotes:preview` prints reserve‑cap, adjusted units, slippage (flags approx and price fallback); `market:best-price:scan` searches smaller inputs.
   * Broker `/v1/env` surfaces effective TRADE_PATH and recent signal summaries.

---

## 🛠 Next (Tasks to Complete MVP)

1. **Liquidity‑Aware Execution Fallback — COMPLETED**

   * Implemented AMM fallback + slippage surfacing in CLI preview.

2. **Docs & Runbooks — COMPLETED**

   * README/AGENTS updated: Venice path rules (no `/diem`), Base Etherscan v2 setup, DEX constraints, multi‑hop TRADE_PATH, reserve‑cap sizing, Replit extras, CLI probes, DIEM price fallback.

3. **ArbiDiem polish (v1)**

   * Ensure rationale logs always include reserve‑cap, utilization, vol_bps, and if fallback pricing was used. Keep execution dry when market not favorable or liquidity too thin.

4. **Pool & Liquidity Discovery**

   * Consider Aerodrome reserves as a secondary source for mid‑price fallback (once stable) while keeping exact‑out disabled.

5. **On-chain Event Watchers**

   * Add listeners for large stake/unstake, DIEM mint/burn and high‑volume trades to trigger risk/treasury adjustments and agent actions.

6. **Broker API Enhancements — COMPLETED**

   * `/v1/env` reflects effective TRADE_PATH and recent signal summaries. Dynamic quotas/DIEM‑denominated pricing remain post‑v1.

7. **Additional Tests & Documentation**

   * Extend test suite for constant‑product fallback, price derivation path (mid DIEM→WETH × WETH→USDC), and quote scan CLI. Ensure CI gate enforces Venice readiness + sane defaults.

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
  - `quotes:preview` → prints reserve‑cap and slippage reasoning; Aerodrome exact‑out skip noted; falls back to derived DIEM price when router quotes are unavailable.
  - `market:best-price:scan` → progressively tries smaller inputs to find a viable quote for thin pools.

---

## ✅ V1 Acceptance Checklist

- Venice API alignment:
  - `venice:models` and `venice:signals` succeed (VVV metrics via explicit endpoints; DIEM via `rate_limits`).
- DEX / Etherscan v2:
  - `startup:probe` prints DIEM→WETH and WETH→USDC pairs/reserves on Base (chainid=8453); cache is warmed.
  - UniswapV2 exact‑out usable; Aerodrome exact‑out skipped with clear log/metric.
- Quotes / Risk:
  - `quotes:preview` returns non‑zero price (fallback ok), shows reserve‑cap and slippage labels; flags approximate/slippage when AMM fallback is used; scan CLI finds a viable size or warns at floor.
- Broker / Ops:
  - `/v1/env` shows `venice.ready=true`, key paths, DEX providers, multiline TRADE_PATH, and metrics path.
- Tests:
  - DEX exact‑out/fallback, DIEM mint/burn dry‑run + sVVV gate, risk sizing (util/vol/reserve‑cap), broker limits/idempotency, orchestrator portfolio cap are green.

Current status: 40 passed, 2 skipped (local run).
