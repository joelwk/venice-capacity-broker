const state = {
  env: null,
  treasury: "",
  quote: null,
  quoteTimer: null,
  verifying: false,
  prices: null,
  purchaseId: null,
  quoteUsdPerDiem: null,
  quoteAsset: null,
  pricesLoading: false,
  lastQuoteLatencyMs: null,
  pricesMeta: null,
  discounts: null,
  lastPricesAt: null,
  inflightPricesPromise: null,
  pricesAbort: null,
};

const assetDecimals = {
  ETH: 18,
  WETH: 18,
  USDC: 6,
  USDT: 6,
  WBTC: 8,
};

const QUOTE_ENDPOINT = "/v1/quotes";
const VERIFY_ENDPOINT = "/v1/purchases/verify";
const PURCHASE_ENDPOINT = "/v1/purchases";
const ENV_ENDPOINT = "/v1/env";
const ENV_AND_PRICES_ENDPOINT = "/v1/env-and-prices?symbols=VVV,DIEM,ETH,USDC,WBTC";
const PRICES_ENDPOINT = "/v1/market/prices?symbols=DIEM,ETH,USDC,WBTC";
const VENICE_API_BASE_URL = "https://api.venice.ai/api/v1";
const TEST_MODELS_ENDPOINT = `${VENICE_API_BASE_URL}/models`;
const TEST_CHAT_ENDPOINT = `${VENICE_API_BASE_URL}/chat/completions`;
const DEFAULT_UNITS = 0.1;
const PRICE_REFRESH_SECONDS = 45;
const MAX_PRICE_STALE_SECONDS = 60;
const PRICING_PRIORITY = ['DIEM', 'USDC', 'ETH', 'WETH', 'WBTC', 'USDT'];
const PRICING_SKELETON_HTML = [
  '<tr class="skeleton-row"><td><span class="skeleton-block skeleton-w-1"></span></td><td><span class="skeleton-block skeleton-w-2"></span></td><td><span class="skeleton-block skeleton-w-3"></span></td><td><span class="skeleton-block skeleton-w-4"></span></td></tr>',
  '<tr class="skeleton-row"><td><span class="skeleton-block skeleton-w-2"></span></td><td><span class="skeleton-block skeleton-w-3"></span></td><td><span class="skeleton-block skeleton-w-4"></span></td><td><span class="skeleton-block skeleton-w-1"></span></td></tr>',
  '<tr class="skeleton-row"><td><span class="skeleton-block skeleton-w-3"></span></td><td><span class="skeleton-block skeleton-w-4"></span></td><td><span class="skeleton-block skeleton-w-1"></span></td><td><span class="skeleton-block skeleton-w-2"></span></td></tr>'
].join('');
const JSON_GET_HEADERS = { Accept: "application/json" };
const JSON_POST_HEADERS = { "Content-Type": "application/json", Accept: "application/json" };
const INIT_WATCHDOG_MS = 4000;
let initWatchdog = setTimeout(() => {
  const empty = document.getElementById("pricing-empty");
  if (empty && (!state.prices || Object.keys(state.prices).length === 0)) {
    empty.textContent = "Market data unavailable.";
  }
}, INIT_WATCHDOG_MS);

function supportsEventSource() {
  return typeof window !== "undefined" && typeof window.EventSource === "function";
}

function $(id) {
  return document.getElementById(id);
}

function showAlert(el, tone, message) {
  if (!el) return;
  const tones = ["alert-info", "alert-success", "alert-error"];
  el.classList.remove("hidden", ...tones);
  const variant = tone === "success" ? "alert-success" : tone === "error" ? "alert-error" : "alert-info";
  el.classList.add(variant);
  el.textContent = message || "";
}

function clearAlert(el) {
  if (!el) return;
  el.textContent = "";
  el.classList.add("hidden");
  el.classList.remove("alert-info", "alert-success", "alert-error");
}

function nowMs() {
  if (typeof performance !== 'undefined' && performance && typeof performance.now === 'function') {
    return performance.now();
  }
  return Date.now();
}

async function fetchWithRetry(url, options = {}, cfg = {}) {
  const { attempts = 5, baseMs = 500, factor = 2, jitter = 0.25, timeoutMs = 5000 } = cfg;
  for (let i = 0; i < attempts; i++) {
    const ac = new AbortController();
    const id = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...options, signal: ac.signal });
      clearTimeout(id);
      if (res.ok) return res;
      if (res.status === 503 && i < attempts - 1) throw new Error('SLA');
      return res; // let caller handle non-OK when not retryable
    } catch (e) {
      clearTimeout(id);
      if (i === attempts - 1) throw e;
      const jitter = 1 + (Math.random() * 2 - 1) * jitter;
      await new Promise(r => setTimeout(r, baseMs * jitter * Math.pow(factor, i)));
    }
  }
}

function formatUsd(value) {
  if (!Number.isFinite(value)) return "--";
  const abs = Math.abs(value);
  if (abs >= 1000) return `\$${value.toFixed(0)}`;
  if (abs >= 1) return `\$${value.toFixed(2)}`;
  return `\$${value.toFixed(4)}`;
}

function formatRatio(value) {
  if (!Number.isFinite(value) || value <= 0) return "--";
  if (value >= 1000) return `${value.toFixed(0)}`;
  if (value >= 10) return `${value.toFixed(2)}`;
  return `${value.toFixed(4)}`;
}

function formatPercent(value) {
  if (!Number.isFinite(value)) return "--";
  const rounded = Math.abs(value).toFixed(2);
  return `${value >= 0 ? '' : '-'}${rounded}%`;
}

function getBaseDiscountPercent(asset) {
  if (!asset) return null;
  const map = state.discounts;
  if (!map || typeof map !== "object") return null;
  const info = map[String(asset).toUpperCase()];
  if (!info || typeof info !== "object") return null;
  const baseBps = Number(info.baseBps ?? info.bps);
  if (Number.isFinite(baseBps)) return baseBps / 100;
  const basePercent = Number(info.basePercent);
  if (Number.isFinite(basePercent)) return basePercent;
  const baseFraction = Number(info.baseFraction ?? info.fraction);
  if (Number.isFinite(baseFraction)) return baseFraction * 100;
  return null;
}

