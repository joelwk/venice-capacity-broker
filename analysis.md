
---

## 1. Frontend (buy.html / app.js)

### 1.1 Unified “Buy Compute” Panel

* Replace the existing admin‑style page with a single “Buy Compute” card containing two tabs:

  * **“By Units” tab**: input compute units, optional “max price per unit” and “total cap,” plus an asset selector (ETH, USDC or any Base token from `/v1/env`).
  * **“By Budget” tab**: input a total spend; the UI shows an estimate of how many units your budget buys at the current clearing price.
* Populate the asset dropdown dynamically from `/v1/env` (`payments.accepted_assets`).

### 1.2 Market & Clearing Price Widgets

* **DEX vs Market**: In the price grid, show:

  * **VVV price (DEX)** — your current on‑chain Base price from `/v1/market/prices`.
  * **VVV price (Market)** — fetch from an external source (e.g., CoinGecko); hide behind a feature flag if needed.
  * **Deviation** — percentage difference between DEX and Market price.
* **Live Clearing Price**: Add a widget that displays the current clearing price, its 24‑h change, and whether a user’s bid is “out‑of‑band,” “in‑band,” or in the “accepted window.” Subscribe to `/v1/pricing/clearing_price/stream` (SSE) for real‑time updates.

### 1.3 Bid Placement & Tracking

* **Place Bid** button: shows an EIP‑712 signature request for a `PurchaseIntent` object and posts the signed intent to `/v1/bids`.
* **My Bids** table: lists user bids (fetched from `/v1/bids` and live‑updated via SSE). Show status (“out‑of‑band,” “in‑band,” “accepted window,” “expired”), amount, unit price cap, chosen asset, creation and expiry times, and a “Settle now” button for eligible bids.
* **Settle Now** flow:

  * If the bid’s asset is accepted, initiate a direct transfer (ETH send or ERC‑20 transfer/permit).
  * If not accepted, request an exact‑out DEX quote via `/v1/settlement/quote`, display slippage, and on confirm, perform a one‑click swap‑then‑transfer.
* Show “Key issued” event after settlement via `/v1/settlement/:id/stream`.

### 1.4 Cleanup

* Remove any admin‑only UI components (e.g., Tenants sidebar).
* On load, fetch `/v1/env` and hydrate the accepted assets list, router config, and other dynamic settings.

---

## 2. Backend APIs

### 2.1 Market & Clearing Price

* **GET `/v1/market/prices?symbols=…`** (existing): return best prices from on‑chain DEX routes. Support multi‑hop (VVV→WETH→USDC) and handle fallback to reserve mid‑price if no path exists.
* **GET `/v1/pricing/clearing_price`** (new): return current clearing price, 24‑h change, acceptance band, and components (VVV/USDC direct quote, VVV→WETH→USDC route quote, DIEM signals).
* **GET (SSE) `/v1/pricing/clearing_price/stream`** (new): push real‑time clearing price changes and acceptance band transitions.

### 2.2 Bids

* **POST `/v1/bids`**: accept a signed `PurchaseIntent`. Fields: `{buyer, units, maxPrice, asset, expiry, slippageBps, nonce, signature}`. Verify the EIP‑712 signature and store the bid with status “received.”
* **GET `/v1/bids/:id`**: return current bid details and status.
* **GET `/v1/bids`**: list bids for the authenticated user (filter by `buyer`).
* **GET (SSE) `/v1/bids/:id/stream`**: emit status transitions (out‑of‑band, in‑band, accepted window, expired) with clearing price context.

### 2.3 Settlement

* **GET `/v1/settlement/quote`**: compute an exact‑out quote from `from` asset to an accepted asset (`ETH` or `USDC`). Respect `RISK_MAX_SLIPPAGE_BPS` and `RISK_MAX_POOL_TAKE_BPS` caps; return route, expected input amount, slippage, and expiry.
* **POST `/v1/bids/:id/settle`**: handle direct transfer or swap‑and‑transfer. Validate the final quote; on success, store a settlement record and return an ID.
* **POST `/v1/settlement/confirm`**: confirm the transaction hash broadcasted by the user; start a watcher that monitors settlement completion and key issuance.
* **GET (SSE) `/v1/settlement/:id/stream`**: send `{txStatus, keyIssued}` updates until the API key is issued.

