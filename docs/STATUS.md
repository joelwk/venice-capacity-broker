# Implementation Status (Plan Alignment)

This snapshot summarizes current functionality vs the implementation plan (`implementation-plan`). Priority remains: core infrastructure and marketplace first, then agents.

What's Done
- Broker API: multi-tenant tenants/limits/chat endpoints; idempotency middleware; optional KV-backed sliding-window limiter; JSON store with optional SQL store; basic metrics at `/metrics`. Admin broker limits now persist in production after setting `KV_URL`/`KV_API_TOKEN`.
- Admin UI: static control panel at `/admin` (token prompt, health/env, tenants, limits, chat probe); buyer page at `/admin/buy.html`.
- Marketplace: feature-gated Quotes and Purchases; on-chain ETH/USDC verification (Base RPC) with scoped Venice subkey issuance on success; admin listings for quotes/purchases/utilization. `/v1/market/prices` and `/v1/market/tokens` now surface live DIEM/VVV pricing (last QA: DIEM ~219.3, VVV ~1.2e-5, ETH ~4452.5).
- Venice SDK + Key Manager: autonomous root/subkey flows; CLI/Make helpers (`rotate-probe`, `db-compact`, `env-status`).
 - Risk: liquidity-aware sizing added to ArbiDiem with conservative halving backoff; emits `vvv_risk_liquidity_*` metrics and enriches decision rationale.
 - Signals: centralized `signal.market.*` events emitted by MarketDataProvider; DIEM events include optional correlationId.
 - Buyer receipts: purchase verification attaches a JSON `receipt` to Purchase and emits `purchase.verified` events; Alembic migration `0005_purchase_receipt` added.
 - DIEM service: on-chain `mint` and `burn` wired via AgentKit actions; optional sVVV capacity gate and lock/unlock hooks; CLI verbs `diem:mint` and `diem:burn` available.
 - Orchestrator: portfolio exposure cap wiring (env-gated) passes computed USD exposure to ArbiDiem; decision records include `ts` and `why` for debugging.
 - Broker limits self-service: tenant endpoints `GET/POST /v1/me/broker-limits` allow tenants to tighten rate limits (increase `windowSeconds`, decrease `maxRequests`); admin retains full control under `/v1/tenants/{id}/broker-limits`.
- Automation: `scripts/start_stack.py` (surface via `make run-stack`) keeps the Broker API, orchestrator, StakeMaster, and token watcher running together with opt-in live flags.

Needs Attention
- Migrations & recovery: ensure compaction and migration runbooks are robust.
- Price latency: `/v1/market/prices` now records latency buckets, warms the hot-symbol cache, and enforces an optional SLA gate; the endpoint still averages ~18 s, so keep probing RPC/DEX latency before scaling tenant traffic.
- Observability: prefer `starlette-exporter` metrics; add tracing hooks for graph/agents. DEX telemetry added (quotes/trades/latency, FOT fallback), agent decisions counters, and optional correlationId on DIEM events.
- Security: enforce `BROKER_REQUIRE_ADMIN_TOKEN=true` in prod; CORS allowlists for buyer/admin; secret hygiene; clear default model (`BROKER_DEFAULT_MODEL`).
- Pricing/risk: evolve static pricing to policy-driven engine; verify decimals/units; add receipts and audit trails.
- Wallet/ABIs: ensure required ABIs and Base RPCs are configured; exercise AgentKit Smart Wallet in non-dev flows.

Next Steps (Execution Order)
1) Stabilize market data latency and metrics (implementation plan Section 6: Market Data) so `/v1/market/prices` meets the latency target before scaling tenant traffic.
2) Promote the quorum orchestrator from single-agent to multi-agent (implementation plan Section 5) and wire decision logging into the existing telemetry surface.
3) Graduate the AI Treasurer from helper to automated execution per implementation plan Section 8, including guard rails and on-chain test coverage.
4) Expand capacity resale beyond scoped keys (implementation plan Section 4) with DIEM rental logic and lifecycle runbooks.

## Plan Gaps vs `implementation-plan.md`

- The orchestrator remains single-agent and lives in `graph/workflows/orchestrator.py:27`, so the quorum governance described in Section 5 of the plan is still future work.

- `agents/ai_treasurer/agent.py:12` keeps the treasurer logic as a pure calculation helper; no automated treasury execution layer exists yet despite the sprint roadmap in the plan.

- `graph/langgraph/graph.py:14` still defaults to a sequential fallback when LangGraph is missing, which means true LangGraph-native orchestration has not been validated end to end.

- Capacity resale stops at scoped sub-key issuance in `services/venice_keys/manager.py`; dynamic DIEM rentals and market-clearing logic remain outside the v1 build.

