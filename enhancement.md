### Validation (matches analysis)
- **Market data**: `GET /v1/market/prices` implemented with multi‑hop and AMM fallbacks via `MarketDataProvider`.  
- **Clearing price (optional)**: `GET /v1/pricing/clearing_price` and `GET /v1/pricing/clearing_price/stream` implemented behind `CLEARING_ENABLED`.  
- **Quotes + Purchases (flag‑gated)**: `GET /v1/quotes`, `POST /v1/purchases/verify`, `GET /v1/purchases/{id}` implemented behind `QUOTES_ENABLED` / `PURCHASES_ENABLED`.  
- **Bids (optional)**: `POST /v1/bids`, `GET /v1/bids`, `GET /v1/bids/{id}`, `GET /v1/bids/{id}/stream` implemented behind `BIDS_ENABLED`. Signature verification uses EIP‑712 and server-side recovery.  
- **Settlement preview (optional)**: `GET /v1/settlement/quote` exact‑out preview implemented; `POST /v1/bids/{bidId}/settle` implemented and price‑guards against bid `maxPrice`.  
- **Buyer UI**: `apps/control-plane/buy.html` wires markets, clearing (SSE), quotes, pay/verify, bids + settle, and DEX preview. Hides cards by features flags.

### Gaps vs analysis.md
- **Env features exposure**  
  - Analysis expects the UI to toggle cards based on `/v1/env.features` for quotes, purchases, clearing, bids, settlement.  
  - Server only exposes `features.quotes` and `features.purchases`. Missing: `features.clearing`, `features.bids`, `features.settlement`.

- **Clearing price 24‑h change**  
  - Analysis calls for a 24‑h delta. `change24h` is currently `None` in `_compute_clearing_price()`; no calculation is wired.

- **Settlement confirmation + SSE**  
  - Analysis describes `POST /v1/settlement/confirm` and `GET /v1/settlement/:id/stream`.  
  - Implementation uses `POST /v1/purchases/verify` and polling `GET /v1/purchases/{id}`. No confirm alias or SSE stream exists.

- **Buyer UI cleanup**  
  - Analysis suggests removing admin-only UI from the buyer page. `buy.html` still includes a Tenants aside (hidden without an admin token). If strict removal is desired, this is outstanding.

- **Risk hints for buyer sizing**  
  - Analysis mentions slippage and pool‑take guards for settlement quotes. Preview endpoint enforces exact‑out venue behavior and marks `approx=true`, but does not currently apply explicit `RISK_MAX_SLIPPAGE_BPS`/`RISK_MAX_POOL_TAKE_BPS` in the preview response.

- **Tests**  
  - Coverage exists for buyer quote→verify (`tests/test_buyer_e2e.py`).  
  - Missing tests for clearing price endpoints (incl. SSE), bids + SSE lifecycle, settlement preview, and error paths (expired quotes/bids).

- **Env signing block**  
  - Buyer UI reads `env.signing.{name,version,chainId}`; server does not expose a `signing` block in `/v1/env` (UI falls back to defaults).

### Pointed tasks to close

- Backend: `/v1/env` feature flags
  - Add `features.clearing` from `CLEARING_ENABLED`, `features.bids` from `BIDS_ENABLED`, `features.settlement` from `SETTLEMENT_ENABLED` in `apps/broker-api/app.py` (`env_status()`).
  - Acceptance: `GET /v1/env` returns booleans for all five features; `buy.html` cards toggle correctly.

- Backend: clearing price 24‑h delta
  - In `_compute_clearing_price()`, compute `change24h` using `db.models.TokenSnapshot` for DIEM (or DIEM/WETH×WETH/USDC composition) when SQL is available; fallback to `None` gracefully.
  - Acceptance: `change24h` returns a float (ratio or pct) when snapshots exist; remains `null` otherwise.

- Backend: settlement confirm + SSE stream
  - Implement `POST /v1/settlement/confirm` as an alias to purchase verification (or bind to the same logic with strict shape) to match analysis.  
  - Add `GET /v1/purchases/{purchaseId}/stream` mirroring the bids SSE pattern, emitting status transitions `confirmed → fulfilled`.
  - Acceptance: SSE endpoint yields JSON events with `purchaseId` and `status`; confirm endpoint returns the same shape as verify.

- Backend: settlement preview risk guards
  - Optionally enforce `RISK_MAX_SLIPPAGE_BPS` and `RISK_MAX_POOL_TAKE_BPS` in the preview response (`/v1/settlement/quote`), returning the computed `slippageBps` when approximated via AMM.
  - Acceptance: response includes `slippageBps` when derivable; rejections occur when configured caps are exceeded.

- Backend: env signing block
  - Add `signing` to `/v1/env` with `{ name: SIGN_DOMAIN_NAME, version: SIGN_DOMAIN_VERSION, chainId: BASE_CHAIN_ID|CHAIN_ID }`.
  - Acceptance: `buy.html` uses server values instead of defaults.

- Frontend: buyer UI cleanup
  - If required by analysis, remove the “Tenants” aside from `buy.html`. If not, keep hidden behind admin token.
  - Acceptance: buyer page shows only purchase‑related UI for non‑admin users.

- Tests
  - Clearing price: test `GET /v1/pricing/clearing_price` and SSE stream behind `CLEARING_ENABLED=true`.  
  - Bids: create→list→stream a bid; validate status transitions vs clearing band.  
  - Settlement preview: exact‑out quote path and AMM `approx=true` fallback.  
  - Settlement confirm/SSE: verify stream transitions after adding endpoints.  
  - Acceptance: new tests green locally (`uv run pytest -q`) with feature flags.

- Docs alignment
  - Update `README.md` “Buyer Flow” to include the confirm/SSE endpoints if implemented; otherwise adjust analysis to reflect the polling model.
  - Acceptance: docs match live endpoints and flags.

- Optional polish
  - Add `features` to include `quotes`, `purchases`, `clearing`, `bids`, `settlement` consistently in both `/v1/env` and README examples.  
  - Emit telemetry for clearing price updates (already emitted indirectly via market prices) with a stable event key for dashboards.

- Deployment safety
  - Ensure `BROKER_REQUIRE_ADMIN_TOKEN=true` in prod and CORS allowlist is set when `CORS_ENABLED=true`. CI gate (`ci:gate`) already validates these; enforce in deploy manifests.

- Nice‑to‑have
  - Compute clearing acceptance band adaptively from realized volatility (as analysis suggests) once token snapshot history is reliable.  
  - Add `RISK_MAX_POOL_TAKE_BPS` utilization in quotes UI as advisory text when reserve cap bites (use `reserve_cap_units()`).

- Small code hooks
  - Wire `env.features` booleans and `signing` block in `apps/broker-api/app.py` → `env_status()`.  
  - Extend `_compute_clearing_price()` with optional SQL query for last 24h DIEM price to compute `change24h`.  
  - Add purchases SSE in the same style as `bids_stream` using DB polling with backoff and timing guards.

- Acceptance criteria (end‑to‑end)
  - With `QUOTES_ENABLED=true`, `PURCHASES_ENABLED=true`, `CLEARING_ENABLED=true`, and optionally `BIDS_ENABLED=true`, `SETTLEMENT_ENABLED=true`:  
    - UI shows Markets, Clearing (live), Quotes, Pay & Verify. Bids/Settlement panels respect feature flags.  
    - Clearing endpoint returns price, band, and (when data present) `change24h`.  
    - Purchases support polling and (after task) SSE for status.  
    - Tests for clearing, bids, settlement preview, and purchases status pass.