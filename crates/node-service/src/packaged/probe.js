// SPDX-License-Identifier: AGPL-3.0-only
// Test-owned initialization script of the `qualification-probe` bundle (never in the default
// bundle). Drives the frozen chat page: DOM token, Stop, terminal. It does NOT quit: the
// harness sends the exact-pid Apple Event, so Quit is proven through the real native path.
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
      await receipt('L11_PACKAGED_CHAT_PAGE_READY');
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
      // Let several tokens stream before Stop so the gate compares more than the first one.
      await until(() => (document.querySelector('.msg.swarm')?.textContent || '').length >= 24);
      const stop = await post('/api/serving/stop', 'X-Mycelium-Action');
      if (stop.status !== 200 || stop.value.stopped !== true) throw new Error('stop');
      await receipt('L11_SLICE_STOP_VERIFIED');
      await until(() => !document.getElementById('send').disabled);
      const answer = document.querySelector('.msg.swarm').textContent;
      if (!answer || answer === '…' || answer.startsWith('Not sent:')) throw new Error('terminal');
      await receipt('L11_SLICE_CHAT_DOM_TERMINAL');
    } catch (error) {
      await receipt('L11_SLICE_FAILED_' + error.message);
      await post('/api/quit', 'X-Mycelium-Action');
    }
  }, {once:true});
})();
