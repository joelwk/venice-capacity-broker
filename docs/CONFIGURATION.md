# Configuration

This is the single source of truth for environment variables and run-mode flags.

`core/config.py` loads and validates these values at process start and raises a single `ConfigError` when required keys are missing.  

DEX providers, trade paths, and debug flags no longer carry in-code defaults outside this loader.  

## Venice API

- `VENICE_API_BASE_URL` — must be `https://api.venice.ai/api/v1` (must include `/api/v1`).
- `VENICE_API_KEY` / `VENICE_PARENT_KEY` — parent key for sub-key creation.
- Optional path overrides: `VENICE_VVV_CIRC_PATH`, `VENICE_VVV_UTIL_PATH`, `VENICE_VVV_YIELD_PATH`, `VENICE_CREATE_SUBKEY_PATH`, `VENICE_CREATE_ROOT_PATH`, `VENICE_CHALLENGE_PATH`, `VENICE_REVOKE_KEY_PATH`.
- Utilization signal: fetched from `VENICE_VVV_UTIL_PATH` and used by Risk and Reflex.  
  
  Track C stub: set `MARKETDATA_UTILIZATION_HINT` (0.0-1.0) to supply utilization when Venice is unavailable.

## Base / On-chain

- `BASE_RPC_URL`, `BASE_RPC_URLS` (comma-separated list), `BASE_CHAIN_ID`.
- **RPC URL Configuration (CRITICAL for production):**
  - **Precedence**: `BASE_RPC_URLS` (plural) takes precedence over `BASE_RPC_URL` (singular)
  - **Production requirement**: Use a paid RPC endpoint (Alchemy, Infura, QuickNode, etc.) to avoid rate limiting and JSON-RPC failures
  - **Docker configuration**: Set `BASE_RPC_URLS` in `docker/.env.local` (see `docker/.env.local.example` for template)
  - **Startup validation**: The system validates RPC configuration at startup and will:
    - Log a warning if only public RPCs are detected
    - Fail fast (raise error) if public RPCs are detected in production (non-dry-run) mode
    - Allow public RPCs in dry-run mode for testing
  - **Example**: `BASE_RPC_URLS=https://base-mainnet.g.alchemy.com/v2/YOUR_API_KEY`
  - **Known public endpoints** (should not be used in production): `base.drpc.org`, `mainnet.base.org`, `base-rpc.publicnode.com`, etc.
  - **Override validation**: Set `RPC_VALIDATION_STRICT=0` to disable strict validation (not recommended)
- Token/contract addresses: `VVV_TOKEN_ADDRESS`, `VVV_STAKING_ADDRESS`, `DIEM_TOKEN_ADDRESS`.
- **DIEM/VVV bridge addresses (required for bridge pricing and DIEM trade routes):**
  - `DIEM_VVV_PAIR_ADDRESS` - DIEM/VVV pair address for bridge pricing. Base mainnet: `0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d`
  - `VVV_USDC_POOL_ADDRESS` - VVV/USDC pool address for bridge pricing. Base mainnet: `0x67A11022B7B6ed66f81233F6C8Ed6e48F7826530`
  - `VVV_USDC_POOL_FEE` - Uniswap V3 fee tier for VVV/USDC pool (default: `3000` = 0.3%). Required for V3 pool discovery.
  - `QUOTE_TOKEN_ADDRESS` - Quote token address (USDC) for pricing. Base mainnet: `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`
  - These are validated at Broker API startup; missing values will log errors and may cause bridge pricing failures.
- RPC health tracking (optional):
  - `RPC_RATE_LIMIT_COOLDOWN_SECONDS` (default: 60) - Cooldown period after 429 rate limit errors.
  - `RPC_BACKOFF_BASE_SECONDS` (default: 60) - Base backoff time for failed endpoints.
  - `RPC_BACKOFF_MAX_SECONDS` (default: 600) - Maximum backoff time.
  - `RPC_BACKOFF_MULTIPLIER` (default: 2.0) - Exponential backoff multiplier.

## DEX Configuration

- `DEX_PROVIDERS` (e.g., `uniswap_v2,aerodrome,uniswap_v3`). Parsed and validated once by `core/config.py`; order controls default discovery/execution.
- **Execution venue constraints:**
  - `DEX_DISCOVERY_PROVIDERS` — comma-separated list of providers used for price discovery/quoting (defaults to `DEX_PROVIDERS` order when unset).
  - `DEX_EXEC_PROVIDERS` — comma-separated list of providers allowed for trade execution (defaults to discovery list when unset).
    - Include `aerodrome_cl` when you want the direct DIEM/USDC SlipStream route to be executable.
  - Example: `DEX_DISCOVERY_PROVIDERS=uniswap_v3,aerodrome` and `DEX_EXEC_PROVIDERS=uniswap_v3` makes Aerodrome discovery-only.
- **Router addresses are REQUIRED** for each provider in `DEX_PROVIDERS`. Without these, providers are silently skipped and quotes fail:
  - `UNISWAP_V2_ROUTER_ADDRESS` — Base mainnet: `0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24`
  - `UNISWAP_V3_ROUTER_ADDRESS` — Base mainnet: `0x2626664c2603336E57B271c5C0b26F421741e481`
  - `UNISWAP_V3_QUOTER_ADDRESS` — Base mainnet: `0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a` (required for V3 quotes)
  - `AERODROME_ROUTER_ADDRESS` — Base mainnet: `0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43`
  - `AERODROME_CL_ROUTER_ADDRESS` — Base mainnet: `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5`
  - `AERODROME_STABLE` — Default `false` for volatile pairs. When `DIEM_VVV_PAIR_ADDRESS` is set, the DIEM/VVV hop is automatically forced to volatile (stable=false) regardless of this setting, as the configured pair is volatile.
- **Aerodrome exact-out:**
  - `AERODROME_EXACT_OUT_ENABLE` — Set to `0`, `false`, `no`, or `off` to disable exact-out quoting on Aerodrome (default: `0`). Keep disabled until Aerodrome router issues are resolved.
- **UniswapV3 route skipping (reduce RPC calls and log noise):**
  - `DEX_V3_SKIP_DIEM` (default: `1`) — Skip UniswapV3 quotes for DIEM routes. No vanilla UniswapV3 pool exists for DIEM; all quotes will fail. Enabling this avoids unnecessary RPC calls and eliminates `UniswapV3 empty quote` warnings for DIEM routes.
  - `DEX_V3_SKIP_VVV_USDC` (default: `1`) — Skip UniswapV3 quotes for VVV/USDC routes. The V3 pool exists but often has no liquidity in the current tick range, causing zero-output quotes. Enabling this reduces RPC calls and eliminates empty quote warnings. Set to `0` if V3 liquidity improves.
- **Slot0 caching (reduce duplicate RPC calls within a cycle):**
  - `DEX_SLOT0_CACHE_TTL_SECONDS` (default: `10`) — Cache V3 `slot0` reads briefly to avoid duplicate RPC calls when quoting V3-style pools.
- **UniswapV3 approval policy (STF prevention):**
  - `DEX_APPROVE_MAX` (default: `1`) — Enable infinite (max uint256) token approvals for swap routers. When enabled, tokens are approved once with max allowance, avoiding per-trade approval transactions. Prevents STF (SafeTransferFrom) reverts on composite trades where intermediate tokens need router allowance. **Recommended: enabled (1)**.
  - `DEX_UNISWAP_V3_APPROVE_MAX` — Provider-specific override for UniswapV3. Takes precedence over `DEX_APPROVE_MAX` when set. Use to enable/disable infinite approvals for V3 only.
  - When a composite trade (e.g., DIEM→VVV→USDC) fails with an STF-style revert on the UniswapV3 leg, the system automatically injects an approval and retries the leg once. This retry is logged as `Composite STF retry` with `retry_reason=stf_allowance_low`.