function computeQuoteMetrics() {
  state.quoteUsdPerDiem = null;
  state.quoteAsset = null;
  if (!state.quote) return;
  const discountUsd = Number(state.quote?.discount?.postDiscountUsdPerUnit);
  const discountAsset = String(state.quote?.asset || getSelectedAsset()).toUpperCase();
  if (Number.isFinite(discountUsd) && discountUsd > 0) {
    state.quoteUsdPerDiem = discountUsd;
    state.quoteAsset = discountAsset;
    return;
  }
  const units = Number(state.quote.units ?? state.quote.quantity ?? 0);
  const totalMinor = Number(state.quote.totalPrice ?? state.quote.total_price ?? 0);
  if (!Number.isFinite(units) || units <= 0 || !Number.isFinite(totalMinor) || totalMinor <= 0) {
    return;
  }
  const asset = String(state.quote.asset || getSelectedAsset()).toUpperCase();
  const decimals = assetDecimals[asset] ?? 18;
  const totalAsset = totalMinor / 10 ** decimals;
  if (!Number.isFinite(totalAsset) || totalAsset <= 0) {
    return;
  }
  const assetUsd = state.prices ? Number(state.prices[asset]) : Number.NaN;
  if (!Number.isFinite(assetUsd) || assetUsd <= 0) {
    return;
  }
  const totalUsd = totalAsset * assetUsd;
  if (!Number.isFinite(totalUsd) || totalUsd <= 0) {
    return;
  }
  state.quoteUsdPerDiem = totalUsd / units;
  state.quoteAsset = asset;
}

function renderPricingTable() {
  const table = $("pricing-table");
  const tbody = $("pricing-tbody");
  const empty = $("pricing-empty");
  const note = $("pricing-note");
  if (!table || !tbody || !empty) return;

  const prices = state.prices;
  const hasPrices = prices && Object.keys(prices).length > 0;
  
  // SWR: Show last good prices immediately, even during refresh
  if (state.pricesLoading && hasPrices) {
    table.classList.remove("hidden");
    empty.classList.add("hidden");
    if (note) note.textContent = 'Refreshing market data...';
    // Keep existing table content, just show refresh indicator
    return;
  }
  
  if (state.pricesLoading && !hasPrices) {
    table.classList.remove("hidden");
    empty.classList.add("hidden");
    if (note) note.textContent = 'Fetching live prices...';
    tbody.innerHTML = PRICING_SKELETON_HTML;
    return;
  }
  if (!hasPrices) {
    table.classList.add("hidden");
    tbody.innerHTML = "";
    empty.classList.remove("hidden");
    empty.textContent = "Market data unavailable.";
    if (note) note.textContent = "";
    return;
  }

  const diemUsd = Number(prices.DIEM);
  const assets = Array.from(new Set([...PRICING_PRIORITY, ...Object.keys(prices || {})]));
  const rows = [];
  assets.forEach((asset) => {
    const upper = String(asset).toUpperCase();
    const priceUsd = Number(prices[upper]);
    if (!Number.isFinite(priceUsd) || priceUsd <= 0) return;
    const ratio = Number.isFinite(diemUsd) && diemUsd > 0 ? diemUsd / priceUsd : null;
    const basePercent = getBaseDiscountPercent(upper);
    let discountPercent = null;
    if (upper === state.quoteAsset) {
      const quoteBps = Number(
        (state.quote && state.quote.discount && state.quote.discount.totalBps) ?? state.quote?.discountBps
      );
      if (Number.isFinite(quoteBps)) {
        discountPercent = quoteBps / 100;
      } else if (state.quoteUsdPerDiem && Number.isFinite(diemUsd) && diemUsd > 0) {
        discountPercent = ((diemUsd - state.quoteUsdPerDiem) / diemUsd) * 100;
      }
    }
    if ((discountPercent === null || !Number.isFinite(discountPercent)) && Number.isFinite(basePercent)) {
      discountPercent = basePercent;
    }
    rows.push({
      asset: upper,
      priceUsd,
      ratio,
      discountPercent: Number.isFinite(discountPercent) ? discountPercent : null,
      basePercent: Number.isFinite(basePercent) ? basePercent : null,
      active: upper === state.quoteAsset,
    });
  });

  tbody.innerHTML = rows
    .map(({ asset, priceUsd, ratio, discountPercent, basePercent, active }) => {
      const hasDiscount = discountPercent !== null && Number.isFinite(discountPercent);
      const display = hasDiscount ? formatPercent(discountPercent) : "--";
      const needsHint =
        hasDiscount &&
        active &&
        basePercent !== null &&
        Number.isFinite(basePercent) &&
        Math.abs(discountPercent - basePercent) > 0.05;
      const hint = needsHint ? ` (${formatPercent(basePercent)} base)` : "";
      return `
      <tr class="${active ? "price-row-active" : ""}">
        <td>${asset}</td>
        <td>${formatUsd(priceUsd)}</td>
        <td>${formatRatio(ratio)}</td>
        <td>${display}${hint}</td>
      </tr>`;
    })
    .join("");

  table.classList.toggle("hidden", rows.length === 0);
  empty.classList.toggle("hidden", rows.length > 0);
  empty.textContent = rows.length > 0 ? "" : "Market data unavailable.";

  if (note) {
    if (state.pricesLoading) {
      note.textContent = 'Refreshing market data...';
    } else if (state.quoteUsdPerDiem && state.quoteAsset) {
      const discount = (Number.isFinite(diemUsd) && diemUsd > 0)
        ? ((diemUsd - state.quoteUsdPerDiem) / diemUsd) * 100
        : null;
      const formattedDiscount = discount !== null ? ` (${formatPercent(discount)} vs. market)` : "";
      note.textContent = `Latest quote (${state.quoteAsset}): ${formatUsd(state.quoteUsdPerDiem)} per DIEM${formattedDiscount}.`;
    } else {
      note.textContent = "Generate a quote to compare mint pricing against the market.";
    }
    if (!state.pricesLoading && state.pricesMeta) {
      const metaParts = [];
      const latencyRaw = Number(state.pricesMeta.latency_ms ?? state.pricesMeta.latencyMs);
      if (Number.isFinite(latencyRaw) && latencyRaw > 0) {
        metaParts.push(`last refresh ${Math.round(latencyRaw)} ms`);
      }
      const hitRateRaw = Number(state.pricesMeta.cache_hit_rate ?? state.pricesMeta.cacheHitRate);
      if (Number.isFinite(hitRateRaw) && hitRateRaw > 0 && hitRateRaw <= 1) {
        metaParts.push(`cache hit ${(hitRateRaw * 100).toFixed(1)}%`);
      }
      // Add last updated timestamp
      if (state.lastPricesAt) {
        const ageSeconds = Math.floor((Date.now() - state.lastPricesAt) / 1000);
        metaParts.push(`updated ${ageSeconds}s ago`);
      }
      if (metaParts.length > 0) {
        const metaText = metaParts.join(', ');
        note.textContent = note.textContent ? `${note.textContent} (${metaText})` : metaText;
      }
    }
  }
}


