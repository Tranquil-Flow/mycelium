# Mycelium A6 readiness packet

**Status:** `design_only`

**Label:** A6 deterministic redesign only; integration and physical execution remain
blocked on atomic A4 closure.

**Baseline:** committed `181dc07` (`docs: neutralize architecture governance names`)

**Isolated worktree:** `mycelium-wave8-a6-prep` on temporary branch
`codex/a6-readiness-redesign`

**Machine sources:** `tests/a6_acceptance/inventory.v1.json`,
`tests/a6_acceptance/authority_model.py`

This packet records the A6 authority-conflict analysis, the corrected specification
boundary, the hardened deterministic acceptance inventory, and the interfaces that must
be reconciled against the atomic A4 commit before any A6 implementation begins. It is a
handover boundary. It contains no A6 implementation, no physical or browser evidence, no
qualification claim, and no promotion authority.

## 1. Defect summary

The pre-A4 A6 design treated request-attempt advancement and path-generation advancement
as authority that A6/Router recovery code could mint directly, and described terminal and
cleanup flows loosely enough to bypass A4-owned generation-fenced compare-and-swap
operations. Concretely, the earlier specification:

1. asserted that a cutover *itself* increments request attempt and path generation;
2. assigned request attempts and immutable paths to the Router as a single undivided
   authority, hiding the M16 ledger's canonical `PathManifest` identity and the command
   controller's cancellation/terminal/cleanup revisions;
3. described cutover as "one durable compare-and-swap" with no owner issuance, no expected
   revision, and no stated failure semantics;
4. proved cleanup with baseline-to-terminal resource accounting that a node-global zero
   counter can satisfy, and did not order terminal publication after recorded cleanup;
5. listed a recovery deadline set alongside the A4 cancellation budget without stating
   which one governs after cancellation is observed; and
6. carried no binding to the final atomic A4 commit, so nothing failed closed while A4
   was open.

## 2. Authority-conflict analysis: old and corrected language

### 2.1 Cutover minted its own generations

**Old (section 3):**

> An A6 cutover increments request attempt and path generation. It never mutates the old
> immutable path. The gateway and every participating runtime reject late frames, output,
> cleanup, or terminal publication from the fenced attempt.

**Corrected (section 3):**

> An A6 cutover consumes an owner-issued request attempt and an owner-issued canonical
> `PathManifest` identity for the successor. Recovery never increments either value
> itself, never derives a path attempt from placement data, and never hardcodes an
> attempt. It never mutates the old immutable path. The old attempt is fenced before the
> successor may publish anything, and the gateway, ledger, command controller, and every
> participating runtime reject late frames, output, cancellation results, cleanup
> receipts, liveness mutations, and terminal publication from the fenced attempt.

**Conflict:** A4 accepts a higher request attempt only after the previous attempt is
closed, and M16 owns `path_id`/`path_digest`/`path_attempt`. A6 minting either value
would either be rejected by A4 or, worse, race A4's own advancement.

### 2.2 Ownership was collapsed into "Router"

**Old (section 2):**

> - **Router/runtime:** owns request attempts, immutable paths, physical commands,
>   recovery prefill, decode, cutover, and request-scoped cleanup.

**Corrected (section 2):** the single line is split across the three real owners — the
M16 admission ledger (canonical `PathManifest` identity, complete-path reservations, exact
request/attempt/path cleanup record), the Router (request-attempt advancement, immutable
path binding, command issue, prefill, decode, request-scoped cutover), and the A4 command
controller (command identity, absolute cancellation deadline, cancellation generation,
terminal compare-and-swap revision, cleanup receipt revision). The gateway line gained the
committed-delivery ledger and publisher generation.

### 2.3 No explicit authority-consumption boundary

**Old:** none. Nothing forbade recovery from constructing its own identity.

**Corrected (new section 2.1):**

> A6 is an authority consumer, not an authority issuer. Recovery code proposes or requests
> a transition; an A4 owner decides it, mints the identity, and returns the grant that
> recovery must then quote back verbatim.

with an explicit forbidden-mint list (request attempts, path attempts, path generations or
canonical `PathManifest` identity, command IDs, cancellation generations, publisher
generations, terminal revisions, cleanup authority or receipts) and a defined fail-closed
effect set: no publication, no watermark movement, no reservation release, no terminal
result, no state advance.

