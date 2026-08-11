# Mycelium M17 Multi-Model Catalog, Feasibility, and Provisioning Specification

**Status:** implementation baseline
**Milestone:** M17
**Parent architecture:** `2026-08-09-mycelium-astra-architecture-product-design.md`

## 1. Outcome

M17 makes model choice a qualified product operation. Mycelium discovers immutable
local artifacts, determines whether one complete compatible track can serve a declared
workload, provisions only each peer's assigned content, proves load and distributed
correctness, and exposes only qualified deployments for selection. It rejects models
that are incomplete, incompatible, or too large before transfer or Router admission.

The operator has required a local-artifact-only gate. M17 must not download a model,
tokenizer, config, or missing shard without fresh explicit approval. Discovery may
read existing caches; it may never repair them from the network implicitly.

M17 does not add tensor parallelism or claim that GGUF/llama.cpp RPC is equivalent to
Mycelium's contiguous stage pipeline. Data-parallel stage replication remains M18.

The priority physical usefulness target is the largest already-local model admitted by
fresh swarm evidence. At the current measured generation that is
`Qwen/Qwen2.5-7B-Instruct`; `Qwen/Qwen3-8B` is rejected by serving-representation load
peak. Its dense Qwen3 adapter still models bias-free Q/K/V projections, per-head Q/K
RMS normalization, grouped-query attention, RoPE, stage-local KV, and untied output
head explicitly. Either model may enter selection only after local
manifest/load/parity and physical qualification gates pass; local presence and a
source-weight fit are not enough.

## 2. Authorities and lifecycle

The model lifecycle is:

`discovered -> compatible -> feasible -> provisioning -> loaded -> qualified -> active`

`incomplete`, `infeasible`, `unavailable`, and `retired` are explicit non-serving
states. Each transition has one authority:

- the local catalog owns discovery and immutable artifact identity;
- the architecture/runtime adapter owns format, tensor, tokenizer, backend, and
  quantization compatibility;
- the Planner owns feasibility and contiguous assignment intent from one signed
  capability/link snapshot;
- the provisioner owns assignment-local integrity evidence;
- the runtime owns load and stage-probe evidence;
- the qualifier alone owns distributed serving readiness;
- the deployment registry owns atomic active selection among current qualifications.

No neighboring state implies the next. A discovered file is not compatible; a
feasible plan is not provisioned; provisioned bytes are not loaded; a loaded stage is
not qualified; and a qualified deployment is not active until explicitly selected.

## 3. Model identity and catalog contract

`mycelium.model_catalog.v1` is a closed, bounded, privacy-reduced projection. Each
entry binds:

- model repository/display ID and immutable revision;
- model family/architecture and architecture-adapter ID;
- tokenizer/config identity and required local presence;
- checkpoint representation and format adapter;
- dtype or quantization identity;
- decoder layer count, hidden size, attention/KV geometry, and context limit;
- exact required file names, bytes, and available content digests;
- static model bytes and assignment-addressable component metadata;
- lifecycle state, source authority, and bounded machine-readable reasons.

Private cache roots and local absolute paths never enter the product projection.
Internal catalog records may hold target-owned paths, but they are not accepted from
the browser and are not emitted to evidence or logs.

Mutable references such as `main` are resolved only from an already-present local
cache ref to one 40-hex snapshot commit. Multiple snapshots are separate catalog
entries. Mixed-snapshot lookup and recursive first-basename cache search are forbidden.

## 4. Architecture and format adapters

One adapter supplies layer count, tensor namespaces, embeddings/final-head ownership,
KV geometry, checkpoint format, and runtime compatibility to catalog, manifest,
assignment compiler, stage-pack builder, and loader. A global hard-coded
`model.layers.<N>` parser is forbidden.

Initial catalog discovery may recognize:

- sharded Safetensors with an exact non-empty index;
- single-file Safetensors with an exact locally derived tensor map;
- GGUF as a distinct representation through a GGUF metadata adapter.

Recognition is not serving support. Unsupported model family, conditional/multimodal
topology, MoE routing, checkpoint layout, tokenizer, backend, quantization, or missing
tensor ownership yields an explicit incompatibility reason. Unknown format and empty
layer mapping fail closed.

Each quantized representation has its own immutable identity. Mycelium never silently
quantizes or substitutes a representation. Promotion requires an independent,
non-participating reference under a frozen exact-token or model-specific error/quality
tolerance plus measured memory/performance benefit.

The implemented preparation choice is an explicitly catalogued
`mycelium.rowwise_symmetric_int8.v1` serving representation with float32 runtime
compute. Its representation digest binds the source artifact, quantizer, runtime
dtype, resident bytes, and modeled conversion-load peak. The UI shows the source to
serving transition before preparation; this is not an implicit change made after the
user selects the model.

## 5. Swarm feasibility

`mycelium.model_feasibility.v1` binds one catalog entry, workload profile, signed
capability evidence generation, directed-link generation, planner policy, and proposed
deployment. Before any transfer the capability-aware contiguous allocation DP must
find one complete track whose every node supports the representation and owns a
positive half-open layer range.

Per-node feasibility includes:

- exact assigned weight/static component bytes;
- exact resident representation bytes, modeled source-conversion load peak, and
  activation/workspace requirements;
- bounded KV for declared context, output, batch, and concurrency;
- runtime reserve/headroom, observed RSS and swap pressure;
- local disk needed for missing content plus staging overhead;
- backend, architecture, dtype/quantization, runtime build, and decode-mode support;
- stage count, directed edge availability/goodput, and assignment role;
- power/thermal/battery state when exposed by that device class.

