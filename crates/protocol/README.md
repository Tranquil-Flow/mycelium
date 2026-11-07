<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Protocol port slice (L5, work in progress)

Continuation oracle: `mycelium-v014` commit
`f4e366e98a1f705dfaf182897044500d290dbd7f` (device-accepted). Earlier fixtures retain
original `941765e882ed3eea9a5576cc53e525c4caa20300` and
`27fdd49de7e18b58affa85b8a31e9a734d7b4d1e` provenance. The explicit
`f4-oracle-repin.json` audit verifies all 87 registered fixture blobs at the new
pin and identifies changed source inputs; historical evidence is not relabeled.
No oracle is imported or run by this crate.
External fixture generators are not part of the product, tests, CI or release.

## Coverage, not a release claim

All **87/87** registered fixture identities are inventoried and have exact canonical
JSON comparisons, with additional float-bit-pattern and strict-ingress tests.
This is not 87 behavioral ports. `fixtures/coverage.json` is the exact crosswalk:

| Implemented boundary | Registered identities |
|---|---:|
| Membership wire/schema/signature | 13 |
| Closed cancellation/liveness/internet schemas | 8 |
| Request submission/event v1/v2 schemas | 4 |
| Transport-path observation and coarse-region evidence schemas | 2 |
| Seed-key transition dual-signature continuity proof | 1 |
| Closed path manifest and binding to independently validated graph facts | 1 |
| Activation status and public swarm status | 2 |
| Execution graph parser and structural semantics | 1 |
| Replica plan/qualification/runtime/benchmark v1/v2 | 5 |
| Manual provisioning route and preparation authorization derivation | 2 |
| Public bootstrap primary JSON Schema | 1 |
| Router binary framing and all eighteen message bodies (finite JSON) | 1 |
| Recovery intent/qualification/runtime/gate and checkpoint projection | 5 |
| Product snapshot/event and route explorer | 3 |
| Performance budgets v1/v2/v3 | 3 |
| Capacity profile exact parser and derived claim reconstruction | 1 |
| Observatory semantic snapshot/event | 2 |
| Evidence projection and immutable history derivation | 2 |
| Candidate promotion report reconstruction | 1 |
| Gossip router/allocator derivation, bundle, signed bundle and link state | 5 |
| Cache/materialization, concurrent runtime, M13/M14/M15 projections and M23 gate | 7 |
| Route-plan parser and unqualified route-qualification record | 2 |
| Evidence-bound planner snapshot derivation | 1 |
| Independently bound loader-proof authentication | 1 |
| Assignment UUIDv5, artifact report and provisioning audit | 3 |
| Governance, seed inventory and live-incident projections | 3 |
| Seed backup boundary and operator rotation/revocation projections | 3 |
| Live-route public status and counter projection | 1 |
| Control-plane tranche and layer-replan simulation report (consumed from the L7 planner; no copy here) | 2 |
| **Implemented wire/schema/derivation total** | **86/87** |
| Source-reviewed historical disposition (not implementation) | 1 |
| Canonical bytes/inventory only | 0 |

The external differential corpus covers accepted and refused mutations, exact
canonical/signature bytes, error precedence and field/clock boundaries. Evidence
schemas validate declared freshness, connection reuse and canonical digests; they
do not perform measurements, authenticate authority, locate peers or prove a
physical topology. Tests check the complete fixture file set against its hash seal.

Additional unregistered surfaces do not inflate the registry count:

- Managed owner hello/configure authentication, closed native command/payload and
  response grammars, signed managed claims/results/token progress/ACK receipts.
- Process-local token grant admission, single-use claims, chained receipt cursor,
  and signed node cancellation observations with caller-pinned subject identity.
- Native progress framing bound to expected node/command, followed by the pinned
  cursor's signature, grant, currentness, deadline, sequence and terminal checks.
- Mobile artifact grant/request/receipt signatures, windows and endpoint binding.

