# Mycelium A2 swarm artifact acquisition specification

**Status:** approved implementation target
**Parent plan:** `2026-08-11-mycelium-completion-plan.md` A2
**Protocol family:** `mycelium.swarm_artifact_acquisition.v1`

## 1. Outcome and boundary

A2 lets the Provisioner acquire an assignment-local model stage pack from one or more
authorized swarm sources. The exact model revision, serving representation, tensor
scope, layer range, assignment, feasibility generation, and maximum bytes are frozen
before transfer. A peer can receive only the chunks named by its current grant and can
serve only after complete verification, atomic promotion, load proof, and independent
physical qualification.

Acquisition is not placement, activation, qualification, selection, replication, or
permission to download or convert a model. Cached content is availability evidence;
only the Planner chooses an assignment and only the Provisioner issues a grant. The
browser can authorize an exact representation and request preparation, but never sends
paths, model bytes, peer addresses, grants, or credentials.

This protocol is platform-neutral. A source or recipient is identified by its signed
member identity, eligible runtime capability, and current evidence rather than a device
brand or operating system.

A member may join as the vendor-neutral `artifact_source_https` peer class when it can
run durable membership and the authenticated HTTPS artifact agent but cannot run the
reviewed Router sidecar. That class declares the exact runtime capability
`{runtime_backend: artifact-source, transport: https, activation_protocol: null}`. It
may publish signed inventory and artifact availability, but is permanently ineligible
for model-layer placement, assignment offers, Router activation, or inference claims.
The first Android/Termux host is conformance evidence for this generic class, not a
Pixel-specific product path.

## 2. Authorities

- The owner authorizes one exact `mycelium.model_representation_decision.v1`.
- The Planner owns the current feasible allocation and assignment digests.
- The Provisioner owns manifests, grants, source selection, transfer budgets, progress,
  quarantine, verification, promotion, cancellation, and terminal history.
- Membership authority authenticates source and recipient identities and generations.
- The runtime loader consumes only atomically promoted stage packs.
- The Qualifier alone decides whether the loaded deployment is selectable.

No authority may widen another authority's decision. In particular, an availability
advertisement cannot grant an assignment, a grant cannot change a representation, and a
successful transfer cannot establish route readiness.

A capability-aware allocation produced by the current model-feasibility path is a
Planner-v2 decision and carries the closed membership provenance `planner_v2`. The
specific algorithm name `capability_aware_contiguous_exact_weight_dp` remains visible in
the feasibility report; it is not a separate assignment-authority class and must not be
inserted into the membership provenance enum.

An existing immutable representation may be revalidated against a newer signed swarm
evidence generation without authorizing another conversion. The capacity report must
retain the exact owner-approved representation digest, fixed stage allocation,
assignment-local filenames, and byte bounds, while separately recording that it is a
current-capability validation of an earlier Planner-owned placement. Host-order,
backend, layer-range, model, revision, dtype, quantization, or representation drift
fails closed. The earlier capability evidence and its expiry never become current merely
because the representation decision remains valid.

A planner-v2 deployment may not have an M14 topology projection. For capacity
re-evaluation only, the coordinator derives the current route order from the closed,
contiguous layer intervals in its validated M13 placement projection. The derived order
is bound into the new feasibility evidence and does not claim measured-topology
selection. Every required forward and loopback edge must still be present in the newly
captured, independently signed activation-plane snapshots; a missing, duplicate, or
non-contiguous placement fails closed.

Membership/control endpoint identity and activation-plane Iroh identity are distinct
authorities. A lease renewal proves that the member and its signing generation are
current; it must not replace the planner-authorized activation endpoint frozen into an
assignment. Renewal re-signs that activation endpoint with the current membership
generation, and route configuration separately proves possession of the corresponding
activation key. Conflicting or incomplete activation endpoint records fail closed and
require a new plan rather than silently following the membership endpoint.

When an ordinary product route owns the physical stage process, the durable node agent
may run in `membership_control_only` mode. That mode retains the peer's declared runtime
capability and signed membership identity, joins or resumes through the ordinary seed
protocol, and renews the exact current membership generation, but it does not launch a
second stage process or claim route readiness. The public node-start status must expose
`runtime_ownership: product_route` and omit a child-process identifier. This is a
control-plane/runtime-ownership boundary, not a source-only peer class and not evidence
that the member is loaded, assigned, qualified, or serving. A product route remains
bound to the planner-authorized activation identity, assignment, and membership
generation; starting a control-only agent after that generation expires requires a new
plan and qualification rather than retroactively reviving the old assignment.

