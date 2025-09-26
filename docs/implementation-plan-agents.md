## Implementation Guidance for Multi‑Agent Capacity Broker

This document translates the high‑level ideas from the technical design and implementation plan into concrete steps for engineers building the multi‑agent Capacity Broker.  
It is an opinionated checklist you can follow sequentially to assemble the core services, agents, and orchestration.  
Where the original plan is ambiguous, this file resolves details or calls out assumptions.  
All design choices remain faithful to the architecture described in the official implementation plan and tokenomics research.

---

## Objectives and core revenue paths

Venice’s Capacity Broker is a multi‑agent system that maximises revenue from staked VVV by exploiting four pathways:

- **Staking yield**: stake VVV to earn emission rewards and daily Diem credits; maintain an “active staker” status via periodic heartbeats.
- **Diem arbitrage**: mint DIEM by locking sVVV and sell when market price exceeds the mint cost.
- **Capacity reselling**: issue scoped API keys and resell unused Diem to tenants via a broker service.
- **Treasury optimisation (future)**: manage VVV/DIEM holdings to support an app’s or DAO’s compute needs.

The system uses LangGraph for orchestration and Coinbase AgentKit for on‑chain actions.  
A quorum layer synthesises signals from several models (Yield, Arbitrage, Risk, Demand and Treasury) to decide which path to execute.  
Agents must respect protocol constraints such as the seven‑day unstake cooldown and maintain safety through stop‑losses and capacity limits.

---

## Pre‑requisites and environment

- **Toolchain**: Python 3.10+.
- **Repo**: clone the repository and review the monorepo layout described in the implementation plan.
- **Secrets & config**:
  - `BASE_RPC_URL`, contract addresses for VVV, staking, and DIEM.
  - Router addresses for Uniswap and Aerodrome.
  - `VENICE_API_BASE_URL` and parent inference key.
  - Store secrets (private keys, API keys) in a secure KMS. Dev may use a local ETH account; prod should use a CDP Smart Wallet.
- **Wallet providers**: implement `services/wallet/` using AgentKit primitives. Provide both Smart Wallet (MPC) and ETH account providers; expose message and transaction signing.
- **Environment**: define variables (mirroring `AGENTS.md`) for Venice API, DIEM mint rates, risk thresholds, staking heartbeat interval, and DEX addresses.

---

## Services and modules

### Staking service (`services/staking`)

Implement a class that wraps the VVV staking contract and emits telemetry:

- `approve_vvv(amount)` — allow the staking contract to spend VVV.
- `stake(amount)` — stake VVV into the staking contract to obtain sVVV.
- `unstake(amount)` — initiate an unstake request; tokens unlock after seven days.
- `claim_emissions()` — claim VVV emissions and optionally restake them.
- `is_active_staker()` — check whether the address has made a heartbeat call within the required interval.

Handle cooldown logic by storing timestamps and preventing duplicate unlock attempts.  
Emit events for stakes, rewards, and cooldown windows.

### DIEM service (`services/diem`)

Manage DIEM flows and market execution:

- `mint_diem(svvv_amount)` — lock sVVV and mint DIEM tokens.
- `burn_diem(diem_amount)` — burn DIEM to unlock sVVV.
- `calc_mint_rate()` — fetch the current mint rate (sVVV per DIEM) from Venice signals or a configured fallback.
- `stake_diem_for_api()` — stake DIEM into the inference system to realise $1/day of compute.
- `trade(side, amount, slippage_bps)` — buy or sell DIEM on the best of Aerodrome or Uniswap using dual‑provider quotes with slippage caps.

Poll token prices and pool reserves via the market‑data module.  
Support exact‑in and exact‑out trades where available.  
Provide read‑only helpers for circulating supply and mint rate.

### Market data (`services/marketdata`)

Provide live inputs and short‑cache them:

- Live token prices (VVV, DIEM) using DEX quotes.
- Circulating supply and mint‑rate statistics from Venice APIs.
- Utilization signals and large on‑chain mints/burns.

Cache with short TTLs and emit telemetry for price volatility and utilisation.  
This module feeds both models and agents.

### Venice keys (`services/venice_keys`)

Wrap the autonomous API key flow:

1. GET `/api_keys/generate_web3_key` to obtain a challenge.
2. Sign the challenge using the staking wallet (via the wallet service). Smart wallets may require MPC.
3. POST `/api_keys/generate_web3_key` with signature, address, and optional `consumptionLimit` and `expiresAt` to create a root inference key or a scoped sub‑key.

Helper methods:

- `issue_root_key()` — returns a new parent inference key tied to the staking wallet.
- `issue_scoped_key(parent_key, label, quota, expires)` — create a sub‑key for a tenant with a daily Diem quota.
- `revoke_key(key_id)` — revoke a sub‑key immediately.

### Risk service (`services/risk`)

Position sizing and guardrails:

- Monitor VVV and DIEM volatility; cap trade sizes (e.g. `RISK_MAX_SLIPPAGE_BPS`, `RISK_MAX_POOL_TAKE_BPS`).
- Enforce a buyback budget for DIEM short exposure.
- Evaluate utilisation stress; warn if Diem quotas exceed safe thresholds.

