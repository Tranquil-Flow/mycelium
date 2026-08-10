# Mycelium Astra Architecture Product Design

**Status:** Successor specification. Implementation begins only after the active
M7–M11 plan is complete, sealed, and accepted. This document does not widen any
current milestone.

**Goal:** Complete the architecture recorded across Astra's synthesis, planner,
Router, recovery, membership, and Network Observatory plans as one testable
product. Every architectural capability must produce executable evidence and a
truthful frontend projection; no feature is complete when it exists only as a
library, simulation, fixture, or prose claim.

## 1. Authority and source reconciliation

This specification reconciles, in descending authority:

1. executing versioned code and passing tests;
2. sealed physical evidence from one coherent run;
3. frozen wire contracts and fixtures;
4. the active M7–M11 live MVP plan;
5. `docs/handover/mycelium-demo-plan.md`, whose architecture was checked
   line-by-line against the earlier synthesis;
6. the private 2026-07-17 DDAI MVP synthesis plan;
7. `LAYER_PLANNER_PRODUCT_V1.md`, `ALLOCATOR.md`,
   `REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md`,
   `FAULT_TOLERANT_LAYER_REPLANNER.md`, `GOSSIP_PROTOCOL.md`, and the
   membership/router design notes;
8. the private 2026-07-15 Network Observatory plan and the 2026-07-19 Product UI
   master plan preserved in repository history.

The exact historical file named `MYCELIUM_MVP_SYNTHESIS_HANDOVER.md` is no
longer present on either current host. Its architectural content survives in the
sources above. Narrative text never overrides executable contracts.

## 2. Completion rule

Each successor milestone is a vertical product slice with all of these gates:

1. a focused design spec freezes ownership, contracts, claim boundary, and
   failure semantics before production edits;
2. contract and adversarial tests fail first;
3. deterministic local integration proves semantics without physical claims;
4. physical or multi-process evidence proves the capability on the path where it
   is claimed;
5. the sole qualification authority accepts one coherent evidence bundle;
6. the product gateway exposes a privacy-reduced, versioned projection;
7. the relevant frontend workspace renders observed, modeled, stale, unavailable,
   and failed states distinctly;
8. browser navigation, refresh, reconnect, accessibility, and privacy tests pass;
9. a negative demonstration proves the UI revokes or withholds the claim when the
   capability fails.

A backend module, green unit test, modeled plan, or fixture UI is useful progress,
but none alone closes a product milestone.

## 3. Target architecture

```text
signed membership + capability + load + directed-link evidence
                              |
                              v
                         GossipService
                              |
                              v
                     PlannerInputAdapter
                              |
             +----------------+----------------+
             |                                 |
             v                                 v
   directed cycle/order search       contiguous layer-allocation DP
             |                                 |
             +----------------+----------------+
                              |
          phase/workload/replication/speculation objectives
                              |
                              v
                         RoutePlanV2
                              |
                              v
 assignment-scoped stage packs -> load proofs -> Layer Builder
                              |
                              v
                       ExecutionGraphV1
                              |
                              v
 request admission -> progressive prefill -> locked PathManifest
                              |
                              v
 stage-local KV -> Router scheduling -> persistent authenticated Iroh
                              |
                              v
 tokens + timing + lifecycle + recovery evidence
                              |
                              v
 qualifier -> product gateway -> Inference / Network / Nodes / Plans /
                 Readiness / Incidents / Device Lab / Settings
```

## 4. Locked architectural capabilities

### 4.1 Evidence-driven planning

- Physical placement is produced from one atomic, signed, freshness-bounded
  evidence snapshot, never from operator-typed layer ranges.
- The production chain is
  `GossipService -> planner_snapshot_from_evidence_bundle -> plan_snapshot ->
  RoutePlanV2 -> compile_bound_layer_assignments -> build_execution_graph`.
- `placement_provenance=planner_v2` is mandatory for every capacity-aware claim.
- Stale, mixed-generation, unsigned, ineligible, or incomplete evidence fails
  closed. Planner intent never implies artifacts loaded or route ready.

