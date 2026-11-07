// SPDX-License-Identifier: AGPL-3.0-only
// Explicit development-only native probe; NEVER changes the frozen UI assets.
async function l11Probe() {
  try {
    const receipt = async value => {
      const recorded = new Promise(resolve => window.addEventListener('l11-title-recorded', resolve, {once:true}));
      document.title = value; await recorded;
    };
    const post = {method:'POST', headers:{'Content-Type':'application/json','X-Mycelium-Action':'1'}, body:'{}'};
    const chatPost = {method:'POST', headers:{'Content-Type':'application/json','X-Mycelium-Chat':'1'}, body:'{}'};
    for (const path of ['/chat/api/state', '/chat/api/send', '/chat/api/events']) {
      if ((await fetch(path, chatPost)).status !== 503) throw new Error('chat header admission');
      if ((await fetch(path, post)).status !== 403) throw new Error('chat requires Chat');
    }
    if ((await fetch('/api/quit', chatPost)).status !== 403) throw new Error('chat on action path');
    if ((await fetch('/chat/api/other', chatPost)).status !== 403) throw new Error('chat prefix escape');
    await receipt(location.pathname === '/chat' ? 'L11_CHAT_HEADER_CHAT_OK' : 'L11_CHAT_HEADER_ROOT_OK');
    if (location.href === 'mycelium://localhost/chat') {
      if (!document.getElementById('prompt') || document.styleSheets.length < 1) throw new Error('chat DOM');
      if ((await fetch('/api/l11-probe', post)).status !== 503) throw new Error('chat capability');
      await receipt('L11_LOCAL_CHAT_OK');
      document.title = 'L11_NATIVE_FETCH_OK'; return;
    }
    if (!document.getElementById('quit') || typeof api !== 'function' || document.styleSheets.length < 1) {
      document.title = 'L11_NATIVE_DOM_FAILED'; return;
    }
    const positive = await fetch('/api/l11-probe', post);
    if (positive.status !== 503) { document.title = 'L11_NATIVE_FETCH_FAILED'; return; }
    const bootstrap = await fetch('/native-session', post);
    const missingAction = await fetch('/api/l11-probe', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    if (bootstrap.status !== 403 || missingAction.status !== 403 || window.__TAURI__ !== undefined) {
      document.title = 'L11_NATIVE_REFUSAL_FAILED'; return;
    }
    // Every promise below waits for a positive WebKit CSP violation receipt,
    // not a sleep or an absence-of-fetch guess. The Rust harness owns the bound.
    const frame = async (name, configure, nativeBlock = false) => {
      const element = document.createElement('iframe');
      const blocked = nativeBlock
        ? new Promise(resolve => window.addEventListener('l11-navigation-blocked', resolve, {once:true}))
        : new Promise(resolve => {
        const listener = event => {
          if (event.effectiveDirective === 'frame-src') {
            document.removeEventListener('securitypolicyviolation', listener);
            resolve();
          }
        };
        document.addEventListener('securitypolicyviolation', listener);
      });
      configure(element); document.body.append(element);
      await blocked; element.remove();
      await receipt('L11_FRAME_' + name + '_OK');
    };
    const payload = '<script src="mycelium://localhost/l11-attack.js"></script>';
    const blob = URL.createObjectURL(new Blob([payload], {type:'text/html'}));
    await frame('SAME_PATH', node => node.src = '/');
    await frame('CHAT', node => node.src = '/chat');
    await frame('DIFFERENT_PATH', node => node.src = '/l11-attack');
    await frame('REDIRECT', node => node.src = '/l11-redirect');
    await frame('REMOTE', node => node.src = 'https://example.invalid/frame');
    // WebKit sends srcdoc through native navigation before emitting any CSP
    // violation; require that positive native denial, not a fabricated CSP event.
    await frame('SRCDOC', node => node.srcdoc = payload, true);
    await frame('DATA', node => node.src = 'data:text/html,' + encodeURIComponent(payload));
    await frame('BLOB', node => node.src = blob);
    const navigation = async (name, url) => {
      const blocked = new Promise(resolve => window.addEventListener('l11-navigation-blocked', resolve, {once:true}));
      window.location.assign(url); await blocked;
      if (location.href !== 'mycelium://localhost/') throw new Error('escaped document');
      await receipt('L11_NAV_' + name + '_OK');
    };
    await navigation('DIFFERENT_PATH', 'mycelium://localhost/l11-attack');
    await navigation('REDIRECT', 'mycelium://localhost/l11-redirect');
    await navigation('DATA', 'data:text/html,' + encodeURIComponent(payload));
    await navigation('BLOB', blob);
    await navigation('EXTERNAL', 'https://example.invalid/external');
    for (const [name, url] of [['EXTERNAL', 'https://example.invalid/popup'], ['DATA', 'data:text/html,x'], ['BLOB', blob], ['DIFFERENT_PATH', 'mycelium://localhost/l11-attack'], ['CHAT_API', 'mycelium://localhost/chat/api/send']]) {
      const popupBlocked = new Promise(resolve => window.addEventListener('l11-popup-blocked', resolve, {once:true}));
      window.open(url, '_blank'); await popupBlocked;
      await receipt('L11_POPUP_' + name + '_OK');
    }
    URL.revokeObjectURL(blob);
    // Trusted document retains its capability after every denied attack.
    const stillTrusted = await fetch('/api/l11-probe', post);
    if (stillTrusted.status !== 503) throw new Error('lost trusted document');
    // The unchanged local-chat _blank link reuses the native pinned window.
    window.open('/chat', '_blank');
  } catch (_) { document.title = 'L11_NATIVE_FETCH_EXCEPTION'; }
}
if (document.readyState === 'complete') { void l11Probe(); }
else { window.addEventListener('load', l11Probe, {once:true}); }