function formatAmount(asset, totalMinor) {
  const decimals = assetDecimals[asset] ?? 18;
  const divisor = 10 ** decimals;
  const value = Number(totalMinor || 0) / divisor;
  if (!Number.isFinite(value)) {
    return {
      value: 0,
      text: "-",
    };
  }
  const digits = decimals > 8 ? 6 : Math.min(6, decimals);
  return {
    value,
    text: `${value.toFixed(digits)} ${asset}`,
  };
}

function computeUsdEstimate(asset, totalMinor, units) {
  const prices = state.prices || {};
  if (asset === "USDC") {
    const usd = Number(totalMinor || 0) / 10 ** (assetDecimals.USDC || 6);
    if (Number.isFinite(usd) && usd > 0) {
      return `~$${usd.toFixed(2)} USD`;
    }
  }
  if (asset === "ETH") {
    const eth = Number(totalMinor || 0) / 10 ** (assetDecimals.ETH || 18);
    const ethUsd = Number(prices.ETH);
    if (Number.isFinite(eth) && Number.isFinite(ethUsd) && ethUsd > 0) {
      return `~$${(eth * ethUsd).toFixed(2)} USD`;
    }
  }
  if (asset === "WBTC") {
    const wbtc = Number(totalMinor || 0) / 10 ** (assetDecimals.WBTC || 8);
    const wbtcUsd = Number(prices.WBTC);
    if (Number.isFinite(wbtc) && Number.isFinite(wbtcUsd) && wbtcUsd > 0) {
      return `~$${(wbtc * wbtcUsd).toFixed(2)} USD`;
    }
  }
  const diemUsd = Number(prices.DIEM);
  if (Number.isFinite(diemUsd) && Number.isFinite(units)) {
    return `~$${(units * diemUsd).toFixed(2)} USD`;
  }
  return "";
}

function isQuoteExpired() {
  if (!state.quote || !state.quote.expiresAt) return true;
  const expiresMs = Number(state.quote.expiresAt) * 1000;
  return Number.isFinite(expiresMs) ? Date.now() >= expiresMs : true;
}

function startQuoteTimer() {
  stopQuoteTimer();
  updateQuoteCountdown();
  state.quoteTimer = setInterval(updateQuoteCountdown, 1000);
}

function stopQuoteTimer() {
  if (state.quoteTimer) {
    clearInterval(state.quoteTimer);
    state.quoteTimer = null;
  }
}

