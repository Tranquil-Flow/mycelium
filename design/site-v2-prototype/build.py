#!/usr/bin/env python3
"""Generate the static prototype pages (shared shell, identical on every page). Run from this folder."""
from pathlib import Path

HERE = Path(__file__).resolve().parent

KEYDLG = '''<dialog id="keydlg" aria-labelledby="keydlg-t"><h2 id="keydlg-t" style="font-size:1.2rem;margin-bottom:10px">Your key</h2>
<p>This browser made a key. It is how swarms recognise you: <b>no email, no password, no account.</b> The private half never leaves this browser.</p>
<p class="mono" style="font-size:1.05rem">2F00-F2E5-3E8E-2E5F</p>
<p class="muted">Swarm owners grant access to this fingerprint. Swarms you chat with can link your prompts to it.</p>
<div class="row"><button class="btn sm" id="keydlg-close">Close</button><button class="btn sm sec" id="forget">Forget this key…</button></div>
<div id="forget-confirm" class="note bad" hidden style="margin-top:12px"><b class="lead">Forget this key?</b> You lose every access you were given and you can no longer manage the swarms you own with it. A new key starts with a fresh allowance, but limits by network address still apply.
<div class="row" style="margin-top:10px"><button class="btn sm danger" id="forget-yes">Forget key</button><button class="btn sm sec" id="forget-no">Keep it</button></div></div></dialog>'''

def proto_bar(extra=''):
    return f'<div class="proto" role="note"><span><b>Design prototype</b><span class="rest">: mock data, no backend, not the live site.</span></span>{extra}</div>'

def header(app=True, current=''):
    nav = '<nav class="primary" aria-label="Site"><a href="index.html"%s>How it works</a><a href="swarms.html"%s>Swarms</a><a href="index.html#privacy">Privacy</a></nav>' % (
        ' aria-current="page"' if current == 'index' else '', ' aria-current="page"' if app else '')
    if app:
        right = '<button class="keychip" id="keychip" type="button" aria-haspopup="dialog"><span class="lbl">Your key</span><span class="mono full">2F00-F2E5-3E8E-2E5F</span><span class="short mono">2F00…2E5F</span></button>'
    else:
        right = '<a class="btn sm" href="swarms.html">Open swarms</a>'
    return f'<header class="top"><div class="wrap"><a class="brand" href="index.html">✳ Mycelium</a>{nav}<span class="spacer"></span>{right}</div></header>'

def tabbar(current):
    items = [('browse', 'Browse', ''), ('chat', 'Chat', ''), ('contributing', 'Helping', ''), ('owned', 'Owned', '<span class="count alert" id="o-bar">2</span>')]
    out = '<nav class="tabbar" aria-label="Swarms sections">'
    for key, label, extra in items:
        cur = ' aria-current="page"' if key == current else ''
        out += f'<a id="bar-{key}" href="swarms.html#{key}"{cur}>{label}{extra}</a>'
    return out + '</nav>'

def tablinks(current, o_count=True):
    items = [('browse', 'Browse', '<span class="count">7</span>'), ('chat', 'Chat', '<span class="count">3</span>'),
             ('contributing', 'Contributing', '<span class="count">1</span>'),
             ('owned', 'Owned', '<span class="count alert" id="o-tabcount">2</span>' if o_count else '<span class="count alert">2</span>')]
    out = '<nav class="tabs" aria-label="Swarms sections">'
    for key, label, c in items:
        cur = ' aria-current="page"' if key == current else ''
        out += f'<a class="tab" href="swarms.html#{key}"{cur}>{label}{c}</a>'
    return out + '</nav>'

