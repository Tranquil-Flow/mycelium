# Request streaming and session lifecycle contract

## Status and claim boundary

This document records the stable local request, streaming, and KV-lifecycle contract used by Mycelium Router. It is product documentation, not an implementation plan.

The contract does not claim physical multi-device execution, authenticated cross-network routing, `route_ready=true`, or release readiness. Heartbeat, dropout recovery, replacement-stage replay, and physical qualification remain a separate fault-tolerance track.

## Ownership

The loopback HTTP/SSE adapter owns request parsing, response framing, connection state, and disconnect detection.

`SessionLifecycleManager` owns opaque request/session identities, activity deadlines, path ownership, eviction selection, and stale-event rejection. Entry Router remains authoritative for admission, path attempts, reservations, token ordering, and terminal state.

Runtime adapters own physical model execution and stage-local KV allocation. They must implement idempotent `RuntimePort.release_kv` for the exact deployment, path, attempt, placement, request, and session being released. Capacity adapters own reservation accounting.

## Request and session identity

- `session_id` identifies reusable conversation state.
- `request_id` identifies one generation turn.
- Clients submit the complete chat on every turn.
- Router metadata stores opaque identity, counts, deployment identity, prefix digest, and timestamps; it does not retain plaintext chat after active execution.
- KV reuse requires exact model, deployment epoch, tokenizer, prompt template, path placement, and prefix-digest agreement.
- Any mismatch causes stale KV release and complete chat prefill rather than guessed reuse.

## Streaming

Local streaming uses an OpenAI-shaped endpoint:

```text
POST   /v1/chat/completions
DELETE /v1/requests/{request_id}
DELETE /v1/sessions/{session_id}
GET    /healthz
```

Successful streams use `Content-Type: text/event-stream`. Internal typed events are converted to role, text-delta, terminal, and `[DONE]` frames by the adapter.

Generation workers never write directly to sockets. They enqueue into a bounded stream sink. Queue timeout yields the stable `slow_consumer` reason, cancels active work, and releases the session rather than allowing unbounded memory growth.

Exactly one terminal event is accepted. Late, duplicate, stale-attempt, or post-terminal token events produce no client output.

## Complete chat and KV reuse

The client sends the complete chat for each turn. This permits safe full-prefill reconstruction after expiry or failure. Reuse is allowed only when the submitted token prefix hashes to the stored prefix digest and all deployment/path/runtime identities still match.

A completed request may leave its session idle with stage-local KV retained for a bounded period. An expired, explicitly deleted, failed, disconnected, or invalidated session must release KV on every participating placement before its tombstone expires.

## Ordered eviction

Eviction is idempotent and ordered:

1. fence the session in an evicting state;
2. detach or fail the stream;
3. cancel active request work;
4. send path-scoped KV release to every participating Router;
5. invoke `RuntimePort.release_kv` once per local placement;
6. release capacity reservations and scheduler state;
7. remove request, path, sink, and token indexes;
8. retain only a bounded opaque tombstone.

Old-attempt release cannot delete new-attempt KV. Stale events cannot revive an evicting or evicted session.

## Failure ownership

Request usability and distributed fault tolerance are separate tracks:

- HTTP/SSE maps malformed input, overload, no-route, cancellation, and runtime failures into stable statuses/reasons.
- Production Router owns token ordering, attempt fencing, cancellation, and reservation cleanup.
- The fault-tolerance track owns peer liveness, route replacement, and recomputation-first recovery.
- Recovery replays the original prompt plus committed generated token IDs; KV transfer is not required for MVP.

## Privacy and exposure

- Bind user API to loopback by default.
- Never log chat content, token IDs, activation bytes, streamed text, invite tokens, or bearer capabilities.
- Bound request bytes, message count, output tokens, stream queues, retained sessions, tombstones, and idle KV lifetime.
- Do not expose this local adapter publicly without separate TLS, authentication, origin, and physical-route qualification.
