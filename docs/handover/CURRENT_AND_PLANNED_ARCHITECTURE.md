# Mycelium current and planned architecture

**Status:** Canonical review entry point, updated 2026-08-18. This document supersedes
the operational status in `ARCHITECTURE_HANDOVER.md`; the older file remains historical
provenance. Milestone evidence through M23 exists; remaining unchecked gates in the
governing plan are still authoritative.

## Read this first

Review these sources in order:

1. this document;
2. `docs/superpowers/specs/2026-08-09-mycelium-live-mvp-design.md` for the bounded
   M0-M11 product contract;
3. `docs/live-mvp-operator-runbook.md` for current operation and limitations;
4. `docs/superpowers/specs/2026-08-09-mycelium-architecture-product-design.md`
   for the target architecture;
5. `docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md` for
   the current A-stage execution order;
6. `docs/reviews/2026-08-10-external-review-status.md` for independent findings and
   their disposition.

Repository baseline: branch `codex/flexible-swarm-catalog`. The A3 physical feature is
closed by `905786df41ffdad5718d3464733e2f5cb8727532`; the hardened A4-A15 acceptance
baseline follows at `e61e780`. Earlier M-stage commits and evidence remain historical
inputs, not substitutes for the current A-stage gates. Never infer a source revision
from an evidence timestamp alone.

For an external architecture review, inspect the working-tree diff in addition to the recorded HEAD.
Private evidence paths in this document are host-local and may not resolve on another
machine; verify the recorded digests or request an explicit evidence bundle rather than
assuming absence. The complete physical/MLX gate must run on a compatible macOS host.
The authoritative remaining release conditions are the numbered conditions in the
external-review status document.

Current gate checkpoint: A2 and A3 are complete. A3 qualified the exact local
Qwen2.5-7B `int8-weight-only` representation over the M4 Pro/MLX → Surface/NumPy
physical route, proved cold acquisition and exact zero-transfer warm reacquisition,
streamed a real browser answer, exercised fail-closed selector negatives, and restored
the qualified Qwen2.5-0.5B incumbent. The 7B candidate remains qualified standby; the
0.5B route remains active and selected. No download or model substitution occurred.
A4 request-scoped concurrency, cooperative cancellation, liveness, and fencing is the
next primary gate. Current qualification may expire independently without invalidating
the retained, atomically committed A3 completion record.

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
is already blocked cannot yet prove request-scoped interruption and cleanup within
2,000 ms; A4 owns that cooperative cancellation and liveness closure.

## Current deployments and topology

The operator has three independently prepared deployment identities. The live registry
and model selector are dynamic. During the 2026-08-11 A1 browser gate,
the ordinary product URL discovered both initially qualified routes and the prepared
3B candidate without a fixed frontend configuration. The selected 1.5B route then
emitted five real tokens and failed closed on `decode_completion_timeout`; the registry
immediately marked it unavailable. During the 2026-08-11 A2 implementation pass, the
same browser activated and physically qualified the prepared three-host 3B candidate.
The selector then exposed both the qualified 0.5B and 3B deployments, and a browser
request explicitly selecting 3B completed with `Paris`. Its prompt, response, model,
deployment, terminal state, and token count survived refresh. The product reconstructs
all of this state from the backend and does not assume a fixed number of models.

Those successful runs are historical observations, not current health. On 2026-08-12
the live registry retained the 0.5B and 3B identities but marked both unavailable after
their required physical members stopped contributing; 0.5B remained the last selected
identity and the selector truthfully exposed zero qualified choices. The 1.5B identity
remains unavailable after its earlier decode timeout. No row below is permission to
admit new inference without a fresh qualifier binding:

On 2026-08-15 the 0.5B deployment was freshly rebound and qualified as the physical
two-host route M4 Pro `node-0 [0,23)` to Surface `node-2 [23,24)`. The ordinary browser
product path completed new distributed requests with current evidence and retained
their prompt, output, and terminal history after refresh and in an independent session.
A fresh capacity refresh independently selected the same contiguous allocation for a
new `int8-weight-only` replacement candidate. That report is planning evidence with
`locally_derived_candidate` representation authority: it is not owner-approved,
prepared, qualified, or selectable until the explicit conversion decision and all A2
gates complete. The catalogue remains dynamic; only qualified deployments enter the
selector.