def sk_card():
    return ('<article class="card swarm-card" aria-hidden="true"><div class="row between"><span class="skel" style="width:46%;height:24px"></span><span class="skel" style="width:84px;height:24px"></span></div>'
            '<div class="meta"><span class="skel" style="width:60%;height:18px"></span></div><p class="desc"><span class="skel" style="display:block;width:80%;height:18px"></span></p>'
            '<div class="stats" style="height:66px;border-color:transparent"><span class="skel" style="grid-column:1/-1;height:66px;border-radius:10px"></span></div>'
            '<div class="foot"><span class="skel" style="width:90px;height:18px"></span><span class="skel" style="width:76px;height:36px;border-radius:10px"></span></div></article>')

def sk_tile():
    return ('<div class="card bs" aria-hidden="true"><div class="k skel" style="width:35%;height:16px"></div><div class="v skel" style="width:48%;height:34px"></div>'
            '<div class="sub skel" style="width:88%;height:16px"></div><div class="src skel" style="width:40%;height:14px"></div></div>')

def page(name, title, body, app=True, current_tab='', extra_proto='', hdr_current=''):
    mobile_tabbar = tabbar(current_tab) if app and current_tab else ''
    if app and current_tab and name != 'swarms':
        i = body.index('>', body.index('<main id="main"')) + 1
        body = body[:i] + tablinks(current_tab) + body[i:]
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><meta name="color-scheme" content="dark light"><title>{title}</title>
<link rel="stylesheet" href="site.css"></head>
<body data-page="{name}">
<a class="skip" href="#main">Skip to content</a>
{proto_bar(extra_proto)}
{header(app, hdr_current)}
{body}
{KEYDLG if app else ''}
{mobile_tabbar}
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<script src="mock-data.js"></script><script src="app.js"></script>
</body></html>
'''

# ------------------------------------------------------------------ apex
DIAGRAM = '''<svg viewBox="0 0 900 250" role="img" aria-labelledby="dg-t dg-d"><title id="dg-t">How a prompt travels</title>
<desc id="dg-d">Your browser sends a prompt directly to the swarm's entry Mac, which passes it through the helper Macs. The answer streams back the same way. The directory only lists swarms and never sees prompts.</desc>
<rect class="box" x="10" y="10" width="300" height="42" rx="10" stroke-dasharray="4 4"/><text x="160" y="36" text-anchor="middle" class="mu">mycelium.now · the phone book</text>
<path class="ln dim" d="M85 52 V100"/><text class="mu" x="98" y="82">lists swarms, never sees prompts</text>
<rect class="box" x="10" y="100" width="150" height="70" rx="12"/><text x="85" y="132" text-anchor="middle" font-weight="650">Your browser</text><text class="mu" x="85" y="152" text-anchor="middle">holds your key</text>
<rect class="box" x="260" y="100" width="170" height="70" rx="12"/><text x="345" y="132" text-anchor="middle" font-weight="650">Entry Mac</text><text class="mu" x="345" y="152" text-anchor="middle">layers 0–11</text>
<rect class="box" x="520" y="100" width="170" height="70" rx="12"/><text x="605" y="132" text-anchor="middle" font-weight="650">Helper Mac</text><text class="mu" x="605" y="152" text-anchor="middle">layers 12–23</text>
<rect class="box" x="770" y="100" width="120" height="70" rx="12" stroke-dasharray="4 4"/><text x="830" y="132" text-anchor="middle" font-weight="650">More Macs</text><text class="mu" x="830" y="152" text-anchor="middle">optional</text>
<path class="ln" d="M160 124 H260"/><path class="ln" d="M430 124 H520"/><path class="ln" d="M690 124 H770"/>
<path class="ln dim" d="M260 150 H160"/><text class="mu" x="210" y="116" text-anchor="middle">prompt</text><text class="mu" x="210" y="170" text-anchor="middle">answer</text>
<text class="mu" x="450" y="222" text-anchor="middle">Prompts go straight to the entry Mac. Everyone on the path with a Mac can read them.</text>
</svg>'''

INDEX = f'''<main id="main">
<section class="hero"><div class="wrap">
<p class="eyebrow">Preview · Apple silicon Macs</p>
<h1>Run an AI model across a few Macs you trust.</h1>
<p class="lead">Mycelium splits a language model across Macs, so no single Mac has to hold the whole thing. Chat with a swarm someone runs, lend your Mac to one, or start your own.</p>
<div class="row"><a class="btn" href="swarms.html">Browse swarms</a><a class="btn sec" href="create.html">Start a swarm</a></div>
</div></section>

