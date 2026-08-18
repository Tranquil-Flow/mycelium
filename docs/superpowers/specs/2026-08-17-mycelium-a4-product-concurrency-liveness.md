# Mycelium A4 Product Concurrency and Scoped Liveness Specification

**Status:** `design_only`; approved dependency-ready acceptance boundary; implementation waits for A3 closure
**Gate:** A4
**Parent:** `2026-08-11-mycelium-astra-completion-plan.md`
**Architecture:** Astra sections 4.6, 4.7, 4.11, and 4.13

## 1. Outcome and claim boundary

A4 makes the ordinary browser inference path genuinely concurrent and promptly
interruptible. Multiple admitted requests may wait, prefill, decode, stream, cancel,
and clean up independently. Valid traffic receipts suppress redundant active-period
heartbeats; bounded idle keepalive detects quiet loss. Failures are assessed at the
narrowest truthful request, edge, placement, peer, or deployment scope.

A4 preserves the qualified M16 complete-path reservation and immutable
`PathManifest` boundary. It replaces the generation-long route-global execution lock,
single dispatch thread, uninterruptible stage commands, and deployment-global fatal
latch where those mechanisms serialize or over-broaden failure.

A4 does **not** claim:

- stage or whole-model replication (A5);
- request continuation, full-context replay, or successor cutover (A6);
- compatible KV standby or KV recovery (A7);
- unrelated-network or relay operation (A8);
- runtime microbatching, continuous batching, or pipeline overlap (A10);
- speculative decoding (A11).

Concurrent admission is not a runtime batch. An A4 runtime may truthfully report
`sequential_dispatch` while still proving independent bounded request lifecycles.
An affected request terminates explicitly in A4; it never silently restarts or claims
recovery.

## 2. Opening state and retained authorities

The following existing boundaries remain authoritative:

- The deployment qualifier is the only readiness authority. A4 cannot turn a stale,
  prepared, loaded, compatible, or historically qualified deployment into a selectable
  one.
- The deployment registry owns explicit future-request model selection.
- The M16 admission ledger owns complete-path reservations, queue bounds, QoS aging,
  immutable path commitment, and exact terminal cleanup.
- The Router owns request path and dispatch state. The transport owns physical send,
  receipt, connection, and active-failure observations. Membership owns leases and
  peer generations. The liveness detector consumes those authorities; it does not
  invent them.
- The request gateway owns private prompt/output and browser stream state. Privacy-
  reduced projections contain neither.
- A3's exact model/revision/representation, assignment-local artifacts, load proofs,
  graph, and qualification binding remain unchanged.

Historical `mycelium.m19_*` documents and direct qualification scripts are design or
sealed evidence only. A4 may reuse their pure algorithms after review, but it must
emit new runtime-owned product contracts and fresh product-path evidence.

The retained authorities divide mutable state into closed subsets. Coordinators may
submit validated commands and consume detached snapshots, but they do not become
mutation owners merely because they assemble a response or projection:

| State subset | Sole mutation owner |
| --- | --- |
| Deployment readiness | Deployment qualifier |
| Future-request model selection | Deployment registry |
| Browser session, replay cursor, and private prompt/output | Request gateway |
| Request path, dispatch, lifecycle, and event sequence | Router |
| Complete-path reservations and terminal cleanup record | M16 admission ledger |
| Stage-command deadline and cancellation generation | Command controller |
| Stage/placement capacity and stage-local KV state | Placement runtime |
| Physical send, connection, and receipt observations | Transport |
| Peer lease and incarnation generation | Membership |
| Subject freshness and scoped incident ledger | Liveness detector |

Every shared or public view is a same-generation, privacy-reduced subset of an
owner-private snapshot. It may omit or pseudonymize fields; it may not synthesize
ownership, union generations, expose private fields, or turn absent/unknown values into
zero or healthy state. A stale, wrong-owner, or cross-generation mutation command fails
closed before changing state. Cleanup is accepted only from the recorded owner for the
matching request/attempt/generation; rejected cleanup cannot release another owner's
resources. A cross-owner conflict remains at the narrowest affected scope and cannot
become deployment-fatal unless it independently proves an allowlisted fatal condition.

