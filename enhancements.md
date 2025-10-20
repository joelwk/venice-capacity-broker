I'll scan the codebase to locate the price/quote retrieval paths, caching layers, and web3 provider setup so we can propose targeted performance enhancements. Running several searches in parallel for speed.

[5 tools called]

I'll open the broker API and market data provider files around the price/quote endpoints and the DEX aggregator builder to pinpoint current caching, concurrency, and timeouts so we can propose precise enhancements.

[4 tools called]

I'll inspect the market data provider's symbol-specific TTL handling and any existing per-symbol overrides, plus search for DIEM-specific caching knobs and any cache invalidation in the price path engine. Then I'll scan for broker API endpoints that call into these paths to see how we can batch or cache responses at the HTTP layer.

[18 tools called]

### Goal
Improve price and quote retrieval latency for the live broker UI and API (`buy.html` and `docs`), while preserving accuracy.

### What we found in the code (relevant hotspots)
- Market prices and DIEM price pathing run through `MarketDataProvider`, which currently builds a DEX aggregator per call in `best_price(...)` and does per-symbol caching with short TTLs.
```1318:1415:services/marketdata/provider.py
def best_price(self, route: RouteLike, amount_in_decimal: float = 1.0, *, label_symbol: Optional[str] = None) -> Dict[str, Any]:
    """Compute best price for the supplied route with telemetry instrumentation."""
    plan = as_route_plan(route)
    tokens = plan.tokens
    if len(tokens) < 2:
        raise ValueError("route must include at least [token_in, token_out]")
    dec_in = self._erc20_decimals(tokens[0])
    dec_out = self._erc20_decimals(tokens[-1])
    amount_in_units = int(amount_in_decimal * (10 ** dec_in))
    label = self._norm_symbol_label(label_symbol or (tokens[0] if tokens else None))

    agg = build_aggregator_from_env()
    supports_reserve = any(getattr(p, "supports_reserve_math", False) for p in getattr(agg, "providers", []))
    ...
    quote = agg.best_quote(amount_in_units, plan)
    ...
```

- The aggregator supports multiple providers and timeouts/backoff, configured by env.
```827:837:libs/dex/providers.py
def build_aggregator_from_env() -> DexAggregator:
    raw_spec = os.getenv("DEX_PROVIDERS", "uniswap_v2,aerodrome")
    specs = _parse_providers_spec(raw_spec)
    providers: List[DexProvider] = []
    for spec in specs:
        provider = _provider_from_spec(spec)
        if provider is not None:
            providers.append(provider)
    if not providers:
        raise EnvironmentError("No DEX providers configured. Set DEX_PROVIDERS and router envs.")
    return DexAggregator(providers)
```

- The broker HTTP `market_prices` endpoint fetches through `MarketDataProvider.prices(...)` on every request; there is no HTTP-layer response cache.
```1608:1716:apps/broker-api/app.py
def market_prices(
    symbols: str = Query(default="VVV,DIEM,ETH,USDC", description="Comma-separated symbols"),
) -> dict:
    ...
    md = MarketDataProvider()
    prices = md.prices(syms)
    duration = time.perf_counter() - start_total
    ...
    return {"symbols": syms, "prices": prices, "ratios": ratios, "meta": meta}
```

### Targeted enhancements (low-risk, measurable)
- Aggregator reuse and micro-TTL for route quotes
  - Reuse a single aggregator instance inside `MarketDataProvider` instead of rebuilding per call.
  - Add a tiny TTL cache (1–3s) for route+amount quote results to collapse bursts and lower RPC calls without sacrificing freshness.

- Warm the price cache proactively
  - Enable the built-in warmer to keep DIEM/ETH/VVV hot and reduce P95 latencies.
  - Keep DIEM/ETH TTL modest (15–30s) and longer TTL for USDC to cut external calls.

- Tune aggregator concurrency and timeouts for Base RPC
  - Use slightly higher provider timeout and limit worker concurrency to avoid RPC rate limits while retaining parallel fan-out.

- Add a small HTTP-layer cache for `/v1/market/prices`
  - Cache response for a short TTL (e.g., 1–2s) keyed by symbol set to shrink P50/P95 under load.

- Validate static trade paths and consider adding Uniswap V3
  - Ensure `TRADE_PATH` is DIEM→WETH→USDC (with fee hints) and keep `TRADE_PATHS_DYNAMIC=false` in production.
  - If you have Base Uniswap V3 router/quoter addresses, add `uniswap_v3` to `DEX_PROVIDERS` with fee tiers 500/3000 to improve reliability and execution.

