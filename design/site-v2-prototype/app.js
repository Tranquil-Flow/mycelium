// Mycelium site V2 prototype: renders MOCK data into pre-sized regions. No network.
// The real build would render from a boot JSON inlined by the directory (see SITE-REDESIGN-V2.md, "Loading").
'use strict';
const $ = (id) => document.getElementById(id);
const esc = (t) => String(t).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const qs = new URLSearchParams(location.search);
const PRIVACY_ONE_LINE = "The swarm's Macs can read your prompt. It is not end-to-end encrypted from them.";

// ---------- formatting ----------
function ago(sec) { if (sec < 60) return sec + ' s ago'; if (sec < 3600) return Math.round(sec / 60) + ' min ago'; return Math.round(sec / 3600) + ' h ago'; }
function upFor(h) { return h >= 48 ? Math.floor(h / 24) + ' d ' + (h % 24) + ' h' : h + ' h'; }
function statusPill(st) {
  const label = { serving: 'Serving', warming: 'Warming up', offline: 'Offline' }[st];
  return `<span class="pill ${st}"><i aria-hidden="true"></i>${label}</span>`;
}
function accessLabel(a) { return { open: 'Open to anyone', request: 'By request', invite: 'Invite only' }[a]; }
function queueText(q) { return q.s === 'free' ? 'Free' : q.s === 'busy' ? '1 running' : q.n + ' waiting'; }
function statCells(s) {
  const t = s.stats;
  const why = s.status === 'offline' ? 'No data: this swarm is offline' : 'No data yet: the route is still qualifying';
  const cell = (k, v, unit, na) => `<div class="stat ${na ? 'na' : ''}" ${na ? `title="${why}"` : ''}><div class="k">${k}</div><div class="v">${na ? '–' : v}${!na && unit ? `<small>${unit}</small>` : ''}</div></div>`;
  return cell('Speed', t && t.tok_s.toFixed(1), 'tok/s', !t) + cell('Latency', t && t.rtt, 'ms', !t) + cell('Macs', s.macs, '', !s.macs) + cell('Uptime', t && t.avail.toFixed(1), '%', !t);
}
function primaryAction(s) {
  const m = s.mine;
  if (m.role === 'owner') return `<a class="btn sm" href="owner.html">Manage</a>`;
  if (s.status === 'offline') return `<a class="btn sm sec" href="swarm.html?id=${s.id}">Details</a>`;
  if (s.access === 'open' || m.grant) return `<a class="btn sm" href="chat.html?id=${s.id}">Chat</a>`;
  if (s.access === 'request') return m.req === 'pending'
    ? `<a class="btn sm sec" href="chat.html?id=${s.id}&state=pending">Request sent</a>`
    : `<a class="btn sm" href="chat.html?id=${s.id}&state=needs-request">Ask to join</a>`;
  return `<a class="btn sm sec" href="swarm.html?id=${s.id}">How to get in</a>`;
}
function youTags(s) {
  const t = [];
  if (s.mine.role === 'owner') t.push('<span class="tag you">You own this</span>');
  if (s.mine.role === 'contributor') t.push('<span class="tag you">Your Mac helps</span>');
  if (s.mine.grant) t.push('<span class="tag you">You have access</span>');
  return t.join('');
}
function swarmCard(s) {
  const q = s.stats ? `<span class="tag" title="Prompts waiting or running on this swarm">Queue: ${queueText(s.stats.queue)}</span>` : '';
  const tags = youTags(s);
  return `<article class="card swarm-card" data-id="${s.id}" data-status="${s.status}" data-access="${s.access}">
    <div class="row between" style="align-items:flex-start;gap:8px;flex-wrap:nowrap"><h3><a href="swarm.html?id=${s.id}">${esc(s.name)}</a></h3>${statusPill(s.status)}</div>
    <div class="meta"><span>${esc(s.model)}</span><span aria-hidden="true">·</span><span>${accessLabel(s.access)}</span>${q}</div>
    <p class="desc">${esc(s.desc)}</p>
    <div class="stats" role="group" aria-label="${esc(s.name)} statistics">${statCells(s)}</div>
    <div class="foot"><span class="who">${tags ? `<span class="row" style="gap:6px;margin-bottom:2px">${tags}</span>` : ''}${s.status === 'offline' ? 'Last seen ' + ago(s.last_seen) : 'Updated ' + ago(s.last_seen)}</span>${primaryAction(s)}</div>
  </article>`;
}

// ---------- shared: key chip dialog, toast ----------
function wireKeyChip() {
  const chip = $('keychip'), dlg = $('keydlg');
  if (!chip || !dlg) return;
  chip.addEventListener('click', () => dlg.showModal());
  $('keydlg-close').addEventListener('click', () => dlg.close());
  const f = $('forget'), c = $('forget-confirm');
  f.addEventListener('click', () => { f.hidden = true; c.hidden = false; $('forget-no').focus(); });
  $('forget-no').addEventListener('click', () => { c.hidden = true; f.hidden = false; f.focus(); });
  $('forget-yes').addEventListener('click', () => { dlg.close(); toast('Key forgotten (prototype: nothing was deleted)'); c.hidden = true; f.hidden = false; });
}
let toastTimer;
function toast(msg) { const t = $('toast'); if (!t) return; t.textContent = msg; t.classList.add('show'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('show'), 3200); }

