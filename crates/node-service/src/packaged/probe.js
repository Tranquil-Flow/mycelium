// SPDX-License-Identifier: AGPL-3.0-only
// Test-owned initialization script of the `qualification-probe` bundle (never in the default
// bundle). Drives the frozen chat page of the shipped bundle over its only chat path, the owner
// run's route (lane CHAT, owner ruling 14 Nov): with no run serving, the page shows the shipped
// not-ready message with Send disabled, its channel refuses an action, a send through the page's
// own channel is refused `chat_not_ready` and an unknown request's events `chat_request_unknown`
// (chat.py:317-318, 460-468 @ f4e366e9). It does NOT quit: the harness sends the exact-pid Apple
// Event, so Quit is proven through the real native path.
(() => {
  let chain = Promise.resolve();
  const record = async name => {
    const recorded = new Promise(resolve => window.addEventListener('l11-title-recorded', resolve, {once:true}));
    document.title = name; await recorded;
  };
  const receipt = name => (chain = chain.then(() => record(name)));
  const until = predicate => new Promise((resolve, reject) => {
    const check = () => {
      try { const value = predicate(); if (value) { observer.disconnect(); resolve(value); } }
      catch (error) { observer.disconnect(); reject(error); }
    };
    const observer = new MutationObserver(check);
    observer.observe(document.documentElement, {subtree:true, childList:true, characterData:true, attributes:true});
    check();
  });
  const post = async (path, header, body) => {
    const response = await fetch(path, {method:'POST', cache:'no-store', credentials:'omit', redirect:'error', referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json', [header]:'1'}, body:JSON.stringify(body || {})});
    return {status:response.status, value:await response.json()};
  };
  window.addEventListener('load', async () => {
    if (location.pathname !== '/chat') { location.href = '/chat'; return; }
    try {
      // chat.js MESSAGES.chat_not_ready, shown from the route chat's own state (available false).
      await until(() => {
        const state = document.getElementById('state').textContent;
        if (state.includes('Cannot reach')) throw new Error('state');
        if (state.startsWith('Serving')) throw new Error('serving_without_a_run');
        return state.includes('not serving right now') && document.getElementById('send').disabled;
      });
      await receipt('L11_PACKAGED_CHAT_PAGE_READY');
      const denied = await post('/api/serving/stop', 'X-Mycelium-Chat');
      if (denied.status !== 403) throw new Error('channel');
      const sent = await post('/chat/api/send', 'X-Mycelium-Chat', {prompt:__PROMPT_JSON__, max_new_tokens:Number(__TOKENS_JSON__)});
      if (sent.status !== 503 || sent.value.error !== 'chat_not_ready') throw new Error('send_' + sent.status + '_' + sent.value.error);
      await receipt('L11_PACKAGED_CHAT_ROUTE_NOT_READY');
      // A well-formed id no chat has (16 to 64 of [A-Za-z0-9_-], chat.py:45): a malformed one is
      // refused 400 chat_request_invalid before the lookup.
      const events = await post('/chat/api/events', 'X-Mycelium-Chat', {request_id:'l11-probe-unknown-request', after:0});
      if (events.status !== 404 || events.value.error !== 'chat_request_unknown') throw new Error('events_' + events.status);
      await receipt('L11_PACKAGED_CHAT_ROUTE_EVENTS_UNKNOWN');
    } catch (error) {
      await receipt('L11_SLICE_FAILED_' + error.message);
      await post('/api/quit', 'X-Mycelium-Action');
    }
  }, {once:true});
})();
