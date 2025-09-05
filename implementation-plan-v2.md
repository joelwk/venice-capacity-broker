### ✅ Current State Summary (What's Implemented)

- Wallet + AgentKit: Smart Wallet and ETH account providers with Base-only gating; unified send/sign/address helpers. Files: `libs/agentkit_ext/agentkit_wallet.py`, `services/wallet/provider.py`.
- Staking: `StakingService` wraps approve/stake/claim/unstake; `status()` performs safe, ABI-light on-chain reads. Files: `services/staking/client.py`, `libs/agentkit_ext/actions.py`, `abi/staking.json`.
- DIEM service: On-chain `mint`/`burn` via AgentKit, plus DEX trading through an aggregator (Uniswap V2, Aerodrome). Files: `services/diem/client.py`, `libs/agentkit_ext/actions.py`, `libs/dex/providers.py`, `abi/*.json`.
- Market data: Best-price quoting via aggregator; DIEM/VVV signal fetch with caching/retry; unified signals. Files: `services/marketdata/provider.py`, `libs/pricing/diem.py`.
- Token watcher (Base/Etherscan v2): Periodic snapshots for price (via DEX), supply, holders, 24h transfers; persists to SQL. Files: `services/marketdata/token_watcher.py`, `db/models.py`.
- Venice SDK + Keys: Configurable paths, chat/models/signals; KeyManager supports challenge-based root key and scoped subkeys; revoke and list helpers. Files: `libs/venice_sdk/client.py`, `services/venice_keys/manager.py`.
- Broker API: FastAPI service with tenants, chat proxying, per-tenant limits, idempotency middleware, optional KV-backed rate limiting, `/v1/debug/counters`, `/v1/env`, metrics, and static admin UI at `/admin`. Files: `apps/broker-api/app.py`, `apps/broker-api/tenant_store*.py`, `apps/control-plane/*`.
- CLI + Operator tools: Idempotency purge, KV→SQL compaction, counters view, env status, rotate/probe, Venice OpenAPI probe. Files: `apps/cli/main.py`, `scripts/*`, `Makefile`.
- Graph + nodes: Minimal sequential or LangGraph-based pipeline (wallet → staking → DIEM decision → broker router) with optional LangSmith tracing. Files: `graph/langgraph/*.py`.
- Agents: StakeMaster (claim in live mode), ArbiDiem (risk-gated mint/sell decision and execution), CapacityBroker (key issuance wrapper), AI Treasurer (initial rebalance heuristic). Files: `agents/*`.
- Tests (select highlights): Risk policy sizing and conversions; ArbiDiem integrates risk limits; DIEM service delegates to on-chain actions; Broker idempotency + purge CLI; Rate limits (KV/memory/redis-optional); Admin counters endpoint stubs. Files: `tests/*.py`.
- Docs + config: Deployment guide, Base DEX router addresses, README with make targets and env guidance. Files: `docs/*.md`, `config/*.yml`, `.env.example`.

---

### 🔍 Gaps and Pending Tasks

- DEX trading modes: Only exact-in “sell” path is wired end-to-end. Missing exact-out “buy” flow (getAmountsIn + swapTokensForExactTokens) and fee-on-transfer fallback routing; aggregator API needs a buy-side path and slippage validation hooks.
- Risk depth: Portfolio exposure (VVV/DIEM/USDC) not computed; no liquidity/volatility-aware sizing; no pool-depth/slippage guardrails; risk context not persisted or surfaced in observability.
- Orchestrator maturity: Quorum scaffolding exists but not coordinating all agents nor persisting decisions/outcomes; no listen-interval/backoff policy; no unified eventing between nodes/agents.
- Capacity Broker agent: Minimal key issuance only; lacks dynamic pricing/allocation logic tied to utilization and market pricing; missing integration loops with Broker API (quote → purchase verify → subkey issuance) for resale.
- AI Treasurer: Only a simple buffer-based rebalance delta; needs budget/constraint modeling, coordination with staking/DIEM inventory, and explainable proposals.
- On-chain ops safety: No dry-run/idempotency guards on mint/burn; no domain events emitted; limited error-class surfacing and correlation IDs in logs.
- Observability: Agent loop metrics and decision logs not exposed in `/metrics`; limited structured tracing across agents/graph; limited admin toggles to pause/resume agents and adjust thresholds live.
- Hardening: Security defaults (require admin token in prod), CORS allowlists, robust receipts/audit trails for quotes/purchases, env sanity checks; encoding artifacts in some docs should be cleaned up.
- E2E coverage: Integration tests missing for exact-out trades, aggregator slippage guards, orchestrator decision branches, and buyer lifecycle.

