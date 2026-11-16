'use strict';
// The packaged window's owner controls on its chat page (lane CHATPAGE): the way back to the main
// page is a plain link (chat.html), and Stop is the main page's own request (run.js
// `act('/api/demo/stop', {})`). Window only: the public relay's chat page never loads this script,
// and the relay refuses an owner action whatever a page sends.
(() => {
  const $ = (id) => document.getElementById(id);
  let busy = false;
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
      credentials:'same-origin', redirect:'error', referrerPolicy:'no-referrer',
      headers: body === undefined ? {} : {'Content-Type':'application/json','X-Mycelium-Action':'1'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'request_failed');
    return value;
  }
  function render(status) {
    const run = status.owner_run || {};
    const owner = status.installation_role === 'owner';
    const active = ['running','serving','stopping','failing'].includes(run.state);
    $('run-stop').hidden = !owner;
    $('run-stop').disabled = busy || !owner || !active || run.state === 'stopping';
    $('run-stop-state').textContent = !owner ? '' : run.state === 'stopping' ? 'Stopping the run…'
      : run.state === 'stopped' ? 'The run is stopped. Start a new one from the main page.' : '';
  }
  async function refresh() {
    try { render(await api('/api/status')); } catch (error) { /* the next poll retries */ }
  }
  $('run-stop').addEventListener('click', async () => {
    if (busy) return;
    busy = true; $('run-stop').disabled = true;
    try { await api('/api/demo/stop', {}); $('run-stop-state').textContent = 'Stopping the run…'; }
    catch (error) { $('run-stop-state').textContent = `Stop not completed (${error.message}).`; }
    finally { busy = false; refresh(); }
  });
  refresh(); setInterval(refresh, 2000);
})();