- **DIEM/VVV bridge leg configuration:**
  - `DIEM_VVV_PAIR_ADDRESS` — Required for DIEM→VVV bridge leg. Base mainnet: `0xbB345D35450BF9Ee76F3D2cE214E8e7AC5e1071d`
  - `DIEM_VVV_DIRECT_SWAP_ENABLE` — Set to `1`, `true`, `yes`, or `on` to enable direct reserve-based quoting for DIEM/VVV leg. When enabled, uses `DIEM_VVV_PAIR_ADDRESS` for reserve math instead of router `getAmountsOut`, avoiding router timeouts and improving reliability. **Recommended: enabled (1)**.
  - `DIEM_ENABLE_PAIR_MATH_FALLBACK` — Set to `1`, `true`, `yes`, or `on` to enable pair math fallback for DIEM/VVV quotes. When enabled, uses direct reserve calculations when router calls revert, allowing quoting even without factory registration. **Critical for production: enabled (1)**. Default: `1` in `config/default.yml`.
  - `DIEM_VVV_STABLE` — Optional override for DIEM/VVV hop stable flag. When `DIEM_VVV_PAIR_ADDRESS` is set, this is automatically forced to `false` (volatile) regardless of `AERODROME_STABLE`.
  - `DIEM_VVV_BRIDGE_PROVIDER` — Provider to use for DIEM↔VVV leg (default: `aerodrome`). Options: `aerodrome`, `uniswap_v2`, `uniswap_v3`.
  - `VVV_USDC_BRIDGE_PROVIDER` — Provider to use for VVV↔USDC leg (default: `aerodrome_cl`). Options: `aerodrome_cl`, `uniswap_v3`, `uniswap_v2`, `aerodrome`. **Note**: The VVV/USDC pool (`0x67A11022...`) is an Aerodrome SlipStream (concentrated liquidity) pool. The `uniswap_v3` quoter fails for this pool; use `aerodrome_cl` for correct quoting. Fallback to `uniswap_v2` is available but requires an on-chain V2 pool.
  - `BRIDGE_LEG2_MIN_VVV_UNITS` (default: `0`) — Skip VVV→USDC leg2 quotes below this VVV amount (base units). Lower this to allow smaller trades through the bridge.
  - `VVV_USDC_V2_FALLBACK_ENABLE` (default: `0`) - Enable Uniswap V2 fallback for VVV↔USDC leg only when a V2 pool exists. Set to `1` to enable.
  - `VVV_USDC_V2_FALLBACK_ONLY_FOR_BUYS` (default: `0`) - Restrict V2 fallback to buy paths only. Set to `1` to enable V2 fallback only for exact-in buy quotes, keeping sells on V3-only path. Useful when V2 liquidity is limited and you want to preserve V3 execution for sells.
- Pricing: `QUOTE_TOKEN_ADDRESS`, optional `TRADE_PATH` for DIEM pricing. `core/config.py` enforces `TRADE_PATH` presence at startup instead of falling back to `TRADE_PATHS` or `TRADE_PATH_2`.
  - **Important**: `TRADE_PATH` should be in the sell direction (`DIEM -> VVV -> USDC`), not the buy direction.
  - Format: comma-separated token addresses with optional fee tiers (`@3000`), e.g., `0x...,0x...,0x...@3000`.
  - Fee tiers are required for Uniswap V3 pools. The `@3000` annotation on the final token applies to the last hop (VVV->USDC).
  - If a hop uses a V3 pool and no fee tier is specified, the route parser will auto-inject `VVV_USDC_POOL_FEE` (default 3000) for VVV/USDC pairs.
  - The ArbiDiem agent's mint/sell operation requires the sell path to work correctly.

### DIEM Price Path Selection

- `DIEM_PREFER_DIRECT_ROUTE` (default: `1`) - Prefer the direct DIEM/USDC pool over multi-hop routes.
- `DIEM_USDC_POOL_ADDRESS` - Direct DIEM/USDC V3 pool address used for pricing.
- `DIEM_USDC_TICK_SPACING` (default: `100`) - Tick spacing for the DIEM/USDC SlipStream pool; required for CL execution.
- `DIEM_FAIR_VALUE_HORIZON_DAYS` (default: `365`) - Horizon for fair value PV calculation.
- `DIEM_FAIR_VALUE_BLEND_MARKET` (default: `0.0`) - Blend ratio between model fair value and market price (`0.0`-`1.0`).
- Dedicated buy path: `TRADE_PATH_BUY` (e.g., `0xUSDC,0xVVV,0xDIEM` in buy direction).
  - Route for buy/burn operations that mirrors the sell path through VVV.
  - **Important**: The WETH route (`USDC -> WETH -> DIEM`) has **no liquidity** for the WETH->DIEM leg. Use VVV instead.
  - The VVV route (`USDC -> VVV -> DIEM`) uses the actual Aerodrome DIEM/VVV pair for execution.
  - Format: buy direction only (`USDC -> VVV -> DIEM`).

Buy execution reliability checklist:

- `DIEM_BUY_EXECUTION_MODE=exact_in`
- `TRADE_PATH_BUY` is `USDC -> VVV -> DIEM`
- `TRADE_PATH` is `DIEM -> VVV -> USDC`
- `DIEM_VVV_DIRECT_SWAP_ENABLE=1` and `DIEM_ENABLE_PAIR_MATH_FALLBACK=1`
- `AERODROME_EXACT_OUT_ENABLE=0`
- **Execution mode configuration:**
  - `DIEM_BUY_EXECUTION_MODE` (default: `exact_in`) - Execution mode for buy trades. Options:
    - `exact_in` - Spend USDC budget, get as much DIEM as possible. Avoids multi-hop exact-out reverts (SPL/no-pool errors). **Recommended for production**.
    - `exact_out` - Get exact DIEM amount, spend variable USDC. Falls back to exact-in if exact-out fails.
  - `DIEM_SELL_EXECUTION_MODE` (default: `exact_out`) - Execution mode for sell trades. Options:
    - `exact_out` - Sell exact DIEM amount, get USDC. Bridge composite path works reliably for sells. **Recommended for production**.
    - `exact_in` - Spend exact DIEM amount, get variable USDC output.
  - When `DIEM_BUY_EXECUTION_MODE=exact_in`, the system computes `amount_in_usdc` from the desired DIEM amount using:
    1. Prior exact-out quote (if available) with 2% buffer
    2. Market price from market data provider with 2% buffer
    3. Fallback estimate ($140/DIEM) with 2% buffer
  - Exact-in attempts are logged with `mode="exact_in"` and include `execution_mode` in structured logs. If exact-in fails, the system automatically falls back to exact-out execution.
