# Authenticated inference request gateway API contract

Status: versioned local component contract. This document does not claim an accepted physical route, distributed inference, iroh transport use, or production deployment.

## Isolation and authority boundary

The request gateway is implemented only in `mycelium_request_gateway/`. It is a separate ASGI application with a separate bearer credential. The read-only `mycelium_gateway` Observatory application remains unchanged and has no prompt submission, cancellation, or other control method.

Only the qualifier authority may create a `RouteQualificationV1` with `route_ready: true`. Gateway composition therefore requires an injected read-only authority object satisfying:

```python
class QualificationSource(Protocol):
    def current(self) -> RouteQualificationV1 | None: ...
```

`current()` must return the qualifier's already-validated current object, not a gateway-created reconstruction. `None` means the route was dropped. The frozen contract intentionally rejects deserializing `route_ready: true` through `route_qualification_from_dict`; therefore the gateway does not invent a file schema or promote an unqualified document. A deployment still needs a qualifier-owned current-record adapter implementing this exact interface.

## Authentication

`GET /healthz` is the only unauthenticated endpoint and returns only service liveness. Every qualification, submission, stream, and cancellation endpoint requires exactly one matching `Authorization` header using the Bearer scheme. Comparison is constant-time. The credential must be distinct from any Observatory credential.

Responses use `Cache-Control: no-store`. Error responses contain stable public codes only. Request bodies, credentials, exception text, and qualification internals are never logged.

## Endpoints

### `GET /healthz`

```json
{"service":"mycelium-request-gateway","status":"ok"}
```

### `GET /v1/qualification/current`

Returns an allowlisted projection of the authority's current record. It excludes endpoints, process identities, reservations, signatures, probes, certificates, evidence payloads, and private topology details.

```json
{
  "protocol": "mycelium.request_gateway.v1",
  "route_ready": true,
  "evidence_class": "physical_qualification",
  "issued_at_unix_ms": 0,
  "reason_codes": [],
  "binding": {
    "qualification_id": "...",
    "qualification_digest": "sha256:...",
    "deployment_id": "...",
    "deployment_epoch": 1,
    "topology_version": 1,
    "model_id": "...",
    "resolved_commit": "...",
    "manifest_digest": "sha256:...",
    "path_manifest_digest": "sha256:...",
    "stage_load_proof_digests": ["sha256:..."]
  }
}
```

The qualification digest covers the complete canonical `RouteQualificationV1`; the sorted stage-load-proof digest set also makes every admitted placement proof explicit.

### `POST /v1/inference`

Request protocol: `mycelium.request_gateway.v1`.

```json
{
  "protocol": "mycelium.request_gateway.v1",
  "prompt": "private prompt text",
  "max_new_tokens": 64,
  "qualification": {"...": "exact binding returned by current qualification"}
}
```

Bounds:

- prompt: non-empty UTF-8 string, at most 131072 encoded bytes;
- `max_new_tokens`: integer from 1 through 4096;
- no unknown fields;
- request body: at most 262144 bytes.

The gateway admits only when the supplied binding exactly matches the current full qualification. It revalidates the same captured identity before backend start, before every token, and while waiting under backpressure.

Success is `202`:

```json
{
  "request_id": "server-generated-id",
  "stream_path": "/v1/inference/server-generated-id/events",
  "cancel_path": "/v1/inference/server-generated-id"
}
```

Stable qualification rejection codes include:

- `stale_qualification`
- `qualification_mismatch`
- `deployment_epoch_changed`
- `path_changed`
- `route_dropped`
- `readiness_revoked`
- `qualification_unavailable`

### `GET /v1/inference/{request_id}/events`

Response is SSE with `Content-Type: text/event-stream; charset=utf-8`. Every event uses protocol `mycelium.request_event.v1`:

```text
id: 1
event: token
data: {"protocol":"mycelium.request_event.v1","request_id":"...","sequence":1,"text":"A","token_index":0,"type":"token"}

```

Event order is deterministic:

1. exactly one `accepted` event at sequence 0;
2. zero or more `token` events with contiguous token indexes and sequence numbers;
3. exactly one terminal event: `completed`, `cancelled`, or `failed`.

`failed` carries one stable `code`. Token IDs are not exposed by the API; decoded token text is delivered only in the authenticated stream.

#### Resume and disconnect

A client reconnects with one decimal `Last-Event-ID` equal to the last event it fully applied. The stream resumes strictly after that sequence. A successful ASGI send is acknowledged only after `send()` returns. Disconnect closes only the subscription, not inference. The bounded buffer retains the most recently acknowledged event when capacity permits, allowing an immediately in-flight event to be replayed if the client reports the preceding sequence.

Only one live subscriber may attach to a request. Invalid, future, or expired cursors fail closed. Under sustained pressure, acknowledged replay entries may be evicted to preserve the hard memory bound and the terminal slot; a cursor older than that retained window receives `resume_cursor_expired` rather than guessed output. Token production blocks before the bound. The session table is independently bounded; the oldest unattached terminal session is evicted on admission when room is needed.

### `DELETE /v1/inference/{request_id}`

Cancellation is idempotent. First accepted cancellation returns `202` with `status: cancelling`; an already-terminal request returns `200` with `status: terminal`. Backend cancellation and Router capacity/KV cleanup are invoked at most once by the gateway lifecycle.

## Router and cleanup path

`RouterSessionBackend` tokenizes the prompt through an injected production codec, builds the existing `RequestContext`, calls existing `Router.admit`, drives existing `Router.decode_one`, and receives tokens through the existing client sink. It does not edit Router, qualifier, transport, MLX, or KV internals.

Normal completion uses Router's existing completion cleanup. Cancellation, qualification revocation, token-order failure, and backend exceptions use Router's existing cancellation cleanup. Local tests assert one capacity release and one runtime/KV cancellation.

## Privacy and metrics

Default counters are label-free integers only:

- admitted and rejected requests;
- emitted token-event count;
- completed, cancelled, and failed request counts.

Logs and metrics contain no prompt text, decoded token content, token IDs, activations, KV data, credentials, raw private endpoints, exception strings, or private qualification fields. Prompt and captured qualification references are released when the worker terminates.

## Claim boundary

Tests under `tests/request_gateway/` use synthetic qualification and local Router fixtures. They prove local admission, session, streaming, privacy, and cleanup contracts only. They do not prove a physical route, distributed execution, route readiness, transport delivery, or acceptable model output.
