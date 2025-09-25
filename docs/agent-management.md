# Agent Management Guide

This guide catalogs the production agents that operate the Venice Capacity Broker. Each profile covers the agent’s purpose, configuration expectations, core functionality, and the value it delivers to the system.

## StakeMaster

**Role**: Maintains staked VVV positions, compounds rewards, and preserves “active staker” status.

**Expected value**
- Keeps the staking allocation at its peak so DIEM emissions remain maximized.
- Smooths cash flow by restaking emissions and scheduling unlocks to avoid liquidity shocks.
- Provides heartbeat telemetry that proves the broker remains an active Venice participant.

**Core functionality**
- Reads staking status via `StakingService.status()`.
- Claims rewards and restakes when live mode is enabled and rewards exceed zero.
- Issues the configured Venice heartbeat call after each cycle; persists last heartbeat via KV when available.
- Emits structured telemetry (`staking.status`, `staking.claim`, `staking.heartbeat`) for downstream automations.

**Configuration requirements**
- Environment: `STAKEMASTER_HEARTBEAT_INTERVAL_HOURS`, `STAKEMASTER_HEARTBEAT_DISABLE`, `STAKEMASTER_HEARTBEAT_PROMPT`, `VENICE_HEARTBEAT_MODEL`, `VVV_ACTIVE_MIN_STAKE_UNITS`, `VVV_COOLDOWN_SECONDS`.
- AgentKit: VVV staking actions (`libs/agentkit_ext.actions.VVVActions`).
- Optional KV backend (`libs.kv`) for heartbeat persistence.

**Run surfaces**
- Combined loop: `uv run python apps/cli/main.py run:loop --sleep 15 --max-cycles 0`.
- Dedicated run: `uv run python apps/cli/main.py run:stakemaster --enable-live`.

## ArbiDiem

**Role**: Executes DIEM mint, sell, buy-back, and burn decisions subject to risk controls.

**Expected value**
- Monetizes DIEM price premia while protecting sVVV collateral.
- Ensures DIEM inventory is right-sized relative to utilization and volatility.
- Provides actionable rationale artifacts so operators understand each trade decision.

**Core functionality**
- Evaluates market price, mint rate, utilization, and volatility to determine whether to mint or burn.
- Sizes trades using `RiskPolicy` (`size_with_risk`, slippage caps, pool take limits).
- Invokes `DIEMService` for mint/burn and DEX trades; enforces exact-out preview on buy-backs before executing.
- Maintains `_last_rationale` with premium, suggested units, liquidity adjustments, and guardrail results.

**Configuration requirements**
- Market data: `TRADE_PATH`, optional `TRADE_PATHS`, DEX provider envs (`DEX_PROVIDERS`, router addresses), quote token configuration.
- Risk: `RISK_MAX_DIEM_TRADE_USD`, `RISK_MAX_DIEM_INVENTORY_USD`, `RISK_MAX_DIEM_TRADE_UNITS`, `RISK_MAX_SLIPPAGE_BPS`, `RISK_MAX_POOL_TAKE_BPS`, `RISK_UTIL_ALPHA`, `RISK_MAX_VOLATILITY_BPS`.
- DIEM service hooks: `DIEM_ENABLE_SVVV_GATE`, `DIEM_MINT_RATE_SVVV_PER_DIEM`, `DIEM_MINT_RATE`, `DIEM_LOCK_ON_MINT`, `DIEM_UNLOCK_AFTER_BURN`.
- Aggregator access via `libs/dex/providers.build_aggregator_from_env()`.

**Run surfaces**
- Combined loop: `uv run python apps/cli/main.py run:loop ...` (dry-run by default).
- Quorum dry-run: `uv run python apps/cli/main.py run:quorum --dry-run`.
- Direct actions: `uv run python apps/cli/main.py diem:mint <amount> [--dry-run]`, `... diem:burn <amount> [--dry-run]`.

## CapacityBroker

**Role**: Issues scoped Venice sub-keys, enforces tenant consumption policy, and surfaces misuse signals.

**Expected value**
- Converts unused DIEM budget into revenue by reselling capacity safely.
- Maintains per-tenant guardrails (`consumptionLimit`, `expiresAt`) to align with Venice policy.
- Supplies usage and violation telemetry for billing, throttling, and incident response.

**Core functionality**
- Calls `KeyManager.issue_scoped_key` to create per-tenant sub-keys with quotas and expirations.
- Fetches `VeniceClient.get_usage()` and `VeniceClient.get_rate_limits()` to audit consumption.
- Flags keys that are missing limits or expirations when `enforce_limits` is true.
- Emits structured summaries for orchestrator logging (`violations`, usage snapshots, errors).