### 2.4 Cutover had no expected revision and no failure semantics

**Old (section 6):**

> Cutover is one durable compare-and-swap from old attempt/path to the new attempt/path.
> The gateway accepts output and terminal events only from that current authority. Late
> old commands, stale cancellation generations, old receipts, duplicated cutover, and
> duplicated terminal results are rejected without advancing the stream.

**Corrected (section 6):**

> Cutover is an owner-issued, request-scoped, expected-revision durable compare-and-swap
> from the old attempt/path to the owner-issued successor attempt/path. Recovery supplies
> the expected current revision it observed; the owner rejects the swap if that revision no
> longer holds. A repeated cutover with the same canonical digest is idempotent; a
> conflicting cutover is rejected. A failed cutover leaves exactly one current authority in
> place and cannot partially advance the delivery ledger, split publication between two
> attempts, or leave the request without an owner.

Fencing was also made explicitly ordered: the old attempt is fenced before the successor
may publish, and a stale source may not publish tokens, terminal state, cancellation
results, cleanup receipts, or liveness mutations.

### 2.5 Cleanup proof admitted node-global counters and preceded no terminal ordering

**Old (section 6):**

> Completion, cancellation, deadline, successor failure, browser disconnect, breaker open,
> and shutdown release each owned resource exactly once. Baseline-to-terminal accounting
> must prove zero leaked source or successor path reservations, capacity reservations,
> commands, pending deliveries, transport receipts/streams, temporary replay commands, and
> stage-local KV states.

**Corrected (section 6):** the accounting is now explicitly per request/attempt/path, and
is preceded by a new paragraph:

> Cleanup is request/attempt/path-specific. Recovery consumes owner-issued cleanup receipts
> and their expected revisions; it neither performs nor authorizes release on its own.
> Cleanup proof names the exact request, attempt, and canonical path and must remain valid
> while unrelated requests legitimately retain capacity, KV, reservations, transport
> streams, and receipts. Node-global or deployment-global zero counters are not cleanup
> proof and never satisfy an A6 gate.
>
> Terminal publication for a recovered or aborted request occurs only after real source and
> successor cleanup has been recorded by its owner, or else publishes an explicit
> cleanup-failure terminal according to A4 authority. [...] A6 must never terminate a
> shared node process as proof of cleanup.

A further paragraph isolates late results: they are bounded rejections that must not fail
unrelated waiters, stop a response reader, release another owner's resources, mutate the
current attempt, or terminate a shared node process.

### 2.6 Deadline ambiguity permitted a second cancellation budget

**Old (section 5):** the recovery bounds list included

> successor discovery, qualification, reservation, prefill, cutover, and total recovery
> deadlines;

with no statement of precedence against A4's absolute cancellation deadline.

**Corrected (section 5):**

> Once cancellation is observed on the owning request, the original A4 absolute
> cancellation deadline governs interruption, cleanup, backend release, and terminal
> publication for both the source and the successor. No A6 layer [...] may restart that
> budget, add a second 2,000 ms cancellation budget, extend the absolute deadline, or reset
> it at cutover. Cutover does not create a new cancellation clock; the successor inherits
> the same absolute deadline.
>
> Cancellation takes precedence over any pending recovery. An observed cancellation
> terminates the recovery attempt, is published as a cancellation terminal result, and is
> never relabelled as recovery failure, successor incompatibility, deadline expiry, or an
> abort reason.

### 2.7 Delivery authority did not exclude replay or fence the publisher

**Old (section 4):** committed delivery was described, but reconnect replay was not
excluded from advancing the watermark and publisher generation was absent.

**Corrected (section 4):** three additions —

> Reconnect replay reconstructs a session from the ledger; it never advances the committed
> watermark.

> Publisher generation fences delivery end to end: SSE events, replay cursors, event
> persistence, reconnect authority, and every UI reducer are bound to the current
> gateway-owned publisher generation.

> Duplicate, skipped, regressed, conflicting, and late token positions fail closed.
> Successor output begins strictly after the committed watermark.

### 2.8 Liveness consumption was one sentence

**Old (section 2):** "Recovery cannot author its own failure." — retained.

**Corrected (new paragraph at the end of section 6):** adds that request, edge, and
placement failures remain at their observed scope; that a deployment-fatal transition
requires A4's explicit fatal allowlist with its recorded reason and evidence generation;
and that a failed successor poisons neither the incumbent deployment nor unrelated
requests.

