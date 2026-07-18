# Capacity-profile freshness/deprecation catalog handover

Date: 2026-07-18

## Source topology

- Repository: `/Users/evinova-self/Projects/mycelium`
- Isolated worktree: `/Users/evinova-self/Projects/mycelium-wt-capacity-catalog`
- Branch: `feature/capacity-profile-catalog`
- Exact base: `9d65a75832c34f8cb876a9f7a06459ed60373414`
- Code commit: `939f4d6de0b2a44970e6eb68d10e9689e062b869`
- Code-commit paths:
  - `mycelium_capacity_profiles/catalog.py`
  - `mycelium_capacity_profiles/init.py`
  - `tests/capacity_profiles/test_catalog.py`

The worktree was created directly from the exact base. Canonical `main` and all
existing source worktrees remained unmodified. No fetch, pull, push, PR,
installation, network access, remote-host action, credential access, runtime
activation, or physical qualification occurred.

## Delivered boundary

This tranche adds a bounded, process-local catalog for capacity-profile bytes
that already pass `parse_capacity_profile_bytes()`. The catalog is storage,
freshness, replacement, and deprecation scaffolding only.

- Every insertion first invokes the canonical byte parser and stores the verified
  profile plus its exact canonical bytes.
- An immutable `CapacityProfileCatalogPolicy` bounds both total retained entries
  and caller-selected TTL.
- The operational slot contains model digest, quantization, backend, runtime
  build, hardware class, power mode, context bucket, and KV mode. Source-evidence
  digest distinguishes revisions but is excluded from slot identity.
- All time is caller-injected. Boolean, non-numeric, non-finite, negative, and
  backward monotonic values fail closed; no wall clock is read.
- Exact profile-digest replay is idempotent and preserves the original insertion
  and expiry times, including for stale or deprecated history.
- Replacement requires `allow_replacement is True`, exact current-digest
  compare-and-swap, and a different verified source-evidence digest.
- Successful replacement retains the prior entry as deprecated audit history and
  records its deprecation time and replacement digest.
- Lookups distinguish `missing`, `current`, `stale`, and `deprecated`. Stale and
  deprecated entries never resolve as current.
- Capacity exhaustion rejects additions and replacements without eviction or
  partial deprecation.
- Result objects are immutable and always expose `route_ready=false`,
  `release_ready=false`, and `qualification_evaluated=false`.
- `initialize_capacity_profile_catalog()` creates an isolated in-memory instance
  from explicit policy. No filesystem, background task, router, scheduler,
  admission, gossip, STATUS, Observatory, or qualification consumer is connected.

## TDD evidence

Strict RED-GREEN sequencing was observed:

1. Tests were written before either production module existed.
2. Initial focused RED:
   `python3.14 -m pytest -q tests/capacity_profiles/test_catalog.py`
   produced `57 failed`; failures were the expected missing-catalog-module
   `ModuleNotFoundError` boundary.
3. Minimal implementation then produced focused GREEN: `57 passed in 0.06s`.
4. Adjacent capacity-profile suite immediately after implementation:
   `92 passed in 0.07s`.
5. Final code-head adjacent result remained `92 passed in 0.06s`.

The 57 focused tests cover parser-only insertion, immutable policy bounds, exact
slot identity, invalid and regressing time, TTL bounds, non-extending replay,
replacement authorization and CAS, source-evidence revision requirements,
deprecated audit history, stale/deprecated resolution, capacity exhaustion,
process-local isolation, fixed-false authority fields, and forbidden runtime/I/O
coupling.

## Independent reviews

### Specification review

Verdict: PASS. The independent reviewer mapped all twelve requested behaviors to
implementation and tests and reported no critical or important gaps. One
non-blocking semantic was recorded: a valid caller time advances the catalog's
monotonic watermark even if a later TTL, authorization, CAS, or capacity check
rejects the operation. This is fail-closed and does not activate or mutate an
entry.

### Quality/security review

Verdict: APPROVED. The independent reviewer read all owned code and read-only
capacity dependencies, reran all 57 focused tests, and ran 16 additional
adversarial probes from `/tmp`; all passed. No repository file was modified by
the review.

- Critical issues: none.
- Important issues: none.
- Re-review: not required because no critical or important repair was requested.
- Non-blocking suggestions: replace the source-substring import guard with an AST
  guard if that test grows; distinguish malformed expected CAS digests from
  well-formed mismatches; optionally add explicit export-hygiene regression;
  document the fail-closed monotonic-watermark floor more prominently.

## Final observed code-head gates

All final commands below exited 0 on code commit
`939f4d6de0b2a44970e6eb68d10e9689e062b869`:

| Command | Observed result |
|---|---|
| `python3.14 -m pytest -q tests/capacity_profiles` | 92 passed in 0.06s. |
| `python3.14 -m pytest -q` | 1219 passed, 2 skipped, 117 subtests passed in 109.48s. |
| `python3.14 scripts/contract_audit.py` | 14 contracts verified. |
| `python3.14 -m compileall -q .` | Passed with no diagnostics. |
| `git diff --check` and staged variant | Passed with no whitespace errors. |
| `ruff check mycelium_capacity_profiles tests/capacity_profiles` | All checks passed. |
| `native/iroh_transport: cargo fmt --check` | Passed. |
| `native/iroh_transport: cargo clippy --all-targets --all-features -- -D warnings` | Passed. |
| `native/iroh_transport: cargo test` | 21 passed, 0 failed: 7 library, 2 capability, 9 wire-golden, and 3 security tests. |
| `ui/web: npm run check` | 98 Vitest and 3 Node contract tests passed; typecheck and production build passed. |
| `python3.14 scripts/release_security_audit.py` | 389 tracked files accepted. |
| `python3.14 scripts/claim_boundary_audit.py` | 152 source files accepted. |

The first direct UI attempt exited 127 because an isolated Git worktree does not
carry ignored `node_modules`; a PATH/NODE_PATH-only retry still could not satisfy
ESM resolution. The successful required gate exposed canonical's existing
dependencies through a temporary `node_modules` symlink, removed it on exit, and
performed no install or network access. Vite emitted only its existing
non-failing large-chunk advisory. Final repository status after code commit was
clean.

## Authority and production gaps

This catalog does not establish qualification, routing authority, release
readiness, or trusted publication. `current` means only the latest retained,
non-expired revision for one operational slot; it does not mean safe to admit or
route.

Still absent:

1. trusted publisher identity and signed replacement authorization;
2. immutable acceptance binding from source-evidence digest to qualifier-owned
   physical evidence;
3. accepted physical capacity measurements for each exact model/runtime/hardware
   slot;
4. crash-durable or distributed catalog state and authenticated replication;
5. router, scheduler, admission, gossip, STATUS, or Observatory consumption;
6. independently bounded admission policy and route-selection enforcement;
7. physical two-host qualification under load, OOM, thermal pressure,
   disconnect, stale-gossip, and recovery conditions;
8. fresh-machine bootstrap and release proof.

No profile was activated or published. `route_ready=false`,
`release_ready=false`, and `qualification_evaluated=false` remain mandatory. No
merge to `main` and no push occurred.
