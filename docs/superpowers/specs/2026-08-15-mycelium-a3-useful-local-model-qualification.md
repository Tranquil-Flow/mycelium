# Mycelium A3 Useful Local Model Qualification Specification

**Status:** implementation specification
**Gate:** A3
**Parent plan:** `2026-08-11-mycelium-astra-completion-plan.md`
**Depends on:** completed A2 assignment and representation authority

## 1. Outcome and claim boundary

A3 makes the largest useful already-local dense Qwen models honest product choices.
`Qwen/Qwen2.5-7B-Instruct` must complete a freshly planned, assignment-local,
physically qualified distributed route before it can enter the inference selector.
`Qwen/Qwen3-8B` must remain a complete compatible catalogue identity, but it stays
visibly blocked unless one exact representation independently passes the same gates.

A3 does not authorize a model download, repair, conversion, quantization, or
representation substitution. An owner decision must name one exact local model,
immutable revision, source representation, serving representation, quantizer if any,
runtime dtype, and representation digest before preparation may start. Approval for
one model or representation grants no authority for the other.

Presence in a cache, architectural compatibility, modeled feasibility, prepared bytes,
or a successful stage load is not qualification. Only the qualifier may publish a
deployment as selectable, and selection changes future requests only.

## 2. Frozen local identities

The current local inventory contains these complete source checkpoints:

| Model | Revision | Source representation | Source artifact digest | Layers | Source weight bytes |
|---|---|---|---|---:|---:|
| `Qwen/Qwen2.5-7B-Instruct` | `a09a35458c702b33eeacc393d103063234e8bc28` | BF16 Safetensors, four shards | `sha256:e9595b0aca297e2a522f6a6b7816370204bb0b3ccd915f21a20e73968ee4c2f0` | 28 | 15,231,233,024 |
| `Qwen/Qwen3-8B` | `b968826d9c46dd6066d109eabc6255188de91218` | BF16 Safetensors, five shards | `sha256:615c48741e928f0f43a9616b5a7ce43413319d09ec94773a90e487fba23d8ef8` | 36 | 16,381,470,720 |

The catalogue currently models these row-wise symmetric int8 serving
representations. On 2026-08-15 the owner explicitly authorized creating the exact
Qwen2.5-7B representation named below from the already-local source revision, with
no download or model substitution. That authority does not extend to Qwen3-8B.

| Model | Representation digest | Resident weight bytes | Modeled conversion load peak |
|---|---|---:|---:|
| Qwen2.5-7B | `sha256:c3029635eddc32a7e2f7e8c004a5ab4e96dd4366425a9e10e9e98f5bf2e63d04` | 7,623,395,328 | 38,085,861,376 |
| Qwen3-8B | `sha256:11721b56c82694801924b593823a70b999205f814b5982329236dd039f45ac78` | 8,198,478,848 | 40,961,420,288 |

The Qwen2.5-7B digest is an approved planning and conversion identity, not evidence
that the representation already exists. The Qwen3-8B digest remains proposed and
unauthorized. The source checkpoints must remain immutable and local-only throughout
A3.

## 3. Fresh topology and capability authority

A3 must not reuse the expired two-host feasibility reports from 2026-08-11. It captures
one fresh signed membership, resource, runtime, storage, thermal/power, and complete
directed-link generation from whatever currently enrolled peers are eligible. The
planner consumes that generation without a fixed host count or hard-coded node names.

The currently reachable candidates include the M4 Pro, Evi's 16 GiB Apple-silicon
MacBook, and the Surface NumPy host. This observation is not enrollment or placement
authority. Each host must join or resume normally, publish a current capability lease,
pass runtime and link probes, and have enough private disk for only its assigned
artifacts. Mobile and browser-only members remain visible but model-layer-ineligible.
Lenovo remains optional.

Every directed path observation must name its signing member as the local endpoint.
After automatic pressure exclusions, verified observations to excluded peers are
ignored; they cannot reintroduce an excluded node into the evidence graph. The
remaining signed members must still provide every directed edge required by the
selected route or the generation fails closed.

Resource snapshots use a signed, bounded lease. The ordinary interactive default is
two minutes; a product preparation operation may explicitly request up to sixty
minutes. The exact expiry is signed into every member snapshot and must be current when
the product freezes a preparation authorization. Authorization v2 records that start
time and remains immutable for the resulting cold build even when deterministic
sharding, private-volume materialization, and conversion outlive the source lease.
Acquisition still revalidates current membership, recipient authority, storage,
thermal/power policy, scoped grants, and source availability; it does not silently
replace the frozen allocation with a later feasibility generation. A preparation lease
is not qualification currency: activation and the physical request gate must recapture
current health, memory, thermal, and route evidence.

