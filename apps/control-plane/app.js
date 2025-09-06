// Minimal Admin UI script
// Stores admin token in localStorage and calls /v1/* endpoints

(function () {
  const qs = (sel) => document.querySelector(sel);
  const qsa = (sel) => Array.from(document.querySelectorAll(sel));

  const storageKey = 'adminToken';
  function getToken() {
    return localStorage.getItem(storageKey) || '';
  }
  function setToken(tok) {
    if (tok) localStorage.setItem(storageKey, tok);
    else localStorage.removeItem(storageKey);
    updateAuthStatus();
  }

  function headers(extra = {}) {
    const h = { ...extra };
    const tok = getToken();
    if (tok) h['Authorization'] = 'Bearer ' + tok;
    return h;
  }

  async function fetchJSON(path, opts = {}) {
    const res = await fetch(path, {
      ...opts,
      headers: headers({ 'Content-Type': 'application/json', ...(opts.headers || {}) }),
    });
    const ct = res.headers.get('content-type') || '';
    const body = ct.includes('application/json') ? await res.json().catch(() => ({})) : await res.text();
    if (!res.ok) {
      const msg = typeof body === 'string' ? body : (body && body.detail) || JSON.stringify(body);
      throw new Error(`${res.status} ${res.statusText}: ${msg}`);
    }
    return body;
  }

  function setText(id, text) {
    const el = qs(id);
    if (el) el.textContent = text;
  }
  function setJSON(id, obj) {
    const el = qs(id);
    if (el) el.textContent = JSON.stringify(obj, null, 2);
  }

  // --- Tokens panel ---
  function renderTokensTable(list) {
    const host = qs('#tokensTable');
    if (!host) return;
    if (!Array.isArray(list) || list.length === 0) {
      host.innerHTML = '<div class="status">No tokens</div>';
      return;
    }
    const rows = list
      .map(
        (t) => `
        <tr>
          <td><code>${t.address}</code></td>
          <td>${t.symbol || ''}</td>
          <td>${t.name || ''}</td>
          <td>${t.decimals ?? ''}</td>
          <td>${t.priceUsd != null ? Number(t.priceUsd).toFixed(6) : ''}</td>
          <td>${t.holders ?? ''}</td>
          <td>${t.transfers24h ?? ''}</td>
          <td>${t.lastTs || ''}</td>
        </tr>`
      )
      .join('');
    host.innerHTML = `
      <table>
        <thead><tr><th>Address</th><th>Symbol</th><th>Name</th><th>Dec</th><th>Price USD</th><th>Holders</th><th>Tx 24h</th><th>Last</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function refreshTokens() {
    try {
      const j = await fetchJSON('/v1/market/tokens');
      renderTokensTable(j);
    } catch (e) {
      const host = qs('#tokensTable');
      if (host) host.innerHTML = `<div class="status error">${String(e)}</div>`;
    }
  }

  function drawLineChart(canvas, points) {
    if (!canvas || !canvas.getContext) return;
    const ctx = canvas.getContext('2d');
    const w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);
    if (!Array.isArray(points) || points.length === 0) return;
    const xs = points.map(p => (new Date(p.ts)).getTime());
    const ys = points.map(p => (p.priceUsd == null ? null : Number(p.priceUsd))).filter(v => v != null);
    if (ys.length === 0) return;
    const xmin = Math.min(...xs), xmax = Math.max(...xs);
    const ymin = Math.min(...ys), ymax = Math.max(...ys);
    const pad = 10;
    function xscale(x){ return pad + (w - 2*pad) * ((x - xmin) / (xmax - xmin || 1)); }
    function yscale(y){ return h - pad - (h - 2*pad) * ((y - ymin) / (ymax - ymin || 1)); }
    ctx.strokeStyle = '#8ab4f8'; ctx.lineWidth = 2; ctx.beginPath();
    points.forEach((p, i) => {
      if (p.priceUsd == null) return;
      const X = xscale((new Date(p.ts)).getTime());
      const Y = yscale(Number(p.priceUsd));
      if (i === 0) ctx.moveTo(X, Y); else ctx.lineTo(X, Y);
    });
    ctx.stroke();
  }

  async function loadTokenHistory() {
    const addr = (qs('#tokenAddr')?.value || '').trim();
    const out = qs('#tokenHistoryOut');
    if (!addr) { if (out) out.textContent = 'Enter token address'; return; }
    try {
      // since default = 24h window on server; keep it simple
      const j = await fetchJSON(`/v1/market/token/${encodeURIComponent(addr)}/history?asc=true&limit=500`);
      if (out) setJSON('#tokenHistoryOut', j);
      drawLineChart(qs('#tokenChart'), j);
    } catch (e) {
      if (out) out.textContent = String(e);
    }
  }

  function updateAuthStatus() {
    const tok = getToken();
    const masked = tok ? tok.slice(0, 3) + '…' + tok.slice(-4) : '(none)';
    setText('#authStatus', tok ? `Token set: ${masked}` : 'No token set');
    const input = qs('#adminToken');
    if (input && tok && !input.value) input.value = tok;
  }

  // Health & Env
  async function refreshHealth() {
    try {
      const h = await fetchJSON('/health', { headers: headers() });
      setJSON('#healthOut', h);
    } catch (e) {
      setText('#healthOut', String(e));
    }
  }
  async function refreshEnv() {
    try {
      const env = await fetchJSON('/v1/env', { headers: headers() });
      setJSON('#envOut', env);
    } catch (e) {
      setText('#envOut', String(e));
    }
  }

  // Venice card
  function renderVeniceStatus(env) {
    const st = qs('#veniceStatus');
    if (!st) return;
    try {
      const v = (env && env.venice) || {};
      const base = v.baseUrl || '(unset)';
      const vvv = v.vvvPath || '/vvv';
      const off = !!v.offlineSignals;
      const sig = (env && env.signals) || {};
      const sigOffline = !!sig.offline;
      const ready = (typeof v.ready === 'boolean') ? v.ready : null;
      let msg = `Base: ${base} | VVV: ${vvv}`;
      if (!base || base === '(unset)') {
        msg += ' — Missing VENICE_API_BASE_URL';
        st.classList.add('error');
      } else if (off || sigOffline) {
        msg += ' — Signals: OFFLINE (dev mode)';
        st.classList.remove('error');
      } else if (ready === false) {
        msg += ' — Venice: NOT READY';
        st.classList.add('error');
      } else {
        st.classList.remove('error');
      }
      st.textContent = msg;
    } catch {
      st.textContent = '';
    }
  }

  async function refreshVenice() {
    try {
      const env = await fetchJSON('/v1/env', { headers: headers() });
      // Config snapshot
      setJSON('#veniceCfgOut', env.venice || {});
      renderVeniceStatus(env);
      // Recent signals
      const sig = (env.signals && env.signals.recent) ? env.signals.recent : [];
      setJSON('#veniceSignalsOut', sig);
    } catch (e) {
      setText('#veniceCfgOut', String(e));
      setText('#veniceSignalsOut', '');
      setText('#veniceStatus', '');
    }
  }

  // Quotes & Purchases (admin receipts UI)
  async function refreshQuotes() {
    try {
      const j = await fetchJSON('/v1/admin/quotes');
      setJSON('#quotesOut', j);
    } catch (e) {
      setText('#quotesOut', String(e));
    }
  }
  async function refreshPurchases() {
    try {
      const j = await fetchJSON('/v1/admin/purchases');
      setJSON('#purchasesOut', j);
      renderPurchasesTable(j);
    } catch (e) {
      setText('#purchasesOut', String(e));
    }
  }

  function renderPurchasesTable(list) {
    const host = qs('#purchasesTable');
    if (!host) return;
    if (!Array.isArray(list) || list.length === 0) {
      host.innerHTML = '<div class="status">No purchases</div>';
      return;
    }
    const rows = list.map((p) => `
      <tr>
        <td>${p.purchaseId}</td>
        <td>${p.quoteId}</td>
        <td>${p.asset}</td>
        <td>${p.amountPaid}</td>
        <td>${p.status}</td>
        <td>${p.expiresAt || ''}</td>
      </tr>`).join('');
    host.innerHTML = `
      <table>
        <thead><tr><th>Purchase</th><th>Quote</th><th>Asset</th><th>Amount Paid</th><th>Status</th><th>Expires</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function probeVenice() {
    const base = (qs('#veniceBaseUrl')?.value || '').trim();
    setText('#veniceProbeOut', '');
    try {
      const params = base ? ('?base=' + encodeURIComponent(base)) : '';
      const res = await fetchJSON('/v1/admin/venice/probe' + params);
      setJSON('#veniceProbeOut', res);
      // Also update the venice config panel so ops see new recommendations immediately
      await refreshVenice();
    } catch (e) {
      setText('#veniceProbeOut', String(e));
    }
  }

  // Tenants
  function renderTenantsTable(list) {
    if (!Array.isArray(list)) {
      qs('#tenantsTable').innerHTML = '<div class="status">No tenants</div>';
      return;
    }
    const rows = list
      .map(
        (t) => `
        <tr>
          <td>${t.id}</td>
          <td>${t.label || ''}</td>
          <td>${t.quota}</td>
          <td>${t.expires_at || ''}</td>
          <td>${t.status}</td>
        </tr>`
      )
      .join('');
    qs('#tenantsTable').innerHTML = `
      <table>
        <thead><tr><th>Id</th><th>Label</th><th>Quota</th><th>Expires</th><th>Status</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  async function refreshTenants() {
    try {
      const list = await fetchJSON('/v1/tenants');
      renderTenantsTable(list);
    } catch (e) {
      qs('#tenantsTable').innerHTML = `<div class="status error">${String(e)}</div>`;
    }
  }

  async function createTenant() {
    const id = qs('#createTenantId').value.trim();
    const label = qs('#createTenantLabel').value.trim();
    const quotaStr = qs('#createTenantQuota').value.trim();
    const expiresAt = qs('#createTenantExpiresAt').value.trim();
    const quota = quotaStr ? Number(quotaStr) : 0;
    setText('#createTenantMsg', '');
    try {
      if (!id || !label) throw new Error('tenant_id and label are required');
      const body = { tenant_id: id, label, quota };
      if (expiresAt) body.expires_at = expiresAt;
      const res = await fetchJSON('/v1/tenants', { method: 'POST', body: JSON.stringify(body) });
      setText('#createTenantMsg', `Created: ${res.id}`);
      await refreshTenants();
    } catch (e) {
      setText('#createTenantMsg', String(e));
    }
  }

  async function rotateTenant() {
    const id = qs('#rotateTenantId').value.trim();
    const label = qs('#rotateTenantLabel').value.trim();
    const quotaStr = qs('#rotateTenantQuota').value.trim();
    const expiresAt = qs('#rotateTenantExpiresAt').value.trim();
    const revokeOld = qs('#rotateRevokeOld').checked;
    setText('#rotateTenantMsg', '');
    try {
      if (!id) throw new Error('tenant_id is required');
      const body = { tenant_id: id };
      if (label) body.label = label;
      if (quotaStr) body.quota = Number(quotaStr);
      if (expiresAt) body.expires_at = expiresAt;
      const url = `/v1/tenants?rotate=true&revoke_old=${revokeOld ? 'true' : 'false'}`;
      const res = await fetchJSON(url, { method: 'POST', body: JSON.stringify(body) });
      setText('#rotateTenantMsg', `Rotated: ${res.id}`);
      await refreshTenants();
    } catch (e) {
      setText('#rotateTenantMsg', String(e));
    }
  }

  async function revokeTenant() {
    const id = qs('#revokeTenantId').value.trim();
    setText('#revokeTenantMsg', '');
    try {
      if (!id) throw new Error('tenant_id is required');
      const res = await fetchJSON(`/v1/tenants/${encodeURIComponent(id)}/revoke`, { method: 'POST' });
      setText('#revokeTenantMsg', `Revoked: ${res.tenant}`);
      await refreshTenants();
    } catch (e) {
      setText('#revokeTenantMsg', String(e));
    }
  }

  async function inspectTenant() {
    const id = qs('#inspectTenantId').value.trim();
    try {
      if (!id) throw new Error('tenant_id is required');
      const res = await fetchJSON(`/v1/tenants/${encodeURIComponent(id)}`);
      setJSON('#inspectTenantOut', res);
    } catch (e) {
      setText('#inspectTenantOut', String(e));
    }
  }

  // Limits
  async function loadLimits() {
    const id = qs('#limitsTenantId').value.trim();
    try {
      if (!id) throw new Error('tenant_id is required');
      const res = await fetchJSON(`/v1/tenants/${encodeURIComponent(id)}/broker-limits`);
      setJSON('#limitsOut', res);
    } catch (e) {
      setText('#limitsOut', String(e));
    }
  }

  async function setLimits() {
    const id = qs('#setLimitsTenantId').value.trim();
    const windowStr = qs('#setLimitsWindow').value.trim();
    const maxStr = qs('#setLimitsMax').value.trim();
    setText('#setLimitsMsg', '');
    try {
      if (!id) throw new Error('tenant_id is required');
      const body = {};
      if (windowStr) body.windowSeconds = Number(windowStr);
      if (maxStr) body.maxRequests = Number(maxStr);
      const res = await fetchJSON(`/v1/tenants/${encodeURIComponent(id)}/broker-limits`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      setText('#setLimitsMsg', 'Saved');
      setJSON('#limitsOut', res);
    } catch (e) {
      setText('#setLimitsMsg', String(e));
    }
  }

  // Chat probe
  async function sendChat() {
    const id = qs('#chatTenantId').value.trim();
    const model = qs('#chatModel').value.trim();
    const msg = qs('#chatMessage').value.trim();
    try {
      if (!id) throw new Error('tenant_id is required');
      if (!msg) throw new Error('message is required');
      const body = { messages: [{ role: 'user', content: msg }] };
      if (model) body.model = model;
      const res = await fetchJSON('/v1/chat', {
        method: 'POST',
        headers: headers({ 'X-Tenant-Id': id }),
        body: JSON.stringify(body),
      });
      setJSON('#chatOut', res);
    } catch (e) {
      setText('#chatOut', String(e));
    }
  }

  function bind(id, evt, fn) {
    const el = qs(id);
    if (el) el.addEventListener(evt, fn);
  }

  function init() {
    // Auth
    qs('#adminToken').value = getToken();
    bind('#saveTokenBtn', 'click', () => setToken(qs('#adminToken').value.trim()));
    bind('#clearTokenBtn', 'click', () => {
      qs('#adminToken').value = '';
      setToken('');
    });
    updateAuthStatus();

    // Health / Env
    bind('#refreshHealthBtn', 'click', refreshHealth);
    bind('#refreshEnvBtn', 'click', refreshEnv);
    bind('#refreshVeniceBtn', 'click', refreshVenice);
    bind('#probeVeniceBtn', 'click', probeVenice);

    // Tenants
    bind('#refreshTenantsBtn', 'click', refreshTenants);
    bind('#createTenantBtn', 'click', createTenant);
    bind('#rotateTenantBtn', 'click', rotateTenant);
    bind('#revokeTenantBtn', 'click', revokeTenant);
    bind('#inspectTenantBtn', 'click', inspectTenant);

    // Limits
    bind('#loadLimitsBtn', 'click', loadLimits);
    bind('#setLimitsBtn', 'click', setLimits);

    // Chat
    bind('#sendChatBtn', 'click', sendChat);

    // Tokens
    bind('#refreshTokensBtn', 'click', refreshTokens);
    bind('#loadTokenHistoryBtn', 'click', loadTokenHistory);

    // Quotes & Purchases
    bind('#refreshQuotesBtn', 'click', refreshQuotes);
    bind('#refreshPurchasesBtn', 'click', refreshPurchases);

    // Initial loads
    refreshHealth();
    refreshEnv();
    refreshVenice();
    if (getToken()) {
      refreshTenants();
      refreshQuotes();
      refreshPurchases();
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();