`ReceiptCursor::accept_verified` / `accept_progress` return an immutable
`VerifiedReceipt`: sequence, token IDs, status/error, receipt digest and the exact
canonical signed envelope bytes. `VerifiedObservation::cancel_receipt` similarly
retains the verified signed observation, digest and exact caller-owned subject.
Downstream sealers should retain these bytes, not reread mutable input. These
records are **not** ACKs, stage liveness, aggregate cleanup or permission to release
resources. Existing token-only and cancellation-predicate APIs remain available.

## Boundaries consumers must keep

Use `strict::object` / `strict::canonical_object` before Value-based verifiers.
Nested/escaped duplicate keys, nonobjects, nonfinite numbers, excessive nesting
and noncanonical managed input refuse. Native progress has its own bounded raw
JSON ingress; its routing fields alone authenticate nothing.

`managed::verify` authenticates an envelope; native command grammar acceptance is
not execution authorization. `control::authenticate_request` adds the initial
owner-command/target-poll common schema and caller-supplied member binding.
L6 still owns mailbox transactions, consent, durable replay/prune/reopen, command
execution, full receipt forwarding, authenticated ACK delivery, bounded inboxes,
worker lifecycle and current participant/quorum authority. The token registry is
process-local and never evicts replay fences to admit new work; it is not F2's
durable Bloom/sequence implementation.

Membership declaration shapes are covered, but inner peer-profile signatures,
seed cutover eligibility, key rotation selection, lease-renewal causality,
pending heartbeat identity and durable generation admission remain consumer work.
Artifact grants bind recipient node/endpoint/key and assignment, not an invented
membership-generation or renewal policy. Time is injected; `TimeLimits` and
`GrantLimits` have no default operational values. L1 still owns the missing
membership-message and artifact TTL/skew registry IDs.

`identity` and `canonical` are shared native extractions. The mobile facade has
no duplicate identity/membership implementation and compiles natively and for
`wasm32-unknown-unknown`; it does not replace the old full node/compute/network API.

`remote` preserves native version-1 framing, lengths and kinds. `transport`
preserves ALPN bytes, authenticated peer/generation and request-correlation
fences, inclusive monotonic deadlines, cancellation and cleanup error propagation.
The deterministic adapter corpus covers all channels, malformed/truncated frames,
delayed/duplicate/reordered replies, stale generations and cleanup failure.
**No real Iroh/QUIC/authentication parity or rebuilt physical consumer is claimed.**

## Additive consumer APIs in this continuation

- `gossip::{build_bundle,parse_bundle,seal_bundle,verify_signed,validate_link}`
  reconstruct both complete registered views (including Python dataclass-repr
  eligibility digests), validate five record payload kinds, unique sorted inputs,
  quarantine and peer eligibility, and bind the complete bundle and canonical
  capacity profiles to the shared Ed25519 identity. `verify_signed` requires an
  independently trusted key digest and injected exact integer clock; freshness is
  `[captured, valid_until)` and records must be current at capture. The source
  verifier does **not** compare authority generation to persisted current state;
  consumers must do that, as well as replay/member/lease checks. `Limits` requires
  L1/caller-supplied gossip record TTL hard cap; no operational default is added.
  The pinned Unicode printability data is needed for source-compatible repr hashes.
  No registry/liveness service, authority issuance or transport is implemented.
  As with other Value APIs, callers must use bounded `strict` raw ingress first.

- `public_evidence::{cache,materialization,runtime,placement,topology,comparison,kv_gate}`
  and the shared schema dispatcher implement seven complete pinned public
  validators. This preserves source permissiveness: M13/M14/M15/M16 fields are
  not silently strengthened into execution authority, materialization digests
  have the source's prefix/length checks, and the M23 measurement/promotion
  relations and ASCII evidence digest are independently checked. None of these
  functions performs cache IO, observes a worker, plans a route or runs a gate.

- `route_contract::{plan,unqualified}` implements the full public route-plan
  parser (default normalization, placements, replica groups, directed edges,
  loopbacks, traffic fractions, provenance and recursive live-field refusal)
  and the strict unqualified record grammar with source error codes. Python's
  integer-first/compensated-float primary sums are pinned, including large-int
  fallback. `route_ready=true` is deliberately refused: only the separate
  physical qualifier may create that authority. Neither parser runs the planner.

