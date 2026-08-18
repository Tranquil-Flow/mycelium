# Mycelium A6 End-to-End Full-Context Replay Recovery Specification

**Status:** approved design boundary; implementation waits for A4 completion
**Gate:** A6
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Reconciles:** `2026-08-11-mycelium-m19-scoped-recovery.md` replay slice
**Depends on:** A1 live authority; A3 qualified model operation; A4 concurrent,
interruptible request lifecycle, scoped liveness, and exact cleanup
**Architecture:** Astra sections 4.6, 4.10, 4.11, 4.13, and 4.15

## 1. Outcome and claim boundary

A6 lets an ordinary browser inference request continue on a newly qualified compatible
immutable path after its assigned path or peer fails. The successor reconstructs model
state from the exact original encoded prompt plus the exact committed generated-token
prefix. The browser receives only output positions after its committed watermark, so
cutover produces one logical stream, no duplicated or missing delivery, and one terminal
result.

The successor need not be an A5 replica. A5 is not an A6 prerequisite. Any compatible
path independently admitted, provisioned, loaded, challenged, physically qualified, and
current at cutover may be considered. Candidate intent, cached artifacts, historical
qualification, membership, or a compatible-looking graph never grants recovery authority.

A6 is `full_context_replay`, not KV transfer, KV migration, standby continuation,
speculative decoding, whole-model fallback, deployment selection, or transparent retry.
It does not claim A7. Source KV is discarded and successor KV is freshly reconstructed by
recovery prefill. If a compatible successor cannot be qualified inside the bounded replay
policy, the request aborts explicitly without a continuity claim.

This specification is dependency-ready design work only. No existing M19 fixture, sealed
document, direct Router method, qualification-only seam, modeled fault, or local replay
test can satisfy the A6 product or physical gate.

## 2. Retained authority and ownership

All earlier fail-closed boundaries remain in force:

- **Request gateway:** owns private prompt/output, the browser delivery ledger, session
  replay cursor, logical terminal result, and client-visible committed watermark.
- **Router/runtime:** owns request attempts, immutable paths, physical commands, recovery
  prefill, decode, cutover, and request-scoped cleanup.
- **A4 liveness authority:** owns the observed failure source and narrow request, edge,
  placement, peer, or deployment scope. Recovery cannot author its own failure.
- **Planner:** proposes deterministic compatible successor intent, preserves surviving
  legal tracks, applies hysteresis and circuit-break policy, and never marks a successor
  ready.
- **Provisioner/loader:** own assignment-local artifacts, atomic promotion, load proof,
  and startup challenge for the exact successor placement set.
- **Qualifier:** alone decides whether the successor path is currently recovery-ready for
  the exact model, representation, runtime, workload, route, and evidence generation.
- **Registry:** retains owner-selected deployment authority for future requests. An A6
  request-scoped cutover neither selects a model nor displaces the incumbent.
- **Observatory/UI:** project bounded privacy-reduced facts and cannot create a failure,
  watermark, successor, cutover, readiness, or terminal state.

One integrator owns gateway, dispatcher, Router, liveness, route lifecycle, and recovery
wiring. A6 does not begin shared integration until the separate atomic A4 close. A6 lands
as one atomic feature commit; no A7 implementation or claim is included in it.

## 3. Immutable recovery compatibility

A compatible replay successor binds the same:

- model identity, immutable revision, serving representation and manifest, tokenizer and
  vocabulary, special-token policy, architecture, context policy, and output semantics;
- generation parameters that can affect output, including sampling/greedy mode, seed,
  temperature, penalties, stop policy, maximum output, and decode-mode semantics;
- qualification parity policy and workload envelope; and
- owner authority, request identity, source attempt, committed watermark, and recovery
  attempt.

The successor may use a different qualified placement sequence, stage allocation, graph,
path, or compatible backend. Its own assignment, stage packs, load proofs, directed edges,
graph, path manifest, backend/runtime compatibility, cleanup proof, and qualification must
be current and internally exact. Compatibility is an explicit closed qualifier result,
not equality guessed by the Planner or UI.

An A6 cutover increments request attempt and path generation. It never mutates the old
immutable path. The gateway and every participating runtime reject late frames, output,
cleanup, or terminal publication from the fenced attempt.

## 4. Private replay checkpoint and committed delivery