<section class="wrap" aria-labelledby="status-h"><div class="card statusband" id="statusband">
<div class="row between"><h2 id="status-h" style="font-size:1.05rem">Right now</h2><span class="muted" style="font-size:13px" id="sb-age">Updated just now</span></div>
<div class="sb-nums" id="sb-nums" aria-live="off"></div>
<div class="tiles" id="sb-tiles"></div>
<div class="row between" style="margin-top:auto"><span class="muted" style="font-size:13px">Reported by each swarm's Macs. A swarm that goes quiet shows as offline after 90 seconds.</span><a href="swarms.html">See all swarms →</a></div>
</div></section>

<section class="sect" id="how"><div class="wrap">
<p class="eyebrow">How it works</p><h2 style="font-size:1.9rem;max-width:26ch;margin-bottom:28px">A swarm is a model shared out across Macs.</h2>
<div class="how">
<div><h3>Someone starts a swarm</h3><p class="muted">They pick a registered model and a name, and decide who may use it: anyone, people they approve, or invited keys. Their browser key signs the listing. There are no accounts.</p></div>
<div><h3>Macs join and share the work</h3><p class="muted">Each helper Mac holds a slice of the model's layers. The owner approves every Mac that joins, and a Mac can pause or leave whenever it needs its resources back.</p></div>
<div><h3>You send a prompt</h3><p class="muted">Your browser sends it straight to the swarm's own entry Mac. It passes through the helper Macs, and the answer streams back token by token.</p></div>
</div>
<div class="diagram">{DIAGRAM}</div>
</div></section>

<section class="sect" id="privacy" style="padding-top:8px"><div class="wrap">
<p class="eyebrow">Privacy, plainly</p><h2 style="font-size:1.9rem;max-width:26ch;margin-bottom:24px">Who can see what.</h2>
<div class="see">
<div><b>This site (the directory)</b><span class="muted">A public list of swarms: names, models, owner key, status. It never sees your prompts, sets no cookies and runs no analytics.</span></div>
<div><b>The swarm's Macs</b><span class="muted">The entry Mac and the helper Macs can read your prompt and the activations. It is <u>not</u> end-to-end encrypted from them. Use swarms you trust.</span></div>
<div><b>Cloudflare</b><span class="muted">Swarm entries sit behind Cloudflare. It ends the HTTPS connection, so it can see your IP address, timing, and prompts and answers in plaintext.</span></div>
<div><b>You</b><span class="muted">A key made in your browser identifies you to swarms. No email, no password. It never leaves the browser, and you can forget it any time.</span></div>
</div></div></section>

<section class="sect" style="padding-top:8px"><div class="wrap">
<p class="eyebrow">Ways in</p>
<div class="roles">
<a href="swarms.html#browse"><b>Chat with a swarm →</b><span class="muted">See which swarms are up, how fast they are, and ask for access if they need it.</span></a>
<a href="swarms.html#contributing"><b>Lend a Mac →</b><span class="muted">Join a swarm from the Mycelium Mac app and see how your Mac is helping.</span></a>
<a href="create.html"><b>Start a swarm →</b><span class="muted">Pick a model from the list, choose who may use it, then attach your Mac.</span></a>
</div></div></section>

