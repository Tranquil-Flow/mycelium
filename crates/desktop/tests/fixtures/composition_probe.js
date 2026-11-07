// SPDX-License-Identifier: AGPL-3.0-only
// Consumer-owned script: tests the additive seam, NOT a real inference backend.
async function compositionProbe() {
  const receipt = async name => {
    const recorded = new Promise(resolve => window.addEventListener('l11-title-recorded', resolve, {once:true}));
    document.title = name; await recorded;
  };
  try {
    const post = (header, body) => ({method:'POST', headers:{'Content-Type':'application/json', [header]:'1'}, body:JSON.stringify(body)});
    for (const [path, body] of [['state',{}], ['send',{prompt:'isolated probe',max_new_tokens:1}], ['events',{request_id:'absent',after:0}]]) {
      const reply = await fetch('/chat/api/' + path, post('X-Mycelium-Chat', body));
      if (reply.status !== 503 || (await reply.json()).error !== 'core_not_integrated') throw new Error('service callback');
    }
    if ((await fetch('/api/serving/stop', post('X-Mycelium-Chat', {}))).status !== 403) throw new Error('stop must require Action');
    if ((await fetch('/api/serving/stop', post('X-Mycelium-Action', {}))).status !== 503) throw new Error('stop dispatch');
    await receipt('L11_CUSTOM_PROBE_OK');
    await receipt('L11_NATIVE_FETCH_OK');
    await fetch('/api/quit', post('X-Mycelium-Action', {}));
  } catch (_) { document.title = 'L11_CUSTOM_PROBE_FAILED'; }
}
window.addEventListener('load', compositionProbe, {once:true});
