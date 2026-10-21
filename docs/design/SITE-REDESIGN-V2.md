<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Site redesign V2: apex explainer, swarms app, one clean path to chat

Status: **design only** (lane `lane/rel2-siteux`, from `rel2/wave1-staging` ff1edc32). No product code, no deploy, no push.
Clickable prototype: `design/site-v2-prototype/` (mock data, open `index.html` through any static server).
Audit evidence: `docs/design/site-v2-audit/` (screenshots of today's pages) and this document's section 1.

Facts below were read from the code at the base commit or measured on the live pages on 2026-10-21 (Chromium via Playwright, 1440x900
and 390x844). Anything that is a proposal is labelled **proposal**; anything I could not check is labelled **unverified**.

## 0. Summary

* Today the apex is a redirect to a long, one-column admin page. The thing a visitor came for (the swarm list) starts at y=987 px on
  a 900 px screen (y=1512 on a phone), after three explanatory cards, and the page shifts while it loads (measured layout shift 0.40
  desktop, 0.18 mobile; "good" is under 0.1).
* The design splits the site by **job**, not by feature:
  * `mycelium.now/` : an **explainer** with one light "Right now" status band. No app chrome, no key, no network calls except one cacheable summary.
  * `mycelium.now/swarms/` : the **app**, four tabs: **Browse** (all swarms), **Chat** (swarms I can talk to), **Contributing** (swarms my Mac helps), **Owned** (swarms I run, with the request inbox). Each swarm has a **detail page with statistics**.
  * `demo.mycelium.now` stops being a "site". It is the **entry origin of one swarm** (the demo swarm) and keeps its small standalone `/chat` page as the fallback every swarm owner can share. The main chat experience lives in `/swarms` and calls the entry directly (browser to entry, CORS `*`, which already exists). Nothing is proxied through `mycelium.now`, so Profile A (R29/R30) holds.
* Request access becomes one in-place flow on the chat screen for the requester, and a one-tap inbox with a **badge** for the owner. Two real dead ends in today's flow are fixed (section 1.3: invite-only cannot be granted from the web UI; an approved requester sees a disabled Chat button with no reason).
* The model field becomes a **dropdown of the five registered models** (never typed), fed from the registry, enforced by the directory.
* Stats (token speed, latency, Macs, uptime, queue) get an honest data model: what exists today, what needs a **heartbeat v2** (integers only, aggregated, no prompt-derived data), and where a number is measured in the visitor's own browser instead.
* Zero layout shift is a rule with a CI gate, not a polish item. The prototype measures 0.000 to 0.0003 on all 22 page/theme combinations at both widths (44 runs).

## 1. Audit of today's pages

### 1.1 Method

* Screenshots at three moments (300 ms, 1.5 s, 5 s after navigation commit) and full page, desktop and mobile:
  `docs/design/site-v2-audit/{swarms-desktop-full,swarms-mobile-t1500,demo-desktop-t5000,demo-mobile-t5000}.png`.
* Layout shift measured with a `PerformanceObserver('layout-shift')` on the live pages.
* The request-access round trip was run end to end against a **local** directory (`python -m mycelium_swarm_directory --db ... --allow-loopback-entries`, two browser contexts = two keys), screenshots `docs/design/site-v2-audit/flow-{1,4,5}-*.png`. Nothing was written to the live directory.
* Live network behaviour was read from the page's own requests (the pages issue only their normal GET/POST calls).

### 1.2 What is on the live site today

| URL | What it is | Observed |
|---|---|---|
| `https://mycelium.now/` | `302 /swarms/` (`redirect_root_to_swarms`, pinned by `tests/demo/test_profile_a_site_closed.py`) | There is no page that says what Mycelium is. |
| `https://mycelium.now/swarms/` | Directory UI: `swarms.js` 539 lines, `index.html`, `swarms.css`. Three listings, **all named "Mycelium demo"**, same model and same entry origin: one serving, two offline | Section order: How it works, helper-app download, browser key, **All swarms**, notice, chat, create, owned. |
| `https://demo.mycelium.now/chat` | The A13 Mac app's own relay page (`mycelium_a13_macos/web/chat.html`) through a Cloudflare named tunnel. `/` is 404; `HEAD` on every path is 501 | One swarm, no list, no stats, no key. The live build has **no first-prompt notice** (that is `lane/rel2-site`, merged into the V2 staging branch, not deployed). |

### 1.3 Problem list (each with evidence)

**A. Layout shift and page jumps**

| # | Problem | Evidence |
|---|---|---|
| A1 | Layout shift 0.401 (desktop) and 0.175 (mobile) on `/swarms/`. The static demo badge is replaced by the longer `config.demo` text, "Creating your key…" and "Checking for the helper app…" are replaced by differently sized content. | PerformanceObserver on live page; sources `demo`, `card`, `identity-card`; `index.html` lines 7, 16, 21; `swarms.js` `main()` lines 520-536 |
| A2 | Every 15 s `refresh()` calls `$('swarms').replaceChildren(...)` and every card shows "Loading…" until its own live-status POST returns. The page is rebuilt under the reader's cursor and each card's height changes twice per refresh. | `swarms.js` 196-208, 168 (`'Loading…'`), 537 (`setInterval(refresh, 15000)`) |
| A3 | **A note the visitor is typing is destroyed.** The "Optional note to the owner" `<input>` is created inside `renderSwarm`, so the 15 s refresh replaces it and the typed text vanishes. The owner's Hours / Requests / Tokens inputs in `renderOwned` have the same flaw. | `swarms.js` 163, 497-499; only the access `<select>` is protected (`pendingMode`) |
| A4 | Opening a chat calls `$('chat-card').scrollIntoView()` on a card that sits *below* the notice, above Create; every message calls `div.scrollIntoView` which scrolls the whole page, not the log. | `swarms.js` 246, 272 |
| A5 | Request load: each refresh issues `GET swarms` + signed `POST my-grants` + one `POST /swarm-entry/v1/state` per swarm; `renderOwned` adds a signed `owner-inbox` POST per owned swarm. N+2 requests every 15 s, even in a hidden tab. | `swarms.js` 198-203, 357, 471-486; observed request list |
| A6 | A Cloudflare bot-detection snippet is injected into every HTML response (`/cdn-cgi/challenge-platform/scripts/jsd/main.js`). The page's CSP (`script-src 'self'`) stops it executing, so every load logs a CSP error. It does not run under our CSP, but the privacy copy says "no analytics" and does not mention that Cloudflare adds a script of its own. | `curl -sL` of all three URLs; console error on every page |

**B. Information architecture**

| # | Problem | Evidence |
|---|---|---|
| B1 | The swarm list starts at y=987 (desktop) / 1512 (mobile). A swarm card is 526 / 866 px tall, so a visitor sees one card at a time; Create starts at y=2270 / 3560. | measured with `getBoundingClientRect` |
| B2 | Everything is one scrolling page: no way to say "show me only what I can chat with" or "only mine". `grantFor()` knows, but the UI shows the same card to everyone. | `swarms.js` 143-180 |
| B3 | Nothing exists for a **contributor**. The only helper content is a card that says the app "is not published yet", plus a note that invites come "from the app". A person whose Mac helps a swarm cannot see it anywhere. | `index.html` 15-17, `renderDownload` |
| B4 | No statistics. The card shows model, Mac count, access mode, owner fingerprint, entry fingerprint, free-text description; the "Live status" block shows route step, helper rows, path counts, "Unavailable (adaptive_off)" style strings. Nothing a person can use to choose (speed, latency, uptime, busy or free). | `renderSwarm`, `renderLive` |
| B5 | Jargon in the primary view: "Route: serving · step serving · run a13-b17b9e-", "Observed paths: Unavailable (no_transport_observation)", "Recovery: Unavailable (recovery_source_unreachable)", "Entry Mac resources: Unavailable (adaptive_off)", owner fingerprint and entry fingerprint (often equal) on every card. | live card text (screenshot `swarms-desktop-full.png`) |
| B6 | Three different words for access across the page ("access open", "Get open access", "Automatic access", "Request access", "Not granted"), and "grant" is used for both the owner's act and the visitor's state. | `swarms.js` 150-166 |
| B7 | **Stale duplicates.** The live directory lists three swarms with the identical name "Mycelium demo" (one serving, two offline with 0 Macs, different owner keys, same entry origin). Offline listings are never pruned, so the list fills with old test swarms and a visitor cannot tell which is real. The only differentiator on the card is the owner fingerprint. | `GET /swarms/api/v1/swarms` on the live site; `Directory.list_swarms` has no expiry |

**C. Request access, today, step by step**

Requester (all in `swarms.js`, directory side in `directory.py`):

1. Opens `/swarms/`; sees the card of a `request` swarm: note "Not granted: ask for access and the owner decides." + a 140-char input + **Request access** (lines 159-164).
2. Clicks: `requestAccess` signs `mycelium.swarm.access_request.v1` and POSTs `/access-requests` (227-234; `_access_request` 211-229 stores `request` + a `pending` grantee row). Feedback is a line in the *list header* ("Request sent to X. The owner sees your key ...") that the next `refresh()` leaves in place, but the card note changes to "Not granted yet. Request sent. Waiting for the owner." (161).
3. Waits. No ETA, no way to nudge the owner, no persistence of what they asked for, no explanation that the owner has no notification.
4. After approval, the next 15 s poll turns the note to "You have access until ..." and shows **Chat**. If the swarm has no entry origin or is not `serving`, the button is **disabled without a reason** (line 151, `usable`). In my local run the approved requester saw exactly `Chat(disabled)`.
5. If declined: note "Not granted: the owner declined your request." and the same Request access box again (no cooldown, no way to see how many times they were refused).
6. **Invite-only swarms: dead end.** `_access_request` raises `swarm_invite_only` for mode `invite` (directory.py 217-218), the card tells the visitor to "ask the owner to grant your key <fingerprint>", and the owner UI has **no field to paste a key**. The owner can only grant from a request row (`approve(s, r.requester_key, ...)`) or "Grant myself". A 64-bit fingerprint cannot be turned back into the public key a grant needs.

Owner:

1. Creates a swarm: **Model is a free-text `<input value="Qwen/Qwen3-0.6B">`** validated only by the regex `MODEL` (`docs.py` 40), so a typo or an unregistered model creates a listing no Mac can serve. (Measured: `INPUT:text`.)
2. The owned card is the last section on a long page. Pending requests are rows inside it with three number inputs each and Approve / Decline (`renderOwned` 486-505). There is **no badge, no title change, no count** anywhere: an owner who is not looking at that card does not know anyone asked.
3. Approve signs a grant (`mycelium.swarm.grant.v1`), POSTs `/grants`; the row stays until the next refresh; no "Undo", no default policy remembered, no "approve all".
4. Revoking one grant "is done in the Mycelium app on the entry Mac" (text of the kill-switch block): the web owner cannot revoke a person.

**D. Chat**

| # | Problem | Evidence |
|---|---|---|
| D1 | Chat is a card that opens far from where the user clicked and "Close" hides it; there is no list of swarms I can chat with, no memory of the last swarm. | `openChat`, `chat-card` |
| D2 | On `/swarms/` the privacy notice is a section lower on the page reached by a link (`#notice`). The live standalone demo page shifts when its form is revealed after the state call (measured 0.004 desktop, 0.026 mobile). The V2 lane's first-prompt notice (`lane/rel2-site`, not live) is a block that sits above the form; I did **not** measure it, but it has the same shape as the problem and is moved into a reserved slot here. | live layout-shift; `chat.js` / `chat.html` in the V2 lane |
| D3 | Busy handling is a message: "Another prompt is running on this swarm. Try again in a few seconds." The user retypes or resubmits. | `MESSAGES.chat_busy` |
| D4 | The demo chat page has a max-tokens `<select>` (64/128/256) and no model, no speed, no queue indicator; the directory page uses a `type=number` max-tokens input. | `chat.html`, `index.html` 44 |
| D5 | Stats the page *could* show per answer are thrown away: `events` returns `seconds` on done, the log holds the token list, but speed is never displayed. | `chat.py` 417-428 |

**E. Misc**

* E1. The demo badge, the footer and the privacy note repeat the same Cloudflare paragraph three times in different words (long, dense, but correct).
* E2. Mobile: 4,498 px tall single column, 866 px cards, 44 px targets are not enforced (buttons are `.45rem` padding).
* E3. No light theme, no `prefers-reduced-motion` handling, no skip link, no landmark structure beyond `<header>/<main>/<footer>`, no focus style beyond browser default.
* E4. `HEAD` on the relay returns 501, so an uptime monitor that uses HEAD reports the demo as down. (A GET works.)

### 1.4 Why a dedicated demo site and not `/swarms`? (owner question)

Because `demo.mycelium.now` is not a second website; it is the **public entry of the Mycelium demo swarm**, and every swarm has its own entry:

* The entry is the owner Mac's own relay (`public_relay.py`) behind a Cloudflare named tunnel (`docs/FRIEND-SWARM.md`, "Use your own address"). It serves `/chat` (a single-swarm page with no key, limited by network address) and `/swarm-entry/v1/*` (signed, grant-checked, CORS `*`).
* `mycelium.now` is a different process on a different Mac path (proxy 4350 to directory 4360). Under Profile A it must stay a **phonebook**: it never carries a prompt. That is why the swarm page on mycelium.now calls `https://<entry>/swarm-entry/v1/send` directly from the browser (verified live: the OPTIONS preflight from `https://mycelium.now` returns `204` with `access-control-allow-origin: *`).
* The standalone `/chat` is still valuable: it is what an owner can send to a friend whose swarm is **not listed** (a quick-tunnel friend swarm), and it works if the directory is down.

So the answer is: keep it, but demote it. It is one swarm's door, not a product surface. The product surface is `/swarms`, which reaches any entry, including the demo's, with the same chat UI.

## 2. Design principles

1. **One job per page.** Apex explains. `/swarms` does. A swarm page decides.
2. **State, not logs.** Show what a person can do next ("Chat", "Ask to join", "Waiting for owner"), not protocol vocabulary. The protocol details live one click down, under "Details".
3. **Nothing moves after first paint.** Space is reserved; updates replace text in place; notices live in a slot that always exists.
4. **Say who can read your prompt, once, where it matters** (the composer and the first-prompt gate), in the same words everywhere.
5. **No accounts, no tracking** stays literal: the key is the identity; nothing about the visitor is sent to the directory except signed documents they chose to send.
6. **Numbers carry their source.** Every statistic says who measured it: the swarm, the directory, or your browser.
7. **Everything works with the keyboard, on a phone, in light and dark.**

## 3. Information architecture

```
mycelium.now/                  APEX: what it is, how it works, who can see what, "Right now" band, 3 ways in
mycelium.now/swarms/           APP (hash routes, no new server routes)
   #browse                       all swarms: search, filters, cards with 4 stats
   #chat[/<swarm_id>]            swarms I can chat with + the chat
   #contributing                 swarms where my Mac helps + how to lend one
   #owned                        swarms I run: request inbox, people, setup
   #swarm/<swarm_id>             detail: stats, Macs, access, who runs it
   #owned/<swarm_id>             owner view of one swarm
   #new                          create a swarm (name, model dropdown, access)
https://<entry>/chat           UNCHANGED standalone single-swarm page on each entry (demo: demo.mycelium.now)
```

Hash routes are deliberate: the directory server and the apex proxy serve a **fixed set of static assets** (pinned by
`tests/demo/test_profile_a_site_closed.py::test_the_proxy_serves_exactly_the_four_directory_assets...`), so a router that needs no new
server path adds no public surface. The prototype uses separate `.html` files only so each state is easy to open and screenshot; the mapping is
`swarms.html#browse|chat|contributing|owned`, `swarm.html?id=` to `#swarm/<id>`, `chat.html?id=` to `#chat/<id>`, `owner.html` to `#owned/<id>`, `create.html` to `#new`.

### Tab definitions (what each list contains and where it comes from)

| Tab | Contains | Computed from |
|---|---|---|
| Browse | every listed swarm | `GET /swarms/api/v1/swarms` |
| Chat | swarms with status `serving` or `warming` where **I can send**: access `open`, or I hold an unexpired grant (`POST my-grants`), or I own it | list + `my-grants` + open grants saved in IndexedDB + `owner_key == me` |
| Contributing | swarms whose `live.helpers.rows` contain a Mac this browser has **claimed** (section 5.4) | IndexedDB claims + entry `state` |
| Owned | `owner_key == me` | list; badge = pending requests (owner-inbox) |

Tab counts are small, tabular badges; **Owned** gets the accent colour only when there is something to decide.

## 4. User journeys

### 4.1 Visitor (no key action needed)

1. Lands on `/`. Reads the hero, sees "5 of 7 swarms serving, 15 Macs helping" and four tiles. One click to **Browse swarms**.
2. `#browse`: sees all swarms in two columns of cards, each with status, model, access, queue, and a four-cell stat strip (speed, latency, Macs, uptime). Filters: All / Serving / Open to anyone / Needs approval. Search by name or model.
3. Opens a card: detail page with six stat tiles, the Mac list, the access explanation, and "Who runs it, and who can read your prompts".
4. Clicks **Chat** (open swarm): the chat screen shows a gate with the three-line notice. One button, "I understand, start chatting", gets the open grant (a signed `open-grant` request to the entry, which exists today) and unlocks the composer **in place**.

### 4.2 Chatter who needs approval

`Browse`, **Ask to join** on a by-request swarm, lands on `#chat/<id>` in the *ask* state; writes an optional note; **Send request**; sees *pending* with the exact time; may leave. On return (or while open, every 15 s, paused when the tab is hidden) the same screen flips to *approved* and the composer enables. Declined / expired / out-of-quota states are covered in section 6.

### 4.3 Owner granting

Owner opens the site; the **Owned** tab and the mobile tab bar show a count badge (`2`). `#owned` shows a card per swarm with "2 people are waiting for your answer." and **Review requests**. The inbox shows each request (fingerprint, note, age, what they will get) with **Approve** / **Decline**, "Approve all with the default" when there are two or more, and an **Undo** per row. The default (24 h, 20 requests, 4,000 tokens) is kept in the owner's browser and editable in a fold.

### 4.4 Contributor Mac

The contributor already runs the Mycelium app. In the app they press **Show on mycelium.now** (proposal, section 5.4): it opens `https://mycelium.now/swarms/#claim=<swarm_id>.<node>`; the browser stores the claim locally. `#contributing` then shows "Your Mac Studio Mac runs layers 14-27 of 36", its state, path (direct or relay), hop latency, and the swarm's speed. Pausing or leaving stays in the app (the site cannot and should not control a Mac). A "Lend another Mac" panel lists the three real steps (install the app, join with the owner's invite, see it here) and says the app is not published yet, the same truth the page states today.