### Telemetry (`libs/telemetry`)

Tracing and metrics:

- Implement tracing with OpenTelemetry or LangChain tracing.
- Emit events for every service call and agent decision with context.
- Persist per‑stream PnL, wasted Diem, and tenant utilisation for analysis.


### Debugging instrumentation

Enable `DIEM_DEBUG_ROUTES=1` when tracing routing or DEX liquidity issues so the runtime log records normalized trade paths and provider responses.


Enable `MARKETDATA_DEBUG_SANITY=1` when investigating price drift so clamps include symbol, internal price, and external reference.


Use `python -m pytest tests/test_logging_debug.py -q` after touching instrumentation to ensure the log contracts stay intact.



---

## Agents and their responsibilities

### StakeMaster

Maintain an active stake and maximise emission yield while respecting the seven‑day cooldown:

- Periodically call `staking.status()` and `staking.claim_emissions()` to compound rewards.
- Stake idle VVV when APR is attractive and DIEM demand is steady.
- Trigger a heartbeat: perform a small Venice inference request at a configurable interval to remain “active” and earn maximum Diem.
- Schedule unstake actions with respect to cooldown; stagger positions to avoid unlocking everything at once.

### ArbiDiem

Exploit price discrepancies between DIEM’s intrinsic value and market price:

- Observe DIEM and VVV prices and compute DIEM fair value (NPV of $1/day perpetuity).
- Mint and sell when `market_price × mint_rate – VVV_price` exceeds a configured threshold, accounting for the 80% emissions that locked sVVV still earns.
- Execute mint and sell via the DIEM service; split orders across venues and cap slippage.
- Buy back and burn DIEM when it trades below fair value or when sVVV needs to be unlocked.
- Watch for large on‑chain mints/burns and adjust strategy.

### CapacityBroker

Resell unused Diem capacity by issuing scoped API keys and adjusting supply dynamically:

- Operate a FastAPI‑based Broker API that authenticates tenants via sub‑keys and proxies chat requests to the Venice API.
- Use the keys service to issue sub‑keys with daily quotas and expirations; enforce rate limiting and revoke keys on abuse.
- Monitor total Diem consumption; raise prices or throttle low‑tier tenants if utilisation exceeds a high watermark (e.g., 85%).
- Offer DIEM rentals or suggest that enterprise customers stake DIEM directly when capacity is scarce.
- Maintain simple dynamic pricing: discount when utilisation is low; surge when high.

### AI Treasurer (future)

Manage VVV/DIEM holdings at the treasury level to support application or DAO compute requirements:

- Hold roughly 150% of average daily Diem usage in reserve.
- Buy VVV or DIEM when demand spikes; redeploy surplus by renting out capacity or selling DIEM.
- Integrate with app billing so a portion of payments funds VVV staking.

---

## Quorum coordinator

Aggregate signals from five models and coordinate execution:

- **YieldModel** — staking/unstaking based on APR, emissions decay, and utilisation.
- **ArbModel** — mint‑sell, hold, or buy‑back based on price premiums.
- **RiskModel** — monitors volatility and unlock risk; can veto aggressive actions.
- **DemandModel** — forecasts API usage; adjusts DIEM allocation and pricing.
- **TreasuryModel** — (future) enforces long‑term treasury policies.

Combine votes with configurable weights; default to “hold” if support is insufficient.  
Implement dynamic listen intervals: decrease cycle sleep when volatility or premium is high; increase it when signals are weak.  
Preempt lower‑priority workflows when `RiskModel` flags a hazard.

---

## Memory and reflection

`services/memory/store.py` now provides an append-only `MemoryStore` that records each orchestrator cycle to `db/agent_memory.jsonl` while keeping a short in-process buffer.  
`services/memory/reflection.py` ships `ReflectionEngine`, which reviews the latest cycle plus a configurable lookback window and emits critiques that feed back into agent prompts.  
Tune behaviour with env: `AGENT_MEMORY_PATH`, `REFLECTION_VOL_BPS_THRESHOLD`, `REFLECTION_HOLD_STREAK`, and the `REFLEX_*` guardrail thresholds before promoting the quorum flow to multi-agent execution.  
`agents/reflex/guardian.py` implements a `ReflexGuardian` that halts live execution on drawdowns, volatility spikes, or inactive staking heartbeats; add new anomaly heuristics here as they emerge.


---

## Implementation timeline

Follow the sprint plan outlined in the implementation document:

- **Sprint 1 – Foundations**: wallet and staking services, Venice autonomous key flow, StakeMaster with heartbeat and compounding.
- **Sprint 2 – Broker & quotas**: Broker API and keys service, per‑tenant quotas, basic usage metrics.
- **Sprint 3 – DIEM mint/trade**: DIEM service and ArbiDiem crew, including mint, sell, buy‑back, and fair‑value calculations.
- **Sprint 4 – Quorum & treasury**: quorum coordinator and initial AI Treasurer actions.
- **Sprint 5 – Hardening & scale**: abuse prevention, SLAs, autoscaling, and advanced risk management (e.g., price hedging).

