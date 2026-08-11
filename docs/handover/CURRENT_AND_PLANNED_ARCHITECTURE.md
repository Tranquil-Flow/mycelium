# Mycelium current and planned architecture

**Status:** Canonical review entry point, 2026-08-10. This document supersedes the
operational status in `ASTRA_CURRENT.md`; the older file remains historical
provenance. M15 is the current implemented tranche; M16 is next.

## Read this first

Review these sources in order:

1. this document;
2. `docs/superpowers/specs/2026-08-09-mycelium-live-mvp-design.md` for the bounded
   M0-M11 product contract;
3. `docs/live-mvp-operator-runbook.md` for current operation and limitations;
4. `docs/superpowers/specs/2026-08-09-mycelium-astra-architecture-product-design.md`
   for the target architecture;
5. `docs/superpowers/plans/2026-08-09-mycelium-post-m11-astra-architecture.md` for
   M12-M21 execution order;
6. `docs/reviews/2026-08-10-external-review-status.md` for independent findings and
   their disposition.

Repository baseline: branch `integration/wave8-two-device-g4`. Milestone commits are
`e8654cb` (M0-M11), `27a3478` (M12), `d6249c1` (M13), and `2ff804d` (M14); M15 is
recorded by `docs/handover/M15_PROGRESS_2026-08-10.md`. The two implementation plans
remain intentionally untracked pending review. Never infer a source revision from an
evidence timestamp alone.

For Astra: review the staged working-tree diff in addition to the recorded HEAD.
Private evidence paths in this document are host-local and may not resolve on another
machine; verify the recorded digests or request an explicit evidence bundle rather than
assuming absence. The complete physical/MLX gate must run on a compatible macOS host.
The authoritative remaining release conditions are the numbered conditions in the
external-review status document.

## As-built request path

```text
React/Vite Inference workspace
        |
        | same-origin HTTP + SSE
        v
product gateway -> request gateway
                    |  RouterPort / PromptCodec / QualificationSource seams
                    v
             LiveDeploymentRegistry
                    |
                    v
               LiveRouterPort
                    |
                    v
             PhysicalLiveRoute
                    |
        persistent node command sessions
                    |
       native Iroh activation transport
                    |
       assignment-bound stage runtimes
```

The request gateway owns admission, bounded SSE streaming, session cancellation,
and terminal lifecycle. The registry atomically selects one already-qualified
deployment and binds each admitted request to that immutable deployment. The live
adapter drives one physical route thread per admitted request. The physical route
owns node processes, endpoint challenges, stage execution, transport counters,
stage-local KV cleanup, and fail-closed route state.

Browser cancellation now sets a request-local cancellation signal. The physical
route observes it between bounded node commands, sends `infer_cancel`, verifies
every peer's cleanup state, and returns `CANCELLED`. Adapter and physical prompt/
output token state is released after terminal lifecycle. A transport command that
is already blocked cannot yet be interrupted before its command timeout; M18 adds
traffic-aware liveness and scoped recovery.

## Current deployments and topology

The active registry contains two independent, qualified two-stage deployments:

| Model | Quantization | Stage 0 | Stage 1 | Purpose |
| --- | --- | --- | --- | --- |
| `Qwen/Qwen2.5-0.5B-Instruct` | int8 weight-only | MLX `[0,12)` | MLX `[12,24)` | baseline and failover |
| `Qwen/Qwen2.5-1.5B-Instruct` | int8 weight-only | MLX `[0,14)` | MLX `[14,28)` | selected quality candidate |

M7 separately proved a three-host 0.5B topology: M4 Pro/MLX `[0,8)`, Evi
MacBook Pro/MLX `[8,16)`, and Surface/NumPy `[16,24)`. That proof is not the same
deployment as either currently registered two-host route. Do not blend their host,
performance, or qualification claims.

The serving path is contiguous pipeline/model parallelism. Embeddings belong to the
entry stage; final normalization and LM head belong to the final stage. Every stage
pack contains only the stage's authorized tensors plus shared tokenizer/config
assets. Normal decode uses request-, deployment-, generation-, and layer-bound
stage-local KV; the ordinary activation path does not transfer KV.

The current product does **not** claim tensor parallelism, data-parallel replicas,
pipeline microbatch overlap, continuous batching, speculative decode, request-time
layer reallocation, continuous topology re-optimization, or in-flight KV migration.

## Current planning, trust, and network authority

