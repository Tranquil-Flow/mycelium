# Isolated external HTTP journeys (L3 baseline + L3b boundaries)

`journey-tests` is Rust-only migration tooling. It treats the immutable Python
package and a future Rust executable as **external processes**. Routine unit and
adapter tests do not run the Python oracle. No product source is imported.

## Scope and result boundaries

The original 20-observation corpus has fresh first-run/defaults, a real 2 GB **unjoined target**
consent, saved-2-GB restart and refusal to silently change it, owner creation with
4 GB and **no standing member grant**, real signed-request positive/tamper controls,
cookie/Origin refusals, model selection without acquisition, normal/fast persisted
restarts, private status, the private `/api/stats` **404**, and clean teardown.

This is NOT the full F14/F15/F15b combined regression. No joined 600-second member
standing grant, stage reservation, worker deadline, rollover, serving, chat,
cancellation, physical device or GUI behavior is qualified. Same-byte restart is
NOT an installer/cross-version saved-2-GB upgrade. Each excluded obligation is
machine-recorded in the run receipt. Never turn the 404 into a stats-shape PASS.

The normal join-code flow requires public HTTPS relay. Do not enable its private
constructor-only loopback seam or substitute manually edited consent. The manual
handoff's production networking also needs separately approved HTTPS. A real
unjoined target can issue a signed request without joining; its preview is tested
on the next isolated owner process while still within its real lifetime. Refusals
are unsigned HTTP errors **about invalid signed requests**, not signed responses.

## Commands

The parent owns the workspace lockfile: reconcile the supplied resolved lock hint
with sibling lanes (do not blindly overwrite the integration lock). The commands
below use `--locked` only **after** that integration. Lane verification used the
same resolution offline without `--locked`; the generated root lock delta is not
part of this owned-path commit.

Use the repository's host-priority guard **before every cargo command**; heavy
commands additionally use `nice -n 19` and `-j 2`. The following commands omit the
operational guard for readability, not permission to bypass it:

```text
cargo test --locked --offline -j 2 -p journey-tests
cargo clippy --locked --offline -j 2 -p journey-tests --all-targets -- -D warnings
cargo fmt --package journey-tests -- --check
cargo xtask lint
```

macOS `sandbox-exec` is mandatory for process/containment qualification; missing
platform support is BLOCKED, not a green skipped test. `--test-isolation` executes
real positive loopback/scratch-write and kernel-denied nonloopback/read/write
controls using disposable sentinel files. No real user data is read for controls.
The nonloopback canary is documentation-only TEST-NET-1; the test requires kernel
`PermissionDenied`, not a timeout, before any traffic can leave.

An operator supplies a **private** target manifest (never check personal paths in):

```json
{
  "implementation": "python-v0.1.4",
  "source_sha": "f4e366e98a1f705dfaf182897044500d290dbd7f",
  "executable": "/absolute/path/to/the/authorized/packaged/executable",
  "executable_sha256": "8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f"
}
```

```text
target/debug/journey-tests --target target.json --receipt new-receipt.json --golden test-support/journeys/fixtures/python-v014.json
target/debug/journey-tests compare old-receipt.json new-receipt.json
```

`--capture new-golden.json` is the explicit initial capture operation; it only
writes after assertions pass and never overwrites existing files. Do not refresh
a golden to hide a regression. `--golden` runs the identical independent assertions
and compares all ordered allowlisted observations. `implementation: "rust"` uses
the same runner/assertions with the candidate's source/executable digest; the future
CLI must expose the same headless/offline and browser-launch protocol or supply a
reviewed launcher adapter. There is no Rust product parity result yet.

## Isolation and lifecycle

* Hash the original executable **before each launch**, including restart; the
  oracle's source/executable pins are closed. No copying, installing, signing or
  modifying the certified app. Source identity is an operator assertion bound to
  the certified executable, not inferred from runtime strings.