// ---------- apex ----------
function pageIndex() {
  const S = MOCK.swarms, serving = S.filter((s) => s.status === 'serving');
  const macs = S.reduce((a, s) => a + (s.status === 'offline' ? 0 : s.macs), 0);
  const models = new Set(serving.map((s) => s.model)).size;
  $('sb-nums').innerHTML = `<span><span class="n">${serving.length}</span><span class="l">of ${S.length} swarms serving</span></span>
    <span><span class="n">${macs}</span><span class="l">Macs helping</span></span><span><span class="n">${models}</span><span class="l">models in use</span></span>`;
  $('sb-tiles').innerHTML = S.slice(0, 4).map((s) => `<a class="tile" href="swarm.html?id=${s.id}"><span class="n"><span class="nm">${esc(s.name)}</span>${statusPill(s.status)}</span><span class="m">${esc(s.model)} · ${accessLabel(s.access)}</span></a>`).join('');
  $('sb-age').textContent = 'Updated 12 s ago';
}

// ---------- swarms app (tabs) ----------
function pageSwarms() {
  const S = MOCK.swarms;
  $('browse-grid').innerHTML = S.map(swarmCard).join('');
  const chatable = S.filter((s) => s.status !== 'offline' && (s.access === 'open' || s.mine.grant || s.mine.role === 'owner'));
  $('chat-list').innerHTML = chatList(chatable, qs.get('id') || chatable[0].id);
  mountChat($('chat-pane'), qs.get('id') || chatable[0].id, qs.get('state'));
  $('contrib-list').innerHTML = S.filter((s) => s.mine.role === 'contributor').map(contribCard).join('');
  $('owned-list').innerHTML = S.filter((s) => s.mine.role === 'owner').map(ownedCard).join('');
  // tabs
  const names = ['browse', 'chat', 'contributing', 'owned'];
  const tabs = names.map((n) => $('tab-' + n)), bars = names.map((n) => $('bar-' + n));
  function show(name) {
    if (!names.includes(name)) name = 'browse';
    names.forEach((n, i) => {
      const on = n === name;
      tabs[i].setAttribute('aria-selected', on); tabs[i].tabIndex = on ? 0 : -1;
      $('panel-' + n).hidden = !on;
      if (bars[i]) { on ? bars[i].setAttribute('aria-current', 'page') : bars[i].removeAttribute('aria-current'); }
    });
    document.title = 'Mycelium · Swarms · ' + name[0].toUpperCase() + name.slice(1);
  }
  tabs.forEach((t, i) => {
    t.addEventListener('click', (e) => { e.preventDefault(); location.hash = names[i]; });
    t.addEventListener('keydown', (e) => {
      const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key]; if (!d) return;
      const j = (i + d + names.length) % names.length; location.hash = names[j]; tabs[j].focus();
    });
  });
  addEventListener('hashchange', () => show(location.hash.slice(1)));
  show(location.hash.slice(1));
  // filters
  let filter = 'all', text = '';
  function apply() {
    let n = 0;
    document.querySelectorAll('#browse-grid .swarm-card').forEach((c) => {
      const s = MOCK.byId(c.dataset.id);
      const okF = filter === 'all' || (filter === 'serving' && s.status === 'serving') || (filter === 'open' && s.access === 'open') || (filter === 'request' && s.access !== 'open');
      const okT = !text || (s.name + ' ' + s.model + ' ' + s.desc).toLowerCase().includes(text);
      c.hidden = !(okF && okT); if (!c.hidden) n++;
    });
    $('browse-count').textContent = n + (n === 1 ? ' swarm' : ' swarms');
    $('browse-empty').hidden = n !== 0;
  }
  document.querySelectorAll('[data-filter]').forEach((b) => b.addEventListener('click', () => {
    filter = b.dataset.filter; document.querySelectorAll('[data-filter]').forEach((x) => x.setAttribute('aria-pressed', x === b)); apply();
  }));
  $('search').addEventListener('input', (e) => { text = e.target.value.trim().toLowerCase(); apply(); });
  apply();
  wireKeyChip();
}
function chatList(list, current) {
  return list.map((s) => `<a class="chat-item" href="chat.html?id=${s.id}" ${s.id === current ? 'aria-current="true"' : ''}>
    <span class="n">${esc(s.name)}</span>
    <span class="s"><span>${esc(s.model)} · ${s.mine.role === 'owner' ? 'yours' : s.mine.grant ? 'approved' : s.access === 'open' ? 'open' : s.mine.req === 'pending' ? 'requested' : 'by request'}</span>${statusPill(s.status)}</span></a>`).join('');
}
function contribCard(s) {
  const m = s.mine.mac;
  return `<article class="card"><div class="row between"><h3><a href="swarm.html?id=${s.id}" style="color:inherit">${esc(s.name)}</a></h3>${statusPill(s.status)}</div>
    <p class="muted" style="margin-top:6px">Your Mac <b style="color:var(--ink)">${esc(m.label)}</b> runs layers ${m.layers[0]}&ndash;${m.layers[1]} of ${s.layers} (${esc(s.model)}).</p>
    <div class="stats" style="margin:12px 0"><div class="stat"><div class="k">State</div><div class="v">Sharing</div></div><div class="stat"><div class="k">Path</div><div class="v">${m.path}</div></div><div class="stat"><div class="k">Hop latency</div><div class="v">${m.hop}<small>ms</small></div></div><div class="stat"><div class="k">Swarm speed</div><div class="v">${s.stats.tok_s.toFixed(1)}<small>tok/s</small></div></div></div>
    <p class="hint" style="margin:0 0 10px">Confirmed by the swarm's own entry (it lists a Mac with this label). To pause or leave, use the Mycelium app on that Mac.</p>
    <div class="row"><a class="btn sm sec" href="swarm.html?id=${s.id}">Swarm details</a><a class="btn sm" href="chat.html?id=${s.id}">Chat</a></div></article>`;
}
function ownedCard(s) {
  return `<article class="card"><div class="row between"><h3>${esc(s.name)}</h3>${statusPill(s.status)}</div>
    <p class="muted" style="margin:6px 0 12px">${esc(s.model)} · ${accessLabel(s.access)} · ${s.macs} Macs</p>
    <div class="note info" style="margin-bottom:12px"><b class="lead">2 people are waiting for your answer.</b> Approve or decline them in one tap.</div>
    <div class="row"><a class="btn sm" href="owner.html">Review requests <span class="count alert" style="margin-left:4px">2</span></a><a class="btn sm sec" href="swarm.html?id=${s.id}">Public page</a></div></article>`;
}