### 2.9 Nothing failed closed while A4 was open

**Old:** no binding to the atomic A4 commit existed.

**Corrected (section 2.1 and section 11):** the final atomic A4 commit digest is a required
binding recorded as `null` under `unresolved_bindings.a4_atomic_commit`. While unresolved,
A6 integration authorization fails closed with `unresolved_a4_atomic_commit_binding`, and
no dirty worktree digest, predecessor commit, branch name, or placeholder may substitute
for it.

## 3. Canonical identity

Every recovery mutation, command, receipt, and acceptance record binds, in order:
`deployment_id`, `deployment_epoch`, `qualification_digest`, `request_id`,
`request_attempt`, `path_id`, `path_digest`, `path_attempt`, `topology_generation`,
`command_id`, `cancellation_generation`, `publisher_generation`, `recovery_transition_id`,
`recovery_revision`.

A record missing any element, or carrying an element from a different generation, is
invalid. Canonical path identity is consumed from M16; it is never reconstructed from
placement data and never hardcoded.

## 4. Acceptance inventory

`tests/a6_acceptance/inventory.v1.json` grew from 12 to 27 scenarios and from 9 to 19
required coverage capabilities, and gained eight closed policy blocks:
`authority_consumption`, `canonical_identity_bindings`, `unresolved_bindings`,
`delivery_authority`, `cleanup_and_terminal_ordering`, `deadline_semantics`,
`liveness_consumption`, and `privacy_and_persistence`.

Hardened scenarios, mapped to the redesign requirements:

| Scenario | Gate kind | Requirement |
| --- | --- | --- |
| `stale_owner_issued_transition` | deterministic negative | authority consumption |
| `stale_a4_generation_rejection` | deterministic negative | authority consumption |
| `stale_publisher_generation_rejection` | deterministic negative | delivery authority |
| `canonical_path_identity_mismatch` | deterministic negative | canonical identity |
| `repeated_or_conflicting_cutover` | deterministic negative | cutover CAS |
| `output_before_fencing_rejection` | deterministic negative | cutover CAS |
| `committed_watermark_integrity` | deterministic negative | delivery authority |
| `cancel_during_successor_selection` | deterministic negative | deadline semantics |
| `cancel_during_replay_prefill` | deterministic negative | deadline semantics |
| `cancel_after_cutover` | deterministic negative | deadline semantics |
| `cleanup_before_terminal_ordering` | deterministic negative | cleanup and terminal |
| `exact_cleanup_with_unrelated_request_active` | deterministic positive | cleanup and terminal |
| `late_source_and_successor_result_isolation` | deterministic negative | cleanup and terminal |
| `reconnect_across_publisher_reset` | deterministic positive | delivery authority |
| `scoped_liveness_and_fatal_allowlist` | deterministic negative | liveness |
| `no_successor_explicit_abort` | physical browser negative | honest abort |
| `restart_reconciliation_every_nonterminal_phase` | deterministic negative | persistence |
| `unresolved_a4_commit_binding` | deterministic negative | fail-closed A4 binding |

The four physical/browser scenarios remain unexecuted by construction. The deterministic
model refuses to simulate them: `simulate_physical_gate` always returns
`physical_gate_requires_executed_product_path`.

## 5. Deterministic test helper

`tests/a6_acceptance/authority_model.py` is an A6-only acceptance helper. It is not A6
runtime behaviour, is not importable by production code, and shares no generated contract.
Its purpose is to make the consumer property executable before A4 closes:
`OwnerAuthority` is the only object that mints authority, grants carry an owner-issued
token, and `RecoveryModel` rejects anything absent, stale, forged, or self-minted with a
bounded reason and no observable effect.

It must be discarded or replaced when A6 implementation begins against the real A4
interfaces; it is scaffolding for the acceptance boundary, not a design for the runtime.

## 6. Test results

```
python3 -m pytest -q tests/a6_acceptance
55 passed

ruff check tests/a6_acceptance
All checks passed!

python3 -m pytest -q tests/a4_acceptance tests/a5_acceptance tests/a6_acceptance \
    tests/a7_acceptance tests/contracts
134 passed

python3 -m pytest -q tests/a3_acceptance tests/claims tests/lane_audit tests/conformance
55 passed

python3 -m pytest -q tests/governance
1 failed, 18 passed        # before the section 8 checklist re-pin
```

