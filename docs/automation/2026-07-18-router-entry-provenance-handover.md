# Router entry provenance handover

## Scope

- Branch: `feature/router-entry-provenance`
- Base: `1c6f6685e5ae84284d31af075877379535ae7ddb`
- Implementation commit: `39af7d3ad74248a0271d7f8b8732cd9d8316f2ac`
- Integration target: `integration/mycelium-agent-batch-20260718`
- Merge/push status: neither performed

This local-only tranche closes the route-building hop-zero provenance gap. Before the fix, an in-process or loopback sender could redirect the first progressive-prefill hop through a non-entry transport. The receiver authenticated that transport source but had no separately registered admission authority, so the forged source became the request entry.

## Change

- `EntryCoordinator.start_distributed_prefill` registers `(request_id, entry_node_id)` before topology, capacity, or request-state side effects.
- `TransportPort.remember_entry` becomes a required adapter contract.
- Fake, in-process, loopback-socket, and iroh adapters keep monotonic request-entry bindings and reject conflicting registration.
- Progressive-prefill dispatch passes the registered entry identity separately from the authenticated transport source.
- `RelayEngine` rejects hop zero with `entry_node_mismatch` when those identities differ.
- Provenance validation runs before replay-cache lookup, so cached results cannot launder a forged source.

## TDD evidence

RED, before production changes:

- In-process focused run: 2 failed. Missing `remember_entry`; forged hop zero reached route execution/manifest handling.
- Loopback focused run: 1 failed. Forged hop zero advanced the entry request to `DECODING`.

GREEN, after production changes:

- In-process focused run: 2 passed, 11 deselected.
- Loopback focused run: 1 passed, 8 deselected.
- Router-focused regression run: 239 passed, 42 subtests passed.
- Independent read-only adversarial review: no blocking finding; replay ordering, conflict handling, transport coverage, and fail-closed behavior reviewed.

## Complete gate evidence

All commands ran from this isolated worktree using existing dependencies only.

| Gate | Exit | Observed result |
| --- | ---: | --- |
| `python3.14 -m pytest -q` | 0 | 1005 passed, 2 skipped, 117 subtests passed |
| `python3.14 scripts/contract_audit.py` | 0 | 14 contracts accepted |
| `python3.14 -m compileall -q .` | 0 | passed |
| `git diff --check` | 0 | passed |
| `cargo fmt --check` | 0 | passed |
| `cargo clippy --all-targets --all-features -- -D warnings` | 0 | passed |
| `cargo test` | 0 | 21 passed, 0 failed |
| `npm run check` | 0 | 70 Vitest tests and 3 Node tests passed; typecheck and build passed |
| release-security audit | 0 | 354 tracked files accepted; no findings |
| claim-boundary audit | 0 | 138 source files accepted across 354 tracked files; no findings |

The UI gate used a temporary symlink to the canonical worktree's existing `node_modules`; no package install occurred, and the symlink was removed immediately after the gate.

## Lane integration notes

Six observed parallel lanes remain isolated:

- Observatory event adapter: uncommitted owned files only.
- Release evidence verifier: uncommitted owned files only.
- Request lifecycle conformance: branch advanced independently and has owned modifications.
- Request-to-iroh E2E: uncommitted `tests/e2e_request_iroh/` only.
- Iroh adapter state conformance: uncommitted `tests/iroh_conformance/` only.
- Physical qualification preflight: uncommitted owned files only.

The iroh state-conformance lane currently owns tests only; this tranche changes `mycelium_router/transports/iroh.py`. Integrate this implementation commit before replaying that lane's tests, then run the complete gates again.

## Remaining gaps and claim boundary

- Evidence is local and synthetic/in-process/loopback plus local iroh adapter tests only.
- No remote host, phone, credentials, network fetch, physical two-Mac qualification, or semantic model-output qualification occurred.
- External transport adapters, if any exist outside this repository, must implement the widened `remember_entry` contract.
- This does not authorize routing, release, or a readiness transition.
- Observatory remains read-only.
- `route_ready=false` and `release_ready=false`.
