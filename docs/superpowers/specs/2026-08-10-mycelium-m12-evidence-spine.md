# Mycelium M12 unified evidence spine specification

## Objective and claim boundary

Publish one privacy-reduced, read-only, coherent product snapshot/event family that
can explain what Mycelium knows without becoming a second authority. The projector
may read authoritative records; it must not call planner, provisioning, Router/runtime
mutation, shell commands, or qualification mutation.

Only `mycelium_qualification.qualifier:RouteQualificationV1` may assert route readiness.
Missing, stale, conflicting, mixed-binding, cursor-gapped, or unknown-major input
withholds the affected claim. Other coherent sources remain visible with a scoped
degradation notice.

## Identity and independent generations

Every entity has a stable public ID, entity kind, source provenance, binding, and
freshness. The snapshot preserves independent values for:

- deployment ID and epoch;
- route ID and generation;
- Router snapshot generation and topology version;
- membership generation and peer incarnation;
- planner snapshot generation and plan ID;
- assignment/load generation;
- qualification ID and issue/expiry time;
- event cursor; and
- request sequence and path attempt.

The projector never collapses these into one synthetic global generation. Its own
publication generation advances once per atomic complete snapshot.

## Versioned product family

`mycelium.product_snapshot.v1` is a closed JSON object containing protocol,
publication, source states, entities, relations, readiness, notices, and provenance.
`mycelium.product_event.v1` contains protocol, cursor, previous cursor, event kind,
and one complete product snapshot. A complete snapshot is used for initial GET, SSE
replay, freeze/replay, export, and reconnect convergence.

The M12 major has closed schemas for physical devices, directed links, stages, routes,
assignments, artifacts, load proofs, runtime/KV ownership, requests, incidents,
qualifications, and source provenance. A producer must also publish a source state:
an entity kind whose adapter is not yet composed is explicitly `unsupported`, emits no
entity, and cannot contribute readiness. The current physical composition has live
authorities for devices, links, stages, routes, assignments, load proofs, incidents,
qualifications, and source provenance; artifact inventory, runtime/KV ownership, and
request-lifecycle adapters remain M12 completion work. The snapshot publishes
`supported_entity_kinds`. Planner plans/workloads, paths,
replica groups, reservations, and recovery records remain `unsupported` until their
own milestone freezes an additive minor-version extension. Relations use stable typed
IDs; they never embed arbitrary source payloads.

An additive minor extension may add one new allowlisted entity kind and its closed
attribute schema without changing existing meanings. Producers must publish the new
kind in `supported_entity_kinds`; older consumers preserve the snapshot but render the
source as unsupported. Removing or changing a field, enum, authority, or readiness
meaning requires a new major version.

Source state is one of `current`, `stale`, `missing`, `conflict`, `unsupported`, or
`replay`, with observed/valid times, source generation, and an allowlisted stable
reason code. Readiness is an independent matrix of membership, artifacts, runtime,
transport, route challenge, qualification, and product-source freshness.

## Freshness, conflict, and ordering

Each source adapter validates its own protocol and canonical binding before projection.
Records outside their TTL are stale, never silently current. Conflicting records with
the same semantic key are both retained as public claim metadata plus one explicit
conflict; qualification is withheld.

Publication cursors are monotonically increasing safe integers. An event declares its
previous cursor. Duplicate identical events are idempotent; divergent duplicates,
regressions, or gaps fail closed and create a scoped incident. Bounded replay returns
all retained events after `Last-Event-ID`; a cursor older than the replay floor receives
an explicit stale-cursor response and must refetch a complete snapshot.

Slow consumers have bounded queues and are disconnected with a stable reason without
blocking publication. State persistence is privacy-reduced, owner-only, atomic, and
fsynced. Restart restores the last valid snapshot/cursor before accepting new sources.

## Privacy contract

Closed schemas and adversarial tests reject unknown fields and any prompt, completion,
token ID/text, logits, activation/hidden state, KV contents, tensor/model weights,
reservation IDs, endpoint addresses, hostnames/IPs, filesystem paths, credentials,
private keys, invite nonces, bearer tokens, or credential-shaped values.

Allowed runtime/KV data is limited to ownership IDs, bounded counts, byte budgets,
freshness, release reason codes, and public digests. Exports pseudonymize device and
request IDs consistently within one export and contain an explicit fixture/live/replay
source label.

## Backend and gateway

One projector reads immutable copies from membership, Gossip, planner, provisioning,
Router/runtime status, request lifecycle, incident, and qualification adapters. It
validates each source independently, joins only exact bindings, produces one immutable
GraphIR, and atomically publishes through the same-origin product gateway:

- `GET /api/v1/product/snapshot`;
- `GET /api/v1/product/events` using bounded SSE replay; and
- `GET /api/v1/product/export` for the pseudonymized frozen snapshot.

Bootstrap advertises these exact paths and supported protocol majors. Fixture and
replay adapters pass through the same decoder and GraphIR builder as live data.

## Frontend convergence

All eight workspaces consume the same immutable product snapshot:

- Inference: qualified deployment, request/path lifecycle, TTFT activity, history;
- Device Lab: browser/mobile worker evidence, explicitly non-activation eligible;
- Network: physical directed links and logical execution toggles;
- Nodes: durable membership, capability, load, runtime/KV ownership;
- Plans: planner inputs, exclusions, plan/assignment bindings and provenance;
- Readiness: independent readiness matrix and authoritative qualification;
- Incidents: source degradation, conflict, recovery and cursor history; and
- Settings: source status, replay/freeze/export, invite/revocation controls.

Stable layout keys prevent relayout for value-only changes. Timeline, change
highlighting, source/provenance drawers, accessible tables, freeze/replay, and export
must survive section switching and refresh.

## Acceptance gates and budgets

Before implementation, the M12 verification record freezes maximum snapshot/event
bytes, entity/relation counts, replay length, subscriber queue, projection latency,
reconnect time, browser render time, and accessibility thresholds. Unbounded success
claims do not pass.

Tests cover exact schemas, unknown majors/fields, every prohibited privacy field,
staleness, mixed binding, conflicts, cursor duplicates/gaps/regression, replay floor,
slow consumers, restart, partial source loss, and qualification revocation.

The physical UI gate requires one durably identified live run to appear coherently in
Network, Nodes, Plans, Readiness, and Incidents. Disconnect, stale evidence, conflict,
and unknown-major mutations revoke live qualification. All eight routes load directly,
survive refresh, and clearly distinguish fixture, replay, degraded, and live sources.