M13 made one signed, source-bound Gossip evidence bundle authoritative for placement.
M14 now adds a complete activation-plane matrix across three physical hosts. Each of
the six directed edges carried four authenticated Router frames over one persistent
Iroh connection. Exact cycle search selected `node-0 -> node-2 -> node-1 -> node-0`
at 42.104 ms instead of the canonical cycle at 384.554 ms under the documented warm
RTT/2 plus jitter objective. The chosen cycle is opened into a forward pipeline plus
explicit final-to-entry decode loopback before the contiguous allocation DP runs.
The resulting mixed MLX/NumPy/MLX allocation is 1/1/22 layers and uses
`complete_context_replay`; homogeneous MLX `stage_local_kv` remains separately
qualified. Continuous topology re-optimization, replication, and scoped replanning
remain later milestones.

M15 evaluates `balanced`, `prefill_ttft`, and `decode_tpot` contiguous-allocation
objectives against the same frozen M13/M14 evidence for content-free interactive and
sustained/batch workload profiles. Deterministic minimax normalized regret selects a
robust winner and preserves the Pareto frontier and signed deltas. The current
physical capability matrix happens to produce the same 1/1/22 allocation under all
three objectives; a heterogeneous pure-planner case proves the objectives can produce
different allocations without changing topology or evidence. Two sequential browser
requests are bound to exact-shape predictions and physical TTFT, TPOT, goodput,
placement, runtime, topology, and frame counters. The first-generation model errors
are large and displayed as such; they pass only the explicitly frozen broad M15
calibration bound and are not relabelled as accurate predictions. Admission,
concurrency, queueing, and batch-shape execution remain M16.

Peers are trusted operator-controlled devices. SSH and Tailscale currently provide
staging and reachability; activation traffic uses native Iroh. Tailscale is not a
protocol requirement, but it is presently an operational dependency for peers on
different LANs.

The live supervisor now loads the durable seed signer and database selected by the
operator plan, verifies the authority generation and key digest, and signs live-clock
assignment offers with that authority. Authority rotation to generation 2 and signed
acknowledgement were physically exercised. Native nodes retain owner-only restart
credentials and can sign a resume request after seed/node restart without reusing an
invite; revoked, stale, changed-key, changed-endpoint, changed-capability, replay-
conflicting, and cross-swarm resumes fail closed. Heterogeneous convergence and
internet-native direct/relay operation remain M20 scope.

The current tranche adds a safe operator bridge for more trusted devices without
claiming M20 complete. One live durable `SeedCoordinator` can be pinned to its stored
signer and issue up to 64 unique, short-lived, single-use, owner-only native invite
files per batch. An executable eight-device test proves independent durable keys,
nonces, HTTP joins, generations, and members. Enrollment is intentionally distinct
from route activation: every new member remains not ready until capability, placement,
artifact load, runtime, and a rebuilt physical topology are separately qualified.
The external-tester contract and operator procedure are in
`docs/contracts/external-tester-boundary.md` and
`docs/swarm-multi-device-onboarding.md`.

## As-built capability matrix against the Astra specification

`qualified` means physical evidence exists for only the boundary stated. It does not
promote the rest of a composite capability.