The report contains required/free resources by candidate peer, proposed ranges,
bottleneck, maximum admitted context/concurrency, already-present and missing bytes,
transfer/runtime estimates, adapter decisions, rejected alternatives, and bounded
reason codes. A stale input generation invalidates the report.

Infeasibility rejects before provisioning. Mycelium must not reduce context, change
quantization, remove a requested peer constraint, choose another model, or fall back
to an old deployment to report success. The UI may offer an explicit separately
qualified alternative, but it remains a new user decision.

## 6. Assignment-local acquisition and integrity

Every assignment names one immutable target-owned artifact root. Downloader,
verifier, stage-pack builder, and runtime receive the same root. Runtime-initiated
secondary cache selection or downloading is forbidden.

The assignment compiler chooses the minimal set of whole upstream files that covers
the assigned decoder range and role-specific shared components. Shard-level overfetch
is recorded. A peer receives only authorized model files/components and shared static
assets; copying the full checkpoint to every peer is not an implementation shortcut.

Before load, each file is verified against revision-local identity, expected byte
count, digest algorithm/value, readable format/header, exact tensor-key coverage,
dtype/shape, component role, and assigned layer prefixes. Presence, basename, and
non-zero size alone are insufficient.

Concurrent acquisitions are keyed by immutable artifact identity and rely on the
underlying library's lock lifecycle plus a Mycelium assignment lock. Mycelium never
recursively deletes cache locks. Transfers use atomic temporary-to-final promotion,
bounded retry/backoff for transient errors, durable resume metadata, and explicit
quarantine/refetch for corruption. Authorization, unsupported format/backend,
insufficient capacity, missing manifest entry, and repeated checksum failure are
terminal.

Warm reuse must transfer zero duplicate bytes. A candidate may not displace the
active qualified deployment until every assignment, load proof, stage challenge,
distributed correctness gate, and qualification binding succeeds. Failure rolls back
to the still-qualified incumbent without mutating its artifacts or registry record.

## 7. Deployment selection and request binding

The registry stores multiple immutable qualified deployments and atomically selects
one active default. The browser may select only a current qualified deployment.
Selection affects future admissions only. Each request binds the exact model,
revision, representation, manifest, assignments, graph, qualification, and deployment
epoch at admission; in-flight requests and history never change when the default does.

Capability drift, peer loss, corruption, expired qualification, or failed startup
challenge marks the affected deployment unavailable and removes it from selectable
choices. It does not silently select another deployment for an already-admitted
request.

## 8. UI contract

- **Model catalog control:** one data-driven projection joins immutable local catalog
  identity, lifecycle, current feasibility, and prepared-deployment activation state.
  Discovery is visibly distinct from compatibility, fit, preparation, qualification,
  and active selection. Only a prepared deployment can be activated, and only a
  qualified deployment can enter the inference selector. Refresh and activation never
  authorize a download.
- **Inference:** qualified model/deployment selector, immutable revision and
  representation, capacity/context envelope, active versus requested choice, and
  history bound to the exact deployment.
- **Plans:** catalog candidates, compatibility/feasibility decision, contiguous
  allocation, required/free resources, bottleneck, cached/missing bytes, transfer and
  execution estimates, and precise rejection reasons.
- **Nodes:** assigned layer/component roles, cache/provision/load state, verified and
  missing bytes, backend/representation support, without private paths.
- **Readiness:** every lifecycle rung and its authority, digest, freshness, and missing
  proof; active selection is distinct from qualification.
- **Incidents:** incomplete snapshot, capacity drift, interrupted transfer, corruption,
  permanent/transient load failure, parity rejection, qualification expiry, and
  rollback.
- **Settings:** a qualified future-request preference only; stale or unqualified IDs
  are not retained as active defaults.

Navigation, refresh, reconnect, and Back/Forward preserve catalog generation,
selection, progress, rejection reason, and bounded request history. Unknown or stale
state is never rendered as zero, healthy, or ready.

Adding a member does not itself make a model runnable. The UI links model capacity to
member enrollment, but a new member contributes only after fresh capability evidence,
feasibility planning, artifact preparation, route loading, and qualification. An
expired feasible or infeasible report is shown as requiring a new capacity check, not
as a current admission result.

## 9. Verification gate

1. RED contract tests cover closed shapes, bounds, duplicate identities, mixed
   revisions, private paths, unknown formats, incomplete tokenizer/weights, unsupported
   adapters, stale evidence, and oversized models.
2. A read-only local inventory proves no network requests or cache mutation. Incomplete
   local snapshots remain incomplete and name every missing requirement.
3. Pure feasibility tests cover exact fit, one-byte-over limit, KV/context pressure,
   disk pressure, representation load-peak pressure, backend/quantization mismatch,
   missing directed edge, stale evidence, deterministic allocation, and no silent
   fallback.
4. Provisioning tests prove minimal covering shards, target-owned roots, concurrent
   deduplication, interrupted resume, warm zero-byte reuse, corruption quarantine,
   bounded retry classification, load proof, and incumbent rollback.
5. Physically qualify the current 0.5B baseline and at least one more useful locally
   complete instruct model if observed capacity permits. Each same-run bundle includes
   independent reference correctness, model/manifest/assignment/load/graph/request
   bindings, output, host identities, and before/after Router frame counters.
6. From the browser, switch between both qualified deployments and complete one real
   distributed request on each. Refresh retains selection and exact history binding.
7. A deliberately incomplete, incompatible, or oversized local entry is rejected
   before transfer with the same reason in Plans, Readiness, and Inference.

M17 completes only when all non-excluded gates pass. A missing local artifact is not an
approved reason to download it; the operator must separately authorize any download.
