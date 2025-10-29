# Implementation Plan: Agents

This plan keeps the agent layer aligned with the production repository.

It supersedes the legacy LangGraph draft and documents the single loop orchestrator in `graph/workflows/orchestrator.py`.

## Scope

Focus on StakeMaster, ArbiDiem, CapacityBroker, Quorum, Reflex Guardian, and AI Treasurer.

Summarize supporting services and configuration so engineers can extend agents without inspecting every module.

Use `docs/implementation-plan-broker.md` for HTTP surface details.

## System Snapshot

The orchestrator runs StakeMaster, ArbiDiem, and CapacityBroker sequentially in each cycle.

Quorum voting gates ArbiDiem, the Reflex Guardian can halt execution, and the AI Treasurer records advisory guidance.

`services/memory/store.py` persists cycle data while `services/memory/reflection.py` critiques outcomes.

Progressive live governs when dry runs hand off to real transactions.

## Agent Responsibilities

### StakeMaster

- Keeps the staking heartbeat alive through Venice using `STAKEMASTER_HEARTBEAT_*`.

- Auto-claims and restakes rewards when `live=True` and gas estimates from `services/staking/client.py` pass thresholds.

- Supports progressive auto stake via `VVV_ACTIVE_MIN_STAKE_UNITS` and wallet balance probes.

StakeMaster caches heartbeat and claim timestamps in the KV store to avoid duplicate Venice calls.

Gas overrides follow `STAKEMASTER_PRIORITY_FEE_*` and `STAKEMASTER_STAKE_GAS_LIMIT` when nonce conflicts appear.

### ArbiDiem

- Uses `services/diem/client.py` plus the DEX aggregator to mint, sell, buy, and burn DIEM.

- Sizes orders with `services/risk/policy.py`, including utilization and volatility multipliers.

- Honors price sanity clamps from `services/marketdata/provider.py` and requires exact out support for buy backs.

ArbiDiem tracks `_last_rationale` for telemetry and exports risk helpers such as `pool_take_bps_cap`.

### CapacityBroker

- Issues scoped Venice sub keys with `services/venice_keys/manager.py`.

- Enforces `consumptionLimit` and `expiresAt` across all sub keys.

- Derives utilization driven pricing and inventory failsafes for the broker runtime.

### AI Treasurer

- Operates in advisory mode and records acquire, hold, or release guidance.

- Targets a 1.5 day DIEM buffer via `agents/ai_treasurer/agent.py`.

- Feeds Quorum with buffer signals without executing trades.

### Quorum and Models

- `agents/quorum/core.py` collects weighted votes with `Quorum.decide_with_details`.

- `agents/quorum/models.py` implements Yield, Arb, Risk, Demand, and Treasury models with configurable weights.

- Environment flags `QUORUM_ENABLE`, `QUORUM_THRESHOLD`, and `QUORUM_WEIGHT_*` tune participation.

### Reflex Guardian

- `agents/reflex/guardian.py` vetoes live execution when volatility, drawdown, or staking health breach limits.

- Uses `REFLEX_MAX_VOL_BPS`, `REFLEX_MAX_PRICE_DRAWDOWN`, and `REFLEX_STAKE_INACTIVE_CONSEC`.

- Records warnings for high utilization so operators can throttle broker tenants.

- Defaults halt live trades once realized volatility tops 450 bps or drawdowns exceed 0.12.

## Orchestrator Flow

`SingleLoopOrchestrator.run_cycle` gathers market signals, simulates ArbiDiem, and builds a `QuorumContext`.

The ArbiDiem simulation runs with `simulate=True` so Quorum can vote before any live action.

If Quorum approves and the Reflex Guardian has not halted, the orchestrator replays the evaluation with `simulate=False` in live mode.

Price guards clamp stale or volatile data using `MARKETDATA_PRICE_SANITY_MAX_DRIFT` and `ARBI_PRICE_GUARD_*`.

Progressive live tracks `STAKEMASTER_PROGRESSIVE_ENABLE` and flips to real execution after healthy heartbeats.

Cycle results append to memory, capture treasury plans, and compute adaptive listen intervals.

## Supporting Services

`services/staking/client.py` wraps AgentKit staking calls, fallback snapshots, and cooldown tracking.

`services/diem/client.py` handles mint, burn, lock metadata, aggregator quotes, and exact out trades.

