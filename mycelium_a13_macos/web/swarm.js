// SPDX-License-Identifier: AGPL-3.0-only
'use strict';
// "Publish this run as a swarm" (owner): signs the swarm record and heartbeats to the
// public swarm directory, shows this Mac's swarm key fingerprint, and approves or
// declines access requests (owner-signed grants). Own status poll, like run.js.
(() => {
  const $ = (id) => document.getElementById(id);
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
      credentials: 'same-origin', redirect: 'error', referrerPolicy: 'no-referrer',
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Mycelium-Action': '1'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'request_failed');
    return value;
  }
  const ERRORS = {
    swarm_relay_required: 'Start the public relay first (Invite or join with a code → Start public relay).',
    swarm_directory_unreachable: 'The swarm directory is not reachable right now.',
    swarm_owner_in_browser: 'This swarm\'s owner key lives in the browser that created it: approve requests there.',
    swarm_record_owner_mismatch: 'The directory returned a swarm with a different owner than the link. Not attached.',
    swarm_link_invalid: 'That is not a swarm link (it starts with mycelium-swarm:).',
  };
  const explain = (code) => (ERRORS[code] ? `${ERRORS[code]} (${code})` : `Action not completed: ${String(code).replaceAll('_', ' ')} (${code}).`);
  let lastError = null;  // the last refused action stays visible until the next one succeeds
  function render(status) {
    const card = $('swarm-publish'); if (!card) return;
    card.hidden = status.installation_role !== 'owner';
    const swarm = status.swarm || {};
    const published = swarm.enabled === true;
    $('swarm-key').textContent = swarm.entry_fingerprint ? `This Mac's swarm key: ${swarm.entry_fingerprint}` : '';
    let text = 'Not published. The run stays private to your Macs.';
    if (published && swarm.mode === 'link' && !swarm.attached) {
      text = `Offered to "${swarm.swarm_id}". In the browser that created it, attach the Mac with fingerprint ${swarm.entry_fingerprint}.`;
    } else if (published) {
      text = `Published "${swarm.name}" · ${swarm.heartbeat || 'starting'} · access ${swarm.access_mode} · entry ${swarm.entry_origin || '—'}`;
    }
    if (swarm.error_code) text += ` · ${explain(swarm.error_code)}`;
    if (lastError) text += ` · Last action refused: ${explain(lastError)}`;
    $('swarm-state').textContent = text;
    $('swarm-publish-btn').disabled = published; $('swarm-attach').disabled = published;
    $('swarm-unpublish').disabled = !published;
    $('swarm-owner-tools').hidden = !(published && swarm.mode === 'app');
    $('swarm-link-out').textContent = published && swarm.mode === 'app' && swarm.link ? swarm.link : '';
  }
  async function refresh() { try { render(await api('/api/status')); } catch (error) { /* app.js shows connectivity */ } }
  async function act(path, body, after) {
    try { const value = await api(path, body); lastError = null; if (after) after(value); }
    catch (error) { lastError = error.message; }
    refresh();
  }
  function quota() { return {max_requests: Number($('swarm-req').value), max_tokens: Number($('swarm-tok').value), ttl_seconds: 86400}; }
  function requestRow(request) {
    const row = document.createElement('div'); row.className = 'swarm-request';
    const label = document.createElement('p');
    label.textContent = `Key ${request.requester_fingerprint} · ${request.state}${request.note ? ' · “' + request.note + '”' : ''}`;
    row.append(label);
    if (request.state === 'pending') {
      const approve = document.createElement('button'); approve.type = 'button'; approve.textContent = 'Approve';
      approve.dataset.approve = request.requester_fingerprint;
      approve.addEventListener('click', () => act('/api/swarm/approve', {requester_key: request.requester_key,
        hours: Number($('swarm-hours').value), max_requests: Number($('swarm-req').value),
        max_tokens: Number($('swarm-tok').value)}, loadInbox));
      const deny = document.createElement('button'); deny.type = 'button'; deny.className = 'secondary'; deny.textContent = 'Decline';
      deny.addEventListener('click', () => act('/api/swarm/deny', {requester_key: request.requester_key}, loadInbox));
      row.append(approve, deny);
    }
    return row;
  }
  async function loadInbox() {
    try {
      const value = await api('/api/swarm/inbox', {});
      const rows = value.requests.map(requestRow);
      if (!rows.length) { const p = document.createElement('p'); p.className = 'detail'; p.textContent = 'No access requests yet.'; rows.push(p); }
      $('swarm-requests').replaceChildren(...rows);
    } catch (error) { $('swarm-requests').textContent = explain(error.message); }
  }
  document.addEventListener('DOMContentLoaded', () => {
    if (!$('swarm-publish')) return;
    $('swarm-publish-btn').addEventListener('click', () => {
      const mode = $('swarm-mode').value;
      const body = {directory: $('swarm-directory').value.trim(), name: $('swarm-name').value.trim(), access_mode: mode,
        description: $('swarm-desc').value.trim()};
      if (mode === 'open') body.open_quota = quota();
      act('/api/swarm/publish', body);
    });
    $('swarm-attach').addEventListener('click', () => act('/api/swarm/publish', {link: $('swarm-link').value.trim()}));
    $('swarm-unpublish').addEventListener('click', () => act('/api/swarm/unpublish', $('swarm-delete').checked ? {delete: true} : {}));
    $('swarm-inbox').addEventListener('click', loadInbox);
    refresh(); setInterval(refresh, 5000);
  });
})();