<section class="sect faq" style="padding-top:8px"><div class="wrap">
<p class="eyebrow">Questions</p>
<details><summary>Is this ready to rely on?</summary><p>No. It is a preview. It has been proven on two Apple silicon Macs the project owns. The public demo swarm runs on one volunteer Mac behind a tunnel and may sleep.</p></details>
<details><summary>What does "serving" mean?</summary><p>The swarm's Macs have loaded the model and the route between them has passed its checks. "Warming up" means that is still happening. "Offline" means the swarm stopped reporting.</p></details>
<details><summary>Why choose a model from a list instead of typing one?</summary><p>Each model needs a pinned set of files and a checked layer layout, so swarms can only run models Mycelium has registered. The list grows with releases.</p></details>
</div></section>
</main>
<footer class="foot"><div class="wrap"><span>AGPL-3.0 · Mycelium · This site sets no cookies, runs no analytics and keeps no logs.</span><span>Prompts go from your browser directly to a swarm's entry Mac, never through this directory.</span></div></footer>'''

# ------------------------------------------------------------------ swarms (tabs)
SWARMS = '''<main id="main" class="app wrap">
<div class="page-head"><div><h1>Swarms</h1><p>Find a swarm, chat with it, or run one.</p></div><a class="btn" href="create.html">Start a swarm</a></div>
<div class="tabs" role="tablist" aria-label="Swarms sections">
<button role="tab" id="tab-browse" aria-controls="panel-browse" aria-selected="true">Browse <span class="count">7</span></button>
<button role="tab" id="tab-chat" aria-controls="panel-chat" aria-selected="false" tabindex="-1">Chat <span class="count">3</span></button>
<button role="tab" id="tab-contributing" aria-controls="panel-contributing" aria-selected="false" tabindex="-1">Contributing <span class="count">1</span></button>
<button role="tab" id="tab-owned" aria-controls="panel-owned" aria-selected="false" tabindex="-1">Owned <span class="count alert">2</span></button>
</div>

<section id="panel-browse" role="tabpanel" aria-labelledby="tab-browse">
<div class="toolbar"><label class="sr" for="search">Search swarms</label><input class="search" id="search" type="search" placeholder="Search by name or model">
<button class="chip" data-filter="all" aria-pressed="true">All</button><button class="chip" data-filter="serving" aria-pressed="false">Serving</button><button class="chip" data-filter="open" aria-pressed="false">Open to anyone</button><button class="chip" data-filter="request" aria-pressed="false">Needs approval</button></div>
<p class="muted" style="font-size:13.5px;margin:-4px 0 14px"><span id="browse-count" class="num">7 swarms</span> · numbers are reported by each swarm and refresh quietly; nothing moves while you read.</p>
<div class="grid g2" id="browse-grid">''' + sk_card() * 6 + '''</div>
<div class="card" id="browse-empty" hidden><b>No swarm matches.</b> <span class="muted">Try another filter, or start one.</span></div>
</section>

<section id="panel-chat" role="tabpanel" aria-labelledby="tab-chat" hidden>
<div class="chat-layout"><aside aria-label="Swarms you can chat with"><p class="muted" style="font-size:13.5px;margin:0 0 10px">Open swarms, swarms that approved you, and swarms you own.</p><div class="chat-list" id="chat-list"></div></aside><div id="chat-pane"></div></div>
</section>

<section id="panel-contributing" role="tabpanel" aria-labelledby="tab-contributing" hidden>
<div class="grid g2"><div class="grid" id="contrib-list" style="align-content:start"></div>
<div class="card"><h3 style="margin-bottom:8px">Lend another Mac</h3>
<ol class="steps" style="margin:12px 0"><li class="now"><span><b>Install the Mycelium app</b><br><span class="muted">Apple silicon Macs only. The app is not published yet: ask a swarm owner for it.</span></span><span></span></li>
<li><span><b>Join with the owner's invite</b><br><span class="muted">The owner sends a link or QR from "Invite a Mac". They approve your Mac.</span></span><span></span></li>
<li><span><b>See it here</b><br><span class="muted">In the app press "Show on mycelium.now". This page remembers it in your browser only.</span></span><span></span></li></ol>
<p class="hint">Your Mac is never listed by name for anyone else, only as a label the swarm shows. Pause or leave any time in the app.</p></div></div>
</section>

<section id="panel-owned" role="tabpanel" aria-labelledby="tab-owned" hidden>
<div class="grid g2"><div class="grid" id="owned-list" style="align-content:start"></div>
<div class="card"><h3 style="margin-bottom:8px">Start another swarm</h3><p class="muted">Pick a model from the registered list, choose who may use it, then attach a Mac from the app. You can own up to five swarms per browser key.</p><a class="btn sec" href="create.html">Start a swarm</a></div></div>
</section>
</main>'''

