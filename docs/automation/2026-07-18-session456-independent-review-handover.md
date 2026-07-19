# Session 4/5/6 integration: independent-review handover

## Review target and immutable boundaries

Review only this clean integration worktree and branch:

- worktree: `/Users/evinova-self/Projects/mycelium-wt-active-integration-456`
- branch: `integration/mycelium-active-session456-20260718`
- review-repair commit: `902905030f7d36232515c8b99530c394d915fc3c`
- physical transport evidence commit: `3ca175d37a24e95f0f463c87c8b09ee39eb7cea7`
- signed process-identity repair: `e9d5ff182db655042a01a1680cc9b70fbc9fe03e`
- Fable review repair: `be1a4d8`
- canonical baseline: `f2bc55b62c5e3103eda584c1c68c222244bde489`

Canonical `main` and all source worktrees remain clean. No merge, push, PR,
fetch, pull, package install, readiness promotion, or Observatory mutation
occurred. Two user-authorized bounded physical sessions ran: the prior two-Mac
probe and the current three-device session using `m4pro`,
`Evis-MacBook-Pro`, and Pixel 8 Pro. Exact scope, evidence, and cleanup appear
below.

`route_ready=false` and `release_ready=false`. Every result below is local
structural, process-local, simulation, loopback, native-library, bounded
physical transport, or exact toy-stage evidence. No unified three-device
production route or pretrained distributed inference is claimed.

## Integrated source lanes and provenance

All source worktrees were clean when re-probed after integration.

| Lane | Source commit | Integration commit | Stable patch ID | Result |
|---|---|---|---|---|
| Qualification evidence-diff inspector | `ec67c645e4f0dcb8d1b2ff51b2684ed6b249ff13` | `0691311c20cbb0d27511673cc6b27dc54631c02e` | `d69ff34f0859339ba4c8fdf905fa2be58b6f278a` | Clean cherry-pick; source and integration patch IDs match. |
| Capacity-catalog adversarial model | `65c12811373335935770f060cedb7b6e7431e55a` | `cbd9b79ab0e6b20adffc66f74cc59a57e9c486f8` | `554c7035e0737ae7791bf650d89a5b12b207fff3` | Clean cherry-pick; source and integration patch IDs match. |
| PathCancellation adversarial lane | `120d6fffd1a1fc35308d22d6988db50f57d70d37` | `485f3927932e2b8590e7fa5d0f6d3a9f7b81b176` | `ec66f4a317bbedeab21a6c95ef753553de036b10` | Clean cherry-pick; source and integration patch IDs match. Its strict RED corpus exposed production defects repaired in `92ac90f`. |
| PathCancellation production hardening | integration review | `92ac90f34a4628d446ed6ecd6c97363be3d4d940` | n/a | Production fixes plus permanent regressions. |
| Incomplete-review recovery and independent re-review | integration review | `902905030f7d36232515c8b99530c394d915fc3c` | n/a | Recovered partial findings; three confirmed gaps repaired with three RED/GREEN regressions; own cross-file review completed. |
| Request/Iroh qualification documentation repair | integration cleanup | `dccb44c24104431dd08bba538a5f922cff285a86` | n/a | Reconciled stale RED/blocker language with the committed cancellation repair and current four-test pass. |
| Two-host Iroh transport probe | authorized physical spike | `3ca175d37a24e95f0f463c87c8b09ee39eb7cea7` | n/a | Three fresh physical sidecar lifecycles; 96/96 authenticated canonical frames remotely dispatched; claim boundary remains false. |

Focused collection at the review head:

- `tests/qualification_diff`: 26 tests;
- `tests/capacity_catalog_adversarial`: 60 tests;
- `tests/path_cancellation_adversarial`: 34 tests;
- combined focused execution: 120 passed.

The complete stacked range can be inspected without mutating state:

```text
GIT_OPTIONAL_LOCKS=0 git log --reverse --oneline 9d65a75832c34f8cb876a9f7a06459ed60373414..3ca175d37a24e95f0f463c87c8b09ee39eb7cea7
```