- Reduce debug overhead in production
  - Disable `DIEM_DEBUG_ROUTES` and `MARKETDATA_DEBUG_SANITY` except during incident triage.

### Proposed code edits (copy/apply)
1) Reuse aggregator and add a tiny TTL quote cache in `MarketDataProvider`:

```python
# services/marketdata/provider.py (add near __init__)
from typing import Tuple
from time import perf_counter

class MarketDataProvider:
    # ...
    _agg = None
    _route_quote_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
    _route_quote_cache_lock = Lock()

    def _get_agg(self):
        if self._agg is None:
            self._agg = build_aggregator_from_env()
        return self._agg

    def _route_quote_ttl(self) -> float:
        try:
            return max(0.0, float(os.getenv("MARKETDATA_ROUTE_QUOTE_TTL_SECONDS") or "2.0"))
        except Exception:
            return 2.0

    def _route_quote_cache_get(self, key: str) -> Optional[Dict[str, Any]]:
        ttl = self._route_quote_ttl()
        if ttl <= 0:
            return None
        now = time.time()
        with self._route_quote_cache_lock:
            entry = self._route_quote_cache.get(key)
            if not entry:
                return None
            ts, payload = entry
            if (now - ts) > ttl:
                self._route_quote_cache.pop(key, None)
                return None
            return dict(payload)

    def _route_quote_cache_set(self, key: str, payload: Dict[str, Any]) -> None:
        ttl = self._route_quote_ttl()
        if ttl <= 0:
            return
        with self._route_quote_cache_lock:
            # simple size cap
            if len(self._route_quote_cache) > 256:
                self._route_quote_cache.clear()
            self._route_quote_cache[key] = (time.time(), dict(payload))

    # in best_price(...) replace aggregator creation and add cache
    def best_price(...):
        plan = as_route_plan(route)
        tokens = plan.tokens
        ...
        amount_in_units = int(amount_in_decimal * (10 ** dec_in))
        key = f"{','.join([t.lower() for t in tokens])}|{amount_in_units}"
        cached_payload = self._route_quote_cache_get(key)
        if cached_payload is not None:
            return cached_payload

        agg = self._get_agg()
        ...
        if quote is not None:
            used_route = quote.route
            price = (quote.amount_out / (10 ** dec_out)) / (quote.amount_in / (10 ** dec_in))
            payload = {
                "provider": quote.provider,
                "amount_in": quote.amount_in,
                "amount_out": quote.amount_out,
                "decimals": {"in": dec_in, "out": dec_out},
                "price": price,
                "path": used_route.tokens,
            }
            self._route_quote_cache_set(key, payload)
            return payload
        ...
        if approx and approx > 0:
            payload = {
                "provider": "approx",
                "amount_in": amount_in_units,
                "amount_out": amount_out_units,
                "decimals": {"in": dec_in, "out": dec_out},
                "price": price,
                "path": plan.tokens,
            }
            self._route_quote_cache_set(key, payload)
            return payload
        ...
```

2) Add a tiny response cache to `/v1/market/prices`:

```python
# apps/broker-api/app.py (module scope, near other globals)
_prices_resp_cache: Dict[str, Tuple[float, dict]] = {}
_PRICES_TTL = float((_os.getenv("BROKER_PRICES_TTL_SECONDS") or "2.0") or 2.0)

def _prices_cache_get(key: str) -> Optional[dict]:
    if _PRICES_TTL <= 0:
        return None
    now = time.time()
    entry = _prices_resp_cache.get(key)
    if not entry:
        return None
    ts, payload = entry
    if (now - ts) > _PRICES_TTL:
        _prices_resp_cache.pop(key, None)
        return None
    return payload

def _prices_cache_set(key: str, payload: dict) -> None:
    if _PRICES_TTL <= 0:
        return
    if len(_prices_resp_cache) > 128:
        _prices_resp_cache.clear()
    _prices_resp_cache[key] = (time.time(), payload)

# inside market_prices(...)
syms = [s.strip() for s in (symbols or "").split(",") if s.strip()]
cache_key = ",".join(sorted([s.upper() for s in syms]))
cached = _prices_cache_get(cache_key)
if cached:
    return cached

md = MarketDataProvider()
prices = md.prices(syms)
...
resp = {"symbols": syms, "prices": prices, "ratios": ratios, "meta": meta}
_prices_cache_set(cache_key, resp)
return resp
```