| Astra capability | State | Exact current boundary |
| --- | --- | --- |
| 4.1 Evidence-driven planning | `qualified` | One signed, source-bound M13 bundle coherently supplies membership, status, runtime/decode mode, memory tiers, and required directed edges to the selected live candidate; stale or mismatched evidence fails closed. |
| 4.2 Capability-aware contiguous allocation | `qualified` | Deterministic contiguous DP selected the M13 two-Mac 15/9 split and the M14 measured-order three-host 1/1/22 split. Compute-only and fast-memory-only physical A/B inputs produced distinct allocations with `planner_v2` provenance. |
| 4.3 Directed cyclic topology | `qualified` | A complete six-edge activation-plane matrix across three physical hosts drove exact cycle selection; the winning cycle differs from canonical order and is opened into forward edges plus explicit decode loopback. |
| 4.4 Phase/workload objectives | `qualified` | M15 compares three phase objectives over two content-free workload profiles on one frozen evidence snapshot, exposes robust/Pareto selection, and binds sequential physical observations to exact request-shape predictions and frozen budgets. Concurrent/batched execution remains M16. |
| 4.5 Assignment-local artifacts | `partially_qualified` | M13 materializes and proves only each host's assigned shard plus shared static assets, with assignment-bound load proofs. Cache eviction, corruption recovery, concurrent staging, and full runtime memory/thermal admission are not qualified. |
| 4.6 Progressive routing and immutable execution | `qualified` | M16 reserves every placement before dispatch, projects the progressive candidate, commits an immutable graph/topology-bound `PathManifest`, and releases all request resources on every terminal path. |
| 4.7 Batching/scheduling/backpressure | `partially_qualified` | M16 physically qualifies bounded concurrent admission, interactive/batch QoS with aging, sequential dispatch, queue bounds, cancellation, and cleanup. Physical runtime batching, continuous batching, and pipeline overlap remain explicitly unclaimed. |
| 4.8 Data-parallel stage replication | `implemented_unintegrated` | Planner/flow concepts exist; no live replica group or multi-track physical throughput proof exists. |
| 4.9 Speculative decode | `design_only` | No promoted draft/target runtime path. |
| 4.10 KV ownership/fault tolerance | `partially_qualified` | Stage-local KV ownership and cleanup are physically observed on homogeneous MLX routes. Any NumPy or Pixel placement downgrades the route to complete-context replay; standby/replay recovery is not implemented. |
| 4.11 Deterministic scoped replanning | `implemented_unintegrated` | Replan types/logic exist; current failure rebuilds or switches a complete deployment. |
| 4.12 Signed heterogeneous membership | `partially_qualified` | Durable signed membership now binds two MLX Macs and one `linux_numpy_iroh` host as fresh activation-eligible members. Device Lab/mobile and different-network heterogeneous activation remain separate M20 work. |
| 4.13 Traffic-aware liveness | `implemented_unintegrated` | General liveness code exists, but the live route can remain blocked until node command timeout after peer loss. |
| 4.14 Authenticated direct/relay transport | `partially_qualified` | Native authenticated Iroh publishes path class, relay metadata when present, cold/warm RTT, goodput, jitter/loss, reconnects, generations, freshness, and connection reuse. M14 physically observed six direct edges; it makes no relay-path claim. |
| 4.15 Privacy/authority/qualification | `qualified` | One privacy-reduced product snapshot/event spine drives browser-safe Nodes, Readiness, and Incidents projections; qualifier-gated admission, durable authority generation, and refresh persistence are physically exercised in the two-Mac M12 boundary. |

## Product UI truth boundary

The shell has eight stable workspaces: Inference, Device Lab, Network, Nodes, Plans,
Readiness, Incidents, and Settings. Inference uses the product/request gateway.
The product snapshot/event spine is the shared privacy-reduced source for Nodes,
Readiness, and Incidents, while bounded same-origin live route/deployment endpoints
remain action/status adapters for Inference. M13 placement, M14 topology, and M15
workload/calibration projections give Plans, Network, Nodes, and Readiness the same
planner inputs, measured directed links, candidate cycles, selected order, allocation,
connection reuse, evidence digests, workload assumptions, policy frontier, and
modeled-versus-observed errors after refresh. Inference records workload/QoS/policy
attribution in tab-session history. Settings exposes only the profiles in the current
M15 projection as defaults for future requests and explicitly withholds M16
queueing/batching claims.

Prompt and response text may persist only in bounded browser tab-session history.
Observatory/status projections must not contain prompts, decoded output, token arrays,
credentials, activations, tensors, KV content, private paths, or private addresses.
The sealed Pixel 8 proof is a real Device Lab browser worker running a labelled
deterministic numerical fixture and remains `activation_eligible=false`. Separately,
the source tree contains a Router-native Android stdlib stage runtime (currently named
`pixel-stdlib` in code for compatibility). That runtime is a device-vendor-neutral
experimental activation candidate, not a qualified mobile deployment; membership,
transport, stage parity, thermal, battery, lifecycle, and network-loss evidence still
gate promotion. The Pixel 8 is only the first physical conformance device.

## Claim and evidence ledger

Private physical evidence remains outside the repository. Paths below are operator
references, not public release artifacts.