// ---------- detail ----------
function sparkBars(seed) {
  let out = ''; for (let i = 0; i < 24; i++) { const v = 0.35 + 0.65 * Math.abs(Math.sin(i * 0.7 + seed)); out += `<i style="height:${Math.round(v * 100)}%"></i>`; }
  return `<div class="spark" role="img" aria-label="Speed over the last 24 hours, mock">${out}</div>`;
}
function availBars(pct) {
  let out = ''; const off = Math.round((100 - pct) / 100 * 48);
  for (let i = 0; i < 48; i++) { out += `<i class="${i === 13 && off ? 'off' : i === 14 && off > 1 ? 'part' : ''}"></i>`; }
  return `<div class="avail" role="img" aria-label="Availability in the last 24 hours, one block per 30 minutes, mock">${out}</div>`;
}
function pageSwarm() {
  const s = MOCK.byId(qs.get('id') || 'reading'), t = s.stats;
  document.title = 'Mycelium · ' + s.name;
  $('d-name').textContent = s.name; $('d-pill').innerHTML = statusPill(s.status);
  $('d-meta').innerHTML = `<span>${esc(s.model)} (${s.model_gb} GB, ${s.layers} layers)</span><span aria-hidden="true">·</span><span>${accessLabel(s.access)}</span>${youTags(s)}`;
  $('d-desc').textContent = s.desc;
  $('d-action').innerHTML = primaryAction(s);
  const na = (why) => `<div class="v" style="color:var(--muted)">–</div><div class="sub">${why}</div>`;
  const why = s.status === 'offline' ? 'Offline: no live data. Last seen ' + ago(s.last_seen) + '.' : 'Warming up: the route is still qualifying.';
  const tile = (k, body, src) => `<div class="card bs"><div class="k">${k}</div>${body}<div class="src">${src}</div></div>`;
  const SRC_SWARM = 'Reported by the swarm', SRC_BROWSER = 'Measured in your browser', SRC_DIR = 'Observed by the directory';
  $('d-stats').innerHTML = t ? [
    tile('Token speed', `<div class="v">${t.tok_s.toFixed(1)}<small>tok/s</small></div><div class="sub">Median of the last ${t.samples} answers · first word in ${t.ttft.toFixed(1)} s</div>${sparkBars(s.id.length)}`, `<b>${SRC_SWARM}</b>`),
    tile('Network latency', `<div class="v" id="rtt">${t.rtt}<small>ms</small></div><div class="sub">Between this swarm's Macs (median hop). Your browser to the entry: <b id="myrtt" class="skel" style="display:inline-block;min-width:4ch">...</b></div>`, `<b>Swarm-reported</b> · yours measured in your browser`),
    tile('Macs', `<div class="v">${s.macs}<small>Macs</small></div><div class="sub">${s.helpers.filter((h) => h.path === 'direct').length} direct, ${s.helpers.filter((h) => h.path === 'relay').length} via relay</div>`, `<b>${SRC_SWARM}</b>`),
    tile('Uptime', `<div class="v">${upFor(t.up_h)}<small>this run</small></div><div class="sub">Reachable ${t.avail}% of the last 24 h</div>${availBars(t.avail)}`, `<b>${SRC_DIR}</b>`),
    tile('Queue', `<div class="v">${t.queue.s === 'free' ? 'Free' : t.queue.s === 'busy' ? '1 running' : t.queue.n + ' waiting'}</div><div class="sub">${t.queue.s === 'free' ? 'Your prompt starts right away.' : t.queue.s === 'busy' ? 'You would wait for the running prompt to finish.' : 'Expect a wait before your prompt starts.'}</div>`, `<b>${SRC_SWARM}</b>`),
    tile('Model', `<div class="v" style="font-size:1.5rem">${esc(s.model)}</div><div class="sub">${s.model_gb} GB · ${s.layers} layers · split across ${s.macs} Macs</div>`, `<b>Registered by Mycelium</b>`),
  ].join('') : [
    tile('Token speed', na(why), ''), tile('Network latency', na(why), ''), tile('Macs', `<div class="v">${s.macs}<small>Macs</small></div><div class="sub">${why}</div>`, ''),
    tile('Uptime', na(why), ''), tile('Queue', na(why), ''), tile('Model', `<div class="v" style="font-size:1.5rem">${esc(s.model)}</div><div class="sub">${s.model_gb} GB · ${s.layers} layers</div>`, '<b>Registered by Mycelium</b>'),
  ].join('');
  // helpers
  $('d-macs').innerHTML = s.helpers.length ? s.helpers.map((h) => `<tr><td><b>${esc(h.label)}</b>${h.mine ? ' <span class="tag you">yours</span>' : ''}</td>
    <td class="num">layers ${h.layers[0]}&ndash;${h.layers[1]}</td><td><span class="layerbar" aria-hidden="true"><i style="width:${Math.round((h.layers[1] - h.layers[0] + 1) / s.layers * 100)}%"></i></span></td><td>${h.state}</td><td>${h.path === '-' ? 'entry' : h.path}</td><td class="num">${h.hop == null ? '–' : h.hop + ' ms'}</td></tr>`).join('')
    : `<tr><td colspan="6" class="muted">No Macs are connected right now.</td></tr>`;
  // access
  const acc = { open: `<b>Open to anyone.</b> One tap on Chat gives your browser key an automatic allowance: ${s.quota ? s.quota.req + ' requests and ' + s.quota.tok.toLocaleString('en') + ' tokens per day' : ''}.`,
    request: `<b>By request.</b> The owner approves each person. You send your key fingerprint and an optional note; the owner answers on their Owned tab.`,
    invite: `<b>Invite only.</b> The owner grants your key directly. Send them your fingerprint <span class="mono">${MOCK.me.fp}</span> by any channel you already use.` }[s.access];
  $('d-access').innerHTML = `<p>${acc}</p>` + (s.mine.grant ? `<div class="note good"><b class="lead">You have access</b> until ${s.mine.grant.until}: ${s.mine.grant.req_left} of ${s.mine.grant.req_max} requests left.</div>` : s.mine.req === 'pending' ? `<div class="note info"><b class="lead">Request sent at ${s.mine.sent}.</b> Waiting for the owner.</div>` : '');
  $('d-who').innerHTML = `<dt>Run by</dt><dd>Owner key <span class="mono">${s.owner_fp}</span><br><span class="muted">A swarm is run by whoever runs its entry Mac, not by Mycelium.</span></dd>
    <dt>Entry (where prompts go)</dt><dd><span class="mono">${s.entry_host}</span><br><span class="muted">Your browser sends prompts straight there. They never pass through mycelium.now.</span></dd>
    <dt>Who can read prompts</dt><dd>The entry Mac, the helper Macs, and the network in between (Cloudflare can see plaintext).</dd>`;
  // simulated entry round-trip fills a reserved slot, no layout shift
  setTimeout(() => { const el = $('myrtt'); if (el) { el.classList.remove('skel'); el.textContent = (s.stats ? s.stats.rtt + 12 : 0) + ' ms'; } }, 900);
  wireKeyChip();
}