## 5. Request access redesigned

### 5.1 States (the requester's screen is one state machine)

```
first (open swarm, no allowance yet) -> ready
needs-request -> pending -> approved(ready) | declined | (expired|quota) -> needs-request
invite (invite-only, waiting for owner to grant my key) -> ready
busy / offline / killed (overlay states, do not change grant state)
```

All of these render in the **same two regions**: a *gate* panel at the top of the conversation area and a *composer* in the dock. The composer
is always present and always at the same place; in a gate state it is disabled with a one-line reason as its placeholder. Approval therefore
enables a box that is already on screen, instead of inserting a new block. Prototype: `chat.html?id=garage&state=needs-request|pending|approved|declined|expired|quota|invite|offline|killed|busy|first|ready` (the prototype bar has a state switcher).

### 5.2 Requester copy and behaviour (proposal)

* Gate title names the action ("Ask Garage Mac Pro for access"), says who decides (owner key fingerprint), what they see (your key and the note, **not your IP**: the directory only receives the signed request, which is true because the request goes to the directory, not the entry), and the 140-char counter.
* After sending, show the **time** and "You can close this tab". The pending state is recoverable: `my-grants` already returns `pending`, so a reload shows the same screen.
* **Nudge**: "Copy a message for the owner" puts text and a link in the clipboard: `I asked for access to <name>. My key is <fp>. Open <link> to answer.` The link is `https://mycelium.now/swarms/#grant=<swarm_id>.<requester_key>` (fragment: never leaves the browser). Opening it in the owner's browser (the one holding the owner key) opens the grant dialog with the key prefilled and its fingerprint shown to compare out of band. This closes the "no notification" gap honestly for the real use case (friends, a group) without email or push.
* **Invite-only** gets the same link, so the dead end in 1.3-C6 is gone with **no protocol change**: `_grant` does not require a prior request.
* Declined: neutral wording, and "ask again after <time>" (**proposal**: the directory stores `denied_at` and refuses a re-request for 24 h with `request_cooldown`; today re-requesting works immediately).
* Approved, but the entry is offline: show the *offline* gate ("You have access; the swarm is asleep") with Chat on an open alternative, never a disabled button without a reason.

