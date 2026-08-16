# Venice Capacity Broker

Self-hosted reseller for unused [Venice AI](https://venice.ai) DIEM. Stake VVV, sell scoped inference keys, and collect USDC, ETH, or WBTC on Base.

Live buyer page: [capacity-broker.replit.app/buy.html](https://capacity-broker.replit.app/buy.html)

## Frontend preview

![Frontend preview](assets/frontendpreview.gif)

## What it does

This is a **capacity broker** you run yourself, not a multi-seller marketplace. Point it at your Venice parent key and a Base treasury, then sell unused daily DIEM as scoped API keys.

Staking VVV earns a daily DIEM allocation (Venice inference credit). Idle credit does not pay you unless you resell it. The broker prices that credit from the live DIEM market, accepts payment on Base, verifies the transfer, and mints a Venice sub-key with `consumptionLimit` (DIEM units) and `expiresAt`. Buyers get API access, not DIEM tokens. Crypto lands in your treasury.

A single-loop orchestrator can keep the inventory behind the storefront funded: StakeMaster (stake and restake VVV), a quorum-gated ArbiDiem trader (mint/sell or buy/burn DIEM vs market), CapacityBroker utilization policy, and AI Treasurer guidance.

## Pricing and discounts

One quote unit is one DIEM. The market engine uses the live DIEM USD price, then applies a utilization markup (`PRICE_UTIL_ALPHA`) so tight inventory costs more.

A per-asset discount then cuts that marked-up price. Defaults in code are 5% for USDC and ETH, 10% for WBTC. Override with `PRICE_DISCOUNT_DEFAULT_BPS` or `PRICE_DISCOUNT_USDC_BPS` / `PRICE_DISCOUNT_ETH_BPS` / `PRICE_DISCOUNT_WBTC_BPS` (100 = 1%). Set an asset to `0` to sell it undiscounted.

Accepted payment assets are `ACCEPT_ASSETS` (default `ETH,USDC,WBTC`). The buy page Market Snapshot shows each asset's USD price, DIEM ratio, and discount.

When utilization is high, CapacityBroker can mark failsafe `hot` and pause new quotes and bids (503). When it is low, it can suggest a discount capped by `BROKER_DISCOUNT_MAX_BPS`.

## Layout

- `apps/broker_api` — public HTTP API and purchase flow
- `apps/control-plane` — static buy page and admin UI
- `apps/cli` — operator CLI (`vvv-agents`)
- `agents/` — StakeMaster, ArbiDiem, CapacityBroker, quorum, reflex
- `services/` — staking, DIEM, market data, keys, risk
- `graph/` — orchestrator loop
- `docs/` — configuration, deployment, operations

## Quick start

Python 3.10+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --extra dev --extra broker
cp .env.example .env
```

Set at least `VENICE_API_KEY` (or `VENICE_PARENT_KEY`), `TREASURY_ADDRESS`, `BASE_RPC_URL`, and `SQL_DATABASE_URL` in `.env` or your host secrets. Never commit `.env` or `.env.local`.

```bash
uv run uvicorn apps.broker_api.app:app --reload --port 8000
uv run pytest tests/test_purchase_security.py tests/test_buyer_e2e.py -q
```

`VENICE_API_BASE_URL` must include `/api/v1` (example: `https://api.venice.ai/api/v1`).

## Deploy

Docker: `docker compose up` (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) and [docs/DOCKER_DEPLOYMENT.md](docs/DOCKER_DEPLOYMENT.md)).

Replit: [docs/REPLIT_DEPLOYMENT.md](docs/REPLIT_DEPLOYMENT.md). Store keys in Replit Secrets, not files.

## Buy flow

The public page is `/` (same as `/admin/buy.html`). Spot and limit share one checkout after a quote exists.

**Spot** is buy-now. `GET /v1/quotes?units=&asset=` returns a live `unitPrice` / `totalPrice` in the pay asset (minor units). The quote is persisted synchronously so verify can find it. Changing DIEM credits scales the total; it does not change the unit price.

**Limit** (`BIDS_ENABLED=1`) is a cap, not a different price. The buyer sets **max unit price** in the pay asset per 1 DIEM (for example `1400` USDC), connects a wallet, and signs EIP-712 `PurchaseIntent`. That signature is not a transfer. `POST /v1/bids` then `POST /v1/settlement/{id}/settle`. If live `unitPrice` is at or under the cap, settle returns the same quote shape as spot. If the cap is below market, settle returns 409 (`price exceeds bid max` or `bid out of band`). The buy page retries a price-exceeds 409 for about 30 seconds, then surfaces the error.

After either path the buyer sends the quoted amount to `payments.treasury_address` on Base, then `GET /v1/purchases/challenge` plus `personal_sign`, then `POST /v1/purchases/verify`. Verify checks chain, confirmations, token, and amount, mints a scoped key, and marks a linked bid `filled`. The paying wallet must match the bid buyer.

Failsafe `hot` (CapacityBroker inventory) returns 503 on new quotes and bids. Unauthenticated status endpoints never return the API key.

Order type and max unit price on the buy page are meant to appear only when `/v1/env` has `features.bids: true`. With bids off, those controls do nothing; use spot.

## License

MIT. See [LICENSE](LICENSE).
