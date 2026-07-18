# Mycelium active-lane integration continuation handover

## Scope and immutable boundaries

Canonical `main` remains clean at
`f2bc55b62c5e3103eda584c1c68c222244bde489`.

Continuation worktree:
`/Users/evinova-self/Projects/mycelium-wt-active-integration`

Continuation branch: `integration/mycelium-active-20260718`

Stacked base:
`5bd2dc0ea8a9f2972a71009cd9d2f65be39b433a`
(`feature/router-entry-provenance`, including the authenticated iroh integration).

Verified code head before this documentation-only handover:
`c9404015004acf7e660867bc556d398f39623043`.

No merge to `main`, push, PR, fetch, pull, package install, remote-host action,
credential access, runtime activation, or physical qualification occurred. Source
worktrees remained read-only provenance inputs. Observatory remains read-only.

## Newly integrated completed lanes

Integration order preserves the two independent lane commits and places the
verification-only tool after the read-only Observatory adapter.

| Lane | Source commit | Integration commit | Result |
|---|---|---|---|
| Read-only Observatory event adapter | `f426f6521f9a8a1354b9d10689a2125c10a769ef` | `b6146556aad9c4d5c3e820e70994588a5b1d1b6c` | Cherry-picked without conflict. Stable patch ID `5ecac65c864e5e23c604b382b63e2b11f4e89939` matches source. |
| Immutable release-evidence verifier | `04c95394897602125e9cb7577337aa35a1053519` | `2c3bc5575b2e08f784b434ff4259b81ce667f4aa` | Cherry-picked without conflict. Stable patch ID `9711b86c71d392376bf3952b16a5f875c571cd1d` matches source. |
| Integration hardening | integration review | `c9404015004acf7e660867bc556d398f39623043` | Closes verifier endpoint-address, readiness-alias, and unbounded-path bypasses with regressions. |

Both source commits have parent
`25c8b1b1e9dc8025f81789bee7d62627bd19adea`, which is an ancestor of the stacked
base. Their changed paths did not overlap the iroh or Router entry-provenance
changes between that parent and `5bd2dc0`.

## Integration review and repairs

Focused tests immediately after the clean cherry-picks passed:

- Observatory plus release verifier Python: 110 passed.
- Observatory UI projection/source: 28 passed.

An independent integration review then found three high-severity verifier gaps:

1. bracketed IPv6 and private hostname/port endpoint material could pass as an
   `endpoint_id`;
2. punctuation-normalized aliases such as `route-ready` and `release.ready`
   entered the allowlist without exact synthetic-readiness checks;
3. manifest paths lacked UTF-8 byte, component-byte, and depth bounds before
   expected-directory expansion.

Regression tests reproduced nine failures before the first repair. The repair:

- parses raw, bracketed, numeric-port, and symbolic-service host forms before
  private/loopback/link-local/reserved checks;
- requires exact canonical snake-case artifact field names and also normalizes
  readiness names in the defense-in-depth synthetic-acceptance check;
- caps bundle paths at 1,024 UTF-8 bytes, 32 components, and 255 UTF-8 bytes per
  component before inventory construction.

A follow-up adversarial review found the symbolic-service-port variant
(`localhost:http`, `db.internal:https`, `[fd00::1]:https`). New RED cases
reproduced it, and the final repair rejects those forms. Final focused result:
122 passed.

## Final observed gates on committed code head `c940401`

| Command | Exit | Observed result |
|---|---:|---|
| `python3.14 -m pytest -q` | 0 | 1127 passed, 2 skipped, 117 subtests passed. |
| `python3.14 -m pytest -q tests/release_bundle tests/observatory_events` | 0 | 122 passed. |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts verified. |
| `python3.14 -m compileall -q .` | 0 | No diagnostics. |
| `git diff --check` plus exact-base and staged variants | 0 | No whitespace errors. |
| `cargo fmt --check` | 0 | Passed. |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | Passed offline. |
| `cargo test` | 0 | 21 passed, 0 failed. |
| `npm run check` | 0 | 98 Vitest plus 3 Node tests passed; typecheck and production build passed. |
| `python3.14 scripts/release_security_audit.py` | 0 | 376 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 0 | 145 source files accepted. |