It contains request lifecycle/model conformance, request-to-iroh qualification,
capacity catalog, bootstrap preflight, qualifier authority, the three lanes
above, and their integration repairs. Earlier tranches are documented in
`docs/automation/2026-07-18-active-integration-continuation-handover.md` and
lane-specific handovers under `docs/automation/`.

## PathCancellation defects repaired

The source adversarial lane began with five strict RED cases. Integration and
several independent review rounds expanded the repair beyond those initial
cases:

1. Wire decoder and encoder reject `bool` for integer cancellation identity
   fields. Direct Relay cancellation applies the same exact-type requirement.
2. Entry request and pending-prefill state changes use per-request locks so
   cancellation cannot race an illegal terminal transition or duplicate cleanup.
3. Relay registrations carry monotonic generations. Queued work, runtime
   outcomes, cache writes, and transport dispatches are accepted only for the
   captured generation and attempt.
4. Cancellation invalidates path state under the path-state lock, then invokes
   scheduler and runtime cancellation outside that lock while a fixed striped
   per-path operation lock serializes attempt replacement. A stale release
   scoped to attempt N cannot remove a newer attempt.
5. Registration returns its monotonic generation in the same atomic operation.
   Entry stores that generation permit on the request record for local
   execution and distributed sends; cancellation between request-state
   inspection and relay dispatch rejects stale permits before runtime work starts.
6. Scheduler enqueue, immediate execution, queued batching, result caching, and
   post-runtime forwarding all revalidate path currency at the relevant
   boundary.
7. Progressive prefill creates a provisional authenticated path identity before
   runtime execution. Cancellation can invalidate provisional participants;
   post-runtime manifest, token, delta, and tensor forwarding are generation
   fenced.
8. Recent exact tombstones are bounded to 4,096 path IDs. A two-window rotating
   cancelled-attempt filter preserves fail-closed replay rejection after recent
   tombstone eviction without permanent set-only saturation. Each 1 MiB window
   rotates after 100,000 insertions. Unknown unscoped releases do not grow
   generation state.
9. Runtime work that obtained a dispatch permit before cancellation is treated
   as in flight. Cancellation invokes `runtime.cancel`; all result publication,
   caching, and later forwarding still require the original generation and are
   rejected after invalidation. The implementation cannot retract bytes already
   accepted by a transport before cancellation linearizes.

Review the probabilistic cancelled-attempt filter and dispatch-permit
linearization explicitly. Five independent digest-derived bit positions yield
an estimated two-window false-positive probability of approximately 1.30e-6
(about 1 in 770,838) at configured capacity. False positives fail closed by
rejecting a path attempt. Cancelled identities age out after two rotations, so
this remains bounded in-memory process-local replay resistance rather than
crash-durable replay authority.

## Observed gates