// ---------- chat ----------
const CHAT_STATES = ['first', 'ready', 'generating', 'busy', 'needs-request', 'pending', 'approved', 'declined', 'expired', 'quota', 'invite', 'offline', 'killed'];
function defaultChatState(s) {
  if (s.status === 'offline') return 'offline';
  if (s.mine.role === 'owner') return 'ready';
  if (s.mine.grant) return 'ready';
  if (s.access === 'open') return 'first';
  if (s.access === 'invite') return 'invite';
  return s.mine.req === 'pending' ? 'pending' : 'needs-request';
}
function mountChat(root, id, state) {
  const s = MOCK.byId(id), t = s.stats;
  state = CHAT_STATES.includes(state) ? state : defaultChatState(s);
  const q = t ? queueText(t.queue) : '–';
  root.innerHTML = `<div class="chat" data-state="${state}">
    <div class="chat-head"><div><h2>${esc(s.name)}</h2><div class="sub">${esc(s.model)} · ${s.macs} Macs · entry ${esc(s.entry_host)}</div></div>${statusPill(s.status)}</div>
    <div class="livebar" id="livebar" aria-label="Live swarm statistics">
      <span>Speed <b id="lb-speed">${t ? t.tok_s.toFixed(1) + ' tok/s' : '–'}</b></span><span>First word <b>${t ? t.ttft.toFixed(1) + ' s' : '–'}</b></span>
      <span>Your latency <b>${t ? t.rtt + 12 + ' ms' : '–'}</b></span><span>Queue <b id="lb-queue">${q}</b></span></div>
    <div class="log" id="log" role="log" aria-live="off" aria-label="Conversation"></div>
    <div class="dock" id="dock"></div></div>`;
  const log = root.querySelector('#log'), dock = root.querySelector('#dock');
  const add = (cls, html) => { const d = document.createElement('div'); d.className = 'msg ' + cls; d.innerHTML = html; log.append(d); log.scrollTop = log.scrollHeight; return d; };
  const sys = (html) => { const d = document.createElement('div'); d.innerHTML = html; log.append(d.firstElementChild); };
  const privacy = `<span class="muted">${PRIVACY_ONE_LINE}</span>`;
  // The composer is ALWAYS in the dock, in the same place. Gate states (notice, ask, wait, declined...) put their
  // panel in the conversation area and leave the composer disabled, so approval only enables it: nothing moves.
  const composer = (extra = '', placeholder = '', disabled = false) => `<div class="composer"><div style="flex:1"><label class="sr" for="prompt">Your prompt</label><textarea id="prompt" maxlength="2000" placeholder="${esc(placeholder || 'Ask ' + s.name + ' something')}" ${disabled ? 'disabled' : ''}></textarea></div><button class="btn" id="send" ${disabled ? 'disabled' : ''}>Send</button></div>
    <div class="fine"><span>${privacy}</span>${extra}</div>`;
  const quotaFine = s.mine.grant ? `<span class="row" style="gap:8px"><span class="num">${s.mine.grant.req_left} of ${s.mine.grant.req_max} requests left</span><span class="meter" role="img" aria-label="Quota left"><i style="width:${Math.round(s.mine.grant.req_left / s.mine.grant.req_max * 100)}%"></i></span></span>` : s.access === 'open' ? `<span class="num">Open allowance: ${s.quota.req} requests · ${s.quota.tok.toLocaleString('en')} tokens per day</span>` : '<span>Your own swarm: no quota.</span>';
  const others = MOCK.swarms.filter((x) => x.id !== s.id && x.status === 'serving' && (x.access === 'open' || x.mine.grant));
  const alt = others.length ? `<a class="btn sm sec" href="chat.html?id=${others[0].id}">Chat with ${esc(others[0].name)} instead</a>` : '';
  const goto = (st) => { history.replaceState(null, '', `chat.html?id=${s.id}&state=${st}`); const sel = document.getElementById('proto-state'); if (sel) sel.value = st; root.querySelector('.chat').dataset.state = st; set[st](); };
  const gate = (html) => { log.querySelectorAll('.gate').forEach((g) => g.remove()); const d = document.createElement('div'); d.className = 'gate'; d.innerHTML = html; log.prepend(d); return d; };
  const locked = (why) => { dock.innerHTML = composer('', why, true); };
  const set = {
    first() {
      const g = gate(`<h3>Before your first prompt to ${esc(s.name)}</h3>
        <ul><li>Your prompt goes from this browser <b>straight to ${esc(s.entry_host)}</b>, the swarm's own door. It does not pass through mycelium.now.</li>
        <li>The Macs that run it can read your prompt. Do not type secrets.</li>
        <li>No account. This swarm sees your key <span class="mono">${MOCK.me.fp}</span> and can link your prompts to it.</li></ul>
        <div class="row"><button class="btn" id="ack">I understand, start chatting</button><a class="btn ghost" href="index.html#privacy">Read the full notice</a></div>`);
      locked('Read the notice above to start');
      g.querySelector('#ack').onclick = () => { toast('Open allowance granted (mock): ' + s.quota.req + ' requests'); goto('ready'); };
    },
    ready() {
      log.querySelectorAll('.gate').forEach((g) => g.remove());
      dock.innerHTML = composer(quotaFine);
      if (log.children.length === 0 && t) {
        add('you', 'Summarise chapter 3 in two sentences.');
        add('swarm', 'Chapter 3 follows the narrator as she returns to the coast town and finds the harbour closed. The rest of the chapter turns on a letter she keeps unopened.<span class="m num">' + t.tok_s.toFixed(1) + ' tok/s · first word ' + t.ttft.toFixed(1) + ' s · served by ' + s.helpers.map((h) => h.label.replace(' (entry)', '')).join(' → ') + '</span>');
      }
      wireSend();
    },
    generating() { set.ready(); startStream(); },
    busy() {
      root.querySelector('#lb-queue').textContent = '1 running';
      sys(`<div class="note info"><b class="lead">Another prompt is running.</b> Press Send and we retry for you for up to 30 s, then tell you plainly. Nothing is lost.</div>`);
      dock.innerHTML = composer(quotaFine).replace('>Send</button>', '>Send (wait in line)</button>');
      wireSend(true);
    },
    'needs-request'() {
      const g = gate(`<h3>Ask ${esc(s.name)} for access</h3>
        <p class="muted">This swarm is by request. It is run by owner key <span class="mono">${s.owner_fp}</span>, who approves each person, usually for a limited time and number of prompts.</p>
        <label class="f" for="note">Note to the owner <span class="muted" style="font-weight:400">(optional)</span></label>
        <textarea id="note" maxlength="140" style="min-height:64px" placeholder="Who are you? 140 characters."></textarea>
        <div class="row between" style="margin-top:6px"><span class="hint" style="margin:0">The owner sees your key <span class="mono">${MOCK.me.fp}</span> and this note. Not your IP address.</span><span class="hint num" id="cnt" style="margin:0">0 / 140</span></div>
        <div class="row" style="margin-top:12px"><button class="btn" id="ask">Send request</button>${alt}</div>`);
      locked('You can chat once the owner approves');
      const note = g.querySelector('#note'); note.oninput = () => { g.querySelector('#cnt').textContent = note.value.length + ' / 140'; };
      g.querySelector('#ask').onclick = () => { s.mine.req = 'pending'; s.mine.sent = '10:07'; set.pending(true); };
    },
    pending(justSent) {
      const g = gate(`<h3>Request sent${justSent ? ' just now' : ' at ' + (s.mine.sent || '10:02')}. Waiting for the owner.</h3>
        <p class="muted">You can close this tab: your request stays, and this page checks again when you come back. While it is open we check every 15 s. Owner key <span class="mono">${s.owner_fp}</span> has your key <span class="mono">${MOCK.me.fp}</span>.</p>
        <div class="row"><button class="btn sm sec" id="check">Check now</button>${alt}<button class="btn sm ghost" id="demo-approve">Prototype: simulate approval</button></div>`);
      locked('You can chat once the owner approves');
      g.querySelector('#check').onclick = () => toast('Still pending (mock)');
      g.querySelector('#demo-approve').onclick = () => goto('approved');
    },
    approved() {
      gate(`<h3 style="color:var(--accent)">Approved</h3><p style="margin:0">The owner of ${esc(s.name)} gave your key access until Thu 22 Oct, 10:05: 20 requests and 4,000 tokens. The box below is now open.</p>`).classList.add('good');
      dock.innerHTML = composer('<span class="num">20 of 20 requests left</span>'); wireSend();
    },
    declined() {
      gate(`<h3>The owner declined this request</h3><p class="muted">That is the owner's call, and you are not told why. You can ask again after Thu 22 Oct, 10:07.</p><div class="row">${alt}<a class="btn sm sec" href="swarms.html#browse">Browse other swarms</a></div>`).classList.add('bad');
      locked('You cannot chat with this swarm right now');
    },
    expired() {
      const g = gate(`<h3>Your access ended</h3><p class="muted">It ran until Tue 20 Oct, 10:05. Ask again and the owner sees your earlier note.</p><div class="row"><button class="btn" id="again">Ask again</button>${alt}</div>`);
      locked('Ask for access to chat again');
      g.querySelector('#again').onclick = () => { s.mine.req = 'pending'; set.pending(true); };
    },
    quota() {
      const g = gate(`<h3>You used all 20 requests of this access</h3><p class="muted">The owner can give you more.</p><div class="row"><button class="btn" id="again">Ask for more</button>${alt}</div>`);
      locked('Ask the owner for more to continue');
      g.querySelector('#again').onclick = () => set.pending(true);
    },
    invite() {
      const g = gate(`<h3>${esc(s.name)} is invite only</h3><p class="muted">The owner grants access to a browser key. Send them yours by any channel you already use; once they grant it, this page unlocks by itself.</p>
        <div class="row"><span class="mono" style="font-size:1.05rem">${MOCK.me.fp}</span><button class="btn sm sec" id="copy">Copy my key</button></div>
        <p class="hint">Owner key <span class="mono">${s.owner_fp}</span></p><div class="row">${alt}</div>`);
      locked('Invite only: waiting for the owner to grant your key');
      g.querySelector('#copy').onclick = () => toast('Key copied (mock)');
    },
    offline() {
      gate(`<h3>${esc(s.name)} is offline</h3><p class="muted">Its entry Mac last reported ${ago(s.last_seen)}. Swarms run on volunteer Macs, and Macs sleep.</p><div class="row">${alt}<a class="btn sm sec" href="swarm.html?id=${s.id}">Swarm details</a></div>`);
      locked('Offline: nothing to send to right now');
    },
    killed() {
      gate(`<h3>Stopped by the swarm owner</h3><p class="muted">New prompts are refused until the owner releases the stop switch.</p><div class="row">${alt}</div>`).classList.add('bad');
      locked('Paused by the owner');
    },
  };
  function wireSend(wait) {
    const send = dock.querySelector('#send'), prompt = dock.querySelector('#prompt');
    send.onclick = () => {
      const text = prompt.value.trim(); if (!text) { prompt.focus(); return; }
      prompt.value = ''; add('you', esc(text)); startStream(wait);
    };
    prompt.onkeydown = (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send.click(); } };
  }
  function startStream(wait) {
    const send = dock.querySelector('#send'), prompt = dock.querySelector('#prompt');
    send.disabled = true; prompt.disabled = true;
    const words = 'The harbour is closed because the storm damaged the quay, so ferries now leave from the north jetty. The letter stays unopened until the final chapter.'.split(' ');
    const bubble = add('swarm', '<span class="cursor" aria-hidden="true"></span>');
    const stopBtn = document.createElement('button'); stopBtn.className = 'btn sec'; stopBtn.id = 'stop'; stopBtn.textContent = 'Stop'; send.replaceWith(stopBtn);
    let i = 0, stopped = false, timer;
    const begin = () => {
      timer = setInterval(() => {
        if (stopped) return;
        i++; bubble.innerHTML = esc(words.slice(0, i).join(' ')) + '<span class="cursor" aria-hidden="true"></span>'; log.scrollTop = log.scrollHeight;
        root.querySelector('#lb-speed').textContent = (6.1 + Math.sin(i) * 0.4).toFixed(1) + ' tok/s';
        if (i >= words.length) finish('Completed');
      }, 140);
    };
    const finish = (label) => {
      clearInterval(timer); bubble.innerHTML = esc(words.slice(0, i).join(' ')) + `<span class="m num">${label} · ${i} tokens · ${t ? t.tok_s.toFixed(1) : '–'} tok/s · served by ${s.helpers.map((h) => h.label.replace(' (entry)', '')).join(' → ')}</span>`;
      const b = document.createElement('button'); b.className = 'btn'; b.id = 'send'; b.textContent = 'Send'; stopBtn.replaceWith(b); prompt.disabled = false; b.onclick = () => { const x = prompt.value.trim(); if (x) { prompt.value = ''; add('you', esc(x)); startStream(); } };
      root.querySelector('#lb-speed').textContent = t ? t.tok_s.toFixed(1) + ' tok/s' : '–';
    };
    stopBtn.onclick = () => { stopped = true; finish('Stopped by you'); };
    if (wait) { bubble.innerHTML = '<span class="muted">Waiting for your turn (1 ahead)…</span>'; setTimeout(begin, 1500); } else begin();
  }
  (set[state] || set.ready)();
  return state;
}
function pageChat() {
  const id = qs.get('id') || 'reading', s = MOCK.byId(id);
  const chatable = MOCK.swarms.filter((x) => x.status !== 'offline' && (x.access === 'open' || x.mine.grant || x.mine.role === 'owner'));
  const list = chatable.some((x) => x.id === id) ? chatable : [s, ...chatable];
  $('chat-list').innerHTML = chatList(list, id);
  const state = mountChat($('chat-pane'), id, qs.get('state'));
  const sel = $('proto-state');
  sel.innerHTML = CHAT_STATES.map((x) => `<option ${x === state ? 'selected' : ''}>${x}</option>`).join('');
  sel.onchange = () => { location.href = `chat.html?id=${id}&state=${sel.value}`; };
  const swSel = $('proto-swarm');
  swSel.innerHTML = MOCK.swarms.map((x) => `<option value="${x.id}" ${x.id === id ? 'selected' : ''}>${esc(x.name)}</option>`).join('');
  swSel.onchange = () => { location.href = `chat.html?id=${swSel.value}`; };
  wireKeyChip();
}