### 4.2 Capability-aware contiguous layer allocation

- Layer splitting uses the existing dynamic-programming allocator over legal
  contiguous, non-empty half-open layer ranges.
- Memory feasibility includes weights, activations, bounded KV, workspace, and
  runtime headroom. Compute input distinguishes prefill and decode.
- Entry owns embeddings; final owns final normalization and LM head; intermediate
  stages own only their decoder range.
- A physical A/B test must change measured capacity and demonstrate a traceable
  allocation change. Identical assignments do not prove capability awareness.

### 4.3 Directed cyclic topology

- One model pass is an acyclic ordered stage graph. Autoregressive decode adds one
  explicit final-to-entry token loopback.
- Device order is a directed Hamiltonian-cycle problem, not ordinary shortest-path
  routing. The closure carries a token envelope while forward edges carry
  activations.
- Exact enumeration, Held–Karp DP, and bounded deterministic heuristics retain
  honest optimality/provenance labels by fleet size.
- Production selection uses measured directed link state, including asymmetric
  latency, jitter, loss, and goodput. Synthetic matrices cannot qualify a live
  cycle-search claim.

### 4.4 Phase- and workload-aware objectives

- TTFT/prefill and TPOT/decode remain separate scores and UI measures.
- Workload profiles include prompt/output distributions, concurrency, explicit
  batch shape, interactive versus batch priority, and—when enabled—speculative
  proposal width and acceptance.
- Uncertain workloads produce a scenario matrix, robust selection, or Pareto
  frontier. The UI identifies the winning scenario and assumptions.
- A request remains pinned to one compatible complete track after prefill unless a
  qualified recovery action explicitly changes generations.

### 4.5 Assignment-local artifacts and runtime admission

- Each node receives only tensors and shared assets authorized for its stage. The
  complete checkpoint is not copied to every peer.
- Model, revision, manifest, quantization, tensor names/shapes/dtypes/digests,
  component roles, backend, layer range, and stage signature bind assignment,
  stage pack, load proof, graph, and qualification.
- Runtime admission accounts for memory pressure, RSS, swap, queue capacity,
  context length, concurrency, KV budget, and power/thermal constraints where the
  device exposes them.
- A node may be a member without being eligible for activation placement.

### 4.6 Progressive routing and immutable execution

- Prefill may select placements progressively from current load and link state,
  with tentative request-scoped KV/capacity reservations.
- Final selection emits one immutable, generation-bound `PathManifest`; decode
  validates path attempt, hop sequence, idempotency, and assignment binding.
- Structural topology remains pinned for a path attempt while bounded dynamic load
  data may be refreshed during prefill decisions.
- Cancellation, timeout, failure, and completion release reservations, queues, and
  KV on every participant.

### 4.7 Batching, scheduling, and backpressure

- Interactive and batch QoS classes use explicit priority and aging.
- Queue, frame, stream, reservation, and runtime-batch bounds are enforced end to
  end; slow consumers do not create unbounded memory growth.
- Microbatch size is an observed runtime contract, never inferred from concurrent
  request count.
- Pipeline microbatch overlap and continuous batching are claimed only after trace
  evidence demonstrates them.

### 4.8 Data-parallel stage replication

- “Data parallel” means replicas of the same contiguous stage range serving
  different requests. It is not tensor parallelism and does not split one request's
  matrix operation across devices.
- Replicas are added iteratively where they improve the robust bottleneck objective;
  flow assignment must use a complete legal track and preserve route-local KV.
- Replica groups include failure-domain and directed-edge compatibility. Zero-flow
  replicas are removed.
- Full-model data parallelism and tensor parallelism require separate future specs;
  neither is silently claimed by stage replication.

### 4.9 Speculative decode overlay

- Speculative decoding is optional and off by default.
- The plan binds draft/target model identities, proposal width, acceptance model,
  verification batch cost, rollback semantics, KV compatibility, and target-only
  fallback.
