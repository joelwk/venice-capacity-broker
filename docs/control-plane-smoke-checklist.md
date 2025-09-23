# Control Plane Manual Smoke Checklist

Use this list when validating the /admin/buy view after deployments.

1. Load `buy.html` from the running broker API and confirm the Market Snapshot renders, Step 1 is active, and Steps 2/3 remain hidden.
2. Request a DIEM quote and verify the amount/address fields populate with copy helpers, the USD estimate appears, and the countdown starts.
3. Confirm the quote-expiry pill decrements in real time and the Refresh button is available once the timer reaches zero.
4. After the quote arrives, ensure Step 2 unlocks the wallet + tx hash inputs (Connect Wallet stays disabled until then) and the Verify button remains disabled until both fields look valid.
5. Submit a known-good transaction hash, watch the spinner and info alert while verification runs, then confirm Step 3 appears with the success checkmark, key value, copy button, and expiry timestamp.
6. Optional: let a quote expire without verifying to confirm Step 2 disables and hides again, and the UI flashes the “Quote expired” alert.
