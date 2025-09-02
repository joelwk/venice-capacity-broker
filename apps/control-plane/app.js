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

    // Initial loads
    refreshHealth();
    refreshEnv();
    if (getToken()) refreshTenants();
  }

  document.addEventListener('DOMContentLoaded', init);
})();