- Timeouts: `DEX_PROVIDER_TIMEOUT_SECONDS`, `DEX_AGGREGATE_TIMEOUT_SECONDS`, `DEX_PROVIDER_TIMEOUT_SECONDS_DIEM`, `DEX_AGGREGATE_TIMEOUT_SECONDS_DIEM`, `RPC_REQUEST_TIMEOUT_SECONDS`.
- Early exit: `DEX_EARLY_EXIT_FIRST_QUOTE` (default: `1`) - Stop quote aggregation after the first valid quote.
- Route muting and circuit breaker (for handling repeated route failures):
  - `DIEM_ROUTE_REVERT_BAN_ENABLE` (default: `1`) - Enable route muting when routes repeatedly revert. Set to `0` to disable.
  - `DIEM_ROUTE_REVERT_BAN_THRESHOLD` (default: `2`) - Number of structural reverts (SPL/no-data) before a route is muted.
  - `DIEM_ROUTE_REVERT_BAN_TTL_SECONDS` (default: `1800`) - Time-to-live for route mutes. Routes are automatically unmuted after this duration.
  - `DIEM_CANONICAL_ROUTE_REVERT_BAN_THRESHOLD` (default: `3`) - Separate threshold for canonical DIEM→WETH→USDC routes. Lowered from 5 to accelerate muting of failing canonical routes.
  - `DIEM_CANONICAL_ROUTE_REVERT_BAN_TTL_SECONDS` (default: `900`) - TTL for canonical route mutes. Lowered from 1800 to allow faster recovery for critical paths.
	  - Preview coherence guard (for DIEM price vs preview mismatches):
	    - `DIEM_ROUTE_COHERENCE_MUTE_ENABLE` (default: `1`) - Enable muting when execution preview price is incoherent vs market price.
	    - `DIEM_ROUTE_COHERENCE_MAX_REL_DIFF` (default: `0.50`) - Strict threshold: mute when `abs(preview_price / market_price - 1)` exceeds this value. Set higher (e.g., `500.0`) when probe amounts cause extreme rounding errors.
	    - `DIEM_ROUTE_COHERENCE_MAX_DRIFT` (default: unset) - Relaxed threshold for DIEM/VVV routes when the bridge reference is unavailable or market data is stale.
	    - `DIEM_ROUTE_COHERENCE_MUTE_TTL_SECONDS` (default: `7200`) - TTL for incoherent-preview mutes.
	    - `DIEM_ROUTE_COHERENCE_SKIP_BRIDGE` (default: `0`) - Skip coherence muting entirely for bridge routes (DIEM→VVV→USDC). Set to `1` when bridge reserve math is trusted but DEX quotes return incoherent prices due to small probe sizes.
  - `DIEM_COHERENCE_BRIDGE_MIN_USD` (default: `5.0`) - Relax the coherence muting guard for small DIEM trades by using a bridge-based DIEM price reference (`bridge_vvv`) and skipping muting when that reference is sane.
  
    This reduces false route mutes when an exact-out preview is noisy for tiny trades or when a bridge leg provider is disabled (e.g., `uniswap_v2` skipped).
  - `DIEM_DISABLE_CANONICAL_WETH` (default: `0`) - Set to `1`, `true`, `yes`, or `on` to disable canonical WETH routes (DIEM→WETH→USDC). When enabled, these routes are omitted from trade route discovery, reducing wasted retries on routes with known liquidity issues. Recommended when bridge routes (DIEM↔VVV↔USDC) are available.
  - Circuit breakers are automatically managed by the DEX aggregator. When all providers for a route have circuits open, the route is skipped immediately.
- Exact-in fallback (for degraded DEX conditions):
  - `DIEM_EXACT_IN_FALLBACK_ENABLE` (default: `0`) - Enable exact-in fallback when exact-out previews fail. Set to `1`, `true`, `yes`, or `on` to enable.
  - `DIEM_EXACT_IN_FALLBACK_MAX_USD` (default: `10.0`) - Maximum USD value per fallback trade. Only small trades use fallback.
  - `DIEM_EXACT_IN_FALLBACK_MAX_SLIPPAGE_BPS` (default: uses `RISK_MAX_SLIPPAGE_BPS`) - Maximum slippage allowed for fallback trades.
  - `DIEM_BUY_FALLBACK_WHEN_BRIDGE_HEALTHY` (default: `0`) - Auto-enable fallback when bridge pricing is healthy and reserves are sane. Set to `1` to enable.
  - `DIEM_COMPOSITE_ANALYTIC_PREVIEW_ENABLE` (default: `0`) - Enable analytic reserve-based preview for DIEM/VVV leg (preview-only, never executes). Set to `1` for diagnostics.
  - `DIEM_VVV_USDC_V3_ANALYTIC_FALLBACK_ENABLE` (default: `0`) - Enable preview-only V3 mid-price analytic fallback for VVV→USDC leg when router exact-out fails. Uses sqrtPriceX96 from V3 pool slot0 to calculate mid-price quotes. **WARNING**: This is ANALYTIC ONLY and quotes are marked with `provider="composite_analytic"` to prevent live execution. Set to `1` for diagnostics/preview only.
  - Fallback size decay coordinates with ArbiDiem's liquidity adjustment config (`ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD`, `ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS`).
- **DIEM Route Policy Configuration:**
  - `AERODROME_CL_ROUTER_ADDRESS` - Aerodrome SlipStream CL router address for DIEM/USDC direct swaps (exact-in and exact-out). Required to execute CL routes. Set to `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5` on Base mainnet.
  - `DIEM_USDC_POOL_ADDRESS` - Address of the DIEM/USDC Aerodrome SlipStream pool (direct, single-hop). Required for slot0 quotes and CL execution.
  - `DIEM_USDC_TICK_SPACING` (default: `100`) - Tick spacing for the DIEM/USDC SlipStream pool. Used to identify the CL pool in `exactInputSingle`.
  - `DIEM_USDC_POOL_FEE` (default: `500`) - Fee tier used for DIEM/USDC direct routing metadata (0.05% for SlipStream). Used for route planning and analytics; CL execution uses tick spacing.
  - `DIEM_MAX_ROUTE_HOPS` (default: `2`) - Cap DIEM routes at specified number of hops. Default 2-hop routes (DIEM→VVV→USDC or DIEM→WETH→USDC) are preferred for reliability. Three-hop routes are disabled by default until SlipStream support is implemented. Routes with more hops are filtered out by both the DIEM client and path engine.
  - `DIEM_ENABLE_THREE_HOP_WETH` (default: `0`) - Enable 3-hop routes via VVV/WETH Aerodrome pool. **Disabled by default** until SlipStream quoting support is implemented. Routes: USDC→WETH→VVV→DIEM (buy) and DIEM→VVV→WETH→USDC (sell). The VVV/WETH Aerodrome pool typically has ~$1.5M+ liquidity vs ~$10K for direct VVV/USDC. Set to `1` to enable when SlipStream support is added.
  - `VVV_WETH_POOL_ADDRESS` - Address of the VVV/WETH Aerodrome SlipStream pool (e.g., `0x01784ef301d79e4b2df3a21ad9a536d4cf09a5ce`). Required for three-hop routing (currently unused when three-hop is disabled).
  - `VVV_WETH_POOL_FEE` (default: `500`) - Fee tier for the VVV/WETH pool (500 = 0.05% for Aerodrome SlipStream).
  - `VVV_WETH_BRIDGE_PROVIDER` (default: `aerodrome`) - DEX provider to use for VVV/WETH leg (currently unused when three-hop is disabled).
  - `DIEM_ROUTE_AVOID_WETH` (default: `0`) - If set to `1`, skip WETH routes when VVV route is available. When enabled, the system prioritizes DIEM→VVV→USDC routes over DIEM→WETH→USDC routes. Set to `1` when VVV routes are more reliable than WETH routes.
  - `DIEM_ROUTE_HEALTH_PROBE_USD` (default: `3.0`) - USD value for route health probe amounts. Prevents dust-sized probes that round to zero on multi-hop routes. Increase to 5.0-10.0 for more reliable health checks on low-liquidity routes.
  - `DIEM_ROUTE_PROBE_AMOUNT_IN_WEI` (default: unset) - Override probe amount in base units (wei). If set, overrides `DIEM_ROUTE_HEALTH_PROBE_USD`. Use only for testing or when USD-based calculation is unavailable.
  - `DIEM_ENABLE_V2_MULTIHOP` (default: `1`) - Enable UniswapV2 for 2-hop DIEM routes (DIEM→VVV→USDC or DIEM→WETH→USDC). When enabled, V2 can handle exact-out quotes for 2-hop routes. Set to `0` to disable V2 multihop (V2 will only handle single-hop routes). **Recommended: enabled (1)** for better quote coverage.
  - Two-transaction fallback was removed. Composite execution is `DexAggregator._execute_composite_*`.

