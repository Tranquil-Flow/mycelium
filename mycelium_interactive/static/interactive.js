import { BrowserPixelStage, matrixDigest } from './pixelStage.js';
import { decodeEvidenceRecord, decodeOperatorStatus } from './operatorContract.js';

const app = document.querySelector('#app');
const state = {
  operatorToken: null,
  status: null,
  invites: [],
  record: null,
  message: '',
  error: '',
  busy: false,
  activeRequestId: null,
  cancellationRequested: false,
  cancelledRequestIds: new Set(),
  statusRefreshing: false,
  draft: {
    prompt: 'moonlit swarm',
    maxNewTokens: '2',
    inviteCount: '2',
  },
  peer: {
    mode: false,
    state: 'idle',
    peerId: null,
    sessionToken: null,
    stage: null,
    completed: 0,
    lastJob: null,
    error: '',
    running: false,
    pollController: null,
  },
};

function h(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
}

function short(value) {
  if (!value) return 'unavailable';
  return value.length > 28 ? `${value.slice(0, 18)}…${value.slice(-8)}` : value;
}

function formatUnix(value) {
  return value ? new Date(value * 1000).toLocaleTimeString() : 'unknown';
}

function scientific(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toExponential(3) : 'unavailable';
}

async function readJson(response) {
  const doc = await response.json().catch(() => null);
  if (!response.ok) throw new Error(doc?.error ?? `http_${response.status}`);
  if (!doc || doc.ok !== true) throw new Error('interactive_api_invalid_response');
  return doc;
}

async function post(path, body, options = {}) {
  return readJson(await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
    credentials: 'same-origin',
    signal: options.signal,
  }));
}

function operatorHeaders() {
  if (!state.operatorToken) throw new Error('operator_capability_missing');
  return { authorization: `Bearer ${state.operatorToken}` };
}

