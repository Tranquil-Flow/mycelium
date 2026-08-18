# Mycelium A7 Fenced KV-Successor Recovery Specification

**Status:** approved design boundary; implementation waits for A6 completion
**Gate:** A7
**Parent:** `2026-08-11-mycelium-completion-plan.md`
**Reconciles:** `2026-08-11-mycelium-m19-scoped-recovery.md` KV slice and
`2026-08-11-mycelium-m23-heterogeneous-stage-local-kv.md`
**Depends on:** A4 interruptible scoped liveness and cleanup; A6 physically qualified
full-context replay fallback; current stage-local KV qualification
**Architecture:** Synthesized architecture sections 4.6, 4.10, 4.11, 4.13, and 4.15

## 1. Outcome and claim boundary

A7 lets an ordinary browser request resume on a compatible standby from a monotonically
acknowledged stage-local KV checkpoint after an assigned placement fails. The old request
attempt is fenced before the successor may publish. When the route-wide acknowledged KV
watermark exactly equals the browser's committed-output watermark, the successor continues
at the next position without replaying the prompt or committed token prefix.

A standby is recovery capacity, not automatically an A5 traffic replica. A7 does not
claim request-level data parallelism, full-model data parallelism, tensor parallelism,
cross-backend KV migration, arbitrary cache conversion, prefix caching, disaggregated
prefill/decode, or continued publication by both source and successor. A standby may serve
ordinary traffic only if A5 separately qualifies and admits it as a replica.

Any missing, lagging, corrupt, stale, skipped, regressed, cross-generation, cross-attempt,
or incompatible checkpoint rejects `fenced_kv_successor`. The request then enters the
already physically proven A6 full-context replay path, or aborts explicitly if A6 has no
qualified successor. A7 never labels replay as KV continuation.

This is dependency-ready design only. M23 proves stage-local incremental execution within
its exact route; it does not prove transferable successor KV. Existing M19 documents,
modeled checkpoints, an all-local runtime, or a fixture recovery panel cannot satisfy an
A7 product or physical gate.

## 2. Retained authorities and integration order

- **Request gateway:** owns private output delivery and the committed-output watermark.
- **Source runtime:** owns current stage-local KV and creates private immutable checkpoints
  for only its assigned layers and request attempt.
- **Standby runtime:** verifies and stores bounded shadow checkpoints but owns no output,
  decode, route, or selection authority before cutover.
- **Checkpoint coordinator/Router:** owns route-wide watermark assembly, acknowledgement,
  fencing generation, replacement path attempt, cutover, and exact source/standby cleanup.
- **A4 liveness authority:** owns the observed physical failure and narrow scope.
- **Planner/Provisioner/loader:** own deterministic standby intent, exact assignment-local
  artifacts, memory reservation, load proof, startup challenge, and candidate lifecycle.
- **Qualifier:** alone decides exact source/successor KV compatibility and current standby
  recovery readiness. The UI and checkpoint transport cannot qualify themselves.
- **A6 recovery authority:** remains the mandatory truthful fallback and is not weakened or
  bypassed by a partially available KV checkpoint.
- **Registry:** retains explicit deployment/model selection for future requests. Request-
  scoped successor cutover cannot displace the incumbent.

A7 shared integration begins only after A6's physical positive/negative gates and atomic
commit. A7 lands as one separate atomic feature commit. It does not reopen or rewrite A6
evidence, A5 replica qualification, or the source deployment's owner authority.

## 3. Exact KV compatibility identity

Source and successor checkpoint identity binds exactly:

- owner-approved model identity, immutable revision, serving representation and manifest,
  architecture, tokenizer semantics, and checkpoint digest;
- request/session, deployment and epoch, source and successor path IDs, attempts and
  generations, placement and assignment identities, exact half-open layer range, and
  component roles;
- stage-local KV schema/version, backend and runtime build, cache implementation, attention
  layout, query/KV head counts, head dimension, batch shape, dtype, quantization effect,
  device/layout encoding, RoPE configuration/scaling, position and sequence semantics,
  decode mode, context policy, and byte order;
