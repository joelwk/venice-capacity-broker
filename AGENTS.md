# AGENTS.md

## Agents Catalog (v1.1)

This catalog defines the production Venice Capacity Broker agents, their contracts, environment, and run/test surfaces.

It supersedes prior drafts where details conflict, but **v1 scope and stop-lines remain as in `implementation-plan.md`**.

## Source of truth and scope

* Planning baseline and tie-breaker: `implementation-plan.md`.

* Tokenomics behavior and constraints: `Venice Tokenomics - Executive Summary.pdf`, `venice-diem-tokenomics.md`.

* Multi-agent architecture rationale and quorum design: `Autonomous Multi-Agent Architecture for VVV (Venice Token) on Base.pdf`.

## WRITING STYLE

* Each long sentence should be followed by **two newline characters**.

* Avoid long bullet lists.

* Use **plain, direct English** and keep sections short.

## Shared prerequisites

### Environment

* **Venice API**

  * `VENICE_API_BASE_URL=https://api.venice.ai/api/v1` **must** include `/api/v1`.

  * `VENICE_API_KEY` (parent key unless noted).

  * Optional path overrides when deployments differ:

    * `VENICE_VVV_CIRC_PATH=/vvv/circulatingsupply`

    * `VENICE_VVV_UTIL_PATH=/vvv/utilization`

    * `VENICE_VVV_YIELD_PATH=/vvv/staking_yield`

    * Key ops: `VENICE_CREATE_SUBKEY_PATH`, `VENICE_CREATE_ROOT_PATH`, `VENICE_CHALLENGE_PATH`, `VENICE_REVOKE_KEY_PATH`.

* **Base / on-chain**

  * `BASE_RPC_URL`, `BASE_CHAIN_ID`.

  * Contracts: `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`.