### Buy Direct-Only and Sanity Guards

Use these guards to anchor buy execution to the direct pool and to block bad inputs.  

`DIEM_BUY_DIRECT_ONLY` forces buy routing to the direct DIEM/USDC pool and skips bridge retries.  

Enable it when the direct pool is liquid and you want to avoid bridge price drift.  

It also filters bridge providers from buy quotes, so missing direct liquidity will fail faster.  

Confirm these settings when it is enabled:  

- `AERODROME_CL_ROUTER_ADDRESS`
- `DIEM_USDC_POOL_ADDRESS`
- `DIEM_USDC_TICK_SPACING`

`DIEM_BUY_AMOUNT_IN_SANITY_THRESHOLD` compares computed exact-in input to the direct slot0 quote.  

The check is active when `DIEM_BUY_AMOUNT_IN_SANITY_ENABLE=1`.  

If the ratio exceeds the threshold, the exact-in buy is skipped.  

Lower the threshold to tighten guardrails, and raise it only during incident response.  

### Trade Efficiency Monitoring

Execution diagnostics logs include `trade_efficiency_ratio`, `sanity_check_ratio`, `sanity_check_passed`, `route_type`, and `expected_amount_in_slot0`.  

`route_type` is `direct` or `bridge`.  

The efficiency ratio is `actual_amount_in / expected_amount_in_slot0` and uses the direct slot0 quote.  

Set `DIEM_TRADE_EFFICIENCY_ALERT_THRESHOLD` to emit a warning when the ratio exceeds the threshold.  

Default is `1.5`.  

### Bridge Route Configuration

Use these settings to tune bridge leg fallback and timeouts.

| Variable | Default | Description |
|----------|---------|-------------|
| `VVV_USDC_V2_FALLBACK_ENABLE` | `0` | Enable V2 fallback for VVV/USDC leg (set to `1` only if a V2 pool exists). |
| `BRIDGE_LEG2_TIMEOUT_SECONDS` | `3.0` | Timeout for leg2 quoter calls in bridge routes. |
| `DEX_BRIDGE_TIMEOUT_SECONDS` | `6.0` | Overall bridge provider timeout in the aggregator. |

- Bridge live fallback (for buy/burn when DEX quotes fail):
  - `DIEM_BRIDGE_LIVE_FALLBACK_ENABLE` (default: `0`) - Enable bridge_vvv price as execution preview for live mode when DEX quotes fail. Set to `1` to enable.
  - `DIEM_BRIDGE_LIVE_FALLBACK_MAX_USD` (default: `5.0`) - Maximum USD value per fallback trade. Only small trades use fallback.
  - `DIEM_BRIDGE_LIVE_FALLBACK_SLIPPAGE_BPS` (default: `50.0`) - Conservative slippage assumption for bridge-anchored execution.
- Bridge execution:
  - Live DIEM trades use aggregator composite only (`trade_best` / `trade_best_exact_out`). There is no bridge-exec or two-tx flag. Partial-leg failure is logged as stranded inventory; it does not auto-unwind.
- Path engine timeout:
  - `MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS` (default: `10.0`, max: `30.0`) - Maximum seconds to wait for path engine quotes before falling back to bridge pricing. Default 10s accommodates Base RPC latency for V3 quoter calls. Can be lowered to 2.5-5.0s for constrained hosts (Docker/Replit).
- Path engine provider management (new):
  - `PATH_ENGINE_ROUTE_CACHE_TTL_SECONDS` (default: `60.0`, range: `5-600`) - Route discovery cache TTL to balance freshness vs. discovery overhead.
  - `PATH_ENGINE_PROVIDER_TIMEOUT_THRESHOLD` (default: `3`, min: `1`) - Consecutive timeouts before blocklisting a provider.
  - `PATH_ENGINE_PROVIDER_BACKOFF_SECONDS` (default: `180.0`, min: `5`) - Backoff duration for blocked providers to prevent repeated queries to slow venues.
  - `PATH_ENGINE_MIN_ROUTE_BUDGET_SECONDS` (default: `0.35`, range: `0.05-5.0`) - Minimum time budget required per route before skipping to prevent starting quotes with insufficient time.
  - `PATH_ENGINE_SOFT_TIMEOUT_MARGIN_SECONDS` (default: `0.75`, range: `0.05-5.0`) - Buffer subtracted from timeout to ensure completion.
  - `PATH_ENGINE_SOFT_TIMEOUT_SECONDS` (default: mirrors `MARKETDATA_PATH_ENGINE_TIMEOUT_SECONDS`) - Optional override for the soft budget that now defaults to 10s when unset.
  - `PATH_ENGINE_ROUTE_WORKERS` (default: `2`) - Parallel route evaluations per quote; keep small to avoid RPC saturation while shortening slow-path quotes.
  
  All defaults are consolidated in `config/default.yml` and apply to all deployment environments (Docker, Replit, local) unless overridden via environment variables.

## Route Health & Diagnostics

- **Route health gating**: ArbiDiem automatically filters routes by health before execution and preview. Routes classified as `no_pool`, `zero_liquidity`, or `revert` are excluded from execution and preview quoting. Only `healthy` or `unknown` routes are used. This prevents execution attempts on routes with known liquidity issues and reduces wasted aggregator calls.
- **Route preference**: Bridge routes (DIEM↔VVV↔USDC) are prioritized first, followed by canonical routes. Unhealthy routes are filtered out automatically. When `DIEM_DISABLE_CANONICAL_WETH=1`, canonical WETH routes are omitted entirely.
- CLI probe: `uv run python apps/cli/main.py diem:route-health` - Shows current mute counters, canonical route health, and active circuit breakers for quick on-call checks.
- Buy preview: `uv run python apps/cli/main.py diem:buy-preview --usdc <amount>` - Preview DIEM buy quotes using USDC amount (exact-in mode, bridge routes only). Example: `diem:buy-preview --usdc 10.0` to see how much DIEM 10 USDC can buy.
- Buy execution: `uv run python apps/cli/main.py diem:buy --usdc <amount> [--dry-run]` - Execute DIEM buy trade using USDC amount (exact-in mode, bridge routes only). Example: `diem:buy --usdc 10.0 --dry-run` to preview a buy, or `diem:buy --usdc 10.0` for live execution.
- Diagnostics logging: Route muting, circuit-open detections, and fallback progression are logged to `logs/dex_diagnostics.jsonl` with structured events:
  - `diem_route_revert` - Structural reverts (SPL/no-data) are always logged for diagnostics
  - `diem_route_muted` - Route mute events with route type (canonical vs standard)
  - `diem_route_skipped` - Routes skipped due to muting or circuit-open
  - `diem_fallback_exact_in` - Exact-in fallback progression
  - `diem_fallback_size_decay` - Size decay strategy for fallback trades
  - `diem_buy_preview_shrinker` - Quote shrinker applied to buy-preview when initial quotes fail
  - `diem_buy_strategy` - Strategy selection with route tokens, provider selected, skip reason, exact-out flag, and adjust step
  - `bridge_route_provider.quote: leg1 reserve fallback succeeded` - Reserve math fallback used for DIEM/VVV leg
  - `bridge_route_provider.quote_exact_out: leg2 reserve fallback succeeded` - Reserve math fallback used for exact-out quotes
  - `bridge_route_provider.quote: leg2 V3 analytic fallback succeeded (preview-only)` - V3 mid-price analytic fallback used for VVV→USDC leg (preview-only)
  - `preview_trade: filtering unhealthy route` - Routes filtered by health classification before quoting
  - `DIEM _trade_routes: canonical WETH routes disabled` - Canonical WETH routes omitted when `DIEM_DISABLE_CANONICAL_WETH=1`