## 3. Closed product contracts

A4 introduces capability-named, versioned contracts rather than browser-facing
milestone names:

1. `mycelium.concurrent_request_runtime.v1` — Router/dispatcher-owned bounded request,
   queue, worker, command, reservation, cancellation, and cleanup state.
2. `mycelium.traffic_liveness.v1` — detector-owned subject observations, budgets,
   receipt suppression, keepalive state, generations, and freshness.
3. `mycelium.scoped_runtime_incident.v1` — bounded incident records carrying detector
   source, scope, affected current tracks, action, and outcome.
4. `mycelium.interruptible_stage_command.v1` — private controller/node command envelope
   binding request, attempt, immutable path, operation, deadline, cancellation
   generation, and idempotency identity.
5. `mycelium.product_concurrency_liveness_qualification.v1` — owner-private executed
   gate artifact with a privacy-reduced public projection.

Every document is closed and rejects unknown fields, unknown protocols, duplicate IDs,
non-finite or negative measurements, stale generations, invalid transitions, and
unbounded collections. Cross-process documents are canonically encoded and digested;
signed observations retain signer and membership-generation authority.

The public projection excludes prompts, decoded text, token IDs, tensors, activations,
KV content, credentials, keys, raw EndpointIDs, addresses, usernames, hostnames, local
paths, command lines, and exception strings. Request and peer IDs are bounded opaque or
pseudonymized identifiers.

## 4. Concurrency and lock decomposition

One short metadata lock may protect registry maps and monotonic counters, but it must
never be held while waiting for admission, a queue slot, a worker, a stage command,
transport I/O, a browser write, process shutdown, or remote cleanup.

Long-lived state is independently synchronized at these scopes:

- deployment authority/selection;
- browser session and replay cursor;
- request lifecycle and event sequence;
- immutable path attempt and reservation set;
- stage/placement runtime capacity;
- transport connection and receipt table;
- detector subject and incident ledger.

When an operation needs more than one scope, locks are acquired in this order and
released before blocking work:

`authority -> deployment -> session -> request -> path -> placement -> transport -> detector`

No callback may re-enter an earlier scope while holding a later one. Contract tests
and a deterministic concurrency harness must prove that inversions fail before
physical execution. Snapshot projections detach state under the relevant short lock
and render outside it.

Absence of the old route-global lock is an explicit acceptance condition, not an
inference from two requests eventually completing. The deterministic harness blocks
one request in turn at admission, queue claim, stage command, transport I/O, browser
write, cancellation, and cleanup boundaries. At each boundary, another admitted
request must advance dispatch and the blocked request must remain cancellable with
exact owner-scoped cleanup. No mutex, semaphore, condition, worker join, or advisory
lease may recreate generation-long route-global serialization under another name.

The lock-order/deadlock detector records the privacy-reduced held and requested scope
ranks plus bounded opaque owner identities. It rejects an inversion or wait-for cycle
before a physical command is issued, unwinds the owning operation through its explicit
request cleanup path, opens a bounded request-scoped incident, and leaves unrelated
dispatch live. Deterministic barriers and bounded joins must make a missed cycle fail
the test instead of hanging;
a test timeout or watchdog kill alone is not evidence that deadlock handling works.

Per-request lifecycle events have a monotonic sequence. Idempotent duplicates are
accepted only when their canonical digest matches; conflicting duplicates fail the
request scope. One request's slow consumer, cancellation, timeout, or terminal write
cannot block another request's dispatcher progress.

## 5. Bounded dispatcher and worker ownership

The single dispatch thread is replaced by a fixed, configured worker pool plus the
existing bounded admission queue. The pool size, per-placement command concurrency,
queue item/byte limits, and browser event-buffer limits are finite product settings;
they are never derived from discovered peer count or silently made unbounded.

