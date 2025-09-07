# Implementation Plan v2 — Current State, v1 Scope, and Alignment

This document reflects the system’s current implementation, defines the “stop line” for a working v1 (core infrastructure complete), and aligns scope with the original plan in `implementation-plan.md`.

## Current State (Implemented)

- Wallets + AgentKit: CDP Smart Wallet and ETH account providers with Base-only gating; unified send/sign/address helpers. Files: `libs/agentkit_ext/agentkit_wallet.py`, `services/wallet/provider.py`.
- Staking: `StakingService` for approve/stake/claim/unstake; `status()` performs safe, ABI-light on-chain reads. Files: `services/staking/client.py`, `libs/agentkit_ext/actions.py`, `abi/staking.json`.
- DIEM service: On-chain `mint`/`burn` via AgentKit plus DEX trading through an aggregator (Uniswap V2, Aerodrome). Emits `diem.*` events with optional `correlationId`. Files: `services/diem/client.py`, `libs/agentkit_ext/actions.py`, `libs/dex/providers.py`, `abi/*.json`.
- DEX trading + telemetry: Exact-out buy via Uniswap V2 (`getAmountsIn` + `swapTokensForExactTokens`); exact-in sell with FOT fallback (`swapExactTokensForTokensSupportingFeeOnTransferTokens`). Aggregator/provider metrics (quotes/trades, selections/errors) and latency buckets at `/metrics`. Per-provider circuit breaker (open/skip/reset) via env thresholds; Aerodrome is skipped for exact-out by design. Files: `libs/dex/providers.py`.
- Market data: Best-price quoting via aggregator; VVV metrics (circulating supply, utilization, staking_yield) with caching/retry; DIEM balances/quotas from rate-limits; unified signals. Files: `services/marketdata/provider.py`, `libs/pricing/diem.py`.
- Token watcher: Periodic snapshots for price (via DEX), supply, holders, 24h transfers; persists to SQL. Files: `services/marketdata/token_watcher.py`, `db/models.py`.
- Venice SDK + Keys: Configurable paths; chat/models and explicit VVV metrics helpers; DIEM balances via rate-limits; KeyManager supports challenge-based root key and scoped subkeys; revoke and list helpers. Files: `libs/venice_sdk/client.py`, `services/venice_keys/manager.py`.
- Broker API: FastAPI with tenants, chat proxying, per-tenant limits, idempotency middleware, optional KV rate limiting, `/v1/debug/counters`, `/v1/env`, metrics, and static admin UI at `/admin`. Files: `apps/broker-api/app.py`, `apps/broker-api/tenant_store*.py`, `apps/control-plane/*`.
- Broker ops UX: `/v1/env` now includes a non‑secret environment snapshot for Web3/DEX/pricing/ABI and emits concise startup logs showing RPC/chainId, DEX routers, pricing path, and ABI presence. Useful to quickly diagnose quote/DEX issues without secrets.
- Orchestrator: Single-agent loop with backoff; persists decision records (SQL when available); decision metrics and `correlationId`. Decision `details` include limits and `why` (premium/threshold, slippage, desired/suggested units). Files: `graph/workflows/orchestrator.py`, `apps/cli/main.py`.
- CLI + Operator tools: Idempotency purge, KV/SQL compaction, counters view, env status, rotate/probe, Venice OpenAPI probe. Files: `apps/cli/main.py`, `scripts/*`, `Makefile`.
- Agents: StakeMaster (claim in live mode), ArbiDiem (risk-gated mint/sell with slippage gate), CapacityBroker (key issuance wrapper), AI Treasurer (initial rebalance heuristic). Files: `agents/*`.
- Tests: Risk policy sizing and conversions; ArbiDiem risk integration; DIEM buy path and FOT fallback; Broker idempotency + purge CLI; rate limits; orchestrator portfolio-cap wiring. Files: `tests/*.py`.
- Docs + config: README/DEPLOYMENT/ADMIN updated with DEX modes/metrics and flags; Base router addresses; `.env.example` includes `BROKER_REQUIRE_ADMIN_TOKEN`, `AGENTS_PAUSED`, pricing flags. Files: `README.md`, `docs/*.md`, `.env.example`.
 - Env loading: `.env` is auto‑loaded by CLI and Broker via a thin wrapper around `python-dotenv` with a safe fallback parser. `python-dotenv` added to base deps for consistency.
 - Replit defaults: `.replit` runs `uv sync --extra broker` by default (can extend with `UV_EXTRAS`), removing ambiguity between workflows.
 - Venice readiness + ops UX: `/v1/env` exposes `venice.ready` with per-check `readyReason` (models/vvv), `signals.offline`, and a `venice` snapshot; Admin UI card shows config, recent signals, inline path probe, banner when NOT READY; dev-only offline signals supported via `VENICE_OFFLINE_SIGNALS`.
 - Admin receipts UI: Purchases table lists `purchaseId`, `quoteId`, `asset`, `amountPaid`, `status`, `expiresAt`; JSON views remain for details.
 - Quotes preview CLI: `quotes:preview` exercises liquidity-aware metrics without trading; uses aggregator preview + risk policy and logs adjusted units and slippage.
 - Price quoting resilience: MarketDataProvider automatically retries pricing with a Base WETH bridge (DIEM→WETH→USDC) when a direct pair lacks liquidity, improving `quotes:preview` usefulness without trading.