* Every launch creates a new `$HOME/.hermes/cache/scratch/v02-l3-<pid>-<sequence>/home`,
  spelled as the resolved path (a symlinked `~/.hermes` is followed once, up front):
  the product refuses a state root that does not resolve to itself, and Seatbelt
  rules only match resolved paths, so every profile rule also names the resolved spelling.
  No installed Hermes tree is assumed: a missing `.hermes`, `cache` or `scratch` level is
  created alone with mode 0700; every level (ordinary or symlinked) must resolve to a
  directory owned by the user and not group/other-writable, and the base must not contain
  HOME, else the launch is refused, never repaired. Only the per-run directory directly
  inside the resolved base is write-granted; the base, HOME and ancestors never are.
  Environment is cleared, then allowlists HOME, TMPDIR, CFFIXED_USER_HOME, PATH and
  LANG. `--headless --offline` is always explicit. No model/path/fast-mode override.
* Seatbelt denies all network except loopback; denies filesystem writes except its
  scratch and `/dev/null`; denies reads of real Library, Applications, demo/physical
  roots, model cache and every `.mycelium*` prefix. Child processes inherit it.
  Kernel containment plus the closed action set are the isolation proof, not an
  assertion that an untraced network capture saw no packets.
* Readiness waits on the launcher's real stdout event, then consumes only its
  browser-facing `open-demo.html` one-use form with Origin `null`. It never reads
  `native-control.json`, keys, grants or database records. Cookies stay in memory.
  All actions use session cookie, exact loopback Origin and X-Mycelium-Action.
* Restart first quits and reaps the old process, then moves its opaque scratch
  Library into a **new** HOME. No keys/consents/defaults are edited. Only saved
  resource-policy JSON is decoded for persisted-choice assertions; standing-grant
  absence is checked by filename. Identity values used for signature relations
  come from public HTTP only.
* Each launched child has its own process group. `ps` records same-group descendants;
  cleanup requests public Quit, waits a named deadline, and only then kills exact
  owned PIDs if needed. Exit status, forced kill, leftovers, listener closure,
  transient control files, total scratch-file bytes and model-file absence are
  asserted. All scratch trees are removed; failure exits remain failures.
  This slice does not qualify arbitrary hostile children that deliberately escape
  their process group, nor power-loss cleanup.
* HTTP uses numeric loopback TCP, no DNS/proxies/redirects, total read deadlines,
  bounded headers/bodies, duplicate/ambiguous framing refusal and chunk decoding.
  SSE is a bounded **finite-response** decoder preserving event order, IDs, CRLF,
  comments and multiline data; it rejects incomplete frames. Live product SSE,
  incremental callbacks and reconnect/Last-Event-ID behavior remain future slices.

### Budget IDs (all deadline values from L1)

| Use | L1 BudgetId |
|---|---|
| Launch readiness, action response, graceful stop observation | `WorkerStartup` |
| Read-only HTTP/session exchange, forced-kill reap, stdout drain, listener probe | `ModelSelection` |
| Bounded teardown observation cadence and adapter stalled-response test | `OwnerPoll` |

These are borrowed foundation operational allowances, NOT calibrated journey p99s
or permission to expand a user's consent. Source values and provenance live in
`crates/budgets/src/table.rs`. No duration literal lives in this crate.

## Audited oracle paths (all at f5bfb709)

