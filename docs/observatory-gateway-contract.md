# Read-only Observatory gateway contract

Status: backend transport contract only. This document does not activate the live UI.

## Ownership and claim boundary

This gateway owns two read-only endpoints:

- `GET /v1/observatory/snapshot`
- `GET /v1/observatory/events`

It publishes already-produced, privacy-projected Observatory evidence. It cannot submit inference requests, generate tokens, mutate Router or Gossip state, provision weights, or send browser-to-backend control messages. The gateway does not make the Observatory UI live by itself. A real semantic decoder and explicit deployment configuration remain required.

## Envelope

Every snapshot and SSE event carries one complete envelope:

```json
{
  "protocol": "mycelium.observatory_stream.v1",
  "generation": 1,
  "bundle": {
    "snapshot": {},
    "incidents": [],
    "provisioning": {}
  }
}
```

Rules:

- `protocol` is exactly `mycelium.observatory_stream.v1`.
- `generation` is a non-negative JavaScript-safe integer. A new empty state allocates generation `1` for its first accepted publication.
- `bundle` has exactly three top-level fields: object `snapshot`, array `incidents`, and object `provisioning`.
- Events are complete bundles, never deltas.
- JSON uses UTF-8, sorted object keys, compact separators, and strict finite numbers. Snapshot persistence, snapshot GET, replay, and live events use the same canonical envelope bytes.
- The gateway validates transport structure, safety, and privacy-deny rules. It intentionally does not invent nested Observatory semantics. The UI decoder must validate the nested snapshot, incident, provisioning, provenance, identity, and unknown/conflict rules.

## Publication and durability

`CoherentSnapshotPublisher.publish(bundle)` accepts one complete bundle as a single unit. It never reads independently mutable snapshot, incident, or provisioning components.

Before publication, the publisher:

1. validates the complete object graph;
2. rejects non-JSON values, cycles, unsafe integers, NaN/Infinity, excessive nesting, oversized envelopes, non-ASCII object keys (preventing Unicode-confusable privacy bypasses), prohibited sensitive fields, private-key blocks, and common credential-shaped strings;
3. canonicalizes through strict JSON to detach the input graph;
4. recursively freezes the retained bundle;
5. acquires an advisory process lock;
6. rereads the durable generation and allocates exactly the next generation;
7. writes the complete envelope to a mode-`0600` temporary regular file;
8. `fsync`s the file, atomically replaces the state file, verifies installed file identity, and `fsync`s the parent directory;
9. only then updates current memory, replay state, and subscriber queues.

The durable state file contains the latest complete envelope, not only a counter. Restart therefore restores both generation and the latest snapshot. The replay ring is deliberately bounded and in-memory; after restart, older generations are unavailable and a stale cursor receives the persisted latest full snapshot as a reset-safe event.

Two publisher processes that share one state path serialize generation allocation through `<state filename>.lock`, so they cannot allocate the same generation. Subscriber queues and wakeups remain process-local. A deployment that serves SSE should use one broadcasting publisher process; another process may safely allocate a generation, but it cannot directly wake subscribers owned by the first process.

Default bounds:

- maximum envelope: 2 MiB;
- maximum nesting: 32 levels;
- replay ring: 32 complete envelopes;
- subscribers: 64;
- queued future envelopes per subscriber: 8.

All bounds are constructor inputs. Each must be a positive integer. Slow subscribers are removed when their queue is full; the publisher never grows a subscriber buffer without limit.

## Snapshot endpoint

Authorized response:

```text
HTTP 200
Content-Type: application/json
Cache-Control: no-store

<canonical complete envelope>
```

If no snapshot has been published, the response is `503` with `{"error":"snapshot_unavailable"}`. Error bodies contain stable public codes only; they do not contain state paths, exception strings, or traces.

## SSE endpoint

Authorized response headers include:

```text
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-store
X-Accel-Buffering: no
```

Each snapshot event is:

```text
id: <generation>
event: snapshot
data: <canonical complete envelope JSON>

```