- `planner_snapshot::{derive,validate_binding}` rebuilds the complete unsigned
  planner snapshot from independently supplied gossip evidence, including current
  self-membership, calibrated memory/performance, common decode mode, complete
  directed links, model binding and net-of-reservation policy. Positive bytes,
  malformed snapshots and correctly rehashed/rebuilt hostile gossip inputs are
  recorded against the oracle. Signed capacity-profile admission and actual
  planner execution remain separate consumer responsibilities.

- `loaded_proof::authenticate` mirrors the complete source loader-authentication
  function: assignment UUID, generation, components/range, all three alias views,
  normalized runtime and retained runtime identity. Caller binding MUST be held
  independently; never manufacture it from an incoming proof. Runtime-model
  normalization, tensor/probe checks and actual loading remain with L8/L9. This
  is intentionally not a proof-only `schemas::validate` shortcut.
- The mobile facade re-exports the new pure modules, with an executable shared
  type/positive-fixture smoke. Native and wasm compilation cover that facade;
  they do not establish a physical phone or native-node release.

- `router_wire::{encode_frame,decode_frame}` implements the source's four-byte
  big-endian header length, ASCII-canonical header, payload length/digest and all
  eighteen constructor/validation branches, including nested graph, manifest,
  prefill, verification and checkpoint/cutover bodies. `DecodedFrame` retains the
  received constructor input and payload; `encode` fills source defaults and
  returns exact source bytes. No authentication or execution authority is implied.
  The Rust ingress is deliberately limited to bounded finite JSON and rejects
  duplicate keys; Python's permissive duplicate/nonfinite JSON behavior is not
  claimed matched. Omitted infinite hop-expiry defaults decode but cannot encode,
  matching the source's `allow_nan=False` encoder. Existing APIs are unchanged.

- `recovery::validate_as` and the shared schema dispatcher cover the four recovery
  documents plus the privacy-reduced checkpoint projection. They preserve ASCII
  self-digests, private-field rejection and the source's declared-state grammar.
  Checkpoint digests are **form-only** here because the projection omits private
  tokens. No retry bound, authority issuance, durable store, recovery transition,
  cleanup observation or physical gate is implemented by these validators.
- `product::{snapshot,event}`, `explorer::validate` and the shared dispatcher
  cover complete public documents, all entity/A7 attribute branches, reference
  binding and event cursor relationships. `explorer::project_from_validated_snapshot`
  derives exact source-bound rows, endpoint history and reviewed coarse-region
  reductions from independently validated input. This is not a new observation,
  authenticated evidence, a public publisher, or HTTP/session authorization.
- `performance::validate_as` covers all three performance-budget versions, including
  v3 dimension/overall-state relationships. No metric or physical claim is measured.
- `capacity::{parse,validate}` reconstructs safe/interactive/batch limits, ordered
  boundary reasons, normalized floating metrics and the profile digest from source
  observations. Raw ingress is duplicate-free, exact canonical UTF-8 and bounded
  at the source's 256 KiB. Profiles never assert qualification or route readiness.
- `observatory::{snapshot,event}` validates all copied bindings, strict UTC dates,
  freshness containment, provenance pairs and exact semantic conflict groups.
  The recorded Python 3.14 oracle accepts exact `24:00:00` as next-day midnight;
  this spelling is preserved while dates are compared correctly. Trusted-now live
  qualification, publication owner leases, durable generations and SSE are not ported.
- `projection::validate` preserves the evidence envelope's source/freshness and
  privacy rules. `projection::history(records, capability)` is a pure **derivation**
  of the output-only historical registry: immutable records, conflicting-ID refusal,
  canonical deduplication, 128-unique-record bound, source-time sorting and filtering.
  It does not invent an isolated history validator or call a live runtime source.
- `promotion::parse` returns the source-normalized candidate report after rebuilding
  medians, nearest-rank p95, member/stage maxima and ordered decision reasons from
  observations and a v1 budget. Neither supplied samples nor a matching report
  authorize promotion or establish an actual canary/measurement.