- **Troubleshooting dry-run failures**:
  - If all routes are filtered as unhealthy, check `logs/dex_diagnostics.jsonl` for `status: "no_pool"` or `status: "zero_liquidity"` entries
  - Verify `DIEM_VVV_PAIR_ADDRESS` is set correctly and the pair has on-chain reserves
  - Ensure `DIEM_ENABLE_PAIR_MATH_FALLBACK=1` and `DIEM_VVV_DIRECT_SWAP_ENABLE=1` are enabled
  - Check that `bridge_vvv` provider is in `DEX_PROVIDERS` list
  - Review `logs/runtime.log` for "reserve fallback" messages to confirm fallback is triggering
  - **V2 fallback for bridge routes**: When VVV/USDC V3 quotes fail, the bridge provider automatically tries UniswapV2 as a fallback. Check logs for "V2 fallback succeeded" messages. If V2 fallback also fails, verify that a VVV/USDC UniswapV2 pair exists on-chain or consider switching `VVV_USDC_BRIDGE_PROVIDER` to `uniswap_v2` or `aerodrome` if those pools exist.

## Run Modes & Toggles

CLI flags:

```bash
# Dry-run (default)
uv run python apps/cli/main.py run:loop --dry-run
# Progressive-live (recommended)
uv run python apps/cli/main.py run:loop --progressive-live
# Enable-live (immediate)
uv run python apps/cli/main.py run:loop --enable-live
```

Environment toggles:

- `AGENTS_PAUSED=true` — emergency stop without bringing API down.
- `STAKEMASTER_PROGRESSIVE_ENABLE`, `STAKEMASTER_PROGRESSIVE_CYCLES`.

### StakeMaster claim gating

- `STAKEMASTER_MIN_CLAIM_USD` (default `0.05`) — Absolute claim floor in USD.
- `STAKEMASTER_CLAIM_GAS_BUFFER_MULT` (default `2.0`) — Gas buffer multiple for claim gating.
- `STAKEMASTER_MIN_CLAIM_INTERVAL_SECONDS` (default `3600`) — Minimum interval between claim attempts.
- `STAKEMASTER_MIN_CLAIM_UNITS` (default `0`) — Optional safety floor used only when USD valuation is unavailable.

Claim only when:

```text
required_reward_usd = max(STAKEMASTER_MIN_CLAIM_USD, gas_fee_usd * STAKEMASTER_CLAIM_GAS_BUFFER_MULT)
reward_usd >= required_reward_usd
```

### StakeMaster idle stake overflow backoff

These settings apply when a stake simulation fails with an arithmetic overflow (e.g., `panic error 0x11`).

- `STAKEMASTER_IDLE_STAKE_OVERFLOW_MAX_RETRIES` (default `4`) — Maximum backoff retries after an overflow estimate failure.
- `STAKEMASTER_IDLE_STAKE_OVERFLOW_BACKOFF_MULT` (default `0.5`) — Multiplier applied to stake size on each retry.

## Persistence & fallbacks (production policy)

- `APP_ENV` — set to `production` (Docker/Replit deployments), `staging`, `development`, or `test`.
- Production requires Postgres. `SQL_DATABASE_URL` must point to Postgres (not SQLite or placeholder). Fallbacks to SQLite are disabled in production.
- `DATABASE_SCHEMA` scopes migrations to a Postgres schema. Use unique values per service so Alembic version tables avoid collisions.
- In non‑production, fallbacks are gated by explicit flags:
  - `ALLOW_SQLITE_FALLBACK=1` — allows SQLite engine when Postgres is absent.
  - `ALLOW_JSON_FALLBACK=1` — allows JSON tenant store and memory logs.
  - `ALLOW_INMEMORY_KV_FALLBACK=1` — allows in‑process KV for rate limiting.
- Replit workspaces expose `REPLIT_DB_URL`; when present it automatically overrides `KV_URL` so hosted deployments use the managed KV while Docker Compose continues to rely on Redis.

- Leave `KV_URL` empty unless you paste the full `https://kv.replit.com/v0/<token>` value; placeholder URLs block StakeMaster heartbeats.
- Agent memory logs persist to SQL table `AgentMemory`. Retention is controlled by `MEMORY_RETENTION_DAYS` (default 30).
- Decision inserts fail fast in production when persistence errors occur.
- **ArbiDiem decision reasons**: The agent's `why` block includes a `reason` field that explains why a trade was executed or held:
  - `no_onchain_liquidity` - Execution was rejected because no healthy executable routes were found. This occurs when all DEX routes fail (revert, no pool, zero liquidity) or when route-type constraints prevent execution (e.g., all routes are V3-only but V2 fallback is disabled). The `has_onchain_liquidity` field in the rationale will be `False` when this reason is set. This is distinct from `no_liquidity_preview` (which indicates preview quotes failed) and `execution_rejected` (which is a generic execution failure).
  - `execution_rejected` - Generic execution failure (check `execution_error` and `execution_diagnostics` for details).
  - `slippage_exceeded` - Trade size was adjusted to zero due to slippage constraints.
  - `risk_rejected` - Risk policy rejected the trade (suggested units = 0).
  - `no_execution_preview` - No valid execution price preview available.
  - `all_routes_unhealthy` - All routes were classified as unhealthy before execution.
  - `market_not_favorable` - Market conditions don't meet premium/discount thresholds.

Metrics and visibility:

- The Broker exposes Prometheus counters at `/metrics` (e.g., `vvv_fallback_sqlite_total`, `vvv_fallback_json_store_total`, `vvv_fallback_inmemory_kv_total`, `vvv_sql_connect_errors_total`).

## Quorum & Reflex

- Quorum: `QUORUM_ENABLE`, `QUORUM_THRESHOLD`, `QUORUM_WEIGHT_*`, model thresholds.

## Replit & Docker prestart

- Migrations must be applied before serving:
  - Docker: call `scripts/prestart.sh` (runs `alembic upgrade head` then environment validation).
  - Replit: call `scripts/replit_prestart.sh` in the deployment prestart hook.
- Replit production databases are PostgreSQL 16 on Neon (managed). Use the provided DSN and apply migrations before serving. See:
  - Replit SQL Database: https://docs.replit.com/cloud-services/storage-and-databases/sql-database.md
  - Replit Production Databases: https://docs.replit.com/cloud-services/storage-and-databases/production-databases.md
  - Replit KV/Database (fallback for KV): https://docs.replit.com/cloud-services/storage-and-databases/replit-database.md

