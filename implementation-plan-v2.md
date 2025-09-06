# Implementation Plan v2 — Current State, v1 Scope, and Alignment

This document reflects the system’s current implementation, defines the “stop line” for a working v1 (core infrastructure complete), and aligns scope with the original plan in `implementation-plan.md`.

## Current State (Implemented)

- Wallets + AgentKit: CDP Smart Wallet and ETH account providers with Base-only gating; unified send/sign/address helpers. Files: `libs/agentkit_ext/agentkit_wallet.py`, `services/wallet/provider.py`.
- Staking: `StakingService` for approve/stake/claim/unstake; `status()` performs safe, ABI-light on-chain reads. Files: `services/staking/client.py`, `libs/agentkit_ext/actions.py`, `abi/staking.json`.
- DIEM service: On-chain `mint`/`burn` via AgentKit plus DEX trading through an aggregator (Uniswap V2, Aerodrome). Emits `diem.*` events with optional `correlationId`. Files: `services/diem/client.py`, `libs/agentkit_ext/actions.py`, `libs/dex/providers.py`, `abi/*.json`.
- DEX trading + telemetry: Exact-out buy via Uniswap V2 (`getAmountsIn` + `swapTokensForExactTokens`); exact-in sell with FOT fallback (`swapExactTokensForTokensSupportingFeeOnTransferTokens`). Aggregator/provider metrics (quotes/trades, selections/errors) and latency buckets at `/metrics`. Per-provider circuit breaker (open/skip/reset) via env thresholds; Aerodrome is skipped for exact-out by design. Files: `libs/dex/providers.py`.
- Market data: Best-price quoting via aggregator; DIEM/VVV signals with caching/retry; unified signals. Files: `services/marketdata/provider.py`, `libs/pricing/diem.py`.
- Token watcher: Periodic snapshots for price (via DEX), supply, holders, 24h transfers; persists to SQL. Files: `services/marketdata/token_watcher.py`, `db/models.py`.
- Venice SDK + Keys: Configurable paths, chat/models/signals; KeyManager supports challenge-based root key and scoped subkeys; revoke and list helpers. Files: `libs/venice_sdk/client.py`, `services/venice_keys/manager.py`.
- Broker API: FastAPI with tenants, chat proxying, per-tenant limits, idempotency middleware, optional KV rate limiting, `/v1/debug/counters`, `/v1/env`, metrics, and static admin UI at `/admin`. Files: `apps/broker-api/app.py`, `apps/broker-api/tenant_store*.py`, `apps/control-plane/*`.
- Orchestrator: Single-agent loop with backoff; persists decision records (SQL when available); decision metrics and `correlationId`. Decision `details` include limits and `why` (premium/threshold, slippage, desired/suggested units). Files: `graph/workflows/orchestrator.py`, `apps/cli/main.py`.
- CLI + Operator tools: Idempotency purge, KV/SQL compaction, counters view, env status, rotate/probe, Venice OpenAPI probe. Files: `apps/cli/main.py`, `scripts/*`, `Makefile`.
- Agents: StakeMaster (claim in live mode), ArbiDiem (risk-gated mint/sell with slippage gate), CapacityBroker (key issuance wrapper), AI Treasurer (initial rebalance heuristic). Files: `agents/*`.
- Tests: Risk policy sizing and conversions; ArbiDiem risk integration; DIEM buy path and FOT fallback; Broker idempotency + purge CLI; rate limits; orchestrator portfolio-cap wiring. Files: `tests/*.py`.
- Docs + config: README/DEPLOYMENT/ADMIN updated with DEX modes/metrics and flags; Base router addresses; `.env.example` includes `BROKER_REQUIRE_ADMIN_TOKEN`, `AGENTS_PAUSED`, pricing flags. Files: `README.md`, `docs/*.md`, `.env.example`.

## Gaps and Pending (Prioritized)

1) DEX trading modes
- Broaden venue tests; codify Aerodrome exact-out non-support (ABI lacks exact-out preflight) in docs; revisit if ABI expands.

2) Risk depth
- Extend from slippage-caps to liquidity/volatility-aware sizing; add pool-depth guardrails inside aggregator; persist risk decision context to metrics.

3) Orchestrator maturity
- Centralize signal ingestion; unify eventing across agents; refine decision store schema (limits/why) as it stabilizes.