- Promotion requires observed end-to-end gain over target-only decode, not draft
  speed alone. Rejection or draft loss falls back without corrupting target KV.

### 4.10 KV ownership and fault tolerance

- Normal decode keeps KV local to each stage; KV does not traverse the ordinary
  activation path.
- Cache identity binds request, deployment, path/generation, layer range,
  position/sequence, backend, layout, and model identity.
- Recovery supports two truthful modes: fenced compatible successor standby with
  replicated watermarks, or replay of the original prompt plus committed tokens on
  a newly qualified path.
- No continuity claim survives missing or incompatible KV. Stale generations and
  old peer incarnations are rejected.

### 4.11 Deterministic scoped replanning

- Failure assessment distinguishes request, hop/edge, placement, peer, and
  deployment scope.
- Surviving legal tracks are retained; the entire swarm is not rebuilt for a
  replica-only or isolated directed-edge loss.
- Join and capacity drift produce candidate intent subject to deterministic
  hysteresis. Provisioning, load proof, epoch fencing, and qualification precede
  activation.
- Stable drain, active recovery, and circuit break remain distinct outcomes.

### 4.12 One signed heterogeneous membership plane

- Mac, Linux, mobile, and browser peers share one durable signed membership,
  invitation, lease, generation, revocation, and capacity-evidence plane.
- Peer class and runtime/transport capability decide placement eligibility; browser
  membership or a synthetic browser job never becomes model-stage evidence.
- Transport authorization uses signed deployment/epoch-scoped EndpointID records,
  cryptographically bound to node identity, process/incarnation, and evidence signer.
- Joining is invitation-gated and trusted-peer for this product generation. An
  external tester boundary must describe weight/activation visibility and evidence
  trust before non-operator devices join.

### 4.13 Traffic-aware liveness

- Valid activation or application receipts are passive liveness and suppress
  redundant active-period heartbeats.
- Idle keepalive detects quiet peer loss only after its bounded interval and evidence
  freshness expires.
- One missing receipt means a scoped edge/hop timeout, not whole-peer death.
- Active transport failure and idle liveness-stale are separate evidence classes and
  separate UI incidents.

### 4.14 Authenticated direct/relay transport

- Native Iroh remains the activation data plane; HTTP is the product/control plane.
- Every connection records selected path class (`direct`, `relay`, or `unknown`),
  relay identity/region when applicable, endpoint identities, timestamps, cold/warm
  RTT, goodput, jitter/loss, reconnect behavior, and receiver-observed timing.
- Connections are persistent and reused across prefill/decode. Opening one connection
  per message fails the production transport gate.
- Tailscale and SSH may bootstrap or stage operator-controlled hosts, but are never
  silent activation, health, or data-plane fallbacks.

### 4.15 Privacy, authority, and qualification

- Observatory remains read-only. Inference and device actions retain separate
  explicitly authorized gateway paths.
- Browser code receives no upstream bearer credential, raw endpoint, private address,
  path, activation, tensor, KV content, raw token array, or prompt/output telemetry.
- Prompt/output may exist in the Inference workspace's bounded tab session history,
  but never in Observatory metrics or default evidence.
- The qualifier is the sole route-readiness authority. All positive claims bind one
  run, deployment, model, plan, assignment, graph, request, and evidence generation.

## 5. Product UI contract

The eight stable workspaces form one product, not eight independent demos.