A fresh execution of an unchanged prepared representation uses a new bounded run/session
identity. Seed rebinding may rotate that identity and remeasure the run-scoped physical
host and boot identities without rebuilding, converting, quantizing, or changing the
model artifacts, allocation scope, graph, or representation decision. The root
operator-plan run ID and controller run-plan run ID must agree before rotation; malformed
plans or invalid run IDs fail closed. This prevents a restarted product server from
reusing command IDs already consumed by an earlier physical session.

Each distinct preparation attempt also receives a fresh candidate deployment identity,
including an exact warm-cache repeat of the same representation and assignment. The
identity separates run-scoped controller commands, grants, promoted manifests, and
candidate-plan publication so a valid warm proof cannot collide with the earlier cold
candidate. It does not change or weaken the immutable model, representation, assignment,
or owner-decision bindings within either attempt, and it does not make either candidate
selectable without its own load and qualification evidence. A later warm attempt may
carry a new assignment ID and digest because fresh feasibility evidence and a fresh
deployment identity are part of assignment identity. Warm equivalence is therefore
proved by the same recipient placement, layer/component and byte scope, model revision,
and representation digest; it must never reuse an earlier assignment grant.

## 3. Immutable content model

### 3.1 Stage-pack manifest

`mycelium.swarm_stage_pack_manifest.v1` is a closed canonical JSON object containing:

- `protocol`;
- `manifest_id` and `manifest_digest`;
- `model_id`, immutable `model_revision`, and `model_artifact_digest`;
- `source_quantization`, `serving_dtype`, `serving_quantization`, and
  `representation_digest`;
- `owner_decision_digest`, `feasibility_digest`, `evidence_generation`,
  `assignment_id`, `assignment_digest`, and `graph_digest`;
- `recipient_member_id`, `recipient_membership_generation`, `placement_id`,
  `stage_id`, `layer_start`, and `layer_end_exclusive`;
- `component_scope`, a sorted list drawn from `embedding`, `transformer_layers`,
  `final_norm`, `lm_head`, `tokenizer`, and `model_config`;
- `tensor_scope_digest`, `pack_format`, ordered `files`, and `stage_pack_digest`;
- `chunk_size_bytes`, `total_size_bytes`, `merkle_root`, and ordered `chunks`;
- `issued_at_unix_ms`, `expires_at_unix_ms`, and `owner_provenance`.

Each chunk contains exactly `index`, `offset_bytes`, `size_bytes`, `content_digest`, and
`merkle_proof`. A proof is an ordered list of closed `{side, digest}` objects, where
`side` is `left` or `right`; leaves are `SHA-256(0x00 || chunk_digest_bytes)` and branch
nodes are `SHA-256(0x01 || left_bytes || right_bytes)`. An unpaired node is duplicated
at each tree level. Chunks are contiguous, ordered, non-overlapping, cover exactly
`total_size_bytes`, and use SHA-256 content digests. The configured chunk size is frozen
into the manifest; only the final chunk may be smaller. Manifest and Merkle
canonicalization are versioned and tested with compatibility fixtures.

`pack_format` is `mycelium.stage_pack_stream.v1`. The pack stream is the exact
concatenation of the ordered file payloads. Each closed file record contains exactly
`relative_path`, sorted `components`, `offset_bytes`, `size_bytes`, and
`content_digest`. A file may bind more than one component because one immutable tensor
container can contain, for example, both shared embedding and output-head tensors.
Paths are canonical relative POSIX paths with no empty, dot, parent, absolute, symlink,
or platform-specific components; records are unique, contiguous, non-overlapping, and
cover the complete pack. The union of all file component sets must equal the manifest's
component scope. Promotion reconstructs only these files beneath a private
temporary root, verifies every file digest again, fsyncs the tree, and atomically renames
the complete assignment directory. Unknown or unlisted files are never materialized.

The preparation topology carries the exact current signed membership generation for
every assigned recipient. Candidate construction copies those generations into both
the regenerated assignment offers and each stage-pack manifest; it must never invent,
renumber, or default a generation. Missing, non-positive, or internally inconsistent
recipient/peer generations fail candidate construction before an artifact grant or
transport job is created.

Every preparation attempt derives unique local and remote controller staging roots from
its unique candidate route identity. It must never clean, overwrite, or reuse the
staging root of the currently serving route or an earlier candidate. Assignment-local
artifact promotions remain separately content-addressed and are bound into that new
staging root only after acquisition succeeds.

