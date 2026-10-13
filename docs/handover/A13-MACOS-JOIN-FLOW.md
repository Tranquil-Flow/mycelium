# A13 macOS join flow: pairing window, standing consent, automatic reports

Status: **local two-app verified** (real owner + target `DemoApp`s, real local
TLS seed, real `SeedRelayProxy` hop, real Chromium on both apps). The public
quick tunnel is a fixture in tests. Not physical two-Mac, venue-Wi-Fi or
public-network evidence.

## User flow (relay-only)

Owner Mac:

1. **Start public relay** (before creating the swarm), then **Create authority & enroll this Mac**.
2. **Invite a Mac** opens a single-use window (5/10/15 min). The app shows a
   join link (`mycelium-join:…`, pins the owner seed key) and a short typed
   form `<tunnel-name> ABCD-EFGH-JKLM`.
3. When a Mac submits, the owner card shows its fingerprint, chip, RAM, free
   disk, MLX availability, power/thermal state and the RTT/up/down throughput
   it measured over the relay. **Approve this Mac** (one click) or **Close code**.

Target Mac:

1. Choose power/network/storage policy, tick the visibility box, paste the link
   (or type relay name + code), read the standing-consent text, choose the
   memory/time limits, tick the one agreement box, **Join with code**.
2. The app shows the fingerprint and waits. After the owner's click it collects
   the recipient-encrypted handoff by itself, joins over HTTPS through the relay,
   binds the standing consent to the membership and sends its first capability
   + link report to the owner. No file or text goes back by hand.
3. **Withdraw standing consent** at any time: automatic acceptance stops and
   running native work is stopped; membership is kept.

## Network routes (public relay → owner app, in-process)

`SeedRelayProxy.bind_local(handler)` adds three prefixes next to `/seed/` and
`/managed-control/`; each request is bounded (1 MiB, no cookies/auth/TE):

| Route | Purpose |
|---|---|
| `POST /pairing-window/v1/<id>/request` | `{protocol, code, request, report, quota}`: target-signed pairing request + signed join report |
| `POST /pairing-window/v1/<id>/poll` | `{protocol, code, request_digest}` → waiting or the encrypted handoff |
| `POST /link-probe/v1/<kib>` | stateless echo (≤256 KiB each way, 64 req / 10 s) used for RTT/throughput |
| `POST /member-report/v1` | signed capability + link report of a CURRENT member (generation-bound) |

Window rules: 4-char id + 8-char secret code (30-symbol alphabet), TTL 30–900 s,
≤8 open windows, single claimant (exact retry is idempotent; a second identity
gets `pairing_window_claimed`; the same identity may replace its own expired
request), 5 wrong codes lock it, owner revocation closes it even after approval,
expiry refuses every operation. Windows are in memory: an owner restart closes
them. The relay sees the code, the public request and recipient ciphertext only.

## Stable library interfaces (for lane a13-demo-ui and others)

`mycelium_a13_macos.standing_consent`:

- `check_work_preview(grant, preview, *, binding, policy, resource_violation=None) -> {"allowed": bool, "reason": str | None}`
  — (a) consent check for one `work_acceptance.preview_work` document. Allowed only
  if the grant is `granted`, bound to `binding` (`{swarm_id, node_id,
  seed_key_digest}` from `RemoteMacMember.membership_binding()`), the preview is
  for this node, `route_ready` is false, every worker limit is within the caps,
  the preview's resource policy equals the consented `policy`
  (`resource-policy.json`) and `resource_violation` is empty. Reasons:
  `standing_consent_missing|withdrawn|unbound|owner_mismatch`,
  `membership_required`, `standing_preview_invalid`,
  `standing_offer_not_for_this_member`, `standing_preview_exceeds_caps`,
  `standing_preview_policy_changed`, or the `resource_*` code.
- `auto_poll_step(app, *, now=None, report_due=False) -> {protocol, state, report, offer, steps}`
  — (b) one click-free target step. `state` is `ran` or the gate reason
  (`not_target`, `no_standing_consent`, `standing_consent_withdrawn`,
  `standing_consent_unbound`, `stopped`, `not_member`,
  `standing_consent_owner_mismatch`). When it runs: optional periodic report,
  then `app.auto_accept()` (preview → `check_work_preview` → accept, once per
  offer), then every callable in `app.auto_steps`
  (`fn(app, context) -> dict | None`, `context = {"binding", "policy"}`) — this
  is where the installation-descriptor and reconnect steps plug in without
  editing this module; a raising step is reported as `{"error": "auto_step_failed"}`.
  `DemoApp` implements the duck-typed surface (`auto_step_context`,
  `auto_report`, `auto_accept`, `auto_steps`) and calls it every 2 s under its guard.
- Grant lifecycle: `grant_standing_consent`, `bind_standing_consent`,
  `withdraw_standing_consent`, `load_standing_consent`; `DISCLOSURE_TEXT` /
  `DISCLOSURE_DIGEST` (the client must echo the digest it displayed).

`DemoApp.member_capability_reports()` (owner) returns the latest verified
report per member `{purpose, generation, observed_at, expires_at, device, link}`
for the compile/planning lane. Reports are self-reported, never qualification.

## App routes

Owner: `/api/pair/window/open {ttl_seconds}`, `/api/pair/window/list {}`,
`/api/pair/window/approve {window_id, request_digest}`,
`/api/pair/window/revoke {window_id}`. Target: `/api/join/window {consent,
standing_consent, join}`, `/api/join/window/poll {}`, `/api/join/window/cancel {}`,
`/api/consent/standing/withdraw {confirm: true}`. `/api/status` gains
`join_flow`. The manual paste routes (`/api/join/request`, `/api/pair/*`) and the
manual per-run routes are unchanged.
