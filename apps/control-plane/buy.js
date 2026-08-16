// Version: 2026-01-10-visibility-polling-cache-aware
// Adds visibility-aware polling, cache-friendly market calls, and diff-based pricing renders.

// Log script load
console.log("[buy.js] Script loaded - Version: 2026-01-04-fast-init-with-background-prices");

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
  diemPriceSnapshot: null,
  lastDiemSnapshotAt: null,
  quoteAbort: null,
  activeQuoteRequestId: 0,
  quoteWarmupTimer: null,
  quoteWarmupAttempts: 0,
  recovering: false,
  recoverPanelVisible: false,
  recoverPrompted: false,
  lastHiddenAt: null,
  resumeFastUntil: null,
  pendingDiemSnapshot: false,
  pendingDiemSnapshotReason: null,
  hiddenTabSuppressions: 0,
  pendingHiddenSuppressions: 0,
};

const assetDecimals = {
  ETH: 18,
  WETH: 18,
  USDC: 6,
  USDT: 6,
  WBTC: 8,
};

const QUOTE_ENDPOINT = "/v1/quotes";
const BIDS_ENDPOINT = "/v1/bids";
const SETTLE_ENDPOINT = "/v1/settlement";
const VERIFY_ENDPOINT = "/v1/purchases/verify";
const PURCHASE_ENDPOINT = "/v1/purchases";
const CHALLENGE_ENDPOINT = "/v1/purchases/challenge";
const RECOVER_ENDPOINT = "/v1/purchases/recover";
const ENV_ENDPOINT = "/v1/env";
const ENV_AND_PRICES_BASE = "/v1/env-and-prices?symbols=DIEM,VVV,ETH,USDC,WBTC";
const ENV_AND_PRICES_ENDPOINT = ENV_AND_PRICES_BASE;
const PRICES_BASE = "/v1/market/prices?symbols=DIEM,VVV,ETH,USDC,WBTC";
const PRICES_ENDPOINT = PRICES_BASE;
const DIEM_PRICE_ENDPOINT = "/v1/market/diem";
const VENICE_API_BASE_URL = "https://api.venice.ai/api/v1";
const TEST_MODELS_ENDPOINT = `${VENICE_API_BASE_URL}/models`;
const TEST_CHAT_ENDPOINT = `${VENICE_API_BASE_URL}/chat/completions`;
const DEFAULT_UNITS = 0.1;
const PRICE_POLL_DEFAULT_MS = 60000;
const PRICE_POLL_ACTIVE_MS = 30000;
const PRICE_POLL_SLOW_MS = 90000;
const PRICE_POLL_RESUME_FAST_THRESHOLD_MS = 120000;
const MAX_PRICE_STALE_SECONDS = 60;
const PRICING_PRIORITY = ['DIEM', 'VVV', 'USDC', 'ETH', 'WETH', 'WBTC', 'USDT'];
const PRICING_SKELETON_HTML = [
  '<tr class="skeleton-row"><td><span class="skeleton-block skeleton-w-1"></span></td><td><span class="skeleton-block skeleton-w-2"></span></td><td><span class="skeleton-block skeleton-w-3"></span></td><td><span class="skeleton-block skeleton-w-4"></span></td></tr>',
  '<tr class="skeleton-row"><td><span class="skeleton-block skeleton-w-2"></span></td><td><span class="skeleton-block skeleton-w-3"></span></td><td><span class="skeleton-block skeleton-w-4"></span></td><td><span class="skeleton-block skeleton-w-1"></span></td></tr>',
  '<tr class="skeleton-row"><td><span class="skeleton-block skeleton-w-3"></span></td><td><span class="skeleton-block skeleton-w-4"></span></td><td><span class="skeleton-block skeleton-w-1"></span></td><td><span class="skeleton-block skeleton-w-2"></span></td></tr>'
].join('');
const JSON_GET_HEADERS = { Accept: "application/json" };
const JSON_POST_HEADERS = { "Content-Type": "application/json", Accept: "application/json" };
const INIT_WATCHDOG_MS = 60000; // Increased from 4s to 60s to match expected load times
const DIEM_SNAPSHOT_DEBUG = (() => {
  try {
    const params = new URLSearchParams(window.location.search);
    const raw = params.get("diemSnapshot");
    if (!raw) return false;
    return ["1", "true", "yes", "on", "debug"].includes(raw.toLowerCase());
  } catch (_) {
    return false;
  }
})();

const pricingRowCache = new Map();
let pricingSkeletonVisible = false;
let pricesTimer = null;
let initWatchdog = setTimeout(() => {
  const empty = document.getElementById("pricing-empty");
  if (empty && (!state.prices || Object.keys(state.prices).length === 0)) {
    empty.textContent = "Market data unavailable. Click Retry to refresh.";
    // Show retry button if watchdog fires
    const retryContainer = document.getElementById("pricing-retry");
    if (retryContainer) {
      retryContainer.classList.remove("hidden");
    }
  }
}, INIT_WATCHDOG_MS);

function supportsEventSource() {
  return typeof window !== "undefined" && typeof window.EventSource === "function";
}

function $(id) {
  return document.getElementById(id);
}

function quotesEnabled() {
  const features = (state.env && state.env.features) || {};
  return features.quotes !== false;
}

function bidsEnabled() {
  const features = (state.env && state.env.features) || {};
  return features.bids === true;
}

function isLimitBidMode() {
  const mode = $("quote-mode");
  return bidsEnabled() && !!mode && mode.value === "limit";
}

function quoteActionLabel() {
  return isLimitBidMode() ? "Place Bid" : "Get Quote";
}

function syncQuoteModeUi() {
  const wrap = $("quote-mode-wrap");
  const limitFields = $("quote-limit-fields");
  const mode = $("quote-mode");
  const enabled = bidsEnabled();
  if (wrap) wrap.hidden = !enabled;
  if (limitFields) {
    limitFields.hidden = !enabled || !mode || mode.value !== "limit";
  }
  const btn = $("quote-btn");
  if (btn && !state.quote) {
    btn.textContent = quoteActionLabel();
  }
}

function getDiemPriceUsd() {
  const direct = Number(state.prices && state.prices.DIEM);
  if (Number.isFinite(direct) && direct > 0) {
    return direct;
  }
  const snapshot = Number(state.diemPriceSnapshot && state.diemPriceSnapshot.priceUsd);
  if (Number.isFinite(snapshot) && snapshot > 0) {
    return snapshot;
  }
  return null;
}

function getAssetPriceUsd(asset) {
  const upper = String(asset || "").toUpperCase();
  if (upper === "DIEM") {
    const diem = getDiemPriceUsd();
    return diem !== null ? diem : null;
  }
  const prices = state.prices || {};
  const value = Number(prices[upper]);
  if (Number.isFinite(value) && value > 0) {
    return value;
  }
  return null;
}

// Centralized quote button state management - eliminates redundancy
function setQuoteButtonState(enabled, text = null) {
  const btn = $("quote-btn");
  if (!btn) return;
  btn.disabled = !enabled;
  if (text !== null) {
    btn.textContent = text;
  }
}