The strict operator-plan parser recognizes the optional closed
`mycelium.controller_prepositioned_artifacts.v1` controller field. It validates an
exact member set and sorted assignment-local destination records with absolute source
paths, positive byte sizes, and SHA-256 digests. Candidate discovery and later
activation must parse the same plan that the staging command already executed; the
artifact binding cannot be stripped merely to make a prepared candidate visible.

The tensor scope must equal the assignment-owned transformer layers plus only the shared
or boundary components required by that stage. A manifest containing an unassigned
layer, an unneeded embedding/head, or a component outside the closed scope is rejected
before any transfer.

When preparation materializes stage-sharded checkpoint files, the sharder must consume
the exact authorized contiguous ranges frozen in the feasibility decision. It must not
fall back to equal-count template ranges when the Planner selected an imbalanced split.
The Provisioner still independently checks every produced container's tensor keys, so a
file that crosses the authorized boundary fails before acquisition even if the runtime
loader could logically ignore the extra tensor.

### 3.2 Availability advertisement

`mycelium.swarm_artifact_availability.v1` is signed, expiring, privacy-reduced evidence
with exactly:

- `protocol`, `advertisement_id`, `source_member_id`, and `membership_generation`;
- `manifest_digest`, sorted `available_chunk_digests`, and `verified_bytes`;
- `max_concurrent_streams`, `max_bytes_per_second`, and `serving_priority`;
- `transfer_health`, one of `healthy`, `degraded`, `paused`, or `unavailable`;
- `observed_at_unix_ms`, `valid_until_unix_ms`, and `signature`.

It contains no local path, host name, IP address, raw transport endpoint, credential,
model byte, tensor name, prompt, output, or private network identity. Expired,
generation-mismatched, unverified, quarantined, or non-member advertisements are not
eligible sources.

`advertisement_id` identifies the signed availability content rather than one lease
refresh. Renewing `observed_at_unix_ms` and `valid_until_unix_ms` while the source,
membership generation, manifest, verified chunks, transfer budget, priority, and health
remain unchanged preserves the ID. Any change to those authority-bearing fields rotates
it. This lets an in-flight chunk receipt remain bound to the availability captured at
admission while the source safely renews a long transfer.

A generic member-side artifact agent emits one private
`mycelium.artifact_availability_bundle.v1` containing exactly `protocol`,
`source_member_id`, `membership_generation`, sorted `advertisements`, and
`published_at_unix_ms`. Each advertisement retains its own source signature; the bundle
is transport material, not a new authority. The coordinator accepts a bundle only from
the matching current member session, verifies every advertisement against that member's
current key and generation, and stores no private source path or credential. Removing,
revoking, or advancing the generation of a member immediately makes its earlier bundle
ineligible. The source endpoint is maintained separately in the private membership
transport projection and is never copied into the public browser contract.

Each source agent owns a private manifest inbox. During preparation the operator
transport may atomically register the exact already-authorized stage-pack manifest in
that inbox; registration contains no model bytes and grants no placement or transfer.
The running agent validates every inbox document, reconciles it against chunks already
present in its content-addressed object store, atomically replaces its in-memory serving
authority, and publishes a fresh signed availability bundle. Unknown, malformed,
expired, duplicate-digest, symlinked, or non-owner inbox content fails closed without
changing the prior serving snapshot. The preparation coordinator waits for the source's
new signed bundle and verifies it through the ordinary membership generation/key path;
it does not author availability on the source's behalf. Browser input cannot name an
inbox, source, path, or endpoint.

Every preparation attempt issues its manifest from the Provisioner's current trusted
clock. A historical operator-plan creation timestamp, candidate build timestamp, sealed
evidence timestamp, or browser timestamp cannot be reused as `issued_at_unix_ms`.
Tests may inject a deterministic clock, but production issuance always evaluates
freshness at the instant the Provisioner begins the attempt. Reusing identical content
under a new current authority does not change its representation or chunk identities.

The Provisioner validates the frozen capability evidence once at preparation-attempt
start. It then gives all stage-pack manifests in that attempt one bounded 15-minute
transport lease issued from that same trusted clock. This transport lease may outlive
the short resource-observation lease because it authorizes only acquisition of the
already approved immutable chunks; it grants neither placement nor activation. Before
publication or serving, the candidate must still pass the ordinary fresh qualification
and activation gates. An attempt that begins after capability evidence expiry fails
closed before any stage-pack manifest is built or registered.