| Rule/value | Read-only source citation |
|---|---|
| HOME state root, headless/offline, launcher event/signals/exit | `mycelium_a13_macos/__main__.py:53-67,151-161,175-224` |
| Server uses loopback; browser launch form and one-use cookie | `mycelium_a13_macos/app.py:628-643,870-880,1883-1925,1936-2012` |
| Actual 4 GB HTML default, unchecked relay, 10-minute option | `mycelium_a13_macos/web/index.html:8,24` |
| Browser consent envelope, dynamic saved-choice rendering | `mycelium_a13_macos/web/app.js:297-299,465-476` |
| Consent validation, saved-policy no-change rule, no-relay create | `mycelium_a13_macos/app.py:237-254,1097-1122,1589-1621` |
| Owner status has no member standing grant; grant filename | `mycelium_a13_macos/app.py:1214-1236`; `mycelium_a13_macos/standing_consent.py:33-51` |
| Owner initial membership/status/TLS/contribution fields | `mycelium_a13_macos/authority.py:638-706` |
| Normal-mode default false, saved next-run modes | `mycelium_a13_macos/fast_mode.py:16-37,53-69` |
| Starter a9-route; qwen3-0.6b picker; saved model | `mycelium_a13_macos/run_model.py:34-43,75-113,116-131`; the Rust target's starter is qwen3-0.6b (`Target::starter_run_model`, `crates/api/contracts/divergences.json` `run_model_default`) |
| Selection opens acquisition but never starts it | `mycelium_a13_macos/app.py:649-672,855-868` |
| Idle/zero-byte new acquisition; CLOSED survives restart | `mycelium_a13_macos/acquisition.py:853-881,905-914,1524-1548` |
| Supported Mac peer class, signature validation before approval | `mycelium_a13_macos/pairing.py:42-44,200-248` |
| Relay-only join windows; constructor seam is test-only | `mycelium_a13_macos/app.py:505-513,1699-1715`; `mycelium_a13_macos/pairing.py:837-854` |
| Serving/download paths excluded | `mycelium_a13_macos/app.py:1362-1399,1476-1579` |
| Public stats POST and publisher construction/network resume | `mycelium_a13_macos/swarm_publish.py:415-424,484-529` |
| Quit response and transient-file removal | `mycelium_a13_macos/app.py:1681-1684,1815-1863` |

## Golden privacy and evidence

Golden data is captured from real responses only after assertion success. Explicit
scalar JSON-pointer allowlists omit credentials, signatures, keys, node IDs,
fingerprints, ports, paths and volatile timestamps. Necessary identity relations
are asserted in memory, not normalized away before the check. Comparator tests
mutate every retained field/status and kill always-refuse, missing/extra observation
faults. Every observation carries a registry-checked L0 obligation ID. No F-ID is
claimed closed by an onboarding-only observation.

Full private receipts retain exact command/env paths, PIDs, source/executable hashes,
sandbox digest, timings, exits, outcomes and exclusions. They remain outside the
repository; the checked-in fixture and evidence excerpts carry no operational
identity. Original failed attempts are retained, including the initial harness
mistake that treated persisted `closed` acquisition as active. Its child was killed
by exact PID, cleanup was reported FAILED (not laundered into success), and a
source-derived regression now distinguishes inert `closed` from active transfer.

`evidence/resolved-workspace.lock` is an integration hint for the parent-owned root
lockfile. All dependencies were available offline; no models/toolchains/installers
or dependency downloads are part of this lane.

## Released v0.1.4 replay (foundation-exit)

The admitted external target is now released source
`f4e366e98a1f705dfaf182897044500d290dbd7f`, executable
`8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f`.
The certified local package is read-only; no installed app or oracle source is
executed or modified. The old capture fixtures retain their original identities
and bytes. Launch identity and historical capture identity are distinct; all
behavioral assertions remain unchanged. Superseded targets still fail admission.

The foundation lane replayed both corpora: 20 baseline observations/5 launches
and 33 boundary observations/3 launches. Every process exited zero without a
forced kill, all exact PIDs/listeners disappeared and scratch was removed.
Memory pressure is checked before each launch and throughout API observations;
only level1 is admitted. Cleanup bypasses that admission check deliberately, so
an unsafe-host refusal cannot prevent exact-owned teardown.

This does **not** complete L3. Successful bound member joining still needs the
normal public HTTPS relay; loopback relay refusal and manual-handoff transport
failure remain the exact observed results. Model-backed planning/serving/chat,
active cancel/SSE, F3/F5/F14/F15/F15b, directory publication and installed upgrade
remain BLOCKED. No private state or grant was injected to bypass those gates.
See `runs/v2/v02/FOUNDATION-EXIT.md` and its external receipt directory.