- committed-output count/digest, KV position, checkpoint sequence, previous checkpoint
  digest, creation/expiry, and source/successor membership incarnations; and
- source load proof, successor load proof, source qualification, successor qualification,
  directed path evidence, resource reservation, and compatibility decision.

A7 v1 permits only a qualifier-approved byte-exact checkpoint schema on the same backend,
runtime family/build, and cache layout. M23's MLX and NumPy stage-local KV modes do not make
their cache representations cross-compatible. Cross-backend, cross-layout, cross-dtype,
or converted KV migration remains a separately reviewed future program and must fall back
to A6.

Every replacement placement owns exactly the failed placement's layer range and component
roles. The resulting successor path covers the same complete model operation with current
qualified directed edges. Unknown compatibility data is incompatible, never assumed.

## 4. Standby lifecycle and resource authority

The Planner may propose a bounded standby only when current evidence shows that its host,
failure domain, artifact state, backend/runtime, memory tier, power/thermal policy, and
directed edges can survive the failure it covers. A standby independently acquires only
its assignment-local stage pack, loads it, passes startup/parity/cleanup challenges, and
receives a current qualifier result.

Standby admission reserves declared static, checkpoint, workspace, transport, and recovery
headroom without borrowing source or ordinary-serving capacity. The preflight calculates
exact maximum KV bytes from context, batch, layers, heads, dtype, checkpoint count, transfer
buffers, and safety reserve. Unknown or insufficient memory, disk, network, lifecycle,
thermal, or power evidence blocks standby readiness before copying KV.

Standby states are:

`proposed -> acquiring -> loaded -> challenged -> qualified -> shadowing ->`
`cutover_pending -> active_successor -> draining -> released`

Any state may become `rejected` or `failed` with exact cleanup. Artifact presence,
membership, load, or shadow checkpoint receipt does not make a standby qualified or active.
The incumbent remains the sole publisher until the fenced cutover succeeds.

## 5. Checkpoint, acknowledgement, and watermark protocol

KV checkpoint traffic uses a dedicated bounded authenticated encrypted channel; it does not
travel on the ordinary activation frame path or enter public evidence. Each source stage
emits an immutable checkpoint sequence linked to the previous digest. The assigned standby
verifies authority, exact identity, size, digest, position, sequence, and generation before
atomic promotion, then returns a signed acknowledgement bound to the stored digest and its
current incarnation.

The coordinator advances the route-wide acknowledged watermark only when every stage that
must change during the covered failure has an exact compatible standby acknowledgement for
the same request attempt and committed position. Partial stage acknowledgement is visible
as lag but grants no no-replay recovery authority.

Watermarks are monotonic and contiguous:

- an exact repeated sequence/digest is idempotent and performs no duplicate storage or
  authority advance;
- a repeated sequence with a different digest, a skipped predecessor, a regressed position,
  an old attempt/generation, or an acknowledgement from another incarnation fails closed;
- checkpoints ahead of the gateway's committed-output watermark cannot authorize cutover;
  checkpoints behind it remain valid bounded shadow data but require A6 fallback after
  failure; and
- `fenced_kv_successor` is eligible only when the complete route checkpoint and gateway
  committed watermark counts and digests exactly match.

The source may checkpoint asynchronously. A policy may require synchronous acknowledgement
before committing selected output positions when bounded no-replay coverage is desired.
The UI reports the real lag and coverage; it never calls an asynchronous, lagging standby
continuous. Backpressure, timeout, cancellation, bandwidth, retained-checkpoint count, and
maximum bytes are frozen before physical measurement.

Raw KV, tokens, prompts, activations, keys, addresses, and paths remain private. Public
records expose only bounded identities/digests, counts, positions, aggregate bytes, lag,
timestamps, state, reason, and freshness.

## 6. Fencing and cutover

After A4 observes a covered source failure, the coordinator freezes the gateway watermark
and latest complete acknowledged checkpoint. It durably increments request attempt and
fencing generation before successor decode. The gateway, Router, every surviving stage,
transport ingress, and standby reject frames, output, cleanup mutation, or terminal results
from the old attempt/generation.