### 3.2.1 Member model inventory

The same generic member runtime may publish one
`mycelium.member_model_inventory.v1` envelope. The envelope contains exactly
`protocol`, `statement`, `signature`, and `verification_key`. Its signed statement
contains exactly `protocol`, `inventory_id`, `member_id`,
`membership_generation`, `observed_at_unix_ms`, `valid_until_unix_ms`, and sorted
`entries`. Each closed entry is a privacy-reduced immutable model description:
model ID, 40-hex revision, artifact digest, discovery state, architecture and
adapter identifiers, checkpoint format, source quantization, layer and byte counts,
file completeness counts, bounded blocker codes, and zero or more exact serving
representation descriptors. It contains no local path, host name, network address,
credential, tensor name, model byte, prompt, or output.

The inventory is signed by the durable member key already bound to the current
membership generation. The coordinator accepts it only against an owner-private
current member authority record containing the member ID, exact generation, and
verification key. Unknown keys, signature failure, expiry, future observations,
generation drift, duplicate identities, unsorted entries, unknown fields, or a
mutable revision fail closed. Removing, revoking, or advancing a member immediately
makes its prior inventory ineligible. An inventory is discovery evidence only; it
does not authorize download, conversion, placement, preparation, loading,
qualification, or selection.

Reconciliation groups entries by `(model_id, revision)`. A coordinator-owned local
entry remains the metadata authority only when every matching member entry has the
same artifact and serving-representation identity. A matching inventory contributes
discovery scope and source count but cannot widen local compatibility. A remote-only
identity is published as `discovered` with
`owner_metadata_reconciliation_required`; it is never sent to the feasibility
planner. Conflicting immutable identities are omitted and reported as a bounded
catalogue discovery blocker. The public catalogue includes aggregate discovery scope,
accepted-member count, rejected-member count, and blocker codes, plus per-entry scope,
member count, reconciliation state, and blockers. It never exposes member keys,
private paths, transport endpoints, or device brands.

### 3.3 Acquisition grant

`mycelium.swarm_artifact_grant.v1` is a closed, signed, single-recipient capability with
exactly:

- `protocol`, `grant_id`, `nonce`, and `provisioner_generation`;
- `recipient_member_id` and `recipient_membership_generation`;
- `manifest_digest`, `assignment_digest`, `representation_digest`, and
  `feasibility_digest`;
- sorted `allowed_chunk_digests`, `maximum_total_bytes`, `maximum_concurrency`, and
  `maximum_bytes_per_second`;
- sorted `authorized_source_member_ids` and `origin_fallback_allowed`;
- `issued_at_unix_ms`, `not_before_unix_ms`, `expires_at_unix_ms`, and `signature`.

The grant is valid only for one active acquisition. It is bound to the current
recipient membership generation, current Provisioner generation, exact manifest, and
exact assignment. Replayed, expired, revoked, substituted, or widened grants fail before
bytes are returned. A source verifies that the requested digest appears in both its
advertisement and the recipient's grant.

The recipient durably consumes the closed `(grant_id, nonce)` pair under the same
one-writer lock before disk reservation or any source request. The private consumed-grant
registry is atomically replaced, rejects malformed or duplicate state, and may prune only
entries whose signed expiry is in the past. A process restart, failed reservation, or
interrupted acquisition therefore cannot make the same grant usable again; recovery uses
a newly issued grant over already verified content.

### 3.4 Authenticated chunk request and source receipt

Every peer range fetch uses a closed, recipient-signed
`mycelium.swarm_artifact_chunk_request.v1` containing exactly `protocol`, `request_id`,
`request_nonce`, the complete signed `grant`, `source_member_id`,
`recipient_member_id`, `recipient_membership_generation`, `manifest_digest`,
`chunk_digest`, `offset_bytes`, `length_bytes`, `issued_at_unix_ms`,
`expires_at_unix_ms`, and `signature`.

The source validates the Provisioner signature and current generation on the embedded
grant, the recipient signature and current membership generation on the request, the
source allow-list, manifest and chunk allow-lists, request time window, and that the
requested range stays within exactly one advertised verified chunk. The source keeps a
bounded expiry-indexed replay set for `(grant_id, request_id, request_nonce)` and rejects
a duplicate. A retry or resume creates a fresh request ID and nonce without widening
the grant.