// ---------- owner ----------
function pageOwner() {
  const s = MOCK.byId('family');
  let pending = MOCK.requests.slice();
  const preset = { hours: 24, req: 20, tok: 4000 };
  const fmt = () => `${preset.hours} h · ${preset.req} requests · ${preset.tok.toLocaleString('en')} tokens`;
  function counts() {
    $('o-count').textContent = pending.length; $('o-count').classList.toggle('alert', pending.length > 0);
    $('o-tabcount').textContent = pending.length; $('o-tabcount').classList.toggle('alert', pending.length > 0);
    const b = $('o-bar'); if (b) b.textContent = pending.length;
    $('o-lede').textContent = pending.length ? `${pending.length} ${pending.length === 1 ? 'person is' : 'people are'} waiting for your answer.` : 'Nobody is waiting. New requests appear here.';
    $('approve-all').hidden = pending.length < 2;
  }
  function renderReqs() {
    $('o-reqs').innerHTML = MOCK.requests.map((r, i) => `<div class="req" data-i="${i}">
      <div><div class="who"><span class="mono">${r.fp}</span> <span class="muted" style="font-weight:400">· ${r.ago}</span></div>
      <p class="note-txt">${r.note ? '“' + esc(r.note) + '”' : 'No note.'}</p><p class="hint" style="margin:4px 0 0">Will get: ${fmt()}</p></div>
      <div class="acts"><button class="btn" data-act="approve">Approve</button><button class="btn sec" data-act="decline">Decline</button></div></div>`).join('');
  }
  function resolve(i, how) {
    const row = document.querySelector(`.req[data-i="${i}"]`); const r = MOCK.requests[i];
    pending = pending.filter((x) => x !== r);
    row.innerHTML = how === 'approve'
      ? `<div><div class="who"><span class="mono">${r.fp}</span> <span class="tag you">approved</span></div><p class="note-txt">${fmt()}. Their page unlocks on its next check.</p></div><div class="acts"><button class="btn sm sec" data-act="undo">Undo</button></div>`
      : `<div><div class="who"><span class="mono">${r.fp}</span> <span class="tag">declined</span></div><p class="note-txt">They are told you declined, nothing more.</p></div><div class="acts"><button class="btn sm sec" data-act="undo">Undo</button></div>`;
    row.dataset.done = how; counts();
    toast(how === 'approve' ? 'Approved. Their browser picks it up within seconds.' : 'Declined.');
  }
  renderReqs(); counts();
  $('o-reqs').addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]'); if (!b) return; const row = b.closest('.req'); const i = Number(row.dataset.i);
    if (b.dataset.act === 'undo') { pending.push(MOCK.requests[i]); const r = MOCK.requests[i]; row.removeAttribute('data-done'); row.innerHTML = `<div><div class="who"><span class="mono">${r.fp}</span> <span class="muted" style="font-weight:400">· ${r.ago}</span></div><p class="note-txt">${r.note ? '“' + esc(r.note) + '”' : 'No note.'}</p><p class="hint" style="margin:4px 0 0">Will get: ${fmt()}</p></div><div class="acts"><button class="btn" data-act="approve">Approve</button><button class="btn sec" data-act="decline">Decline</button></div>`; counts(); return; }
    resolve(i, b.dataset.act);
  });
  $('approve-all').addEventListener('click', () => { document.querySelectorAll('.req:not([data-done])').forEach((r) => resolve(Number(r.dataset.i), 'approve')); });
  $('preset-now').textContent = fmt();
  ['hours', 'req', 'tok'].forEach((k) => $('p-' + k).addEventListener('input', (e) => { preset[k] = Math.max(1, Number(e.target.value) || 1); $('preset-now').textContent = fmt(); document.querySelectorAll('.req:not([data-done]) .hint').forEach((h) => { h.textContent = 'Will get: ' + fmt(); }); }));
  $('o-people').innerHTML = MOCK.people.map((p) => `<tr><td><span class="mono">${p.fp}</span><br><span class="muted">${p.label}</span></td><td class="num">until ${p.until}</td>
    <td><span class="num">${p.req} / ${p.req_max} requests</span><br><span class="meter" aria-hidden="true" style="display:block;margin-top:4px"><i style="width:${p.req / p.req_max * 100}%"></i></span></td><td style="text-align:right"><button class="btn sm sec" data-revoke>Revoke</button></td></tr>`).join('');
  $('o-people').addEventListener('click', (e) => { if (e.target.closest('[data-revoke]')) toast('Revoke is sent to your entry Mac (needs the entry revoke route, phase 3)'); });
  $('o-kill').addEventListener('click', (e) => { const on = e.target.dataset.on === '1'; e.target.dataset.on = on ? '0' : '1'; e.target.textContent = on ? 'Stop new prompts' : 'Release: allow prompts again'; $('o-kill-state').textContent = on ? 'Running normally.' : 'Stopped. New prompts and open grants are refused.'; });
  $('o-stats').innerHTML = `<div class="stats" style="grid-template-columns:repeat(4,minmax(0,1fr))">${statCells(s)}</div>`;
  $('o-link').textContent = 'mycelium-swarm:eyJkaXJlY3RvcnkiOiJodHRwczovL215Y2VsaXVtLm5vdyIsLi4u (mock)';
  wireKeyChip();
}