# ------------------------------------------------------------------ detail
DETAIL = '''<main id="main" class="app wrap">
<p class="crumbs"><a href="swarms.html#browse">← All swarms</a></p>
<div class="page-head" style="align-items:flex-start"><div><div class="row" style="gap:12px;min-height:44px"><h1 id="d-name">&nbsp;</h1><span id="d-pill"></span></div>
<div class="meta row" id="d-meta" style="gap:8px;color:var(--muted);margin-top:6px;min-height:24px"></div><p id="d-desc" style="margin-top:8px;max-width:60ch;min-height:26px"></p></div><div id="d-action" style="min-height:36px;min-width:120px"></div></div>
<div class="bigstats" id="d-stats" style="margin:20px 0 16px">''' + sk_tile() * 6 + '''</div>
<div class="grid g2" style="align-items:stretch">
<section class="card dcard" aria-labelledby="acc-h"><h2 id="acc-h" style="font-size:1.05rem;margin-bottom:8px">Access</h2><div id="d-access"></div></section>
<section class="card dcard" aria-labelledby="who-h"><h2 id="who-h" style="font-size:1.05rem;margin-bottom:12px">Who runs it, and who can read your prompts</h2><dl class="kv" id="d-who"></dl></section>
</div>
<section class="card" style="margin-top:16px" aria-labelledby="macs-h"><h2 id="macs-h" style="font-size:1.05rem;margin-bottom:8px">Macs in this swarm</h2>
<table class="t stack"><thead><tr><th>Mac</th><th>Layers</th><th></th><th>State</th><th>Path</th><th>Hop</th></tr></thead><tbody id="d-macs"></tbody></table></section>
</main>'''

# ------------------------------------------------------------------ chat
CHAT = '''<main id="main" class="app wrap">
<h1 class="sr">Chat</h1>
<div class="chat-layout"><aside aria-label="Swarms you can chat with"><div class="chat-list" id="chat-list"></div></aside><div id="chat-pane"></div></div>
</main>'''

