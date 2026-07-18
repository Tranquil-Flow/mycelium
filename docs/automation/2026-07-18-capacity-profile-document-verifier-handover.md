# Capacity-profile canonical document verifier handover

Date: 2026-07-18

## Source topology

- Worktree: `/Users/evinova-self/Projects/mycelium-wt-capacity-document-verifier`
- Branch: `feature/capacity-profile-document-verifier`
- Base: `54b43f037da5c55a24589f9a13a956b0575884d2`
- Code commit: `3180c6e03b25a6a93e12e6610fbb7469cb6565ef`
- No merge to `main`; no push occurred.

## Delivered boundary

`mycelium_capacity_profiles.parse_capacity_profile_bytes()` now accepts only
exact `bytes` emitted by `CapacityProfile.canonical_json_bytes()`. It:

- bounds payloads to 256 KiB;
- rejects empty, non-UTF-8, malformed, duplicate-key, non-finite, deeply
  nested, and non-canonical JSON;
- requires exact closed schemas for the profile, key, policy, points, and
  boundary objects;
- reconstructs the key, policy, and raw observations, recompiles the profile,
  and requires byte-identical compiler output;
- therefore re-derives protocol, evaluated flags, all concurrency limits,
  boundaries, evidence binding, profile digest, and readiness booleans rather
  than trusting document claims.

Compiler ingestion now consumes at most 257 iterable items before rejecting a
profile above the 256-observation limit. Contiguous concurrency validation is
linear in supplied points and no longer materializes `list(range(...))` from an
attacker-controlled terminal concurrency.

Exact `bytes` are required rather than subclasses, preventing overridden
`__len__` from bypassing the payload bound.

## TDD and review evidence

- Initial RED: `22 failed`; every new verifier case failed because the parser
  did not exist.
- Initial GREEN: `22 passed`.
- Adjacent capacity-profile suite: `35 passed`.
- Spec review: PASS.
- First quality-review attempt timed out and is uncounted.
- Second quality/security review: APPROVED, with one minor `bytes`-subclass
  size-bound bypass noted.
- Follow-up RED reproduced that bypass; exact-type fix made it GREEN.

## Final source-lane gates

All commands exited 0 unless noted:

- `python3.14 -m pytest -q`: 1162 passed, 2 skipped, 117 subtests passed.
- `python3.14 scripts/contract_audit.py`: 14 contracts.
- `python3.14 -m compileall -q .`: passed.
- `git diff --check`: passed.
- `python3.14 scripts/release_security_audit.py`: 385 tracked files.
- `python3.14 scripts/claim_boundary_audit.py`: 150 source files.
- `ruff check mycelium_capacity_profiles tests/capacity_profiles`: passed.
- `native/iroh_transport: cargo fmt --check`: passed.
- `native/iroh_transport: cargo clippy --all-targets --all-features -- -D warnings`: passed.
- `native/iroh_transport: cargo test`: 21 passed.
- `ui/web: npm run check`: 98 Vitest tests and 3 Node contract tests passed;
  typecheck and build passed. Existing Vite chunk-size advisory remains
  non-blocking. Existing canonical dependencies were exposed through a
  temporary symlink and the symlink was removed afterward; no install ran.

## Claim boundary

This tranche verifies immutable local profile documents only. It does not
select, publish, authorize, activate, advertise, or consume a profile. No
router, scheduler, admission, gossip, or status path calls the parser.

- `route_ready=false`
- `release_ready=false`
- `qualification_evaluated=false`
- evidence scope remains `bounded_local_samples`

Still absent: trusted publisher/replacement authority, freshness/deprecation
semantics, accepted physical capacity evidence, runtime admission consumption,
and physical two-host qualification.
