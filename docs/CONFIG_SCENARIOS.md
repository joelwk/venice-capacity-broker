# Configuration Scenarios

Below are ready-to-use environment bundles with when/why to use them, the key flags to set, and operational notes.

## Scenario 1: Progressive live orchestrator (default safeguards)

**When to use**: Normal operations with StakeMaster heartbeats, progressive live warm-up, and reflex guardrails intact.

**Env block**
```
STAKEMASTER_HEARTBEAT_INTERVAL_HOURS=48
STAKEMASTER_HEARTBEAT_DISABLE=false
STAKEMASTER_HEARTBEAT_PROMPT="Please respond with a single word 'alive'."
VVV_ACTIVE_MIN_STAKE_UNITS=10000000000000000000
VVV_COOLDOWN_SECONDS=604800
AGENTS_PAUSED=false
AUTOSTART_STAKEMASTER=false
AUTOSTART_ORCHESTRATOR_LIVE=true
AUTOSTART_ORCHESTRATOR_INTERVAL=15
AUTOSTART_SHUTDOWN_TIMEOUT=30
ORCHESTRATOR_STAKE_LIVE=true
STAKEMASTER_PROGRESSIVE_ENABLE=true
STAKEMASTER_PROGRESSIVE_CYCLES=5
STAKEMASTER_AUTO_STAKE_MAX_ATTEMPTS=3
AGENT_MEMORY_PATH=db/agent_memory.jsonl
REFLECTION_VOL_BPS_THRESHOLD=600
REFLECTION_HOLD_STREAK=3
REFLEX_MAX_VOL_BPS=450
REFLEX_MAX_UTILIZATION=0.92
REFLEX_MAX_PRICE_DRAWDOWN=0.12
REFLEX_APPLY_DRY_RUN=false
REFLEX_REQUIRE_ACTIVE_STAKE=true
REFLEX_ALLOW_INACTIVE_STAKE=1
```

**Notes**
- Progressive live kicks in after 5 healthy cycles; reflex guard can still halt ArbiDiem on high vol/drawdown.
- Heartbeats are on; cooldown and min active stake are enforced.
- Memory and reflection are persisted to `db/agent_memory.jsonl`.
- **Log verification**: Check the first `single-loop cycle` log entry to confirm `reflex.limits` matches your `REFLEX_MAX_VOL_BPS`, `REFLEX_MAX_UTILIZATION`, and `REFLEX_MAX_PRICE_DRAWDOWN` env values. ReflexGuardian logs effective configuration at startup with prefix "ReflexGuardian initialized:".

## Scenario 2: Quorum / reflex relaxed for testing

**When to use**: Lab or staging runs where you need quorum decisions despite noisy markets; disables most reflex halts.

**Env block**
```
QUORUM_ENABLE=1
REFLEX_MAX_VOL_BPS=100000
REFLEX_MAX_PRICE_DRAWDOWN=1.0
REFLEX_MAX_UTILIZATION=0.99
REFLEX_REQUIRE_ACTIVE_STAKE=0
REFLEX_STAKE_INACTIVE_CONSEC=10
REFLECTION_VOL_BPS_THRESHOLD=100000
```

**Notes**
- Keeps quorum on but widens reflex thresholds so decisions are evaluated instead of blocked.
- Use only in non-production; re-tighten limits before live trading.
- **Quorum observability**: Every `single-loop cycle` log includes an `arbi.quorum` block with `status` field. Status values: `approved`/`blocked` (quorum voted), `skipped` (reflex/price guard), `not_invoked` (no trade signal), `disabled` (QUORUM_ENABLE=0), or `error`. Use `vvv-agents orchestrator:cycles` or `vvv-agents quorum:inspect` to view quorum decisions.

## Scenario 3: Dry-run smoke (no live trades)

**When to use**: CI or local smoke where you want the full loop minus live execution and with safe defaults.

**Env block**
```
AUTOSTART_ORCHESTRATOR_LIVE=false
STAKEMASTER_PROGRESSIVE_ENABLE=false
REFLEX_APPLY_DRY_RUN=false
QUORUM_ENABLE=1
AGENTS_PAUSED=false
LOG_LEVEL=INFO
```