All commands below ran against the integrated code with the physical-evidence
document staged. The final pre-handover-commit results are recorded.

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1,653 passed, 2 skipped, 121 subtests passed in 87.90s. |
| `python3.14 -m pytest -q tests/e2e_request_iroh/test_request_iroh_e2e.py` | 0 | 4 passed. |
| `python3.14 -m pytest -q tests/qualification_diff tests/capacity_catalog_adversarial tests/path_cancellation_adversarial` | 0 | 120 passed. |
| Router/Iroh/request focused aggregate | 0 | 370 passed, 46 subtests passed. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 scripts/release_security_audit.py` | 0 | 473 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | 176 source files accepted. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `/opt/homebrew/bin/ruff check` on changed Python/tests | 0 | All checks passed. |
| `git diff --check` | 0 | No whitespace errors. |
| `cargo fmt --check` | 0 | Passed. |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | Passed. |
| `cargo test` | 0 | 21 passed: 7 library, 2 capabilities, 9 wire-golden, 3 sidecar-security. |
| `npm run check` | 0 | 98 Vitest and 3 Node tests passed; typecheck and production build passed. |

UI verification reused canonical's existing `node_modules` through a temporary
symlink and removed it on command exit. No install ran. Vite emitted only its
existing chunk-size advisory.

One independent re-review invocation timed out before returning a verdict; it is
not counted as approval. Earlier review rounds returned concrete concurrency
counterexamples. Each returned counterexample was reproduced or encoded as a
permanent deterministic regression before the gates above.

## Exact review procedure

Use read-only Git operations first:

```text
cd /Users/evinova-self/Projects/mycelium-wt-active-integration-456
GIT_OPTIONAL_LOCKS=0 git status --short --branch
GIT_OPTIONAL_LOCKS=0 git diff f2bc55b62c5e3103eda584c1c68c222244bde489..HEAD --stat
GIT_OPTIONAL_LOCKS=0 git show --stat --oneline 92ac90f34a4628d446ed6ecd6c97363be3d4d940
```

Review these concurrency boundaries first:

- `mycelium_router/entry.py`: pending-to-record migration, cancellation,
  completion, recovery, local/distributed decode permits, and exactly-once
  cleanup;
- `mycelium_router/relay.py`: provisional and locked registration, generation
  capture, attempt-scoped release, runtime-start checks, queued batches,
  progressive prefill, cache writes, transport sends, bounded metadata, and
  cancellation source authentication;
- `mycelium_router/wire.py`: exact scalar identity validation before payload
  decoding and on outbound encoding;
- `tests/path_cancellation_adversarial/`: every deterministic race barrier and
  bounded cleanup assertion.

Then run:

```text
python3.14 -m pytest -q tests/path_cancellation_adversarial
python3.14 -m pytest -q tests/qualification_diff tests/capacity_catalog_adversarial
python3.14 -m pytest -q
python3.14 scripts/contract_audit.py
python3.14 scripts/release_security_audit.py
python3.14 scripts/claim_boundary_audit.py
python3.14 -m compileall -q .
git diff --check
(cd native/iroh_transport && cargo fmt --check)
(cd native/iroh_transport && cargo clippy --all-targets --all-features -- -D warnings)
(cd native/iroh_transport && cargo test)
```

For UI, reuse an already-existing dependency directory only; do not install:

```text
cd ui/web
ln -s /Users/evinova-self/Projects/mycelium/ui/web/node_modules node_modules
trap 'rm node_modules' EXIT
npm run check
```

Recommended new adversarial review targets:

1. cancellation during each scheduler/batch boundary, including mixed-current
   batches;
2. progressive-prefill cancellation at every provisional participant;
3. recovery attempt replacement concurrent with stale cleanup;
4. cancelled-filter saturation and intentional false-positive behavior;
5. runtime adapters whose `cancel(path_id)` is non-sticky or delayed;
6. transport calls that block or re-enter Entry/Relay callbacks.

## Bounded physical transport evidence

After the local review cleanup and gate pass, an authorized throwaway spike ran
the production Python `IrohTransport` and native sidecar on `m4pro` and the
separate `Evis-MacBook-Pro`. The exact evidence and claim boundary are in
`docs/qualification/2026-07-19-two-host-iroh-transport-probe.md`.

Observed across three fresh sidecar/EndpointID lifecycles:

- 48/48 canonical `PathCancellation` frames sent from `m4pro` were dispatched
  by the peer capture adapter before authenticated acknowledgement;
- 48/48 canonical `TokenEvent` frames sent in reverse were dispatched on
  `m4pro` before authenticated acknowledgement;
- all 12 wrong-destination or malformed-frame probes failed closed with the
  expected stable code;
- each host reported 16 sent, 16 received, 16 dispatched, zero duplicates, and
  `route_ready=false` in each run;
- no source fetch, package install, model download, remote build, credential
  transfer, or Observatory operation occurred.

This used a narrow capture adapter rather than a production Router. It proves a
two-host authenticated native-Iroh transport/dispatch boundary, not distributed
inference or end-to-end cancellation. The sidecar advertised IP addresses but
did not expose selected-path telemetry, so direct LAN selection is not claimed.
Private raw evidence and the exact spike scripts remain mode 0600 outside the
repository. All run-scoped staging and processes were removed on both hosts.

## Remaining semantic and physical gaps

This branch is ready for independent code review and bounded physical transport
inspection, not readiness promotion. Still absent:

- accepted end-to-end two-host/two-Mac inference qualification; the bounded
  physical transport probe above is not a route qualification;
- physical production-Router, stage/load, tensor/token parity, full negative-run,
  and qualifier-issued evidence;
- real distributed model tensor and token parity against a trusted local oracle;
- physical request-to-iroh-to-token-stream proof under loss, disconnect, and
  cancellation;
- selected-path proof distinguishing direct LAN from any alternate Iroh path;
- crash-durable replay/cancellation authority and remote-delivery recovery;
- proof that each production runtime adapter implements prompt, sticky,
  attempt-aware cancellation semantics;
- recovery proof on an accepted stable physical base route;
- fresh-machine bootstrap/release proof.

No code path in this tranche may promote `route_ready` or mutate Observatory.
No merge to `main` and no push occurred.

## 2026-07-19 post-review MVP hardening

This follow-up began from clean integration head
`1882a4835abe4d1247cc5ac886e212072e033348` in the same worktree and branch.
Its code commits are:

- `da5b9e110fcfa4d1ce2205e486538c0d8235cc38` — fail closed on concurrent
  tracked-file mutation during the release-security audit;
- `c11a358aaaca062907f55d10b38f189f83a64667` — clear repository-wide Ruff
  diagnostics while preserving compatibility exports.

It did not merge, push, fetch, pull, install packages, access another repository,
modify a source worktree, contact a remote host, run a new physical probe, or
write through Observatory.

### Audit findings and repairs

Read-only release, concurrency, packaging, and test-discovery audits ranked three
remaining classes:

1. The tracked-working-tree security scanner checked inode identity only before
   reading and did not prove that file metadata remained stable through the read.
   A deterministic regression first failed when a same-size secret replacement
   restored the original `mtime`; the scanner reported the secret finding rather
   than `tracked_file_changed_during_read`. The final implementation compares
   device, inode, size, `mtime_ns`, and `ctime_ns` after the bounded read and now
   fails closed for that race.
2. Fresh-checkout dependency materialization remains unproven. The bounded
   bootstrap preflight correctly exited 1 in this isolated worktree because local
   `node_modules` was absent and only 399 of 414 Cargo lockfile packages were
   present in the inspected cache. No dependency was downloaded or installed.
3. Physical route semantics remain outside this local hardening phase. The prior
   two-host Iroh transport boundary evidence is preserved, but it is not full
   Router/runtime/model qualification.

The repository-wide Ruff gate initially exposed 55 existing diagnostics. Safe
cleanup removed unused imports/locals and normalized import/string formatting.
Independent diff review caught that an automatic cleanup would have removed the
public compatibility-module `__all__` attributes; explicit re-exports preserve
those APIs. Final Ruff and compatibility assertions pass.

The first independent final-diff review found the restored-`mtime` TOCTOU gap
above. Its stronger RED regression reproduced the bypass before the `ctime_ns`
repair. A second independent read-only review after repair returned no actionable
findings. Residual scanner limits remain explicit: this is not an atomic
whole-tree snapshot; untracked files, history, dependency vulnerabilities,
runtime security, authenticated-transport semantics, and physical qualification
are outside the scanner's scope.

### Deterministic stress evidence

No random-order plugin was installed. A Python 3.14 subprocess harness collected
node IDs, applied recorded deterministic shuffles, and failed on the first
non-zero child exit.

| Surface | Seeds / rounds | Observed result |
|---|---:|---:|
| PathCancellation plus concurrent lifecycle races | `45610` through `45619`, 10 rounds | 350/350 test executions passed. |
| Request, model, and capacity conformance | `45620` through `45622`, 3 rounds | 444/444 passed. |
| Two-process inference/runtime qualification | `45630` through `45632`, 3 rounds | 54/54 passed. |
| Request-to-Iroh E2E | `45640` and `45641`, 2 rounds | 8/8 passed. |
| Bootstrap, release, security, claims, and lane-audit tooling | `45650` and `45651`, 2 rounds | 356/356 passed. |
| Total | 20 deterministic rounds | 1,212/1,212 passed. |

### Final observed gates after all repairs

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1,654 passed, 2 skipped, 121 subtests passed in 82.96s. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `/opt/homebrew/bin/ruff check .` | 0 | All checks passed. |
| `git diff --check` | 0 | No whitespace errors. |
| `python3.14 scripts/release_security_audit.py --repo-root . --json` | 0 | 473/473 tracked files scanned; zero findings; readiness flags false. |
| `python3.14 scripts/claim_boundary_audit.py --repo-root . --json` | 0 | 176 source files scanned; zero findings; readiness flags false. |
| `cargo fmt --check` | 0 | Passed offline. |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | Passed offline. |
| `cargo test` | 0 | 21 passed: 7 library, 2 capability, 9 wire-golden, 3 sidecar-security. |
| `npm run check` | 0 | 98 Vitest and 3 Node tests passed; typecheck and production build passed. |

The UI gate reused canonical's already-materialized `node_modules` through a
temporary symlink, then removed it. No install ran. Vite emitted only the existing
large-chunk advisory. Rust commands used the existing local cache with network
access disabled.

### Feature-lane handover boundary

This branch is locally green and ready for feature-lane rebasing and independent
review, not readiness promotion. `route_ready=false` and `release_ready=false`
remain mandatory. Evidence is local except for the separately documented prior
bounded two-host transport probe; no new physical evidence was produced here.

Feature lanes must still prove production Router/runtime model execution, tensor
and token parity against a trusted oracle, selected physical path, loss and
disconnect behavior, end-to-end cancellation, crash-durable delivery/replay,
stable-route recovery, accepted qualifier evidence, and a genuinely fresh-machine
bootstrap. Observatory remains read-only. No merge to `main` and no push occurred.

## Subsequent Iroh lifecycle hardening

Commit `ab455ac408981c2d8a67e4b49917c045bea649b1` closes five locally
reproducible lifecycle gaps found by strict resource testing and adversarial
concurrency review:

1. Both native-sidecar test harnesses close captured child stdout/stderr streams
   after process termination. The cross-language harness also closes its
   bootstrap descriptor when process creation fails and terminates the child on
   every readiness-validation exception.
2. `SidecarClient.close()` now interrupts blocked socket I/O before waiting for
   the request lock. A deterministic socket-pair regression proves close cannot
   deadlock behind an in-flight request whose peer withholds a response.
3. `IrohTransport.send_router_frame()` rechecks the running fence after bounded
   send admission, publishes pending state atomically with that recheck, replaces
   an optimization-sensitive assertion with an explicit fail-closed error, and
   releases pending/timer/semaphore state on every admitted exit.
4. `IrohTransport.send_path_cancellation()` rechecks the running fence after
   bounded cancellation admission and starts its registered worker while the
   state lock still prevents close from observing an unstarted thread.
5. Seven permanent regressions cover normal and exceptional process cleanup,
   stream closure, blocked-request interruption, and both close/admission races.
   Each confirmed defect was observed RED before its minimal repair passed.

Post-repair evidence:

| Command or surface | Exit | Observed result |
|---|---:|---|
| Python 3.14 full suite with `-X dev`, asyncio debug, fixed hash seed, and `ResourceWarning`/`RuntimeWarning` promoted to errors | 0 | 1,661 passed, 2 optional-Zenoh skips, 121 subtests passed in 87.83s. |
| Focused Iroh/router/request/conformance/adversarial aggregate under the same strict warning policy | 0 | 151 passed. |
| Deterministic close-race lifecycle loop | 0 | 100 rounds, 300 checks; descriptors 4→4 and threads 1→1. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `/opt/homebrew/bin/ruff check .` | 0 | All checks passed. |
| `git diff --check` | 0 | No whitespace errors. |
| `python3.14 scripts/release_security_audit.py` | 0 | 473 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | 176 source files accepted. |
| Rust fmt, clippy with warnings denied, and tests | 0 | 21 tests passed. |
| `cargo audit --no-fetch --file Cargo.lock` | 0 | No vulnerability failure; one allowed unmaintained `paste 1.0.15` transitive warning in the pinned Iroh stack. |
| `npm run check` using existing dependencies only | 0 | 98 Vitest and 3 Node tests passed; typecheck and production build passed. |

The UI dependency symlink was removed after verification. No install, network,
remote-host action, new physical qualification, Observatory mutation, merge, or
push occurred. These repairs strengthen local process and concurrency semantics;
they do not promote readiness. `route_ready=false` and `release_ready=false`
remain mandatory.

## Atomic Iroh path-state publication

Commit `54e9e4068407df5764d3b387054c35d04ca2362b` closes a further
cancellation race in Iroh path registration. Previously,
`send_manifest_locked()` published the graph and participant set in separate,
unlocked operations. A concurrent cancellation could observe the graph without
participants, fail with `unknown_path`, and be lost. The deterministic regression
paused exactly between those publications and failed RED before repair.

Path graph, participant set, and entry metadata now publish together under the
transport state lock for outbound manifests, outbound progressive-prefill setup,
and inbound accepted manifests. Running-state fences are rechecked before
publication, while wire dispatch remains outside the lock. Manifest encoding is
also performed once and reused for all destinations.

Post-repair evidence:

- deterministic publication/cancellation regression: 1 passed after observed RED;
- publication race stress: 100/100 rounds, descriptors 4→4, threads 1→1;
- focused Iroh/adversarial/conformance aggregate: 64 passed;
- strict full Python 3.14 suite: 1,662 passed, 2 optional-Zenoh skips, 121
  subtests passed;
- contract audit: 14 contracts; security audit: 473 tracked files; claim audit:
  176 source files; compileall, Ruff, and `git diff --check`: passed;
- Rust fmt/clippy/tests: passed, 21 tests;
- UI check using existing dependencies only: 98 Vitest tests, 3 Node tests,
  typecheck, and production build passed; temporary dependency symlink removed.

This remains local software evidence only. No new physical test ran;
`route_ready=false`, `release_ready=false`, Observatory read-only, no merge to
`main`, and no push remain mandatory.

## 2026-07-19 signed-identity repair and live three-device session

### Confirmed qualification defect and repair

A fresh adversarial audit found that signed load-proof statements bound node,
endpoint, deployment, model, assignment, tensor, and freshness facts, but did
not bind `process_id` or `process_host_id`. An evidence bundle could therefore
relabel both process fields consistently in stage and KV evidence after the
load-proof statement was signed. The qualifier could still accept the forged
physical-process labels.

Commit `e9d5ff182db655042a01a1680cc9b70fbc9fe03e` adds both process fields to the
exact signed statement schema and reconstructs their expected values from the
corresponding stage evidence. Two permanent RED/GREEN regressions mutate host
and process identity without re-signing and now fail with
`signed_load_proof_mismatch`. Existing downstream adversarial cases explicitly
re-sign their mutated statements so duplicate/invalid process identities still
reach and exercise `process_identity_invalid` rather than being masked by the
new earlier gate. The generated contract manifest was refreshed.

This closes post-signature relabeling. It does not turn peer signatures into
hardware attestation: a peer can still sign a false host claim unless an
external trusted physical-identity authority proves it. The physical evidence
below is therefore separate operator-controlled evidence, not a qualifier
readiness promotion.

### Final local gates at the repaired code head

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1,664 passed, 2 optional-Zenoh skips, 121 subtests passed. |
| Focused qualification/request aggregate | 0 | 273 passed. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `/opt/homebrew/bin/ruff check .` | 0 | All checks passed. |
| `python3.14 scripts/release_security_audit.py` | 0 | 473 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | 176 source files accepted. |
| `git diff --check` | 0 | No whitespace errors. |
| Rust fmt, clippy with warnings denied, and tests | 0 | 21 tests passed. |
| `npm run check` using existing dependencies only | 0 | 98 Vitest and 3 Node tests passed; typecheck and production build passed. |

The UI dependency symlink was removed on command exit. No package install or
dependency download ran.

### Live physical observations

The current integration code at `e9d5ff182db655042a01a1680cc9b70fbc9fe03e`
was staged into fresh, mode-restricted throwaway roots on the two Macs. The
remote files and native sidecar were digest-verified before execution. One
fresh production-Iroh session observed:

- 16/16 canonical `PathCancellation` frames confirmed from `m4pro` to
  `Evis-MacBook-Pro`, all 16 dispatched to the peer capture adapter;
- 16/16 canonical `TokenEvent` frames confirmed in reverse, all 16 dispatched
  on `m4pro`;
- 32 unique authenticated dispatch acknowledgements and no duplicate delivery;
- four wrong-destination/malformed-frame probes rejected with the expected
  stable codes;
- `route_ready=false` and `release_ready=false` in the emitted evidence.

Pixel 8 Pro was re-probed live as Android 16/SDK 36, `aarch64`, Python 3.14.6,
through the existing authenticated argv-only bridge. The isolated mobile lab
then ran 161 tests and verified 11 pinned canonical files. Two complete
redeploy-and-execute cycles each observed:

- exact deterministic toy-transformer parity with `max_abs_error=0.0`;
- one physical Pixel stage request and 128 activation bytes round-tripped;
- a pinned canonical graph/manifest fixture sourced from clean canonical
  snapshot `f2bc55b62c5e3103eda584c1c68c222244bde489`;
- a changed runtime-instance identity and changed load-proof digest after the
  second redeploy, confirming restart invalidation in the lab fixture.

Important source boundary: the two-Mac Iroh result exercised current integration
head `e9d5ff1`; the Pixel graph fixture consumed pinned contracts from canonical
main snapshot `f2bc55b`. These are two bounded physical tests in one
three-device session, not one unified three-device production route. Pixel ran
an exact deterministic toy stage, not a pretrained assigned Mycelium stage.
Canonical RelayEngine/Router ports, live gossip publication, real capacity
reservations, selected-path telemetry, and accepted qualifier output were not
exercised.

Pixel stage listener port 9021 was closed after testing. Remote stage source,
PID, and stage-token artifacts were removed; the host stage token was removed.
All two-Mac run processes stopped, and every local/remote throwaway staging root
created by this session was removed. The long-lived authenticated bridge on
port 9020 remains available; its token bytes are absent from the evidence
archive. Observatory remained read-only.

Exact private evidence archive:

```text
/Users/evinova-self/mycelium-physical-qualification-evidence/multi-device-live-20260719T074206Z
```

- `SHA256SUMS`: 9/9 artifacts independently re-verified;
- `session-summary.json` SHA-256:
  `ea554d0d90ca42766732c6d294f5254c554b8c3c0f3ca1fdf28f06a6a9799866`;
- `SHA256SUMS` SHA-256:
  `1cd70cc6b3e1d8ec1109bdf7056bbe3635f180d40c6b62b0ea5eefa28870fc98`.

### Remaining feature-lane gaps

`route_ready=false` and `release_ready=false` remain mandatory. Next feature
lanes still need a single accepted route that combines canonical planner and
assignment provenance, real target-owned load proofs, hardware/trusted process
identity evidence, coherent live gossip/link state, production Router capacity
and runtime ports, physical tensor/token execution, pretrained oracle parity,
selected direct-versus-relay path proof, multi-token KV loopback, disconnect and
restart behavior, end-to-end cancellation, recovery, and qualifier-issued
evidence. No merge to `main` and no push occurred.

## Fable review adjudication and final repair

Commit `be1a4d8e1b357de791c5e8ecef699551e0b2a9dd` records the final code repair.
Each reported finding was independently reproduced or refuted rather than
accepted from prose alone:

1. Cancel-before-arrival was confirmed. Relay now keeps a bounded 4,096-entry
   pending-cancellation map for not-yet-registered attempts and atomically
   consumes the cancellation when the manifest arrives. Four focused
   regressions observed RED before repair and GREEN afterward.
2. Replay wording was overstated. Documentation now says the two-window rotating
   filter provides bounded process-local resistance only; cancelled identities
   can age out and no crash-durable replay authority is claimed.
3. Scattered generation fencing contained one duplicate implementation.
   `_send_token_if_path_current` was removed and all callers use the single
   `_send_if_path_current` helper. Existing send-boundary tests remain green.
4. Canonical-evidence drift was confirmed where it changed acceptance behavior.
   Qualification evidence now applies the same 48-level/100,000-node JSON bounds
   and strict path byte/component/control-character bounds as the evidence-diff
   inspector. Model-manifest serialization rejects NaN and infinity. Permanent
   excessive-nesting, unsafe-path, and non-finite-number regressions cover these
   boundaries. Protocol-specific serializers and error taxonomies were not
   collapsed where their byte/newline/ASCII contracts intentionally differ.
5. Claim-audit output redaction was confirmed missing. Secret-shaped values and
   private-key markers are now replaced by deterministic SHA-256-based redaction
   labels before findings are emitted. Its regression observed RED then GREEN.
6. Final-hop source authentication had duplicated call shape, not a reproduced
   bypass. The check now has one `_final_hop_origin_matches` predicate operating
   directly on graph, manifest, and source identity.
7. Parallel path-state containers remain an architectural review target, but no
   state-resurrection defect beyond the repaired cancellation races was
   reproduced. A broad unproven state-model rewrite was therefore not bundled
   into this repair.
8. The claimed 27 Ruff errors were refuted. Repository-wide and changed-file
   Ruff checks both pass; the cited import/E402 findings describe an older code
   state already repaired by `c11a358`.
9. Repository hygiene scan found no tracked caches, build output, temporary
   files, or large binary artifacts. Empty tracked files were package markers.
   Ignored handover/plan files remain intentional provenance evidence; no history
   rewrite, provenance deletion, or trailer rewrite was performed.

Focused repaired surfaces passed 64 tests. The 39-case PathCancellation
adversarial suite passed, then ten deterministic shuffled repetitions passed
10/10. Final code-head gates observed after the repair:

| Command or surface | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1,670 passed, 2 optional-Zenoh skips, 121 subtests passed. |
| Focused repaired surfaces | 0 | 64 passed. |
| PathCancellation deterministic stress | 0 | 10/10 shuffled repetitions passed. |
| Contract manifest check, contract audit, fixture checks | 0 | 14 contracts verified; manifest and fixtures passed. |
| `python3.14 scripts/release_security_audit.py` | 0 | 473 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | 176 source files accepted. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `/opt/homebrew/bin/ruff check .` | 0 | All checks passed. |
| `git diff --check` | 0 | No whitespace errors. |
| Rust fmt, clippy with warnings denied, and tests | 0 | 21 tests passed. |
| `npm run check` using existing dependencies only | 0 | 98 Vitest and 3 Node tests passed; typecheck and production build passed. |

The Fable verdict's claim-boundary correction is accepted: this branch is ready
for further physical and feature-lane testing, not guaranteed deployment.
Physical evidence above is bounded and does not establish one unified
production route. `route_ready=false`, `release_ready=false`, Observatory
read-only, no merge to `main`, and no push remain mandatory.