| Milestone | Current assessment | Primary evidence | Remaining closure |
| --- | --- | --- | --- |
| M7 | Physical three-host route demonstrated | `/Users/evinova-self/mycelium-physical-run/m7-qwen-three-host-surface-topology.json`; M7 operator plan/build report | Seal prompt/counter/latency/browser proof into a manifest tied to the staged source |
| M8 | Stage-local KV, timing, browser completion, and browser cancellation observed on both physical stages | `/Users/evinova-self/mycelium-physical-run/m8-qwen-two-host-mlx-topology.json`; owner-only `m8-qwen2.5-int8-two-host-mlx-v4/review-live-status-20260810.json` (`sha256:9bb18fe80465503dadd3752c3d072a827881dc3fcc8027284f88d7b6ad2b1429`) | Bind the fresh proof and staged source state into the coherent release manifest |
| M9 | Stage-sharded 1.5B route and three model-answer checks demonstrated | `/Users/evinova-self/mycelium-physical-run/m9-qwen15-two-host-mlx-topology.json`; v4 `quality-gate.json` | Label the fourth case as gateway-policy refusal; seal baseline comparison/counters |
| M10 | Registry persistence, atomic selection, history attribution, and failover paths implemented | `/Users/evinova-self/mycelium-physical-run/m10-deployment-registry.json`; registry tests | Seal one failover/restart transcript and source binding |
| M11 | Pixel Device Lab proof, eight-section browser checks, and fresh stable-peer inference/cancellation demonstrated | `/Users/evinova-self/mycelium-physical-run/m11-device-lab/pixel8-physical-proof.json`; M8 review status; test/runbook records | Rebuild/reprove current 1.5B availability, decide persistent seed identity, and create the coherent release manifest |
| M12 | Closed for the operator-approved two-Mac scope: durable authority generation 2, signed rotation acknowledgements, invite-free node resume, unified product projections, and post-restart browser inference/history | `/Users/evinova-self/mycelium-physical-run/m12-resume-proof-20260810`; `docs/handover/M12_PROGRESS_2026-08-10.md` | Different-network Mac and Android/Termux remain explicitly deferred conformance work; they are not inference-stage proofs |
| M13 | Signed evidence drove the physically deployed contiguous 1/1/22 allocation | `docs/handover/M13_PROGRESS_2026-08-10.md`; frozen planner snapshot in the M14 run bundle | Runtime resource admission remains M16 |
| M14 | Complete measured directed activation matrix selected a non-canonical three-host cycle and physical loopback | `docs/handover/M14_PROGRESS_2026-08-10.md` | Continuous topology optimization remains later scope |
| M15 | Two workload profiles, three policies, robust/Pareto comparison, exact-shape physical calibration, UI attribution/defaults, and explicit M16 deferrals completed | `docs/handover/M15_PROGRESS_2026-08-10.md`; `/Users/evinova-self/mycelium-physical-run/m14-directed-topology-20260810/m15-calibration-input.json` | Improve model accuracy; peak-memory, energy/thermal, and reconnect are approved exclusions; concurrent admission/batching remain M16 |
| M16 | Three concurrent admissions, complete-path reservations, immutable locked paths, QoS priority/aging, bounded queueing, v2 lifecycle events, cancellation cleanup, and synchronized UI completed | `docs/handover/M16_PROGRESS_2026-08-10.md`; `/Users/evinova-self/mycelium-physical-run/m14-directed-topology-20260810/m16-physical-gate.json` | Runtime reports sequential dispatch; microbatching, continuous batching, and pipeline overlap remain unclaimed |
| M17 | Multi-model inventory, dense Qwen2/Qwen3 adapters, exact-weight feasibility, assignment-local acquisition, and fail-closed model selection completed | M17 model-operation endpoint/UI and catalog/parity suites | Qwen3-8B is adapter-compatible and locally complete but capacity-infeasible on the current swarm |
| M18 | Qualified replica planning/runtime evidence and concurrency attribution completed | M18 replica plan/runtime endpoints and UI | Continuous batching and pipeline overlap remain unclaimed |
| M19 | Traffic-aware liveness, scoped recovery evidence, and fenced recovery paths completed | M19 liveness/recovery endpoints and UI | Cross-backend live KV migration remains unclaimed |
| M20 | Target-authoritative speculative planning/runtime evidence completed | M20 speculative plan/runtime endpoints and UI | Promotion remains workload- and target-parity-bound |
| M21 | Heterogeneous membership and physical MLX/NumPy route evidence completed | M21 heterogeneous endpoint/UI and physical route records | Mobile activation and different-network Mac conformance remain deferred, not silently inferred |
| M22 | Release closure, service packaging, UI audit, local 3B qualification, Qwen3 adapter proof, and reviewer bundle completed | `/Users/evinova-self/mycelium-physical-run/m22-release-20260811/m22-release.json` | Qwen3-8B placement remains capacity-blocked; no public release has occurred |
| M23 | Three-host MLX/MLX/NumPy stage-local KV physically qualified with exact output parity, one-token decode on every stage, terminal cleanup, and measured performance gain | `docs/handover/M23_PROGRESS_2026-08-11.md`; `contracts/compatibility-fixtures/m23-kv-gate-v1.json` | Tensor parallelism, continuous batching, KV migration, and mobile activation remain later scope |

The deterministic credential-theft refusal is a gateway safety policy and performs no
Router admission. The factual, arithmetic, and exact-format cases are distributed
model outputs. Never describe all four as model-quality generations.

