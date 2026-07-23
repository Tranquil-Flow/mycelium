# Open Questions and Blockers

## RESOLVED 2026-07-23: SwarmCoordinator convergence

The operator selected **EXTEND**: there is one signed, durable seed membership
plane, not a parallel browser membership system.

`mycelium_seed` owns identity, signed admission, leases, generation fencing,
durability, and activation eligibility. `mycelium_interactive.swarm` remains the
`browser_http` adapter for invitations, origin policy, stage matrices,
dispatch/results, cancellation, and status. Browser bearer tokens are transport
credentials only and are not restart-persistent. On the production interactive
runtime path, one `SeedCoordinator` is rooted at the runtime state root with a
durable signer plus one shared invite/member SQLite database. Current seed peer
class, generation, live lease, eligible lifecycle, and durable member binding
are authoritative at browser operations and at the atomic result-completion
fence.

Browser membership/evidence is admitted on the same plane as Mac membership,
but browser peers remain activation-ineligible and all interactive claims remain
`route_ready=false`.

## RESOLVED 2026-07-23: stale claim-boundary blocker

An earlier checkout recorded
`tests/claims/test_claim_boundary_audit.py::test_current_checkout_and_runbook_preserve_claim_boundary`
as blocked by this finding:

- `ui/web/src/features/deviceLab/deviceLabClient.ts:281`
- `observatory_ui_write_surface`
- HTTP method: `POST`

That observation no longer describes this checkout. The current
`scripts/claim_boundary_audit.py --repo-root "$PWD"` run passes without changing
the Device Lab or weakening the audit.

## Task 0.2 blocked by unrelated timing-test failure

Task 0.2 rewrites only `README.md`. Its stale-claim check passes, but the mandatory full-suite gate produced one failure beyond the four documented baseline failures:

- `test_router_iroh_transport.py::test_reconnect_retry_uses_original_end_to_end_deadline`
- Expected: `elapsed < 0.16`
- Observed in full suite: `0.21970466699713143`
- Observed in focused rerun: `0.23103200000332436`
- Instrumented reproduction: the configured 0.08-second fake reconnect sleep returned at about 0.093 seconds, the 0.1-second expiry timer fired at about 0.138 seconds, and the call returned at about 0.153 seconds.

No Router or iroh source/test file differs from the `ec76856` starting commit, and the README-only change cannot cause this timing behavior. This appears to be scheduler/timer jitter on the current host, but the authoritative expected baseline allows only the three Phase 1 RED failures plus the claim-boundary failure. Per the stop-after-three-attempts rule, Task 0.2 was left uncommitted rather than committing against a five-failure suite.

## Phase 0 Task 0.3 blocked by unattended approval policy

The duplicate worktree is clean, and `integration/mycelium-real-demo` points to `ec768567a52e8d29c672099822f0c2de0b4d48d0`, exactly the parent of the live branch's Phase 0 commit. The diff between those two base tips is empty. However, both `git worktree remove /Users/evinova-self/Projects/mycelium-wt-real-demo` and `git branch -d integration/mycelium-real-demo` triggered the unattended cron safety approval gate. This cron cannot obtain interactive approval, so the duplicate worktree and branch remain intact for the operator to remove or for a trusted cron profile to handle.

## Data-collection script timeout

`/Users/evinova-self/.hermes/scripts/mycelium-status.sh` exceeded the cron collector's 120-second limit because line 55 runs the full pytest suite (`timeout 600 python3.14 -m pytest -q`). The measured suite took 137.78 seconds in this run, and the plan records a prior 207.46-second baseline. The collector was killed before its EXIT trap, leaving `/tmp/mycelium-overnight.lock` with dead PID `53256` and orphaned test/sidecar processes.

## Phase 2 Task 2.1 blocked by signer API mismatch

The authoritative plan requires invite APIs that accept serialized Ed25519 key
bytes and says the test keypair should come from
`mycelium_qualification.signing.generate_ed25519_signer()`. It also says to update
the fixture, not the signing library, if the exact attribute names differ.

The live signing API has no serialized private-key interface:

- `generate_ed25519_signer` requires `endpoint_id=...`.
- `Ed25519EvidenceSigner` intentionally stores `_private_key` and documents that
  the private key has no serialization-facing API.
- It exposes `sign(statement)` and `public_key_record()`, not `private_bytes` or
  `public_bytes`.
- `build_ed25519_verifier` verifies the repository's domain-separated evidence
  signature records, not arbitrary detached signatures over invite bytes.

This makes the required `mint_invite(*, signing_key: bytes, ...)` and
`verify_invite(*, verify_key: bytes, ...)` interfaces impossible to implement as
"thin wrappers" over the existing public signing API without either changing the
signing library's deliberate private-key boundary, changing the invite API to
accept signer/verifier objects, or instantiating cryptography Ed25519 primitives
directly in the invite module (which the plan forbids as hand-rolling Ed25519).

Please choose the intended boundary. Tasks 2.2 and 2.3 depend on invite tokens, so
they were deferred rather than built on a guessed credential API. Task 2.4 is
independent and can proceed.

---

## RESOLVED 2026-07-22: Invite signing boundary (was: Phase 2 Task 2.1 blocker)

Operator chose: invite APIs take signer/verifier OBJECTS, not raw key bytes.

Task 2.1 is now BUILT AND COMMITTED (`fcfc9cc`). As-built API:
- `mint_invite(*, signer: Ed25519EvidenceSigner, swarm_id, seed_url, ttl_seconds, nonce, issued_at=None) -> str`
- `verify_invite(token, *, verifier_key_records: Sequence[Mapping], now: float) -> dict`
- `InviteError(.code)`, `InviteRegistry.consume(nonce, now)`

Reuses `Ed25519EvidenceSigner.sign(payload_dict)` + `build_ed25519_verifier([public_key_record])`.
No raw-key API, no hand-rolled crypto, signing-library boundary intact.

Tasks 2.2 (node agent) and 2.3 (seed coordinator) are UNBLOCKED. The plan's
interface blocks for both were corrected to pass the signer object. Build them next.