### 5.3 Owner (proposal)

* **Badge**: while the app is open, poll `owner-inbox` for each owned swarm every 30 s (one signed query per owned swarm; limit is 90 per key per minute, five swarms max per owner, so at most 10 per minute). The count appears on the Owned tab, the mobile bar, and in `document.title` ("(2) Mycelium"). Paused when hidden.
* **Honest limit**: with no account and no email, an owner who is not on the site is not notified. The Mac app already has the inbox (`/api/swarm/inbox`); showing its count in the app's header is the second channel (V2 task), and the nudge link above is the third.
* **Row**: key fingerprint (monospace), note, age, "Will get: 24 h, 20 requests, 4,000 tokens", Approve, Decline. After either, the row shows the result with **Undo**. Undo is a **5-second hold before the signed write is sent** (nothing is committed until the hold ends); after that, withdrawing a grant needs the entry `revoke` route below. A grant cannot be "expired now": `_grant` refuses `expires_at <= now`.
* **People with access**: the grantee list from the owner's own record of grants. Today the directory deletes nothing but also has no owner-facing grant list except requests (`owner-inbox` returns requests, not grants). **Needs a small protocol addition** (section 7.4: `owner-inbox` also returns `grants` with `used` from the entry) before this table can be complete; the prototype shows it as the target.
* **Revoke** needs an entry route (`/swarm-entry/v1/revoke`, owner-signed, like `kill`) because revocation is enforced at the entry ledger (`UsageLedger.revoke`), not in the directory. The app can already do it.
* Access policy is three radio cards in plain words (By request / Open to anyone / Invite only), not a `<select>` with parenthetical hints.