| Workspace | Final live responsibilities |
| --- | --- |
| Inference | Qualified deployment/model choice; prompt/stream/resume/cancel; waiting and token progress; TTFT/TPOT/throughput; batch/QoS attribution; immutable history binding; recovery/speculation state without exposing internal payloads. |
| Network | Separate physical and execution layers; pipeline, ring, SCC, elastic-geo and true-map layouts; directed measured links; prefill/decode/closure/alternative/replica tracks; real trace overlay; stable positions; inspectors and exact formulas. |
| Nodes | Durable membership, peer class, trust/generation/freshness, backend/precision, memory/storage/power, runtime eligibility, assigned ranges, replicas, artifact/load state, reachability and selected direct/relay path. |
| Plans | Current and historical `planner_v2` plans; capability inputs; DP allocation; cycle strategy and optimality; TTFT/TPOT/workload scenarios; pruning; replica flow; speculation decision; A/B comparison and assumptions. |
| Readiness | Independent proof matrix from discovery through serving; exact bindings and provenance; qualification freshness; missing/failed proof reasons; evidence history/diff/export. |
| Incidents | Active edge failure, idle stale peer, reservation/backpressure, cancellation, stable drain, active recovery, circuit break, replanning, KV outcome, and route-generation timeline. |
| Device Lab | Signed bounded onboarding for native and browser/mobile workers; stage parity, runtime, thermal, battery, lifecycle, and network-loss qualification; synthetic probes visibly distinct from inference. |
| Settings | Safe generation/QoS defaults, qualified model preference, workload policy, privacy/retention, relay/bootstrap policy, source/contract versions, redacted diagnostics, accessibility and motion settings. |

### UI truth rules

- `fixture`, `modeled`, `measured`, `qualified`, `stale`, `disconnected`,
  `failed`, `unknown`, and `not applicable` are visually and semantically distinct.
- Missing values are never coerced to zero, healthy, offline, or ready.
- Planned placement is not assigned; assigned is not provisioned; provisioned is not
  loaded; loaded is not challenged; challenged is not serving.
- Animation is off by default. Modeled animation is labelled. A real traffic overlay
  requires an authenticated trace contract.
- Physical and logical edges, prefill/decode/closure, primary/alternative/replica, and
  old/new generations remain independently filterable.
- Direct navigation, Back/Forward, section switching, refresh, reconnect, and replay
  preserve or truthfully restore state for every workspace.

## 6. Successor milestone sequence

The implementation plan elaborates these gates:

- **M12 — Evidence spine:** one versioned, privacy-reduced live product snapshot and
  event stream covers all architectural entities and drives the rich UI.
- **M13 — Planner placement:** signed evidence drives physical capability-aware DP
  allocation and assignment-local deployment.
- **M14 — Directed topology:** measured directed links drive cycle selection and an
  opened physical decode loop.
- **M15 — Workload intelligence:** separate prefill/decode and robust scenario
  objectives produce explainable A/B plans.
- **M16 — Runtime control:** resource admission, progressive prefill, immutable paths,
  bounded batching, QoS, and backpressure work physically.
- **M17 — Replicated throughput:** data-parallel stage replicas and legal flow tracks
  improve measured multi-request capacity.
- **M18 — Recovery:** traffic-aware liveness, scoped replanning, KV standby/replay,
  drain/recovery/circuit-break outcomes work and are replayable.
- **M19 — Speculative decode:** an optional measured draft/target overlay improves a
  qualified workload or remains honestly disabled.
- **M20 — Internet-native heterogeneous swarm:** unified signed membership,
  EndpointID trust, direct/relay observability, and eligible native peer classes work
  without Tailscale as a product dependency.
- **M21 — Full product closure:** all rich UI behavior, accessibility, performance,
  privacy/security, physical qualification, cold bootstrap, and operator runbooks pass
  as one release gate.

## 7. Final definition of done

The Astra architecture program is complete only when:

1. every capability in Section 4 is either physically qualified and visible in the
   product or explicitly rejected by a separately approved scope decision;
2. every workspace in Section 5 is complete in fixture, replay, and live modes;
3. accepted capacity-aware claims trace to sealed `planner_v2` evidence;
4. accepted topology claims trace to measured directed links and real Router frames;
5. accepted performance claims distinguish TTFT, TPOT, throughput, workload, and
   topology;
6. accepted resilience claims include both positive recovery and negative
   fail-closed evidence;
7. all browser, Python, Rust, contract, privacy, security, accessibility,
   performance, and physical gates pass from a clean environment;
8. no Tailscale requirement, fixture substitution, full-model fallback, hidden
   tensor load, synthetic transport, or UI-invented readiness participates in the
   accepted release.
