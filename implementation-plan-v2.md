# Implementation Plan — V2

This document tracks the iterative scope for the Venice Capacity Broker (replit branch). It summarizes what is done and what remains to reach the fuller architecture in `implementation-plan.md`. If there is any conflict, treat `implementation-plan.md` as the source of truth for functional boundaries.

---

## Done (on `replit` branch)

1) Environment & Config
- `.env` template and `config/default.yml` wired with Base chain addresses and DEX configuration.
- Etherscan v2 guidance (`chainid=8453`) documented for on-chain probes.

2) Core Services
- Staking (`services/staking`): approve/stake/claim/unstake + status reads.
- Market data (`services/marketdata`): DEX quotes + Venice VVV metrics and DIEM balance.
- Venice SDK + Key manager: API key lifecycle and scoped sub-keys.
- DEX aggregator (`libs/dex`): Uniswap V2 and Aerodrome, with slippage controls and circuit-breaker.
- CLI tooling (`apps/cli`): staking, trading, usage/admin utilities, startup DEX probe.
- Broker API: tenants, scoped subkeys, limits and counters.

3) Agents & Orchestrator
- StakeMaster: active-staker upkeep and reward claims.
- ArbiDiem: premium detection, mint+sell path, liquidity-aware sizing via aggregator previews.
- CapacityBroker: minimal issuance; resale API.
- AITreasurer: initial buffer logic.
- Orchestrator: single-agent loop with persistence and portfolio-cap wiring.

4) Testing & Observability
- Unit tests across trading paths, DIEM service, risk policy, broker limits and orchestrator.
- Prometheus metrics and light event bus for decisions and signals.

---

## Recent Changes (this iteration)

1) DIEM Service (Complete for v1)
- Implemented on-chain `mint`/`burn` via AgentKit actions in `services/diem/client.py` with robust error propagation and telemetry events.
- Optional sVVV capacity gate: `DIEM_ENABLE_SVVV_GATE` with mint-rate via `DIEM_MINT_RATE_SVVV_PER_DIEM` (base units) or `DIEM_MINT_RATE` (token units, decimals-aware). Available capacity override via `DIEM_SVVV_AVAILABLE_UNITS`.
- Added in-memory state helpers: `last_results()` and `totals()`.
- Tests: `tests/test_diem_capacity_gate.py` added; full suite green.

2) Risk Module (Incremental)
- New hooks in `services/risk/policy.py`:
  - `utilization_multiplier(utilization_ratio)` using `RISK_UTIL_ALPHA`.
  - `volatility_bps(prices)` and `cap_by_volatility(units, vol_bps)` using `RISK_MAX_VOLATILITY_BPS`.
  - `size_with_risk(...)` composing base limits + utilization + volatility.

3) Wiring
- ArbiDiem now uses `RiskPolicy.size_with_risk(...)` when `utilization_ratio`/`vol_bps` are provided (falls back to existing limits if not).
- Orchestrator computes utilization from `MarketDataProvider.vvv_metrics()` and keeps a small DIEM price history to compute realized volatility (bps) via the risk policy; passes both to ArbiDiem when not in dry-run.
 - Optional SQL-backed price history buffer added (disabled by default via `RISK_VOL_PERSIST`).

---

## Next

1) Advanced Agents (closed for v1 scope)
- ArbiDiem: risk-aware sizing wired (utilization, volatility), liquidity preview, and simulate/live modes. Further enhancements (full mint-rate awareness, hedging) deferred post‑v1.
- CapacityBroker & AITreasurer: retain minimal behavior; advanced strategies deferred post‑v1.
- Quorum: remains post‑v1; single orchestrator loop is the v1 stop line.

2) Market & Pool Discovery (next focus)
- Etherscan v2 factory probes (`getPair`, reserves) to discover DIEM/USDC and VVV/DIEM pools; multi-hop pathing via WETH where needed.
 - Added `services.marketdata.etherscan_verify.get_token0/get_token1` for precise reserve mapping.
 - Added `MarketDataProvider.discover_trade_path()` and `reserve_cap_units()` to expose pool discovery and a reserve-based sizing cap (env `RISK_MAX_POOL_TAKE_BPS`, default 1%).
 - Tests cover reserve-cap computation with monkeypatched Etherscan responses.

3) Event Watchers
- On-chain listeners for stake/unstake, DIEM mint/burn, and large trades to trigger policy adjustments or alerts.

4) Broker Enhancements
- Tenant self-service, quota adjustments, usage dashboards, security hardening (rotation, expiry warnings).

5) Tests & Docs
- Expand tests around utilization/volatility-driven sizing and orchestration decisions.
- Update AGENTS.md and README with the new DIEM/Risk environment variables and examples (in progress).

---

## Immediate Follow-ups
- Documentation: ensure README/AGENTS enumerate new envs with examples.
- Optional: persist price history in SQL for richer volatility analytics post‑v1.
