// SPDX-License-Identifier: AGPL-3.0-only
'use strict';
// One-click demo run (owner), this Mac's run status (joined Mac), model picker,
// QR join codes and chat links. Independent of app.js: own status poll.
(() => {
  const $ = (id) => document.getElementById(id);
  let busy = false;
  let pickerKey = '';
  let fastMessage = '';
  async function api(path, body) {
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
      credentials:'same-origin', redirect:'error', referrerPolicy:'no-referrer',
      headers: body === undefined ? {} : {'Content-Type':'application/json','X-Mycelium-Action':'1'},
      body: body === undefined ? undefined : JSON.stringify(body)});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || 'request_failed');
    return value;
  }
  const gb = (n) => (n / 1e9).toFixed(n >= 1e10 ? 0 : 1) + ' GB';
  const ERRORS = {
    owner_model_not_prepared:'Download the selected model on this Mac first: under Inference, tick the authorization box, press “Download / resume source” and wait for “prepared”, then press Start again.',
    peer_clock_skew_exceeded:'A joined Mac\'s clock is too far from this Mac\'s. Turn on “Set time and date automatically” on both Macs (System Settings → General → Date & Time), then press Start again.',
    router_port_shutdown_cleanup_unproven:'The route could not prove it had cleaned up the Macs after stopping. This is recorded; press Start again to build a new route.',
    two_macs_required:"Invite one other physical Mac with a code or QR, keep Mycelium open on both Macs, and press Start again. This Mac also contributes.",
    member_capability_report_required:"A Mac has not reported its available memory and storage. On that Mac, press Report this Mac capability under Device readiness, then press Start again on the owner.",
    token_path_policy_expired:"Fast-mode permission expired. Keep Mycelium open and connected on both Macs, then press Start on the owner to begin a new run.",
    token_path_process_expired:"The fast-mode worker reached the end of its allowed run. Press Start on the owner to begin a new run.",
    managed_control_expired:"A Mac's control session expired. Keep Mycelium open on both Macs, use Start / reconnect on the disconnected Mac, then press Start on the owner.",
    fast_mode_run_active:"Stop the current run and wait for it to finish closing before changing Fast mode. The setting applies to the next run only.",
    fast_mode_locked_by_environment:"Fast mode is fixed by MYCELIUM_TOKEN_PATH. Remove that environment override and reopen Mycelium to use this switch.",
    two_distinct_hosts_required:'The run needs two different physical Macs; two copies of the app on one Mac do not count.',
    member_descriptors_timeout:'Some joined Macs did not report in.',
    owner_swarm_required:'Create the swarm first.',
    owner_run_active:'A run is already active. Stop it first.',
    member_slice_timeout:'A joined Mac did not finish fetching its slice in time.',
    route_qualification_timeout:'The route did not qualify in time.',
    member_memory_insufficient_for_share:'A Mac does not have enough memory for its share of this model. Add a Mac with more memory, or choose a smaller model.',
    member_disk_insufficient_for_share:'A Mac does not have enough free disk for its share of this model.',
    member_memory_cap_below_share:'A joined Mac agreed to less memory per run than its share of this model needs.',
    member_storage_cap_below_share:'A joined Mac agreed to less storage than its share of this model needs.',
    member_cannot_hold_share:'A Mac cannot hold even the smallest share of this model. Add a Mac with more memory, or choose a smaller model.',
    owner_storage_consent_below_model:'This Mac\'s storage limit is below what the selected model needs here.',
    previous_run_still_closing:'The previous route is still closing (a Mac that left it has not confirmed cleanup yet). Wait a minute and press Start again.',
    member_restarted:'A joined Mac was restarted while the demo was serving, so it left the route and the run ended. Press Start to run it again.',
    execution_ceiling_reached:'The run reached its wall-clock limit (30 minutes), so it ended before the Macs\' workers expired. Press Start to run it again.',
    route_qualification_failed:'The route failed its startup qualification, so nothing is served. The owner log names the Mac and reason.',
  };
  function explain(code) { return ERRORS[code] ? `${ERRORS[code]} (${code})` : `Action not completed: ${String(code).replaceAll('_', ' ')} (${code}).`; }
  function renderPicker(status) {
    const picker = status.run_model; if (!picker) return;
    const key = JSON.stringify([picker.selected, picker.locked]);
    if (key !== pickerKey) {
      pickerKey = key;
      $('run-model').replaceChildren(...picker.choices.map((c) => {
        const o = document.createElement('option'); o.value = c.name;
        o.textContent = `${c.label} · ${gb(c.snapshot_bytes)} download on this Mac${c.fits_storage_ceiling ? '' : ' · too large for the storage limit'}`;
        o.selected = c.name === picker.selected; return o;
      }));
    }
    const run = status.owner_run || {};
    $('run-model').disabled = busy || picker.locked || ['running','serving','stopping'].includes(run.state);
    $('run-model-note').textContent = picker.locked ? `Fixed by MYCELIUM_A13_RUN_MODEL (${picker.selected}).`
      : `Pinned: ${picker.model_id} @ ${picker.resolved_commit.slice(0, 12)} · ${picker.source === 'saved' ? 'your choice' : 'demo default'}. Joined Macs fetch a whole snapshot for small models, or only their slice when slice mode is available.`;
  }
  function renderFastMode(status) {
    const mode = status.fast_mode; if (!mode) return;
    const active = ['running','serving','stopping','failing'].includes(status.owner_run?.state);
    $('fast-mode').checked = mode.enabled;
    $('fast-mode').disabled = busy || mode.locked || active;
    $('fast-mode-note').textContent = fastMessage || (active ? ERRORS.fast_mode_run_active
      : mode.locked ? 'Fixed by MYCELIUM_TOKEN_PATH; the environment overrides the saved switch.'
      : 'Saved for the next run only. MYCELIUM_TOKEN_PATH, if set, overrides this switch.');
  }
  async function changeFastMode() {
    if (busy || $('fast-mode').disabled) return;
    busy = true; fastMessage = ''; $('fast-mode').disabled = true;
    try {
      await api('/api/fast-mode', {enabled:$('fast-mode').checked});
      fastMessage = 'Fast mode saved for the next run.';
    } catch (error) { fastMessage = explain(error.message); }
    finally { busy = false; await refresh(); }
  }
  // Start without a prepared model: show where to prepare it (the Inference download step), once per failure.
  let pointedAt = null;
  function pointToModelStep(fix) {
    const target = $(fix.target || 'acquisition');
    if (!target || pointedAt === latestRunId) return;
    pointedAt = latestRunId; target.scrollIntoView({block: 'center'}); target.classList.add('needs-attention');
  }
  let latestRunId = null;
  function renderRun(status) {
    const run = status.owner_run; if (!run) return;
    latestRunId = run.run_id;
    const active = ['running','serving','stopping'].includes(run.state);
    $('demo-start').disabled = busy || active;
    $('demo-stop').disabled = busy || !active;
    $('demo-rebuild').disabled = busy || !run.run_id || run.state === 'stopping';
    const step = (run.steps || []).find((s) => s.name === run.step);
    $('demo-state').textContent = run.state === 'failed' ? `Stopped at “${(run.steps.find((s) => s.name === run.failed_step) || {}).label}”: ${run.error_message ? `${run.error_message} (${run.error_code})` : explain(run.error_code)}`
      : run.state === 'serving' ? 'Serving — open the chat below.' : run.state === 'running' ? `Running: ${step ? step.label : ''}…`
      : run.state === 'stopping' ? 'Stopping…' : run.state === 'stopped' ? 'Stopped.' : 'Idle.';
    if (run.state === 'failed' && run.error_fix && run.error_fix.action === 'prepare_model') pointToModelStep(run.error_fix);
    $('demo-steps').replaceChildren(...(run.steps || []).map((s) => {
      const li = document.createElement('li'); li.className = s.state;
      li.textContent = s.label + (s.detail ? ` — ${s.detail}` : '') + (s.state === 'done' ? ' ✓' : s.state === 'failed' ? ' ✗' : '');
      return li;
    }));
    const people = run.participants || [];
    if (!people.length) { $('demo-participants').replaceChildren(); }
    else {
      const table = document.createElement('table'); table.className = 'members';
      const head = table.insertRow(); ['Mac','Node','State','Layers','Memory guard','Disk limit'].forEach((t) => { const th = document.createElement('th'); th.textContent = t; head.append(th); });
      for (const p of people) {
        const row = table.insertRow();
        const progress = p.progress && p.progress.total_bytes ? ` ${Math.floor(100 * p.progress.done_bytes / p.progress.total_bytes)}%` : '';
        [p.label, p.node_id.slice(0, 12), (p.state || 'waiting') + progress + (p.detail ? ` (${p.detail})` : ''),
         p.layers ? `${p.layers[0]}–${p.layers[1] - 1}` : '—',
         p.resources ? gb(p.resources.max_rss_bytes) : '—', p.resources ? gb(p.resources.max_disk_bytes) : '—'].forEach((t) => { row.insertCell().textContent = t; });
      }
      $('demo-participants').replaceChildren(table);
    }
    const chat = status.chat || {};
    const links = $('chat-links'); links.replaceChildren();
    if (chat.available) {
      const local = document.createElement('a'); local.href = '/chat'; local.target = '_blank'; local.rel = 'noopener'; local.textContent = 'Open the chat on this Mac';
      links.append(local);
      if (chat.share_url) {
        const share = document.createElement('p'); share.textContent = 'Share with the room: ' + chat.share_url; links.append(share);
      }
    } else links.textContent = 'The chat opens when the route is serving.';
  }
  function renderQr(status) {
    const flow = status.join_flow || {}; const box = $('window-qr'); if (!box) return;
    const open = (flow.pairing_windows || []).filter((w) => w.state === 'open' && w.short_code);
    const ids = open.map((w) => w.window_id).join(',');
    if (box.dataset.ids === ids) return; box.dataset.ids = ids;
    box.replaceChildren(...open.map((w) => {
      const figure = document.createElement('figure');
      const img = document.createElement('img'); img.src = `/api/pair/window/qr/${w.window_id}.svg`;
      img.alt = `QR code: join page for ${w.short_code}`;
      const caption = document.createElement('figcaption'); caption.textContent = `Scan to see how to join (code ${w.short_code})`;
      figure.append(img, caption); return figure;
    }));
  }
  const TARGET = {
    not_joined:['Not joined yet','Join with a code from the owner above.'],
    joined:['Joined · idle','Waiting for the owner to start a run.'],
    idle:['Joined · idle','Waiting for the owner to start a run.'],
    waiting_for_consent:['Joined · waiting for standing consent','Grant standing consent when joining so this Mac can take part without clicks.'],
    reported:['Joined · reported to the owner','This Mac told the owner it is ready.'],
    preparing:['Preparing','Starting native hello (no model load).'],
    bootstrap_running:['Preparing','Native hello running (no model load).'],
    cleaning_up:['Preparing','Cleaning up the hello step.'],
    downloading_slice:['Downloading slice','Fetching only this Mac’s layers of the model, verifying every byte.'],
    slice_ready:['Slice ready','Waiting for the owner to start serving.'],
    work_running:['Loading / qualifying','Running its slice; the route is being qualified.'],
    serving:['Serving','This Mac is computing its layers for prompts in the room.'],
    stopped:['Joined · idle','The owner stopped the run.'],
    error:['Problem','See the detail below; the owner sees it too.'],
  };
  function renderTarget(status) {
    const t = status.demo_target; if (!t) return;
    const [title, text] = TARGET[t.state] || [t.state, ''];
    $('target-title').textContent = title;
    $('target-detail').textContent = text + (t.model_id ? ` Model: ${t.model_id}.` : '') + (t.detail ? ` ${ERRORS[t.detail] ? explain(t.detail) : `(${t.detail})`}` : '');
    const bar = $('target-progress');
    if (t.progress && t.progress.total_bytes) { bar.hidden = false; bar.max = t.progress.total_bytes; bar.value = t.progress.done_bytes; }
    else bar.hidden = true;
  }
  async function refresh() {
    let status;
    try { status = await api('/api/status'); } catch (error) { return; }
    const owner = status.installation_role === 'owner' && status.authority && status.authority.swarm_created;
    $('demo-run').hidden = !owner;
    $('demo-target').hidden = status.installation_role !== 'target';
    if (owner) { renderPicker(status); renderFastMode(status); renderRun(status); }
    renderQr(status); renderTarget(status);
  }
  async function act(path, body) {
    busy = true;
    try { await api(path, body); } catch (error) { $('demo-state').textContent = explain(error.message); }
    finally { busy = false; refresh(); }
  }
  $('fast-mode').addEventListener('change', changeFastMode);
  $('demo-start').addEventListener('click', () => act('/api/demo/start', {}));
  $('demo-stop').addEventListener('click', () => act('/api/demo/stop', {}));
  $('demo-rebuild').addEventListener('click', () => act('/api/demo/rebuild', {}));
  $('run-model').addEventListener('change', () => act('/api/run-model', {choice:$('run-model').value}));
  refresh(); setInterval(refresh, 2000);
})();