An unreachable old process is fenced by authority, not trusted to stop itself. A reconnecting
source must learn the new generation and drain; it cannot reclaim the old stream. If any
current participant cannot install or prove the fence, cutover is withheld and A6/abort owns
the outcome.

The successor atomically opens the exact checkpoint, proves its position equals the frozen
committed watermark, reserves the complete replacement path, and performs a bounded local
next-position challenge without public output. One compare-and-swap then grants the new
attempt publication authority. No recovery prefill occurs and no prompt or committed-token
prefix is sent to the successor in the successful A7 path.

Late source checkpoints, repeated cutover, duplicate terminal events, stale cancellation,
and source output after the fence are rejected. Failure during cutover never permits both
publishers: the state either remains old-and-aborting, becomes new-and-active once, or falls
back through a new A6 attempt.

## 7. Fallback and exact cleanup

The following outcomes are disjoint:

- `fenced_kv_successor`: exact acknowledged KV, no replay, next-position continuation;
- `full_context_replay`: A6 reconstructs from private prompt plus committed tokens and
  reports `kv_outcome=rejected_or_unavailable`; or
- `aborted`: neither mode has qualified authority and one explicit terminal result is
  published.

Rollback, skipped/repeated-conflicting watermark, lag, corruption, expiry, source or
successor restart, cross-generation, stale attempt, incompatible backend/layout/dtype/RoPE,
standby pressure, failed fence, qualification expiry, and duplicate terminal publication
are exercised before successor work or force the A6/abort path.

Completion, cancellation, timeout, fallback, cutover failure, standby loss, source return,
shutdown, and terminal state release checkpoint transfer buffers, shadow KV, source KV,
successor KV, path/capacity reservations, commands, pending acknowledgements, receipts, and
streams exactly once. Baseline-to-terminal counters must return to zero on source and
standby. Bounded terminal history and incident/audit records are intentional retained state
and are accounted separately.

## 8. Closed contracts and restart reconciliation

A7 introduces closed, bounded, canonically digest-bound records:

1. `mycelium.private_kv_checkpoint.v1` — private exact cache identity, predecessor,
   watermark, byte bounds, encrypted payload reference, expiry, and source authority.
2. `mycelium.private_kv_checkpoint_ack.v1` — private standby verification, stored digest,
   position, current incarnation, resource reservation, and acknowledgement authority.
3. `mycelium.kv_standby_intent.v1` — Planner-owned placement, compatibility requirements,
   failure coverage, bounds, hysteresis, and `route_ready=false` claim boundary.
4. `mycelium.kv_successor_runtime.v1` — privacy-reduced source/standby lifecycle,
   checkpoint sequences, aggregate bytes, acknowledged watermark/lag, fence, cutover,
   fallback, cleanup, and terminal outcome.
5. `mycelium.kv_successor_qualification.v1` — qualifier-owned exact compatibility,
   stage/path completeness, resource, parity, fence, fallback, physical challenge, and
   expiry result.
6. `mycelium.kv_successor_recovery_gate.v1` — owner-private executed positive/negative,
   browser, performance, counter, cleanup, and regression evidence with a privacy-reduced
   projection.

All contracts reject unknown fields, unbounded collections, private projection fields,
non-finite/negative values, stale generations, illegal transitions, duplicate/conflicting
IDs, incompatible identity, and digest mismatch. Cross-process sources and fixtures are
governance-pinned.

After coordinator, source, standby, or gateway restart, each checkpoint and recovery
attempt reconciles to exactly one of `shadow_restored`, `fallback_required`, `released`,
`aborted`, or `already_terminal`. Reconciliation validates filesystem/store identity,
checkpoint digest chain, current owner authority, membership incarnation, resource
reservation, fence, and terminal compare-and-swap. Orphans and ambiguous publishers are
quarantined and cannot be rendered ready.

## 9. Eight-workspace product behavior

Product copy uses `KV successor recovery`, not A/M milestone labels.