Every successful response carries a closed, source-signed
`mycelium.swarm_artifact_chunk_receipt.v1` containing exactly `protocol`, `request_id`,
`source_member_id`, `source_membership_generation`, `recipient_member_id`,
`manifest_digest`, `chunk_digest`, `offset_bytes`, `length_bytes`,
`range_content_digest`, `advertisement_id`, `responded_at_unix_ms`, and `signature`.
The recipient verifies that receipt against the expected source identity, current
availability statement, request, returned range bytes, and source membership generation
before the Provisioner counts any transferred byte.

The recipient may backdate a newly signed chunk request by at most five seconds to
absorb bounded host-clock skew. The source still evaluates the request and embedded
grant against its own current clock, requires a future expiry, and claims the unique
request ID and nonce in its replay store; this tolerance cannot extend the grant.

The network endpoint is HTTPS with certificate validation against the operator's
configured swarm transport trust root. Plain HTTP is permitted only for an explicit
loopback-only test server and is never an advertised member endpoint. Application
signatures do not replace transport confidentiality: grants, model bytes, member
requests, or receipts must not cross an unencrypted non-loopback link.
The transport trust root must be an X.509 CA certificate with critical
`basicConstraints=CA:TRUE` and critical `keyUsage` permitting certificate and CRL
signing. Each source certificate must carry critical `basicConstraints=CA:FALSE`,
critical digital-signature and key-encipherment usage, `serverAuth` extended usage,
and a subject alternative name for the exact advertised DNS name or IP address. A
certificate accepted only by a permissive legacy client is not qualified evidence.

Acquisition executes on the assigned recipient member under that member's durable
identity. The coordinator may issue the signed Provisioner grant and deliver a private,
closed acquisition job, but it must not proxy peer bytes through its own cache and then
describe a controller copy as peer acquisition. The member verifies current source
availability, Provisioner authority, TLS, source receipts, assignment scope, and the
promoted pack locally. Controller staging may deliver control documents, but must omit
model bytes already promoted through this recipient-side path and verify their exact
digests before launch.

The handoff into physical controller staging uses one closed
`mycelium.controller_prepositioned_artifacts.v1` document containing exactly
`protocol` and `members`. `members` is keyed by the exact assigned member ID; every
value is a sorted list of closed records containing exactly `destination_path`,
`source_path`, `size_bytes`, and `content_digest`. `source_path` is a private absolute
path to an atomically promoted file on that same member. `destination_path` must equal
one record in the controller's immutable base transfer manifest, must not also be in
that member's coordinator archive, and all coordinator-archive plus prepositioned
destinations must cover the base manifest. The remote member verifies that every source
is a current-user-owned regular file reached without a symlink, verifies its exact size
and digest, copies it into a new private staging root, and verifies the destination
again. The stage acknowledgement and cleanup marker bind both the coordinator archive
digest and canonical preposition document digest. Missing, substituted, stale,
duplicate, extra, or remotely unverifiable records fail before node launch. A
coordinator-local source path, coordinator byte relay, or unverified existing staging
root cannot satisfy this contract.

Recipient execution has one closed success envelope containing exactly `protocol` and
`status`, and one closed failure envelope containing exactly `protocol` and
`reason_code`. The failure envelope may expose only a reason from the A2 public reason
code set. The controller validates canonical encoding and the exact envelope shape
before propagating that reason; malformed output, private exception text, runtime
closure failure, or an unknown reason collapses to `member_artifact_job_execution_failed`.
This preserves the exact fail-closed cause for product evidence without treating remote
stderr or a process exit code as trusted status.

The operator may configure the product preparation service with one private, closed
`mycelium.member_artifact_transport_plan.v1`. It binds a persistent Provisioner signing
identity and generation, swarm TLS trust root, current signed source projections, and a
recipient execution root for each eligible member. Source entries contain the current
member ID/generation, HTTPS endpoint, verification key, a closed operator-control
record, explicit remote interpreter, private manifest inbox, and private
availability-bundle file. The control record is independent of compute placement and is
exactly either owner-local execution or strict-host-key SSH with an explicit target,
port, and owner-private identity file. This lets any POSIX-capable source be provisioned
without adding it to the execution graph or introducing a device-brand path. The SSH
channel carries only bounded argv operations and model bytes already authorized by the
exact stage-pack manifest; credentials never appear in argv, logs, or browser evidence.
Recipient entries contain only the member's private artifact-store, durable
identity, interpreter, reviewed runtime-closure, and job roots. Preparation signs one
short-lived exact grant, stages only the closed job and control documents to the
assigned recipient, and invokes the generic recipient executor there. The plan does not
contain model bytes. The preparer may idempotently install only the manifest-authorized
content-addressed chunks into each configured source before registering that same
manifest; it cannot copy any unassigned tensor or widen the source inventory. A missing
recipient, stale or incomplete
source projection, unverified runtime closure, unavailable HTTPS source, or absent plan
fails preparation explicitly. Updating this private plan changes connectivity, not
placement or qualification authority.