## L3b: historical final-oracle slice and two-instance loopback boundaries

At the L3b capture, launch admission was pinned to the then-certified `ef9ae61d` oracle.
The historical `fixtures/python-v014.json` is **byte-unchanged**, including its
`f5bfb709` capture provenance. All 20 observations match the final oracle. The
product-source delta is only `managed_runtime.py` (owner-stop replacement-loop
handling), outside this no-worker corpus; the Git range also changes its regression
test and the A6 source manifest. The final pin does not retroactively rewrite the
old fixture or turn these observations into F16 worker evidence.

An additive, explicitly selected corpus exercises **33 observations over 3
launches**, with owner + unjoined target simultaneously alive under separate
HOMEs/state roots/listeners, then a target restart. Run it with:

```text
target/debug/journey-tests --target target.json --receipt boundary-receipt.json --golden test-support/journeys/fixtures/python-v014-boundaries.json --scenario loopback-boundaries
```

The ordinary six-argument invocation remains the unchanged baseline scenario.
Both fixtures require exact ordered equality. The boundary fixture was captured
from real packaged HTTP responses after independent assertions; ordinary
`cargo test` only reads fixtures and exercises harness/adapter controls. It does
not launch the application. No new dependencies, Python scripts or source imports.

**What is genuinely reachable:** pending join-window open/status/revoke; explicit
loopback external-relay refusal; recipient-encrypted manual handoff issuance and
byte-identical approval retry; changed-quota and tampered-request-binding refusals;
real manual-accept transport refusal; both private statuses and member stats 404;
saved-2GB refusal after failed join; absent-consent withdrawal and missing-window
poll refusals; window cancellation; no-model serving/status with null proposal;
owner/member leave guards; unjoined instant/smooth leave with **every cleanup step
checked**, and leave/storage persistence after restart. No owner standing grant is
created. Two live processes alone are not a joined two-member swarm.

**BLOCKED, not simulated:**

- Production join windows wait for public HTTPS relay. `provider=external` is
  supported, but requires a public DNS HTTPS origin and refuses loopback/LAN names
  and IP literals. It is not a local-relay switch.
- Manual pairing does issue a real recipient-encrypted handoff for the owner’s
  loopback TLS seed. Acceptance returns `409 peer_transport_unreachable`; source
  tracing shows the target uses system Web-PKI, not the owner’s private CA. The
  loopback transport/CA alternatives are constructor-only test seams, not
  product launcher options. Generic TLS environment overrides were not enabled
  or evaluated. We neither inject trust nor edit identities, grants, DNS or hosts
  files. `MYCELIUM_NETWORK_MODE=lan|local` informs the offline-model switch; the
  packaged GUI launcher exposes no `--network-mode`/join-transport option and its
  peer client construction does not select a different trust path from that mode.
- Consequently joined 600-second / 2GB **bound** consent, actual member leave,
  member-preserving owner restart (F3), and joined stats remain unexercised.
- `serving/status` has null compilation/serving before preparation. There is no
  plan-only HTTP route. Split/resource proposals require prepared model source and
  distinct-host observations; actual F14 reservations/F15/F15b deadlines need
  additional runtime work. No compiler, worker, model download or serving starts.
- Published stats are directory POSTs, not the private stats URL. Publication,
  serve/chat/product cancellation/SSE, F5 compile-stall behavior, historical
  F14/F15/F15b REDs and cross-version installer upgrade remain blocked.

The first boundary attempt retained a **harness failure**: immediately after the
failed manual acceptance, published status still said `pending`. Failed actions do
not synchronously publish. The corrected observer waits a named `ModelSelection`
budget for the **same** expected `handoff_retained` state, checking quiescence on
every poll. No assertion or expected value was relaxed; the original failed
receipt is preserved outside the repository. Smooth leave similarly waits for its
actual published completion rather than sleeping and assuming success.

