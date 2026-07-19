# Session 4/5/6 integration: independent-review handover

## Review target and immutable boundaries

Review only this clean integration worktree and branch:

- worktree: `/Users/evinova-self/Projects/mycelium-wt-active-integration-456`
- branch: `integration/mycelium-active-session456-20260718`
- review head before this handover: `902905030f7d36232515c8b99530c394d915fc3c`
- canonical baseline: `f2bc55b62c5e3103eda584c1c68c222244bde489`

Canonical `main` and all source worktrees remain clean. No merge, push, PR,
fetch, pull, package install, remote-host action, credential access, physical
qualification, or readiness promotion occurred. Observatory remains read-only.

`route_ready=false` and `release_ready=false`. Every result below is local
structural, process-local, simulation, loopback, or native-library evidence.

## Integrated source lanes and provenance

All source worktrees were clean when re-probed after integration.

| Lane | Source commit | Integration commit | Stable patch ID | Result |
|---|---|---|---|---|
| Qualification evidence-diff inspector | `ec67c645e4f0dcb8d1b2ff51b2684ed6b249ff13` | `0691311c20cbb0d27511673cc6b27dc54631c02e` | `d69ff34f0859339ba4c8fdf905fa2be58b6f278a` | Clean cherry-pick; source and integration patch IDs match. |
| Capacity-catalog adversarial model | `65c12811373335935770f060cedb7b6e7431e55a` | `cbd9b79ab0e6b20adffc66f74cc59a57e9c486f8` | `554c7035e0737ae7791bf650d89a5b12b207fff3` | Clean cherry-pick; source and integration patch IDs match. |
| PathCancellation adversarial lane | `120d6fffd1a1fc35308d22d6988db50f57d70d37` | `485f3927932e2b8590e7fa5d0f6d3a9f7b81b176` | `ec66f4a317bbedeab21a6c95ef753553de036b10` | Clean cherry-pick; source and integration patch IDs match. Its strict RED corpus exposed production defects repaired in `92ac90f`. |
| PathCancellation production hardening | integration review | `92ac90f34a4628d446ed6ecd6c97363be3d4d940` | n/a | Production fixes plus permanent regressions. |
| Incomplete-review recovery and independent re-review | integration review | `902905030f7d36232515c8b99530c394d915fc3c` | n/a | Recovered partial findings; three confirmed gaps repaired with three RED/GREEN regressions; own cross-file review completed. |

Focused collection at the review head:

- `tests/qualification_diff`: 26 tests;
- `tests/capacity_catalog_adversarial`: 60 tests;
- `tests/path_cancellation_adversarial`: 34 tests;
- combined focused execution: 120 passed.

The complete stacked range can be inspected without mutating state:

```text
GIT_OPTIONAL_LOCKS=0 git log --reverse --oneline 9d65a75832c34f8cb876a9f7a06459ed60373414..902905030f7d36232515c8b99530c394d915fc3c
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

All commands below ran against the integrated code and were repeated after the
handover documentation commit. The final post-commit results are recorded.

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1,653 passed, 2 skipped, 121 subtests passed in 83.48s. |
| `python3.14 -m pytest -q tests/qualification_diff tests/capacity_catalog_adversarial tests/path_cancellation_adversarial` | 0 | 120 passed. |
| Router/Iroh/request focused aggregate | 0 | 370 passed, 46 subtests passed. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 scripts/release_security_audit.py` | 0 | 472 tracked files accepted. |
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

## Remaining semantic and physical gaps

This branch is ready for independent code review and local testing, not readiness
promotion. Still absent:

- accepted two-host/two-Mac physical qualification;
- authenticated physical EndpointID, transport, stage/load, parity, negative-run,
  and qualifier-issued evidence;
- real distributed model tensor and token parity against a trusted local oracle;
- physical request-to-iroh-to-token-stream proof under loss, disconnect, and
  cancellation;
- crash-durable replay/cancellation authority and remote-delivery recovery;
- proof that each production runtime adapter implements prompt, sticky,
  attempt-aware cancellation semantics;
- recovery proof on an accepted stable physical base route;
- fresh-machine bootstrap/release proof.

No code path in this tranche may promote `route_ready` or mutate Observatory.
No merge to `main` and no push occurred.