---

### 📦 Updated Implementation Plan to Reach v1

1) DEX + Trading Enhancements
- Add exact-out “buy” support to `libs/dex.providers` and `DIEMACTIONS.trade` (getAmountsIn + swapTokensForExactTokens).
- Add fee-on-transfer fallback for exact-in (swapExactTokensForTokensSupportingFeeOnTransferTokens).
- Extend aggregator API to choose path per side with explicit slippage guards and provider health telemetry.

2) DIEM Service Safety + Events
- Add `dry_run` and idempotency key options to `services/diem/client.py` for mint/burn.
- Emit structured domain events for mint/burn/trade with correlation IDs; surface error classes consistently.

3) Risk Policy 1.1
- Compute portfolio exposure (USD) across VVV/DIEM/USDC using TokenWatcher or MarketDataProvider.
- Add liquidity/volatility-aware sizing caps and slippage thresholds; expose decisions in a compact struct.
- Gate DIEM actions through risk API; persist last decision and reason for audits.

4) Orchestrator (Quorum) Build-Out
- Compose StakeMaster, ArbiDiem, CapacityBroker, and AITreasurer in a unified graph/runner.
- Add listen-interval/backoff policy, centralize signal ingestion, and persist decisions/outcomes (SQL or KV).
- CLI entry to run orchestrator with flags for dry-run/live and feature gates.

5) Capacity Broker Agent 1.0
- Integrate `libs/pricing.engine` (Static/Market) for dynamic pricing; add utilization feedback from counters.
- Build loop to publish quotes, verify purchases, and issue scoped subkeys via Broker API.

6) AI Treasurer 0.1
- Define budget/constraints; compute allocations across VVV/DIEM/USDC; integrate staking actions where safe.
- Produce explainable proposals and optionally auto-apply under feature flag.

7) Observability + Ops
- Extend `/metrics` with agent loop metrics, DIEM ops, and error classes; add structured logs with correlation IDs.
- Admin toggles via env or API to pause/resume agents and adjust thresholds without redeploys.

8) Hardening + Security
- Enforce `BROKER_REQUIRE_ADMIN_TOKEN=true` in production; set CORS allowlists for admin/buyer surfaces.
- Improve receipts/audit logs for quotes/purchases; document recovery/rotation runbooks.

9) Tests + CI
- Add integration tests for exact-out trades and slippage checks; orchestrator branches and buyer lifecycle.
- Keep mocks and monkeypatches lightweight to avoid external dependencies in CI; document required env gates.

10) Docs + Defaults
- Update `.env.example` for DEX/buyer/orchestrator flags; refresh README/DEPLOYMENT with new flows and runbooks.
- Fix encoding artifacts in plan/docs; ensure Base router docs reflect current addresses.

---

### 🚀 Next Steps Toward Final System (Focused on Agents + System Features)

- Implement buy-side in aggregator: add getAmountsIn + swapTokensForExactTokens, plus FOT fallback; unit tests for both providers.
- Add `dry_run` + idempotency guard to `DIEMService.mint/burn`; return structured events and surface errors.
- Extend `RiskPolicy` with portfolio exposure and slippage caps; wire into ArbiDiem and orchestrator.
- Build orchestrator loop: unify agents under `graph/langgraph`, add listen-interval/backoff, persist decisions; CLI `run:quorum` to drive it.
- Capacity Broker loop: integrate pricing → quote → purchase verify → subkey issuance; minimal utilization feedback.
- Observability: add agent metrics and decision logs; correlation IDs across actions; optional tracing via LangSmith.
- Documentation: update README/DEPLOYMENT and admin runbooks; ensure `.env.example` matches new flags; clean up encoding issues.

