# Implementation Status (Plan Alignment)

This snapshot summarizes current functionality vs the implementation plan (`implementation-plan`). Priority remains: core infrastructure and marketplace first, then agents.

What's Done
- Broker API: multi-tenant tenants/limits/chat endpoints; idempotency middleware; optional KV-backed sliding-window limiter; JSON store with optional SQL store; basic metrics at `/metrics`.
- Admin UI: static control panel at `/admin` (token prompt, health/env, tenants, limits, chat probe); buyer page at `/admin/buy.html`.
- Marketplace: feature-gated Quotes and Purchases; on-chain ETH/USDC verification (Base RPC) with scoped Venice subkey issuance on success; admin listings for quotes/purchases/utilization.
- Venice SDK + Key Manager: autonomous root/subkey flows; CLI/Make helpers (`rotate-probe`, `db-compact`, `env-status`).
 - Risk: liquidity-aware sizing added to ArbiDiem with conservative halving backoff; emits `vvv_risk_liquidity_*` metrics and enriches decision rationale.
 - Signals: centralized `signal.market.*` events emitted by MarketDataProvider; DIEM events include optional correlationId.
 - Buyer receipts: purchase verification attaches a JSON `receipt` to Purchase and emits `purchase.verified` events; Alembic migration `0005_purchase_receipt` added.

Needs Attention
- Migrations & recovery: ensure compaction and migration runbooks are robust.
- Observability: prefer `starlette-exporter` metrics; add tracing hooks for graph/agents. DEX telemetry added (quotes/trades/latency, FOT fallback), agent decisions counters, and optional correlationId on DIEM events.
- Security: enforce `BROKER_REQUIRE_ADMIN_TOKEN=true` in prod; CORS allowlists for buyer/admin; secret hygiene; clear default model (`BROKER_DEFAULT_MODEL`).
- Pricing/risk: evolve static pricing to policy-driven engine; verify decimals/units; add receipts and audit trails.
- Wallet/ABIs: ensure required ABIs and Base RPCs are configured; exercise AgentKit Smart Wallet in non-dev flows.

Next Steps (Execution Order)
1) Core hardening: finalize metrics and `/v1/env` introspection (now on by default with builtin metrics); optional Redis-backed limiter when needed.
2) Marketplace to production: enable flags in deploys; add admin tables/UX for quotes, purchases, utilization; surface receipts; polish buyer UX.
3) Agent operations: add minimal consumers for `signal.*` events; expand StakeMaster/ArbiDiem loops; add Quorum and AI Treasurer workflows in LangGraph.
4) Docs & runbooks: keep `ADMIN.md` and `DEPLOYMENT.md` updated; include recovery/rotation procedures and rate-limit tuning guidance.
