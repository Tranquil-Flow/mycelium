# Mycelium A1 live evidence separation specification

## 1. Outcome and claim boundary

A1 replaces the current practice of loading sealed capability documents once and
serving them repeatedly as if they were live. The product exposes two distinct evidence
classes:

- runtime projections captured from a running authority; and
- immutable historical records captured by a named planner, qualifier, gate, or release
  process.

The projection layer is read-only. It cannot grant membership, select placement,
activate a deployment, admit a request, change qualification, or promote a historical
record to current. Qualification remains the sole route-readiness authority.

A green A1 gate proves truthful evidence provenance and browser behavior. It does not
promote replication, recovery, speculative decoding, heterogeneous networking, release
closure, or heterogeneous KV beyond their governance-ledger states.

## 2. Closed evidence envelope

Every browser-facing capability record uses
`mycelium.evidence_projection.v1`, a closed JSON object with exactly:

- `protocol`;
- `record_id`, a stable privacy-reduced identifier;
- `capability`, an allowlisted product capability name;
- `source_kind`, one of `live_runtime`, `planner_intent`, `sealed_historical`,
  `replay`, or `fixture`;
- `authority`, the code or process authority that produced the payload;
- `generation`, a non-negative source-local integer;
- `captured_at_unix_ms`, when this projection was created;
- `observed_at_unix_ms`, when the underlying fact was observed;
- `valid_until_unix_ms`, a positive integer for expiring live sources and `null` for
  immutable historical sources;
- `freshness`, one of `current`, `degraded`, `stale`, `historical`, `replay`, or
  `fixture`;
- `payload_protocol`; and
- `payload`, the already validated, privacy-reduced capability document.

The following invariants are mandatory:

1. `live_runtime` is the only source kind that may be `current` or `degraded`.
2. `sealed_historical` is always `historical`, has no validity deadline, and is never
   accepted by a live gate.
3. `replay` is always `replay`; `fixture` is always `fixture`.
4. Planner intent may be `current`, `degraded`, or `stale`, but cannot establish that a
   runtime action occurred.
5. Live capture time is at least observed time and no later than validity time.
6. Record IDs, authority names, capabilities, and payload protocols are allowlisted;
   unknown fields and privacy-sensitive values fail closed.
7. A historical payload's observation time comes from its validated record. File mtime,
   server restart time, and browser poll time cannot refresh it.

## 3. Runtime authority and terminal history

The running route publishes one fresh `live_runtime` envelope from current route state.
Its generation changes only when its canonical privacy-reduced payload changes. The
payload includes route identity, deployment/model identity, topology version, aggregate
and per-peer physical counters, current stage/KV ownership summaries, recent inference
terminal records, and runtime incidents already exposed by the validated live route
status contract.

The route retains bounded terminal inference and incident history. A complete live
snapshot returned after refresh or reconnect reconstructs that history from the server;
browser-local storage is never its authority. Polling without a runtime change may
advance capture time but must not invent a new source generation or observation time.

A current runtime envelope expires under the same bounded freshness policy as the
product evidence spine. Expired data renders as stale and cannot satisfy a live gate.

## 4. Historical evidence register

Validated M18–M23 documents are removed from live route mutation/status methods and
stored in a read-only historical evidence register. The register preserves each
document's original protocol, digest/binding, observation or generation time, and named
authority. Its records are immutable for the lifetime of the server and are returned as
`sealed_historical` envelopes.

The browser may filter the register by product capability. It must not poll individual
historical records as though they were live, and a server restart must not rewrite their
observation time. Missing historical files remain absent rather than becoming fabricated
empty success records.

Browser-facing paths use product capability names rather than internal milestone labels:

- `GET /__mycelium/evidence/runtime`;
- `GET /__mycelium/evidence/history`;
- optional filtered reads through `?capability=<allowlisted-name>`.

The remaining live capability reads use product names:

- `GET /__mycelium/planning/workload-comparison`;
- `GET /__mycelium/runtime/admission-status`;
- `GET /__mycelium/models/operation`; and
- `GET /__mycelium/swarm/resource-observations`.

The old milestone-numbered capability paths from M15 through M23 are removed. There is no compatibility
requirement because Mycelium has not been publicly released.

## 5. Fabricated fact removal

A1 deletes or withholds facts that were not observed by their owning authority:

- hard-coded detection, duplicate-delivery, cleanup, and negative-recovery success;
- membership participation interpreted as network connectivity;
- operating-system class interpreted as external-network proof;
- a stored release or KV gate interpreted as current runtime readiness; and
- static test values interpreted as current physical measurements.

Qualification scripts may still create candidate evidence from measurements they
actually execute, but every success boolean must be derived from captured events,
counters, terminal state, or verified artifact digests. Until that derivation exists,
the field is `unknown`, absent under a new protocol, or the gate remains withheld.