The accepted request lifecycle durably retains, in an owner-private bounded store:

- canonical encoded prompt and prompt digest;
- generation settings and their digest;
- deployment/model/representation/tokenizer authority;
- session, request, attempt, immutable path, qualification, and evidence generations;
- committed generated-token count and incremental prefix digest;
- monotonically sequenced public-output event identities and delivery-ledger cursor;
- recovery attempts, successor decisions, cutovers, cleanup, and terminal authority; and
- creation, expiry, retention, and restart-reconciliation state.

The raw prompt, decoded output, token IDs, logits, tensors, activations, and KV never enter
public evidence, logs, incidents, metrics, qualification projections, or Observatory
contracts. Private prompt and token material crosses hosts only in an authenticated,
encrypted, request-scoped runtime command to an assigned qualified successor. It is size
bounded, deadline bounded, digest checked, zeroized/released at terminal cleanup, and
never persisted on a peer beyond its declared request/KV lifecycle.

A generated token is client-committed only after the gateway atomically appends its
ordered output event to the durable tab-session delivery ledger and advances the prefix
count/digest. Emission and reconnect replay use that event identity. A reconnect may replay
an already recorded event to reconstruct the session, but the UI de-duplicates by session,
request, and event sequence and never appends it twice to prompt/response history.

Generated but uncommitted work may be discarded. A replay command contains the original
encoded prompt plus exactly the committed generated-token prefix. Recovery prefill may
recompute logits and KV for that full context, but all prefix outputs are suppressed.
Only the first position strictly after the committed watermark may enter the delivery
ledger. A regressed, skipped, conflicting, or non-contiguous watermark fails before
successor model work.

The checkpoint is canonical, digest-bound, atomically replaced, fsynced, bounded by
count/bytes/age, and tied to the durable store identity. Restart never trusts an orphan
temporary file, browser-authored cursor, or output count reconstructed from text.

## 5. Recovery state machine and bounds

The closed request states are:

`active -> failure_observed -> checkpoint_frozen -> successor_pending ->`
`recovery_prefill -> cutover_pending -> recovered_decode -> cleanup -> terminal`

Any recovery state may instead enter `cleanup -> aborted`. No transition skips fencing,
current qualification, complete-path reservation, recovery prefill, or cutover authority.
Idempotent duplicates require the same canonical digest; conflicting duplicates fail the
owning request.

A6 retains the M19 baseline of at most two recovery attempts per logical request and the
30-second breaker after three successor failures within 60 seconds. Implementations must
also freeze before physical measurement:

- maximum prompt-plus-prefix tokens and bytes accepted for replay;
- successor discovery, qualification, reservation, prefill, cutover, and total recovery
  deadlines;
- maximum retained candidates and recovery-event records;
- per-deployment recovery concurrency and memory/KV reservations; and
- exact reason codes for timeout, incompatibility, capacity, breaker, and cleanup failure.

The Planner's normal candidate hysteresis remains three equivalent generations over at
least ten seconds. An observed loss of every usable incumbent track may use an emergency
candidate, but cannot bypass artifact, load, graph, compatibility, qualification,
reservation, fencing, or cleanup gates.

## 6. Cutover, publication, and exact cleanup

Before successor work, the Router freezes the latest gateway-owned checkpoint and fences
the old request attempt. It reserves one complete currently qualified successor path and
issues recovery prefill with the frozen digest. The successor proves that the reconstructed
prefix ends at the exact committed count before it may decode the next position.

Cutover is one durable compare-and-swap from old attempt/path to the new attempt/path.
The gateway accepts output and terminal events only from that current authority. Late old
commands, stale cancellation generations, old receipts, duplicated cutover, and duplicated
terminal results are rejected without advancing the stream.

Completion, cancellation, deadline, successor failure, browser disconnect, breaker open,
and shutdown release each owned resource exactly once. Baseline-to-terminal accounting
must prove zero leaked source or successor path reservations, capacity reservations,
commands, pending deliveries, transport receipts/streams, temporary replay commands, and
stage-local KV states. The retained bounded delivery/history and incident records are
intentional durable product state and are reported separately from live resources.

If cleanup cannot prove ownership, only the affected request or placement fails unless an
A4 deployment-fatal allowlist invariant is truly contradicted. Recovery code cannot widen
an unknown exception into deployment-fatal state.

## 7. Closed contracts and persistence