## Gaps and Pending (Prioritized)

1) DEX trading modes
- Completed: venue E2E (exact-out skip for Aerodrome; exact-in selection) and router docs finalized.
- Pending: monitor Aerodrome ABI for `getAmountsIn`; add exact-out support if/when available; keep negative tests in place to guard regressions.

2) Risk depth
- Extend from slippage-caps to liquidity/volatility-aware sizing; add pool-depth guardrails inside aggregator; persist risk decision context to metrics.
  - Implemented: conservative liquidity-aware sizing in ArbiDiem using aggregator preview + halving backoff; emits `vvv_risk_liquidity_*` metrics and records rationale.

3) Orchestrator maturity
- Centralize signal ingestion; unify eventing across agents; refine decision store schema (limits/why) as it stabilizes.
  - Implemented: centralized `signal.market.prices` and `signal.market.signals` events emitted from MarketDataProvider; decisions already persisted with `why` context.

4) Venice API alignment (signals → metrics)
- Completed: Removed fictional `/diem` signals. Added explicit VVV metrics endpoints in client and provider; DIEM balances fetched via `/api_keys/rate_limits`. Updated CLI/Broker readiness and docs/config.
- Pending: Monitor deployments that expose legacy `/vvv` aggregate; keep compatibility. Consider adding CLI examples/output snapshots for VVV metrics.

5) CapacityBroker agent
- Evolve from issuance wrapper to dynamic pricing/allocation tied to utilization and market pricing; integrate quote → purchase verify → subkey issuance loop.

5) AI Treasurer
- Move beyond buffer heuristic to budget/constraint modeling; coordinate with staking/DIEM inventory; include explainable proposals.

6) On-chain ops safety
- Normalize error-class surfacing; add structured error mapping; keep dry-run/idempotency and correlation IDs.

7) Observability
- Add agent-loop and error-class metrics; optional tracing across agents/graph.
  - Implemented: `/v1/env` surfaces recent `signal.market.*` and a Venice config snapshot; Admin UI card shows both for quick ops validation.
  - Implemented: Venice readiness booleans with per-check `readyReason` and offline signals indicator; server-side and CLI path probes to recommend correct env.

8) Hardening
- Enforce secure defaults (admin token in prod), CORS allowlists, receipts/audit trails for quotes/purchases, env sanity checks.
  - Implemented: purchase verification attaches a JSON receipt to the `Purchase` row and emits `purchase.verified` events; Alembic migration `0005_purchase_receipt` added.

9) E2E coverage
- Add buyer lifecycle E2E: quote → on-chain purchase verification → scoped subkey issuance; expand orchestrator branch coverage.

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
- Broaden venue tests and document Aerodrome exact-out limitation explicitly in `docs/BASE_DEX_ROUTERS.md` (tests updated; keep monitoring ABI).
- Validate liquidity-aware sizing across venues/sizes; tune slippage buckets/labels; add a non-dry orchestration pass for preview quotes.
- Minimal consumers of `signal.*` events (e.g., cache warmers) now that centralized emission exists.
- Harden Broker API defaults (prod-safe CORS, admin token required) and finalize receipts UX/admin listings.
- Venice config alignment runbook: add `venice:probe-openapi` guidance to ADMIN and DEPLOYMENT; ensure base includes `/api/v1` and override paths when needed.
 - CI/health gate: Implemented as `ci:gate` in CLI. Keep in the release checklist and wire into CI where applicable.