**Notes**
- Leaves reflex and quorum enabled for visibility, but `--enable-live` is off so swaps/mints are simulated only.
- Good for validating config, price sanity, and ArbiDiem rationales without on-chain risk.
- **Troubleshooting**: If `arbi.quorum.status` is `not_invoked`, ArbiDiem did not propose a trade (common reasons: `no_exact_out_preview`, fair value below thresholds). If `skipped` with `reason: reflex_guard`, check `reflex.reasons` and `reflex.limits` in the same cycle log.

## Scenario 4: Broker-only API (agents off)

**When to use**: Serving broker API only (e.g., front-end demo) without agents, staking, or quorum traffic.

**Env block**
```
AUTOSTART_ORCHESTRATOR=0
AUTOSTART_STAKEMASTER=0
AUTOSTART_TOKEN_WATCHER=0
AUTOSTART_BROKER_API=1
QUOTES_ENABLED=true
PURCHASES_ENABLED=true
LOG_LEVEL=INFO
```

**Notes**
- Orchestrator and StakeMaster are disabled; only the broker API starts.
- Ideal for lightweight demos or when agent credentials are unavailable.

## Log Field Reference: Reflex and Quorum

### Reflex Guardian Fields

Every `single-loop cycle` log includes a `reflex` block with:

- `halt`: Boolean indicating if reflex guardian blocked execution
- `reasons`: List of halt reasons (e.g., `volatility_exceeded`, `price_drawdown`, `stake_inactive`)
- `warnings`: List of non-blocking warnings (e.g., `utilization_hot`)
- `observed`: Current values (`price`, `utilization`, `vol_bps`, `stake_status`, `stake_inactive_consecutive`)
- `limits`: Effective guardrail thresholds:
  - `max_vol_bps`: From `REFLEX_MAX_VOL_BPS` (default: 450.0)
  - `max_utilization`: From `REFLEX_MAX_UTILIZATION` (default: 0.92)
  - `max_drawdown`: From `REFLEX_MAX_PRICE_DRAWDOWN` (default: 0.12)

**Quick sanity check**: Compare `reflex.limits` in the first cycle log against your `REFLEX_*` env vars. ReflexGuardian logs effective configuration at startup: look for "ReflexGuardian initialized:" in logs.

### Quorum Status Fields

Every `single-loop cycle` log includes an `arbi.quorum` block with:

- `status`: One of:
  - `approved`: Quorum voted to allow trade (requires `signal_decision=True` and `live_mode=True`)
  - `blocked`: Quorum voted to block trade
  - `skipped`: Quorum not invoked due to `reflex_guard`, `price_guard`, or `agents_paused`
  - `not_invoked`: ArbiDiem did not propose a trade (`signal_decision=False`)
  - `disabled`: `QUORUM_ENABLE=0` or quorum not configured
  - `error`: Exception during quorum decision
- `reason`: Explanation for skipped/not_invoked/disabled status
- When `status` is `approved` or `blocked`:
  - `ratio`: Weighted vote ratio (0.0-1.0)
  - `threshold`: Quorum threshold (default: 0.55)
  - `approvedWeight` / `totalWeight`: Vote weight breakdown
  - `confidence`: Maximum confidence across models
  - `breakdown`: Per-model votes with `name`, `approve`, `weight`, `confidence`, `reason`

**Troubleshooting**:
- Missing `quorum` block: Should not occur after these updates; if present, indicates unexpected state.
- `status: not_invoked`: Normal when ArbiDiem finds no profitable trade or DEX previews fail. Check `arbi.why.reason` for details.
- `status: skipped` with `reason: reflex_guard`: Reflex guardian halted before quorum could vote. Check `reflex.reasons` and `reflex.limits`.
- `status: skipped` with `reason: price_guard`: Price guard detected anomaly. Check `arbi.priceGuard` for details.

**CLI inspection**:
- `vvv-agents orchestrator:cycles --limit 5`: Shows recent cycles with quorum status
- `vvv-agents quorum:inspect`: Shows latest quorum decision with model breakdown
