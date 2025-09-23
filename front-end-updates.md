This outline uses Bootstrap for quick styling and vanilla JS for logic. You can adapt it to Tailwind or React if preferred. The key points are: hide steps until needed, show timers and spinners, provide copy buttons, and present the API key cleanly. The same back‑end endpoints you already have (`/admin/api/new_quote`, `/admin/api/verify`) can be reused.

---

## Key improvements for a better user experience

* **Wizard‑style flow** – separate the steps (get quote, send payment, verify & receive key) into clearly labelled sections that hide the next step until the current one is complete.
* **Clean, friendly layout** – group related items together in cards with plenty of whitespace, descriptive headings and clear instructions.
* **Explicit status indicators** – show timers for quote expiry, spinners when verifying, and coloured success/error messages rather than raw JSON.
* **Clear data presentation** – show the API key and expiry date separately with copy‑buttons; no need to expose internal IDs or a full JSON blob.
* **Accessible buttons and inputs** – use clearly labelled buttons for copying amounts/addresses and verifying the transaction; disable the “Verify” button until a TX hash is entered.

---

## Example wireframe

Below is a high‑level mock‑up of how the flow could be organised (colours and exact spacing can be adjusted to match your branding):

```
Buy DIEM Credits

Card: Step 1 – Payment Details
• Instructional text explaining the purchase.
• Display the amount (0.000495 ETH) with “Copy Amount” button.
• Display the destination address with “Copy Address” button.
• Show approximate USD value and a live countdown (“Expires in 14:59”).
• A “Refresh Quote” button if the quote expires.

Card: Step 2 – Confirm Transaction   (initially collapsed/disabled)
• Instruction: “After sending payment, paste your transaction hash here.”
• TX hash input field.
• “Verify Payment” button (disabled until input has value).
• Spinner overlay while verifying.
• On error, show a red alert: “Couldn’t verify transaction. Please check the hash.”

Card: Step 3 – Receive API Key   (hidden until verified)
• Success message with a green checkmark.
• Show the API key in an input box with a “Copy Key” button.
• Display the key’s expiry date.
• Helpful note: “Store this key securely; it grants access to your DIEM credits.”
```

---

## Implementation outline

Assuming you’re using a simple Flask/FastAPI back‑end with vanilla JS or Bootstrap (as in the current repo), you can implement the above flow without a framework:

### HTML structure (buy.html)

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Buy DIEM Credits</title>
  <link rel="stylesheet" href="/static/bootstrap.min.css">
  <style>
    body { background-color: #0d1117; color: #c9d1d9; }
    .card { background-color: #161b22; border-color: #30363d; }
    .disabled-card { opacity: 0.5; pointer-events: none; }
  </style>
</head>
<body>
  <div class="container py-4">
    <h1 class="text-center mb-4">Purchase DIEM Credits</h1>

    <!-- Step 1 -->
    <div id="step1-card" class="card mb-3">
      <div class="card-header fw-bold">Step 1: Payment Details</div>
      <div class="card-body">
        <p>Send the following amount to the address below. This quote expires in <span id="quote-countdown">…</span>.</p>
        <div class="row">
          <div class="col-md-6 mb-3">
            <label class="form-label">Amount</label>
            <div class="input-group">
              <input id="amount" class="form-control" readonly>
              <button class="btn btn-outline-secondary" id="copy-amount">Copy</button>
            </div>
          </div>
          <div class="col-md-6 mb-3">
            <label class="form-label">Payment Address</label>
            <div class="input-group">
              <input id="address" class="form-control" readonly>
              <button class="btn btn-outline-secondary" id="copy-address">Copy</button>
            </div>
          </div>
        </div>
        <button class="btn btn-secondary" id="refresh-quote">Refresh Quote</button>
      </div>
    </div>

    <!-- Step 2 -->
    <div id="step2-card" class="card mb-3 disabled-card">
      <div class="card-header fw-bold">Step 2: Confirm Transaction</div>
      <div class="card-body">
        <p>After sending your payment, paste the transaction hash.</p>
        <input id="txhash" class="form-control mb-3" placeholder="Transaction hash" disabled>
        <button class="btn btn-primary" id="verify-btn" disabled>Verify Payment</button>
        <div id="verify-spinner" class="spinner-border text-primary ms-2 d-none"></div>
        <div id="verify-error" class="alert alert-danger mt-2 d-none"></div>
      </div>
    </div>

    <!-- Step 3 -->
    <div id="step3-card" class="card mb-3 d-none">
      <div class="card-header fw-bold">Step 3: Your API Key</div>
      <div class="card-body">
        <p>Payment verified. Your DIEM credits are ready!</p>
        <label class="form-label">API Key</label>
        <div class="input-group mb-2">
          <input id="api-key" class="form-control" readonly>
          <button class="btn btn-outline-secondary" id="copy-key">Copy Key</button>
        </div>
        <small class="text-muted">Expires on <span id="expires-at"></span></small>
      </div>
    </div>
  </div>

  <script src="/static/buy.js"></script>
</body>
</html>
```

### JavaScript logic (buy.js)

```js
let currentQuote = null;
const step2Card  = document.getElementById("step2-card");
const step3Card  = document.getElementById("step3-card");

async function fetchQuote() {
  const res = await fetch("/admin/api/new_quote");
  currentQuote = await res.json();  // {quoteId, ethAmount, toAddress, expiresAt}
  document.getElementById("amount").value = `${currentQuote.ethAmount} ETH`;
  document.getElementById("address").value = currentQuote.toAddress;
  startCountdown(new Date(currentQuote.expiresAt));
  enableStep2(false);
}

// Countdown display
function startCountdown(expiry) {
  const countdownEl = document.getElementById("quote-countdown");
  const interval = setInterval(() => {
    const secs = Math.max(0, Math.floor((expiry - new Date()) / 1000));
    countdownEl.textContent = `${Math.floor(secs/60)}:${String(secs%60).padStart(2,"0")}`;
    if (secs === 0) {
      clearInterval(interval);
      enableStep2(false); // disable next step
    }
  }, 1000);
}

function enableStep2(enable) {
  step2Card.classList.toggle("disabled-card", !enable);
  document.getElementById("txhash").disabled = !enable;
  document.getElementById("verify-btn").disabled = !enable;
}

document.getElementById("refresh-quote").addEventListener("click", fetchQuote);
document.getElementById("copy-amount").addEventListener("click", () =>
  navigator.clipboard.writeText(currentQuote.ethAmount));
document.getElementById("copy-address").addEventListener("click", () =>
  navigator.clipboard.writeText(currentQuote.toAddress));

document.getElementById("verify-btn").addEventListener("click", async () => {
  const txHash = document.getElementById("txhash").value.trim();
  if (!txHash) return;
  document.getElementById("verify-spinner").classList.remove("d-none");
  const res = await fetch("/admin/api/verify", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ quoteId: currentQuote.quoteId, txHash })
  });
  document.getElementById("verify-spinner").classList.add("d-none");
  if (res.ok) {
    const { apiKey, expiresAt } = await res.json();
    showKey(apiKey, expiresAt);
  } else {
    const err = await res.json();
    document.getElementById("verify-error").textContent = err.message || "Verification failed";
    document.getElementById("verify-error").classList.remove("d-none");
  }
});

function showKey(key, expiresAt) {
  step2Card.classList.add("d-none");
  step3Card.classList.remove("d-none");
  document.getElementById("api-key").value = key;
  document.getElementById("expires-at").textContent =
    new Date(expiresAt).toLocaleString();
}
fetchQuote();
```