The recipient runtime closure is built from an explicit, deterministic allowlist of the
generic acquisition executor and its local dependencies. Package `__init__.py` files in
that isolated closure are minimal package markers rather than the product packages'
convenience re-export surfaces, so importing one acquisition submodule cannot acquire
unrelated router, qualification, or node-agent authority. The builder hashes every
emitted file, publishes the sorted closed runtime manifest, and MUST pass an isolated
import/entrypoint probe against the emitted root before it can be installed on a member.
Missing transitive imports therefore fail at build time, not as an opaque remote job
failure after a grant is issued.

## 4. Acquisition policy

The Provisioner freezes one closed `mycelium.swarm_artifact_policy.v1` with exactly
`protocol`, `chunk_size_bytes`, `maximum_sources`, `per_source_concurrency`,
`aggregate_concurrency`, `maximum_retries_per_chunk`, `maximum_source_rotations`,
`partial_state_ttl_seconds`, `disk_reserve_bytes`, `per_source_bytes_per_second`,
`aggregate_bytes_per_second`, `serving_traffic_reserve_ratio`,
`multi_source_threshold_bytes`, `minimum_predicted_improvement_ratio`,
`allow_redundant_hedging`, `thermal_classes_allowed`, and `power_classes_allowed`.
These are operator policy values, not hard-coded UI assumptions.

Multi-source acquisition is selected only when all of the following are true:

1. at least two eligible sources have useful missing chunks; their advertised sets may
   overlap or be identical when each member holds a complete replica;
2. missing bytes exceed the frozen threshold;
3. measured directed-link evidence predicts a material improvement after coordination
   and hashing overhead; and
4. the serving-traffic reserve remains satisfied.

Otherwise the Provisioner uses one best eligible source or the approved operator origin.
Critical-path chunks needed to complete the next stage are prioritized first. Rare-chunk
preference is secondary and can never cause unassigned or unsolicited replication.

Serving traffic has priority. Acquisition uses a separate bounded queue and cannot
consume the reserved inference bandwidth, memory, disk, thermal, or power envelope. New
staging pauses or slows before an admitted inference request violates its frozen
activation/goodput budget.

## 5. State machine and public projection

One acquisition follows:

```text
pending
  -> reserving
  -> discovering_sources
  -> transferring
  -> verifying_chunks
  -> verifying_pack
  -> promoting
  -> ready

any non-terminal state -> cancelling -> cancelled
any non-terminal state -> failed
```

`mycelium.swarm_artifact_acquisition.v1` is the closed browser-facing status. It contains:

- `protocol`, `generation`, `acquisition_id`, `state`, and `phase`;
- privacy-safe model/revision/representation, assignment, placement, stage, and layer
  range identifiers;
- `total_bytes`, `cached_verified_bytes`, `transferred_verified_bytes`,
  `missing_bytes`, `quarantined_bytes`, and `duplicate_bytes_prevented`;
- `eligible_source_count`, `active_source_count`, privacy-safe per-source byte counts,
  `origin_bytes`, `aggregate_bytes_per_second`, and `eta_seconds`;
- `chunk_count`, `verified_chunk_count`, `resumed_chunk_count`, and
  `source_rotation_count`;
- `manifest_digest`, `assignment_digest`, `representation_digest`,
  `feasibility_digest`, and `evidence_generation`;
- `promotion_digest`, `reason_code`, `retryable`, `started_at_unix_ms`,
  `updated_at_unix_ms`, and `terminal_at_unix_ms`.

The projection is server-owned, bounded, and contains current work plus terminal
history. Refresh/reconnect reconstructs it from the Provisioner. Private paths,
addresses, grants, nonces, signatures, credentials, tensors, and model bytes are never
projected.

When the recipient Provisioner runs on another member, the controller accepts only its
canonical terminal status returned by the hash-verified runtime over the authenticated
operator channel. It validates that status again, assigns the next controller-ledger
generation without changing the acquisition identity or byte accounting, and durably
appends it to the same ordinary product history used for local recipients. A remote
ready result must therefore survive refresh and be available to the product close gate;
it cannot remain only in a private SSH job directory.