| Model | Quantization | Stage 0 | Stage 1 | Purpose |
| --- | --- | --- | --- | --- |
| `Qwen/Qwen2.5-0.5B-Instruct` | int8 weight-only | MLX `[0,23)` | NumPy `[23,24)` | current Lenovo-independent qualified route; replacement acquisition awaits owner approval |
| `Qwen/Qwen2.5-1.5B-Instruct` | int8 weight-only | MLX `[0,14)` | MLX `[14,28)` | unavailable after observed timeout; must requalify |
| `Qwen/Qwen2.5-3B-Instruct` | int8 weight-only | three-stage MLX/MLX/NumPy physical route | — | browser-inference verified historically; currently unavailable |

M7 separately proved a three-host 0.5B topology: M4 Pro/MLX `[0,8)`, Evi
MacBook Pro/MLX `[8,16)`, and Surface/NumPy `[16,24)`. That proof is not the same
deployment as either currently registered two-host route. Do not blend their host,
performance, or qualification claims.

The serving path is contiguous pipeline/model parallelism. Embeddings belong to the
entry stage; final normalization and LM head belong to the final stage. Runtime loading
is logically restricted to assignment-owned tensors. The pre-A2 sharder, however,
placed embedding and final-normalization tensors in one shared static safetensors file,
so existing candidates can physically over-transfer a boundary tensor even though the
loader does not use it. The A2 builder now emits separate embedding and output static
containers and rejects any acquisition file whose tensor set exceeds its assignment;
that correction has software coverage but is not yet the artifact lineage of the
previously active 3B candidate. Normal decode uses request-, deployment-, generation-,
and layer-bound stage-local KV; the ordinary activation path does not transfer KV.

The current product does **not** claim tensor parallelism, data-parallel replicas,
pipeline microbatch overlap, continuous batching, speculative decode, request-time
layer reallocation, continuous topology re-optimization, or in-flight KV migration.

The generic native enrollment path includes `node-lenovo`, classified as
`linux_numpy_iroh`. Fresh capability and complete directed-link evidence assigned exactly
layer 21 to it in the A2 three-host route. That assignment was acquired, promoted,
loaded, and exercised by real inference; it was not inferred from enrollment or cache
presence. The member later lost its live seed lease and was unreachable on 2026-08-15,
so the same route now correctly refuses startup until membership becomes fresh again.
This preserves the boundary that prior qualification and cached bytes do not override
current membership authority.

The supervisor can also watch one owner-controlled directory of already-prepared
operator plans. Candidate discovery is read-only and path-private. Explicit browser
activation snapshots the immutable plan, opens its physical route, runs the startup
challenge, and adds it to the registry only after qualification; it never selects the
new deployment or downloads artifacts. Activation is single-flight, reports bounded
progress, is retryable after failure, and preserves the incumbent route. This closes
the gateway-restart gap for prepared deployments, not the remaining automatic M17
feasibility, assignment-local acquisition, or lifecycle-convergence gates.

A qualified candidate-backed standby can now be explicitly unloaded from memory
without deleting its prepared plan or changing the selected deployment. The registry
refuses active or in-use targets, closes the route before removing it, persists the
reduced serving set, and projects the candidate as ready to activate again. This
provides the first finite-memory residency control for switching among a larger local
catalog; boot-only deployments still need immutable candidate plans before they can
be safely unloaded and later reactivated.

The live Inference and Settings workspaces now present one model catalog control that
joins immutable local identities, lifecycle state, feasibility evidence, and prepared
activation candidates. Qualified models alone populate the inference selector;
prepared models expose explicit activation; compatible or capacity-blocked local
models remain visible with bounded reasons. Stale capacity results are never treated
as current, and catalog refresh never downloads model data.