The capability-aware contiguous exact-weight dynamic program owns the allocation. Each
candidate interval accounts for representation-resident weights, role-specific static
components, bounded KV at the declared context/concurrency, runtime workspace and
reserve, conversion peak where conversion occurs, staging disk, backend support, and
directed activation cost. An infeasible result rejects before acquisition.

The product capacity refresh may consume an owner-private live observation file
produced by the same signed multi-member capture automation. It rereads and verifies
the file on every explicit refresh; the file is not a sealed-history or fixture seam.
Execution transport bindings are supplied separately as an enrolled topology document
whose ordered node IDs and runtime backends must exactly match the planner stages.
Neither document hard-codes a model, placement, or device count.

## 4. Representation decision and preparation

The owner decision is a closed `mycelium.model_representation_decision.v2` document.
V2 adds the source artifact digest, quantizer identity, and explicit no-download field
that the incumbent-compatible v1 contract did not carry directly. Existing v1 decisions
remain valid only for their already-bound preparation path; A3 cannot use v1 to create a
new larger-model representation.
The closed `mycelium.model_preparation_authorization.v2` adds the product-recorded
authorization start time and explicitly repeats the source-artifact digest and
quantizer from the owner decision. Its binding digest covers those identities as well
as feasibility, owner-decision, and representation digests. It rejects a start after
the signed feasibility expiry, but does not turn a long deterministic build into a
false stale-capacity failure. Existing v1 authorizations retain their stricter
same-lease behavior. The preparation authorization and every downstream manifest must
bind the exact same:

- model ID, immutable revision, and source artifact digest;
- source and serving quantization, runtime dtype, quantizer, and representation digest;
- catalogue and feasibility generation/digests;
- host set, ordered contiguous allocation, graph, deployment epoch, and workload;
- exact per-placement layers, component roles, files, tensors, bytes, and stage-pack
  digests.

Any mismatch fails before conversion, transfer, or runtime load. Candidate construction
uses bounded one-stage-at-a-time materialization and assignment-local A2 acquisition.
Row-wise int8 conversion is additionally streamed one tensor at a time in row chunks
whose source float payload is bounded to 32 MiB. The loader preallocates only the final
int8 tensor and its row scales, reads and quantizes bounded row slices, then releases
each source slice before the next. It must never hold the aggregate source tensors for
a stage in memory.

The planner and loader share this conversion bound. Each planned placement reserves
the full resident stage representation plus the largest single-tensor streaming
transient for that placement, as well as static components, KV, workspace, and reserve.
The runtime/build digest binds the implementation that enforces the bound. This loading
strategy does not change the source artifact digest, quantizer identity, or serving
representation digest. Candidate construction must reserve its modeled peak and output
disk before reading source weights. Failure leaves the incumbent 0.5B route selected
and usable, publishes no candidate, and records a bounded reason without private paths.

Every dynamically prepared candidate must be rebound to the current durable seed before
artifact acquisition or peer staging. The candidate keeps its immutable model,
representation, deployment, graph, assignment, stage-pack, and activation-endpoint
identities, while the durable seed reissues the offers with the current swarm identity,
member generations, leases, and seed signature. A preparation-local signing key is never
activation authority. An expired member lease, mismatched seed identity, or failed
rebinding rejects the candidate before publication and cannot disturb the incumbent.

### 4.1 Durable preparation recovery

Preparation is a resumable product operation, not an all-or-nothing shell command. The
operator workspace durably checkpoints these completed phases: frozen authority and
topology, locally challenged candidate build, current seed binding, assignment-local
acquisition, peer staging, and atomic publication. A checkpoint is canonical, private,
atomically replaced and fsynced before the next phase begins. It binds the preparation
binding, immutable representation authority, exact ordered stages, workspace filesystem
identity, candidate/deployment identity, and the cryptographic digests of every smaller
artifact needed to validate the completed phase.

A restarted product may resume only after validating the checkpoint and all referenced
artifacts. Missing, malformed, symlinked, permission-unsafe, tampered, differently mounted,
or authority-mismatched state fails closed. An interrupted acquisition resumes through
the A2 content-addressed store, whose partial and terminal records remain the authority
for bytes already verified. Seed binding is always reissued from current signed
membership before acquisition or staging, so a network or membership-lease interruption
never turns a stale offer into new authority. If fresh capacity has the same immutable
representation and exact ordered placement, a prior challenged build may be retained;
otherwise preparation starts a distinct attempt and cannot borrow its checkpoint.