The dispatcher preserves:

- qualifier-gated admission and exact deployment selection;
- complete-path reservation before physical dispatch;
- interactive priority and bounded batch aging;
- deterministic tie breaking;
- immutable path attempt and topology binding;
- per-placement capacity and KV accounting;
- bounded backpressure and retry hints;
- exactly-once terminal cleanup.

A worker claims one queued request generation atomically. It cannot claim a stale,
cancelled, expired, already-owned, or terminal request. A worker crash returns only its
owned request through the explicit terminal/cleanup path; it does not drain unrelated
queue entries or poison the deployment.

Shutdown stops new admission, interrupts active commands, drains bounded cleanup, joins
every worker, and proves zero queued items, active commands, reservations, pending
receipts, stream buffers, and stage-local KV states.

## 6. Interruptible command protocol

Every stage or transport command binds:

- deployment ID/epoch and qualification digest;
- request ID, request attempt, path ID/digest, and topology generation;
- stage, placement, assignment, and operation ID;
- command kind (`prefill`, `decode`, `cleanup`, `shutdown`, or `probe`);
- issue and absolute monotonic deadline;
- cancellation generation and idempotency digest;
- expected terminal compare-and-swap and bounded cleanup-result envelope;
- maximum request/response bytes.

The controller owns the deadline and cancellation generation. The node acknowledges
start, periodically reaches bounded cancellation points, and returns exactly one of
`completed`, `cancelled`, `deadline_exceeded`, `peer_unavailable`, or a closed bounded
error code. A stale cancellation generation or late result cannot mutate a newer
request attempt.

The owner-approved cancellation model is cooperative bounded-step execution. Prefill
and decode are divided into bounded cancellable work units, and node command handling
remains responsive while those units execute. Cancellation is correlated by request,
attempt, path digest, absolute deadline, and cancellation generation. Both the command
controller and runtime revalidate that identity at each cancellation point and before
terminal compare-and-swap or cleanup.

Command transport uses a short write lock for one canonical frame and
command-ID-correlated waiters; no lock spans request and response. A bounded command
worker may execute runtime work, but the node continues reading cancellation, deadline,
cleanup, and unrelated commands from stdin. Interruption and request-owned cleanup must
together complete within one 2,000 ms end-to-end bound. A backend that cannot prove this bound is A4-ineligible
and remains unavailable rather than weakening the gate.

Dedicated request-owned process and SSH wrappers may use process groups and bounded
terminate/kill escalation; a shared node process may not.
Native transport commands close or cancel their request/stream handle without closing
unrelated persistent connections. Thread cancellation is cooperative; A4 must not rely
on unsafe asynchronous thread termination.

Killing a shared node is never request-scoped cancellation: it destroys unrelated
request/path/attempt ownership and cannot satisfy this gate. Command failure and cleanup
remain scoped by request, path, and attempt and are not promoted automatically to
deployment-global fatal state. A4 disables automatic replay or recovery; an affected
request terminates explicitly, and replay/recovery remains owned by A6.

The active-disconnect gate requires the blocked command to return cooperatively and all
request-owned cleanup to finish no later than 2,000 ms after the detector's verified
active-failure observation and before the previous uninterruptible command timeout.
There is no second cleanup budget after interruption.

## 7. Traffic-aware liveness

Detector subjects are directed edges, placements, peers, and deployment generations.
State is `fresh`, `suspect`, `quarantined`, `failed`, or `recovered`.

Frozen default budgets are:

| Measurement | Default bound |
| --- | ---: |
| Verified active transport failure detection | 2,000 ms |
| Idle keepalive interval | 5,000 ms |
| Suspect threshold | 2 consecutive missed keepalives |
| Quarantine threshold | 3 consecutive misses and at least 15,000 ms stale |
| Recovery | 2 consecutive fresh signed observations |
| Detector subjects | 4,096 maximum |
| Retained scoped incidents | 256 maximum |