Explicitly out-of-scope for v1 (post‑v1 backlog):
- Quorum multi-agent orchestration and advanced agendas.
- AI Treasurer optimization beyond buffer heuristic.
- Dynamic pricing/allocation for CapacityBroker; DIEM rentals.
- Full tracing across agents and graph.
- Market hedges and stop-loss automations.
 - Aerodrome multi‑hop trade path support (quotes already support multi‑hop via routes; trade currently uses `swapExactTokensForTokensSimple` single‑hop).

## Known Gaps Discovered During Validation

- DEX path discovery can fail when a direct pair lacks a pool. Addressed for price preview by adding a WETH bridge fallback (Base), but trading on Aerodrome remains single‑hop; multi‑hop trade support is deferred beyond v1.
- Developer experience in hosted environments (Replit): default extras were ambiguous; `.replit` now ensures `--extra broker` is installed. Document extending with `UV_EXTRAS` for web3/agentkit/graph when needed.
- Operational clarity: added public env snapshot in `/v1/env` and startup logs to show effective RPC/routers/path/ABI. Keep this as a standard ops check.

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

- Verify liquidity-aware sizing under more venues and sizes; tune buckets/labels.
- Wire minimal consumers of `signal.*` events (e.g., orchestrator-driven cache). Admin UI now surfaces recent signals; keep.
- Enforce prod defaults (admin token, CORS) in deployment profiles and add a CI check.
- Add buyer lifecycle E2E (quote → purchase verify → subkey issuance) and orchestrator branch tests.
- Add Venice alignment note: ensure `VENICE_API_BASE_URL` includes `/api/v1`. For VVV metrics, prefer explicit paths and override when deployments differ: `VENICE_VVV_CIRC_PATH`, `VENICE_VVV_UTIL_PATH`, `VENICE_VVV_YIELD_PATH` (legacy aggregate: `VENICE_VVV_PATH`). DIEM balances come from `GET /api_keys/rate_limits` (no DIEM signals endpoint). Use `venice:probe-openapi`.
- Cut a v1 tag with STATUS updated and core agents enabled.
 - Gate production deploys on readiness: `venice.ready`, admin token, and CORS allowlist; add a smoke `quotes:preview` step to ensure aggregator is functional.

Nice‑to‑have (post‑v1 hardening, if time remains):
- Document a DEX setup checklist (RPC/routers/path/ABI) and add a `make setup-broker` target shortcut.
- Add a small unit test for MarketDataProvider’s bridge fallback path.

---

Changelog
- 2025-09-06: Venice API alignment: removed non-existent `/diem` signals; added explicit VVV metrics endpoints (circulatingsupply/utilization/staking_yield); DIEM balances via `/api_keys/rate_limits`; updated CLI `venice:signals`, Broker readiness, and docs/config; improved 404 error hints.
- 2025-09-07: Ops UX & DX: `/v1/env` extended with web3/dex/pricing/abi; startup logs show effective config; `.replit` defaults to `--extra broker`; `.env` loading standardized via `python-dotenv`; MarketDataProvider adds WETH bridge fallback for preview quotes.
- 2025‑09‑06: Admin UI card for Venice config + recent signals; `/v1/env` exposes `venice` snapshot; optional `VENICE_OFFLINE_SIGNALS` fallback for local dev.
 - 2025‑09‑06: Added Venice readiness with `readyReason`; Admin receipts table; CLI `quotes:preview`; improved error hints for Venice client; server-side path probe.
- 2025‑09‑06: Added liquidity-aware sizing with metrics; emitted centralized market signals; attached purchase receipts and events; updated docs and migration 0005.
- 2025‑09‑06: Expanded DEX venue E2E tests; finalized router capabilities docs; ensured Web3 deps in base env; tightened UniswapV2 trade path to avoid unnecessary Web3 import in tests.
- 2025‑09‑05: Rewrote v2 to reflect current state, clarified the v1 stop line, removed encoding issues and inconsistent formatting, and aligned scope to the original plan.