## Risk & DIEM

- Mint/burn gates: `DIEM_ENABLE_SVVV_GATE`, `DIEM_MINT_RATE_SVVV_PER_DIEM`, `DIEM_MINT_RATE`, `DIEM_DECIMALS`, `SVVV_DECIMALS`.
- Optional supply target for fair value heuristics: `DIEM_TARGET_SUPPLY` (default 38_000).
- Thresholds: `DIEM_PREMIUM_THRESHOLD`, `DIEM_DISCOUNT_THRESHOLD`.
- **Capacity recovery (locked sVVV ratio cap):**
  Balances staked (unlocked) sVVV for emissions vs. locked sVVV for DIEM minting. When the locked ratio exceeds the cap, the agent triggers recovery by either buying DIEM + burning (to unlock sVVV) or buying VVV + staking (to grow the denominator). This ensures enough capacity remains unlocked for yield while capturing DIEM premium opportunities.

  - `DIEM_LOCKED_SVVV_RATIO_CAP` (default `0.65`) — Trigger recovery when `locked_svvv/total_svvv` exceeds this cap (65% locked).
  - `DIEM_LOCKED_SVVV_RATIO_TARGET` (default `0.50`) — Target ratio to reach after recovery (hysteresis). Provides buffer before re-triggering.
  - `DIEM_LOCKED_SVVV_RATIO_MIN_TOTAL_SVVV_UNITS` (default `50e18`) — Minimum total sVVV base units (50 sVVV) required before the ratio cap can trigger. Prevents noise on small positions.
  - `DIEM_RECOVERY_MIN_TRADE_USD` (optional) — Minimum USDC notional for recovery trades.
  - `DIEM_RECOVERY_MAX_TRADE_USD` (optional) — Maximum USDC notional for recovery trades.
  - `DIEM_RECOVERY_MAX_STEPS` (default `10`) — Max halving steps when searching for an executable recovery size.
  - `VVV_FV_PREFER_STAKE_DISCOUNT_MULT` (default `1.15`) — Prefer the stake recovery path when `vvv_intrinsic_fv_usd / vvv_price_usd` exceeds this multiple (15% discount to intrinsic).
  - `ARBI_DIEM_RECOVERY_PREFERRED_ACTION` (default `auto`) — `stake`, `burn`, or `auto`.
  - `ARBI_DIEM_RECOVERY_MAX_SLIPPAGE_BPS` (default `500`) — Recovery-only max slippage cap (bps) for small recovery trades.
  - `ARBI_DIEM_RECOVERY_SMALL_TRADE_USD` (default `5`) — Only apply the recovery-only slippage cap at or below this USD notional.
  - `ARBI_DIEM_RECOVERY_PRICE_SANITY_MAX_REL_DIFF` (default `0.75`) — Block recovery when quote-implied price differs from reference price by more than this relative fraction.
- **Mint slippage protection:**
  - `DIEM_MINT_SLIPPAGE_PCT` (default `25.0`) — Slippage tolerance percentage for `minDiemAmountOut`. The mint curve changes between simulation and execution, so this must be generous. 5% caused reverts; 25% is the tested safe default.
  - `DIEM_MINT_DISABLE_SLIPPAGE` — Set to `true` to disable slippage protection entirely (sets `minDiemAmountOut=0`). Matches manual transaction behavior but removes protection against unfavorable mint rates.
  - `DIEM_MINT_MIN_OUTPUT` — Force a specific `minDiemAmountOut` value (overrides slippage calculation). Set to `0` for same behavior as disabled slippage.
- **Trade size limits:**
  - `RISK_MAX_DIEM_TRADE_UNITS` — absolute maximum DIEM units per trade (overrides USD limit). Supports scientific notation (e.g., `5e17` for 0.5 DIEM with 18 decimals).
  - `RISK_MAX_DIEM_TRADE_USD` — maximum USD notional per trade (default: 10_000). Ignored if `RISK_MAX_DIEM_TRADE_UNITS` is set.
  - `ARBI_DIEM_TRADE_USD` — target USD notional per mint/sell or buy/burn before liquidity/risk adjustments (default: 20). Converts to DIEM units using the live price.
  - `ARBI_DIEM_MIN_TRADE_USD` — minimum USD notional for ArbiDiem trades (use `1.0` for normal ops; lower only in test environments to allow tiny trades).
- **Liquidity adjustment (slippage-based sizing):**
  - `ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS` (default: `3`) — Maximum number of iterations to shrink trade size when slippage exceeds cap. Each iteration halves the size. Increase for deeper markets, decrease for faster decisions.
  - `ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD` (default: `2.0`) — Minimum trade notional in USD below which ArbiDiem stops shrinking. Trades smaller than this threshold will not execute even if slippage is acceptable. Set lower (e.g., `0.5`) for thin markets, higher (e.g., `5.0`) to avoid dust trades.
  - `ARBI_DIEM_SLIPPAGE_SOFT_BPS` (optional) — Soft slippage cap in basis points for telemetry/classification only. If set, trades slightly over the hard cap (`RISK_MAX_SLIPPAGE_BPS`) but under this soft cap may be classified differently in logs/metrics, but execution still requires hard cap compliance. Leave unset to disable soft cap.
- Exact-in fallback: See DEX Configuration section above for `DIEM_EXACT_IN_FALLBACK_*` variables.

### DIEM Fair Value Model

- `DIEM_FAIR_VALUE_HORIZON_DAYS` (default 365) - PV calculation horizon in days (30-1825). Longer horizons increase fair value.
- `DIEM_FAIR_VALUE_BLEND_MARKET` (default 0.0) - Blend ratio between model fair value and market price (0.0-1.0).
- `DIEM_ADOPTION_BASE` (default 0.60) - Baseline adoption rate when utilization is unknown (0.25-0.90). Represents expected DIEM usage.
- `DIEM_ILLIQUIDITY_DISCOUNT` (default 0.80) - Discount multiplier when no on-chain DEX pools exist (0.50-1.00). Set to 1.0 when liquidity is added.
- `DIEM_DISCOUNT_RATE_APY` (default 0.15) - Annual discount rate for PV calculation (0.10-0.30). Higher rates reduce fair value.
- `DIEM_GROWTH_RATE_APY` (default 0.05) - Expected growth rate for Gordon Growth Model (0.00-0.10).
- Sizing & guards: `RISK_MAX_SLIPPAGE_BPS`, `RISK_MAX_POOL_TAKE_BPS`, `RISK_MAX_VOLATILITY_BPS`, `RISK_UTIL_ALPHA`.
- Price ticks: `RISK_VOL_PERSIST` (off switch). When unset, persistence is on if `SQL_DATABASE_URL` is set. Orchestrator writes `PriceTick` and seeds the last 16 prices into vol history.
- DIEM staking helpers: `DIEM_STAKING_ADDRESS`, `DIEM_STAKING_ABI`, `DIEM_STAKE_FN`, `DIEM_LOCK_ON_MINT`, `DIEM_UNLOCK_AFTER_BURN`, `DIEM_UNLOCK_COOLDOWN_SECONDS`.
- **Purchased DIEM handling (for DEX-purchased DIEM without locked sVVV):**
  - `DIEM_SKIP_BURN_IF_NO_LOCKED_SVVV` (default: `0`) - Set to `1` to gracefully skip burn attempts when the wallet holds purchased DIEM (no locked sVVV collateral). When enabled, burn attempts return `status: "skipped"` instead of `status: "error"`, preventing high-severity reflection halts. **Recommended: enabled (1)** when the wallet primarily holds DEX-purchased DIEM. The system will recommend selling on DEX instead of burning.
  - `DIEM_DEFER_POST_BUY_BURN` (default: `1`) - Set to `1` to defer burning newly purchased DIEM to the next cycle instead of attempting immediately after the DEX buy. This avoids race conditions where the RPC state hasn't updated yet, causing on-chain burn reverts with `INSUFFICIENT_BALANCE`. **Recommended: enabled (1)**. The purchased DIEM will be burned in the next cycle when the wallet balance is properly reflected.