# ------------------------------------------------------------------ owner
OWNER = '''<main id="main" class="app wrap">
<p class="crumbs"><a href="swarms.html#owned">← Owned swarms</a></p>
<div class="page-head" style="align-items:flex-start"><div><div class="row" style="gap:12px"><h1>Family swarm</h1><span class="pill serving"><i aria-hidden="true"></i>Serving</span></div>
<p>Qwen3-8B · By request · 2 Macs · you are the owner</p></div><a class="btn sec" href="swarm.html?id=family">Public page</a></div>
<div class="grid" style="grid-template-columns:minmax(0,1.6fr) minmax(0,1fr);gap:16px;align-items:start" id="o-cols">
<div class="grid">
<section class="card" aria-labelledby="req-h"><div class="row between"><h2 id="req-h" style="font-size:1.15rem">Requests <span class="count alert" id="o-count" style="margin-left:6px">2</span></h2><button class="btn sm sec" id="approve-all">Approve all with the default</button></div>
<p class="muted" id="o-lede" style="margin:6px 0 4px;min-height:24px"></p>
<div id="o-reqs"></div>
<details class="fold" style="margin-top:12px"><summary>Default for approvals: <span id="preset-now" style="margin-left:8px;font-weight:500" class="muted"></span></summary><div>
<div class="grid g3"><div><label class="f" for="p-hours">Hours</label><input id="p-hours" type="number" min="1" max="720" value="24"></div><div><label class="f" for="p-req">Requests</label><input id="p-req" type="number" min="1" max="10000" value="20"></div><div><label class="f" for="p-tok">Tokens</label><input id="p-tok" type="number" min="1" max="1000000" value="4000"></div></div>
<p class="hint">Kept in this browser. You can still change it per person when you approve.</p></div></details></section>
<section class="card" aria-labelledby="ppl-h"><h2 id="ppl-h" style="font-size:1.15rem;margin-bottom:8px">People with access</h2>
<table class="t stack"><thead><tr><th>Key</th><th>Access</th><th>Used</th><th></th></tr></thead><tbody id="o-people"></tbody></table></section>
</div>
<div class="grid">
<section class="card" aria-labelledby="stat-h"><h2 id="stat-h" style="font-size:1.05rem;margin-bottom:10px">How it is running</h2><div id="o-stats"></div><p class="hint">Queue free · up 2 d 4 h · helper Macs and layers are on the <a href="swarm.html?id=family">public page</a>.</p></section>
<section class="card" aria-labelledby="set-h"><h2 id="set-h" style="font-size:1.05rem;margin-bottom:10px">Setup <span class="tag you" style="margin-left:6px">complete</span></h2>
<p class="muted" style="margin:0 0 10px">Swarm created · Kitchen Mac attached (2F00-…-91D0) · second Mac joined.</p>
<details class="fold"><summary>Swarm link for another Mac</summary><div><p class="hint">Paste this in the Mycelium app ("Publish this run as a swarm") or scan the QR.</p><p class="mono" id="o-link" style="word-break:break-all;font-size:12px"></p></div></details></section>
<section class="card" aria-labelledby="acm-h"><h2 id="acm-h" style="font-size:1.05rem;margin-bottom:10px">Who may use it</h2>
<div class="radio-cards"><label><input type="radio" name="am" checked><span><b>By request</b><span class="muted">People ask; you approve each one.</span></span></label>
<label><input type="radio" name="am"><span><b>Open to anyone</b><span class="muted">Automatic, with a daily allowance per key.</span></span></label>
<label><input type="radio" name="am"><span><b>Invite only</b><span class="muted">Only keys you grant. Nobody can ask.</span></span></label></div>
<div class="row" style="margin-top:12px"><button class="btn sm" disabled>Save</button><span class="hint" style="margin:0">Entry Macs pick up the change within a minute.</span></div></section>
<details class="fold"><summary>Emergency stop</summary><div><p class="muted">Stops new prompts and open grants, and cancels what is running. Release it to reopen.</p><p id="o-kill-state" class="num" style="font-weight:650">Running normally.</p><button class="btn danger sm" id="o-kill" data-on="0">Stop new prompts</button></div></details>
</div></div></main>
<style>@media (max-width:900px){#o-cols{grid-template-columns:1fr!important}}</style>'''