A valid application/activation delivery receipt is passive liveness for its exact
directed subject and membership generation. It suppresses an otherwise-due keepalive
for that interval. A receipt from another edge, old generation, old process, or merely
connected socket cannot refresh the subject.

One missed receipt or command deadline enters `suspect` only. It does not declare a
whole peer dead, revoke membership, rebuild the deployment, or fail unrelated
requests. Idle keepalive begins only after traffic freshness expires. Active transport
failure and idle liveness-stale remain distinct sources and UI incidents.

Recovery from suspect/quarantine requires the declared consecutive fresh signed
observations at the current membership generation. A new incarnation never inherits
the old incarnation's receipts, pending commands, reservations, qualification, or
detector state.

## 8. Failure scope and fatal allowlist

The detector computes the narrowest affected scope from the immutable path and current
legal-track set:

- **session:** browser stream detachment, reconnect, or replay-cursor rejection; these
  affect the subscription and private replay authority, not the server-owned request;
- **request:** authenticated request cancellation/deadline, malformed request-local
  response, or one command failure;
- **edge:** one directed transport subject loses receipts or fails actively;
- **placement:** one assigned runtime is unavailable or violates its load proof;
- **peer:** the current signed incarnation is quarantined or its lease expires;
- **deployment:** every legal qualified track is lost or a deployment-wide invariant is
  contradicted.

A non-participating peer failure changes membership/detector evidence only. It cannot
block current-route admission, fail active requests, or mark the selected deployment
unavailable. An edge/placement/peer failure affects only immutable paths containing it.
Surviving legal paths remain admissible, but A4 does not create or qualify successors.

Only these errors may latch deployment-fatal state:

1. contradiction in immutable deployment/model/representation/graph authority;
2. corruption of the deployment-wide reservation/resource ledger that prevents exact
   ownership or cleanup;
3. compromise or generation conflict in the active deployment authority that cannot be
   isolated to one peer/incarnation; or
4. loss of every currently qualified legal track.

All fatal transitions record the exact allowlist reason and evidence generation.
Unknown exceptions fail the owning request/worker and open a bounded incident; they do
not become deployment-fatal merely because they were unexpected.

## 9. Product behavior and persistence

Qualification and selection remain future-request scoped. A request accepted under a
current qualification binds that exact generation until terminal state. Expiry blocks
new admission but does not rewrite history or silently move an active request.

Concurrent request state is retained by the server's bounded lifecycle ledger. Browser
refresh, section navigation, Back/Forward, gateway reconnect, and SSE replay reconstruct
the same current public generation and terminal outcomes. Tab-private prompt/output
history remains isolated between browser sessions; server evidence retains only
privacy-reduced request facts.

A clean second browser session may submit concurrently but cannot read the first tab's
prompt, decoded output, or tab-private history. Both sessions observe the same public
deployment, queue, path, liveness, and incident authority.

A gateway disconnect during waiting, prefill, or decode detaches only that authenticated
session's stream; inference continues under the same request/attempt/path ownership.
Reconnect presents the last fully applied event sequence and resumes strictly after it,
with no gap or duplicate output and at most one live subscriber. A cross-session,
invalid, future, or expired cursor fails closed without exposing retained output,
changing the request, or guessing success. Cancellation after reconnect targets the
same active request, terminates it once, and invokes request-owned cleanup once. While
the stream is detached or replaying, another request's dispatcher progress remains
independent. Terminal replay and all shared workspace projections remain privacy-
reduced; private prompt/output never enters public state or another session.

## 10. Eight-workspace UI contract

Product copy uses capability language and contains no A/M milestone labels.

- **Inference:** per-request waiting, admitted, queued, prefill, first-token, decode,
  cancelling, cleanup, interrupted, and terminal state; independent request progress;
  bounded retry and interruption reason.
- **Device Lab:** capability requirements for interruptible commands, receipt support,
  idle keepalive, generation fencing, and maximum concurrent commands. Synthetic
  browser workers remain visibly ineligible for model stages.