function showAlert(el, tone, message) {
  if (!el) {
    console.warn("[showAlert] Element not found, cannot display alert:", message);
    return;
  }
  const tones = ["alert-info", "alert-success", "alert-error"];
  el.classList.remove("hidden", ...tones);
  const variant = tone === "success" ? "alert-success" : tone === "error" ? "alert-error" : "alert-info";
  el.classList.add(variant);
  el.textContent = message || "";
  // Ensure the element is visible (in case CSS or other factors hide it)
  if (el.classList.contains("hidden")) {
    console.warn("[showAlert] Alert element still has hidden class after removal, forcing visibility");
    el.classList.remove("hidden");
  }
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
  // Tune timeouts: prices need longer due to cold-start RPC warmup (~30s on Replit)
  const defaultTimeout = url.includes('/env-and-prices') || url.includes('/market/prices') 
    ? 60000  // 60s for market data (covers cold-start + rate-limit backoff)
    : url.includes('/quotes') 
      ? 20000  // 20s for quotes (increased for warmup timeouts)
      : 5000;  // 5s for other calls
  // Quotes rely on backend warm-up gating; avoid redundant retries here.
  const defaultAttempts = url.includes('/quotes') ? 1 : 5;
  const { attempts = defaultAttempts, baseMs = 400, factor = 2, jitter = 0.25, timeoutMs = cfg.timeoutMs || defaultTimeout } = cfg;
  for (let i = 0; i < attempts; i++) {
    const ac = new AbortController();
    // If caller supplied a signal, forward its abort to our controller so external aborts cancel retries too
    const externalSignal = options && options.signal;
    if (externalSignal) {
      if (externalSignal.aborted) {
        throw new DOMException('Aborted', 'AbortError');
      }
      try {
        externalSignal.addEventListener('abort', () => ac.abort(), { once: true });
      } catch (_) {}
    }
    const id = setTimeout(() => ac.abort(), timeoutMs);
    try {
      const res = await fetch(url, { ...options, signal: ac.signal });
      clearTimeout(id);
      if (res.ok) return res;
      // Retry 503 errors (service unavailable, warmup issues)
      if (res.status === 503 && i < attempts - 1) {
        console.log(`[fetchWithRetry] 503 error on attempt ${i + 1}/${attempts}, retrying...`);
        throw new Error('SLA');
      }
      return res; // let caller handle non-OK when not retryable
    } catch (e) {
      clearTimeout(id);
      if (i === attempts - 1) throw e;
      const jitterFactor = 1 + (Math.random() * 2 - 1) * jitter;
      const delay = baseMs * jitterFactor * Math.pow(factor, i);
      console.log(`[fetchWithRetry] Retry ${i + 1}/${attempts - 1} after ${delay.toFixed(0)}ms`);
      await new Promise(r => setTimeout(r, delay));
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

// Convert heterogeneous error payloads (strings, arrays, FastAPI detail objects) into a readable string
function normalizeErrorMessage(raw) {
  if (raw instanceof Error) return raw.message || raw.name || "Unknown error";
  if (raw === null || raw === undefined) return "Unknown error";
  if (typeof raw === "string") return raw;
  if (Array.isArray(raw)) {
    const parts = raw.map((item) => normalizeErrorMessage(item)).filter(Boolean);
    return parts.join("; ") || "Unknown error";
  }
  if (typeof raw === "object") {
    if (typeof raw.message === "string") return raw.message;
    if (typeof raw.detail === "string") return raw.detail;
    if (Array.isArray(raw.detail)) return normalizeErrorMessage(raw.detail);
    if (typeof raw.error === "string") return raw.error;
    if (typeof raw.reason === "string") return raw.reason;
    if (typeof raw.msg === "string") return raw.msg;
    try {
      const compact = JSON.stringify(raw);
      if (compact && compact !== "{}") return compact;
    } catch (_) {
      /* ignore */
    }
  }
  return String(raw);
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
  // Use decimals from quote response if available
  const quoteDecimals = Number(state.quote.discount?.decimals ?? state.quote.decimals);
  const decimals = Number.isFinite(quoteDecimals) && quoteDecimals > 0 
    ? quoteDecimals 
    : (assetDecimals[asset] ?? 18);
  const totalAsset = totalMinor / 10 ** decimals;
  if (!Number.isFinite(totalAsset) || totalAsset <= 0) {
    return;
  }
  const assetUsd = getAssetPriceUsd(asset);
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

function resetPricingRows() {
  pricingRowCache.clear();
}

function createPricingRow(asset) {
  const row = document.createElement("tr");
  const assetCell = document.createElement("td");
  const priceCell = document.createElement("td");
  const ratioCell = document.createElement("td");
  const discountCell = document.createElement("td");
  row.appendChild(assetCell);
  row.appendChild(priceCell);
  row.appendChild(ratioCell);
  row.appendChild(discountCell);
  const entry = {
    el: row,
    assetCell,
    priceCell,
    ratioCell,
    discountCell,
    last: {},
  };
  assetCell.textContent = asset;
  entry.last.asset = asset;
  return entry;
}

function updatePricingRow(entry, data) {
  const priceText = formatUsd(data.priceUsd);
  const ratioText = formatRatio(data.ratio);
  const hasDiscount = data.discountPercent !== null && Number.isFinite(data.discountPercent);
  const display = hasDiscount ? formatPercent(data.discountPercent) : "--";
  const needsHint =
    hasDiscount &&
    data.active &&
    data.basePercent !== null &&
    Number.isFinite(data.basePercent) &&
    Math.abs(data.discountPercent - data.basePercent) > 0.05;
  const hint = needsHint ? ` (${formatPercent(data.basePercent)} base)` : "";
  const discountText = `${display}${hint}`;

  if (entry.last.asset !== data.asset) {
    entry.assetCell.textContent = data.asset;
    entry.last.asset = data.asset;
  }
  if (entry.last.priceText !== priceText) {
    entry.priceCell.textContent = priceText;
    entry.last.priceText = priceText;
  }
  if (entry.last.ratioText !== ratioText) {
    entry.ratioCell.textContent = ratioText;
    entry.last.ratioText = ratioText;
  }
  if (entry.last.discountText !== discountText) {
    entry.discountCell.textContent = discountText;
    entry.last.discountText = discountText;
  }
  if (entry.last.active !== data.active) {
    entry.el.classList.toggle("price-row-active", data.active);
    entry.last.active = data.active;
  }
}

function renderPricingTable() {
  const table = $("pricing-table");
  const tbody = $("pricing-tbody");
  const empty = $("pricing-empty");
  const note = $("pricing-note");
  const retryContainer = $("pricing-retry");
  if (!table || !tbody || !empty) return;

  const prices = state.prices || {};
  const priceKeys = Object.keys(prices);
  const diemUsd = getDiemPriceUsd();
  const hasPrices = priceKeys.length > 0 || (Number.isFinite(diemUsd) && diemUsd > 0);
  
  // SWR: Show last good prices immediately, even during refresh
  if (state.pricesLoading && hasPrices) {
    table.classList.remove("hidden");
    empty.classList.add("hidden");
    if (retryContainer) retryContainer.classList.add("hidden");
    if (note) note.textContent = 'Refreshing market data...';
    // Keep existing table content, just show refresh indicator
    return;
  }
  
  if (state.pricesLoading && !hasPrices) {
    table.classList.remove("hidden");
    empty.classList.add("hidden");
    if (note) note.textContent = 'Fetching live prices...';
    if (!pricingSkeletonVisible) {
      tbody.innerHTML = PRICING_SKELETON_HTML;
      resetPricingRows();
      pricingSkeletonVisible = true;
    }
    return;
  }
  if (!hasPrices) {
    table.classList.add("hidden");
    tbody.innerHTML = "";
    resetPricingRows();
    pricingSkeletonVisible = false;
    empty.classList.remove("hidden");
    empty.textContent = "Market data service unreachable. Please try again later.";
    // Show retry button when no prices available
    if (retryContainer) retryContainer.classList.remove("hidden");
    if (note) note.textContent = "";
    return;
  }

  if (pricingSkeletonVisible) {
    tbody.innerHTML = "";
    resetPricingRows();
    pricingSkeletonVisible = false;
  }

  const assets = Array.from(new Set([...PRICING_PRIORITY, ...Object.keys(prices || {})]));
  const rows = [];
  assets.forEach((asset) => {
    const upper = String(asset).toUpperCase();
    const priceUsd = getAssetPriceUsd(upper);
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

  const desiredAssets = new Set(rows.map((row) => row.asset));
  for (const [asset, entry] of pricingRowCache.entries()) {
    if (!desiredAssets.has(asset)) {
      entry.el.remove();
      pricingRowCache.delete(asset);
    }
  }

  const desiredEntries = [];
  rows.forEach((row) => {
    let entry = pricingRowCache.get(row.asset);
    if (!entry) {
      entry = createPricingRow(row.asset);
      pricingRowCache.set(row.asset, entry);
    }
    updatePricingRow(entry, row);
    desiredEntries.push(entry);
  });

  const existingChildren = Array.from(tbody.children);
  let needsReorder = existingChildren.length !== desiredEntries.length;
  if (!needsReorder) {
    for (let i = 0; i < desiredEntries.length; i += 1) {
      if (existingChildren[i] !== desiredEntries[i].el) {
        needsReorder = true;
        break;
      }
    }
  }
  if (needsReorder) {
    const fragment = document.createDocumentFragment();
    desiredEntries.forEach((entry) => {
      fragment.appendChild(entry.el);
    });
    tbody.appendChild(fragment);
  }

  // Hide retry button when prices are successfully displayed
  if (retryContainer) retryContainer.classList.add("hidden");
  
  table.classList.toggle("hidden", rows.length === 0);
  empty.classList.toggle("hidden", rows.length > 0);
  empty.textContent = rows.length > 0 ? "" : "Market data service unreachable. Please try again later.";

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
        let priceAgeSeconds = null;
        if (state.lastPricesAt) {
          priceAgeSeconds = Math.floor((Date.now() - state.lastPricesAt) / 1000);
          metaParts.push(`updated ${priceAgeSeconds}s ago`);
        }
        // Readiness hint: show cache coverage when warming just finished
        const ch = Number(state.pricesMeta.cache_hits);
        const cm = Number(state.pricesMeta.cache_misses);
        if (Number.isFinite(ch) && Number.isFinite(cm)) {
          const total = ch + cm;
          if (total > 0) {
            const hitRate = ch / total;
            if (hitRate >= 0.8 && priceAgeSeconds !== null && priceAgeSeconds <= 30) {
              metaParts.push('ready');
            }
          }
        }
      if (metaParts.length > 0) {
        const metaText = metaParts.join(', ');
        note.textContent = note.textContent ? `${note.textContent} (${metaText})` : metaText;
      }
    }
  }
}


function formatAmount(asset, totalMinor, decimalsOverride = null) {
  const decimals = decimalsOverride !== null ? decimalsOverride : (assetDecimals[asset] ?? 18);
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

function computeUsdEstimate(asset, totalMinor, units, decimalsOverride = null) {
  const prices = state.prices || {};
  const decimals = decimalsOverride !== null ? decimalsOverride : (assetDecimals[asset] ?? 18);
  
  if (asset === "USDC") {
    const usd = Number(totalMinor || 0) / 10 ** decimals;
    if (Number.isFinite(usd) && usd > 0) {
      return `~$${usd.toFixed(2)} USD`;
    }
  }
  if (asset === "ETH") {
    const eth = Number(totalMinor || 0) / 10 ** decimals;
    const ethUsd = Number(prices.ETH);
    if (Number.isFinite(eth) && Number.isFinite(ethUsd) && ethUsd > 0) {
      return `~$${(eth * ethUsd).toFixed(2)} USD`;
    }
  }
  if (asset === "WBTC") {
    const wbtc = Number(totalMinor || 0) / 10 ** decimals;
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

function hasActiveQuote() {
  return !!(state.quote && !isQuoteExpired());
}

function isSlowNetwork() {
  const conn = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  const effective = conn && conn.effectiveType ? conn.effectiveType : "";
  return effective === "slow-2g" || effective === "2g";
}

function effectiveIntervalMs() {
  const now = Date.now();
  const resumeFast = state.resumeFastUntil && now < state.resumeFastUntil;
  let interval = hasActiveQuote() || resumeFast ? PRICE_POLL_ACTIVE_MS : PRICE_POLL_DEFAULT_MS;
  if (isSlowNetwork()) {
    interval = Math.max(interval, PRICE_POLL_SLOW_MS);
  }
  return interval;
}

function stopPricePolling() {
  if (pricesTimer) {
    clearTimeout(pricesTimer);
    pricesTimer = null;
  }
}

function scheduleNextPricePoll() {
  stopPricePolling();
  if (document.visibilityState !== "visible") return;
  pricesTimer = setTimeout(runPricePoll, effectiveIntervalMs());
}

function runPricePoll() {
  if (document.visibilityState !== "visible") return;
  Promise.resolve(fetchPrices())
    .catch((err) => console.warn("[runPricePoll] price refresh failed", err))
    .finally(() => {
      scheduleNextPricePoll();
    });
}

function startPricePolling(options = {}) {
  const { immediate = false } = options || {};
  stopPricePolling();
  if (document.visibilityState !== "visible") return;
  if (immediate) {
    Promise.resolve(fetchPrices())
      .catch((err) => console.warn("[startPricePolling] immediate refresh failed", err))
      .finally(() => {
        scheduleNextPricePoll();
      });
    return;
  }
  scheduleNextPricePoll();
}

function flushPendingDiemSnapshot() {
  if (!state.pendingDiemSnapshot) return;
  const reason = state.pendingDiemSnapshotReason || "visibility-resume";
  state.pendingDiemSnapshot = false;
  state.pendingDiemSnapshotReason = null;
  fetchDiemMarketSnapshot({ reason }).catch((err) => {
    console.warn("[flushPendingDiemSnapshot] failed", err);
  });
}

function handleVisibilityChange() {
  if (document.visibilityState === "visible") {
    const now = Date.now();
    const hiddenAt = state.lastHiddenAt;
    if (hiddenAt && now - hiddenAt > PRICE_POLL_RESUME_FAST_THRESHOLD_MS) {
      state.resumeFastUntil = now + PRICE_POLL_RESUME_FAST_THRESHOLD_MS;
    } else {
      state.resumeFastUntil = null;
    }
    state.lastHiddenAt = null;
    startPricePolling({ immediate: true });
    flushPendingDiemSnapshot();
    return;
  }
  state.lastHiddenAt = Date.now();
  state.hiddenTabSuppressions += 1;
  state.pendingHiddenSuppressions += 1;
  stopPricePolling();
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
    showAlert($("quote-status"), "error", "Quote expired. Refresh the quote and try again.");
    stopQuoteTimer();
    setQuoteButtonState(true, "Get Quote");
    if (!state.recoverPrompted) {
      showRecoverPanel({ focus: false, reason: "quote-expired" });
      const recoverStatus = $("recover-status");
      if (recoverStatus) {
        showAlert(recoverStatus, "info", "Already sent funds? Recover your API key below with your transaction.");
      }
      state.recoverPrompted = true;
    }
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

function setRecoverPanelVisible(show, options = {}) {
  const card = $("recover-card");
  if (!card) return;
  card.classList.toggle("hidden", !show);
  state.recoverPanelVisible = !!show;
  if (!show) {
    return;
  }
  const { focus = true } = options;
  if (!focus) {
    return;
  }
  const walletInput = $("recover-wallet");
  const txInput = $("recover-tx");
  const target = walletInput && !walletInput.value ? walletInput : txInput;
  if (target) {
    setTimeout(() => target.focus(), 0);
  }
}

function showRecoverPanel(options = {}) {
  setRecoverPanelVisible(true, options);
  updateRecoverButtonState();
}

function updateRecoverButtonState() {
  const btn = $("recover-btn");
  if (!btn) return;
  const walletField = $("recover-wallet");
  const txField = $("recover-tx");
  const walletRaw = (walletField?.value || "").trim();
  const txRaw = (txField?.value || "").trim();
  const walletOk = walletRaw.startsWith("0x") && walletRaw.length === 42;
  const txOk = txRaw.startsWith("0x") && txRaw.length === 66;
  btn.disabled = state.recovering || !walletOk || !txOk;
  if (walletField) {
    walletField.setAttribute("aria-invalid", String(walletRaw.length > 0 && !walletOk));
  }
  if (txField) {
    txField.setAttribute("aria-invalid", String(txRaw.length > 0 && !txOk));
  }
}

function setRecovering(flag) {
  state.recovering = flag;
  const spinner = $("recover-spinner");
  if (spinner) spinner.classList.toggle("hidden", !flag);
  const card = $("recover-card");
  if (card) card.setAttribute("aria-busy", String(flag));
  updateRecoverButtonState();
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

function saveSessionToStorage() {
  try {
    if (state.purchaseId) {
      localStorage.setItem("purchaseId", state.purchaseId);
    }
    if (state.quote && state.quote.quoteId) {
      localStorage.setItem("activeQuote", JSON.stringify({
        quoteId: state.quote.quoteId,
        expiresAt: state.quote.expiresAt,
        asset: state.quote.asset,
        units: state.quote.units,
        totalPrice: state.quote.totalPrice ?? state.quote.total_price,
      }));
    }
  } catch (err) {
    console.warn("Failed to save session to localStorage", err);
  }
}

function loadSessionFromStorage() {
  try {
    const storedPurchaseId = localStorage.getItem("purchaseId");
    if (storedPurchaseId) {
      state.purchaseId = storedPurchaseId;
    }
    const storedQuote = localStorage.getItem("activeQuote");
    if (storedQuote) {
      const parsed = JSON.parse(storedQuote);
        const now = Math.floor(Date.now() / 1000);
        const expires = Number(parsed.expiresAt);
        if (Number.isFinite(expires) && expires > now) {
          // Quote is still valid, restore it
          state.quote = parsed;
          return true;
      } else {
        // Quote expired, clear it
        localStorage.removeItem("activeQuote");
      }
    }
  } catch (err) {
    console.warn("Failed to load session from localStorage", err);
  }
  return false;
}

function clearSessionStorage() {
  try {
    localStorage.removeItem("purchaseId");
    localStorage.removeItem("activeQuote");
  } catch (err) {
    console.warn("Failed to clear session storage", err);
  }
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
  if (!details || !amountInput || !addressInput) {
    console.error("[applyQuote] Missing required DOM elements", { details: !!details, amountInput: !!amountInput, addressInput: !!addressInput });
    throw new Error("Missing required DOM elements for quote display");
  }

  if (!state.treasury) {
    showAlert($("quote-status"), "error", "Treasury address is not configured on the server.");
    details.classList.add("hidden");
    enableStep2(false);
    throw new Error("Treasury address is not configured on the server.");
  }

  // Use decimals from quote response if available, otherwise fall back to assetDecimals
  const quoteDecimals = Number(quoteData.discount?.decimals ?? quoteData.decimals);
  const decimals = Number.isFinite(quoteDecimals) && quoteDecimals > 0 ? quoteDecimals : null;
  
  const formatted = formatAmount(resolvedAsset, totalMinorRaw, decimals);
  amountInput.value = formatted.text;
  addressInput.value = state.treasury;
  if (usdLine) {
    const usdText = computeUsdEstimate(resolvedAsset, totalMinorRaw, unitsValue, decimals);
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
  const metaLatency = formatLatencyMeta(result.meta);
  const metaText = metaLatency ? `${metaLatency} ` : "";
  const statusEl = $("quote-status");
  if (statusEl) {
    showAlert(statusEl, "success", `${latencyText}${metaText}Send the payment before it expires.`);
    // Double-check that the alert is visible
    if (statusEl.classList.contains("hidden")) {
      console.warn("[applyQuote] Status alert still hidden after showAlert, forcing visibility");
      statusEl.classList.remove("hidden");
    }
  } else {
    console.error("[applyQuote] quote-status element not found");
  }
  enableStep2(true);
  resetStep3();
  startQuoteTimer();
  setQuoteButtonState(false, "Quote Active");
  startPricePolling();
  
  // Save to localStorage for persistence
  saveSessionToStorage();
}

function signingConfig() {
  const signing = (state.env && state.env.signing) || {};
  return {
    domain: signing.domain || "Venice Broker",
    version: signing.version || "1",
    chainId: Number(signing.chainId) || 8453,
  };
}

function toAssetMinorUnits(human, asset) {
  const decimals = assetDecimals[String(asset || "").toUpperCase()] || 6;
  const value = Number(human);
  if (!Number.isFinite(value) || value <= 0) return null;
  return Math.round(value * Math.pow(10, decimals));
}

async function ensureBuyerAddress() {
  const field = $("wallet-address");
  if (field && field.value) return String(field.value).trim();
  if (!window.ethereum) {
    throw new Error("Connect a wallet to place a limit bid.");
  }
  const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
  const account = Array.isArray(accounts) ? accounts[0] : null;
  if (!account) {
    throw new Error("Wallet did not return an address.");
  }
  if (field) field.value = account;
  return account;
}

async function signPurchaseIntent(message) {
  if (!window.ethereum) {
    throw new Error("Connect a wallet to place a limit bid.");
  }
  const cfg = signingConfig();
  const typed = {
    types: {
      EIP712Domain: [
        { name: "name", type: "string" },
        { name: "version", type: "string" },
        { name: "chainId", type: "uint256" },
      ],
      PurchaseIntent: [
        { name: "buyer", type: "address" },
        { name: "units", type: "uint256" },
        { name: "maxPrice", type: "uint256" },
        { name: "asset", type: "string" },
        { name: "expiry", type: "uint256" },
        { name: "slippageBps", type: "uint16" },
        { name: "nonce", type: "uint256" },
        { name: "chainId", type: "uint256" },
      ],
    },
    primaryType: "PurchaseIntent",
    domain: {
      name: cfg.domain,
      version: cfg.version,
      chainId: cfg.chainId,
    },
    message,
  };
  return window.ethereum.request({
    method: "eth_signTypedData_v4",
    params: [message.buyer, JSON.stringify(typed)],
  });
}

async function readErrorDetail(res, fallback) {
  try {
    const body = await res.json();
    return body.detail || body.message || fallback;
  } catch (_) {
    return fallback;
  }
}

async function settleBidUntilQuoted(bidId, signal) {
  const deadline = Date.now() + 30000;
  let lastDetail = "Bid is waiting for a quote.";
  while (Date.now() < deadline) {
    const settle = await fetchWithRetry(
      `${SETTLE_ENDPOINT}/${encodeURIComponent(bidId)}/settle`,
      { method: "POST", headers: JSON_POST_HEADERS, signal },
      { timeoutMs: 20000 }
    );
    if (settle.ok) {
      return settle.json();
    }
    lastDetail = await readErrorDetail(settle, lastDetail);
    if (settle.status !== 409 || /expired|out of band/i.test(String(lastDetail))) {
      const err = new Error(lastDetail);
      err.status = settle.status;
      throw err;
    }
    await new Promise((resolve) => setTimeout(resolve, 1500));
  }
  throw new Error(lastDetail);
}

async function requestLimitBid({ unitsRaw, asset, signal }) {
  const maxInput = $("quote-max-price");
  const maxError = $("max-price-error");
  if (maxError) maxError.classList.add("hidden");
  const maxPrice = toAssetMinorUnits(maxInput && maxInput.value, asset);
  if (!maxPrice) {
    if (maxError) {
      maxError.textContent = "Enter a maximum unit price in the payment asset.";
      maxError.classList.remove("hidden");
    }
    if (maxInput) maxInput.setAttribute("aria-invalid", "true");
    throw new Error("Enter a maximum unit price for the limit bid.");
  }
  if (maxInput) maxInput.setAttribute("aria-invalid", "false");

  const buyer = await ensureBuyerAddress();
  const cfg = signingConfig();
  const ttl = Number(state.env && state.env.buyer && state.env.buyer.quote_ttl) || 3600;
  const message = {
    buyer,
    units: Math.round(Number(unitsRaw) * 1_000_000),
    maxPrice,
    asset,
    expiry: Math.floor(Date.now() / 1000) + Math.max(60, ttl),
    slippageBps: 50,
    nonce: Date.now(),
    chainId: cfg.chainId,
  };
  const signature = await signPurchaseIntent(message);
  const created = await fetchWithRetry(
    BIDS_ENDPOINT,
    {
      method: "POST",
      headers: JSON_POST_HEADERS,
      signal,
      body: JSON.stringify({ ...message, signature }),
    },
    { timeoutMs: 20000 }
  );
  if (!created.ok) {
    throw new Error(await readErrorDetail(created, "Bid request failed"));
  }
  const bid = await created.json();
  if (!bid || !bid.bidId) {
    throw new Error("Bid response missing bidId");
  }
  return settleBidUntilQuoted(bid.bidId, signal);
}

async function requestQuote(options = {}) {
  const { warmupRetry = false } = options;
  console.log("[requestQuote] Quote request initiated", { warmupRetry });
  if (state.quoteWarmupTimer) {
    clearTimeout(state.quoteWarmupTimer);
    state.quoteWarmupTimer = null;
  }
  if (!warmupRetry) {
    state.quoteWarmupAttempts = 0;
  }
  const unitsInput = $("quote-units");
  const assetSelect = $("quote-asset");
  const refreshBtn = $("quote-refresh");
  const status = $("quote-status");
  const details = $("quote-details");
  
  console.log("[requestQuote] DOM elements found:", {
    unitsInput: !!unitsInput,
    assetSelect: !!assetSelect,
    status: !!status,
    details: !!details
  });
  
  if (!warmupRetry) {
    setQuoteButtonState(true, quoteActionLabel());
    if (refreshBtn) refreshBtn.hidden = true;
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
    startPricePolling();
    enableStep2(false);
    resetStep3();
    clearAlert(status);
    clearAlert($("verify-status"));
    state.lastQuoteLatencyMs = null;
  }

  // Force reload env/prices if treasury is missing (could be stale cache)
  if (!state.treasury && !warmupRetry) {
    console.warn("[requestQuote] Treasury not loaded, forcing env refresh");
    try {
      await loadEnvAndPrices();
      if (!state.treasury) {
        showAlert(status, "error", "Treasury address is not configured on server. Please contact support.");
        return;
      }
    } catch (err) {
      console.error("[requestQuote] Failed to load treasury", err);
      showAlert(status, "error", "Failed to load server configuration. Please refresh the page.");
      return;
    }
  }

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
      setQuoteButtonState(true, quoteActionLabel());
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
      setQuoteButtonState(true, quoteActionLabel());
      return;
  }

  if (assetSelect) assetSelect.setAttribute("aria-invalid", "false");

  // Allow quotes even without local price data; the backend provides pricing info
  const priceSnapshot = state.prices || {};
  const hasPriceData = priceSnapshot && Object.keys(priceSnapshot).length > 0;
  const diemPrice = Number(priceSnapshot.DIEM);
  // Only warn if prices are missing; don't block quote request
  if (!hasPriceData || !Number.isFinite(diemPrice) || diemPrice <= 0) {
    console.warn("Market snapshot not yet loaded, but proceeding with quote request");
  } else if (asset !== "USDC") {
    const paymentPrice = Number(priceSnapshot[asset]);
    if (!Number.isFinite(paymentPrice) || paymentPrice <= 0) {
      console.warn(`No current price for ${asset} in snapshot, but proceeding with quote request`);
    }
  }

  // Abort any in-flight quote and start a new request id to prevent races
  if (state.quoteAbort) {
    try { state.quoteAbort.abort(); } catch (_) {}
  }
  state.quoteAbort = new AbortController();
  state.activeQuoteRequestId = (state.activeQuoteRequestId || 0) + 1;
  const thisRequestId = state.activeQuoteRequestId;

    try {
      setQuoteButtonState(false, warmupRetry ? "Initializing..." : "Getting quote...");
      if (refreshBtn) refreshBtn.disabled = true;

    const startedAt = nowMs();
    let body;
    if (isLimitBidMode()) {
      setQuoteButtonState(false, warmupRetry ? "Initializing..." : "Placing bid...");
      body = await requestLimitBid({
        unitsRaw,
        asset,
        signal: state.quoteAbort.signal,
      });
    } else {
    const params = new URLSearchParams();
    params.set("units", String(unitsRaw));
    params.set("asset", asset);
    // Multi-layer cache-bust: timestamp + random to avoid intermediary/browser/CDN caches serving stale quotes
    params.set("_t", String(Date.now()));
    params.set("_r", String(Math.random()).substring(2, 10));
    const quoteUrl = `${QUOTE_ENDPOINT}?${params.toString()}`;
    console.log("[requestQuote] Fetching quote from:", quoteUrl);
    const res = await fetchWithRetry(
      quoteUrl,
      { 
        headers: { 
          ...JSON_GET_HEADERS, 
          'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
          'Pragma': 'no-cache',
          'Expires': '0'
        },
        cache: 'no-store',
        signal: state.quoteAbort.signal
      },
      { timeoutMs: 20000 }  // 20s timeout (uses default 3 attempts)
    );
    // If a newer request started, ignore this response
    if (thisRequestId !== state.activeQuoteRequestId) {
      console.warn("[requestQuote] Stale quote response ignored");
      return;
    }
    console.log("[requestQuote] Response received:", { ok: res.ok, status: res.status, statusText: res.statusText });
    if (!res.ok) {
      let errorDetail = "Quote request failed";
      let fallbackPriceInfo = null;
      try {
        const text = await res.text();
        try {
          const jsonError = JSON.parse(text);
          errorDetail = jsonError.detail || jsonError.message || text || errorDetail;
          if (jsonError && typeof jsonError.detail === "object") {
            fallbackPriceInfo = jsonError.detail.fallbackPrice || jsonError.detail.fallback_price || null;
          } else if (jsonError && typeof jsonError.fallbackPrice === "object") {
            fallbackPriceInfo = jsonError.fallbackPrice;
          }
        } catch {
          errorDetail = text || errorDetail;
        }
      } catch {
        errorDetail = res.status === 503 ? "Service temporarily unavailable" : `HTTP ${res.status}`;
      }

      const normalizedDetail = normalizeErrorMessage(errorDetail);

      const createError = (message) => {
        const err = new Error(message);
        if (fallbackPriceInfo) {
          err.fallbackPrice = fallbackPriceInfo;
        }
        return err;
      };

      const warmupDetail = normalizedDetail || "";
      const isWarmup = res.status === 503 && (
        /warming up|market data|pricing unavailable/i.test(warmupDetail)
      );

      if (isWarmup) {
        throw createError("Broker is initializing market data. Please wait a moment and try again.");
      }

      throw createError(normalizedDetail);
    }
    body = await res.json();
    }
    const latencyMs = Math.round(nowMs() - startedAt);
    state.lastQuoteLatencyMs = latencyMs;
    console.debug(`[quote] fetched in ${latencyMs} ms`, body);
    
    // If a newer request started, ignore this response
    if (thisRequestId !== state.activeQuoteRequestId) {
      console.warn("[requestQuote] Stale quote JSON ignored");
      return;
    }
    
    // Validate response structure
    if (!body || typeof body !== "object") {
      throw new Error(`Invalid quote response format: ${typeof body}`);
    }
    
    // Check for required fields (either camelCase or snake_case)
    const hasRequiredFields = (
      (body.totalPrice !== undefined || body.total_price !== undefined) &&
      (body.units !== undefined || body.quantity !== undefined) &&
      body.asset !== undefined &&
      (body.quoteId !== undefined || body.quote_id !== undefined)
    );
    
    if (!hasRequiredFields) {
      console.error("[quote] Missing required fields in response", body);
      throw new Error(`Quote response missing required fields. Received: ${JSON.stringify(Object.keys(body))}`);
    }
    
    // Wrap applyQuote in try-catch for better error handling
    try {
      applyQuote(body);
      state.quoteWarmupAttempts = 0;
      updateVerifyButtonState();
    } catch (applyErr) {
      console.error("[quote] applyQuote failed", applyErr, body);
      const applyErrMessage = applyErr instanceof Error ? applyErr.message : String(applyErr);
      throw new Error(`Failed to process quote: ${applyErrMessage}. Response: ${JSON.stringify(body)}`);
    }
  } catch (err) {
    // If a newer request started, suppress error UI for this one
    if (thisRequestId !== state.activeQuoteRequestId) {
      console.warn("[requestQuote] Stale quote error suppressed");
      return;
    }
    console.error("[quote] request failed", err);
    const errMessage = normalizeErrorMessage(err);
    const fallbackInfo =
      err && typeof err === "object" && "fallbackPrice" in err ? err.fallbackPrice : null;
    if (fallbackInfo && typeof fallbackInfo === "object") {
      const fallbackPriceUsd = Number(fallbackInfo.priceUsd);
      if (Number.isFinite(fallbackPriceUsd) && fallbackPriceUsd > 0) {
        state.diemPriceSnapshot = fallbackInfo;
        state.lastDiemSnapshotAt = Date.now();
        if (diemPriceMissing(state.prices)) {
          state.prices = state.prices || {};
          state.prices.DIEM = fallbackPriceUsd;
        }
        computeQuoteMetrics();
        renderPricingTable();
      }
    } else if (diemPriceMissing(state.prices) && !state.diemPriceSnapshot) {
      fetchDiemMarketSnapshot({ force: true, reason: "quote-error" }).catch((snapErr) => {
        console.warn("[requestQuote] fallback snapshot failed", snapErr);
      });
    }
    const fallbackHint =
      fallbackInfo && Number(fallbackInfo.priceUsd) > 0
        ? ` Current DIEM market price: ${formatUsd(Number(fallbackInfo.priceUsd))}.`
        : "";
    const warmup = err instanceof Error && (
      err.name === "AbortError" || 
      err.message === "SLA" || 
      /abort|timeout/i.test(errMessage) ||
      /warming up|market data|pricing unavailable|broker is initializing/i.test(errMessage)
    );
    const message =
      warmup
        ? `Broker is initializing market data. This usually takes a few seconds. Please try again.${fallbackHint}`
        : `${errMessage || "Quote request failed. Please try again."}${fallbackHint}`;

    if (status) {
      showAlert(status, warmup ? "info" : "error", message);
      // Ensure error is visible
      if (status.classList.contains("hidden")) {
        console.warn("[requestQuote] Error alert still hidden, forcing visibility");
        status.classList.remove("hidden");
      }
    } else {
      console.error("[requestQuote] quote-status element not found, cannot display error");
      // Fallback: try to alert user via browser alert
      alert(`Quote request failed: ${message}`);
    }
    
    if (warmup) {
      const retryContainer = $("pricing-retry");
      if (retryContainer) {
        retryContainer.classList.remove("hidden");
      }
      const attempts = Number(state.quoteWarmupAttempts) || 0;
      const nextDelay = Math.min(30000, Math.pow(2, attempts) * 2000);
      state.quoteWarmupAttempts = attempts + 1;
      if (status) {
        const seconds = Math.round(nextDelay / 1000);
        const retryMessage = seconds > 0
          ? `Broker is initializing market data. Retrying in ${seconds} second${seconds === 1 ? "" : "s"}...`
          : "Broker is initializing market data. Retrying shortly...";
        showAlert(status, "info", retryMessage);
      }
      setQuoteButtonState(false, "Initializing...");
      state.quoteWarmupTimer = setTimeout(() => {
        state.quoteWarmupTimer = null;
        if (thisRequestId === state.activeQuoteRequestId) {
          requestQuote({ warmupRetry: true }).catch((retryErr) => {
            console.error("[requestQuote] Warmup retry failed", retryErr);
          });
        } else {
          console.log("[requestQuote] Warmup retry skipped due to newer request");
        }
      }, nextDelay);
    }
  } finally {
    // Only reset UI if this is still the latest request
    if (thisRequestId === state.activeQuoteRequestId) {
      // Only re-enable if quote wasn't successfully applied (applyQuote sets it to disabled)
      const btn = $("quote-btn");
      if (state.quoteWarmupTimer) {
        setQuoteButtonState(false, "Initializing...");
      } else if (btn && btn.disabled && btn.textContent === "Quote Active") {
        // Quote was successfully applied, keep it disabled
      } else {
        setQuoteButtonState(true, quoteActionLabel());
      }
      if (refreshBtn) refreshBtn.disabled = false;
    }
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

// The server only releases API keys to the wallet that paid: fetch a
// single-use challenge and sign it with the buyer wallet.
async function requestWalletSignature(txHash, buyerAddress) {
  if (!window.ethereum) {
    throw new Error("No wallet provider detected. Use a browser wallet to sign the verification challenge.");
  }
  const challengeUrl = `${CHALLENGE_ENDPOINT}?txHash=${encodeURIComponent(txHash)}&buyerAddress=${encodeURIComponent(buyerAddress)}`;
  const challengeRes = await fetch(challengeUrl, { headers: JSON_GET_HEADERS });
  const challengeBody = await challengeRes.json().catch(() => ({}));
  if (!challengeRes.ok) {
    const detail = challengeBody && (challengeBody.detail || challengeBody.message);
    throw new Error(detail || "Failed to issue signing challenge.");
  }
  const { message, nonce } = challengeBody || {};
  if (!message || !nonce) {
    throw new Error("Server returned an invalid signing challenge.");
  }
  let signature;
  try {
    signature = await window.ethereum.request({
      method: "personal_sign",
      params: [message, buyerAddress],
    });
  } catch (err) {
    if (err && typeof err === "object" && "code" in err && err.code === 4001) {
      const rejected = new Error("Signature request was rejected.");
      rejected.userRejected = true;
      throw rejected;
    }
    throw err instanceof Error ? err : new Error(String(err));
  }
  if (!signature) {
    throw new Error("Wallet did not return a signature.");
  }
  return { signature, nonce };
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

  try {
    showAlert($("verify-status"), "info", "Sign the verification challenge with the wallet that sent the payment...");
    const { signature, nonce } = await requestWalletSignature(txHash, buyerAddress);

    showAlert($("verify-status"), "info", "Verifying payment on-chain...");
    const res = await fetch(VERIFY_ENDPOINT, {
      method: "POST",
      headers: JSON_POST_HEADERS,
      body: JSON.stringify({
        quoteId: state.quote.quoteId,
        txHash,
        buyerAddress,
        signature,
        nonce,
      }),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      const detail = body && (body.detail || body.message);
      throw new Error(detail || "Verification failed");
    }
    handleVerifyResponse(body, "verify");
  } catch (err) {
    const level = err && err.userRejected ? "info" : "error";
    showAlert($("verify-status"), level, err instanceof Error ? err.message : String(err));
  } finally {
    setVerifying(false);
    updateVerifyButtonState();
  }
}

async function recoverKey() {
  const walletInput = $("recover-wallet");
  const txInput = $("recover-tx");
  const statusEl = $("recover-status");
  const walletErrorEl = $("recover-wallet-error");
  const txErrorEl = $("recover-tx-error");
  if (!walletInput || !txInput || !statusEl) return;

  const walletRaw = (walletInput.value || "").trim();
  const txRaw = (txInput.value || "").trim();
  const buyerAddress = normalizeHex(walletRaw);
  const txHash = normalizeHex(txRaw);

  if (walletErrorEl) walletErrorEl.classList.add("hidden");
  if (txErrorEl) txErrorEl.classList.add("hidden");

  if (buyerAddress.length !== 42) {
    if (walletErrorEl) {
      walletErrorEl.textContent = "Enter the wallet that sent the payment.";
      walletErrorEl.classList.remove("hidden");
    }
    walletInput.setAttribute("aria-invalid", "true");
    return;
  }

  if (txHash.length !== 66) {
    if (txErrorEl) {
      txErrorEl.textContent = "Enter the Base transaction hash (starts with 0x).";
      txErrorEl.classList.remove("hidden");
    }
    txInput.setAttribute("aria-invalid", "true");
    return;
  }

  walletInput.setAttribute("aria-invalid", "false");
  txInput.setAttribute("aria-invalid", "false");

  if (!window.ethereum) {
    showAlert(statusEl, "error", "No wallet provider detected. Use a browser wallet to sign the recovery challenge.");
    return;
  }

  clearAlert(statusEl);
  showAlert(statusEl, "info", "Sign the recovery challenge in your wallet…");
  setRecovering(true);

  try {
    const { signature, nonce } = await requestWalletSignature(txHash, buyerAddress);

    showAlert(statusEl, "info", "Verifying transaction and recovering key…");
    const recoverRes = await fetch(RECOVER_ENDPOINT, {
      method: "POST",
      headers: JSON_POST_HEADERS,
      body: JSON.stringify({
        txHash,
        buyerAddress,
        signature,
        nonce,
      }),
    });
    const recoverBody = await recoverRes.json().catch(() => ({}));
    if (!recoverRes.ok) {
      const detail = recoverBody && (recoverBody.detail || recoverBody.message);
      throw new Error(detail || "Recovery failed.");
    }
    handleVerifyResponse(recoverBody, "recover");
  } catch (err) {
    const level = err && err.userRejected ? "info" : "error";
    const message = err instanceof Error ? err.message : String(err);
    showAlert(statusEl, level, message || "Recovery failed.");
  } finally {
    setRecovering(false);
  }
}

function handleVerifyResponse(body, context = "verify") {
  const preferredStatus = context === "recover" ? $("recover-status") : $("verify-status");
  const statusEl = preferredStatus || $("verify-status");
  if (!body) {
    showAlert(statusEl, "error", "Verification returned no data.");
    return;
  }
  const subkey = body.subkey;
  const expiresAt = body.expiresAt;
  const purchaseId = body.purchaseId;
  if (subkey) {
    const message =
      context === "recover"
        ? "Key recovered. Copy your API key below."
        : "Payment verified. Copy your API key below.";
    showAlert(statusEl, "success", message);
    showKey({ subkey, expiresAt });
    return;
  }
  if (purchaseId) {
    state.purchaseId = purchaseId;
    saveSessionToStorage(); // Persist purchaseId
    const message =
      context === "recover"
        ? "Key recovery confirmed. Issuing your key now."
        : "Payment verified. Issuing your key now.";
    showAlert(statusEl, "success", message);
    showKey({ status: body.status, purchaseId });
    pollPurchaseUntilReady(purchaseId);
    return;
  }
  const infoMessage =
    context === "recover"
      ? "Recovery complete. Waiting for key issuance..."
      : "Verification complete. Waiting for key issuance...";
  showAlert(statusEl, "info", infoMessage);
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

// Status endpoints never carry the key itself; once issuance is confirmed the
// buyer retrieves it with a wallet signature via the recovery flow.
function promptKeyRetrieval(keyStatus) {
  showAlert(
    keyStatus,
    "success",
    "Your key is ready. Use \"Recover your API key\" below and sign with the paying wallet to retrieve it."
  );
  showRecoverPanel({ focus: false });
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
          if (payload.keyIssued) {
            clearAlert(keyStatus);
            promptKeyRetrieval(keyStatus);
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
      if (body && body.keyIssued) {
        clearAlert(keyStatus);
        promptKeyRetrieval(keyStatus);
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


async function connectWallet(evt) {
  const btn = (evt && evt.currentTarget) || $("connect-wallet");
  if (!window.ethereum || !btn) {
    if (btn) btn.classList.add("hidden");
    return;
  }
  const targetFieldId = btn.dataset?.field || "wallet-address";
  const statusId = btn.dataset?.status || "verify-status";
  const walletField = $(targetFieldId);
  const statusEl = $(statusId);
  if (!walletField) {
    showAlert(statusEl, "error", "Unable to locate wallet field for connection.");
    return;
  }
  try {
    btn.disabled = true;
    btn.textContent = "Connecting...";
    const accounts = await window.ethereum.request({ method: "eth_requestAccounts" });
    const account = Array.isArray(accounts) ? accounts[0] : null;
    if (account) {
      walletField.value = account;
      if (statusEl) {
        showAlert(statusEl, "info", "Wallet connected. Address prefilled.");
      }
      // Prefill the other form if empty to save time
      if (targetFieldId !== "wallet-address") {
        const verifyField = $("wallet-address");
        if (verifyField && !verifyField.value) {
          verifyField.value = account;
        }
      }
      if (targetFieldId !== "recover-wallet") {
        const recoverField = $("recover-wallet");
        if (recoverField && !recoverField.value) {
          recoverField.value = account;
        }
      }
    }
  } catch (err) {
    const message = err && err.message ? err.message : "Wallet connection failed.";
    if (statusEl) {
      showAlert(statusEl, "error", message);
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect Wallet";
  }
  updateVerifyButtonState();
  updateRecoverButtonState();
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
  syncQuoteModeUi();
  if (feats && feats.quotes === false) {
    setQuoteButtonState(false, "Get Quote");
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

function diemPriceMissing(prices) {
  const diem = Number(prices && prices.DIEM);
  return !(Number.isFinite(diem) && diem > 0);
}

function maybeFetchDiemSnapshot(prices, reason) {
  const needsSnapshot = diemPriceMissing(prices);
  if (!needsSnapshot && !DIEM_SNAPSHOT_DEBUG) return;
  const why = DIEM_SNAPSHOT_DEBUG
    ? needsSnapshot
      ? `${reason}-debug`
      : "debug"
    : reason;
  fetchDiemMarketSnapshot({ reason: why }).catch((err) => {
    console.warn("[maybeFetchDiemSnapshot] failed", err);
  });
}

function diemSnapshotMode() {
  const mode = state.env && state.env.features && state.env.features.diem_snapshot_mode;
  return String(mode || "always").toLowerCase();
}

function scheduleDiemSnapshot(prices, reason) {
  const mode = diemSnapshotMode();
  if (mode === "on-demand") {
    maybeFetchDiemSnapshot(prices, reason);
    return;
  }
  fetchDiemMarketSnapshot({ reason }).catch((err) => {
    console.warn("[scheduleDiemSnapshot] failed", err);
  });
}

function marketRequestHeaders() {
  const headers = { ...JSON_GET_HEADERS };
  if (state.pendingHiddenSuppressions > 0) {
    headers["X-Broker-Hidden-Suppressed"] = String(state.pendingHiddenSuppressions);
    state.pendingHiddenSuppressions = 0;
  }
  return headers;
}

async function loadEnvAndPrices(options = {}) {
  const { force = false } = options || {};
  // Single-flight: prevent concurrent calls
  if (state.inflightPricesPromise && !force) {
    return state.inflightPricesPromise;
  }
  if (state.inflightPricesPromise && force) {
    if (state.pricesAbort) {
      state.pricesAbort.abort();
    }
    state.inflightPricesPromise = null;
  }

  const hadPrices = !!(state.prices && Object.keys(state.prices).length);
  state.pricesMeta = null;
  state.pricesLoading = true;
  const quotesAvailable = quotesEnabled();
  if (!hadPrices && quotesAvailable) {
    setQuoteButtonState(false, "Loading prices...");
  }
  
  // Abort previous request if any
  if (state.pricesAbort) {
    state.pricesAbort.abort();
  }
  const abortController = new AbortController();
  state.pricesAbort = abortController;

  // Watchdog timer: force pricesLoading=false after max wait time to prevent stuck loading state
  // Increased to 60s for cold-start scenarios where RPC warmup can take 30+ seconds
  const maxWaitTime = 60000; // 60s max wait (cold-start RPC + buffer)
  const watchdog = setTimeout(() => {
    if (state.pricesLoading) {
      console.warn("[loadEnvAndPrices] Watchdog: forcing pricesLoading=false after", maxWaitTime, "ms");
      state.pricesLoading = false;
      renderPricingTable();
    }
  }, maxWaitTime);

  let promise;
  promise = (async () => {
    try {
      const url = ENV_AND_PRICES_BASE;
      const res = await fetchWithRetry(
        url,
        {
          headers: marketRequestHeaders(),
          signal: abortController.signal
        },
        { timeoutMs: 60000, attempts: 2 }  // Cold-start RPC warmup can exceed 20s; don't abort first
      );
      // Log timeout/abort for debugging
      if (abortController.signal.aborted) {
        console.warn("[loadEnvAndPrices] Request was aborted");
      }
      if (!res.ok) {
        const text = await res.text();
        throw new Error(text || "Combined env/prices request failed");
      }
      const body = await res.json().catch(err => {
        throw new Error(`Failed to parse JSON response: ${err.message || err}`);
      });
      const envPayload = body && body.env;
      const pricePayload = body && body.prices; // Can be empty {} if prices fetch failed
      if (!envPayload) {
        throw new Error("Combined payload missing env");
      }
      // Allow empty prices - backend may return {} on failure for graceful degradation
      try {
        applyEnvPayload(envPayload || {});
      } catch (envErr) {
        console.error("[loadEnvAndPrices] Failed to apply env payload:", envErr);
        throw new Error(`Failed to process server config: ${envErr.message || envErr}`);
      }
      state.prices = pricePayload || {};
      scheduleDiemSnapshot(state.prices, "env-and-prices");
      state.pricesMeta = body && body.meta ? body.meta : null;
      state.lastPricesAt = Date.now();
      computeQuoteMetrics();
      clearAlert($("quote-status"));
      if (quotesAvailable) {
        setQuoteButtonState(true, "Get Quote");
      }
      return true;
    } catch (err) {
      state.pricesMeta = null;
      console.warn("Combined env/prices fetch failed", err);
      if (abortController.signal.aborted) {
        return false;
      }
      
      // Check if we have fresh cached data (< 60s old)
      if (state.lastPricesAt && (Date.now() - state.lastPricesAt) < MAX_PRICE_STALE_SECONDS * 1000) {
        console.log("Using cached prices due to fetch failure");
        return true; // Use cached data
      }
      
      showAlert($("quote-status"), "error", "Market data service unreachable. Please try again later.");
      if (!hadPrices && quotesAvailable) {
        setQuoteButtonState(false, "Retry prices");
      }
      return false;
    } finally {
      clearTimeout(watchdog);
      state.pricesLoading = false;
      if (state.inflightPricesPromise === promise) {
        state.inflightPricesPromise = null;
      }
      if (state.pricesAbort === abortController) {
        state.pricesAbort = null;
      }
      renderPricingTable();
    }
  })();

  state.inflightPricesPromise = promise;
  return promise;
}


async function fetchDiemMarketSnapshot(options = {}) {
  const { force = false, reason = "unspecified" } = options || {};
  if (!force && document.visibilityState === "hidden") {
    state.pendingDiemSnapshot = true;
    state.pendingDiemSnapshotReason = reason;
    return;
  }

  // Throttle: reuse recent snapshot if <30s old to reduce snapshot endpoint traffic
  const now = Date.now();
  if (!force && state.lastDiemSnapshotAt && (now - state.lastDiemSnapshotAt) < 30000) {
    return;
  }
  
  const cacheBust = `?_t=${Date.now()}&_r=${String(Math.random()).substring(2, 10)}`;
  const url = `${DIEM_PRICE_ENDPOINT}${cacheBust}`;
  try {
    const res = await fetchWithRetry(
      url,
      {
        headers: {
          ...JSON_GET_HEADERS,
          "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
          Pragma: "no-cache",
          Expires: "0",
        },
        cache: "no-store",
      },
      { timeoutMs: 15000, attempts: 3 }
    );
    if (!res.ok) {
      throw new Error(`DIEM price snapshot failed: HTTP ${res.status}`);
    }
    const body = await res.json();
    const snapshot = (body && body.diem) || null;
    if (snapshot && typeof snapshot === "object") {
      state.diemPriceSnapshot = snapshot;
      state.lastDiemSnapshotAt = Date.now();
      const priceUsd = Number(snapshot.priceUsd);
      if (Number.isFinite(priceUsd) && priceUsd > 0) {
        state.prices = state.prices || {};
        const current = Number(state.prices.DIEM);
        if (!Number.isFinite(current) || current <= 0) {
          state.prices.DIEM = priceUsd;
        }
      }
      computeQuoteMetrics();
      renderPricingTable();
    }
  } catch (err) {
    console.warn("[fetchDiemMarketSnapshot] failed", err);
  }
}


function populateAssets(assets) {
  const select = $("quote-asset");
  if (!select) return;
  select.innerHTML = "";
  // Default to the full set of broker-supported payment assets when the server
  // does not return a list yet (e.g., env/prices endpoint transient failure).
  const list = Array.isArray(assets) && assets.length ? assets : ["USDC", "ETH", "WBTC"];
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
  const previousMeta = state.pricesMeta;
  state.pricesLoading = true;
  state.pricesMeta = null;

  // Abort previous request if any
  if (state.pricesAbort) {
    state.pricesAbort.abort();
  }
  state.pricesAbort = new AbortController();

  // Watchdog timer: ensure we don't get stuck in "Fetching live prices..." if the
  // network call hangs or never settles. This also aborts the in‑flight request.
  // Increased to 60s for cold-start scenarios where backend cache is warming up
  const maxWaitTime = 60000; // 60s safety cap (cold-start RPC + buffer)
  const watchdog = setTimeout(() => {
    if (state.pricesLoading) {
      console.warn("[fetchPrices] Watchdog: forcing pricesLoading=false after", maxWaitTime, "ms");
      state.pricesLoading = false;
      try {
        if (state.pricesAbort) {
          state.pricesAbort.abort();
        }
      } catch (_) {}
      renderPricingTable();
    }
  }, maxWaitTime);

  if (!hadPrices) {
    renderPricingTable();
  }

  const promise = (async () => {
    try {
      const url = PRICES_BASE;
      const res = await fetchWithRetry(url, {
        headers: marketRequestHeaders(),
        signal: state.pricesAbort.signal
      });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const body = await res.json();
      state.prices = (body && body.prices) || {};
      scheduleDiemSnapshot(state.prices, "market-prices");
      state.pricesMeta = body && body.meta ? body.meta : null;
      state.lastPricesAt = Date.now();
      clearAlert($("quote-status"));
    } catch (err) {
      if (err) {
        console.warn('Market price fetch failed', err);
      }
      
      // Check if we have fresh cached data (< 60s old)
      if (state.lastPricesAt && (Date.now() - state.lastPricesAt) < MAX_PRICE_STALE_SECONDS * 1000) {
        console.log("Using cached prices due to fetch failure");
        if (previousMeta) {
          state.pricesMeta = previousMeta;
        }
        return; // Use cached data
      }

      if (!hadPrices) {
        showAlert($("quote-status"), "error", "Market data service unreachable. We'll retry automatically.");
        // Show retry button for market snapshot failures
        const retryContainer = $("pricing-retry");
        if (retryContainer) {
          retryContainer.classList.remove("hidden");
        }
      }
    } finally {
      clearTimeout(watchdog);
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

function formatLatencyMeta(meta) {
  if (!meta || typeof meta !== "object") return "";
  const { totalMs, marketDataMs, engineMs, persistMs } = meta;
  const parts = [];
  if (Number.isFinite(totalMs)) parts.push(`total ${Math.round(totalMs)} ms`);
  if (Number.isFinite(marketDataMs)) parts.push(`market ${Math.round(marketDataMs)} ms`);
  if (Number.isFinite(engineMs)) parts.push(`engine ${Math.round(engineMs)} ms`);
  if (Number.isFinite(persistMs)) parts.push(`persist ${Math.round(persistMs)} ms`);
  return parts.length ? `(${parts.join(", ")})` : "";
}

function setupEventHandlers() {
  // Debounce quote requests to prevent rapid-fire clicking causing race conditions
  let quoteDebounceTimer = null;
  const debouncedRequestQuote = () => {
    if (quoteDebounceTimer) {
      console.log("[setupEventHandlers] Debouncing quote request - ignoring rapid click");
      return;
    }
    quoteDebounceTimer = setTimeout(() => {
      quoteDebounceTimer = null;
    }, 500); // 500ms debounce window
    requestQuote();
  };

  const quoteBtn = $("quote-btn");
  if (quoteBtn) {
    console.log("[setupEventHandlers] Quote button found, attaching click handler");
    quoteBtn.addEventListener("click", debouncedRequestQuote);
    setQuoteButtonState(true, quoteActionLabel());
  } else {
    console.error("[setupEventHandlers] Quote button NOT found!");
  }
  const quoteMode = $("quote-mode");
  if (quoteMode) {
    quoteMode.addEventListener("change", syncQuoteModeUi);
  }
  const refreshBtn = $("quote-refresh");
  if (refreshBtn) refreshBtn.addEventListener("click", debouncedRequestQuote);
  
  // Wire up retry button for price loading
  const retryBtn = $("retry-prices-btn");
  if (retryBtn) {
    retryBtn.addEventListener("click", () => {
      const retryContainer = $("pricing-retry");
      if (retryContainer) retryContainer.classList.add("hidden");
      loadEnvAndPrices({ force: true }).catch(err => console.error("Retry failed", err));
    });
  }
  
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
  const recoverBtn = $("recover-btn");
  if (recoverBtn) recoverBtn.addEventListener("click", recoverKey);
  const recoverWallet = $("recover-wallet");
  if (recoverWallet) recoverWallet.addEventListener("input", updateRecoverButtonState);
  const recoverTx = $("recover-tx");
  if (recoverTx) recoverTx.addEventListener("input", updateRecoverButtonState);
  const recoverConnect = $("recover-connect");
  if (recoverConnect) {
    if (!window.ethereum) {
      recoverConnect.classList.add("hidden");
    } else {
      recoverConnect.addEventListener("click", connectWallet);
    }
  }
  const recoverLink = $("recover-link");
  if (recoverLink) {
    recoverLink.addEventListener("click", (evt) => {
      evt.preventDefault();
      showRecoverPanel({ focus: true });
      state.recoverPrompted = true;
      const statusEl = $("recover-status");
      if (statusEl && !statusEl.textContent) {
        showAlert(statusEl, "info", "Recover your key with the transaction hash you used to pay the treasury.");
      }
    });
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
  document.addEventListener("visibilitychange", handleVisibilityChange);
  if (document.visibilityState === "hidden") {
    state.lastHiddenAt = Date.now();
  }
  updateRecoverButtonState();
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
  // setupEventHandlers() is now called before init() to ensure button works even if init fails
  enableStep2(false);
  state.pricesLoading = true;
  renderPricingTable();
  
  // CRITICAL: Load env/treasury FIRST - this is required for the app to function
  // Strategy: Race-based loading for better UX during cold starts
  // 1. Try combined endpoint with quick timeout (5s)
  // 2. If slow, immediately load env separately (fast) while prices continue in background
  let envLoaded = false;
  
  // Create a timeout promise for quick fallback
  const quickTimeout = new Promise(resolve => setTimeout(() => resolve('timeout'), 5000));
  
  try {
    // Race between combined endpoint and quick timeout
    const combinedPromise = loadEnvAndPrices();
    const result = await Promise.race([combinedPromise, quickTimeout]);
    
    if (result === 'timeout') {
      // Combined endpoint is slow (cold start) - don't wait, load env separately NOW
      console.log('[init] Combined endpoint slow, falling back to separate env load');
      await loadEnv();
      envLoaded = true;
      // Let the original combined promise continue - it will update prices when done
      combinedPromise.then(loaded => {
        if (loaded) {
          console.log('[init] Background combined load completed');
          renderPricingTable();
        }
      }).catch(err => {
        console.warn('[init] Background combined load failed, starting dedicated prices fetch:', err);
        fetchPrices().catch(e => console.warn('[init] Dedicated prices fetch also failed:', e));
      });
    } else if (result) {
      // Combined endpoint responded quickly with success
      envLoaded = true;
    } else {
      // Combined endpoint responded but failed - fall back
      await loadEnv();
      envLoaded = true;
      fetchPrices().catch(err => {
        console.warn('[init] Prices fetch failed (non-critical):', err);
      });
    }
  } catch (err) {
    console.error('[init] Failed to load env/prices:', err);
    // Try fallback load - prioritize env/treasury over prices
    try {
      await loadEnv();
      envLoaded = true;
      // Prices are non-critical - continue even if they fail
      fetchPrices().catch(err => {
        console.warn('[init] Fallback prices fetch failed (non-critical):', err);
      });
    } catch (envErr) {
      console.error('[init] Critical: Failed to load env/treasury:', envErr);
      // Only throw if treasury/env fails - this is critical
      throw new Error(`Failed to initialize: ${envErr.message || envErr}`);
    }
  }
  
  // Ensure prices are loaded in background even if initial load failed
  if (!state.prices || Object.keys(state.prices).length === 0) {
    console.log('[init] Prices not loaded, attempting background fetch');
    // Retry prices with exponential backoff
    let retryCount = 0;
    const maxRetries = 3;
    const retryPrices = async () => {
      try {
        await fetchPrices();
      } catch (err) {
        retryCount++;
        if (retryCount < maxRetries) {
          const delay = Math.min(1000 * Math.pow(2, retryCount), 5000);
          console.log(`[init] Prices retry ${retryCount}/${maxRetries} in ${delay}ms`);
          setTimeout(retryPrices, delay);
        } else {
          console.warn('[init] Prices failed after all retries - app will continue');
        }
      }
    };
    setTimeout(retryPrices, 1000);
  }
  
  // Now that treasury is loaded, we can safely restore quote
  const hasStoredQuote = loadSessionFromStorage();
  const btn = $("quote-btn");
  if (hasStoredQuote) {
    // Restore quote UI if we have a valid stored quote
    try {
      if (!state.treasury) {
        console.warn("[init] Treasury not loaded, cannot restore quote. Clearing stored quote.");
        throw new Error("Treasury not available");
      }
      applyQuote(state.quote);
      startQuoteTimer();
      // applyQuote already sets button state to "Quote Active" and disabled
      console.log("[init] Successfully restored quote from localStorage");
      } catch (err) {
        console.error("[init] Failed to restore quote:", err.message || err);
        state.quote = null;
        setQuoteButtonState(true, "Get Quote");
        localStorage.removeItem("activeQuote");
      }
    } else {
      setQuoteButtonState(true, "Get Quote");
    }
  
  // If we have a stored purchaseId but no key yet, resume polling
  if (state.purchaseId && !$("api-key")?.value) {
    showKey({ status: "issuing", purchaseId: state.purchaseId });
    pollPurchaseUntilReady(state.purchaseId);
  }
  
  const shouldRefreshNow = !state.prices || Object.keys(state.prices).length === 0;
  startPricePolling({ immediate: shouldRefreshNow });
  updateVerifyButtonState();
  
  // Mark initialization as complete even if prices aren't loaded
  state.pricesLoading = false;
  console.log('[init] Initialization complete (env loaded:', envLoaded, ', prices:', !!state.prices && Object.keys(state.prices).length > 0, ')');
}

// Centralized initialization - eliminates duplication between loading/loaded states
function initializeApp() {
  setupEventHandlers();
  setQuoteButtonState(true, "Get Quote");
  init().catch(err => {
    console.error('[init] initialization failed', err);
    // Only show critical error if treasury/env failed - prices are non-critical
    const isCritical = err.message && (err.message.includes('treasury') || err.message.includes('env') || err.message.includes('Failed to initialize'));
    if (isCritical) {
      showAlert($("quote-status"), "error", "Server configuration unavailable. Please refresh the page.");
    } else {
      // Non-critical error - show warning but allow app to continue
      showAlert($("quote-status"), "warning", "Some features may be limited. Market data loading in background...");
      // Try to load prices in background
      setTimeout(() => {
        fetchPrices().catch(e => console.warn('[init] Background prices fetch failed:', e));
      }, 2000);
    }
    // Always enable quote button - backend will handle pricing
    setQuoteButtonState(true, "Get Quote");
  });
}

// Ensure DOM is ready before initializing
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initializeApp);
} else {
  // DOM already loaded
  initializeApp();
}