### VVV Intrinsic Fair Value (recovery-only)

- Used only to choose between stake-based recovery and buy/burn unlock recovery.
- Inputs:
  - `VVV_FV_HORIZON_DAYS` (default `365`)
  - `VVV_FV_DISCOUNT_APY` (default `0.20`)
  - `VVV_FV_EMISSIONS_VVV_PER_DAY_PER_STAKED_VVV` (default `0.0`)
  - `VVV_FV_DIEM_PER_DAY_PER_STAKED_VVV` (default `0.0`)
  - `VVV_FV_DIEM_UTILITY_USD_PER_DIEM_DAY` (default `1.0`)
  - `VVV_FV_LOCKED_EMISSIONS_MULT` (default `0.8`)

## Reflex & Reflection

- **Reflex Guardian (runtime anomaly detection):**
  - `REFLEX_MAX_VOL_BPS` (default: `450`) - Maximum realized volatility (bps) before halting live trades.
  - `REFLEX_MAX_UTILIZATION` (default: `0.92`) - Maximum utilization ratio before halting.
  - `REFLEX_MAX_PRICE_DRAWDOWN` (default: `0.12`) - Maximum price drawdown before halting (as decimal, e.g., 0.12 = 12%).
  - `REFLEX_APPLY_DRY_RUN` (default: `false`) - Apply reflex guards even in dry-run mode.
  - `REFLEX_REQUIRE_ACTIVE_STAKE` (default: `true`) - Require active staker status for live trades.
  - `REFLEX_STAKE_INACTIVE_CONSEC` (default: `3`) - Consecutive inactive stake cycles before halting.
  - `REFLEX_PROVIDER_ERROR_THRESHOLD` (default: `2`) - DEX provider error streak count before halting.
- **Reflection Engine (post-cycle analysis):**
  - `REFLECTION_VOL_BPS_THRESHOLD` (default: `450`) - Volatility threshold for medium severity notes.
  - `REFLECTION_HOLD_STREAK` (default: `4`) - Hold streak count before noting in reflection.
  - `REFLECTION_HALT_ENABLE` (default: `true`) - Enable sticky halts on high-severity reflection events.
  - `REFLECTION_HALT_TTL_SECONDS` (default: `900`) - Duration (seconds) for high-severity reflection halts. Reduce for faster recovery (e.g., `300`).

## Broker

- Core: `BROKER_ADMIN_TOKEN`, `BROKER_REQUIRE_ADMIN_TOKEN`, `BROKER_DEFAULT_MODEL`.
- Features: `QUOTES_ENABLED`, `PURCHASES_ENABLED`, `BIDS_ENABLED`, `PRICE_ENGINE`, `ACCEPT_ASSETS`, `TREASURY_ADDRESS`.
- Spot quotes: `GET /v1/quotes` prices `units` in `asset`. Live unit price is market × `(1 + inventory_utilization * PRICE_UTIL_ALPHA)`, then the per-asset discount. Utilization is tenant Diem used / issued limits from the inventory snapshot, not Venice `/vvv/utilization` and not `CAPACITY_UNITS_PER_MIN` (that env is unused).
- Failsafe: CapacityBroker writes `hot` when utilization crosses `BROKER_UTIL_SURGE_THRESHOLD`. New quotes and bids then return 503 until the snapshot cools.
- Bids: `BIDS_ENABLED` turns `POST /v1/bids` and `POST /v1/settlement/{id}/settle` on together. The buy page then shows Order type. Max unit price is human units of the pay asset per 1 DIEM (USDC 6 decimals, ETH 18, WBTC 8). Place Bid asks the wallet to sign EIP-712 `PurchaseIntent` (domain from `SIGN_DOMAIN_NAME` / `SIGN_DOMAIN_VERSION` / `CHAIN_ID`); that is not a payment. Settle persists a quote when live `unitPrice` ≤ `maxPrice`. 409 means expired, out of band, or price exceeds max. Confirm stays on `POST /v1/purchases/verify` (purchases also expose `/v1/settlement/confirm` as an alias). The paying wallet must match `Bid.buyer_address`.
- Clearing: `CLEARING_ENABLED` is in-band classification and optional SSE only. It does not turn on settlement. `CLEARING_SSE_INTERVAL` is seconds.
- Quote persistence: `QUOTES_PERSIST_ENABLED` (default true), `QUOTES_ASYNC_ENABLED` (default false; keep false so verify cannot race the quote row).
- Payment checks: `PURCHASE_MIN_CONFIRMATIONS` (default 5), `PURCHASE_CHAIN_ID` / `BASE_CHAIN_ID` (default 8453), `PURCHASE_UNDERPAY_TOLERANCE_BPS` (default 0).
- Public rate limits: `BUY_RATE_LIMITS_ENABLED` (default true), `BUY_RATE_LIMIT_WINDOW_SECONDS` (default 60), `BUY_RATE_LIMIT_MAX_REQUESTS` (default 30).
- CapacityBroker guidance knobs (policy snapshot / failsafe, not a second quote formula): `BROKER_UTIL_TARGET`, `BROKER_PRICE_STEP_BPS`, `BROKER_DISCOUNT_MAX_BPS`, `BROKER_HYSTERESIS_WINDOW`, `BROKER_UTIL_SURGE_THRESHOLD`, `BROKER_UTIL_RELAX_THRESHOLD`, `BROKER_BASE_PRICE_USD`, `BROKER_SURGE_MULTIPLIER`.
- Inventory policy snapshot: `BROKER_INVENTORY_POLICY_PATH` (default `db/broker_inventory_policy.json`). CapacityBroker writes it; quotes and bids read it.
- CORS: `CORS_ENABLED`, `CORS_ALLOW_ORIGINS`.

### Buyer Discounts (DIEM Pricing)

Discounts are applied to DIEM quote pricing to incentivize purchases. Configure per-asset or use a default.

| Env Var | Default | Description |
| --- | --- | --- |
| `PRICE_DISCOUNT_DEFAULT_BPS` | 500 | Default discount (basis points, 100 = 1%) for all assets |
| `PRICE_DISCOUNT_USDC_BPS` | (falls back to default) | Discount for USDC payments |
| `PRICE_DISCOUNT_ETH_BPS` | (falls back to default) | Discount for ETH payments |
| `PRICE_DISCOUNT_WBTC_BPS` | 1000 | Discount for WBTC payments (10% default) |

Discounts are displayed in the `buy.html` frontend Market Snapshot table and applied when generating quotes.
Set `PRICE_DISCOUNT_*_BPS=0` to disable discounts for a specific asset.

## Wallet Configuration

- `TREASURY_ADDRESS` — Base address for portfolio tracking and broker treasury operations. If unset, falls back to the default wallet provider address. **Do not set to placeholder values like "set-in-secrets"** — either set a valid Base address (0x...) or leave unset.
- `COLD_WALLET_ADDRESS` — Base address for cold wallet sweep operations. If unset, sweep operations will fail. **Do not set to placeholder values like "set-in-secrets"** — either set a valid Base address (0x...) or leave unset.