**Configuration requirements**
- Venice API env: `VENICE_API_BASE_URL`, `VENICE_API_KEY`, optional overrides for create/revoke paths.
- Broker defaults: policy thresholds surfaced through `.env` for quotas and expirations.
- Optional parent key override via orchestrator `parent_key` argument (defaults to global env).

**Run surfaces**
- Combined loop: orchestrator calls `capacity_broker.run_once()` each cycle.
- CLI audit: `uv run python apps/cli/main.py broker:tenants:list`, `uv run python apps/cli/main.py venice:keys:cleanup --prefix <PREFIX> [--dry-run]`.

## Single-Loop Orchestrator

**Role**: Executes StakeMaster → ArbiDiem → CapacityBroker sequentially, coordinating shared context and telemetry.

**Expected value**
- Guarantees the minimal agent cadence runs in a predictable order without external coordination.
- Provides a single decision record per cycle (`stake`, `arbi`, `capacity`, correlation ID, signals) for observability and post-mortems.
- Centralizes live/dry-run toggles and quorum hooks so operators can safely graduate to on-chain execution.

**Core functionality**
- Calls `StakeMaster.run_once()` (respecting live flag) and captures claim/heartbeat outcomes.
- Derives utilization, volatility, mint rate, and inventory inputs before evaluating ArbiDiem.
- Executes ArbiDiem in simulate mode first; if signals approve and live mode is enabled, evaluates quorum (if configured) and runs live actions.
- Invokes `CapacityBroker.run_once()` for quota verification and aggregated usage reporting.
- Persists decision records via SQL `Decision` model when available; emits metrics and tracing spans.

**Configuration requirements**
- Orchestrator env controls: `AGENTS_PAUSED`, `AUTOSTART_ORCHESTRATOR_LIVE`, `RISK_ENABLE_PORTFOLIO_CAP`, inventory unit envs, optional quorum wiring.
- Market data backing (`services/marketdata.provider.MarketDataProvider`).
- Optional quorum object (`agents.quorum.core.Quorum`).

**Run surfaces**
- CLI: `uv run python apps/cli/main.py run:loop --sleep 15 --max-cycles 0` (add `--enable-live`).
- Automation: `make run-stack` (default dry-run) with `AUTOSTART_ORCHESTRATOR_LIVE=1` to enable on-chain actions.

## Quorum Coordinator (Staged)

**Role**: Aggregates votes from Yield, Arbitrage, Risk, Demand, and Treasury models to govern higher-risk actions.

**Expected value**
- Adds a policy layer above ArbiDiem and future agents to prevent unilateral risky decisions.
- Enables adaptive listen intervals (shorten during volatility, lengthen during calm conditions).

**Core functionality** *(targeted for post-v1 enhancement)*
- Collects weighted votes from model agents; defaults to hold when support is insufficient.
- Provides vetoes when `RiskModel` signals hazards.
- Exposes hooks that the single-loop orchestrator can call before executing live actions.

**Configuration requirements**
- Model registration (Yield/Arb/Risk/Demand/Treasury) with assigned weights.
- Threshold definitions per action type.
- Optional escalation hooks for human-in-the-loop overrides.

**Current status**
- `agents/quorum/core.py` implements foundational quorum voting logic; integration into the live loop is slated for Sprint 4 per `implementation-plan-agents.md`.

## AI Treasurer (Staged)

**Role**: Manages treasury allocations of VVV and DIEM to buffer demand and optimize surplus deployment.

**Expected value**
- Ensures the broker maintains ~1.5× daily DIEM coverage for tenant workloads.
- Automates conversions between treasury assets and DIEM rentals once guardrails are in place.

**Core functionality** *(targeted for post-v1 enhancement)*
- Calculates required delta between current holdings and target buffer (`agents/ai_treasurer/agent.py`).
- Plans buy/sell flows to top up or offload inventory.
- Coordinates with CapacityBroker for DIEM rental strategies.

**Configuration requirements**
- Treasury policy parameters (buffer days, min/max holdings).
- Market data access for VVV/DIEM pricing.
- Safeguards for human approval before executing large treasury moves.

**Current status**
- Analytics-only helper in v1; execution paths and guardrails will arrive with Sprint 4 per the implementation plan.

---

### Operational Notes

- All agents share common configuration via `.env` and `docs/AGENTS.md`; keep values synchronized across environments.
- `make run-stack` uses `scripts/start_stack.py` to launch the Broker API, single-loop orchestrator, and token watcher; enable legacy helper loops with the appropriate `AUTOSTART_*` flags when debugging.
- For dry-run validation, set `DIEM_FAKE_PRICE` and `DIEM_FAKE_MINT_RATE` so ArbiDiem decisions can be inspected without touching Venice or on-chain liquidity.
- Telemetry spans follow the `vvv.*` naming convention; enable LangSmith/OTel exports by setting tracing env variables (`LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`).
