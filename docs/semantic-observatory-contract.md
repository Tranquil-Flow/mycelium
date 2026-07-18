# Phase 9 semantic Observatory contract

Status: v1, strict and fail closed.

This contract defines the only public semantic projection accepted by the Phase 9 Observatory gateway and browser live lane. It is an observation surface, not a Router control or transport surface.

## Protocols

- Snapshot: `mycelium.observatory.snapshot.v1`
- Publication/SSE event: `mycelium.observatory.event.v1`

Unknown protocol values, including unknown major versions, are rejected. Objects require exactly the fields listed below; aliases, omissions, and additional fields are rejected. Arrays must be dense JSON arrays. Integers must be non-boolean safe integers.

## Snapshot v1

A snapshot contains exactly:

```text
protocol
snapshot_id
freshness { observed_at, valid_until }
binding { deployment, model, route }
claims[]
conflicts[]
route_challenge
request_lifecycle
provenance
```

`freshness` uses RFC 3339 UTC timestamps. `observed_at` must precede `valid_until`. Every nested freshness interval must remain inside the snapshot interval.

### Exact binding

```text
deployment { id, epoch }
model { id, revision, manifest_digest, num_layers }
route { id, generation, digest, assignments[] }
assignment { id, peer_id, start_layer, end_layer_exclusive }
```

Digests are lowercase `sha256:` values. Assignment IDs are unique. Assignments provide ordered, contiguous, half-open coverage from layer zero through exactly `num_layers`. The route challenge and request lifecycle each carry a complete binding equal to the snapshot binding; partial or approximate matches are invalid.

Public identifiers are bounded opaque IDs. They cannot contain credentials, network addresses, endpoint strings, or filesystem paths.

### Claims and exact provenance

Each claim contains exactly:

```text
id
scope { kind, id }
statement
value
freshness
provenance { kind, producer }
```

Scope, statement, bound identifier, and provenance must match this table:

| Scope | Statement | Allowed bound ID | Required provenance |
| --- | --- | --- | --- |
| `deployment` | `deployment_bound` | deployment ID | `gateway_projection` / `mycelium_gateway` |
| `model` | `model_bound` | model ID | `provisioning_audit` / `mycelium_provisioning` |
| `route` | `route_challenge_succeeded` | route ID | `route_challenge` / `mycelium_router` |
| `assignment` | `assignment_ready` | one exact assignment ID | `provisioning_audit` / `mycelium_provisioning` |
| `request` | `request_lifecycle_observed` | request lifecycle ID | `router_runtime` / `mycelium_router` |

Claim values are `confirmed`, `rejected`, or `unknown`.

### Conflicts

Each conflict contains exactly:

```text
claim_ids[]
scope { kind, id }
reason
```

Reasons are `binding_mismatch`, `value_mismatch`, or `freshness_overlap`. A conflict references at least two unique existing claims. Every referenced claim must have the exact conflict scope. Multiple claims with the same semantic scope and statement are invalid unless one exact conflict names the complete duplicate group. Any conflict blocks live qualification.

### Route challenge and request lifecycle

The route challenge contains exactly `id`, `status`, `freshness`, `binding`, and `provenance`. Status is `succeeded` or `failed`; provenance is exactly `route_challenge` / `mycelium_router`.

The request lifecycle contains exactly `request_id`, `state`, `path_attempt`, `freshness`, `binding`, and `provenance`. State is one of `admitting`, `prefill`, `locked`, `decoding`, `completed`, `failed`, or `cancelled`; provenance is exactly `router_runtime` / `mycelium_router`.

The top-level snapshot provenance is exactly `gateway_projection` / `mycelium_gateway`.

## Event v1

An event contains exactly:

```text
protocol   = mycelium.observatory.event.v1
generation = positive safe integer
snapshot   = one complete snapshot v1
```

Publication JSON is canonical and deterministic (`sort_keys`, compact separators). Generation advances only after strict validation and atomic persistence succeed.

## Privacy boundary

The projection excludes prompts, token IDs, token content, activations, tensors, weights, credentials, raw endpoints, network addresses, filesystem paths, and raw Router frames. Unknown fields fail closed. Validation errors are generic and never echo rejected values. A rejected privacy canary cannot advance generation or reach persistence, snapshot GET, replay, or SSE.

## Publication and SSE ownership

`ObservatoryPublicationOwner` owns semantic validation, durable generation, snapshot GET data, replay, and SSE fan-out for one state path. A non-blocking owner lease rejects a second concurrent semantic owner.

Read surfaces:

- `GET /v1/observatory/snapshot`: latest complete event v1
- `GET /v1/observatory/events`: SSE named `snapshot`, with event ID equal to event generation

Both are read-only, no-store, and deny by default without an explicit read policy. Reconnect uses `Last-Event-ID`; retained complete envelopes are replayed, and replay gaps reset to the latest complete envelope.

## Browser source mode and live gate

Browser mode is explicit:

- `source_mode: fixture`: bundled evidence only; never live-qualified
- `source_mode: live`: same-origin snapshot GET and SSE defaults; strict event decoder only

Production selection uses `VITE_OBSERVATORY_SOURCE_MODE=fixture|live`. Unknown values fail closed. Cross-origin live URLs are rejected.

The UI displays `LIVE · QUALIFIED` only while all conditions hold:

1. source mode is `live`;
2. SSE transport is currently connected;
3. snapshot, route challenge, request lifecycle, and all required claims are current;
4. route challenge status is `succeeded` with exact route binding and provenance;
5. request lifecycle state is `completed` with exact request binding and router-runtime provenance;
6. deployment, model, route, every assignment, and request each have a current confirmed exact-scope claim;
7. `conflicts[]` is empty.

Disconnect, malformed or unknown-major input, generation/SSE-ID mismatch, stale evidence, conflict, failed challenge, or incomplete lifecycle revokes the live label. Recovery requires a strictly newer valid event after a fail-closed transport or validation boundary.

## Claim limit: Phase 6 remains blocking

Phase 9 reports only validated semantic projection state. It does not prove genuine two-process Router execution, native transport, or numerical parity. Current repository fixtures remain `FIXTURE · READ ONLY`. No system-level execution or native-transport claim may be elevated until the separate Phase 6 two-process and parity gates pass with real evidence.
