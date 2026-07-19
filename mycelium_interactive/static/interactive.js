import { BrowserPixelStage, matrixDigest } from './pixelStage.js';

const app = document.querySelector('#app');
const state = {
  operatorToken: null,
  status: null,
  invite: null,
  record: null,
  message: '',
  error: '',
  busy: false,
  statusRefreshing: false,
  draft: {
    prompt: 'moonlit swarm',
    maxNewTokens: '2',
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

async function readJson(response) {
  const doc = await response.json().catch(() => null);
  if (!response.ok) throw new Error(doc?.error ?? `http_${response.status}`);
  if (!doc || doc.ok !== true) throw new Error('interactive_api_invalid_response');
  return doc;
}

async function post(path, body) {
  return readJson(await fetch(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    cache: 'no-store',
    credentials: 'same-origin',
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
  if (state.peer.mode || state.busy || state.statusRefreshing) return;
  state.statusRefreshing = true;
  const previousStatus = JSON.stringify(state.status);
  const previousError = state.error;
  try {
    state.status = (await operatorGet('/api/interactive/status')).status;
    state.error = '';
  } catch (error) {
    state.error = error instanceof Error ? error.message : 'status_failed';
  } finally {
    state.statusRefreshing = false;
  }
  if (JSON.stringify(state.status) !== previousStatus || state.error !== previousError) render();
}

async function createInvite() {
  state.busy = true;
  state.message = '';
  state.error = '';
  render();
  try {
    state.invite = (await operatorPost('/api/interactive/invite', { ttl_seconds: 300 })).invite;
    state.message = 'Invite created. Open this link on another browser/device.';
  } catch (error) {
    state.error = error instanceof Error ? error.message : 'invite_failed';
  }
  state.busy = false;
  render();
  await refreshStatus();
}

async function copyInvite() {
  if (!state.invite) return;
  try {
    await navigator.clipboard.writeText(state.invite.url);
    state.message = 'Invite copied.';
  } catch {
    state.message = 'Copy failed; select the URL manually.';
  }
  render();
}

async function runInference(event) {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  state.draft.prompt = String(form.get('prompt') ?? '');
  state.draft.maxNewTokens = String(form.get('max_new_tokens') ?? '1');
  state.busy = true;
  state.message = '';
  state.error = '';
  render();
  try {
    state.record = (await operatorPost('/api/interactive/infer', {
      prompt: String(form.get('prompt') ?? ''),
      max_new_tokens: Number(form.get('max_new_tokens') ?? 1),
    })).record;
    state.message = 'Inference completed with local browser-stage evidence.';
  } catch (error) {
    state.error = error instanceof Error ? error.message : 'inference_failed';
  }
  state.busy = false;
  render();
  await refreshStatus();
}

async function startPeer(token) {
  state.peer.mode = true;
  state.peer.state = 'joining';
  state.peer.error = '';
  render();
  try {
    const grant = (await post('/api/interactive/join', { token })).grant;
    state.peer.peerId = grant.peer_id;
    state.peer.sessionToken = grant.session_token;
    state.peer.stage = BrowserPixelStage.fromDocument(grant.stage_pack);
    state.peer.running = true;
    state.peer.state = 'running';
    render();
    await peerLoop();
  } catch (error) {
    state.peer.running = false;
    state.peer.state = 'failed';
    state.peer.error = error instanceof Error ? error.message : 'peer_failed';
    render();
  }
}

async function stopPeer() {
  state.peer.running = false;
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
    const response = await post('/api/interactive/poll', {
      peer_id: state.peer.peerId,
      session_token: state.peer.sessionToken,
      timeout_seconds: 15,
    });
    const work = response.work;
    if (work === null) {
      await sleep(250);
      continue;
    }
    if (work.route_ready !== false) throw new Error('work_route_ready_invalid');
    const output = state.peer.stage.execute(work.hidden);
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
    state.peer.completed += 1;
    state.peer.lastJob = work.job_id;
    render();
  }
}

function hostHtml() {
  const status = state.status;
  const peers = status?.peers ?? [];
  const record = state.record;
  return `
    <section class="hero">
      <p class="eyebrow">Interactive distributed inference test</p>
      <h1>Mycelium browser swarm</h1>
      <p>Create a one-use link, open it on another browser/device, then route a bounded decoder-stage job through that peer. Observatory remains read-only; this is a separate local test console.</p>
      <div class="claim"><span>route_ready=false</span><span>local evidence only</span><span>same-origin API</span><span>HTTPS required for non-loopback use</span></div>
    </section>
    <section class="grid">
      <div class="card"><p class="eyebrow">Swarm status</p><dl class="facts">
        <div><dt>Peers</dt><dd>${h(status?.peer_count ?? '—')}</dd></div>
        <div><dt>Pending jobs</dt><dd>${h(status?.pending_job_count ?? '—')}</dd></div>
        <div><dt>Completed requests</dt><dd>${h(status?.completed_request_count ?? '—')}</dd></div>
        <div><dt>Stage pack</dt><dd>${h(short(status?.stage_pack_digest))}</dd></div>
        <div><dt>Route ready</dt><dd>${h(String(status?.route_ready ?? false))}</dd></div>
      </dl></div>
      <div class="card stack"><p class="eyebrow">Invite peer</p><button id="create-invite" class="primary" ${state.busy ? 'disabled' : ''}>Create one-use join link</button>
        ${state.invite ? `<label for="invite-url">Peer URL</label><input id="invite-url" readonly value="${h(state.invite.url)}"><small>Expires ${h(formatUnix(state.invite.expires_at))}; token is URL fragment until join.</small><button id="copy-invite">Copy link</button>` : ''}
      </div>
      <form id="request-form" class="card stack"><p class="eyebrow">Request</p><label for="prompt">Prompt seed</label><textarea id="prompt" name="prompt" maxlength="512">${h(state.draft.prompt)}</textarea><label for="max-new">Max new tokens</label><input id="max-new" name="max_new_tokens" type="number" min="1" max="8" value="${h(state.draft.maxNewTokens)}"><button class="primary" ${state.busy || (status?.peer_count ?? 0) < 1 ? 'disabled' : ''}>Run through browser worker</button></form>
      <div class="card"><p class="eyebrow">Connected peers</p><dl class="facts">${peers.map((peer) => `<div><dt>${h(peer.peer_id)}</dt><dd>${h(peer.state)} · jobs ${h(peer.completed_jobs)}</dd></div>`).join('') || '<div><dt>none</dt><dd>create invite</dd></div>'}</dl></div>
      ${state.message ? `<div class="card wide message">${h(state.message)}</div>` : ''}
      ${state.error ? `<div class="card wide error" role="alert">${h(state.error)}</div>` : ''}
      ${record ? `<div class="card wide"><p class="eyebrow">Latest evidence</p><h2>${h(record.generated_labels.join(' ') || '(no tokens)')}</h2><dl class="facts"><div><dt>Request</dt><dd>${h(record.request_id)}</dd></div><div><dt>Prompt digest</dt><dd>${h(short(record.prompt_digest))}</dd></div><div><dt>Max stage error</dt><dd>${h(record.max_intermediate_error.toExponential(3))}</dd></div><div><dt>Max logit error</dt><dd>${h(record.max_logit_error.toExponential(3))}</dd></div><div><dt>Peer IDs</dt><dd>${h(record.peer_ids.join(', '))}</dd></div><div><dt>Route ready</dt><dd>${h(String(record.route_ready))}</dd></div></dl></div>` : ''}
    </section>`;
}

function peerHtml() {
  const peer = state.peer;
  return `
    <section class="hero"><p class="eyebrow">Browser worker · one-link join</p><h1>Joined swarm worker</h1><p>This browser computes only the assigned decoder substage and returns digest-bound results. Session token stays in memory.</p><div class="claim"><span>route_ready=false</span><span>local evidence only</span><span>exact stage JS</span></div></section>
    <section class="grid"><div class="card"><p class="eyebrow">Peer state</p><dl class="facts"><div><dt>State</dt><dd>${h(peer.state)}</dd></div><div><dt>Peer</dt><dd>${h(peer.peerId ?? 'joining…')}</dd></div><div><dt>Completed jobs</dt><dd>${h(peer.completed)}</dd></div><div><dt>Last job</dt><dd>${h(peer.lastJob ?? 'none')}</dd></div><div><dt>Route ready</dt><dd>false</dd></div></dl><br><button id="stop-peer">Stop peer worker</button>${peer.error ? `<p class="error" role="alert">${h(peer.error)}</p>` : ''}</div></section>`;
}

function render() {
  app.innerHTML = state.peer.mode ? peerHtml() : hostHtml();
  document.querySelector('#create-invite')?.addEventListener('click', createInvite);
  document.querySelector('#copy-invite')?.addEventListener('click', copyInvite);
  document.querySelector('#request-form')?.addEventListener('submit', runInference);
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