An explicit local-only capacity refresh now captures fresh signed resources from the
richest currently qualified planned route and reruns the capability-aware contiguous
allocation planner before atomically replacing the model-operation generation. It is
single-flight and visibly separate from provisioning, activation, qualification, and
selection. Standby enrolled members still require a future capability/link probe and
new route plan before they contribute capacity; enrollment alone is never counted.
The local catalogue currently inventories 39 identities. A3 selected the exact local
Qwen2.5-7B revision `a09a35458c702b33eeacc393d103063234e8bc28` and the owner-approved
row-wise-int8 representation
`sha256:c3029635eddc32a7e2f7e8c004a5ab4e96dd4366425a9e10e9e98f5bf2e63d04`.
The physical M4 Pro → Evi Mac route uses the exact dynamic-program allocation
`node-0 [0,21) -> reviewer-mac-2 [21,28)`. Cold assignment acquisition, exact warm
zero-transfer reacquisition, independent parity, stage-local KV execution, physical
Router counter advance, qualified-only selection, a browser-streamed request, and
incumbent switchback all passed. The 7B deployment is qualified standby while the
Qwen2.5-0.5B deployment is the selected active route. Qwen3-8B remains a compatible
local identity with no owner-approved representation; it is unqualified, unselectable,
and no provisioning has started.

The catalog now also exposes a separate local-only preparation operation. A browser
can submit only one catalog model ID and immutable revision; the supervisor freezes a
fresh feasible allocation, resolves the owner-configured local cache and physical
topology, builds assignment-addressable stage shards, verifies them, stages only each
peer's allowed files, and atomically publishes a candidate for the existing activation
gate. Progress and bounded failure are reconstructable after navigation while paths,
addresses, and command diagnostics stay private. Preparation never downloads,
activates, qualifies, or selects. The first physical Qwen3-8B attempts failed closed
in local candidate construction before peer transfer: the legacy loader's transient
BF16-to-int8 materialization peak exceeded the coordinator's safe memory even after
the challenge was changed from all-stages-resident to one-stage-at-a-time. No candidate
was published and the incumbent remained qualified. The planner now binds the
row-wise-int8 representation digest, float32 runtime dtype, exact resident bytes, and
modeled conversion peak into feasibility and preparation authorization. Capacity alone
never authorizes Qwen3 conversion; the already-local, explicitly authorized
Qwen2.5-7B representation is now a physically qualified, dynamically selectable
deployment.

The A2 acquisition implementation now defines closed assignment-, representation-,
feasibility-, graph-, member-, file-, chunk-, and Merkle-bound contracts; resumable
content-addressed acquisition with bounded concurrency, source rotation, quarantine,
disk reservation, warm reuse, and atomic file-tree promotion; durable current and
terminal ledgers; and live projections across Inference, Nodes, Plans, Readiness,
Incidents, and Settings. New local preparation is wired through that Provisioner using
the explicitly authorized operator origin. Remote preparation replicates only the
recipient's assignment-local verified chunks to every authorized source, registers the
exact digest-named manifest in each private inbox, waits for freshly signed matching
availability, runs the no-origin acquisition on the assigned recipient, and hands
promoted bytes into controller staging without coordinator relay. Signed peer grants
are consumed once in a persistent recipient registry. Remote success and failure use
canonical closed envelopes, and failed final attempts preserve quarantine and recovery
accounting.

The physical Lenovo and Surface recipient runs prove cold two-source acquisition, warm reuse,
source-loss continuation, serving-safe source-side pacing, cache/source corruption,
disk reservation, manifest and membership drift, grant replay, one-writer locking,
cancellation, no-source failure, and interrupted-process resume. The ordinary product
path now also records the M4/mobile-to-Surface cold acquisition of 302,097,376 bytes from
two sources with zero origin, followed by exact warm reuse of all 302,097,376 bytes with
zero transfer. Executed gate evidence
`sha256:f1bcdd2e5b3366a942a15ac614576934ce50e185f6934b7ba25f4152a9e0504d`
binds those ledger records to a fresh physical inference returning `Paris`, a stable
two-stage route identity, and positive frame/operation deltas on both stages. The live
activation authority discovers the separately staged candidate as prepared with zero
invalid candidates. All eight browser workspaces passed current-source checks; refresh,
navigation, Back/Forward, backend outage/reconnect, and a clean second session preserved
the intended tab-private history boundary. The final regression passed 3,942 Python
tests (13 skipped), 485 frontend tests, TypeScript/build/contracts, repository-wide
Ruff, and all governance, claim-boundary, contract, and release-security audits. A2 is
closed by its atomic milestone commit.