function updateQuoteCountdown() {
  const el = $("quote-countdown");
  if (!el) return;
  if (!state.quote || !state.quote.expiresAt) {
    el.textContent = "Quote expires in --:--";
    return;
  }
  const now = Math.floor(Date.now() / 1000);
  const expires = Number(state.quote.expiresAt) || 0;
  const remaining = Math.max(0, expires - now);
  const minutes = Math.floor(remaining / 60);
  const seconds = remaining % 60;
  el.textContent = remaining <= 0 ? "Quote expired" : `Quote expires in ${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  if (remaining <= 0) {
    enableStep2(false);
    showAlert($("quote-status"), "error", "Quote expired. Refresh for a new quote.");
    stopQuoteTimer();
  }
}

function enableStep2(enable, options = {}) {
  const { keepVisible = false, focus = true } = options || {};
  const card = $("step-verify");
  const verifyBtn = $("verify-btn");
  const walletInput = $("wallet-address");
  const txInput = $("tx-hash");
  const connectBtn = $("connect-wallet");
  if (!card || !verifyBtn) return;
  const shouldHide = !enable && !keepVisible;
  card.classList.toggle("step-hidden", shouldHide);
  card.classList.toggle("step-disabled", !enable);
  if (walletInput) walletInput.disabled = !enable;
  if (txInput) txInput.disabled = !enable;
  if (connectBtn) connectBtn.disabled = !enable;
  if (!enable) {
    verifyBtn.disabled = true;
    return;
  }
  updateVerifyButtonState();
  if (focus) {
    setTimeout(() => walletInput?.focus(), 0);
  }
}

function setStep3Visible(show) {
  const card = $("step-key");
  if (!card) return;
  card.classList.toggle("step-hidden", !show);
}

function updateVerifyButtonState() {
  const verifyBtn = $("verify-btn");
  if (!verifyBtn) return;
  if (!state.quote || isQuoteExpired()) {
    verifyBtn.disabled = true;
    return;
  }
  const walletInput = $("wallet-address");
  const txInput = $("tx-hash");
  const wallet = (walletInput?.value || "").trim();
  const txHash = (txInput?.value || "").trim();
  const walletOk = wallet.startsWith("0x") && wallet.length === 42;
  const hashOk = txHash.startsWith("0x") && txHash.length === 66;
  verifyBtn.disabled = !walletOk || !hashOk || state.verifying;

  if (walletInput) walletInput.setAttribute("aria-invalid", String(!walletOk));
  if (txInput) txInput.setAttribute("aria-invalid", String(!hashOk));
}

function resetStep3() {
  setStep3Visible(false);
  state.purchaseId = null;
  clearAlert($("key-status"));
  const keyInput = $("api-key");
  if (keyInput) keyInput.value = "";
  const expiry = $("key-expiry");
  if (expiry) expiry.textContent = "";
}

function applyQuote(result) {
  if (!result || typeof result !== "object") {
    throw new Error("Quote response missing data.");
  }
  const resolvedAsset = String(result.asset || getSelectedAsset() || "").toUpperCase();
  if (!resolvedAsset) {
    throw new Error("Quote response missing payment asset.");
  }
  const totalMinorRaw = result.totalPrice ?? result.total_price;
  if (totalMinorRaw === undefined || totalMinorRaw === null) {
    throw new Error("Quote response missing total price.");
  }
  const unitsValue = Number(result.units ?? result.quantity ?? 0);
  if (!Number.isFinite(unitsValue) || unitsValue <= 0) {
    throw new Error("Quote response missing DIEM quantity.");
  }
  const sanitized = { ...result, asset: resolvedAsset, units: unitsValue };
  const quoteData = sanitized;
  state.quote = quoteData;
  computeQuoteMetrics();
  renderPricingTable();
  const details = $("quote-details");
  const amountInput = $("quote-amount");
  const addressInput = $("quote-address");
  const usdLine = $("quote-usd");
  const discountLine = $("quote-discount");
  const refreshBtn = $("quote-refresh");
  if (!details || !amountInput || !addressInput) return;

  if (!state.treasury) {
    showAlert($("quote-status"), "error", "Treasury address is not configured on the server.");
    details.classList.add("hidden");
    enableStep2(false);
    return;
  }

  const formatted = formatAmount(resolvedAsset, totalMinorRaw);
  amountInput.value = formatted.text;
  addressInput.value = state.treasury;
  if (usdLine) {
    const usdText = computeUsdEstimate(resolvedAsset, totalMinorRaw, unitsValue);
    usdLine.textContent = usdText || "";
  }
  if (discountLine) {
    discountLine.textContent = "";
    discountLine.classList.add("hidden");
    const discountBps = Number(quoteData.discountBps ?? quoteData.discount?.totalBps);
    if (Number.isFinite(discountBps) && discountBps > 0) {
      const baseBps = Number(quoteData.discount?.baseBps ?? discountBps);
      const reliefBps = Math.max(0, discountBps - baseBps);
      const totalPct = (discountBps / 100).toFixed(2);
      const basePct = (baseBps / 100).toFixed(2);
      let text = `Discount applied: ${totalPct}% off market`;
      if (reliefBps > 0) {
        text += ` (base ${basePct}% + relief ${(reliefBps / 100).toFixed(2)}%)`;
      } else {
        text += ` (base ${basePct}%)`;
      }
      text += ".";
      const marketUsd = Number(quoteData.discount?.marketUsdPerUnit);
      const quoteUsd = Number(quoteData.discount?.postDiscountUsdPerUnit);
      if (Number.isFinite(marketUsd) && marketUsd > 0 && Number.isFinite(quoteUsd) && quoteUsd > 0) {
        text += ` Market ${formatUsd(marketUsd)} → Quote ${formatUsd(quoteUsd)} per DIEM.`;
      }
      discountLine.textContent = text;
      discountLine.classList.remove("hidden");
    }
  }
  details.classList.remove("hidden");
  if (refreshBtn) refreshBtn.hidden = false;
  const latencyValue = Number(state.lastQuoteLatencyMs);
  const latencyText = Number.isFinite(latencyValue) && latencyValue > 0
    ? `Quote ready in ${latencyValue} ms. `
    : '';
  showAlert($("quote-status"), "success", `${latencyText}Send the payment before it expires.`);
  enableStep2(true);
  resetStep3();
  startQuoteTimer();
}

async function requestQuote() {
  const unitsInput = $("quote-units");
  const assetSelect = $("quote-asset");
  const btn = $("quote-btn");
  const refreshBtn = $("quote-refresh");
  const status = $("quote-status");
  const details = $("quote-details");
  if (details) details.classList.add("hidden");
  stopQuoteTimer();
  state.quote = null;
  state.quoteUsdPerDiem = null;
  state.quoteAsset = null;
  const discountLine = $("quote-discount");
  if (discountLine) {
    discountLine.textContent = "";
    discountLine.classList.add("hidden");
  }
  renderPricingTable();
  enableStep2(false);
  resetStep3();
  clearAlert(status);
  clearAlert($("verify-status"));
  state.lastQuoteLatencyMs = null;

  const unitsRaw = unitsInput ? Number(unitsInput.value) : DEFAULT_UNITS;
  const unitsError = $("units-error");
  const assetError = $("asset-error");

  // Clear previous error messages
  if (unitsError) unitsError.classList.add("hidden");
  if (assetError) assetError.classList.add("hidden");

  if (!Number.isFinite(unitsRaw) || unitsRaw <= 0) {
    if (unitsError) {
      unitsError.textContent = "Enter a valid DIEM credit amount (minimum 0.01).";
      unitsError.classList.remove("hidden");
    }
    if (unitsInput) unitsInput.setAttribute("aria-invalid", "true");
    return;
  }

  if (unitsInput) unitsInput.setAttribute("aria-invalid", "false");

  const asset = assetSelect && assetSelect.value ? String(assetSelect.value).toUpperCase() : "USDC";

  if (!asset) {
    if (assetError) {
      assetError.textContent = "Please select a payment asset.";
      assetError.classList.remove("hidden");
    }
    if (assetSelect) assetSelect.setAttribute("aria-invalid", "true");
    return;
  }

  if (assetSelect) assetSelect.setAttribute("aria-invalid", "false");

  const priceSnapshot = state.prices || {};
  const hasPriceData = priceSnapshot && Object.keys(priceSnapshot).length > 0;
  const diemPrice = Number(priceSnapshot.DIEM);
  if (!hasPriceData || !Number.isFinite(diemPrice) || diemPrice <= 0) {
    showAlert(status, "error", "Live market data is unavailable. Refresh the page and try again.");
    return;
  }
  if (asset !== "USDC") {
    const paymentPrice = Number(priceSnapshot[asset]);
    if (!Number.isFinite(paymentPrice) || paymentPrice <= 0) {
      showAlert(status, "error", `No current price for ${asset}. Refresh market data before requesting a quote.`);
      return;
    }
  }

  try {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Getting quote...";
    }
    if (refreshBtn) refreshBtn.disabled = true;

    const params = new URLSearchParams();
    params.set("units", String(unitsRaw));
    params.set("asset", asset);
    const startedAt = nowMs();
    const res = await fetch(`${QUOTE_ENDPOINT}?${params.toString()}`, { headers: JSON_GET_HEADERS });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || "Quote request failed");
    }
    const body = await res.json();
    const latencyMs = Math.round(nowMs() - startedAt);
    state.lastQuoteLatencyMs = latencyMs;
    console.debug(`[quote] fetched in ${latencyMs} ms`);
    applyQuote(body);
    updateVerifyButtonState();
  } catch (err) {
    console.error("[quote] request failed", err);
    showAlert(status, "error", err instanceof Error && err.message ? err.message : "Quote request failed. Please try again.");
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Get Quote";
    }
    if (refreshBtn) refreshBtn.disabled = false;
  }
}

function getSelectedAsset() {
  const assetSelect = $("quote-asset");
  return assetSelect && assetSelect.value ? assetSelect.value : "USDC";
}

function normalizeHex(value) {
  let v = String(value || "").trim();
  if (!v) return "";
  if (!v.startsWith("0x")) {
    v = `0x${v}`;
  }
  return v.toLowerCase();
}

function setVerifying(flag) {
  state.verifying = flag;
  const btn = $("verify-btn");
  const spinner = $("verify-spinner");
  const card = $("step-verify");
  if (btn) btn.disabled = flag;
  if (spinner) spinner.classList.toggle("hidden", !flag);
  if (card) card.setAttribute("aria-busy", String(flag));
}

async function handleVerify() {
  if (!state.quote) {
    showAlert($("verify-status"), "error", "Get a quote before verifying a payment.");
    return;
  }
  if (isQuoteExpired()) {
    showAlert($("verify-status"), "error", "Quote expired. Refresh the quote and try again.");
    enableStep2(false, { keepVisible: true, focus: false });
    return;
  }
  const walletInput = $("wallet-address");
  const txInput = $("tx-hash");
  if (!walletInput || !txInput) return;
  const buyerAddress = normalizeHex(walletInput.value);
  const txHash = normalizeHex(txInput.value);
  const walletErrorEl = $("wallet-error");
  const txErrorEl = $("tx-error");

  // Clear previous error messages
  if (walletErrorEl) walletErrorEl.classList.add("hidden");
  if (txErrorEl) txErrorEl.classList.add("hidden");

  if (buyerAddress.length !== 42) {
    if (walletErrorEl) {
      walletErrorEl.textContent = "Enter a valid wallet address (42 characters starting with 0x).";
      walletErrorEl.classList.remove("hidden");
    }
    walletInput.setAttribute("aria-invalid", "true");
    return;
  }

  if (txHash.length !== 66) {
    if (txErrorEl) {
      txErrorEl.textContent = "Enter a valid transaction hash (66 characters starting with 0x).";
      txErrorEl.classList.remove("hidden");
    }
    txInput.setAttribute("aria-invalid", "true");
    return;
  }

  // Clear invalid states when validation passes
  walletInput.setAttribute("aria-invalid", "false");
  txInput.setAttribute("aria-invalid", "false");

  clearAlert($("verify-status"));
  clearAlert($("key-status"));
  setStep3Visible(false);
  setVerifying(true);
  updateVerifyButtonState();
  showAlert($("verify-status"), "info", "Verifying payment on-chain...");

  try {
    const res = await fetch(VERIFY_ENDPOINT, {
      method: "POST",
      headers: JSON_POST_HEADERS,
      body: JSON.stringify({
        quoteId: state.quote.quoteId,
        txHash,
        buyerAddress,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = body && (body.detail || body.message);
      throw new Error(detail || "Verification failed");
    }
    handleVerifyResponse(body);
  } catch (err) {
    showAlert($("verify-status"), "error", err instanceof Error ? err.message : String(err));
  } finally {
    setVerifying(false);
    updateVerifyButtonState();
  }
}

function handleVerifyResponse(body) {
  const verifyStatus = $("verify-status");
  if (!body) {
    showAlert(verifyStatus, "error", "Verification returned no data.");
    return;
  }
  const subkey = body.subkey;
  const expiresAt = body.expiresAt;
  const purchaseId = body.purchaseId;
  if (subkey) {
    showAlert(verifyStatus, "success", "Payment verified. Copy your API key below.");
    showKey({ subkey, expiresAt });
    return;
  }
  if (purchaseId) {
    state.purchaseId = purchaseId;
    showAlert(verifyStatus, "success", "Payment verified. Issuing your key now.");
    showKey({ status: body.status, purchaseId });
    pollPurchaseUntilReady(purchaseId);
    return;
  }
  showAlert(verifyStatus, "info", "Verification complete. Waiting for key issuance...");
  showKey({ status: body.status, purchaseId: null });
}

function showKey({ subkey, expiresAt, status, purchaseId }) {
  const keyInput = $("api-key");
  const expiry = $("key-expiry");
  const keyStatus = $("key-status");
  setStep3Visible(true);
  if (subkey && keyInput) {
    keyInput.value = subkey;
  }
  if (expiry) {
    expiry.textContent = expiresAt ? formatExpiry(expiresAt) : subkey ? ""
      : "Pending";
  }
  if (subkey) {
    showAlert(keyStatus, "success", "API key issued. Store it in a safe place.");
  } else {
    const baseMessage = "Payment verified. We are issuing your key.";
    const extra = purchaseId ? ` Purchase id: ${purchaseId}.` : "";
    showAlert(keyStatus, "info", baseMessage + extra);
  }
}

function formatExpiry(expiresAt) {
  try {
    const date = new Date(expiresAt);
    if (!Number.isFinite(date.getTime())) return String(expiresAt);
    return date.toLocaleString();
  } catch {
    return String(expiresAt);
  }
}

async function pollPurchaseUntilReady(purchaseId) {
  const keyStatus = $("key-status");
  const streamUrl = `${PURCHASE_ENDPOINT}/${encodeURIComponent(purchaseId)}/stream`;
  if (supportsEventSource()) {
    try {
      const source = new EventSource(streamUrl);
      source.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data || "{}");
          if (payload.subkey) {
            showKey({ subkey: payload.subkey, expiresAt: payload.expiresAt });
            clearAlert(keyStatus);
            showAlert(keyStatus, "success", "API key issued. Store it in a safe place.");
            source.close();
          } else {
            showAlert(keyStatus, "info", `Issuing key... status=${payload.status || "pending"}`);
          }
        } catch {
          showAlert(keyStatus, "info", "Issuing key...");
        }
      };
      source.onerror = () => {
        showAlert(keyStatus, "error", "Status stream interrupted. Refresh this page and keep your purchase id.");
        source.close();
      };
      return;
    } catch (err) {
      showAlert(keyStatus, "error", err instanceof Error ? err.message : String(err));
    }
  }

  let attempt = 0;
  const maxAttempts = 20;

  const poll = async () => {
    attempt += 1;
    try {
      const res = await fetch(`${PURCHASE_ENDPOINT}/${encodeURIComponent(purchaseId)}`, { headers: JSON_GET_HEADERS });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        throw new Error(body && (body.detail || body.message) || res.statusText);
      }
      if (body && body.subkey) {
        showKey({ subkey: body.subkey, expiresAt: body.expiresAt });
        clearAlert(keyStatus);
        showAlert(keyStatus, "success", "API key issued. Store it in a safe place.");
        return;
      }
      showAlert(keyStatus, "info", `Issuing key... status=${body.status || "pending"}`);
    } catch (err) {
      showAlert(keyStatus, "error", err instanceof Error ? err.message : String(err));
      return;
    }

    if (attempt < maxAttempts) {
      setTimeout(poll, 3000);
    } else {
      showAlert(keyStatus, "info", "Still waiting for key issuance. Keep your purchase id handy and try again later.");
    }
  };

  setTimeout(poll, 3000);
}

async function copyToClipboard(value, successMessage, alertEl) {
  if (!value) return;
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(value);
    } else {
      const temp = document.createElement("textarea");
      temp.value = value;
      temp.setAttribute("readonly", "true");
      temp.style.position = "absolute";
      temp.style.opacity = "0";
      document.body.appendChild(temp);
      temp.select();
      document.execCommand("copy");
      document.body.removeChild(temp);
    }
    if (alertEl) {
      showAlert(alertEl, "success", successMessage);
    }
  } catch (err) {
    if (alertEl) {
      showAlert(alertEl, "error", "Copy failed. Copy it manually.");
    }
  }
}


async function connectWallet() {
  const btn = $("connect-wallet");
  const walletField = $("wallet-address");
  if (!window.ethereum || !btn || !walletField) {
    if (btn) btn.classList.add("hidden");
    return;
  }
  try {
    btn.disabled = true;
    btn.textContent = "Connecting...";
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    const account = Array.isArray(accounts) ? accounts[0] : null;
    if (account) {
      walletField.value = account;
      showAlert($("verify-status"), "info", "Wallet connected. Address prefilled.");
    }
  } catch (err) {
    showAlert($("verify-status"), "error", err && err.message ? err.message : "Wallet connection failed.");
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect Wallet";
  }
  updateVerifyButtonState();
}

function applyEnvPayload(payload) {
  const body = payload || {};
  state.env = body;
  const pricingInfo = (body && body.pricing) || {};
  const rawDiscounts = pricingInfo && pricingInfo.discounts;
  if (rawDiscounts && typeof rawDiscounts === "object") {
    const normalized = {};
    Object.entries(rawDiscounts).forEach(([key, info]) => {
      if (!key) return;
      const upper = String(key).toUpperCase();
      if (!upper) return;
      if (!info || typeof info !== "object") return;
      normalized[upper] = info;
    });
    state.discounts = Object.keys(normalized).length > 0 ? normalized : null;
  } else {
    state.discounts = null;
  }
  const payments = (body && body.payments) || {};
  state.treasury = payments.treasury_address || "";
  populateAssets(payments.accepted_assets);
  if (!state.treasury) {
    showAlert($("quote-status"), "error", "Treasury address missing in server config.");
  }
  const feats = (body && body.features) || {};
  if (feats && feats.quotes === false) {
    const btn = $("quote-btn");
    if (btn) btn.disabled = true;
    showAlert($("quote-status"), "error", "Quotes are disabled by the server.");
  }
  if (feats && feats.purchases === false) {
    enableStep2(false, { keepVisible: true });
    showAlert($("verify-status"), "error", "Purchases are disabled by the server.");
  }
}

async function loadEnv() {
  try {
    const res = await fetch(ENV_ENDPOINT, { headers: JSON_GET_HEADERS });
    if (!res.ok) return;
    const body = await res.json();
    applyEnvPayload(body || {});
  } catch {
    showAlert($("quote-status"), "error", "Failed to load server configuration.");
  }
}

async function loadEnvAndPrices() {
  // Single-flight: prevent concurrent calls
  if (state.inflightPricesPromise) {
    return state.inflightPricesPromise;
  }

  state.pricesMeta = null;
  state.pricesLoading = true;
  
  // Abort previous request if any
  if (state.pricesAbort) {
    state.pricesAbort.abort();
  }
  state.pricesAbort = new AbortController();

  const promise = (async () => {
    try {
      const res = await fetchWithRetry(ENV_AND_PRICES_ENDPOINT, { 
        headers: JSON_GET_HEADERS,
        signal: state.pricesAbort.signal 
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "Combined env/prices request failed");
      }
      const body = await res.json();
      const envPayload = body && body.env;
      const pricePayload = body && body.prices;
      if (!envPayload || !pricePayload) {
        throw new Error("Combined payload missing env or prices");
      }
      applyEnvPayload(envPayload || {});
      state.prices = pricePayload || {};
      state.pricesMeta = body && body.meta ? body.meta : null;
      state.lastPricesAt = Date.now();
      computeQuoteMetrics();
      clearAlert($("quote-status"));
      return true;
    } catch (err) {
      state.pricesMeta = null;
      console.warn("Combined env/prices fetch failed", err);
      
      // Check if we have fresh cached data (< 60s old)
      if (state.lastPricesAt && (Date.now() - state.lastPricesAt) < MAX_PRICE_STALE_SECONDS * 1000) {
        console.log("Using cached prices due to fetch failure");
        return true; // Use cached data
      }
      
      showAlert($("quote-status"), "error", "Unable to load market data. Please refresh in a few seconds.");
      return false;
    } finally {
      state.pricesLoading = false;
      state.inflightPricesPromise = null;
      state.pricesAbort = null;
      renderPricingTable();
    }
  })();

  state.inflightPricesPromise = promise;
  return promise;
}


function populateAssets(assets) {
  const select = $("quote-asset");
  if (!select) return;
  select.innerHTML = "";
  const list = Array.isArray(assets) && assets.length ? assets : ["USDC", "ETH"];
  list.forEach((asset) => {
    const opt = document.createElement("option");
    opt.value = String(asset).toUpperCase();
    opt.textContent = String(asset).toUpperCase();
    select.appendChild(opt);
  });
  select.value = list[0].toUpperCase();
}

async function fetchPrices() {
  // Single-flight: prevent concurrent calls
  if (state.inflightPricesPromise) {
    return state.inflightPricesPromise;
  }

  const hadPrices = state.prices && Object.keys(state.prices).length > 0;
  state.pricesLoading = true;
  state.pricesMeta = null;
  
  // Abort previous request if any
  if (state.pricesAbort) {
    state.pricesAbort.abort();
  }
  state.pricesAbort = new AbortController();

  if (!hadPrices) {
    renderPricingTable();
  }

  const promise = (async () => {
    try {
      const res = await fetchWithRetry(PRICES_ENDPOINT, { 
        headers: JSON_GET_HEADERS,
        signal: state.pricesAbort.signal 
      });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const body = await res.json();
      state.prices = (body && body.prices) || {};
      state.lastPricesAt = Date.now();
      clearAlert($("quote-status"));
    } catch (err) {
      if (err) {
        console.warn('Market price fetch failed', err);
      }
      
      // Check if we have fresh cached data (< 60s old)
      if (state.lastPricesAt && (Date.now() - state.lastPricesAt) < MAX_PRICE_STALE_SECONDS * 1000) {
        console.log("Using cached prices due to fetch failure");
        return; // Use cached data
      }
      
      if (!hadPrices) {
        showAlert($("quote-status"), "error", "Unable to refresh market data. Trying again soon.");
      }
    } finally {
      state.pricesLoading = false;
      state.inflightPricesPromise = null;
      state.pricesAbort = null;
      computeQuoteMetrics();
      renderPricingTable();
    }
  })();

  state.inflightPricesPromise = promise;
  return promise;
}

function schedulePriceRefresh() {
  setInterval(fetchPrices, PRICE_REFRESH_SECONDS * 1000);
}

function normalizeModelList(payload) {
  if (!payload) return [];
  if (Array.isArray(payload.data)) return payload.data;
  if (payload.data && Array.isArray(payload.data.models)) return payload.data.models;
  if (payload.data && Array.isArray(payload.data.items)) return payload.data.items;
  if (payload.data && Array.isArray(payload.data.data)) return payload.data.data;
  if (Array.isArray(payload.models)) return payload.models;
  if (Array.isArray(payload.results)) return payload.results;
  if (Array.isArray(payload.items)) return payload.items;
  if (Array.isArray(payload)) return payload;
  return [];
}

function pickChatModel(payload) {
  const list = normalizeModelList(payload);
  let fallback = null;
  for (const entry of list) {
    if (!entry || typeof entry !== "object") continue;
    const id = entry.id || entry.name || entry.model;
    if (!id) continue;
    if (!fallback) fallback = id;
    const caps = entry.capabilities || entry.modes || entry.tags || entry.features || entry.supports;
    if (Array.isArray(caps) && caps.some((cap) => typeof cap === "string" && cap.toLowerCase().includes("chat"))) {
      return { id, supportsChat: true };
    }
    const type = entry.type || entry.category || entry.kind;
    if (typeof type === "string" && type.toLowerCase().includes("chat")) {
      return { id, supportsChat: true };
    }
  }
  return { id: fallback, supportsChat: false };
}

function messageContentToString(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content.map((item) => {
      if (typeof item === "string") return item;
      if (item && typeof item === "object") {
        return messageContentToString(item.text || item.content || item.value || item.message || item);
      }
      return "";
    }).filter(Boolean).join(" ");
  }
  if (content && typeof content === "object") {
    if (typeof content.text === "string") return content.text;
    if (typeof content.content === "string") return content.content;
    if (Array.isArray(content.parts)) return messageContentToString(content.parts);
  }
  return "";
}

function extractChatReply(body) {
  if (!body || typeof body !== "object") return null;
  const choices = Array.isArray(body.choices) ? body.choices : [];
  for (const choice of choices) {
    if (!choice) continue;
    const message = choice.message || choice.delta || {};
    const text = messageContentToString(message.content || message.text || choice.text || "");
    if (text) return text;
  }
  const output = body.output || body.result || body.message;
  const text = messageContentToString(output);
  return text || null;
}

async function testApiKey() {
  const keyField = $("test-api-key");
  const promptField = $("test-api-prompt");
  const statusEl = $("test-api-status");
  const outputEl = $("test-api-output");
  const triggerBtn = $("test-api-btn");
  if (!keyField || !statusEl || !outputEl) return;

  const apiKey = (keyField.value || "").trim();
  const promptRaw = (promptField?.value || "").trim();

  statusEl.classList.remove("error");
  statusEl.textContent = "";
  outputEl.textContent = "";
  outputEl.classList.add("hidden");

  if (triggerBtn) {
    triggerBtn.disabled = true;
    triggerBtn.textContent = "Running test...";
  }

  if (!apiKey) {
    statusEl.textContent = "Enter an API key to run the test.";
    statusEl.classList.add("error");
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = "Run test call";
    }
    return;
  }

  const results = {};
  statusEl.textContent = "Checking your key...";

  try {
    const modelsRes = await fetch(TEST_MODELS_ENDPOINT, {
      headers: {
        Authorization: `Bearer ${apiKey}`,
        Accept: "application/json",
      },
    });
    const modelsBody = await modelsRes.json().catch(() => ({}));
    if (!modelsRes.ok) {
      const detail = modelsBody && (modelsBody.detail || modelsBody.message);
      throw new Error(detail || `Key check failed (${modelsRes.status})`);
    }

    const normalized = normalizeModelList(modelsBody)
      .map((entry) => ({
        id: entry && (entry.id || entry.name || entry.model) || null,
        type: entry && (entry.type || entry.category || entry.kind || null),
      }))
      .filter((item) => Boolean(item.id));
    results.models = normalized.slice(0, 5);

    const sampleNames = results.models.map((item) => item.id).join(", ");
    statusEl.classList.remove("error");
    statusEl.textContent = sampleNames
      ? `Key works. Sample models: ${sampleNames}.`
      : "Key works. Models endpoint responded.";

    const { id: chatModelId } = pickChatModel(modelsBody);
    if (!chatModelId) {
      statusEl.textContent += " No chat model detected. Grab one from the docs and try again.";
      outputEl.textContent = JSON.stringify(results, null, 2);
      outputEl.classList.remove("hidden");
      return;
    }

    const prompt = promptRaw || "Say hi to me in one friendly sentence.";
    statusEl.textContent = `Key works. Sending your prompt with ${chatModelId}...`;

    const chatRes = await fetch(TEST_CHAT_ENDPOINT, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${apiKey}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
      body: JSON.stringify({
        model: chatModelId,
        messages: [
          { role: "system", content: "You are a helpful Venice assistant." },
          { role: "user", content: prompt },
        ],
      }),
    });
    const chatBody = await chatRes.json().catch(() => ({}));
    if (!chatRes.ok) {
      const detail = chatBody && (chatBody.detail || chatBody.message);
      throw new Error(detail || `Chat call failed (${chatRes.status})`);
    }

    const reply = extractChatReply(chatBody);
    results.chat = {
      model: chatModelId,
      prompt,
      reply: reply || null,
    };
    if (!reply) {
      results.chat.raw = chatBody;
    }

    statusEl.textContent = `Success. ${chatModelId} replied below.`;
    outputEl.textContent = JSON.stringify(results, null, 2);
    outputEl.classList.remove("hidden");
  } catch (err) {
    statusEl.textContent = err instanceof Error ? err.message : String(err);
    statusEl.classList.add("error");
    if (Object.keys(results).length > 0) {
      outputEl.textContent = JSON.stringify(results, null, 2);
      outputEl.classList.remove("hidden");
    }
  } finally {
    if (triggerBtn) {
      triggerBtn.disabled = false;
      triggerBtn.textContent = "Run test call";
    }
  }
}

function setupEventHandlers() {
  const quoteBtn = $("quote-btn");
  if (quoteBtn) quoteBtn.addEventListener("click", requestQuote);
  const refreshBtn = $("quote-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", requestQuote);
  const verifyBtn = $("verify-btn");
  if (verifyBtn) verifyBtn.addEventListener("click", handleVerify);
  const walletField = $("wallet-address");
  if (walletField) walletField.addEventListener("input", updateVerifyButtonState);
  const txField = $("tx-hash");
  if (txField) txField.addEventListener("input", updateVerifyButtonState);
  const connectBtn = $("connect-wallet");
  if (connectBtn) {
    if (!window.ethereum) {
      connectBtn.classList.add("hidden");
    } else {
      connectBtn.addEventListener("click", connectWallet);
    }
  }
  const copyAmount = $("copy-amount");
  if (copyAmount) {
    copyAmount.addEventListener("click", async () => {
      const value = $("quote-amount")?.value || "";
      if (value) {
        await copyToClipboard(value, "Amount copied to clipboard.", $("quote-status"));
      }
    });
  }
  const copyAddress = $("copy-address");
  if (copyAddress) {
    copyAddress.addEventListener("click", async () => {
      const value = state.treasury || $("quote-address")?.value || "";
      if (value) {
        await copyToClipboard(value, "Treasury address copied.", $("quote-status"));
      }
    });
  }
  const copyKey = $("copy-key");
  if (copyKey) {
    copyKey.addEventListener("click", async () => {
      const value = $("api-key")?.value || "";
      if (value) {
        await copyToClipboard(value, "API key copied to clipboard.", $("key-status"));
      }
    });
  }
  const testBtn = $("test-api-btn");
  if (testBtn) {
    testBtn.addEventListener("click", testApiKey);
  }
}

function initDefaults() {
  const unitsInput = $("quote-units");
  if (unitsInput && !unitsInput.value) {
    unitsInput.value = String(DEFAULT_UNITS);
  }
}

async function init() {
  clearTimeout(initWatchdog);
  initDefaults();
  setupEventHandlers();
  enableStep2(false);
  state.pricesLoading = true;
  renderPricingTable();
  const combinedLoaded = await loadEnvAndPrices();
  if (!combinedLoaded) {
    await Promise.all([loadEnv(), fetchPrices()]);
  }
  schedulePriceRefresh();
  updateVerifyButtonState();
}

init();