Heartbeat comments are the fixed bounded frame `: heartbeat\n\n`. The configurable interval must be between 0.01 and 60 seconds.

### Cursor and replay rules

The server honors one decimal `Last-Event-ID` header in the JavaScript-safe range:

- no cursor: emit the latest full envelope;
- cursor equals current generation: wait for a newer publication;
- every generation after the cursor remains in the ring: replay all in ascending order;
- retained history has a gap: emit only the latest full envelope;
- cursor is ahead of current generation: emit only the latest full envelope as a reset;
- malformed, duplicate, negative, or oversized cursor: return `400` before opening SSE.

Replay/current capture and subscriber registration occur under the same publisher lock used for in-process publication. A publication therefore lands either in the captured replay or in the subscriber queue; it cannot fall into a snapshot-to-SSE handoff gap. Disconnect and cancellation always unregister the client. A queue-full client is marked `slow_consumer` and removed.

Recommended browser sequence remains subscribe first, then acquire the snapshot, then render the source's newest accepted generation. SSE replay closes the server-side race; generation monotonicity closes stale GET/event races in the consumer.

## Authorization, methods, and browser policy

Authorization denies by default. Deployment code must inject a read policy:

```python
from pathlib import Path
from mycelium_gateway.init import build_observatory_gateway

runtime = build_observatory_gateway(
    state_path=Path("/var/lib/mycelium/observatory-state.json"),
    read_policy=lambda scope: authenticated_read_principal(scope),
)
app = runtime.app

# A producer publishes one already-projected complete bundle.
runtime.publisher.publish(bundle)
```

The policy may be synchronous or asynchronous and must return the literal boolean `True`. Missing policies, false results, and policy exceptions return generic `403` responses.

- `POST`, `PUT`, `PATCH`, and `DELETE` on either endpoint return `405` with `Allow: GET`.
- Unknown paths return generic `404` responses.
- HTTP WebSocket upgrades return `400`.
- ASGI WebSocket scopes close with code `1008`.
- No permissive CORS headers are emitted. Cross-origin access requires a separately reviewed exact-origin deployment layer.
- Responses are `no-store`; there is no cacheable evidence path.

## Privacy boundary

A publisher must perform semantic privacy projection before calling `publish`. The gateway then enforces a second deny layer:

- raw tensors, activations, hidden states, logits, model weights, and state dictionaries;
- prompts, messages, completions, generated text, token arrays, token IDs, and input/output IDs;
- private keys, API keys, access/refresh/bearer/auth tokens, passwords, passphrases, credentials, and secret-labelled fields;
- PEM/OpenSSH private-key blocks and common bearer/API/JWT-shaped credential strings.

Counts, rates, hashes, provenance, public identifiers, and explicitly projected status evidence may be published when the semantic schema permits them. Renaming a secret into an innocuous field is not privacy projection; upstream producers remain responsible for semantic allowlisting. The gateway never logs or returns rejected bundle content.

## Remaining semantic-decoder and deployment decisions

The transport contract is complete, but the UI live decoder still needs explicit answers:

1. What versioned schema owns the nested `snapshot`, `incidents`, and `provisioning` objects?
2. Which nested fields are mandatory, optional, nullable, unknown, or conflict-bearing?
3. Which deployment/model/route identities bind all three bundle components to one coherent evidence generation?
4. Which provenance classes and claim-scope labels are accepted, and how are unsupported future fields handled?
5. What semantic allowlist proves that backend projection removed prompts, token payloads, tensors, weights, and credentials before publication?
6. How does decoder/version negotiation handle a protocol it understands but a nested semantic schema it does not?
7. Which authenticated read policy, state path, process model, reverse-proxy limits, TLS origin, snapshot URL, and event URL does each deployment inject?
8. Is same-origin deployment required, or will an exact-origin CORS layer be separately specified and reviewed?
9. Will one ASGI process own publication and SSE fan-out, or will a future inter-process notification mechanism be added for multi-worker serving?
10. What deployment evidence changes the UI label from static/demo to live without overstating inference, provisioning, or Router readiness?
