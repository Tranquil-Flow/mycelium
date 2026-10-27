// SPDX-License-Identifier: AGPL-3.0-only
'use strict';
const $ = (id) => document.getElementById(id);
let state = null;
let sending = false;
async function call(path, body) {
  const response = await fetch(path, {method:'POST', cache:'no-store', credentials:'omit', redirect:'error',
    referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json','X-Mycelium-Chat':'1'},
    body:JSON.stringify(body || {})});
  const value = await response.json();
  if (!response.ok) {
    const code = typeof value.error === 'string' ? value.error : value.error?.code;
    throw new Error(typeof code === 'string' && code ? code : 'request_failed');
  }
  return value;
}
const MESSAGES = {
  token_path_policy_expired:'Fast-mode permission expired. Ask the owner to keep both Macs connected and press Start for a new run, then send your prompt again.',
  token_path_process_expired:'The fast-mode worker reached the end of its allowed run. Ask the owner to press Start for a new run, then send your prompt again.',
  managed_control_expired:'A Mac’s control session expired. Ask the owner to reconnect the Macs and press Start for a new run, then send your prompt again.',
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
  request_cleanup_unproven:'The swarm could not confirm node cleanup in time. Try again in a moment.',
};
// Codes that end a request after text was already produced, where that text stands as the answer.
const NOTES = {
  request_cleanup_unproven:'the swarm could not confirm node cleanup in time. The text above is everything the swarm produced.',
};
function finishedText(text, error) {
  if (!error) return text;
  if (text && NOTES[error]) return `${text}\n[note: ${NOTES[error]}]`;
  return (text ? text + '\n' : '') + `[stopped: ${MESSAGES[error] || error}]`;
}
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
  $('send').disabled = sending || !state || !(state.available || state.rejoining);
}
function entry(kind, text) {
  const div = document.createElement('div'); div.className = 'msg ' + kind; div.textContent = text;
  $('log').append(div); div.scrollIntoView({block:'end'}); return div;
}
async function send(event) {
  event.preventDefault();
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
        if (page.error) answer.textContent = finishedText(text, page.error);
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
refresh(); setInterval(refresh, 4000);