### Configuration (Replit Secrets / env)
- Core DEX and routing
  - DEX_PROVIDERS=uniswap_v2,aerodrome            (add uniswap_v3 if router/quoter are configured)
  - TRADE_PATH=0xf4d97f2da56e8c3098f3a8d538db630a2606a024@3000,0x4200000000000000000000000000000000000006@500,0x833589fCD6eDb6e08f4c7C32D4f71b54bdA02913
  - TRADE_PATHS_DYNAMIC=false
  - UNISWAP_V3_DEFAULT_FEE=3000
  - QUOTE_TOKEN_ADDRESS=0x833589fCD6eDb6e08f4c7C32D4f71b54bdA02913
  - Optionally add: UNISWAP_V3_ROUTER_ADDRESS, UNISWAP_V3_QUOTER_ADDRESS (Base) then include `uniswap_v3` in `DEX_PROVIDERS` (fees: 500,3000).

- Aggregator timeouts/concurrency
  - DEX_PROVIDER_TIMEOUT_SECONDS=2.5
  - DEX_AGGREGATOR_MAX_WORKERS=3
  - DEX_CIRCUIT_FAILURES=3
  - DEX_CIRCUIT_COOL_OFF_SECONDS=60

- Market data caching/warming
  - MARKETDATA_PRICE_CACHE_MAX_SYMBOLS=64
  - MARKETDATA_PRICE_CACHE_TTL_SECONDS=60
  - MARKETDATA_PRICE_CACHE_TTL_DIEM_SECONDS=30
  - MARKETDATA_PRICE_CACHE_TTL_USDC_SECONDS=300
  - MARKETDATA_EXTERNAL_PRICE_TTL_SECONDS=60
  - MARKETDATA_PRICE_FETCH_WORKERS=4
  - MARKETDATA_WARM_SYMBOLS=DIEM,ETH,VVV,WBTC,USDC
  - MARKETDATA_WARM_INTERVAL_SECONDS=30
  - MARKETDATA_ROUTE_QUOTE_TTL_SECONDS=2.0  (new; pairs with edit above)
  - BROKER_PRICES_TTL_SECONDS=2.0           (new; pairs with edit above)

- Production debug
  - DIEM_DEBUG_ROUTES=0
  - MARKETDATA_DEBUG_SANITY=0

- Venice API base (to avoid spurious 404s per repo rule)
  - VENICE_API_BASE_URL=https://api.venice.ai/api/v1

### Cross-validation plan (accuracy + perf)
- Route and liquidity sanity
  - Run: `uv run python apps/cli/main.py startup:probe`
  - Run: `uv run python apps/cli/main.py quotes:preview --units 1.0`
  - Run: `uv run python apps/cli/main.py market:best-price:scan --start 1.0 --min 1e-12 --factor 10`
  - Expect UniswapV2/Aerodrome quotes to be non-empty for DIEM→WETH→USDC.

- API latency measurement
  - Hit `GET /v1/market/prices?symbols=DIEM,ETH,USDC` repeatedly; confirm `meta.latency_ms` and `meta.provider_latency_ms` decline with cache warm.
  - Hit `GET /v1/quotes?units=1&asset=USDC` repeatedly; verify `pricing.quote` telemetry shows `prefetch=true` and reduced `dexCalls` in `prefetch` payload.
  - Check `GET /v1/env-and-prices` to confirm cache hits/misses and overall duration in `meta`.

- Functional correctness
  - Ensure DIEM price agrees within sanity clamp window when `MARKETDATA_DEBUG_SANITY=1` (temporarily during tests).
  - Run tests: `uv run pytest -q` focusing on:
    - `tests/test_diem_service.py`
    - `tests/test_diem_buy_path.py`
    - `tests/test_dex_exact_out.py`
    - `tests/test_marketdata_prices.py`

- Rollback lever
  - Set `MARKETDATA_ROUTE_QUOTE_TTL_SECONDS=0` and `BROKER_PRICES_TTL_SECONDS=0` to disable the new caches instantly if needed.

### Notes on DB optimizations
- Price/quote latency is dominated by on-chain quoting, not SQL, but add indexes to keep admin endpoints snappy:
  - Composite indexes in `db/models.py` for `Quote(asset, created_at)` and `Purchase(status, created_at)` if you observe admin list slowness.

### Operational tips
- Use a performant Base RPC in `BASE_RPC_URL` (low-latency provider with good rate limits).
- Keep `/v1/market/prices` payloads minimal in UI by requesting only required symbols.

Summary
- Reuse DEX aggregator and add a 2s micro‑TTL for route quotes in `MarketDataProvider`.
- Add a 2s HTTP response cache for `/v1/market/prices`.
- Tune env: static `TRADE_PATH`, aggregator timeout/workers, price TTLs, warm symbols; disable debug in prod.
- Optional: add Uniswap V3 provider if Base addresses are available.
- Validate via CLI probes and `/v1/market/prices`/`/v1/quotes` latency; tests remain green.