* **DEX config**

  * `DEX_PROVIDERS=uniswap_v2,aerodrome`.

  * Routers: `UNISWAP_V2_ROUTER_ADDRESS`, `AERODROME_ROUTER_ADDRESS`, `AERODROME_STABLE`.

  * Pricing: `QUOTE_TOKEN_ADDRESS`, optional `TRADE_PATH` for DIEM pricing`.

* **Debug instrumentation**

  * `DIEM_DEBUG_ROUTES=1`.

    Shows normalized trade routes and DEX aggregator diagnostics in `runtime.log`.

    Disable after troubleshooting.

  * `MARKETDATA_DEBUG_SANITY=1`.

    Emits price sanity clamp context with symbol, internal price, and external reference.

    Leave off in production unless chasing a data drift incident.



### DIEM & Risk configuration

* **Mint/burn gate**

  * `DIEM_ENABLE_SVVV_GATE` - require sVVV capacity pre-check before mint.

  * `DIEM_MINT_RATE_SVVV_PER_DIEM` (base units per DIEM), `DIEM_MINT_RATE` (tokens per DIEM), `DIEM_SVVV_AVAILABLE_UNITS` override, `DIEM_DECIMALS`, `SVVV_DECIMALS`.

* **DIEM staking helpers**

  * `DIEM_STAKING_ADDRESS` (defaults to token), `DIEM_STAKING_ABI=diem.json`, `DIEM_STAKE_FN=stake`.

  * `DIEM_LOCK_ON_MINT`, `DIEM_UNLOCK_AFTER_BURN`, `DIEM_UNLOCK_COOLDOWN_SECONDS`.

* **Risk sizing**

  * `RISK_UTIL_ALPHA` multiplier = `1 + alpha * utilization`.

  * `RISK_MAX_VOLATILITY_BPS` to cap units when realized vol is high.

### StakeMaster heartbeat

* `STAKEMASTER_HEARTBEAT_INTERVAL_HOURS` (default 48).

* `STAKEMASTER_HEARTBEAT_DISABLE` to disable.

* `STAKEMASTER_HEARTBEAT_PROMPT`, `VENICE_HEARTBEAT_MODEL`.

* `VVV_ACTIVE_MIN_STAKE_UNITS`, `VVV_COOLDOWN_SECONDS` as informational defaults.

### Libraries and services

* Venice SDK client - `libs/venice_sdk/client.py`.

* Key manager - `services/venice_keys/manager.py`.

* Market data - `services/marketdata/provider.py`.

* DEX aggregator - `libs/dex/providers.py`.

* CLI entrypoint - `apps/cli/main.py`.

## Venice tokenomics you must assume (for code and tests)

* **Staking VVV yields a daily Diem allocation plus VVV emissions**.

  Daily Diem is your share of total staked capacity, and emissions APY is paid in VVV.

* **DIEM is tokenized inference capacity - $1/day of API credit when staked**.

  Only VVV stakers can mint DIEM by **locking sVVV**, and while locked they continue to earn **80%** of normal emissions.

  The mint rate rises with DIEM supply and targets a tight float (~38k DIEM).

  Burn DIEM to unlock the sVVV.

* **Venice allows reselling capacity via scoped API keys** and explicitly supports third-party consumption with quotas.

  Our Broker must attach `consumptionLimit` and `expiresAt` to every sub-key and enforce revocation on abuse.

* **Reference posts**: Venice blog posts on VVV and DIEM restate $1/day semantics, VVV-gated minting, and agent-first API design. ([Venice AI][1])

## Agents overview (production v1)

We run a **single-loop orchestrator** in v1 for simplicity.

StakeMaster, ArbiDiem, and CapacityBroker execute sequentially with shared state, while a quorum coordinator and Treasurer are staged for post-v1 upgrades.

> **Design note**
> The manager-and-tools pattern (single agent with tools) is the simplest start.
> Handoffs and multi-agent graphs are introduced later when specialization or tool-overload requires splitting. ([OpenAI Platform][2])

### 1) StakeMaster

**Purpose** - Keep VVV staked, harvest and restake emissions, and maintain the "active staker" status via a light Venice heartbeat call.

Schedule unlocks respecting contract cooldown and stagger exits to avoid lumped liquidity risk.

**Inputs** - VVV stake state, yield metrics, Diem usage, cooldown timers, utilization.

**Decisions** -

* Claim and restake when rewards exceed gas and risk thresholds.

* Stake idle VVV when APY and utilization are favorable.

* Unstake partially on stop-loss or policy triggers from Risk service.

**Run**

```
uv run python apps/cli/main.py run:stakemaster --enable-live
```

**Notes** - Send emissions/cooldown telemetry and perform configurable heartbeat using `STAKEMASTER_HEARTBEAT_*` envs.

### 2) ArbiDiem

**Purpose** - Risk-gated DIEM mint/sell and buy/burn workflows.

Exploit deviations between DIEM market price and its $1/day fair-value proxy while observing mint-rate curve and sVVV opportunity cost.

**Sub-roles** - Watcher (events), Analyst (fair value + mint curve), Decider (risk & demand signals), Executor (mint/burn/swap with slippage guards).

**Inputs** - DIEM price path (`TRADE_PATH`), quotes and pool reserves, mint rate, utilization, volatility.

**Run**

```
uv run python apps/cli/main.py run:quorum --dry-run
uv run python apps/cli/main.py diem:mint <amountBaseUnits> [--dry-run]
uv run python apps/cli/main.py diem:burn <amountBaseUnits> [--dry-run]
```

**Guards** - Slippage caps, pool-take caps, and reserved buyback budget limit short DIEM exposure when selling minted supply.

Lock/unlock hooks are configurable via DIEM_* envs.

### 3) CapacityBroker

**Purpose** - Issue scoped Venice sub-keys, meter usage, and resell unused Diem capacity via a multi-tenant HTTP API.

All sub-keys **must** include `consumptionLimit` and `expiresAt`.

Abuse triggers immediate revocation and rotation.

**Endpoints (broker-side)**

* `GET /v1/me`, `GET /v1/me/usage`, `GET /v1/me/broker-limits`, `POST /v1/me/broker-limits` for tenant self-service within tighter bounds.

**Venice key ops** - Use parent key to create scoped sub-keys and revoke on abuse; replay buyer verifications safely.

See `venice.swagger.yaml` for OpenAI-compatible model endpoints and error semantics we must proxy faithfully.

**Run**

```
uv run python apps/cli/main.py broker:tenants:list
uv run python apps/cli/main.py venice:keys:cleanup --prefix T1 --dry-run
```

**Pricing posture** - If midday utilization is high and Diem budget tight, throttle low-tier tenants, raise price, or pause new intake.

This is the **inventory failsafe** documented in the implementation plan.

### 4) Quorum coordinator *(post-v1 wiring, optional in v1 loop)*

Aggregate votes from YieldModel, ArbModel, RiskModel, and DemandModel.

Weight signals and act only when a confidence threshold is cleared; otherwise hold.

Shorten listen interval during high volatility or strong signals.

### 5) AI Treasurer *(placeholder for later phase)*

Hold VVV/DIEM to guarantee compute for apps, keep ~1.5x average daily Diem as buffer, and reallocate surplus into rentals or DIEM sales.

Trigger purchases of VVV/DIEM on demand spikes and sell or rent excess when slack persists.

## Orchestrator loop (v1)

The **single-loop orchestrator** initializes wallet, staking, keys, market-data, then runs: StakeMaster -> ArbiDiem -> CapacityBroker.

Design the loop so a quorum coordinator can drop in later with minimal changes.

`agents/reflex/guardian.py` evaluates each cycle before ArbiDiem runs in live mode and halts execution when price drawdowns, volatility spikes, or inactive staking heartbeats show up.

> Reference patterns: OpenAI Agents SDK treats agents as models with instructions, tools, guardrails, and **handoffs**.
> Start simple with one agent and tools, then introduce handoffs or multi-agent graphs only when specialization is required. ([OpenAI GitHub][3])

> If you later split into multiple agents, follow graph handoffs as described in LangGraph, using controlled routing and explicit exit conditions. ([LangChain AI][4])

## Memory, reflexion, and logs

* `services/memory/store.py` logs every decision, input signal, and outcome to `db/agent_memory.jsonl`.

* `services/memory/reflection.py` runs the reflection pass after material actions and keeps a configurable hold streak critique.

* `agents/reflex/guardian.py` halts live trading on anomalies; persist PnL, Diem used vs. wasted, and tenant utilization for long-term recall.

* Configure via `AGENT_MEMORY_PATH`, `REFLECTION_VOL_BPS_THRESHOLD`, `REFLECTION_HOLD_STREAK`, and the `REFLEX_*` guardrail envs; use `DIEM_FAKE_PRICE` / `DIEM_FAKE_MINT_RATE` for offline simulations.

## Operational policy and risk

* **Sizing** - Respect pool-take caps (e.g., `RISK_MAX_POOL_TAKE_BPS`) and slippage caps (default 150 bps).

* **Cooldown scheduling** - Stagger unlocks; never expose all stake at once.

* **Broker** - Enforce per-tenant quotas, attach expiries, and revoke on anomaly.

* **Market risk** - Reserve buyback budget when short DIEM via sales of minted supply.

* **Observability** - Emit OpenTelemetry spans, on-chain tx logs, request metrics at `/metrics`.

## Venice API usage (what we must support/proxy)

* **Models / chat completions** - `POST /chat/completions` and related model listings.

* **Signals & metrics** - `/vvv/circulatingsupply`, `/vvv/utilization`, `/vvv/staking_yield`.

* **Keys** - `POST /api_keys` (scoped sub-keys) and Web3 root key flow when required.

  Match Venice response semantics and error payloads in the Broker proxy.

> OpenAI Agents SDK and API reference are good baselines for tool/guardrail semantics and error handling style when exposing compatible surfaces. ([OpenAI Platform][5])

## Inputs and outputs

**Inputs**

* On-chain state and DEX quotes.

* Venice signals and rate-limit usage where relevant.

* Config from env and Broker tenant limits.

**Outputs**

* Trades (dry-run by default), staking claims in live mode, sub-key issuance, telemetry metrics, decision records.

## Run & test surfaces

**Selected tests**

* DEX paths and slippage: `tests/test_dex_exact_out.py`, `tests/test_dex_fot_fallback.py`, `tests/test_dex_exact_out_venues.py`.

* DIEM service paths: `tests/test_diem_service.py`, `tests/test_diem_buy_path.py`, `tests/test_diem_mint_burn_dryrun.py`.

* Risk policy sizing: `tests/test_risk_policy.py`, `tests/test_arbi_diem_risk_integration.py`.

* Broker limits & idempotency: `tests/test_broker_limits.py`, `tests/test_cli_idempotency_purge.py`.

* Market-data normalization: `tests/test_marketdata_prices.py`.

**Orchestrator**

```
uv run python apps/cli/main.py run:loop --enable-live --sleep 15 --max-cycles 3
uv run pytest -q
```

**Startup probes**

```
uv run python apps/cli/main.py startup:probe
uv run python apps/cli/main.py quotes:preview --units 1.0
uv run python apps/cli/main.py market:best-price:scan --start 1.0 --min 1e-12 --factor 10
```

> Default DIEM buy path on Base is multi-hop: `DIEM -> WETH -> USDC`.
> Aerodrome exact-out remains disabled by design; use UniswapV2 for exact-out buys.

## Security & guardrails

* **Wallets** - Prefer smart-wallet or MPC custody; keep a dev EOA only for local tests.

* **Key hygiene** - Parent keys locked down; rotate sub-keys daily; revoke on anomaly.

* **Broker policy** - Always require `consumptionLimit` and `expiresAt`, and store issuance audit trails.

* **Agent guardrails** - Add relevance, safety, and tool-risk checks around high-risk actions; escalate to human review when thresholds trip.

  Manager-and-handoff patterns from OpenAI Agents SDK map cleanly to this style of guardrails. ([OpenAI Platform][2])

## Design contracts for OpenAI-style agents and tools

These contracts guide code-gen and tool wiring.

We begin with **prompted, simple agents** and expand later.

**Agent skeleton**

* **Instructions** - One paragraph, role and objectives, hard constraints.

* **Tools** - Small action space with explicit names and JSON schemas.

* **Exit** - Success criteria or `max_turns`.

* **Handoffs (later)** - Only when specialization forces it. ([OpenAI GitHub][6])

**Example tool stubs**

* `stake_vvv(units_wei)` -> on-chain tx or dry-run preview.

* `claim_and_compound()` -> harvest then restake.

* `get_diem_mint_rate()` -> live or configured mint rate.

* `mint_diem(units_wei, lock=True)` / `burn_diem(units_wei)` -> return tx hash and new sVVV status.

* `quote_swap_exact_in(path, amount_in_wei, slippage_bps)` / `quote_swap_exact_out(path, amount_out_wei, slippage_bps)`.

* `issue_scoped_key(consumption_limit, expires_at, label)` -> Venice sub-key.

## Known limits and follow-ups

* v1 uses a **single orchestrator loop**.

  The multi-agent quorum will ship post-v1 without breaking current contracts.

* Capacity-aware **dynamic pricing and DIEM rentals** remain post-v1.

  We only issue and meter sub-keys in v1.

* Exact-out swaps on Aerodrome remain disabled; revisit once ABI/routers support reliable previews.

## Appendix - Venice docs you'll proxy

When the Broker front-ends Venice endpoints, keep payloads and error semantics compatible.

`venice.swagger.yaml` is the canonical reference for response shapes and error codes used by our proxy.

## References (for developers)

* **OpenAI Agents SDK** - agents, tools, guardrails, handoffs. ([OpenAI GitHub][7])

* **LangGraph** - multi-agent handoffs, routing, and "manager vs. decentralized" patterns if we outgrow the single loop. ([LangChain AI][4])

* **Venice** - VVV and DIEM mechanics and $1/day semantics. ([Venice AI][1])

* **Architecture & tokenomics (internal)** - full multi-agent spec and revenue playbooks.

---

### Why this version

* Aligns the **most simple agents first** directive while leaving hooks for handoffs/quorum.

* Encodes **high-signal tokenomics** into defaults and tests so Codex-style code-gen won't drift.

* Keeps **broker guardrails** enforceable in one place, with Venice compatibility called out explicitly.

* Uses **primary sources** for SDK patterns and Venice semantics, with internal files for exact requirements. ([OpenAI Platform][2])

---

**End of AGENTS.md**

[1]: https://venice.ai/blog/introducing-the-venice-token-vvv?utm_source=chatgpt.com "Introducing the Venice token: VVV"
[2]: https://platform.openai.com/docs/guides/agents-sdk?utm_source=chatgpt.com "Agents SDK Guide"
[3]: https://openai.github.io/openai-agents-python/ref/agent/?utm_source=chatgpt.com "OpenAI Agents SDK"
[4]: https://langchain-ai.github.io/langgraph/concepts/multi_agent/?utm_source=chatgpt.com "LangGraph Multi-Agent Systems - Overview"
[5]: https://platform.openai.com/docs/api-reference/introduction?utm_source=chatgpt.com "API Reference - OpenAI API"
[6]: https://openai.github.io/openai-agents-python/agents/?utm_source=chatgpt.com "Agents - OpenAI Agents SDK"
[7]: https://openai.github.io/openai-agents-python/?utm_source=chatgpt.com "OpenAI Agents SDK"