The member-side transport is now platform-neutral and deployable. It serves verified
content-addressed chunks over certificate-validated HTTPS only after checking a fresh
Provisioner-signed grant, recipient signature, source availability, member generations,
range scope, and a durable replay fence. Each response has a signed byte-range receipt,
and the Provisioner enforces aggregate and per-source byte-rate schedules. Signed
availability bundles provide the dynamic discovery input. Source agents atomically
reconcile private exact-manifest control documents without restart and renew their
authority before expiry; invalid inbox state preserves the last valid serving snapshot.
This control path grants no placement authority and transfers no model bytes.

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

## As-built capability matrix against the synthesized architecture specification

`qualified` means physical evidence exists for only the boundary stated. It does not
promote the rest of a composite capability.

| Architecture capability | State | Exact current boundary |
| --- | --- | --- |
| 4.1 Evidence-driven planning | `qualified` | One signed, source-bound M13 bundle coherently supplies membership, status, runtime/decode mode, memory tiers, and required directed edges to the selected live candidate; stale or mismatched evidence fails closed. |
| 4.2 Capability-aware contiguous allocation | `qualified` | Deterministic contiguous DP selected the M13 two-Mac 15/9 split and the M14 measured-order three-host 1/1/22 split. Compute-only and fast-memory-only physical A/B inputs produced distinct allocations with `planner_v2` provenance. |
| 4.3 Directed cyclic topology | `qualified` | A complete six-edge activation-plane matrix across three physical hosts drove exact cycle selection; the winning cycle differs from canonical order and is opened into forward edges plus explicit decode loopback. |
| 4.4 Phase/workload objectives | `partial` | M15 compares three phase objectives over two content-free workload profiles on one frozen evidence snapshot, exposes robust/Pareto selection, and binds sequential physical observations to exact request-shape predictions and frozen budgets. Concurrency and batch shape remain modeled rather than executed. |
| 4.5 Assignment-local artifacts | `partial` | M13 materializes and proves only each host's assigned shard plus shared static assets, with assignment-bound load proofs. Cache eviction, corruption recovery, concurrent staging, and full runtime memory/thermal admission are not qualified. |
| 4.6 Progressive routing and immutable execution | `qualified` | M16 reserves every placement before dispatch, projects the progressive candidate, commits an immutable graph/topology-bound `PathManifest`, and releases all request resources on every terminal path. |
| 4.7 Batching/scheduling/backpressure | `partial` | M16 physically qualifies bounded concurrent admission, interactive/batch QoS with aging, sequential dispatch, queue bounds, cancellation, and cleanup. Physical runtime batching, continuous batching, and pipeline overlap remain explicitly unclaimed. |
| 4.8 Data-parallel stage replication | `implemented_unintegrated` | Planner/flow concepts exist; no live replica group or multi-track physical throughput proof exists. |
| 4.9 Speculative decode | `design_only` | No promoted draft/target runtime path. |
| 4.10 KV ownership/fault tolerance | `partial` | M23 physically qualifies stage-local KV ownership and cleanup on one three-host MLX/MLX/NumPy Qwen2 route. Live replay recovery and fenced successor recovery are not integrated; the M23 result does not claim KV migration or a general cross-backend cache format. |
| 4.11 Deterministic scoped replanning | `implemented_unintegrated` | Replan types/logic exist; current failure rebuilds or switches a complete deployment. |
| 4.12 Signed heterogeneous membership | `partial` | Durable signed membership now binds two MLX Macs and one `linux_numpy_iroh` host as fresh activation-eligible members. Platform-neutral Android/iOS and off-tailnet heterogeneous activation remain open. |
| 4.13 Traffic-aware liveness | `implemented_unintegrated` | General liveness code exists, but the live route can remain blocked until node command timeout after peer loss. |
| 4.14 Authenticated direct/relay transport | `partial` | Native authenticated Iroh publishes path class and activation-connection observations. M14 physically observed six direct edges; forced relay, relay region/redaction, unknown rather than zero for missing samples, and off-tailnet control-plane operation remain open. |
| 4.15 Privacy/authority/qualification | `partial` | Privacy-reduced projections, qualifier-gated admission, durable authority generation, and refresh persistence are real. A0 static boundary/contract governance is green. A1 separates a monotonic `live_runtime` envelope from immutable `sealed_historical` records, removes the numbered historical browser endpoints, and refuses historical evidence as current. The ordinary product URL was browser-verified live: one 0.5B request completed, runtime generation advanced from 4 to 5, physical frames advanced from 9/9 to 15/15, server-retained terminal history advanced from one to two, and refresh preserved both the terminal history and the original recorded-evidence timestamp. The composite remains `partial` because A15 release closure and the other listed privacy/authority gates remain open, not because the A1 browser gate is open. |