A6 introduces capability-named, closed, bounded, canonically encoded contracts:

1. `mycelium.private_request_replay_checkpoint.v1` — owner-private prompt/token,
   delivery-ledger, authority, attempt, expiry, and reconciliation state.
2. `mycelium.full_context_recovery_intent.v1` — Planner-owned retained paths,
   compatible candidates, hysteresis, rejection reasons, circuit-break state, and an
   explicit `route_ready=false` claim boundary.
3. `mycelium.full_context_recovery_runtime.v1` — Router/gateway-owned privacy-reduced
   attempts, committed count/digest, state transitions, successor binding, prefill,
   cutover, delivery, cleanup, and terminal facts.
4. `mycelium.full_context_recovery_qualification.v1` — qualifier-owned compatibility,
   freshness, parity, resource, route, fallback, physical challenge, and expiry result.
5. `mycelium.full_context_recovery_gate.v1` — owner-private executed positive/negative,
   browser, counter, cleanup, and regression evidence with a privacy-reduced projection.

Unknown fields, unbounded lists/strings, private public fields, non-finite values,
duplicate identities, stale generations, invalid transitions, unsupported protocols,
and digest mismatches fail closed. Cross-process records are canonically digested and
governance-pinned.

After coordinator or gateway restart, every nonterminal request reconciles to exactly one
of `resumed`, `aborted`, or `already_terminal`. The current attempt, committed watermark,
cutover, and terminal compare-and-swap survive restart. A stale process cannot reacquire
publication authority. Refresh, reconnect, Back/Forward, workspace switching, and a clean
second browser session reconstruct public recovery state; tab-private prompt/output remains
isolated to its authorized session.

## 8. Eight-workspace product behavior

Product copy uses `full-context replay recovery`, never an A/M milestone label.

- **Inference:** request attempt, committed output count, recovery phase, replay mode,
  bounded wait/progress, successor path, cutover, cancellation, one terminal result, and
  tab-private stream/history without token IDs.
- **Device Lab:** replay-command, context, runtime, cancellation, cleanup, and qualification
  requirements for a member; membership alone remains ineligible.
- **Network:** failed/superseded path ghosted separately from proposed, qualified, and
  active successor paths; observed old/new frame traces remain generation-bound.
- **Nodes:** affected placement, old/new attempt, successor readiness, reservation/work
  counters, replay-prefill work, current health, and terminal resource delta.
- **Plans:** retained legal tracks, deterministic candidates, compatibility reasons,
  hysteresis, emergency intent, bounded attempts, and circuit-break state.
- **Readiness:** independent checkpoint, compatibility, artifact, load, graph, route,
  qualification, reservation, parity, cutover, delivery, and cleanup rungs.
- **Incidents:** observed detector source, narrow scope, committed watermark count,
  candidate timeline, rejection/fallback, fencing, cutover, breaker, cleanup, and terminal
  outcome without prompt, output, or token content.
- **Settings:** future-request replay limits, deadlines, retention, breaker, privacy, and
  fallback policy. Changes cannot rewrite an active request or current qualification.

All workspaces consume the same public generation and distinguish live, degraded, stale,
historical, replay, fixture, unavailable, and unknown states. Missing values never become
zero, compatible, healthy, or recovered. Direct navigation, refresh, Back/Forward,
reconnect, responsive layout, keyboard use, reduced motion, accessible names, and a clean
second session are part of the gate.

## 9. Frozen design-only acceptance inventory

The closed manifest at `tests/a6_acceptance/inventory.v1.json` is the machine-readable
A6 acceptance inventory. It freezes authority ownership, immutable request and generation
bindings, recovery and abort states, required capability coverage, physical/browser gate
kinds, and exact scenario invariants. Unknown fields, missing scenarios, widened scope,
duplicate identities, or invented success authority fail the inventory checks.

The request gateway is the sole authority for the canonical encoded prompt, committed
generated-token prefix count and digest, delivery ledger and watermark, and logical
terminal result. Browser cursors, reconstructed text, the Router, Planner, successor, UI,
fixtures, and evidence cannot replace or advance that authority. Private prompt and prefix
material remains excluded from public acceptance records.

Cutover is request-scoped and compare-and-swaps only that request's current attempt and
immutable path. It does not change the selected deployment or any unrelated request. Both
request-attempt and path generation advance, and stale source frames, output, terminal
publication, cancellation, receipts, and cleanup are fenced without changing the current
ledger or successor authority.