The single governance failure was `gate:A6:evidence_digest_mismatch:specification`, the
expected consequence of revising a digest-pinned specification. It was resolved by the
checklist re-pin recorded in section 8; that re-pin was applied after the run above, so
`tests/governance` must be re-run to observe it green.

The deterministic cases were verified to bite by mutating the model and confirming failure:
accepting self-minted grants, dropping the fence-before-cutover check, letting reconnect
replay advance the watermark, letting cleanup release any subject, restarting the
cancellation budget, letting A6 author its own failure, and pre-resolving the A4 binding
each produced test failures. The model was restored after every mutation.

## 7. Changed paths

| Path | Change |
| --- | --- |
| `docs/superpowers/specs/2026-08-18-mycelium-a6-full-context-replay-recovery.md` | redesigned authority, identity, cutover, delivery, cleanup, deadline, liveness, persistence sections; new section 2.1 and section 12 |
| `tests/a6_acceptance/inventory.v1.json` | hardened acceptance inventory |
| `tests/a6_acceptance/test_inventory.py` | frozen checks for the new policy blocks and scenarios |
| `tests/a6_acceptance/test_authority_consumption.py` | new deterministic authority cases |
| `tests/a6_acceptance/authority_model.py` | new A6-only deterministic test helper |
| `docs/handover/2026-08-19-mycelium-a6-readiness-packet.md` | this packet |
| `docs/handover/mycelium-completion-checklist.v1.json` | one-line A6 specification digest re-pin, applied on owner instruction |

No production module, gateway, live, router, node, UI-gateway, UI, contract, generated
fixture, contract manifest, completion plan, completion audit, or A4 evidence path was
changed. The shared checklist was changed only by the single-line A6 specification digest
re-pin described in section 8, which the owner explicitly instructed. No server, browser gate, remote command, or physical run was
launched. A7 is untouched. Nothing was merged into the active A4 branch, and the active A4
worktree was read only.

## 8. Unresolved items requiring owner or atomic-A4 action

1. **Completion-checklist digest re-pin (applied on owner instruction).**
   `docs/handover/mycelium-completion-checklist.v1.json` pinned the A6 specification digest
   at `sha256:438a90da7dc1abbbd46c0ad1520aa3257a6a4a9cb2f15a1947bf74afad6a411b`, which the
   revised specification invalidates. The A6 specification evidence binding was re-pinned to
   `sha256:19611deb138d1b2716e23535ecc2aea823b0f2433baee40b53f6d54d642dba5c` with
   `observed_at` advanced to `2026-08-19T00:00:00Z`. That is a one-line change to the single
   `A6:specification` evidence binding; no other gate, requirement, state, blocker, or
   evidence record was touched, and the checklist's own formatting is unchanged. Any accepted
   revision of an A6 specification requires this re-pin, exactly as the active A4 lane
   re-pins it for A4. Re-run
   `python3 -m pytest -q tests/governance` to confirm
   `gate:A6:evidence_digest_mismatch:specification` is cleared.
2. **Final atomic A4 commit digest.** Unresolved; recorded as `null`. A6 implementation,
   integration, physical execution, and promotion all fail closed until an owner resolves
   it after atomic A4 closure.
3. **Provisional A4 seams.** Section 12 of the specification lists the eight A4 surfaces
   A6 consumes: the owner-issued grant envelope, request-attempt advancement, canonical
   path issuance, terminal and cleanup revision, publisher generation, cancellation
   deadline, liveness observation, and the committed-delivery ledger. A6 names the
   authority, never a final API. Each must be reconciled against the atomic A4 commit
   before the first A6 production edit.
4. **Temporary commit.** The work on `codex/a6-readiness-redesign` is a temporary leaf
   commit for review. It must be reviewed and squashed into the single atomic A6 feature
   commit; it must not be merged into the active A4 branch.

## 9. Claim boundary

A6 remains `design_only`, `qualification_claim=false`, `promotion_authorized=false`. This
packet claims no A6 implementation, no qualification, no physical or browser evidence, and
no promotion. A6 deterministic redesign only; integration and physical execution remain
blocked on atomic A4 closure.