## Product UI truth boundary

The shell has eight stable workspaces: Inference, Device Lab, Network, Nodes, Plans,
Readiness, Incidents, and Settings. Inference uses the product/request gateway.
The product snapshot/event spine is the shared privacy-reduced live source. One
contract-pinned evidence projection exposes current route execution separately from a
read-only recorded-evidence register. Every live workspace shows the same human-readable
source provenance; sealed records display their original observation time and authority
and cannot satisfy live readiness. The former numbered M15-M23 browser endpoints and
their repeated source-panel polling are removed. Bounded same-origin live
route/deployment endpoints remain action/status adapters for Inference. M13 placement,
M14 topology, and M15
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
| M17 | `partial` | Multi-model inventory, dense Qwen2/Qwen3 adapters, representation-bound resident/load-peak feasibility, fail-closed selection, live capacity refresh, exact owner-authorized local preparation, and prepared-deployment activation exist; live 0.5B/1.5B and no-restart 3B qualification are the physical boundary | Successful representation-approved 7B preparation/qualification and the remaining acquisition failure matrix stay open |
| M18 | `implemented_unintegrated` | Historical replica contracts, planner documents, and a qualification-only whole-model throughput observation exist. They are no longer served through a polling endpoint as live runtime state. | Re-prove a replicated stage inside a multi-stage pipeline through concurrent browser requests on the normal product path; do not promote the historical single-stage whole-model result as stage replication |
| M19 | `implemented_unintegrated` | Historical liveness/recovery contracts and script-driven positive replay evidence exist. They are no longer exposed as live sources. The qualification script now measures detection, delivery, and cleanup and marks the unexecuted negative gate explicitly instead of fabricating success. | Integrate traffic-aware detection, scoped failure, full-context replay, fenced KV successors, circuit breakers, restart reconciliation, and observed positive/negative browser-path recovery |
| M20 | `design_only` | Target-authoritative speculative contracts and pure decoders remain; the stored disabled decision is not a live browser authority and no speculative capability endpoint is exposed. | Add real multi-position target verification, same-session measurement, bounded draft fallback, and either measured material gain or an honestly measured disabled decision |
| M21 | `partial` | Durable heterogeneous membership and physical MLX/NumPy execution are real. Participation and OS class no longer fabricate per-member connectivity or external-network proof; current direct-path observations remain historical. | Derive member connectivity only from bound activation observations, prove off-tailnet join plus direct/relay serving, version generic peer capabilities, and separately qualify Android/iOS eligibility |
| M22 | `partial` | A historical release/service/UI/reviewer bundle exists; it is not exposed as current evidence and is not a current complete-architecture or public-release claim. | Replace operator-authored gate booleans with executed-artifact provenance and rerun closure only after the open architecture gates pass |
| M23 | `partial` | One three-host MLX/MLX/NumPy stage-local-KV route is physically qualified with exact output parity, one-token decode on every stage, terminal cleanup, and measured performance gain. The exact sealed record is available only under `Recorded evidence` with its original capture time. | Preserve that exact result while replay recovery, tensor parallelism, continuous batching, KV migration, and mobile activation remain open |

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

