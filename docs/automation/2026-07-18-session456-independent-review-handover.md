# Session 4/5/6 integration: independent-review handover

## Review target and immutable boundaries

Review only this clean integration worktree and branch:

- worktree: `/Users/evinova-self/Projects/mycelium-wt-active-integration-456`
- branch: `integration/mycelium-active-session456-20260718`
- review-repair commit: `902905030f7d36232515c8b99530c394d915fc3c`
- physical transport evidence commit: `3ca175d37a24e95f0f463c87c8b09ee39eb7cea7`
- canonical baseline: `f2bc55b62c5e3103eda584c1c68c222244bde489`

Canonical `main` and all source worktrees remain clean. No merge, push, PR,
fetch, pull, package install, credential transfer, readiness promotion, or
Observatory mutation occurred. One user-authorized, bounded remote-host probe
ran on `m4pro` and `Evis-MacBook-Pro`; its exact scope and cleanup appear below.

`route_ready=false` and `release_ready=false`. Every result below is local
structural, process-local, simulation, loopback, native-library, or bounded
two-host physical transport evidence.

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
