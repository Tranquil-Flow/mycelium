'use strict';
const $ = (id) => document.getElementById(id);
let state = null;
let sending = false;
// -- first-prompt notice: one note in localStorage (per origin), no cookie, no server state --------------------
const NOTE_KEY = 'mycelium.chat.notice.v1';
let memoryNote = null;  // when localStorage is blocked the note lives for this page load only
function readNote() {
  let raw = memoryNote;
  try { raw = window.localStorage.getItem(NOTE_KEY); } catch (error) { /* blocked: memory only */ }
  if (typeof raw !== 'string') return null;
  try {
    const note = JSON.parse(raw);
    return note && note.v === 1 && (note.owner === null || typeof note.owner === 'string') ? note : null;
  } catch (error) { return null; }
}
function currentOwner() { return state && state.notice && state.notice.owner_fingerprint ? state.notice.owner_fingerprint : null; }
// Consent is for this origin and for the owner the page names now: a different owner is asked again.
function acknowledged() {
  const note = readNote();
  return !!state && !!note && note.owner === currentOwner();
}
function acknowledge() {
  if (!state) return;
  const raw = JSON.stringify({v: 1, owner: currentOwner()});
  memoryNote = raw;
  try { window.localStorage.setItem(NOTE_KEY, raw); } catch (error) { /* memory only */ }
  render();
}
function forgetAll() {
  memoryNote = null;
  try {
    const mine = [];
    for (let i = 0; i < window.localStorage.length; i++) { const key = window.localStorage.key(i); if (key && key.startsWith('mycelium.')) mine.push(key); }
    for (const key of mine) window.localStorage.removeItem(key);
  } catch (error) { /* nothing was stored */ }
  $('forget-confirm').hidden = true;
  render();
}
function noticeOwnerText(notice) {
  if (!notice || !notice.published) {
    return 'This swarm is not listed in a directory, so nothing here proves who runs it: it is run by whoever shared this link with you, not by Mycelium.';
  }
  const name = notice.swarm_name ? `“${notice.swarm_name}” ` : '';
  return [`This swarm ${name}is run by the owner whose key fingerprint is `, ['fp', notice.owner_fingerprint],
    '. That key signed its listing in the directory. A swarm is run by whoever runs its Mac, not by Mycelium.'];
}
function noticeQuotaText(notice, limits) {
  const lim = limits ? `On this page: ${limits.per_client_per_minute} prompts a minute for you (counted by network address), ` +
    `${limits.swarm_per_minute} for the whole swarm, one at a time, up to ${limits.max_new_tokens} new tokens each.` : '';
  if (notice && notice.published && notice.access_mode && notice.access_mode !== 'open') {
    return `This swarm is not open to everyone (${notice.access_mode} only), so this page will refuse prompts. Ask the owner for access through the directory. ${lim}`.trim();
  }
  if (notice && notice.published && notice.open_quota) {
    return `Open quota: ${notice.open_quota.max_requests} requests and ${notice.open_quota.max_tokens} tokens per key. ${lim}`.trim();
  }
  return `Quota: no per-key allowance is published for this swarm. ${lim}`.trim();
}
function fill(node, parts) {
  node.replaceChildren(...[].concat(parts).map((part) => {
    if (!Array.isArray(part)) return document.createTextNode(part);
    const span = document.createElement('span'); span.className = part[0]; span.textContent = part[1]; return span;
  }));
}
function render() {
  const notice = state && state.notice;
  if (notice) {
    fill($('notice-owner'), noticeOwnerText(notice));
    fill($('notice-quota'), noticeQuotaText(notice, state.limits));
    $('notice-kept').textContent = String(notice.kept_answers);
    const link = $('forget-directory');
    const directory = typeof notice.directory === 'string' && notice.directory.startsWith('https:') ? notice.directory.replace(/\/+$/, '') + '/swarms/' : null;
    if (directory) { link.href = directory; link.textContent = directory; }
    $('forget-directory-wrap').hidden = !directory;
  }
  const ok = acknowledged();
  $('notice').hidden = ok;
  $('form').hidden = !ok;
  $('send').disabled = sending || !ok || !state || !(state.available || state.rejoining);
}
async function call(path, body) {
  const response = await fetch(path, {method:'POST', cache:'no-store', credentials:'omit', redirect:'error',
    referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json','X-Mycelium-Chat':'1'},
    body:JSON.stringify(body || {})});
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || 'request_failed');
  return value;
}
const MESSAGES = {
  chat_not_ready:'The swarm is not serving right now. The host starts it from the Mycelium app.',
  chat_warming_up:'Warming up: the swarm is qualifying its route. Chat opens when it is ready.',
  chat_route_not_qualified:'The route is not qualified yet (warming up). Try again in a moment.',
  route_rejoining:'Rejoining: this Mac is reconnecting its route. Try again in a few seconds.',
  route_rejoining_busy:'Several messages are already waiting for the route to reconnect. Try again in a few seconds.',
  chat_rejoining:'Rejoining - connecting route. Your message waits up to 15 seconds.',
  chat_busy:'Someone else\'s prompt is running. Try again in a few seconds.',
  chat_rate_limited:'You sent several prompts this minute. Wait a little.',
  chat_swarm_rate_limited:'The room sent many prompts this minute. Wait a little.',
  chat_prompt_too_long:'That prompt is too long (2,000 characters at most).',
  entry_chat_requires_entry:'This swarm is not open to everyone. Ask the owner for access through the directory.',
  entry_killed_by_owner:'The swarm owner has stopped this swarm (kill switch).',
  entry_swarm_daily_cap:'This swarm reached its daily limit for open access. Try again tomorrow (UTC).',
};
function servedText(list) {
  if (!list || !list.length) return '';
  return 'Served by: ' + list.map((p) => `${p.label} ${p.node}${p.layers ? ` (layers ${p.layers[0]}–${p.layers[1] - 1})` : ''}`).join(' → ');
}
async function refresh() {
  try {
    state = await call('/chat/api/state');
    $('state').textContent = state.available ? `Serving ${state.model_id || ''}${state.busy ? ' · busy' : ' · ready'}`
      : state.rejoining ? MESSAGES.chat_rejoining : state.warming_up ? MESSAGES.chat_warming_up : MESSAGES.chat_not_ready;
    $('served').textContent = servedText(state.served_by);
  } catch (error) { $('state').textContent = 'Cannot reach the host right now.'; state = null; }
  render();
}
function entry(kind, text) {
  const div = document.createElement('div'); div.className = 'msg ' + kind; div.textContent = text;
  $('log').append(div); div.scrollIntoView({block:'end'}); return div;
}
async function send(event) {
  event.preventDefault();
  if (!acknowledged()) return;
  const prompt = $('prompt').value.trim(); if (!prompt || sending) return;
  sending = true; $('send').disabled = true;
  entry('you', prompt);
  const answer = entry('swarm', '…');
  const meta = entry('meta', '');
  try {
    const accepted = await call('/chat/api/send', {prompt, max_new_tokens:Number($('tokens').value)});
    meta.textContent = servedText(accepted.served_by) + (accepted.clamped ? ` · capped at ${accepted.max_new_tokens} tokens` : '');
    $('prompt').value = '';
    let next = 0; let text = '';
    for (;;) {
      const page = await call('/chat/api/events', {request_id:accepted.request_id, after:next});
      next = page.next; text += page.tokens.join(''); answer.textContent = text || '…';
      if (page.done) {
        if (page.error) answer.textContent = (text ? text + '\n' : '') + `[stopped: ${MESSAGES[page.error] || page.error}]`;
        if (page.seconds !== null) meta.textContent += ` · ${page.seconds} s`;
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, 600));
    }
  } catch (error) {
    answer.textContent = MESSAGES[error.message] || ('Not sent: ' + error.message);
  } finally { sending = false; refresh(); }
}
$('form').addEventListener('submit', send);
$('notice-ack').addEventListener('click', acknowledge);
$('forget').addEventListener('click', () => { $('forget-confirm').hidden = false; $('forget-yes').focus(); });
$('forget-no').addEventListener('click', () => { $('forget-confirm').hidden = true; $('forget').focus(); });
$('forget-yes').addEventListener('click', forgetAll);
refresh(); setInterval(refresh, 4000);