The governing remaining sequence is
`docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md`. It reopens
M18-M20 as product-path capability gates, separates sealed historical evidence from live
runtime state, decouples replay recovery from replica capacity, moves real batched target
verification before speculation, and gives Android/iOS, off-tailnet onboarding, direct/relay
transport, installation, and UI closure explicit gates. Tensor/hybrid parallelism,
full-model data parallelism, cross-backend KV migration, prefix caching, disaggregated
prefill/decode, expert parallelism, and sequence parallelism remain separately reviewed
future programs rather than additions to the Mycelium completion plan.

### Frozen A3-A15 execution boundaries

The machine-checked live progress register is
`docs/handover/mycelium-completion-checklist.v1.json`; `scripts/mycelium_completion_audit.py`
requires exactly one primary gate, binds every gate to its specification, partitions all
closure requirements into completed/partial/pending state, and requires all eight UI
workspaces. The register is navigation, not qualification evidence. A3 remains the sole
primary gate; A4-A15 remain `design_only` even where their dependency-ready specification
and deterministic acceptance inputs now exist.

| Gate | Current state | Frozen boundary |
| --- | --- | --- |
| A3 | `integrated_unqualified` | Exact authorized local Qwen2.5-7B preparation, qualification, selector, physical runtime, and live browser gate |
| A4 | `design_only` | Concurrent dispatch, interruptible commands, traffic-aware scoped liveness, cleanup, and second-session privacy |
| A5 | `design_only` | Request-level replicas of a contiguous stage in a real multi-stage graph, complete legal tracks, and measured marginal gain |
| A6 | `design_only` | Gateway-committed full-context replay with request-scoped cutover, exact-once delivery, and explicit no-successor abort |
| A7 | `design_only` | Byte-compatible acknowledged KV successor with generation fencing and mandatory A6/abort fallback |
| A8 | `design_only` | Pinned public-HTTPS membership control plus EndpointID-authenticated Iroh direct/relay activation without Tailscale dependency |
| A9 | `design_only` | Versioned platform-neutral membership/profile/capability/qualification with unknown-ineligible and no legacy eligibility carry-over; physically qualified accelerated Windows x86_64 backend; persistent int8 execution without warm decode-time whole-weight materialization; qualified reduced-precision activations; component/edge timing and component-aware placement; frozen single-request performance floor |
| A10 | `design_only` | Depends directly on atomic A4 and A9 closure; runtime-authoritative continuous batches, causal pipeline overlap, and transactional multi-position target verification, measured without hiding backend/precision/placement changes inside the scheduling candidate |
| A11 | `design_only` | Off-by-default target-authoritative speculation promoted only by same-route parity and confidence-bound material gain |
| A12 | `design_only` | Generic native Android and Apple-mobile eligibility, separately qualified by role, lifecycle, thermal, power, memory, and network evidence |
| A13 | `design_only` | Signed normal-user packages, target-owned identity, encrypted recipient-bound invitation handoff, consent, and managed lifecycle |
| A14 | `design_only` | Read-only privacy-preserving coarse-region/logical route explorer with unknown tray and accessible non-map equivalents |
| A15 | `design_only` | Release decision derived from validated executed-artifact provenance, reproducible packages/SBOM, and a clean external reviewer path |

## Reviewer rules

Treat executing code and fresh tests as stronger than prose. Treat private evidence as
a claim only after verifying its digest, source binding, host/process identities, and
qualification authority. Do not equate a checked plan box, fixture, simulated route,
staged artifact, or startup challenge with a sealed physical product claim. Report
current implementation defects separately from planned M12-M21 capabilities.