### 2.4 Security & Risk

* EIP‑712 signature verification for all `PurchaseIntent` messages.
* Nonce management to prevent replay attacks.
* Enforce `RISK_MAX_SLIPPAGE_BPS` and `RISK_MAX_POOL_TAKE_BPS` on all settlement quotes.
* Validate pool reserves and disable settlement if reserves fall below configured thresholds.

---

## 3. Pricing Engine & Market Data

### 3.1 Multi‑hop Routing

* Configure Uniswap V2 and Aerodrome providers to support multi‑hop routes (e.g., `VVV → WETH → USDC` and `DIEM → WETH → USDC`).
* Ensure factories and init‑code hashes are configured (e.g., Uniswap V2 factory at `0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6`).
* Provide fallback to mid‑price (reserve‑based) when no pools exist or when Uniswap V2 factory/init‑code parameters are missing.
* Optionally incorporate external market prices (e.g., CoinGecko) behind a feature flag.

### 3.2 Clearing Price Calculation

* Define a clearing price as an executable base‑chain price anchored to:

  * Best VVV/USDC or VVV→WETH→USDC route,
  * DIEM signals ratio (if used for compute valuation),
  * Liquidity depth and pool health (skip shallow pools).
* Compute an **acceptance band** around the clearing price (e.g., ±X% or adaptive based on volatility) and expose it via `/v1/pricing/clearing_price`.

---

## 4. Wallet Integration & EIP‑712

* Define a `PurchaseIntent` typed data schema with fields: `buyer (address)`, `units (uint256)`, `maxPrice (uint256)`, `asset (address)`, `expiry (uint256)`, `slippageBps (uint16)`, `nonce (uint256)`, `chainId (uint256)`.
* In frontend, use the wallet’s `signTypedData` function to create a signature.
* Backend recovers the signer and validates the nonce and domain (chain ID = 8453 for Base).
* Maintain a nonce per buyer to guard against replay.

---

## 5. Config & Environment

* Ensure your `ENV` or `.env` file includes:

  * **Uniswap V2**: `UNISWAP_V2_ROUTER_ADDRESS`, `UNISWAP_V2_FACTORY_ADDRESS` (0x8909…ec6), and `UNISWAP_V2_INIT_CODE_HASH` (0x96e8ac4277…da348845f).
  * **Aerodrome**: `AERODROME_ROUTER_ADDRESS` (0xBE6D8f0d05c…D18a5, Slipstream Router), and `AERODROME_FACTORY_VOLATILE` / `STABLE` (0x420dd381b31aef6683db6b902084cb0ffece40da).
  * **WETH**: `WETH_ADDRESS=0x4200000000000000000000000000000000000006`.
  * **Trade Path**: `TRADE_PATH=0xf4d97f2da56e8c3098f3a8d538db630a2606a024@3000,0x4200000000000000000000000000000000000006@500,0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` to route DIEM via WETH to USDC.
  * **Base RPC**: `BASE_RPC_URL=https://mainnet.base.org`  for reliable chain access.
* Provide a feature flag to toggle the “Market price” display and external API integration.

---

## 6. Risk & Guard Rails

* **Max slippage**: Use `RISK_MAX_SLIPPAGE_BPS` (default 150 bps) to cap allowed slippage for swaps.
* **Pool take**: `RISK_MAX_POOL_TAKE_BPS` (default 25 bps) to avoid draining more than 0.25% of pool reserves.
* **Time to Live**: Force all bids to expire within a configurable limit (e.g., 30 minutes).
* **Volume thresholds**: Reject quotes if reserves or volume on the chosen pools are below safe thresholds.

---

## 7. Testing & Monitoring

* **Unit tests**: signature verification, price calculations (direct vs multi‑hop), acceptance band logic, exact‑out quoting, nonce management.
* **Integration tests**: full bid lifecycle (create, SSE updates, settle); bridging via DEX; key issuance after settlement.
* **End‑to‑end**: user enters units/budget → sees price deviation → places bid → receives updates → settles → key arrives.
* **Telemetry**: expose metrics (bids per status, settlement success rate, clearing price refresh latency) via `/metrics`.
* **Observability**: log signature failures, quote rejections, SSE disconnects, and key issuance latencies.

---
