'use strict';
// Diagnostics card (PV26): reads /api/diagnostics, says in plain words what is wrong and how to fix it, and offers
// the redacted support bundle. Read-only: nothing here changes the swarm. Own poll, like run.js and swarm.js.
(() => {
  const $ = (id) => document.getElementById(id);
  async function api(path) {
    const response = await fetch(path, {method: 'GET', cache: 'no-store', credentials: 'same-origin',
      redirect: 'error', referrerPolicy: 'no-referrer'});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'request_failed');
    return value;
  }
  function node(tag, text, className) {
    const element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  const secs = (value) => (typeof value === 'number' ? value.toFixed(1) : '?');
  function clockText(label, clock) {
    if (!clock || !clock.available) return `${label}: ${clock && clock.reason ? clock.reason : 'not measured yet'}`;
    const size = Math.abs(clock.offset_s);
    const spread = typeof clock.uncertainty_s === 'number' ? ` (±${secs(clock.uncertainty_s)} s)` : '';
    if (clock.direction === 'in_sync') return `${label}: in sync${spread}`;
    return `${label}: ${secs(size)} s ${clock.direction}${spread}`;
  }
  function liveText(live) {
    if (!live || !live.available) return `Health checks: ${live && live.reason ? live.reason : 'not available'}`;
    const seen = typeof live.last_seen_seconds_ago === 'number' ? `, last heard ${Math.round(live.last_seen_seconds_ago)} s ago` : '';
    return `Health checks: ${live.label}${seen}`;
  }
  function mailText(mail) {
    if (!mail || !mail.available) return `Commands: ${mail && mail.reason ? mail.reason : 'not available'}`;
    const c = mail.counts || {};
    const latency = typeof mail.median_claim_latency_s === 'number' ? `, claimed after ${secs(mail.median_claim_latency_s)} s (median)` : '';
    return `Commands (recent): ${c.completed || 0} completed, ${c.expired || 0} expired, ` +
      `${c.reconciliation_required || 0} reconciliation required${latency}`;
  }
  function renderFindings(findings) {
    const box = $('diag-findings');
    box.replaceChildren(...findings.map((item) => {
      const div = document.createElement('div');
      div.className = `finding ${item.severity}`;
      div.append(node('p', item.title, 'finding-title'), node('p', item.detail, 'detail'), node('p', `Fix: ${item.fix}`));
      return div;
    }));
  }
  function renderPeers(peers) {
    $('diag-peers').replaceChildren(...peers.map((peer) => {
      const div = document.createElement('div');
      const seen = peer.last_seen && typeof peer.last_seen.membership_age_s === 'number'
        ? `Last heard by the swarm ${Math.round(peer.last_seen.membership_age_s)} s ago` : 'Last heard: unknown';
      div.append(node('h3', peer.name), node('p', clockText('Clock', peer.clock), 'detail'),
        node('p', liveText(peer.liveness), 'detail'), node('p', seen, 'detail'), node('p', mailText(peer.mailbox), 'detail'));
      return div;
    }));
  }
  function renderMember(doc) {
    const box = $('diag-member');
    box.hidden = doc.role !== 'target' || !doc.member_view;
    if (box.hidden) { box.replaceChildren(); return; }
    const loop = doc.member_view.claim_loop || {};
    const errors = Object.entries(loop.errors || {}).map(([code, n]) => `${code} ×${n}`).join(', ');
    box.replaceChildren(node('h3', 'This Mac and the owner'),
      node('p', clockText('Owner clock', doc.member_view.owner_clock), 'detail'),
      node('p', `Calls to the owner: ${loop.polls || 0} polls, median ${typeof loop.median_poll_seconds === 'number' ? secs(loop.median_poll_seconds) + ' s' : 'n/a'}; ` +
        `acks median ${typeof loop.median_ack_seconds === 'number' ? secs(loop.median_ack_seconds) + ' s' : 'n/a'}${errors ? '; errors: ' + errors : ''}`, 'detail'));
  }
  function renderRelay(relay) {
    if (!relay || relay.state === 'private') { $('diag-relay').textContent = 'Relay: none (this swarm stays on this Mac).'; return; }
    const left = typeof relay.expires_in_seconds === 'number' ? `, expires in ${Math.max(0, Math.round(relay.expires_in_seconds / 60))} min` : '';
    const code = relay.error_code ? `, ${relay.error_code}` : '';
    $('diag-relay').textContent = `Relay: ${String(relay.state).replaceAll('_', ' ')} (${relay.provider || 'unknown'})${left}${code}` +
      (relay.renewals ? `, renewed ${relay.renewals}×` : '');
  }
  function renderTimings(timings) {
    if (timings && timings.available) {
      const r = timings.last_request;
      $('diag-timings').textContent = `Last request: ${r.tokens_per_second ?? '?'} tok/s overall, first token after ${r.ttft_ms ?? '?'} ms, ` +
        `${r.decode_tokens_per_second ?? '?'} tok/s while decoding. Per-hop times: ${timings.per_hop.reason}`;
    } else {
      $('diag-timings').textContent = `Timings: not available (${timings ? timings.reason : 'unknown'}). Per-hop times: ${timings && timings.per_hop ? timings.per_hop.reason : 'not available'}`;
    }
  }
  function renderChats(chats) {
    const recent = (chats && chats.recent) || [];
    const failed = recent.filter((c) => c.terminal === 'failed').length;
    const codes = Object.entries((chats && chats.error_codes) || {}).map(([code, n]) => `${code} ×${n}`).join(', ');
    $('diag-chats').textContent = recent.length
      ? `Recent chats: ${recent.length}, ${failed} failed${codes ? ' (' + codes + ')' : ''}` : 'Recent chats: none yet.';
  }
  function renderHost(host) {
    const load = Array.isArray(host.load_average) ? host.load_average.join(' / ') : 'n/a';
    const disk = typeof host.disk_free_bytes === 'number' ? `${(host.disk_free_bytes / 1e9).toFixed(1)} GB disk free` : 'disk free n/a';
    const mds = typeof host.spotlight_mds_cpu_percent === 'number' ? `${host.spotlight_mds_cpu_percent}% CPU` : 'n/a';
    $('diag-host').textContent = `This Mac: load ${load}, memory pressure ${host.memory_pressure || 'unknown'}, Spotlight ${mds}, ${disk}`;
  }
  function render(doc) {
    const findings = doc.findings || [];
    $('diag-summary').textContent = findings.length
      ? `${findings.length} problem${findings.length === 1 ? '' : 's'} found.` : 'No problems found.';
    renderFindings(findings);
    renderPeers(doc.peers || []);
    renderMember(doc);
    renderRelay(doc.relay);
    renderTimings(doc.timings);
    renderChats(doc.chats);
    renderHost(doc.host || {});
  }
  async function refresh() {
    try { render(await api('/api/diagnostics')); }
    catch (error) { $('diag-summary').textContent = `Diagnostics unavailable (${error.message}).`; }
  }
  const button = $('diag-refresh');
  if (button) button.addEventListener('click', refresh);
  refresh();
  setInterval(refresh, 10000);
})();