- `public::{activation,swarm,bootstrap}` and `replica::validate_as` preserve the
  browser/replica shape boundaries; `schemas::{validate,validate_as}` dispatches
  them. The bootstrap oracle is its pinned Draft 2020-12 primary schema, so
  integral JSON floats are accepted in its `integer` field. This is not session,
  CSRF, HTTP endpoint, benchmark or runtime authorization.
- `replica::qualification_digest` preserves the placeholder-based self-binding
  digest. A matching self-digest is not an owner signature or admission decision.
- `provisioning::manual_route` validates the open manual route contract, not
  Planner v2. `provisioning::authorization(operation, decision, now_unix_ms)`
  derives the exact preparation record and binding digests from caller-owned
  current catalog/feasibility evidence and owner decision. It does not validate
  an isolated authorization record or download/stage anything. The input clock
  is explicit; no time policy is selected here.
- `consent::caps` and `consent::standing_admission` preserve the pure standing
  predicates, including refusal precedence and the device-memory ceiling. They
  are **unregistered**, do not increase the coverage numerator, and do not persist grants, handle
  withdrawal, acquire membership locks, observe resources or admit mailbox work.
  `Ok(None)` means only that the supplied context passes that source predicate.

All changes are additive to the public APIs consumed by L6/L10. The added schema
branches accept previously unsupported identities; no prior type/signature was
removed. Whole-document replacement, every-field deletion/type mutations, numeric
bounds, digest rebinding, privacy depth/node/width bounds, authority refusals and
positive canonical outputs are recorded as bounded external corpora. These do
not prove exhaustive input coverage or consumer lifecycle/physical behavior.

## Integration

Root workspace/lockfile, budgets and contract authority manifests are unchanged.
The integrator must reconcile the separately supplied lockfile proposal before
running locked builds directly in this lane. Validation used a source-identical
private mirror with that proposal, unchanged lint policy, offline dependencies,
two jobs and low priority. The continuation gates include native tests, workspace
format check, all-target Clippy with warnings denied, xtask lint and pure wasm check.

## Forwarding retirement and authority boundaries

`progress::verify_response` authenticates the owner response and binds request,
member, command, grant, receipt sequence and receipt digest. `VerifiedDecision`
preserves the signed envelope. `PendingForward::record` persists the exact pending
prefix and decision before making it visible; an identical retirement retry is
idempotent. A cancelled decision exposes **no native eviction ACK**, does not set
terminal, and only unblocks ordinary command delivery for a separately validated
`executed` or `reconciliation_required` claim. Bare HTTP refusal is still fatal;
`stale_infer_decode_generation` remains a native failure, not success.

`control::authenticate_request` also accepts the closed token-progress request
schema. `progress::retirement_subject` is only the pure owner-side claim/subject
predicate: its `RetirementContext` must be supplied by a trusted owner under the
mailbox/member locks **after** outer request authentication. Its returned fields
are not a signed response or execution authority. Only explicit owner cancellation
may mark a registration; ordinary close, HTTP failure, expiry and owner restart
must not reconstruct a tombstone. L6 must implement bounded ephemeral tombstones,
atomic replay/auth recording, persistence/reopen, reader closure and cleanup IO.

`seed_transition::verify` checks both old/new signatures and an optional trusted
old-key digest. It preserves the oracle's numeric conversions (including Unicode
decimals) and does not invent an overlap-expiry check: the source validates `now`
but leaves overlap admission to the caller. Path manifest parsing likewise does
not invent reservation-expiry checks. Its graph-binding helper requires an
independently validated graph. `execution_graph::parse` now provides the pinned
parser/structural validator and a detached `Graph` value, but does not authenticate
the graph, check reservation freshness or install a route.

The live Python branch may advance independently. Passing pure tests is not
foundation exit, real Iroh/device qualification or parity to an unreviewed newer
oracle. The `demo_target.py` discovery-read fix and owner-run cancellation
finalizer remain orchestration work for their consuming lanes.