Before every app spawn, the runner rechecks busy markers and certification
waiters. If a marker arrives mid-run, the next launch is BLOCKED and owned live
processes are cleaned up. The outer required shell guard still waits before every
cargo or oracle invocation. No busy marker contents are read. Invocation-level
budgets are unchanged; a selected invitation lifetime borrows `WorkerStartup`,
not a fabricated member execution grant. Final fmt uses `CARGO_BUILD_JOBS=2`
(the fmt subcommand has no `-j` option); build/test/clippy/xtask use `-j 2`.

### Additional source citations (all at ef9ae61d)

| Boundary | Source |
|---|---|
| CLI has no product join/CA override; normal constructor | `__main__.py:151-175`; `app.py:505-513`; `target_app.py:21-31` |
| Shared network-mode env is not a GUI pairing transport switch | `mycelium_platform_neutral/network_mode.py:1-29,77-90`; `mycelium_platform_neutral/offline.py:63-83`; `peer.py:544-598` |
| Pending windows and public-relay gate | `app.py:803-834,1687-1723` |
| External relay validates origin before proxy startup | `app.py:695-735,754-768`; `public_relay.py:259-274,297-326` |
| Manual approval is recipient-bound and retry-idempotent | `authority.py:877-942`; `pairing.py:265-331,356-433` |
| Local owner TLS vs ordinary remote Web-PKI | `tls.py:378-424`; `authority.py:525-527,1034-1040`; `peer.py:99-103,544-598,740-789` |
| Only join-window path grants and binds standing consent | `app.py:1725-1813`; `standing_consent.py:97-111,139-179` |
| Failed actions can leave a stale published snapshot | `app.py:1306-1326`; `published_status.py:50-76` |
| Read-only serving/status versus model-backed proposals | `app.py:1377-1399`; `chain_compile.py:138-186,251-265` |
| Leave role/confirmation/mode and cleanup-step projection | `member_leave.py:127-155,233-260,270-316` |
| Public stats publication | `swarm_publish.py:415-424,484-529` |

Paths in this table are relative to `mycelium_a13_macos/`, except where already
qualified. These are source-audit citations, not claims that every cited path ran.

## Two-host joined-member journeys (L3C)

`journey-tests twohost <validate|plan|run|stabilize>` executes **data** scenarios
(`fixtures/twohost-scenarios.json`: 16 scenarios, 10 sessions) over loopback HTTP against instances an orchestrator
(`runs/v2/v02/L3C-evidence/window-run.sh`) launched from fresh test HOMEs. A MacBook instance is reached through an
`ssh -L` forward on the **same** loopback port; this crate never connects to a non-loopback address.

* `twohost validate --scenarios F [--evidence DIR]`: static checks (closed ops/budgets, template variables defined before use,
  dependencies ordered, claims covered, every `EXECUTED:<file>#<key>` reference resolves in the probe evidence).
* `twohost plan --scenarios F --session S [--capabilities a,b] [--relay-mode external|quick]`: the ordered steps and which are NOT_RUN.
* `twohost run --scenarios F --session S --endpoints E.json --receipt R.json [--capture G | --golden G]`: executes the session.
  Result is `PASS`, `PARTIAL` (NOT_RUN steps remain), `FAIL`, `RED_AS_EXPECTED` or `UNEXPECTED_GREEN` (historical executables).
  The executable hash is verified before and after. Control steps (owner restart, v0.1.3 upgrade) call commands listed in `endpoints.control`.
* `twohost stabilize A.json B.json --out G.json`: golden = fields equal in two independent recordings; the rest is listed `unstable`.

The existing goldens (`python-v014.json`, `python-v014-boundaries.json`) and the original runner are untouched; the only change to
shared code is that `http::Client` also accepts `DELETE` (product active cancel).
