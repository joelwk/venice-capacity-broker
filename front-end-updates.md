# Control Plane Buyer UI (v1)

The `/admin/buy.html` page now matches the production-ready flow described in the implementation plan.
It ships as a static wizard that guides buyers from quote -> payment -> key delivery without exposing raw JSON.

## Flow summary

1. Step 1 collects the purchase request, fetches `/v1/quotes`, and reveals the quote card with amount, treasury address, USD estimate, countdown pill, and copy helpers.
2. Step 2 stays hidden (and inputs disabled) until a quote is active; once enabled it lets buyers connect a wallet, paste the tx hash, and posts to `/v1/purchases/verify` while showing an inline spinner and status alerts.
3. Step 3 appears after verification, surfaces the scoped API key and expiry, and keeps polling `/v1/purchases/{id}` when issuance is still pending.

Each transition resets alerts and countdowns so back-to-back quotes behave predictably.

## Market snapshot

A sticky sidebar requests `/v1/market/prices` every 45 seconds and renders a comparison table for DIEM, USDC, ETH, and any other assets the backend exposes.
It highlights the asset tied to the latest quote and shows the live discount versus the DIEM fair value when possible.
An empty state explains when market data is unavailable so buyers understand the absence of pricing hints.

## Status and accessibility details

- Alerts use the `.alert-info|success|error` classes so copy successes, countdown expiries, and verification failures surface clearly.
- The Verify button requires both wallet and tx hash to look like valid hex before it unlocks; the button also disables during verification to prevent double posts.
- Copy buttons rely on the async clipboard API with a textarea fallback and emit user-friendly confirmations in the relevant status area.
- Wallet connect hides automatically when `window.ethereum` is missing and shows an informative alert on failure.

## Backend hooks and configurables

The page consumes `/v1/env` to discover the treasury address, supported assets, and feature flags.
Quotes, purchases, and price polling each respect server toggles so the UI degrades gracefully when a feature is disabled.
Formatting choices stay decimals-aware via `assetDecimals`, and the default input is `0.10` DIEM credits until the buyer chooses otherwise.

## Testing references

`tests/ui/test_control_plane_buy_flow.py` walks the full happy path with Playwright: market snapshot renders, quote copy helpers fill the clipboard, verification emits success alerts, and the issued key becomes available with its copy button.
Use `docs/control-plane-smoke-checklist.md` for manual validation after deployments.