// ---------- create ----------
function pageCreate() {
  const sel = $('c-model');
  sel.innerHTML = '<option value="" selected disabled>Choose a model…</option>' + MOCK.models.map((m, i) => `<option value="${i}">${m.label} · ${m.gb} GB download · ${m.layers} layers</option>`).join('');
  const info = $('c-model-info');
  sel.addEventListener('change', () => {
    const m = MOCK.models[Number(sel.value)];
    info.innerHTML = `<b>${m.label}</b> <span class="muted">(${m.note})</span><br><span class="muted">${m.id} · ${m.gb} GB download · ${m.layers} layers, split across your Macs by memory.</span>`;
    $('c-go').disabled = !$('c-name').value.trim();
  });
  $('c-name').addEventListener('input', () => { $('c-go').disabled = !($('c-name').value.trim() && sel.value !== ''); });
  document.querySelectorAll('input[name=mode]').forEach((r) => r.addEventListener('change', () => { $('c-quota').hidden = r.value !== 'open' || !r.checked; }));
  $('c-form').addEventListener('submit', (e) => {
    e.preventDefault();
    $('c-step1').hidden = true; $('c-step2').hidden = false; $('c-done-name').textContent = $('c-name').value.trim();
    $('c-done-model').textContent = MOCK.models[Number(sel.value)].label; window.scrollTo({ top: 0 });
  });
  wireKeyChip();
}
const PAGE = { index: pageIndex, swarms: pageSwarms, swarm: pageSwarm, chat: pageChat, owner: pageOwner, create: pageCreate }[document.body.dataset.page];
if (PAGE) PAGE();