- **Network:** directed edge freshness, passive receipt/idle probe source, suspect and
  quarantine state, affected current paths, and connection reuse.
- **Nodes:** current incarnation, detector state, active/maximum commands, queue and
  reservation load, receipt age, and recovery observations without private identity.
- **Plans:** immutable current paths, retained legal paths, failure-scope result, and
  explicit deferral of successor/recovery behavior.
- **Readiness:** independent qualification, dispatcher, interruption, cleanup,
  liveness, and fatal-allowlist checks with their observed budgets.
- **Incidents:** detector source, narrow scope, affected paths/requests, action,
  interruption latency, cleanup, and outcome; active failure and idle staleness are
  separate records.
- **Settings:** bounded worker/queue and detector policy with qualified defaults; edits
  apply to future requests/generations and cannot mutate an active path.

All eight views consume the same backend generation and truthfully display stale,
degraded, unavailable, and unknown state. Missing measurements are never zero or
healthy by default.

The machine-checked inventory binds the workspace requirements to acceptance scenarios
as follows. `active_request_reconnect`, `state_subset_ownership_boundaries`, and
`second_session_privacy` apply across all eight workspaces in addition to the focused
mappings below.

| Workspace | Focused acceptance scenarios |
| --- | --- |
| Inference | `overlapping_requests`, `route_global_lock_absence`, `lock_order_deadlock_detection`, `cancel_isolation`, `participating_active_disconnect`, `late_command_result`, `queue_saturation`, `worker_exit` |
| Device Lab | `overlapping_requests`, `participating_active_disconnect`, `participating_idle_staleness`, `one_missed_receipt`, `stale_incarnation_receipt`, `late_command_result`, `bounded_shutdown` |
| Network | `overlapping_requests`, `participating_active_disconnect`, `participating_idle_staleness`, `nonparticipating_peer_exit`, `one_missed_receipt`, `stale_incarnation_receipt` |
| Nodes | `overlapping_requests`, `route_global_lock_absence`, `cancel_isolation`, `participating_active_disconnect`, `nonparticipating_peer_exit`, `stale_incarnation_receipt`, `late_command_result`, `queue_saturation`, `worker_exit`, `bounded_shutdown` |
| Plans | `overlapping_requests`, `cancel_isolation`, `participating_active_disconnect`, `participating_idle_staleness`, `nonparticipating_peer_exit`, `one_missed_receipt`, `fatal_allowlist_rejection` |
| Readiness | Every scenario; it owns the independent check matrix and observed budgets, not the underlying runtime state |
| Incidents | Every fault scenario; successful overlap has no fabricated incident |
| Settings | `overlapping_requests`, `route_global_lock_absence`, `participating_idle_staleness`, `one_missed_receipt`, `queue_saturation`, `bounded_shutdown` |

## 11. Verification matrix

### Contract and deterministic tests

- The machine-checked scenario inventory in
  `tests/a4_acceptance/scenarios.v1.json` freezes the required positive, negative,
  browser, latency, cleanup, scope, and workspace coverage. Passing its manifest test
  proves only that acceptance inputs are complete; each scenario remains unsatisfied
  until its named product-path test or physical artifact executes.
- Closed-shape, size/count, canonical digest, privacy, unknown-field, stale generation,
  duplicate/conflict, and illegal transition tests for every new contract.
- Lock-order and deterministic interleaving tests cover admission/cancel, timeout/
  completion, receipt/keepalive, disconnect/cleanup, shutdown/dispatch, and projection
  during mutation without deadlock or leaked ownership.
- The `route_global_lock_absence` harness blocks every specified wait boundary and
  proves independent progress, cancellation, owner-scoped cleanup, and no disguised
  generation-long lock. `lock_order_deadlock_detection` injects an inversion/cycle and
  proves rejection before physical execution, privacy-reduced reporting, bounded joins,
  exact release, unaffected progress, and no unallowlisted fatal latch.