## Known operational state and limitations

- The live supervisor is a foreground loopback process, not an installed durable
  service. Seed and node identity/membership survive restart, but route process restart
  still requires the exact staged deployment bundles and startup challenge.
- On 2026-08-10 both routes requalified, then the Evi MacBook Pro went offline during
  the first new request. The product cancellation became terminal immediately, while
  the in-flight physical command remained bounded by the node command timeout. This is
  a concrete example of why M18 traffic-aware liveness and scoped recovery remain open.
- Evi MacBook Pro has a user LaunchAgent at
  `~/Library/LaunchAgents/com.mycelium.keep-awake.plist` which keeps `caffeinate -si`
  active across logins. It protects against ordinary idle sleep but not lid-triggered sleep. Supported closed-lid service
  requires AC power and Apple's clamshell peripherals; the runbook also records the
  explicit, reversible administrator override for an operator-approved unattended run.
- A fatal route error latches fail-closed. Recovery rebuilds and requalifies a complete
  topology; there is no transparent in-flight failover or KV migration.
- The larger model is materially more useful than the 0.5B baseline but remains slow
  over the present two-host route.
- After the final 2026-08-10 rebuild, both deployments passed their startup challenge.
  The first 1.5B browser request then streamed six decoded tokens before a
  `decode_completion_timeout`; that deployment failed closed and the operator selected
  the still-qualified 0.5B route. Treat the larger deployment as unavailable until it
  is rebuilt and a fresh request completes. This is additional physical evidence for
  the M18 traffic-aware liveness/recovery scope, not a successful M9 availability claim.
- Assignment-local remote staging now hashes a bounded stream into an owner-only
  temporary archive and extracts from disk. The 1.79 GB physical restage held the
  helper near 15-18 MB RSS instead of buffering the complete archive and forcing swap.
- M11 evidence is distributed across private run directories and is not yet one sealed
  release manifest. Task 0 of the post-M11 plan is therefore the immediate gate before
  M12 implementation.

## Planned architecture, M12-M23 and next boundaries

```text
signed membership + capability + load + directed-link evidence
                              |
                         GossipService
                              |
                      PlannerInputAdapter
                              |
          measured cycle search + contiguous allocation DP
                              |
                          RoutePlanV2
                              |
       assignment-local stage packs and runtime load proofs
                              |
                      ExecutionGraphV1
                              |
     admission -> progressive prefill -> immutable PathManifest
                              |
  stage-local KV -> bounded scheduling -> persistent authenticated Iroh
                              |
      lifecycle/recovery evidence -> qualifier -> product evidence spine
                              |
                   all eight live UI workspaces
```

| Milestone | Architectural result |
| --- | --- |
| M12 | Durable native membership plus unified privacy-reduced evidence snapshot/event spine and rich live UI foundation |
| M13 | Signed evidence drives capability-aware contiguous allocation DP and assignment-local deployment |
| M14 | Measured directed links drive honest cycle/order selection and physical decode loopback |
| M15 | Separate prefill/decode and workload scenarios drive robust/Pareto plan comparison |
| M16 | Resource admission, progressive prefill, immutable paths, bounded QoS scheduling and backpressure |
| M17 | Qualified multi-model catalog, swarm feasibility rejection, assignment-local acquisition, and user model selection |
| M18 | Data-parallel stage replicas and complete legal request tracks improve measured concurrency |
| M19 | Traffic-aware liveness, scoped replanning, fenced KV standby or truthful replay recovery |
| M20 | Optional speculative decode promoted only with target parity and measured end-to-end gain |
| M21 | Heterogeneous invited participation and authenticated direct/relay operation without Tailscale dependency |
| M22 | Complete planned UI, accessibility/performance/privacy gates, cold bootstrap, and sealed release closure |
| M23 | Architecture-scoped heterogeneous stage-local KV, physical replay A/B, per-placement runtime evidence, and fail-closed mode negotiation |

The next milestone must be specified before implementation. The leading M24 candidate is
continuous batching with honest queue/batch attribution and a physical concurrency A/B. It
must not be conflated with the already-completed M16 admission scheduler. Tensor parallelism,
cross-backend KV migration, and mobile activation eligibility should remain independent
milestones with their own parity, failure, resource, and UI gates.

## Reviewer rules

Treat executing code and fresh tests as stronger than prose. Treat private evidence as
a claim only after verifying its digest, source binding, host/process identities, and
qualification authority. Do not equate a checked plan box, fixture, simulated route,
staged artifact, or startup challenge with a sealed physical product claim. Report
current implementation defects separately from planned M12-M21 capabilities.
