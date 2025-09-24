### Phase 1: Quick Wins
1. **Parallelize Frontend Fetches** (`apps/control-plane/buy.js`):
   - Replace sequential `await loadEnv(); await fetchPrices();` with `Promise.all([loadEnv(), fetchPrices()])`.
   - Add skeleton loading: Show placeholder price table immediately; swap with real data on resolve.
   - Impact: Cuts initial load from ~1s to ~0.3s.

2. **Extend Cache TTL Selectively** (`services/marketdata/provider.py`):
   - Set `MARKETDATA_PRICE_CACHE_TTL_SECONDS=60` (env default; 30s for DIEM/VVV, 300s for USDC).
   - Add per-symbol TTL: In `_cache_price_set`, use `ttl = 60 if symbol in ['DIEM', 'VVV'] else 300`.
   - Impact: Boosts cache hits from ~60% to 85%, reducing DEX calls by half.

3. **Async Batch DEX Calls** (`libs/dex/providers.py` & `services/marketdata/provider.py`):
   - In `best_price`, use `asyncio.gather` for parallel router quotes (Uniswap + Aerodrome).
   - Batch price resolution in `prices()`: Call `_price_for_symbol` concurrently for symbols.
   - Impact: Parallelizes 2-3s sequential RPCs to ~1s max.

### Phase 2: API/Backend Efficiency
1. **Batch Endpoint** (`apps/broker-api/app.py`):
   - Add `/v1/env-and-prices` endpoint merging `get_env` + `prices` responses.
   - Frontend: Replace separate calls with one batched fetch.
   - In `get_quote`, pre-fetch prices if cache miss (avoid per-quote resolution).

2. **Circuit Breaker for DEX Failures** (`libs/dex/providers.py`):
   - Skip failed routers (e.g., if Aerodrome times out, fallback to Uniswap only) with exponential backoff.
   - Add timeout (500ms) to `agg.best_quote` via `asyncio.wait_for`.
   - Impact: Prevents single slow router from blocking entire quote.

### Phase 3: Monitoring and Iteration
1. **Add Quote-Specific Metrics** (`libs/telemetry/events.py`):
   - Track: `quote_latency_ms` (P50/P95), `dex_call_count`, `cache_hit_rate`.
   - Emit in `PricingService.get_quote`: Wrap in timer, log outcomes (e.g., "dex_error", "cache_hit").

2. **Alerts and Review**:
   - Set alert: P95 quote time >2s or cache hit <80%.