- **Inference:** committed count, recovery mode, current attempt, acknowledged KV coverage,
  cutover/fallback progress, one terminal result, and explicit no-replay versus replay copy.
- **Device Lab:** stage-local KV schema/runtime, checkpoint transport, memory, lifecycle,
  cancellation, fencing, and cleanup requirements for standby eligibility.
- **Network:** source, standby, shadow-checkpoint channel, ghosted old path, current
  successor path, fence generation, and observed cutover frames without KV content.
- **Nodes:** exact layer/backend role, source/standby state, aggregate KV bytes, watermark,
  lag, reservation, work, qualification, drain, and terminal cleanup.
- **Plans:** standby intent, covered failure, compatibility, resource/failure-domain cost,
  hysteresis, rejected candidates, and A6 fallback availability.
- **Readiness:** independent artifact, load, stage-local KV, schema, resource, checkpoint,
  acknowledgement, fence, path, parity, qualification, fallback, and cleanup rungs.
- **Incidents:** detector source, mismatch/rejection, old/new attempts and generations,
  last acknowledged count, fence, cutover, fallback reason, cleanup, and terminal outcome.
- **Settings:** future-request standby count, coverage, checkpoint cadence, lag/byte/deadline
  limits, synchronous-ack policy, fallback, retention, and privacy controls.

Unknown KV coverage, lag, compatibility, or cleanup remains unknown and withholds readiness.
All views share one live public generation and distinguish live, degraded, stale,
historical, replay, fixture, unavailable, and unknown. Direct navigation, refresh during
shadowing/cutover, Back/Forward, reconnect, terminal history, responsive layout, keyboard,
reduced motion, accessible names, privacy redaction, and a clean second session are required.

## 10. Frozen design-only acceptance inventory

The closed manifest at `tests/a7_acceptance/inventory.v1.json` is the machine-readable
A7 acceptance inventory. It freezes authority ownership, the route-wide acknowledgement
rule, exact gateway-watermark equality, compatibility identity, fencing behavior, disjoint
outcomes, browser evidence, and positive/negative scenario invariants. Missing scenarios,
unknown fields, widened authority, or invented success evidence fail the inventory checks.

The coordinator may advance the route-wide acknowledged KV watermark only when every stage
that must change has acknowledged one exact checkpoint for the same request attempt,
committed count and digest, checkpoint chain, and current standby incarnation. Partial,
mixed, skipped, regressed, conflicting, or stale acknowledgement sets report lag but grant
no cutover authority. Successful A7 eligibility requires exact count and digest equality
between that complete route watermark and the frozen gateway-committed state; neither
ahead nor behind is treated as equal.

Compatibility is closed over the exact model and immutable revision, assigned half-open
layer range and component roles, backend and runtime family/build, and byte-exact KV cache
schema/layout/dtype/shape/position semantics. Unknown or converted compatibility is
incompatible. Only a current qualifier result for the complete replacement path may admit
the successor; receipt of KV, candidate intent, membership, or UI state cannot.

Request attempt, path generation, and fencing generation advance monotonically and durably
before successor decode or publication. The gateway, Router, runtimes, transport ingress,
and standby must install the fence. Old-generation checkpoint, acknowledgement, frame,
output, cancellation, cleanup, or terminal events are rejected without ledger advance or
authority mutation. At most one generation may publish.

The successful `fenced_kv_successor` path performs no recovery prefill and transfers no
prompt or committed-token prefix. It opens the exact acknowledged KV and publishes only
the next contiguous position. If any watermark, compatibility, qualification, resource,
or fence gate fails, the same ordinary browser request enters the qualified A6
`full_context_replay` interface and is labelled replay, never KV continuation. If neither
A7 nor A6 has current authority, the request aborts explicitly with one honest terminal
result and no silent restart or recovery-success claim.