- State-subset tests bind every mutable subset to its declared sole owner. Wrong-owner,
  stale-generation, cross-session, and non-owner cleanup attempts fail closed; public
  views remain same-generation privacy-reduced subsets with unknown values preserved.
- Worker tests prove bounded parallel claims, QoS/aging, queue/backpressure, independent
  slow consumers, process-group interruption, deadline escalation, idempotent cleanup,
  and complete worker join.
- Liveness tests prove passive receipt suppression, directed subject binding, one-miss
  suspect behavior, idle thresholds, active failure, generation conflict, quarantine,
  recovery, non-participating peer isolation, and the deployment-fatal allowlist.

### Physical positive gates

1. Two clean browser sessions submit overlapping requests through the ordinary product
   gateway. Both are independently admitted and reach terminal state; cancelling one
   while the other is waiting or decoding does not block or cancel the other. Physical
   stage and Router counters bind both request IDs, while the runtime reports its true
   batch mode.
2. During a browser request, one participating stage/transport is disconnected. The
   active failure is observed, its blocked command is interrupted within 2,000 ms and
   before the old timeout, the affected request terminates explicitly, and every
   request-owned reservation/KV/receipt/stream resource returns to baseline.
3. During an idle qualified interval, a participating subject becomes unreachable.
   The detector follows the declared keepalive -> suspect -> quarantine thresholds;
   new affected admissions fail closed without fabricating active-traffic failure.
4. A non-participating current swarm member exits while the selected route serves a
   browser request. That request and a subsequent admission on the unaffected route
   complete with positive physical counters.

### Physical negative gates

- One suppressed or missed receipt produces only `suspect` and leaves unaffected
  admission available.
- Old-incarnation receipts, stale command results, conflicting cancellation,
  generation rollback, and forged affected-track lists are rejected.
- Queue saturation returns bounded backpressure without worker or reservation leaks.
- A worker/process exit affects only its owned request; shutdown interrupts and joins
  all commands.
- Deployment-fatal state is rejected for any reason outside the explicit allowlist.
- No fixture, direct method, sealed M19 document, modeled timer, or qualification-only
  seam can satisfy a physical or browser check.

### Browser and regression gates

All eight workspaces are verified against the live backend through refresh, navigation,
Back/Forward, reconnect, stale/degraded state, terminal history, and a clean second
session. Accessibility, privacy, contract, governance, security, full frontend, full
Python, and physical regression suites pass after the executed evidence shape is
stable.

The active-request reconnect browser gate disconnects separately during waiting,
prefill, and decode. It proves server-side continuation, sequence-exact replay, one live
subscriber, cross-session privacy, fail-closed invalid/future/expired cursors,
cancellation after reconnect, exactly-once cleanup, independent other-request progress,
and the same public generation across all eight workspace projections.

## 12. Executed artifact and completion

The owner-private `mycelium.product_concurrency_liveness_qualification.v1` artifact
binds specification and source digests; selected deployment and qualification;
dispatcher/lock configuration; request/session/path/reservation identities; command
deadlines; detector budgets; traffic receipts; physical before/after counters; active,
idle, non-participant, cancellation, overload, shutdown, and fatal-allowlist results;
route-lock-absence and deadlock-detection results; state-owner/subset checks; active-
request reconnect/browser checks; all-eight workspace mappings; and regression/audit
results.

Its public projection exposes only bounded states, timings, counts, digests, scopes,
and reasons. A4 is complete only when:

1. real overlapping product requests execute independently with exact cleanup;
2. a blocked physical command is interrupted within budget;
3. active and idle liveness are physically distinguished;
4. non-participating and unaffected scopes remain usable;
5. deployment-fatal errors are limited to the reviewed allowlist;
6. all eight live workspaces and browser reconstruction checks pass;
7. full regressions and audits pass; and
8. the architecture ledger, handover, and one atomic A4 feature commit contain the
   specification, implementation, tests, and executed evidence.

Until then A4 remains `design_only`, even if individual concurrency or liveness units
already exist.