The same-origin `GET /__mycelium/artifacts/acquisitions` endpoint returns the closed
`mycelium.swarm_artifact_acquisition_ledger.v1` object with exactly `protocol`,
`generation`, `current`, and `history`. `current` is either one current acquisition or
`null`; `history` is an oldest-to-newest bounded list of terminal acquisition objects.
The endpoint is read-only. Preparation remains the only browser action that may create
an acquisition, and cancellation is owned by the preparation workflow rather than by a
browser-supplied peer, source, path, or grant.

## 6. Transfer, resume, and duplicate suppression

- The recipient reserves the complete staging and promotion envelope before transfer.
- One manifest/recipient lock prevents concurrent staging of divergent content.
- A verified content-addressed chunk is reused without transfer; a matching partial
  chunk resumes only after prefix integrity verification against a newly authenticated
  authorized source. Bytes re-read solely to establish that prefix are accounted as
  duplicate bytes prevented/revalidated, never as newly transferred verified bytes.
- Every requested range is bounded by one allowed chunk. Sources never expose arbitrary
  file ranges.
- Every non-origin range has a fresh recipient-signed request and source-signed receipt;
  a TLS connection or a valid content hash alone is insufficient authority.
- The scheduler tracks in-flight digests globally for the acquisition. A digest may have
  one winning transfer; redundant hedging requires explicit policy and the loser is
  cancelled before its bytes are counted as useful.
- Interrupted transfers retain only verified chunks and bounded verified partial state.
  Resume can rotate to another authorized source without changing the grant scope.
- If sources disappear, the approved origin is used only when the grant permits it. With
  no authorized source or origin, acquisition stays explicitly failed or pending; it
  never widens its scope or downloads another representation.

## 7. Verification and promotion

Each chunk is hashed while streaming and verified against its content digest and Merkle
proof before it becomes reusable. A mismatch quarantines the received bytes and marks
that source/digest observation unhealthy for the bounded policy interval.

After all chunks verify, the Provisioner reconstructs the pack in a private temporary
root and verifies, in order:

1. total size and stage-pack digest;
2. canonical manifest digest and owner provenance;
3. model ID, revision, artifact digest, and exact serving representation;
4. owner decision, feasibility generation/digest, assignment, graph, member generation,
   placement, layer range, component scope, and tensor scope;
5. available disk after promotion and loader peak-memory envelope.

Only then is the complete directory fsynced and atomically promoted. Partial,
quarantined, expired, cancelled, or mismatched state is never visible to the runtime
loader. Load proof binds the promoted pack digest and does not reuse the acquisition
grant as execution authority.

An operating-system storage failure while reconstructing, fsyncing, or atomically
promoting the pack is a terminal `artifact_storage_failure`. The Provisioner MUST clear
`current`, append the bounded failed acquisition to terminal history, and leave
`promotion_digest` null; an unexpected storage exception MUST NOT strand an acquisition
in `promoting` or collapse to a generic preparation error.

## 8. Failure and quarantine rules

Closed public reason codes cover insufficient disk, stale evidence, authorization
drift, membership drift, representation drift, manifest substitution, assignment scope
violation, grant replay, no authorized source, source disappearance, chunk integrity
failure, pack integrity failure, concurrent staging conflict, budget exhaustion,
thermal/power pause, cancellation, bounded retry exhaustion, and operating-system
artifact storage failure.

Corrupt bytes move to a private bounded quarantine; public evidence exposes only byte
count, reason code, source pseudonym, and time. Quarantine can never be reused as a cache.
Quarantine, verified-prefix duplication, and source-rotation counters accumulated by a
failed final attempt are carried into its terminal status rather than disappearing with
the raised failure. These counters describe observed recipient work even when no chunk
was accepted and no promotion occurred.
Eviction removes expired partials and least-recently-used unassigned verified content
under policy, but never an active route's resident or reserved artifacts.

## 9. Product UI

- **Inference:** only physically qualified deployments appear in the selector. The
  dynamically discovered catalogue is visible beside the inference controls with
  search, lifecycle filters, preparation, and activation actions, so an acquiring,
  prepared, stale, failed, or merely cached model is explainable without becoming
  selectable.
  When an incumbent deployment is already serving, a fresh feasible representation of
  the same immutable model identity may still expose an explicit prepare-replacement
  action. That action must preserve the incumbent route, require the same affirmative
  representation decision, and remain unavailable when capacity evidence is stale,
  provisioning is unauthorized, or an equivalent prepared/active candidate already
  exists.