4) CapacityBroker agent
- Evolve from issuance wrapper to dynamic pricing/allocation tied to utilization and market pricing; integrate quote → purchase verify → subkey issuance loop.

5) AI Treasurer
- Move beyond buffer heuristic to budget/constraint modeling; coordinate with staking/DIEM inventory; include explainable proposals.

6) On-chain ops safety
- Normalize error-class surfacing; add structured error mapping; keep dry-run/idempotency and correlation IDs.

7) Observability
- Add agent-loop and error-class metrics; optional tracing across agents/graph.

8) Hardening
- Enforce secure defaults (admin token in prod), CORS allowlists, receipts/audit trails for quotes/purchases, env sanity checks.

9) E2E coverage
- Broaden integration tests for venues (exact-out, FOT) and orchestrator branches; buyer lifecycle.

## v1 Scope (“Stop Line”)

v1 is a working system with stable core infrastructure to run agents and iterate strategy. We deliberately stop here; enhancements roll into post‑v1.

Included in v1 (must be done):
- Wallet + Staking services on Base with tested flows (approve/stake/claim/unstake) and safe reads.
- DIEM service with buy/sell via aggregator (Uniswap V2 exact-out buys; exact-in sells with FOT fallback) and metrics at `/metrics`.
- Venice Keys: root challenge flow and scoped subkeys with limits; revoke/list.
- Broker API: multi-tenant quotas, idempotency, rate limiting option, counters endpoints, basic admin UI.
- Orchestrator: single-agent loop, persistence of decision records, decision metrics, correlation IDs.
- Agents: StakeMaster and ArbiDiem running with risk gating; CapacityBroker minimal issuance.
- Tests: unit + integration for trading paths, risk policy, broker limits, orchestrator portfolio cap.
- Security & ops: admin token required in prod, CORS allowlist, env sanity checks, receipts/audit trails for buys.
- Docs: ADMIN/DEPLOYMENT/STATUS updated; `.env.example` consistent.

Remaining for v1 (short list to finish):
- Broaden venue tests and document Aerodrome exact-out limitation explicitly in `docs/BASE_DEX_ROUTERS.md`.
- Add liquidity-aware sizing in risk engine (conservative defaults); persist decision context metrics.
- Unify agent eventing and centralize signal ingestion in orchestrator (minimal viable).
- Harden Broker API defaults (prod-safe CORS, admin token required) and add purchase receipts.

Explicitly out-of-scope for v1 (post‑v1 backlog):
- Quorum multi-agent orchestration and advanced agendas.
- AI Treasurer optimization beyond buffer heuristic.
- Dynamic pricing/allocation for CapacityBroker; DIEM rentals.
- Full tracing across agents and graph.
- Market hedges and stop-loss automations.

## Alignment with `implementation-plan.md`

- v1 corresponds to end of Sprint 3 (“DIEM mint/trade”), plus minimal hardening from Sprint 5 required for safe operation. Sprint 4 (“Quorum & treasury”) remains post‑v1 except the basic Treasurer heuristic already present.
- Monorepo layout and service boundaries match the original plan (`apps/`, `services/`, `agents/`, `graph/`, `libs/`, `infra/`, `tests/`).
- Coinbase AgentKit example parity kept for wallet patterns; LangGraph used for orchestration as planned.

## Formatting & Conventions (for this repo)

- Headings: sentence case after the first word; use `##`/`###` consistently.
- File paths: inline code style, e.g., `services/diem/client.py`.
- Endpoints and commands: inline code style, e.g., `/metrics`.
- Avoid emoji and special glyphs to prevent encoding issues; use plain text.
- Keep checklists short and prioritized; avoid speculative scope inside v1.

## Next Steps (to complete v1)

- Expand E2E venue tests and finalize docs for router capabilities.
- Add conservative liquidity-aware sizing; emit risk decision context metrics.
- Wire basic centralized signals + event bus across agents.
- Enforce prod defaults (admin token, CORS), add purchase receipts/audit trail.
- Cut a v1 tag with STATUS updated and core agents enabled.

---

Changelog
- 2025‑09‑05: Rewrote v2 to reflect current state, clarified the v1 stop line, removed encoding issues and inconsistent formatting, and aligned scope to the original plan.

