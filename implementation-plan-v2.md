Implementation Plan v2 (concise)

Reflects actual repository state against implementation-plan.md and locks a clear v1 stop line. Keep v1 stable; push multi-agent/quorum features post‑v1.

## v1 Stop Line
- Single-agent loop orchestrator coordinating MarketData -> ArbiDiem decisions (persisted, backoff, dry-run friendly).
- Agents in scope: StakeMaster (heartbeat/claims), ArbiDiem (risk-gated mint/sell), CapacityBroker (minimal sub-key issuance). Minimal Treasurer heuristic allowed.
- DEX aggregator: UniswapV2 (exact-in/out) and Aerodrome (exact-in, exact-out intentionally not selected by aggregator). Slippage guard enforced.
- DIEM service: on-chain mint/burn via AgentKit actions; optional sVVV capacity gate and lock/unlock flows via env; CLI verbs for mint/burn/quotes.
- Risk policy: USD caps, units cap, slippage cap, utilization multiplier, volatility capping; liquidity-aware sizing via quote preview; max_stake helpers for staking caps.
- Signals: Venice VVV metrics and DIEM balances with caching/retry and offline stub; Etherscan v2 path discovery and reserve helpers.
- Broker API: minimal multi-tenant proxy with scoped sub-keys, limiter, idempotency, metrics; admin CLI helpers.
- Tests and observability: selected unit/integration tests; metrics/events; optional tracing knobs.

## Done
- Environment & config: Base addresses/routers, TRADE_PATH, slippage; `.env.example` populated; CLI OpenAPI probe suggests correct VENICE_* paths.
- Staking & wallet: approve/stake/claim/unstake; ETH account and Smart Wallet ready via AgentKit wrappers.
- Market data: VVV metrics and DIEM balances via Venice; best-price across DEX with bridge fallbacks; symbol pricing helpers.
- Venice SDK & keys: models/chat, rate-limits, root/sub-key flows; CLI and Broker integration; cleanup helper.
- DEX aggregator: quotes and trades (exact-in + exact-out on UniswapV2; FOT fallback; circuit breaker; latency buckets).
- DIEM service: mint/burn implemented via ABI-backed actions; optional sVVV capacity pre-check and mint-rate conversions; optional lock/unlock (pre-mint/post-burn) with cooldown metadata and telemetry; CLI verbs and quotes preview.
- Orchestrator & agents: single-loop orchestrator with price history/volatility, portfolio cap wiring (env-gated); ArbiDiem uses risk sizing + liquidity preview; StakeMaster CLI runner.
- Etherscan discovery: v2 proxy helpers for getPair/getReserves, startup probe, pool-reserve based sizing cap helper.
- Tests & metrics: coverage for DEX exact-out, FOT fallback, DIEM service, capacity gate, risk sizing, orchestrator util/vol; centralized events and Prometheus metrics.
  - New tests: DIEM lock/unlock sequencing and RiskPolicy max_stake helpers.

## Next (v1)
1) Liquidity fallback: when preview is unavailable, cap input by pool reserves using MarketData.reserve_cap_units; expose small metric for path taken (preview|reserve|none); integrate into ArbiDiem sizing.
2) Docs polish: tighten AGENTS.md/README snippets to reference `run:orchestrator` vs `run:quorum` for v1; clearly note Aerodrome exact-out is disabled by design; document DIEM lock/unlock toggles.
3) Config hygiene: add brief notes in `.env.example` for DIEM_* gate vars and RISK_* knobs already supported, plus lock/unlock toggles (DIEM_LOCK_ON_MINT, DIEM_UNLOCK_AFTER_BURN, DIEM_UNLOCK_COOLDOWN_SECONDS; DIEM_LOCK_FN/DIEM_UNLOCK_FN; VVV_LOCK_FN/VVV_UNLOCK_FN); ensure defaults are safe for dry runs.

## Post‑v1 Backlog
- Quorum multi-agent orchestration and richer Treasurer policies.
- Broker dynamic pricing/allocation and DIEM rentals.
- Event watchers driving adaptive listen intervals and inventory.
- Expanded UI/analytics for tenants, usage, and costs.

This keeps v1 lean, testable, and non-redundant while preserving a clean path to the multi‑agent roadmap.