Cancellation before or after cutover, fallback, abort, timeout, source return, standby
failure, and shutdown retain one terminal authority and release all source/standby KV,
buffers, reservations, commands, acknowledgements, receipts, and streams exactly once.
Browser-visible evidence is privacy-reduced and shows the real committed and acknowledged
counts, lag, compatibility/qualification state, attempt and fence generation, recovery
mode and phase, fallback or abort reason, cancellation, cleanup, and terminal outcome. It
never exposes raw KV, prompt, token IDs, output content, activations, keys, addresses, or
private paths.

This inventory is acceptance design, not KV transport, runtime recovery, Router wiring,
UI implementation, or physical evidence. It always reports `gate_state=design_only`,
`qualification_claim=false`, and `promotion_authorized=false`; passing its tests cannot
promote A7.

## 11. Verification matrix

### Contract, deterministic, and adversarial gates

- Closed-shape, canonical digest, privacy, count/size, stale authority, unknown-field,
  identity mismatch, invalid transition, chain corruption, expiry, and duplicate/conflict
  tests for every contract.
- Exact tests cover Qwen2/Qwen3 qualified shapes where supported; tied/untied heads;
  position/sequence edges; rollback, skip, idempotent repeat, conflicting repeat,
  cross-generation, stale attempt, old incarnation, incompatible layout/dtype/RoPE/backend,
  checkpoint corruption, lag, pressure, cancellation, standby exit, restart, fence failure,
  source return, and duplicate terminal rejection.
- Deterministic interleavings cover output commit/ack, failure/checkpoint, fence/source
  output, cutover/cancel, fallback/late ack, shutdown/cutover, restart/terminal, and
  projection during mutation with exactly one publisher.
- A7 fallback tests invoke the ordinary A6 product interface and prove that a KV rejection
  cannot be relabelled as continuity.

### Physical positive gate

1. Provision a source and independently qualified compatible standby for the exact model,
   representation, layer range, backend/runtime, KV schema/layout, path, and workload.
2. Through an ordinary browser request, produce a nonzero route-wide checkpoint whose
   signed standby acknowledgements exactly match the gateway committed watermark.
3. Kill or disconnect the assigned source placement. A4 observes the real failure. Fence
   the old attempt/generation, promote the stored checkpoint, and cut over to the qualified
   successor without recovery prefill or prompt/token-prefix replay.
4. Prove uninterrupted-reference parity, no duplicate/missing output, exactly one publisher
   and terminal result, positive successor work/frame counters, source stale-publication
   rejection, bounded interruption, and zero terminal resource delta on all participants.

### Physical negative and fallback gates

Repeat a physical mid-request failure with one controlled incompatibility or lag so the KV
checkpoint cannot qualify. Prove no KV-successor work or continuity claim occurs, and the
same browser request continues through the already qualified A6 full-context replay path
with parity, no duplicate delivery, one terminal result, and exact cleanup. Also exercise a
no-A6-successor case that aborts explicitly. Deterministic/adversarial tests cover every
other mismatch listed above; no authored sealing boolean may substitute for the physical
fallback and abort observations.

### Browser, performance, and regression gates

All eight live workspaces are exercised through shadowing, successful cutover, A6 fallback,
and abort, including refresh, reconnect, Back/Forward, workspace switching, stale/degraded
state, terminal history, accessibility, privacy, and a clean second session. The gate records
checkpoint bytes, cadence, acknowledgement lag, added TPOT/throughput/memory cost, cutover
latency, fallback latency, and cleanup. Thresholds and allowed regression are frozen before
candidate measurements. Focused, M23 parity, A4/A6 recovery, full Python, frontend,
contract, governance, claim-boundary, privacy, security, and invalidated physical suites pass.

## 12. Completion

A7 is complete only when a compatible physical standby continues an ordinary browser
request from an exact acknowledged KV watermark without replay or stale publication, an
incompatible physical case truthfully falls back through A6, a no-fallback case aborts,
reference parity/exact delivery/one publisher/exact cleanup hold, all eight workspaces pass,
performance cost is reported honestly, ledgers describe only observed scope, and one atomic
A7 feature commit lands.

Until then A7 remains `design_only`. A stage-local KV route, checkpoint serializer,
standby process, modeled cutover, deterministic test, or fixture projection is not A7
product integration or physical qualification.
