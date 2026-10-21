# Read-only Observatory event adapter

Status: local integration contract. This adapter does not qualify a route or activate inference.

## Purpose

`mycelium_gateway.event_adapter.ObservatoryEventAdapter` consumes two already-versioned event streams and publishes one privacy-reduced, deterministic bundle through the existing `CoherentSnapshotPublisher`:

- `mycelium.route_qualification.v1`
- `mycelium.request_event.v1`

The resulting publisher envelope remains `mycelium.observatory_stream.v1`. Its bundle contains exactly:

- snapshot protocol `mycelium.observatory.request_projection.v1`;
- incident records with public protocol, source cursor, and safe reason code only;
- status protocol `mycelium.observatory.event_adapter_status.v1`.

`route_ready` is always the literal `false`. The adapter does not derive, preserve, or promote a positive readiness claim.

## Read-only composition

```python
from pathlib import Path

from mycelium_gateway.event_adapter import ObservatoryEventAdapter
from mycelium_gateway.init import build_observatory_gateway

runtime = build_observatory_gateway(
    state_path=Path("/var/lib/mycelium/observatory-events.json"),
    read_policy=lambda scope: authenticated_read_principal(scope),
)
adapter = ObservatoryEventAdapter(runtime.publisher)

# A separate observation-only collector supplies canonical event bytes and a
# monotonically increasing source cursor. This call never controls the Router.
outcome = adapter.apply(source_cursor, event_bytes, observed_at_unix_ms=now_ms)
app = runtime.app
```

The only HTTP surfaces remain:

- `GET /v1/observatory/snapshot`
- `GET /v1/observatory/events`

`POST`, `PUT`, `PATCH`, and `DELETE` remain method-not-allowed. The adapter has no prompt submission, cancellation, Router mutation, transport dispatch, qualification-authority, or route-promotion method.

## Ordering, replay, and staleness

Input cursors start at zero and advance exactly by one. Duplicates at or below the applied cursor do not publish again. Gaps, malformed payloads, unknown versions, cross-session events, sequence conflicts, and stale binding/time events are rejected and represented only by bounded safe-code incidents.

Sessions are ordered by request ID. Stage proof digests are ordered by digest. Canonical publisher serialization gives identical projection order for identical accepted input order. Terminal request events transition a session once; replay is consumed without another applied publication, while a conflicting terminal at the same sequence is quarantined.

Qualification age, session idle age, sessions, incidents, input bytes, publisher replay, subscribers, and subscriber queues are all bounded. Active sessions are quarantined when qualification deployment, epoch, path digest, or qualification identity changes. Persisted projections are strictly revalidated before restart resume.

Browser consumption uses `LiveObservatoryEventSource`. It performs a bounded streaming read of the initial same-origin GET body, then consumes named SSE `snapshot` events. Each SSE ID must be the exact decimal publication generation. A lower source cursor, lower observation timestamp, malformed/oversized event, or unknown protocol disconnects the projection. Recovery requires a strictly newer valid generation. Freshness expires after five minutes by default and never changes `route_ready=false`.

## Privacy boundary

Only allowlisted values survive projection: bounded public opaque IDs, SHA-256 references, non-negative counters, timestamps, enum states, booleans fixed by this contract, and safe reason codes.

The default publisher, logs, errors, incidents, and browser state expose no prompt or generated text, token content or IDs, credentials, private endpoints, reservation/process details, activations, tensors, weights, hidden state, or KV data. Identifier-shaped endpoints, IP addresses, local/private hostnames, credential patterns, unknown fields, accessors, and non-JSON values fail closed. Validation errors never echo rejected values.

## Claim boundary and remaining gaps

This lane proves local contract validation, projection, persistence/restart validation, GET/SSE publication, cursor replay, browser resume, staleness, and privacy reduction only. It does not prove physical two-host inference, native transport, authenticated production collection, deployment qualification, numerical parity, Router recovery, or release orchestration. Those remain separate physical/integration gates. `route_ready=false` remains mandatory until an authorized qualification lane supplies evidence to its own control-plane consumer; this read-only adapter will still not promote it.
