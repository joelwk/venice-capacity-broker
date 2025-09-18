# Control Plane Manual Smoke Checklist

Use this list when validating the /admin/buy view after deployments.

1. Load `buy.html` from the running broker API and confirm wallet status renders before interaction.
2. Connect a wallet (Metamask or Coinbase Wallet), then verify the status banner reflects the connected address.
3. Request a DIEM quote and confirm the summary shows the treasury address, accepted asset, and a ticking TTL countdown.
4. Click "Continue to Pay & Verify", then exercise the copy address and copy amount buttons while watching the inline status text update.
5. Submit a known-good transaction hash, wait for "Key issued" to appear, and ensure the issued key is visible and copyable.
6. Optional: replay with an expired quote to confirm the UI blocks verification until a fresh quote is generated.
