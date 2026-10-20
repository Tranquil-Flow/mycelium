'use strict';
// "Use this private swarm from your own tools" (owner): OpenAI-compatible API keys, quotas and the two
// exposure settings (this Mac's loopback port, and the swarm's own relay address).  Own poll, like swarm.js.
(() => {
  const $ = (id) => document.getElementById(id);
  async function api(path, body) {
    const response = await fetch(path, {method: 'POST', cache: 'no-store', credentials: 'same-origin',
      redirect: 'error', referrerPolicy: 'no-referrer',
      headers: {'Content-Type': 'application/json', 'X-Mycelium-Action': '1'}, body: JSON.stringify(body || {})});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'request_failed');
    return value;
  }
  const ERRORS = {
    openai_port_in_use: 'That port is already used by another program. Pick another port.',
    openai_port_invalid: 'The port must be 0 (automatic) or between 1024 and 65535.',
    openai_relay_consent_required: 'Tick the box that says you understand the relay can see requests, replies and your key.',
    key_label_invalid: 'Give the key a short label (up to 64 characters, no line breaks).',
    key_quota_invalid: 'Quota values must be whole numbers above zero (window: 1 minute to 30 days).',
    key_limit_reached: 'You already have 32 keys. Revoke one first.',
    key_unknown: 'That key no longer exists.',
    owner_role_required: 'Only the swarm owner manages API keys.',
  };
  const explain = (code) => (ERRORS[code] ? `${ERRORS[code]} (${code})` : `Not completed: ${String(code).replaceAll('_', ' ')} (${code}).`);
  let loaded = false;
  let lastError = null;
  const number = (id) => { const raw = $(id).value.trim(); return raw === '' ? null : Number(raw); };
  const windowText = (seconds) => (seconds % 86400 === 0 ? `${seconds / 86400} day(s)` : seconds % 3600 === 0 ? `${seconds / 3600} hour(s)` : `${seconds / 60} minute(s)`);
  function keyRow(key) {
    const row = document.createElement('div'); row.className = 'openai-key';
    const quota = key.quota; const usage = key.usage;
    const limits = [quota.requests_per_window ? `${usage.requests}/${quota.requests_per_window} requests` : null,
      quota.tokens_per_window ? `${usage.tokens}/${quota.tokens_per_window} tokens` : null].filter(Boolean);
    const text = document.createElement('p');
    text.textContent = `${key.label} · ${key.prefix}… · ${key.revoked_at ? 'revoked' : (limits.length ? limits.join(', ') + ' per ' + windowText(quota.window_seconds) : 'no limits')}`;
    row.append(text);
    if (!key.revoked_at) {
      const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'secondary';
      revoke.textContent = 'Revoke'; revoke.dataset.revoke = key.id;
      revoke.addEventListener('click', () => {
        if (!window.confirm(`Revoke "${key.label}"? Tools using it stop working at once.`)) return;
        act('/api/openai/key/revoke', {id: key.id});
      });
      row.append(revoke);
    }
    return row;
  }
  function render(status) {
    const card = $('openai-api'); if (!card) return;
    card.hidden = false;
    const settings = status.settings;
    if (!loaded) {
      $('openai-enabled').checked = settings.enabled; $('openai-port').value = settings.port || '';
      $('openai-relay').checked = settings.expose_via_relay; loaded = true;
    }
    let text = status.running ? `Serving at ${status.url} (this Mac only). Model: ${status.model_id || 'not chosen yet'}.` : 'Off.';
    if (settings.expose_via_relay) text += status.relay_url ? ` Also on the relay: ${status.relay_url}` : ' Relay access is on; it starts answering when the public relay is running.';
    if (lastError) text += ` · ${explain(lastError)}`;
    $('openai-state').textContent = text;
    const list = $('openai-keys'); list.replaceChildren(...status.keys.map(keyRow));
  }
  async function refresh() {
    try { render(await api('/api/openai/status')); } catch (error) { if (error.message === 'owner_role_required') $('openai-api').hidden = true; }
  }
  async function act(path, body, after) {
    try { const value = await api(path, body); lastError = null; if (after) after(value); }
    catch (error) { lastError = error.message; }
    refresh();
  }
  document.addEventListener('DOMContentLoaded', () => {
    if (!$('openai-api')) return;
    $('openai-save').addEventListener('click', () => {
      const port = $('openai-port').value.trim();
      const body = {enabled: $('openai-enabled').checked, expose_via_relay: $('openai-relay').checked};
      if (port !== '') body.port = Number(port);
      if (body.expose_via_relay) body.relay_acknowledged = $('openai-relay-ack').checked;
      act('/api/openai/settings', body);
    });
    $('openai-create').addEventListener('click', () => {
      const body = {label: $('openai-label').value, window_seconds: Number($('openai-window').value)};
      if (number('openai-req') !== null) body.requests_per_window = number('openai-req');
      if (number('openai-tok') !== null) body.tokens_per_window = number('openai-tok');
      act('/api/openai/key/create', body, (value) => {
        $('openai-secret').textContent = value.secret; $('openai-secret-box').hidden = false; $('openai-label').value = '';
      });
    });
    $('openai-secret-done').addEventListener('click', () => { $('openai-secret').textContent = ''; $('openai-secret-box').hidden = true; });
    refresh();
    setInterval(refresh, 5000);
  });
})();