Cold source-object materialization is also attempt-local and resumable. Each preparation
uses one deterministic private source checkpoint rather than a new scratch directory on
every restart. A restart rereads the immutable transfer bundle and recomputes every
expected chunk digest; an already materialized object is reused only when its type, size,
and full content digest match. Corrupt, permission-unsafe, symlinked, or multiply
ambiguous legacy source checkpoints fail closed. Source objects remain temporary input
to ordinary A2 acquisition and are removed only after every assignment has a terminal
verified promotion. Reusing these verified bytes is recovery work, not a cold or warm
receipt and not qualification evidence.

Retaining that build is an explicit recovery operation, not an implicit lease rewrite.
The product records a separate private, fsynced recovery-authorization document that
binds the prior checkpoint and frozen build authority to the newly derived feasibility
and its authorization start. The original build authorization remains immutable. The
recovery path revalidates every challenged-build digest, requires the same owner
decision, source artifact, quantizer, representation, ordered layer ranges, backends,
files, bytes, and node identities, then resets execution to the challenged boundary so
seed binding, acquisition, staging, and publication run again. A changed feasibility
digest is permitted only through this exact-match recovery record; any placement or
representation drift fails closed and cannot trigger reuse.

Assignment acquisition authority remains time-bounded but is sized for the verified
aggregate assignment bytes and configured transfer rate, including source hashing,
object materialization, transfer verification, and promotion. It is clamped between
15 minutes and six hours. This prevents a valid large cold acquisition from expiring
merely because removable storage is slower than the model planner, without creating an
unbounded grant; recipient generation, assignment, representation, and allowed chunk
digests remain closed for the entire interval.

### 4.2 Exact warm reacquisition

After the first cold preparation, the product exposes a separate warm-reacquisition
operation for an explicitly named published candidate. It accepts the same closed owner
representation decision and derives fresh capacity authority through the ordinary model
operation. It then requires an exact match of model, revision, source digest, quantizer,
serving representation, owner decision, ordered stage ranges, backends, files, and bytes
against the candidate's challenged immutable build. The candidate's original artifact
manifest and acquisition binding remain unchanged so content-addressed cache identity is
not rewritten merely to manufacture a new receipt; the fresh authority is an additional
eligibility gate, never a replacement for the candidate's build authority.

Warm reacquisition rebinds current seed offers and invokes the ordinary local and remote
assignment acquisition paths. Success requires one new terminal receipt for every exact
stage with cached verified bytes equal to total bytes, zero transferred bytes, zero origin
bytes, zero peer bytes, and the same promotion digest as the verified cache object. File
presence, an earlier receipt, a build report, or a challenge checkpoint alone cannot
satisfy this operation. Failure publishes no replacement candidate, starts no activation,
does not change selection, and leaves the qualified incumbent usable.

## 5. Independent parity and runtime correctness

Before physical promotion, one non-participating reference process evaluates a frozen
prompt corpus using the exact local source checkpoint and tokenizer. The distributed
candidate uses the approved serving representation. Evidence binds prompt-token
digests, source and serving identities, implementation/runtime builds, logits or
declared comparison summaries, greedy tokens, tolerance, and terminal cleanup.

Promotion requires:

1. exact greedy-token parity for every frozen prompt;
2. finite logits within the representation's predeclared numeric tolerance;
3. every stage loads only its assignment-owned tensors and role components;
4. prefill and multi-token decode advance every serving placement;
5. one-token decode payloads and nonzero assignment-local stage KV while active;
6. zero active KV states/bytes after normal completion, cancellation, and shutdown;
7. positive Router frame and applied-operation deltas on every serving peer; and
8. no fatal route state, hidden origin fetch, or unqualified fallback.

Reference execution is correctness evidence only. It never becomes a hidden serving
stage or substitutes for a distributed result.

The bounded-memory startup challenge must therefore contain at least two expected
tokens: one produced by prefill and at least one produced by decode. Its builder may
load only one stage at a time, but it must replay the ordered stage sequence for every
challenge token rather than publishing a prefill-only plan with zero decode steps.

## 6. Memory-tier allocation A/B

The A3 planner gate includes a controlled memory-only A/B using the same model,
representation, workload, signed topology, compute coefficients, and directed links.
Only one peer's fast allocatable memory tier changes across the candidate snapshot.