`services/marketdata/provider.py` unifies Venice metrics, DEX paths, Etherscan discovery, and price sanity clamps.

`services/risk/policy.py` exposes trade sizing, utilization multipliers, volatility caps, and exposure calculations.

`services/venice_keys/manager.py` generates root keys, scoped keys, and supports challenge signing.

`services/memory` modules persist cycle records, while `libs.telemetry` provides structured logging and metrics.

## Configuration Checklist

Core environment:

- `VENICE_API_BASE_URL`, `VENICE_API_KEY`, and optional path overrides from `docs/AGENTS.md`.

- Base chain setup with `BASE_RPC_URL`, `BASE_CHAIN_ID`, `VVV_TOKEN_ADDRESS`, `DIEM_TOKEN_ADDRESS`, and staking contract addresses.

- DEX routing via `DEX_PROVIDERS`, router addresses, and `TRADE_PATH` for DIEM quotes.

Risk and guardrails:

- Trade sizing via `DIEM_PREMIUM_THRESHOLD`, `DIEM_DISCOUNT_THRESHOLD`, `RISK_MAX_SLIPPAGE_BPS`, and `RISK_MAX_POOL_TAKE_BPS`.

- Guard knobs `REFLEX_MAX_VOL_BPS`, `REFLEX_MAX_PRICE_DRAWDOWN`, `ARBI_PRICE_GUARD_MAX_DRIFT`, and `MARKETDATA_PRICE_SANITY_MAX_DRIFT`.

- Default thresholds ship at 450 bps volatility, 0.12 drawdown, and four hold streaks; adjust with `REFLEX_*`, `REFLECTION_VOL_BPS_THRESHOLD`, or `REFLECTION_HOLD_STREAK`.

- Progressive live using `STAKEMASTER_PROGRESSIVE_CYCLES`, `STAKEMASTER_PROGRESSIVE_ENABLE`, and `ARBI_PRICE_GUARD_STREAK_MAX`.

- Enable `RISK_VOL_PERSIST=1` once SQL storage is available to persist `db.models.PriceTick` records.

Memory and analytics:

- `AGENT_MEMORY_PATH`, `REFLECTION_VOL_BPS_THRESHOLD`, and `REFLECTION_HOLD_STREAK` for reflection tuning.

- `DIEM_DEBUG_ROUTES` and `MARKETDATA_DEBUG_SANITY` when troubleshooting routing or guardrail behaviour.

- `RISK_VOL_PERSIST` to persist price ticks into SQL if long lived analytics is required.

## Implementation Roadmap

Phase 1 focuses on staking foundations: verify `services/staking`, heartbeat KV storage, and progressive auto stake.

Phase 2 finalizes DIEM execution: confirm aggregator configuration, price guards, and Quorum integration with simulated decisions.

Phase 3 strengthens broker coordination: ensure CapacityBroker utilization feeds Quorum and treasury signals.

Phase 4 hardens guardrails: calibrate Reflex limits, reflection thresholds, and SQL persistence for long term telemetry.

Defaults now enforce the calibrated thresholds above and gate price tick persistence behind `RISK_VOL_PERSIST`.

## Testing and Validation

Run `uv run pytest -q` to cover orchestrator, risk, DIEM, DEX, and broker suites.

Targeted tests include `tests/test_single_loop_orchestrator.py`, `tests/test_diem_service.py`, `tests/test_arbi_diem_risk_integration.py`, `tests/test_dex_exact_out.py`, and `tests/test_broker_limits.py`.

`uv run python apps/cli/main.py run:loop --sleep 15 --max-cycles 3` exercises the orchestrator end to end.

`uv run python apps/cli/main.py startup:probe` sanity checks market data before enabling live mode.

## Operational Practices

Set `AGENTS_PAUSED=true` to freeze live trades while keeping telemetry online.

Use `uv run python apps/cli/main.py venice:keys:cleanup --dry-run` to spot parent key drift before CapacityBroker cycles.

Enable `--progressive-live` for staged go live and monitor `logs/runtime.log` for price guard streaks.

## References

Tokenomics and mint mechanics live in `docs/venice-diem-tokenomics.md`.

On chain routing guidance resides in `docs/EtherScan.md`.

Broker surfaces and tenant policy are documented in `docs/implementation-plan-broker.md`.