Replay uses the original encoded prompt plus exactly the gateway-committed prefix. Every
reconstructed prefix position is suppressed, and only the next contiguous position may be
appended under its durable event identity. Duplicate or conflicting delivery, watermark
rollback, skip, repeat, and late old-attempt output fail closed, including across ordinary
browser reconnect and ledger replay.

A successor is eligible only through its own current, exact compatibility and physical
qualification for the bound operation. A5 membership, candidate intent, cached artifacts,
historical qualification, or similarity is insufficient. Cancellation before or after
cutover, successor failure, deadline, disconnect, and shutdown each retain one terminal
authority and prove exact source/successor cleanup.

The positive inventory requires continuation of a nonzero-prefix request submitted through
the ordinary browser and gateway path, never a direct Router call or test-only seam. The
negative inventory requires an explicit bounded abort when no current compatible qualified
successor exists: no silent prompt restart, continuity claim, duplicate output, or leaked
resource is allowed, and unrelated requests and incumbent selection remain unchanged.

This inventory is acceptance design, not executed product or physical evidence. It always
reports `gate_state=design_only`, `qualification_claim=false`, and
`promotion_authorized=false`; satisfying the test-only inventory cannot promote A6.

## 10. Verification matrix

### Contract, deterministic, and adversarial gates

- Closed-shape, canonical digest, privacy, size/count, unknown-field, duplicate/conflict,
  stale generation, incompatible identity, invalid transition, and expiry tests for every
  contract.
- Deterministic interleavings cover failure/output commit, cancel/failure, timeout/cutover,
  old-result/new-attempt, reconnect/replay, shutdown/recovery, restart/terminal, and
  projection during mutation.
- Replay tests cover zero-token and multi-token prefixes, prompt/context bounds, watermark
  rollback/skip/repeat/conflict, prefix suppression, one next-position publication,
  tokenizer/settings mismatch, backend compatibility, successor saturation, breaker,
  bounded attempts, and exact cleanup.
- Fault injection is product-path controlled, reproducible, and incapable of authoring a
  success boolean. Fixtures and historical M19 documents remain negative controls.

### Physical positive gate

1. Start with two compatible independently qualified immutable physical paths for the same
   exact model operation; the successor must remain usable after the chosen incumbent
   placement or path fails.
2. Submit a browser request through the ordinary gateway and commit a nonzero output prefix.
   Kill or disconnect an assigned participating peer/path through the controlled physical
   fault mechanism. A4 records and interrupts the actual affected command within its bound.
3. Freeze the gateway watermark, reserve and freshly validate the successor, replay the
   exact private prompt plus committed prefix through the normal Router/stage runtimes, and
   cut over to the incremented attempt.
4. Prove uninterrupted-reference parity, no duplicate or missing logical output event,
   exactly one terminal result, positive source and successor physical work/frame counters,
   and zero terminal live-resource delta on every participant.

### Physical negative gate

Repeat the browser fault with no currently compatible qualified successor. Stale,
incomplete, unqualified, saturated, mismatched, or dead candidates are rejected. The
request explicitly aborts inside the bounded policy, publishes no replay/continuity success,
does not silently restart from the prompt, retains one terminal history, and cleans all
resources. The incumbent deployment and unrelated requests remain governed by A4's narrow
scope.

### Browser and regression gates

All eight live workspaces are exercised for the positive recovery and truthful abort,
including refresh during recovery, reconnect before/after cutover, Back/Forward, workspace
switching, stale/degraded evidence, terminal history, accessibility, privacy redaction, and
a clean second session. Focused, full Python, frontend, contract, governance, claim-boundary,
privacy, security, and invalidated physical regression suites pass after evidence stabilizes.

## 11. Completion

A6 is complete only when the ordinary product path physically continues one browser
request by full-context replay, physically aborts another when no compatible successor
exists, proves reference parity and exact-once delivery, cleans every source/successor
resource, passes all-eight-workspace verification and required regressions, updates the
ledger/handover with observed scope, and lands as one atomic A6 feature commit.

Until those gates execute, A6 remains `design_only`. An implemented replay library,
private checkpoint, planner candidate, deterministic test, sealed artifact, or fixture UI
does not promote it.
