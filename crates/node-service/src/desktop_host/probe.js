// SPDX-License-Identifier: AGPL-3.0-only
// Test-owned initialization script. Frozen chat assets remain unchanged.
(() => {
  let chain = Promise.resolve();
  const record = async name => {
    const recorded = new Promise(resolve => window.addEventListener('l11-title-recorded', resolve, {once:true}));
    document.title = name; await recorded;
  };
  // One title change at a time, so page-fetch receipts never interleave with milestones.
  const receipt = name => (chain = chain.then(() => record(name)));
  // Observation only: off, the page runs exactly the frozen chat flow with no extra title changes.
  const pageReceipts = __PAGE_RECEIPTS__;
  let fetches = 0;
  const nativeFetch = window.fetch.bind(window);
  if (pageReceipts) window.fetch = (input, init) => {
    const path = String(input);
    if (!path.startsWith('/chat/api/')) return nativeFetch(input, init);
    const label = path.slice('/chat/api/'.length).replace(/[^a-z]/g, '') + '_' + (++fetches);
    receipt('L11_SLICE_PAGE_FETCH_START_' + label);
    return nativeFetch(input, init).then(
      response => { receipt('L11_SLICE_PAGE_FETCH_END_' + label + '_' + response.status); return response; },
      error => { receipt('L11_SLICE_PAGE_FETCH_ERR_' + label); throw error; });
  };
  const until = predicate => new Promise((resolve, reject) => {
    const check = () => {
      try { const value = predicate(); if (value) { observer.disconnect(); resolve(value); } }
      catch (error) { observer.disconnect(); reject(error); }
    };
    const observer = new MutationObserver(check);
    observer.observe(document.documentElement, {subtree:true, childList:true, characterData:true, attributes:true});
    check();
  });
  const post = async (path, header) => {
    const response = await fetch(path, {method:'POST', cache:'no-store', credentials:'omit', redirect:'error', referrerPolicy:'no-referrer', headers:{'Content-Type':'application/json', [header]:'1'}, body:'{}'});
    return {status:response.status, value:await response.json()};
  };
  window.addEventListener('load', async () => {
    if (location.pathname !== '/chat') { location.href = '/chat'; return; }
    try {
      await until(() => {
        if (document.getElementById('state').textContent.includes('Cannot reach')) throw new Error('state');
        return !document.getElementById('send').disabled;
      });
      const denied = await post('/api/serving/stop', 'X-Mycelium-Chat');
      if (denied.status !== 403) throw new Error('channel');
      document.getElementById('prompt').value = __PROMPT_JSON__;
      document.getElementById('tokens').value = __TOKENS_JSON__;
      document.getElementById('form').requestSubmit();
      await until(() => {
        const answer = document.querySelector('.msg.swarm')?.textContent;
        if (answer?.startsWith('Not sent:')) throw new Error('native_send');
        return answer && answer !== '…';
      });
      await receipt('L11_SLICE_CHAT_DOM_TOKEN');
      const stop = await post('/api/serving/stop', 'X-Mycelium-Action');
      if (stop.status !== 200 || stop.value.stopped !== true) throw new Error('stop');
      await receipt('L11_SLICE_STOP_VERIFIED');
      await until(() => !document.getElementById('send').disabled);
      const answer = document.querySelector('.msg.swarm').textContent;
      if (!answer || answer === '…' || answer.startsWith('Not sent:')) throw new Error('terminal');
      await receipt('L11_SLICE_CHAT_DOM_TERMINAL');
      const quit = await post('/api/quit', 'X-Mycelium-Action');
      if (quit.status !== 200 || quit.value.stopping !== true) throw new Error('quit');
    } catch (error) {
      await receipt('L11_SLICE_FAILED_' + error.message);
      await post('/api/quit', 'X-Mycelium-Action');
    }
  }, {once:true});
})();