## 6. Product evidence spine and UI

The existing product snapshot/event spine remains the shared live source for all eight
workspaces. A1 adds the evidence envelope as provenance, not as a second readiness
authority.

The deployable product frontend starts as a live same-origin client when no build-time
source mode is supplied. Fixture mode is an explicit development or recorded-demo
choice. Failure to establish the product gateway session renders a truthful unavailable
state and never silently falls back to fixture data. Consequently, an ordinary product
URL exposes the live qualified deployment registry, model catalogue, and swarm
membership controls without a private query-string convention or a separately rebuilt
frontend.

Every workspace displays a compact source badge using human terms:

- `Live now` for fresh runtime authority;
- `Live · degraded` for a reachable but degraded runtime source;
- `Stale` for expired runtime or planner intent;
- `Recorded evidence` for sealed history;
- `Replay` for an operator-selected replay; and
- `Demo data` for fixtures.

Network, Plans, Readiness, and Incidents render genuine live route/product state as
their primary content. Historical replication, recovery, speculation, heterogeneous
network, release, and KV records move into a single `Recorded evidence` section with
capture time and authority. They no longer appear as a stack of apparently live
milestone panels. Inference, Device Lab, Nodes, and Settings use the same badge and
historical register where relevant.

No product copy, heading, error, request path, or navigation element exposes internal
`M18`–`M23` milestone names. Protocol details remain available in an expandable
provenance view.

## 7. Persistence, refresh, and ordering

- Server-owned terminal history survives browser refresh and workspace switching.
- The product event cursor remains the ordering authority for shared live snapshots.
- Runtime evidence generation is monotonic within one server incarnation.
- A server incarnation identifier prevents a restarted counter from appearing as a
  continuation of the previous process.
- Historical records are ordered by observation time then stable record ID.
- Duplicate records with the same ID and digest are idempotent; divergent duplicates
  fail server startup.

## 8. Privacy and security

The evidence envelope and register reject prompts, completions, token IDs/text, logits,
activations, KV contents, tensor/model weights, local paths, endpoints, hostnames/IPs,
credentials, private keys, invite material, and bearer tokens. Payload validation occurs
before envelope creation. The two endpoints are read-only and remain covered by the
claim-boundary and contract audits.

## 9. Verification gates

### Software positive

1. Contract fixtures and strict Python/TypeScript decoders accept all source kinds and
   reject unknown fields or illegal source/freshness combinations.
2. A runtime counter change increments the runtime evidence generation and updates its
   observation time; an unchanged poll does neither.
3. Terminal inference and incident history remain visible after constructing a new
   browser client and changing workspaces.
4. Historical records retain their original observation time across repeated reads and
   are visibly labelled `Recorded evidence` with their authority.
5. All eight live workspaces consume the shared live source/provenance model, and a
   repository scan finds no internal milestone labels in user-facing copy or endpoint
   constants.
6. A production build with no source-mode variable bootstraps the same-origin live
   product session. Fixture mode remains available only when explicitly configured.

### Negative

1. A sealed record cannot be decoded or rendered as current and cannot satisfy a live
   readiness predicate.
2. A stale runtime envelope cannot satisfy readiness.
3. Divergent duplicate historical records, invalid observation times, unknown source
   kinds, unknown capabilities, private fields, and payload protocol mismatches fail
   closed.
4. Removing the runtime authority does not fall back to a sealed record or fixture.
5. Fabricated network/recovery/release facts are absent or explicitly unknown.

### Physical/browser positive

Run a real inference from the browser. Before the request, capture runtime evidence
generation and physical frame counters. During/after the request, capture the new
generation and increased counters, then refresh the page and verify the terminal request
history remains. Open one recorded capability item and verify it remains labelled
historical with its original observation time.

The physical gate remains open if browser automation cannot access localhost or if no
real request is executed. Component tests and direct HTTP checks are supporting evidence,
not a substitute for this browser observation.

### Executed browser result

The 2026-08-11 product-path run satisfied this gate on the ordinary live URL. A bounded
Qwen2.5-0.5B request completed with decoded output `Paris`; runtime evidence generation
advanced from 4 to 5, physical frames advanced from 9 sent/received to 15
sent/received, and server-retained terminal history advanced from one to two. A full
page refresh preserved the prompt, response, terminal table, generation 5, frame
counters, and the sealed historical record's original observation time.

A separate 1.5B request emitted five decoded tokens and then failed closed on
`decode_completion_timeout`. That route became unavailable and is not counted as a
successful A1 inference or a currently qualified model. The UI then allowed an explicit
switch to the healthy 0.5B route; it did not silently rebind the in-flight request.