## Portfolio Monitoring

Configuration defaults for low USDC balance warnings.

| Env Var | Default | Description |
| --- | --- | --- |
| PORTFOLIO_USDC_LOW_BALANCE_USD | 5.0 | USDC threshold below which warning is logged |
| PORTFOLIO_USDC_WARN_INTERVAL_CYCLES | 10 | Minimum cycles between repeated warnings |

## AI Treasurer

- Automation: `TREASURER_ENABLE_AUTOMATION`, `TREASURER_MIN_ACTION_USD`, `TREASURER_MAX_ACTIONS_PER_CYCLE`.
- Recycling: `STAKEMASTER_MIN_STAKE_USD`, `USDC_TOKEN_ADDRESS`, `USDC_DECIMALS`.
- Portfolio caps: `RISK_ENABLE_PORTFOLIO_CAP`, `RISK_MAX_USDC_TRADE_PCT`.

## Debug & Instrumentation

- `DIEM_DEBUG_ROUTES=1` — logs normalized routes and aggregator diagnostics.
- `MARKETDATA_DEBUG_SANITY=1` — emits price sanity clamp context (avoid in prod).
- Sanity drift: `MARKETDATA_PRICE_SANITY_MAX_DRIFT`, `MARKETDATA_SANITY_THRESHOLD`.
- CLI helper: `uv run python apps/cli/main.py diem:mint-rate [--live]` to inspect cached or on-chain mint rate.

## Troubleshooting: When V3 Routes Are Down

When V3 DIEM/VVV routes repeatedly fail with "execution reverted" + "no data" or "SPL" errors:

1. **Check route health**: `uv run python apps/cli/main.py diem:route-health`
   - Look for muted routes and circuit-open providers
   - Canonical routes should show `healthy: true` if available

2. **Review diagnostics**: Check `logs/dex_diagnostics.jsonl` for:
   - `diem_route_revert` events showing which routes are failing
   - `diem_route_muted` events indicating routes that have been muted
   - `diem_fallback_exact_in` showing fallback progression

3. **Adjust thresholds** (if needed):
   - Lower `DIEM_ROUTE_REVERT_BAN_THRESHOLD` (default: 2) to mute faster
   - Increase `DIEM_CANONICAL_ROUTE_REVERT_BAN_THRESHOLD` (default: 3) for more resilience on canonical paths
   - Adjust `DIEM_ROUTE_REVERT_BAN_TTL_SECONDS` (default: 1800) to control mute duration
   - Set `DIEM_DISABLE_CANONICAL_WETH=1` to disable canonical WETH routes entirely when bridge routes are available

4. **Enable exact-in fallback**: Set `DIEM_EXACT_IN_FALLBACK_ENABLE=1` to allow size-decay fallback when exact-out fails

5. **Force V2 for canonical**: Set `DEX_FORCE_V2_FOR_CANONICAL=1` to prioritize V2 providers for canonical routes

6. **Monitor circuit breakers**: Check `diem:route-health` output for `circuit_breakers` section showing which providers have circuits open

## Preflight Checks and Progressive-Live Runbook

Before running live trades, perform these preflight checks:

### 1. Preflight Checks

**Wallet and Gas:**
- Ensure wallet balance > 0.01 ETH on Base for gas
- Verify `BASE_RPC_URL` is correct and accessible
- Confirm `ETH_PRIVATE_KEY` is set and matches the intended wallet

**Token Approvals:**
- Run allowance check: `uv run python apps/cli/main.py diem:check-allowances` (if available)
- Or manually ensure approvals for USDC, VVV, DIEM to:
  - `UNISWAP_V2_ROUTER_ADDRESS`
  - `AERODROME_ROUTER_ADDRESS`
  - `UNISWAP_V3_ROUTER_ADDRESS` (if used)
- Approvals should be set to `MAX_UINT256` (2^256 - 1) for maximum flexibility

**Route Health:**
- Run: `uv run python apps/cli/main.py startup:probe`
- Run: `uv run python apps/cli/main.py quotes:preview --units 1.0`
- Verify bridge routes (DIEM↔VVV↔USDC) are healthy and returning quotes
- Confirm canonical WETH routes are suppressed if `DIEM_DISABLE_CANONICAL_WETH=1`

**Configuration:**
- Verify critical toggles:
  - `DIEM_DISABLE_CANONICAL_WETH=1`
  - `DIEM_VVV_DIRECT_SWAP_ENABLE=1`
  - `DIEM_ENABLE_PAIR_MATH_FALLBACK=1`
  - `QUORUM_ENABLE=1`
- Check risk caps:
  - `RISK_MAX_SLIPPAGE_BPS=75` (or your target)
  - `RISK_MAX_POOL_TAKE_BPS=200`
  - `ARBI_DIEM_LIQUIDITY_MIN_TRADE_USD=2`
  - `ARBI_DIEM_LIQUIDITY_MAX_ADJUST_STEPS=3`

### 2. Progressive-Live Smoke Test

**Run progressive-live mode (2 cycles):**
```bash
uv run python apps/cli/main.py run:loop --progressive-live --sleep 15 --max-cycles 2
```

**Expected behavior:**
- Starts in dry-run mode
- After `STAKEMASTER_PROGRESSIVE_CYCLES` (default 5) healthy heartbeats, switches to live
- Executes small notional trades ($2–$5 USD)
- Slippage should be within configured caps
- Execution logs show:
  - `provider != composite_analytic`
  - `muted=false`
  - Realistic `slippage_bps` values
  - Correct `bounds` (min_amount_out/max_amount_in)
  - `allowance_ok=true` after approvals

**Review execution logs:**
- Check `logs/runtime.log` for `execute_trade: execution diagnostics` entries
- Verify route health filtering logs (`execute_route_health_filter_applied`)
- Confirm no analytic quotes were rejected (`execute_reject_analytic_quote`)
- Check that only healthy routes were used

### 3. Rollback Procedures

**If anomalies detected:**
- Set `QUORUM_ENABLE=0` to disable quorum gating (emergency override)
- Or revert to dry-run only: remove `--progressive-live` and `--enable-live` flags
- Leave toggles intact for diagnostics
- Review `logs/dex_diagnostics.jsonl` for route health details

**If execution fails:**
- Check `logs/runtime.log` for specific error messages
- Verify allowances are sufficient: `ensure_router_allowances()` logs
- Check route health: `diem:route-health` CLI command
- Review circuit breaker status in diagnostics

### 4. Full Live Mode

**After successful smoke test:**
```bash
uv run python apps/cli/main.py run:loop --enable-live --sleep 15
```

**Monitoring:**
- Watch execution diagnostics logs for route/provider/bounds/mute/allowance snapshots
- Monitor `slippage_bps` to ensure it stays within caps
- Check route health filtering logs to confirm unhealthy routes are blocked
- Review quorum decisions and guardrail triggers

## Examples

Minimal local `.env` example:

```bash
VENICE_API_BASE_URL=https://api.venice.ai/api/v1
VENICE_PARENT_KEY=sk-...
BASE_RPC_URL=...
VVV_TOKEN_ADDRESS=0x...
DIEM_TOKEN_ADDRESS=0x...
BROKER_ADMIN_TOKEN=...
QUOTES_ENABLED=1
PURCHASES_ENABLED=1
```

See also: `./DEPLOYMENT.md` for preflight and run-mode commands.