- **Models/Settings:** show every immutable identity discovered from the coordinator or
  a current signed member inventory, exact representation, discovery scope, lifecycle,
  acquisition policy, owner-decision state, and human-readable blocker. A peer-provided
  identity is not treated as globally available until its metadata identity and
  representation are reconciled with the owner catalogue.
- **Nodes:** show each placement's assigned layers, cached/missing/verified bytes, source
  count, transfer rate, ETA, artifact state, and contribution eligibility.
- **Plans:** show the selected contiguous allocation, representation binding, source
  strategy, redundancy, origin fallback, and serving-traffic reserve.
- **Readiness:** show manifest, authorization, transfer, verification, promotion, load,
  and qualification gates as separate states.
- **Incidents:** show privacy-safe source loss, rotation, resume, corruption, quarantine,
  fallback, cancellation, and terminal acquisition failure.

The frontend never assumes a fixed member count, model count, stage count, source count,
or device brand. Adding a member changes the inventory immediately, but changes model
fit, placement, or selector contents only after fresh evidence, planning, preparation,
load, and qualification.

Inference reconciles the selected deployment registry and qualifier-owned request
binding on model lifecycle events, focus, section changes, visibility return, and a
bounded live poll. A cross-tab or external model switch is shown as a new binding that
the user must accept before submission; stale browser state cannot silently submit to
the previous deployment.

## 10. Verification gates

### Software positive

1. Strict Python/TypeScript decoders and compatibility fixtures reject unknown fields,
   illegal states, privacy-sensitive values, and cross-protocol digest drift.
2. Manifest tests prove exact chunk coverage, Merkle verification, tensor/component
   scope, assignment locality, and representation binding.
3. Scheduling tests prove bounded single- versus multi-source selection, critical-path
   priority, duplicate suppression, source rotation, origin fallback, serving reserve,
   cancellation, and deterministic terminal accounting.
4. Storage tests prove disk reservation, one-writer locks, verified resume, quarantine,
   eviction protection, atomic promotion, and zero duplicate bytes on warm reuse.
5. Refresh tests reconstruct active progress and terminal history from Provisioner state.

### Physical positive

Add one freshly assigned eligible peer. Acquire its stage pack from at least two existing
authorized peers with exact per-source/chunk accounting, no unassigned-layer transfer,
no duplicate origin fetch, full digest verification, promotion, load proof, physical
qualification, and one completed browser inference. Repeat from warm cache with zero
duplicate transferred bytes. An A/B run must show acquisition budgets preserve the
frozen serving latency/goodput envelope.

The close gate is sealed by an owner-private
`mycelium.a2_product_gate.v1` executed artifact. The gate reads the ordinary product
acquisition ledger and live physical status rather than a qualification seam. It binds
one recipient stage to two ordered terminal `ready` acquisitions with the same immutable
model revision, representation, recipient placement, layer interval, and total byte
scope. Each record retains its own exact assignment and feasibility digests, which may
rotate across a fresh capacity decision and candidate deployment. The first record must
be fully cold: zero cached and origin bytes, all bytes
transferred and verified, and positive verified-byte participation from at least two
privacy-safe source references. The second must be fully warm: all bytes cached, zero
transferred, missing, and origin bytes, every chunk resumed, and at least the complete
pack counted as duplicate transfer prevented. Both records must have promotion digests
and the cold record must terminate before the warm record starts.

The same executed artifact submits a fresh prompt through the normal session, CSRF,
qualification-binding, inference, and event-stream endpoints after the fault exercises.
It captures only bounded owner-private request/output evidence and the before/after live
status needed to prove that the route was non-simulated, remained alive and on the same
deployment, had no fatal error, contained the acquired recipient stage, advanced equal
positive physical frame counters globally and for every serving stage in the latest
per-request timing record, and returned a non-empty completed response. The
artifact contains explicit Boolean checks, canonical digests of its source ledger and
live snapshots, and a canonical evidence digest. A failed check withholds A2 closure;
written assertions or an empty ledger cannot substitute for this executed artifact.

### Negative

Exercise corrupt cache, corrupt source bytes, insufficient disk, stale feasibility,
representation/authorization/membership drift, interrupted transfer, source loss,
unauthorized chunk request, unassigned-layer request, replayed grant, manifest
substitution, concurrent staging, cancellation, and no-source/no-origin terminal
failure. Every case fails within its own scope, leaves the incumbent route usable, and
never promotes partial content or widens the assignment.