The UI gate used a temporary symlink to canonical's existing `node_modules`,
performed no install, and removed the symlink after completion. Vite emitted only
its existing large-chunk advisory.

## Remaining four active source lanes

These worktrees remain isolated and were not staged, committed, reset, or
otherwise modified by integration:

1. `test/request-lifecycle-conformance` — committed review counterexamples plus
   further tracked and untracked work in progress.
2. `test/request-iroh-e2e` — owned handover and E2E harness paths staged, not yet
   committed.
3. `test/iroh-adapter-state-conformance` — owned package and tests untracked, not
   yet committed.
4. `tool/physical-qualification-preflight` — owned package, tests, and runbook
   untracked, not yet committed.

Do not integrate any of these until its owner reports a clean committed head and
its final gates. Re-probe status, parent, changed paths, and patch overlap at that
time.

## Claim boundary and unresolved gaps

`route_ready=false` and `release_ready=false` remain mandatory. All evidence in
this continuation is local structural, simulation, or process-local evidence.
The release verifier validates supplied bytes and bindings only; it does not
accept physical evidence or perform qualification semantics. The Observatory
adapter consumes GET/SSE events only and exposes no submission, cancellation,
router mutation, qualification reconstruction, or readiness-promotion path.

Still absent:

- accepted physical two-host/two-Mac route qualification;
- authenticated physical EndpointID, transport, stage/load, parity, negative-run,
  and qualifier-issued evidence;
- physical request-to-iroh-to-token-stream proof;
- crash-durable remote delivery;
- recovery proof on a stable physical base route;
- fresh-machine release/bootstrap proof.

No merge to `main` and no push occurred.

## Capacity-profile continuation

While the four remaining lanes continued, the independent capacity-profile
compiler tranche was completed, reviewed, and integrated after this handover's
first documentation commit.

| Source commit | Integration commit | Result |
|---|---|---|
| `a614bc7b8c702ba832805951079993b99c26f56d` | `ab33b1b2bc36e50b6c0128108290ee8e3132a79d` | Compiler, contracts, STATUS adapter, and 13 tests; stable patch ID `4250c8f346f00aca5640905f4026d23907da6b51` matches. |
| `82a99964a8075e9ad5ff1d473d0df5b0a190f994` | `a927093398d1cc2993c2635d4c3bdf4b9e5ce488` | Capacity-profile handover; stable patch ID `3b29df6b15fa0207eea07521ba7bf83d74346a96` matches. |

The six capacity-tranche paths had zero overlap with the 24 paths changed on the
continuation branch after `5bd2dc0`. Both commits cherry-picked without conflict.
Exact design, review, and claim boundaries are recorded in
`docs/automation/2026-07-18-capacity-profile-compiler-handover.md`.

Final committed code head before this documentation update:
`a927093398d1cc2993c2635d4c3bdf4b9e5ce488`.

Final integrated gates on that head:

- full Python: 1140 passed, 2 skipped, 117 subtests passed;
- combined capacity/Observatory/release-verifier focus: 135 passed;
- contract audit: 14 contracts;
- compileall and all diff checks: passed;
- Rust fmt and clippy: passed; Rust tests: 21 passed;
- UI: 98 Vitest and 3 Node tests; typecheck and production build passed;
- release-security audit: 383 tracked files accepted;
- claim-boundary audit: 149 source files accepted.

The remaining four lane worktrees were re-probed after these gates. Each still
contains tracked, staged, or untracked work and therefore remains intentionally
unintegrated. `route_ready=false`; capacity profiles are bounded local evidence,
not qualification or route-promotion authority.