# ------------------------------------------------------------------ create
CREATE = '''<main id="main" class="app wrap" style="max-width:760px">
<p class="crumbs"><a href="swarms.html#owned">← Swarms</a></p>
<div id="c-step1"><div class="page-head"><div><h1>Start a swarm</h1><p>Three choices. You can change who may use it later.</p></div></div>
<form id="c-form" class="grid" style="gap:22px" novalidate>
<div class="card"><label class="f" for="c-name">1. Name it</label><input id="c-name" type="text" maxlength="48" autocomplete="off" placeholder="Reading group"><p class="hint">Shown in the public list. 48 characters at most.</p></div>
<div class="card"><label class="f" for="c-model">2. Pick a model</label><select id="c-model"></select>
<div class="note info" id="c-model-info" style="margin-top:12px;min-height:76px"><span class="muted">Models are registered by Mycelium so every Mac loads exactly the same pinned files. The app checks that your Macs have enough memory before anything is downloaded.</span></div></div>
<fieldset class="card" style="margin:0;border:1px solid var(--line)"><legend class="f" style="padding:0 6px">3. Who may use it</legend>
<div class="radio-cards"><label><input type="radio" name="mode" value="request" checked><span><b>By request</b><span class="muted">People ask and you approve each one. Good for friends and groups.</span></span></label>
<label><input type="radio" name="mode" value="open"><span><b>Open to anyone</b><span class="muted">Automatic, with a daily allowance per key and per swarm. Anyone can read what you run, so keep the quota small.</span></span></label>
<label><input type="radio" name="mode" value="invite"><span><b>Invite only</b><span class="muted">Only keys you grant. People cannot ask.</span></span></label></div>
<div id="c-quota" hidden class="grid g2" style="margin-top:14px"><div><label class="f" for="q-req">Requests per key</label><input id="q-req" type="number" value="20" min="1" max="1000"></div><div><label class="f" for="q-tok">Tokens per key</label><input id="q-tok" type="number" value="4000" min="1" max="200000"></div></div></fieldset>
<div class="card"><label class="f" for="c-desc">Description <span class="muted" style="font-weight:400">(optional)</span></label><input id="c-desc" type="text" maxlength="280" placeholder="What is this swarm for?"></div>
<div class="row"><button class="btn" id="c-go" type="submit" disabled>Create swarm</button><span class="hint" style="margin:0">Signed with your browser key. No account needed.</span></div>
</form></div>
<div id="c-step2" hidden><div class="page-head"><div><h1>Swarm created</h1><p><b id="c-done-name"></b> · <span id="c-done-model"></span> · not serving yet</p></div></div>
<div class="note good"><b class="lead">It is listed, but offline.</b> A swarm serves once a Mac running the Mycelium app is attached to it.</div>
<ol class="steps" style="margin:20px 0"><li class="done"><span><b>Swarm created</b></span><span></span></li>
<li class="now"><span><b>Attach your Mac</b><br><span class="muted">In the Mycelium app choose "Publish this run as a swarm" and paste the link below, or scan the QR.</span></span><span></span></li>
<li><span><b>Add helper Macs</b><br><span class="muted">Use "Invite a Mac" in the app. You approve each Mac.</span></span><span></span></li>
<li><span><b>Share it</b><br><span class="muted">Once it shows Serving, others can find it in the list.</span></span><span></span></li></ol>
<div class="card"><p class="mono" style="word-break:break-all;font-size:12px;margin:0">mycelium-swarm:eyJwcm90b2NvbCI6Im15Y2VsaXVtLnN3YXJtLmxpbmsudjEi… (mock)</p></div>
<div class="row" style="margin-top:16px"><a class="btn" href="owner.html">Go to your swarm</a><a class="btn sec" href="swarms.html#owned">Back to Owned</a></div></div>
</main>'''

def write(name, html):
    (HERE / name).write_text(html)
    print('wrote', name, len(html))

write('index.html', page('index', 'Mycelium: run an AI model across a few Macs you trust', INDEX, app=False, hdr_current='index'))
write('swarms.html', page('swarms', 'Mycelium · Swarms', SWARMS, current_tab='browse'))
write('swarm.html', page('swarm', 'Mycelium · Swarm', DETAIL, current_tab='browse'))
write('chat.html', page('chat', 'Mycelium · Chat', CHAT, current_tab='chat',
      extra_proto='<label>Swarm <select id="proto-swarm" style="min-height:24px;padding:0 6px;width:150px"></select></label><label>State <select id="proto-state" style="min-height:24px;padding:0 6px;width:150px"></select></label>'))
write('owner.html', page('owner', 'Mycelium · Owner view', OWNER, current_tab='owned'))
write('create.html', page('create', 'Mycelium · Start a swarm', CREATE, current_tab='owned'))