### 5.4 Contributor claim (proposal)

The live projection already exposes per Mac: `label`, `node` (12 chars), `layers`, `state`, `detail`, `withdrawn` (`swarm_live._helpers`). The browser must learn *which row is mine* without any server linking a browser key to a Mac. Flow: the app opens `#claim=<swarm_id>.<node12>`; the page stores `{swarm_id, node}` in IndexedDB (never sent anywhere), and the Contributing tab marks the row whose `node` matches. If the Mac later disappears from the rows the card says "Your Mac is not in this swarm right now". Nothing is verified cryptographically (it only labels rows in the visitor's own view), so no new trust is created.

## 6. Chat experience

* **One chat surface** in `/swarms#chat/<id>`: left rail (desktop) or horizontal scroller (mobile) of chat-able swarms; right, the conversation. Header shows swarm, model, Macs, entry host. A **live bar** under it shows speed, first-word time, your latency, queue, updated in place.
* **Composer is fixed** (dock), enter sends, shift-enter newline; **Stop** replaces **Send** in the same slot while generating (the existing `entry_cancel` route).
* **Answer footer** per message: tok/s, first word, tokens, and the Macs that served it (`served_by` from `send`). All computed from data the page already receives (`tokens`, `seconds`).
* **Busy**: when the entry answers `chat_busy`, the page retries for up to 30 s and says "Waiting for your turn" in the bubble, then stops with a plain message (**proposal**; today the user retypes). Concurrency stays one prompt per swarm (`SESSIONS_POLICY`).
* **Privacy line** is always under the composer: "The swarm's Macs can read your prompt. It is not end-to-end encrypted from them." The first-prompt gate adds where the prompt goes (the entry host by name), who sees it, and that the swarm sees your key and can link your prompts (this is the R3 review's open item; the V2 lane `rel2-site` already shows a notice on the standalone page, and this reuses its wording).
* **Forget** moves to the key dialog (the key chip in the header), with the existing explanation of what forgetting does and does not reset.
* Message log is `role="log"` with `aria-live="off"`; a separate polite status announces "Answer complete" (not token by token). No `scrollIntoView`: the log scrolls itself and only auto-follows when the reader is at the bottom.

## 7. Data each view needs, and where it comes from

### 7.1 Today (no change)

| Field | Source today |
|---|---|
| name, description, model, access_mode, open_quota, owner_key, owner_fingerprint | signed `mycelium.swarm.record.v1` via `GET /swarms/api/v1/swarms` |
| status (serving / warming / offline), members, served_model, last_seen_seconds | signed `heartbeat.v1` (fields: `status, members, served_model, at`), every 30 s (`HEARTBEAT_SECONDS`), offline after 90 s (`heartbeat_ttl`) |
| entry_origin, entry_fingerprint | record `entry` |
| my grants, my pending, my denied | signed `POST /my-grants` |
| owner inbox (requests, entry offers) | signed `POST /owner-inbox` |
| Route state, helper rows (label, node, layers, state, withdrawn), path counts direct/relay/unknown, recovery counts, host sharing state, busy, killed, daily budget left | entry `POST /swarm-entry/v1/state` (`live`, `swarm_budget`, `served_by`, `limits`) |
| **Run uptime** (since the run started) | `live.route.started_at` (already in `swarm_live._route`) |
| **Your latency to the entry** | measured in the browser: time of the `state` call |
| **Your answer speed, first-word time** | measured in the browser from `events` pages (`tokens`, `seconds`) |
| Registered models | `mycelium_a13_macos.run_model.CHOICES` + `model_resources.MODEL_SHAPES` (not yet exposed over HTTP) |

### 7.2 Not available today (what the stats need)

| Stat in the design | Exists today? | Needs |
|---|---|---|
| Token speed (swarm median) | **No.** The chat service keeps `started`, `finished` and the token list per request but exports only `seconds` on the single request | entry computes a rolling median from its last N finished answers; publish it (7.3) |
| First-word time | No | entry records time of first token per request |
| Network latency between Macs | **No.** `swarm_live._transport` only counts `direct / relay / unknown` edges; no RTT is exported (**unverified** whether the observatory snapshot carries RTT; `_transport` reads none) | a measured hop RTT (A14 transport observation or the network probe) published as a median; until then show direct/relay split and the visitor's own latency |
| Uptime over 24 h | **No.** The directory stores only the latest heartbeat (`row['heartbeat']`) | directory keeps a 48-bit half-hour presence mask per swarm, derived from heartbeats it already receives |
| Queue depth | **Only busy/free.** The web entry is single-flight (`chat_busy`) | `queue_waiting` only if an entry queue ships; until then the UI shows **Free / 1 running** |
| Macs | Yes (`members`), plus rows | none |
| Model size, layers | In the registry (`MODEL_SHAPES`, `CHOICES.snapshot_bytes`) | expose in `config.models` (section 8) |

### 7.3 Protocol additions (proposal)

**Heartbeat v2.** `HEARTBEAT` has an *exact* key set (`wire.exact_keys`) and canonical JSON **forbids floats** (`wire._check_value`), so stats must be a new
document version with integers only. The directory accepts both `heartbeat.v1` and `heartbeat.v2` for one release.

```
mycelium.swarm.heartbeat.v2 = heartbeat.v1 fields +
  stats: null | {
    window_s:       int  fixed 600           // aggregation window
    answers:        int  0..100000           // completed answers in the window (count only)
    tok_s_x10:      int | null               // median decode tokens/s, x10 (no floats)
    first_token_ms: int | null               // median time to first token
    hop_ms:         int | null               // median inter-Mac hop RTT; null until measured
    direct:         int                      // Macs reached directly
    relay:          int                      // Macs reached through a relay
    busy:           bool                     // a prompt is running right now
    queue_waiting:  int | null               // null while the entry has no queue
    run_started_at: int | null               // unix s, rounded down to 60
  }
```

Size: about 250 bytes more per heartbeat; at one heartbeat per 30 s that is about 30 KB/hour per swarm, and one document stays far below `max_body = 8192`; the directory-wide heartbeat budget is 1200/min.

**Directory presence mask** (new, derived): per swarm a 48-bit integer, bit k = "at least one valid heartbeat in half-hour k of the last 24 h". Public as `uptime_24h_pct` and `up_since` (the first of the current contiguous heartbeats). No new data comes in; only an aggregate of what already arrives.

**Entry `state` additions** (additive, the page already ignores unknown keys): `stats` (same object as the heartbeat) so the live bar can refresh every 5 s without touching the directory.

**Directory additions**: `GET /swarms/api/v1/summary` (counts and four tiles, small, `Cache-Control: public, max-age=15`) for the apex; `GET /swarms/api/v1/config` gains `models`.

**Entry additions**: `POST /swarm-entry/v1/revoke` (owner-signed grant revoke); `owner-inbox` also returns the grants the owner has issued for that swarm.

### 7.4 Privacy limits on stats

* **Aggregates only.** Medians over a fixed window, never per request, never anything derived from prompt or answer text (no lengths, no token ids, no timestamps of individual answers).
* **Minimum sample.** `tok_s_x10` and `first_token_ms` are `null` until at least 3 answers are in the window (otherwise one person's prompt timing is the number).
* **Quantised.** Times rounded to 60 s, speeds to 0.1 tok/s, latency to 1 ms.
* **No per-Mac identifiers beyond what `live.helpers` already shows** (label, 12-char node). **Open question C**: labels are owner-chosen display names; the default must not be the host name.
* **No visitor data.** No IP, no key, no count of visitors, no "viewers now". The directory never learns who chats with which swarm.
* **Self-reported, labelled so.** Speed, latency and queue come from the swarm's own entry and can be wrong or inflated. The UI says "Reported by the swarm", the uptime says "Observed by the directory", the browser latency says "Measured in your browser". **No ranking by speed** (default order: status, then name) to avoid rewarding inflated numbers.
* **Stale data is never shown as live.** A swarm that stopped heartbeating shows `Offline`, all stat cells show an en dash with a tooltip, and the card says "Last seen 2 h ago" (this already matches `status` and `last_seen_seconds`).

## 8. Model dropdown (registered models only)

* **Source of truth**: `run_model.CHOICES` (name, label, model id, `snapshot_bytes`, `int8_weight_bytes`) with layers from `model_resources.MODEL_SHAPES`. Today: Qwen2.5-0.5B (1.0 GB download, 24 layers), Qwen3-0.6B (1.5 GB, 28), Qwen2.5-7B (15.2 GB, 28), Qwen3-8B (16.4 GB, 36), Qwen3-14B (29.6 GB, 40). The prototype uses exactly these.
* **Delivery** (proposal): the directory's `config` response gains `models: [{id, label, snapshot_bytes, resident_bytes, layers, note}]`, generated at startup from the registry (a test asserts equality with `CHOICES`), so the site never carries its own list.
* **Enforcement** (proposal): `docs.check_record` rejects a model id that is not in the registered set (`model_not_registered`), because a UI-only dropdown can be bypassed by a hand-signed record. Records created before the change stay readable. **Open question B**: strict enforcement ties directory releases to registry releases.
* **What the dropdown shows**: `Qwen3-8B · 16.4 GB download · 36 layers`; after choosing, a one-paragraph note ("split across your Macs by memory; the app checks fit before anything downloads"). The **fit gate stays in the Mac app** (V2 models lane: the owner picker lists the five models and gates download by the Macs' usable memory), because the site cannot know the Macs' memory before any Mac is attached. The site says so rather than duplicating a weaker check.
* The field is a native `<select>` (keyboard and screen reader behaviour for free, no free text path), `Create` is disabled until a name and a model are chosen.

## 9. Loading strategy: zero layout shift

Rules (each is checked by the CI gate below):

1. **Reserve first.** Every region that will receive async content is in the HTML with its final size and a skeleton of that size (`min-height` equal to the loaded content, not "about"). Swarm cards are 292 px tall with a fixed stat strip, detail stat tiles are 190 px, the status band has fixed rows.
2. **No late-inserted banners or sections.** The first-prompt notice, the request form, the "approved" message, the demo/privacy line are all *content of an existing slot* (`.gate` inside the conversation area, `.dock`). Transient messages are **overlays** (toast, dialog), which cannot shift layout.
3. **The key chip has a fixed width** (19 characters, tabular) and shows a skeleton until the key exists; "Creating your key..." as a paragraph is gone. Key generation never blocks first render of the list.
4. **Update in place.** Polling updates `textContent` of keyed nodes (`data-swarm-id`, `data-stat`) and never calls `replaceChildren` on a list. Inputs, focus, selection and scroll are never touched; polling for a card pauses while a field in it has focus; polling stops when the tab is hidden (`visibilitychange`) and backs off to 60 s after errors.
5. **Stale-while-revalidate.** Render the last list from `sessionStorage` instantly (public data), mark "Updated 12 s ago", then revalidate. A first visit shows skeletons.
6. **No web fonts, no layout-affecting images without dimensions**; QR codes get `width`/`height`; `scrollbar-gutter: stable`.
7. **Stat cells are `tabular-nums` and fixed-width**, so `9.9` to `10.1` tok/s does not reflow.
8. **No `scrollIntoView`.** Focus moves with `focus({preventScroll:true})`.
9. **Optional later: render the first list on the server.** The directory can inline a `<script type="application/json" id="boot">` data block (inert, so allowed by `script-src 'self'`) with the list and summary; this removes the skeleton flash but is **not needed for zero shift**, so it is last in the plan.

**Gate**: `design/site-v2-prototype/tools/capture.py` already loads every page at both widths with a layout-shift observer and fails above 0.02; the product version should fail above 0.01 on each hash route, with the network throttled and with a slow `/swarms` response (so the skeleton path is exercised).

Measured, prototype vs live:

| Page | Live today (desktop / mobile) | Prototype (desktop / mobile) |
|---|---|---|
| `/swarms/` (browse) | 0.401 / 0.175 | 0.000 / 0.000 |
| standalone demo chat | 0.004 / 0.026 | n/a (chat screens 0.000 / 0.000) |
| detail, owner, create, apex (dark and light) | n/a | 0.000 to 0.0003 |

(The 0.0003 is sub-pixel rounding of a two-column grid on the detail and owner pages. An earlier 0.0087 reading on the mobile Owned tab
was my own capture script restyling the fixed tab bar after load; the script now reads the shift first. The request, approve, chat
flow was run in one page session and produced 0.000.)

## 10. Accessibility

* Landmarks (`header`, `nav` with labels, `main`, `footer`), a skip link, one `h1` per page, visible focus ring (3 px, 2 px offset) on every control.
* Tabs follow the ARIA pattern: `role=tablist/tab/tabpanel`, roving `tabindex`, Left/Right arrows (tested in `tools/behaviour.py`); on mobile the same destinations are a bottom bar of links (`aria-current="page"`).
* Targets are at least 44 px on touch (buttons, inputs, tab bar); small chips are 36 px and sit 10 px apart.
* Status is **never colour alone**: every pill has a text label and a distinct shape (filled dot, half dot, ring).
* Contrast measured (WCAG relative luminance): dark theme ink/card 14.2, muted/card 7.5, accent/card 9.1, button ink/accent 10.3, warning 9.6, danger 5.5, danger button 6.5 (after fixing a 3.0 failure I found with this check); light theme ink 16.8, muted 6.9, accent 5.4, warning 6.3, danger 5.9, danger button 7.0. All at least 4.5:1.
* The conversation is `role="log"` with `aria-live="off"`; state changes ("Request sent", "Approved", "Answer complete") go through one polite status element; streaming text is not announced token by token.
* `prefers-reduced-motion` disables the skeleton shimmer, the caret blink and the toast animation; the theme follows `prefers-color-scheme`.
* Dialog (key) uses the native `<dialog>` (focus trap, Esc closes). The destructive "Forget this key" needs a second explicit confirmation and states what it does not reset.
* **Not verified**: a screen-reader pass (VoiceOver) and a real-user walkthrough. Both are in the plan as release gates; this document only claims what the Playwright checks and contrast arithmetic cover.

## 11. Mobile

* Bottom tab bar (Browse, Chat, Helping, Owned) with the owner badge; the top bar keeps only the brand and the key chip (`2F00…2E5F`).
* Cards go single column; the four-stat strip becomes a 2x2 grid; the Macs table becomes stacked rows (the layer bar is dropped under 640 px).
* Chat: the swarm rail becomes a horizontally scrolling strip; the composer sits above the bottom bar; the log is the only scrolling region; the first-prompt gate scrolls inside the log so the composer and its reason stay visible.
* Prototype screenshots at 390x844 for every page are in `design/site-v2-prototype/screenshots/*-mobile.png`.

## 12. Apex: explainer plus a light global status

Sections (all static text; the status band is the only data):

1. **Hero**: "Run an AI model across a few Macs you trust." + two buttons (Browse swarms, Start a swarm). Badge "Preview, Apple silicon Macs".
2. **Right now** (one card): `5 of 7 swarms serving`, `15 Macs helping`, `3 models in use`, four tiles (name, model, access, status pill), "Updated 12 s ago". Source: `GET /swarms/api/v1/summary` (proposal) or the existing list; fixed-height card, so a slow or failed fetch changes text, not layout. On failure: "Status unavailable (the directory may be asleep). Swarms still work if you have their link."
3. **How it works**: three steps and one SVG (browser, entry Mac, helper Macs; the directory shown dashed as "phone book: lists swarms, never sees prompts").
4. **Who can see what**: four short blocks (the directory, the swarm's Macs, Cloudflare, you). Same facts as `docs/user/PRIVACY.md` and the directory footer, stated once.
5. **Ways in**: Chat with a swarm / Lend a Mac / Start a swarm.
6. **Questions**: three disclosure items (is it ready; what does "serving" mean; why a list of models).

Honesty constraints from `docs/user/WHAT-MYCELIUM-DOES.md` apply: it is a preview, proven on two project Macs; the demo runs on one volunteer Mac behind a tunnel and may sleep; the helper app is not published; macOS Apple silicon only. The prototype copy is draft and marked as such by the prototype bar; no claim on the apex goes beyond those documents.

**Profile A**: the apex serves static files only and one cacheable summary GET. It carries no prompt, no inference route, no chat endpoint. `tests/demo/test_profile_a_site_closed.py` stays; its two pins that mention a fixed four-asset set and the root redirect are **changed on purpose** in the plan (RED first, with the new asset list named explicitly) rather than loosened.

## 13. Where the demo entry fits

* `demo.mycelium.now` remains the Cloudflare named tunnel to the owner Mac's relay. It stays the `entry_origin` in the demo swarm's signed record.
* `mycelium.now/swarms/#chat/<demo id>` chats with it through `https://demo.mycelium.now/swarm-entry/v1/*` from the browser. CORS and the CSP `connect-src 'self' https:` already allow it.
* The standalone `demo.mycelium.now/chat` gets (a) the same tokens and look as the app (shared CSS), (b) a quiet link "All swarms" to `https://mycelium.now/swarms/`, (c) no change to its limits. `demo.mycelium.now/` (404 today) can answer `302 /chat`; `HEAD` support fixes E4.
* **No inference through mycelium.now, ever**: add a test that the proxy and directory expose no path accepting a prompt, and a CSP test that the app page connects to `'self'` plus `https:` only (no `'unsafe-inline'`).

## 14. Visual system (from the existing tokens)

Lifted from `swarms.css`: background `#0f1412`, card `#18201d`, ink `#e8efe9`, muted `#9fb3a8`, accent `#7bd389`, warn `#f0c674`, bad `#e57373`, line `#2a3631`, system font stack. New: a light theme with the same hue family (accent `#1d7a38`), type scale (hero 3.6 rem, h1 1.9 rem, body 15 px / 16 px mobile), 12 px card radius, one accent, no gradients, no icons except the status shapes. One shared stylesheet for apex, app and (later) the standalone chat.

## 15. Prototype (what exists, what it is not)

`design/site-v2-prototype/`

| File | What |
|---|---|
| `index.html` | apex: hero, Right now band, How it works + diagram, Who can see what, Ways in, Questions |
| `swarms.html` | app with Browse / Chat / Contributing / Owned tabs (`#browse` ...), filters, search, skeletons |
| `swarm.html?id=reading|demo|lab|weekend|studio|garage|family` | detail with six stat tiles, Mac table, access, who runs it; warming and offline variants |
| `chat.html?id=...&state=...` | chat screen in every access state, with a state/swarm switcher in the prototype bar |
| `owner.html` | owner view: request inbox with Approve/Decline/Undo, default policy, people with access, setup, access policy, emergency stop |
| `create.html` | create flow with the model dropdown and the post-create steps |
| `mock-data.js` | **all data is mock**; only the "Mycelium demo" swarm mirrors a real listing; every speed, latency, uptime and queue figure is invented |
| `tools/capture.py`, `tools/behaviour.py` | screenshots at both widths + layout-shift gate; 20 behaviour assertions |
| `screenshots/` | desktop and mobile, dark and light |

Not in the prototype: real keys, any network, the QR code, real animation timing, a screen-reader pass.

Verification run (2026-10-21): `tools/behaviour.py` 20/20 pass; `tools/capture.py` worst layout shift 0.0003 over 44 page/width/theme combinations; every screenshot was viewed, and fixes made from them include: a danger button that failed contrast, a quota field that showed for the wrong mode (`hidden` overridden by `display:grid`), a mobile chat that overflowed horizontally, two detail-page card heights that differed per swarm (a measured 0.13 shift), and a composer that was clipped by the bottom bar.

## 16. Phased build plan (V2 lane tasks)

Rules: each task is its own lane worktree from `rel2/wave1-staging`; TDD means the RED commit lands first (tests that fail for the right reason), then GREEN; tests that need a browser use the existing Playwright pattern in `tests/swarm_directory/test_*_browser.py`; the apex proxy pins in `tests/demo/` are edited with the new explicit asset list, not loosened.

| Phase | Task | Needs directory/protocol change? | Ships first? |
|---|---|---|---|
| **0** | **Shared CSS + shell + no-shift core.** New `site.css`; skeleton/keyed-update helper; key chip with fixed width; replace `replaceChildren` list rebuild with keyed updates; remove `scrollIntoView`; pause polling when hidden / when a field is focused; `sessionStorage` list cache. Tests (RED): layout-shift budget on `/swarms/` at two widths; the note input survives a refresh (fails today, A3); no `scrollIntoView` in the bundle. | No | **Yes**: fixes A1-A5 on the existing page with no protocol change |
| **1** | **Tabbed app shell** (`#browse #chat #contributing #owned #swarm/<id> #new`), cards with the four-stat strip using **only today's fields** (status, Macs, access, queue as Free/Busy from the entry `state`, last seen, run uptime from `route.started_at`, browser-measured latency). Dropdown of models from a **static** list generated from the registry with an equality test. | No (static asset set grows; Profile A tests updated deliberately) | **Yes** |
| **2** | **Chat in the app**: gate (first-prompt notice, ask, pending, approved, declined, expired, quota, invite, offline, killed), fixed composer, Stop, busy retry, per-answer speed footer, key dialog with Forget. Tests: each state's screenshot + a11y tree; request to approve to chat flow with CLS 0; `chat_busy` retry. | No | **Yes** |
| **3** | **Request-access v2 + owner inbox**: owner badge (poll), Approve/Decline/Undo, default policy, **grant-by-link** `#grant=` (fixes invite-only), nudge message, `request_cooldown` after decline (small directory change), `owner-inbox` returns issued grants, entry `revoke` route. TDD at the directory and entry (Python) and in the browser. | **Small**: cooldown, inbox grants, revoke route (additive routes/fields) | After 1-2 |
| **4** | **Apex explainer + summary**: static `index.html`, `GET /summary`, apex asset set and root no longer redirects (changes `redirect_root_to_swarms` default and its test). Copy review against `docs/user/*`. | **Small**: `/summary` endpoint | Parallel with 3 |
| **5** | **Stats protocol**: `heartbeat.v2` with `stats`, entry `state.stats`, directory presence mask and `uptime_24h_pct`; app-side measurement (rolling median of decode tok/s and first-token time from the chat service records; queue state); privacy tests (min sample 3, no floats, no per-request data, labels). | **Yes**: new document version, accepted alongside v1 | After 1; the UI degrades to dashes until entries send v2 |
| **6** | **Hop latency**: publish a measured inter-Mac RTT median (network probe / A14 observation). | Yes (field exists in v2 as `hop_ms`; the *measurement* is app work) | Last; UI already shows `–` |
| **7** | **Models enforcement**: `config.models`, `model_not_registered` in `check_record`, grandfathering of existing records, dropdown from `config`. | Yes (validation) | After owner answers question B |
| **8** | **Contributor claim**: app "Show on mycelium.now" button -> `#claim=`; IndexedDB claim store; Contributing tab. | App change only | After 1 |
| **0b** | **Stale-listing handling**: Browse collapses offline listings under "Show offline (2)" and shows a disambiguating owner chip; optional directory expiry after N offline days (question J). | Only if expiry is chosen | **Yes** (UI part) |
| **9** | **Standalone chat polish** on each entry: shared CSS, "All swarms" link, `/` redirect, `HEAD` support. | No | Any time |
| **10** | **Optional server-rendered first paint** (`#boot` JSON) and screen-reader / real-user walkthroughs as release gates. | No | Last |

**What can ship first** (phase 0 + 1 + 2, no protocol change): the tabbed app with zero shift, the dropdown (static list), the chat UI with all states, the contributor-less Contributing tab with its how-to. Stats then fill in as phases 5 and 6 land.

**Release interaction**: nothing here touches the V1 release. Phase 0-2 are static-asset changes to the directory page; they are deployed by the owner's existing steps, never by this lane.

## 17. Open questions for the owner

A. Orientation: ship the apex redirect removal (Profile A test pin changes) in the same wave as the explainer, or keep `/` redirecting until the app tabs are live?

B. Models: enforce "registered models only" in `check_record` (rejects typed ids even from a hand-signed record; couples directory releases to registry releases), or only in the UI?

C. Mac labels shown publicly (`Studio Mac`): ok as owner-chosen display names, with a default of `Mac 2` (never the host name)? Today `label` is whatever the app supplies.

D. Decline: add the 24 h re-request cooldown (small directory change), or leave unlimited re-asks?

E. Undo on approve: acceptable as a 5-second hold before the write (as designed), with real revocation only through the new entry `revoke` route?

F. Speed numbers are self-reported by the swarm: show them next to the "Reported by the swarm" label as designed, or hide swarm-reported speed until it can be cross-checked?

G. Cloudflare JS Detections: the bot snippet injected into every page (A6) is blocked by our CSP but still named in no copy. Turn it off in the Cloudflare zone, or add one sentence to the privacy text?

H. Notification: is "badge + nudge link + app inbox count" enough for owners, or do you want an opt-in browser notification while the tab is open (no push service, so it still needs an open tab)?

I. The helper app is not published (`HELPER_DOWNLOAD.available = False`). Keep the Contributing tab's "Lend another Mac" panel saying so, or hide the tab until the app can be downloaded?

J. Stale listings: the live directory has two offline duplicates of "Mycelium demo". Prune policy: hide offline listings older than N days behind a "Show offline" fold only, or have the directory expire listings after N days offline (owner can re-publish)? What N?

K. Name: tab label **Contributing** (as asked) vs **Helping** on the mobile bar (shorter). Prototype uses both; pick one.