async function operatorPost(path, body) {
  return readJson(await fetch(path, {
    method: 'POST',
    headers: { ...operatorHeaders(), 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
    credentials: 'same-origin',
  }));
}

async function operatorGet(path) {
  return readJson(await fetch(path, {
    method: 'GET',
    headers: operatorHeaders(),
    cache: 'no-store',
    credentials: 'same-origin',
  }));
}

function fragmentCapability(kind) {
  const fragment = window.location.hash.replace(/^#/, '');
  const prefix = `${kind}/`;
  if (!fragment.startsWith(prefix)) return null;
  const token = fragment.slice(prefix.length);
  return token.length > 0 ? token : null;
}

async function refreshStatus() {
  if (state.peer.mode || state.statusRefreshing) return;
  state.statusRefreshing = true;
  const previousStatus = JSON.stringify(state.status);
  const previousRecordId = state.record?.request_id ?? null;
  const previousError = state.error;
  try {
    state.status = decodeOperatorStatus((await operatorGet('/api/interactive/status')).status);
    if (!state.record && state.status.recent_requests?.length) {
      state.record = state.status.recent_requests.at(-1);
    }
    state.error = '';
  } catch (error) {
    state.error = error instanceof Error ? error.message : 'status_failed';
  } finally {
    state.statusRefreshing = false;
  }
  if (
    JSON.stringify(state.status) !== previousStatus
    || (state.record?.request_id ?? null) !== previousRecordId
    || state.error !== previousError
  ) render();
}

function selectedDeviceTarget() {
  const requested = Number(state.draft.inviteCount);
  return Number.isInteger(requested) && requested >= 1 && requested <= 6 ? requested : 2;
}

async function createInvites() {
  const count = selectedDeviceTarget();
  state.busy = true;
  state.message = '';
  state.error = '';
  render();
  const firstDevice = state.invites.length + 1;
  try {
    for (let index = 0; index < count; index += 1) {
      const invite = (await operatorPost('/api/interactive/invite', { ttl_seconds: 300 })).invite;
      state.invites.push(invite);
    }
    const lastDevice = state.invites.length;
    state.message = count === 1
      ? `Device ${lastDevice} link created. Open it once on that device.`
      : `Device ${firstDevice}–${lastDevice} links created. Open each link once on a different device.`;
  } catch (error) {
    state.error = error instanceof Error ? error.message : 'invite_failed';
    if (state.invites.length >= firstDevice) {
      state.message = `${state.invites.length - firstDevice + 1} link(s) created before the error.`;
    }
  }
  state.busy = false;
  render();
  await refreshStatus();
}

async function copyInvite(index) {
  const invite = state.invites[index];
  if (!invite) return;
  try {
    await navigator.clipboard.writeText(invite.url);
    state.message = `Device ${index + 1} link copied.`;
  } catch {
    state.message = `Copy failed for device ${index + 1}; select the URL manually.`;
  }
  render();
}

async function runInference(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const requiredDistinctPeers = selectedDeviceTarget();
  state.draft.prompt = String(form.get('prompt') ?? '');
  state.draft.maxNewTokens = String(form.get('max_new_tokens') ?? '1');
  const requestId = `request-${crypto.randomUUID()}`;
  state.activeRequestId = requestId;
  state.cancellationRequested = false;
  state.busy = true;
  state.message = 'Bounded synthetic matrix exercise running through joined browser workers…';
  state.error = '';
  render();
  try {
    const record = decodeEvidenceRecord((await operatorPost('/api/interactive/infer', {
      prompt: state.draft.prompt,
      max_new_tokens: Number(state.draft.maxNewTokens),
      required_distinct_peers: requiredDistinctPeers,
      request_id: requestId,
    })).record);
    if (state.activeRequestId === requestId) {
      state.record = record;
      state.message = `Local matrix exercise completed with ${record.observed_distinct_peers}/${record.required_distinct_peers} distinct peer sessions contributing.`;
    }
  } catch (error) {
    const code = error instanceof Error ? error.message : 'inference_failed';
    const wasCancelled = state.cancelledRequestIds.has(requestId) || code === 'request_cancelled';
    if (wasCancelled && (state.activeRequestId === requestId || state.activeRequestId === null)) {
      state.message = 'Local matrix exercise cancelled safely; joined workers remain available.';
      state.error = '';
    } else if (state.activeRequestId === requestId) {
      state.error = code;
      state.message = '';
    }
  } finally {
    state.cancelledRequestIds.delete(requestId);
    if (state.activeRequestId === requestId) {
      state.busy = false;
      state.activeRequestId = null;
      state.cancellationRequested = false;
    }
  }
  render();
  await refreshStatus();
}

async function cancelInference() {
  if (!state.activeRequestId || state.cancellationRequested) return;
  const requestId = state.activeRequestId;
  state.cancellationRequested = true;
  state.message = 'Cancellation requested; waiting for active browser-stage work to stop…';
  state.error = '';
  render();
  try {
    const response = await operatorPost('/api/interactive/cancel', { request_id: requestId });
    if (response.cancelled) {
      state.cancelledRequestIds.add(requestId);
      if (state.activeRequestId === requestId) {
        state.busy = false;
        state.activeRequestId = null;
        state.cancellationRequested = false;
      }
      state.message = 'Cancellation accepted; no new browser-stage compute can start for this request.';
    } else {
      state.message = 'Local matrix exercise finished before cancellation reached active work.';
    }
  } catch (error) {
    state.cancellationRequested = false;
    state.error = error instanceof Error ? error.message : 'cancel_failed';
  }
  render();
}

function downloadEvidence() {
  const record = state.record;
  if (!record) return;
  const blob = new Blob([`${JSON.stringify(record, null, 2)}\n`], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const safeRequestId = String(record.request_id).replace(/[^a-zA-Z0-9._-]+/g, '-');
  link.href = url;
  link.download = `mycelium-evidence-${safeRequestId}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  state.message = 'Evidence JSON downloaded locally.';
  render();
}

function peerEnvironment() {
  return {
    secureContext: window.isSecureContext === true,
    webCrypto: Boolean(globalThis.crypto?.subtle),
    stageLoaded: state.peer.stage !== null,
    joined: state.peer.peerId !== null,
    polling: state.peer.running && state.peer.state === 'running',
  };
}

async function startPeer(token) {
  state.peer.mode = true;
  state.peer.state = 'joining';
  state.peer.error = '';
  render();
  try {
    const environment = peerEnvironment();
    if (!environment.secureContext) throw new Error('browser_secure_context_required');
    if (!environment.webCrypto) throw new Error('browser_webcrypto_required');
    const grant = (await post('/api/interactive/join', { token })).grant;
    state.peer.peerId = grant.peer_id;
    state.peer.sessionToken = grant.session_token;
    state.peer.stage = BrowserPixelStage.fromDocument(grant.stage_pack);
    state.peer.running = true;
    state.peer.state = 'running';
    render();
    await peerLoop();
  } catch (error) {
    if (!state.peer.running && ['stopping', 'stopped'].includes(state.peer.state)) {
      state.peer.state = 'stopped';
      state.peer.error = '';
      render();
      return;
    }
    state.peer.running = false;
    state.peer.state = 'failed';
    state.peer.error = error instanceof Error ? error.message : 'peer_failed';
    render();
  }
}

async function stopPeer() {
  state.peer.running = false;
  state.peer.pollController?.abort();
  state.peer.pollController = null;
  state.peer.state = 'stopping';
  render();
  if (state.peer.peerId && state.peer.sessionToken) {
    await post('/api/interactive/leave', {
      peer_id: state.peer.peerId,
      session_token: state.peer.sessionToken,
    }).catch(() => null);
  }
  state.peer.state = 'stopped';
  render();
}

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function peerLoop() {
  while (state.peer.running) {
    const pollController = new AbortController();
    state.peer.pollController = pollController;
    let response;
    try {
      response = await post('/api/interactive/poll', {
        peer_id: state.peer.peerId,
        session_token: state.peer.sessionToken,
        timeout_seconds: 15,
      }, { signal: pollController.signal });
    } catch (error) {
      if (!state.peer.running && error instanceof DOMException && error.name === 'AbortError') return;
      throw error;
    } finally {
      if (state.peer.pollController === pollController) state.peer.pollController = null;
    }
    if (!state.peer.running) return;
    const work = response.work;
    if (work === null) {
      await sleep(250);
      continue;
    }
    if (work.route_ready !== false) throw new Error('work_route_ready_invalid');
    const permit = await post('/api/interactive/start', {
      peer_id: state.peer.peerId,
      session_token: state.peer.sessionToken,
      job_id: work.job_id,
      request_id: work.request_id,
      input_digest: work.input_digest,
    });
    if (permit.route_ready !== false) throw new Error('work_start_route_ready_invalid');
    if (permit.started !== true) {
      state.peer.lastJob = `${work.job_id} (cancelled before compute)`;
      render();
      continue;
    }
    const output = state.peer.stage.execute(work.hidden);
    try {
      await post('/api/interactive/result', {
        peer_id: state.peer.peerId,
        session_token: state.peer.sessionToken,
        result: {
          protocol: 'mycelium.browser_stage_result.v1',
          job_id: work.job_id,
          request_id: work.request_id,
          assignment_id: work.assignment_id,
          stage_id: work.stage_id,
          pack_digest: work.pack_digest,
          input_digest: work.input_digest,
          output,
          output_digest: await matrixDigest(output),
          route_ready: false,
        },
      });
    } catch (error) {
      if (error instanceof Error && error.message === 'result_job_not_active') {
        state.peer.lastJob = `${work.job_id} (cancelled)`;
        render();
        continue;
      }
      throw error;
    }
    state.peer.completed += 1;
    state.peer.lastJob = work.job_id;
    render();
  }
}

function hostHtml() {
  const status = state.status;
  const peers = status?.peers ?? [];
  const record = state.record;
  const peerCount = status?.peer_count ?? 0;
  const readyPeerCount = status?.ready_peer_count ?? 0;
  const targetCount = selectedDeviceTarget();
  const minimumReady = readyPeerCount >= targetCount;
  const deviceCountOptions = [1, 2, 3, 4, 5, 6]
    .map((count) => `<option value="${count}" ${count === targetCount ? 'selected' : ''}>${count}</option>`)
    .join('');
  const tokenRows = (record?.token_records ?? []).map((token) => `
    <tr data-token-evidence>
      <td>${h(token.token_index)}</td>
      <td>${h(token.selected_label)}</td>
      <td>${h(short(token.browser_peer_id))}</td>
      <td>${h(scientific(token.intermediate_error))}</td>
      <td>${h(scientific(token.logit_error))}</td>
      <td title="${h(token.browser_output_digest)}">${h(short(token.browser_output_digest))}</td>
    </tr>`).join('');
  const inviteRows = state.invites.map((invite, index) => `
    <div class="invite-row">
      <div class="invite-title"><strong>Device ${index + 1}</strong><span>one use</span></div>
      <label for="invite-url-${index}">Join link</label>
      <input id="invite-url-${index}" data-invite-url readonly value="${h(invite.url)}">
      <small>Expires ${h(formatUnix(invite.expires_at))}; open once on device ${index + 1}.</small>
      <button data-copy-invite="${index}">Copy device ${index + 1} link</button>
    </div>`).join('');
  return `
    <section class="hero">
      <p class="eyebrow">Interactive browser-session matrix exercise</p>
      <h1>Mycelium browser swarm</h1>
      <p>Create one unique link per browser session, wait for them to join, then route bounded synthetic matrix jobs across the swarm. This is never model inference. Observatory remains read-only; this is a separate local test console.</p>
      <div class="claim"><span>route_ready=false</span><span>local evidence only</span><span>same-origin API</span><span>trusted HTTPS required off-host</span><span>${minimumReady ? `minimum ${targetCount} distinct peer sessions ready` : `${readyPeerCount}/${targetCount} minimum ready`}</span></div>
    </section>
    <section class="grid">
      <div id="live-console-guide" class="card wide"><p class="eyebrow">Optional physical-device observation path</p><ol class="steps"><li><strong>Trust</strong><span>install the local CA on the operator host and every worker device</span></li><li><strong>Create</strong><span>choose a minimum and generate one unique link per device</span></li><li><strong>Join</strong><span>open each link once and wait for the minimum cohort to become ready</span></li><li><strong>Run</strong><span>send a bounded request through the synthetic browser matrix exercise</span></li><li><strong>Save</strong><span>inspect the completed request's exact session cohort, parity rows, and unsigned local JSON summary</span></li></ol><p class="boundary">Keep every device on a network that can reach this host. The local CA enables Web Crypto over LAN HTTPS. This proves distinct authenticated peer sessions, not physical-device identity. This is bounded matrix-fixture evidence, never model inference or production readiness.</p></div>
      <div class="card"><p class="eyebrow">Swarm status</p><dl class="facts">
        <div><dt>Peer sessions joined</dt><dd>${h(peerCount || (status ? 0 : '—'))}</dd></div>
        <div><dt>Workers ready</dt><dd>${h(status ? readyPeerCount : '—')}</dd></div>
        <div><dt>Distinct-peer minimum</dt><dd>${minimumReady ? `READY · ${h(readyPeerCount)} ready, minimum ${h(targetCount)}` : `WAITING · ${h(readyPeerCount)} ready, minimum ${h(targetCount)}`}</dd></div>
        <div><dt>Active requests</dt><dd>${h(status?.active_request_count ?? '—')}</dd></div>
        <div><dt>Pending jobs</dt><dd>${h(status?.pending_job_count ?? '—')}</dd></div>
        <div><dt>Completed requests</dt><dd>${h(status?.completed_request_count ?? '—')}</dd></div>
        <div><dt>Stage pack</dt><dd>${h(short(status?.stage_pack_digest))}</dd></div>
        <div><dt>Route ready</dt><dd>${h(String(status?.route_ready ?? false))}</dd></div>
      </dl></div>
      <div class="card stack"><p class="eyebrow">Join browser peers</p><p>First trust the CA certificate printed by <code>device-lab</code> on this operator host and every worker device. Then create one unique capability link per browser peer.</p><label for="invite-count">Minimum distinct peer sessions</label><select id="invite-count" ${state.busy ? 'disabled' : ''}>${deviceCountOptions}</select><button id="create-invites" class="primary" ${state.busy ? 'disabled' : ''}>Create ${h(targetCount)} unique worker link${targetCount === 1 ? '' : 's'}</button><small>Links expire after five minutes. Never reuse or share one link across peer sessions.</small>
        <div class="invite-list">${inviteRows || '<small>No device links created yet.</small>'}</div>
      </div>
      <form id="request-form" class="card stack" aria-busy="${state.busy}"><p class="eyebrow">Request</p><label for="prompt">Prompt seed</label><textarea id="prompt" name="prompt" maxlength="512">${h(state.draft.prompt)}</textarea><label for="max-new">Max fixture tokens</label><input id="max-new" name="max_new_tokens" type="number" min="${h(targetCount)}" max="8" value="${h(state.draft.maxNewTokens)}"><div class="request-actions"><button class="primary" type="submit" ${state.busy || !minimumReady ? 'disabled' : ''}>${state.busy ? 'Matrix exercise running…' : 'Run browser matrix exercise'}</button>${state.busy ? `<button id="cancel-request" class="danger" type="button" ${state.cancellationRequested ? 'disabled' : ''}>${state.cancellationRequested ? 'Cancelling…' : 'Cancel request'}</button>` : ''}</div>${state.busy ? `<small role="status">Active ${h(short(state.activeRequestId))}; status remains live while work runs.</small>` : `<small>Use at least ${h(targetCount)} fixture token${targetCount === 1 ? '' : 's'} so the completed request can freeze an exact ${h(targetCount)}-peer cohort. Maximum 8; local evidence only; never model inference.</small>`}</form>
      <div class="card"><p class="eyebrow">Connected peers</p><dl class="facts">${peers.map((peer) => `<div><dt>${h(peer.peer_id)}</dt><dd>${h(peer.state)} · jobs ${h(peer.completed_jobs)}</dd></div>`).join('') || '<div><dt>none</dt><dd>create device links</dd></div>'}</dl></div>
      ${state.message ? `<div class="card wide message" role="status">${h(state.message)}</div>` : ''}
      ${state.error ? `<div class="card wide error" role="alert">${h(state.error)}</div>` : ''}
      ${record ? `<div class="card wide evidence-card"><div class="evidence-heading"><div><p class="eyebrow">Latest recoverable local result</p><h2>${h(record.generated_labels.join(' ') || '(no tokens)')}</h2></div><button id="download-evidence" type="button">Download local JSON</button></div><dl class="facts evidence-facts"><div><dt>Request</dt><dd>${h(record.request_id)}</dd></div><div><dt>Prompt digest</dt><dd>${h(short(record.prompt_digest))}</dd></div><div><dt>Max stage error</dt><dd>${h(scientific(record.max_intermediate_error))}</dd></div><div><dt>Max logit error</dt><dd>${h(scientific(record.max_logit_error))}</dd></div><div><dt>Peer IDs</dt><dd>${h(record.peer_ids.join(', '))}</dd></div><div><dt>Peer sessions proven</dt><dd>${h(record.observed_distinct_peers)} / ${h(record.required_distinct_peers)}</dd></div><div><dt>Route ready</dt><dd>${h(String(record.route_ready))}</dd></div><div><dt>Summary scope</dt><dd>${record.local_evidence_only === true ? 'unsigned local JSON' : 'invalid'}</dd></div></dl><div class="evidence-scroll"><table><caption>Per-token browser-stage parity</caption><thead><tr><th>Token</th><th>Label</th><th>Peer</th><th>Stage error</th><th>Logit error</th><th>Output digest</th></tr></thead><tbody>${tokenRows}</tbody></table></div></div>` : ''}
    </section>`;
}

function peerHtml() {
  const peer = state.peer;
  const environment = peerEnvironment();
  const canStop = ['running', 'stopping'].includes(peer.state);
  const checks = [
    ['secure-origin', 'Trusted HTTPS secure context', environment.secureContext],
    ['web-crypto', 'Web Crypto available', environment.webCrypto],
    ['stage-loaded', 'Bounded browser stage loaded', environment.stageLoaded],
    ['swarm-joined', 'Unique invite accepted', environment.joined],
    ['worker-ready', 'Worker polling and ready', environment.polling],
  ].map(([key, label, passed]) => `
    <div data-device-check="${key}" data-check-state="${passed ? 'pass' : 'wait'}">
      <dt>${h(label)}</dt><dd>${passed ? 'PASS' : 'WAIT'}</dd>
    </div>`).join('');
  return `
    <section class="hero"><p class="eyebrow">Browser worker · one-link join</p><h1>Joined swarm worker</h1><p>Keep this page open and the browser awake. This session computes only an assigned bounded synthetic matrix fixture—never model inference. Authenticated session distinctness does not prove physical-device identity.</p><div class="claim"><span>route_ready=false</span><span>local evidence only</span><span>synthetic fixture</span><span>not model inference</span><span>identity unproven</span><span>${environment.polling ? 'session ready' : 'session preparing'}</span></div></section>
    <section class="grid"><div class="card"><p class="eyebrow">Browser-session preflight</p><dl class="facts check-list">${checks}</dl><p class="boundary">Origin: ${h(window.location.origin)}. If secure context or Web Crypto does not pass, trust the device-lab CA and reopen the original unconsumed link.</p></div><div class="card"><p class="eyebrow">Peer state</p><dl class="facts"><div><dt>State</dt><dd>${h(peer.state)}</dd></div><div><dt>Peer session</dt><dd>${h(peer.peerId ?? 'joining…')}</dd></div><div><dt>Completed jobs</dt><dd>${h(peer.completed)}</dd></div><div><dt>Last job</dt><dd>${h(peer.lastJob ?? 'none')}</dd></div><div><dt>Route ready</dt><dd>false</dd></div></dl><br>${canStop ? `<button id="stop-peer" ${peer.state === 'stopping' ? 'disabled' : ''}>${peer.state === 'stopping' ? 'Stopping…' : 'Stop peer worker'}</button>` : '<p class="message">Worker stopped. This one-use link cannot be reused.</p>'}${peer.error ? `<p class="error" role="alert">${h(peer.error)}</p>` : ''}</div></section>`;
}

function render() {
  app.innerHTML = state.peer.mode ? peerHtml() : hostHtml();
  document.querySelector('#create-invites')?.addEventListener('click', createInvites);
  document.querySelector('#invite-count')?.addEventListener('change', (event) => {
    state.draft.inviteCount = event.currentTarget.value;
    if (Number(state.draft.maxNewTokens) < Number(state.draft.inviteCount)) {
      state.draft.maxNewTokens = state.draft.inviteCount;
    }
    render();
  });
  for (const button of document.querySelectorAll('[data-copy-invite]')) {
    button.addEventListener('click', () => copyInvite(Number(button.dataset.copyInvite)));
  }
  document.querySelector('#request-form')?.addEventListener('submit', runInference);
  document.querySelector('#cancel-request')?.addEventListener('click', cancelInference);
  document.querySelector('#download-evidence')?.addEventListener('click', downloadEvidence);
  document.querySelector('#prompt')?.addEventListener('input', (event) => {
    state.draft.prompt = event.currentTarget.value;
  });
  document.querySelector('#max-new')?.addEventListener('input', (event) => {
    state.draft.maxNewTokens = event.currentTarget.value;
  });
  document.querySelector('#stop-peer')?.addEventListener('click', stopPeer);
}

const joinCapability = fragmentCapability('join');
const operatorCapability = fragmentCapability('operator');
if (joinCapability) {
  window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  state.peer.mode = true;
  render();
  void startPeer(joinCapability);
} else {
  if (operatorCapability) {
    state.operatorToken = operatorCapability;
    window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
  } else {
    state.error = 'operator_capability_missing';
  }
  render();
  if (state.operatorToken) {
    void refreshStatus();
    window.setInterval(refreshStatus, 1500);
  }
}