The executed artifact records both snapshot digests, the exact changed input, explored
allocations, rejected intervals and reasons, selected ranges, estimated stage and cycle
cost, and whether the allocation changed. A changed allocation must be traceable to the
memory constraint. If the allocation remains stable, the artifact must prove that the
same plan remains the unique minimum or that every alternative is dominated; a written
assertion alone cannot satisfy the gate.

## 7. Physical positive product gate

For Qwen2.5-7B, after explicit representation approval and fresh feasibility:

1. prepare assignment-local stage packs through the ordinary product operation;
2. verify cold acquisition and exact warm reuse under A2 authority;
3. activate the prepared physical route without stopping the qualified incumbent;
4. run startup challenge, independent parity, prefill/decode, stage-local KV, and
   terminal cleanup checks;
5. register the candidate only after the qualifier accepts the same-run evidence;
6. explicitly select it for future requests; and
7. submit a browser prompt and retain streamed output plus before/after per-peer
   counters and request history.

Then switch back to the 0.5B deployment and complete another request. This proves
selection is dynamic and future-request-scoped. A3 does not require simultaneous model
residency when safe unload/reactivation preserves immutable prepared candidates.

Qwen3-8B is independently evaluated against the same gate. If its approved existing
representation is infeasible, the correct A3 result is a current, visible blocker with
zero provisioning. A quantized Qwen3 representation is outside authority until it
receives its own explicit owner decision.

## 8. Physical negative gate

The product must reject before selection:

- stale membership, capability, link, feasibility, or qualification evidence;
- incomplete local files or mixed revisions;
- model, representation, tokenizer, manifest, graph, assignment, or load-proof drift;
- insufficient memory tier, disk, runtime reserve, KV budget, or directed connectivity;
- unsupported architecture/backend/quantization/decode-mode combinations;
- conversion without exact owner authorization;
- parity or stage-local-KV cleanup failure;
- a dead peer, fatal route, stalled counter, or failed startup challenge; and
- any attempt to select a compatible, feasible, prepared, loaded, or historically
  qualified deployment that is not currently qualifier-approved.

Every rejection keeps the incumbent route usable and appears with the same bounded
reason across the product authority and UI.

## 9. UI verification

All eight workspaces consume current backend authority and use product language:

- **Inference:** only current qualified deployments in the selector; exact model,
  revision, representation, context/concurrency envelope, streaming activity, output,
  and history binding.
- **Device Lab:** current eligible device classes and the evidence needed before a host
  may contribute to the larger-model route.
- **Network:** proposed, selected, and observed ordered topology with directed costs and
  physical frame movement.
- **Nodes:** assigned layer intervals, backend, resident/static/KV bytes, artifact and
  load state, without private paths.
- **Plans:** memory-tier A/B, contiguous allocation, representation authority, transfer
  scope, and rejected alternatives.
- **Readiness:** local completeness, authorization, feasibility, parity, load,
  qualification, KV cleanup, and selector eligibility as separate rungs.
- **Incidents:** preparation, capacity, parity, load, peer, cleanup, or activation
  failures with incumbent preservation.
- **Settings:** dynamic local catalogue and qualified future-request default; no fixed
  models, peers, or topology.

Refresh, section navigation, Back/Forward, backend reconnect, and a clean second browser
session must reconstruct live lifecycle state. Tab-private prompts/responses remain
isolated between sessions. No internal gate or milestone label appears in product copy.

## 10. Executed artifact and completion

`mycelium.a3_useful_model_qualification.v1` is an owner-private canonical artifact that
binds the chosen 7B representation decision; fresh evidence generation and topology;
memory-tier A/B; preparation/acquisition records; reference parity; physical route,
stage, KV, and counter evidence; browser request/output; selector switch-back; negative
results; and privacy-reduced UI verification. Its public projection exposes digests,
states, bounded measurements, and reasons only.

A3 is complete only when:

1. the exact local Qwen2.5-7B representation is explicitly owner-approved;
2. the memory-tier A/B is executed and traceable;
3. independent parity and all physical runtime checks pass;
4. the 7B deployment is selectable and completes real browser inference;
5. the 0.5B incumbent survives failure cases and can be selected again;
6. Qwen3-8B is truthfully qualified or truthfully current-blocked without unauthorized
   conversion or provisioning;
7. all eight UI workspaces and persistence/reconnect gates pass;
8. focused and full regressions plus governance/security audits pass; and
9. one atomic A3 commit contains the spec, implementation, tests, and claim-ledger
   update.
