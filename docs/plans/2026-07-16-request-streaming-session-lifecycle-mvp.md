# User Request, Streaming, and Session Lifecycle MVP Implementation Plan

> **For Hermes:** Use strict TDD and implement this plan task-by-task. Keep heartbeat/dropout recovery in the separate fault-tolerance track.

**Goal:** Provide a usable local inference API that accepts chat requests, streams generated text to the originating client, safely retains reusable KV state between recent turns, and deterministically evicts idle or abandoned requests and sessions.

**Architecture:** Keep request/session policy in Router core because Entry owns request identity, locked paths, attempts, client routing, and reservation cleanup. Keep physical KV allocation/release in Runtime adapters. Add a thin loopback HTTP/SSE adapter around Router ports; do not create another project or couple HTTP into Router core.

**Tech stack:** Python 3.11+, stdlib core, `unittest`, `ThreadingHTTPServer`, Server-Sent Events, existing versioned Router wire framing.

---

## 1. Ground truth and current claim boundary

Verified on 2026-07-16:

- Router-focused compilation passes.
- 88 Router tests pass.
- Distributed progressive prefill and locked decode work across three Router instances.
- Opt-in chunked prefill works in-process and over real loopback TCP.
- Entry remains `LOCKED` until every prompt chunk completes.
- Final-stage `TokenEvent` already routes back to Entry.
- Entry already calls an injected `client_sink.emit(token_index, token_id)`.
- No HTTP request ingress exists.
- No text detokenization adapter exists.
- No real client-stream sink exists.
- No persistent user-session model exists.
- Completed requests currently release reservations and cancel runtime state immediately.
- No idle-request or idle-session KV eviction exists.
- No heartbeat, liveness detector, layer duplication, or distributed dropout integration exists in this Router tree.
- Loopback TCP is same-process, bound to `127.0.0.1`, and unauthenticated.
- Project snapshot has no Git metadata; verification artifacts, not commits, are the checkpoint mechanism here.

This plan does not claim a production multi-host service. It builds the complete local MVP user path and the contracts needed by later real Runtime and fault-tolerance integrations.

## 2. Product decisions locked for this MVP

### 2.1 Component ownership

Create session lifecycle inside the Router package:

```text
mycelium_router/
  sessions.py                 NEW: session state, deadlines, sweeps, eviction decisions
  streaming.py                NEW: bounded stream events and sink lifecycle
  adapters/
    __init__.py               NEW
    http_sse.py               NEW: loopback HTTP/SSE ingress and egress
```

Do not create a separate repository or daemon for session eviction in MVP.

Reason:

- Router Entry already owns request/path lifecycle.
- Eviction must be atomic with cancellation, reservation release, stale-event rejection, and stream closure.
- A separate service would require consensus or durable ownership transfer before it could safely evict distributed KV.
- `sessions.py` remains transport/runtime agnostic, so extraction later remains possible if scale requires it.

Ownership split:

```text
HTTP/SSE adapter
  owns: JSON validation, connection, SSE encoding, disconnect detection

EntryCoordinator + SessionLifecycleManager
  owns: request/session state, activity timestamps, expiry decision,
        path ownership, eviction sequence, stale event rejection

RelayEngine
  owns: local path registration and local release-message handling

RuntimePort implementation
  owns: model execution, physical KV handles, idempotent KV deletion

CapacityPort implementation
  owns: KV-byte reservation, commit, release accounting

Fault-tolerance track
  owns: heartbeat, device liveness, layer replicas, dropout rerouting/recompute
```

### 2.2 Request and session identity

- One `session_id` identifies reusable conversational KV state.
- One `request_id` identifies one user turn/generation.
- Client may omit `session_id` to create a new session.
- Client must send the complete chat `messages` array on every turn.
- Server tokenizes the complete chat but sends only the verified suffix when prior KV is reusable.
- Session stores only token count, prefix digest, model/deployment identity, path ownership, and timestamps in Router metadata.
- Server does not retain plaintext messages after active request completion.
- If KV expired, the full submitted messages permit transparent full prefill rebuild.
- If model/deployment epoch changed, prior KV is invalid and full prefill rebuild is mandatory.

### 2.3 Default lifecycle policy

Add conservative configurable defaults to `RouterConfig`:

```python
request_idle_timeout_seconds = 120.0
session_idle_ttl_seconds = 900.0
session_max_lifetime_seconds = 14_400.0
session_eviction_tombstone_seconds = 60.0
maximum_retained_sessions = 1_024
stream_queue_max_events = 64
stream_queue_put_timeout_seconds = 1.0
```

Semantics:

- Active generation is not evicted merely because the session TTL elapsed.
- An active request with no progress, no heartbeat from its source connection, and no token/hop activity for `request_idle_timeout_seconds` is cancelled and evicted.
- A completed turn moves its session to `IDLE`; reusable KV remains until idle TTL, max lifetime, explicit deletion, model epoch mismatch, capacity pressure, or process shutdown.
- On capacity pressure, evict least-recently-used `IDLE` sessions first.
- Never evict an actively decoding session to admit an idle-cache reuse optimization.
- Client disconnect during active generation cancels the request and evicts its session in MVP. This is safer than guessing whether distributed KV reflects the last emitted token.

### 2.4 Streaming protocol

Implement a local OpenAI-shaped endpoint:

```text
POST   /v1/chat/completions
DELETE /v1/requests/{request_id}
DELETE /v1/sessions/{session_id}
GET    /healthz
```

MVP request body:

```json
{
  "model": "deployment-id",
  "messages": [
    {"role": "user", "content": "Hello"}
  ],
  "stream": true,
  "max_tokens": 64,
  "session_id": null,
  "qos_class": "interactive"
}
```

MVP response headers:

```text
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
X-Request-Id: <request_id>
X-Session-Id: <session_id>
```

MVP SSE sequence:

```text
data: {"id":"<request>","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"<request>","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"<request>","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]

```

After headers begin, failures use a typed SSE error event followed by connection close. Before headers begin, use normal HTTP status codes.

### 2.5 HTTP status mapping

- `400` malformed JSON, invalid role/content, unsupported non-stream request, invalid limits.
- `404` unknown request/session for explicit deletion.
- `409` request idempotency conflict or session currently has another active turn.
- `410` explicitly supplied session tombstone is still visible and cannot be resumed.
- `413` request or prompt exceeds configured bounds.
- `429` Router queue/backpressure rejection; include `Retry-After`.
- `503` no feasible path, runtime unavailable, or deployment not ready.

### 2.6 Privacy and logging invariants

- Bind API to `127.0.0.1` by default.
- Never log message content, prompt token IDs, generated token IDs, activation bytes, SSE text, or bearer tokens.
- Log only request/session opaque IDs, model/deployment, state, counts, durations, and stable reason codes.
- Session metadata stores a SHA-256 prefix fingerprint, not plaintext.
- Tombstones store only opaque IDs, expiry, and reason.
- Explicit session deletion must be idempotent.

## 3. Core lifecycle model

### 3.1 Session states

```text
CREATING -> PREFILLING -> GENERATING -> IDLE
    |           |             |          |
    +-----------+-------------+----------+-> EVICTING -> EVICTED
                              |
                              +-> FAILED
```

`EVICTED` is represented by a short-lived tombstone, not a retained full record.

### 3.2 Per-turn request states

Preserve current Router request state machine for execution, but separate terminal request state from retained session state:

```text
ADMITTING -> PREFILL -> LOCKED -> DECODING -> COMPLETED
     |          |         |          |
     +----------+---------+----------+-> FAILED | CANCELLED
```

A completed request may leave its parent session `IDLE` with KV retained. Request state is not reused for the next turn.

### 3.3 Activity timestamps

Update session/request activity on:

- successful admission;
- accepted prefill chunk;
- manifest lock;
- accepted decode hop;
- accepted token event;
- successful stream enqueue;
- client source liveness signal;
- follow-up request admission;
- explicit cancellation/deletion.

Do not update activity for stale, duplicate, malformed, or rejected events.

### 3.4 Eviction sequence

Eviction must be ordered and idempotent:

1. Atomically transition session to `EVICTING`.
2. Detach or fail the client stream so no new output is accepted.
3. Cancel active request work if present.
4. Send path-scoped `KvRelease` to every participating Router.
5. Each Relay invokes idempotent `RuntimePort.release_kv(...)` for its local placement.
6. Release capacity reservations.
7. Release Relay path/idempotency/scheduler state.
8. Remove request-to-session and path-to-session indexes.
9. Drop token metadata and sink references.
10. Create bounded short-lived tombstone with stable reason.
11. Transition logical state to `EVICTED`.

Late token, chunk-completion, or failure messages after step 1 must be rejected without client output or resource resurrection.

### 3.5 KV reuse gate

Reuse prior KV only when all conditions hold:

- session state is `IDLE`;
- idle and maximum lifetime deadlines remain live;
- model ID, deployment ID, deployment epoch, tokenizer ID, and prompt-template ID match;
- submitted complete-context token count is at least prior context count;
- SHA-256 of submitted first `prior_context_token_count` token IDs equals stored prefix fingerprint;
- locked path placements remain registered and runtime reports KV handle live;
- no fault-track invalidation marked the path stale.

Otherwise evict stale KV and perform full prefill. Never silently reuse on an unverified prefix.

## 4. Streaming model

### 4.1 Internal stream events

Create immutable internal events independent of SSE:

```python
StreamOpened(request_id, session_id, model_id)
StreamDelta(request_id, token_index, token_id, text)
StreamFinished(request_id, finish_reason, usage)
StreamFailed(request_id, code, message, retryable)
```

`EntryCoordinator` emits internal events. `HttpSseSink` encodes them. Future CLI, WebSocket, or gateway adapters can consume the same events without changing Router core.

### 4.2 Bounded producer/consumer behavior

Do not write socket bytes from the final Router callback thread.

```text
Router generation worker -> BoundedStreamingSink queue -> HTTP handler -> SSE socket
```

- Queue is bounded by event count.
- Producer waits at most `stream_queue_put_timeout_seconds`.
- Timeout means `slow_consumer`; cancel request and evict session.
- HTTP handler detects broken pipe/reset, marks source disconnected, and triggers cancellation.
- Exactly one terminal event is permitted.
- No token may be emitted after terminal event.
- Duplicate `TokenEvent` remains filtered before stream enqueue.

### 4.3 Tokenization boundary

Add ports rather than importing a backend tokenizer into core:

```python
class TokenizerPort(Protocol):
   def encode_chat(self, messages, *, model_id: str) -> TokenizedChat: ...
   def decode_incremental(self, token_ids, *, state) -> DecodedDelta: ...
```

Runtime/model adapter supplies implementation. Tests use deterministic fake tokenizer.

The Router routes token IDs; user adapter streams text. Incremental decoder must handle tokens that produce empty text or complete UTF-8 only after multiple tokens.

## 5. Bite-sized TDD implementation sequence

### Task 1: Reconcile current chunked-prefill documentation

**Objective:** Finish the interrupted verification slice before adding new lifecycle behavior.

**Files:**
- Modify: `ROUTER_HANDOVER.md`
- Modify: `docs/plans/2026-07-15-router-next-steps.md`
- Test: `test_router_docs.py`

**Steps:**
1. Require `88 Router tests`, opt-in chunked prefill, and `prefill_chunk_size_tokens` in documentation test.
2. Run the documentation test and observe RED on stale plan wording.
3. Update next-steps status, Task 19, verification command, and next-sprint text.
4. Run `python3 -m unittest -v test_router_docs` and expect PASS.

### Task 2: Add session and stream contracts

**Objective:** Define stable identities and events without changing execution behavior.

**Files:**
- Modify: `mycelium_router/contracts.py`
- Modify: `mycelium_router/ports.py`
- Test: `test_router_session_contracts.py` (NEW)

**TDD behaviors:**
1. Session/request IDs are non-empty and distinct concepts.
2. Session deadline fields reject negative/non-finite values.
3. Stream terminal event can occur exactly once.
4. Usage counts reject negative values.
5. Existing RequestContext decoding remains compatible.

**Gate:** Existing 88 tests plus new contract tests pass.

### Task 3: Implement deterministic session registry

**Objective:** Track active, idle, evicting, and tombstoned sessions with no background thread.

**Files:**
- Create: `mycelium_router/sessions.py`
- Modify: `mycelium_router/contracts.py`
- Test: `test_router_sessions.py` (NEW)

**TDD behaviors:**
1. Create and retrieve session.
2. Reject concurrent active turn for one session.
3. Complete turn transitions session to `IDLE`.
4. Activity extends idle deadline but not maximum lifetime.
5. Stale/duplicate activity does not revive `EVICTING` or `EVICTED` session.
6. Tombstone storage is TTL- and size-bounded.

**Gate:** registry decisions depend only on injected monotonic clock.

### Task 4: Add idle and capacity-pressure eviction selection

**Objective:** Select safe eviction candidates deterministically.

**Files:**
- Modify: `mycelium_router/sessions.py`
- Modify: `mycelium_router/contracts.py`
- Test: `test_router_session_eviction_policy.py` (NEW)

**TDD behaviors:**
1. Idle TTL selects expired `IDLE` sessions.
2. Maximum lifetime selects old `IDLE` sessions.
3. Active generation is never selected by normal idle sweep.
4. Stalled request timeout selects active request for cancellation.
5. Capacity pressure selects least-recently-used idle session first.
6. Tie breaks by opaque session ID for deterministic tests.
7. Sweep is idempotent.

### Task 5: Add runtime KV-release contract

**Objective:** Let Router request physical KV deletion without knowing backend internals.

**Files:**
- Modify: `mycelium_router/contracts.py`
- Modify: `mycelium_router/ports.py`
- Modify: `mycelium_router/fakes.py`
- Test: `test_router_kv_release.py` (NEW)

**Contract sketch:**

```python
@dataclass(frozen=True)
class KvRelease:
   session_id: str
   request_id: str
   path_id: str
   path_attempt: int
   placement_id: str
   reason: str

class RuntimePort(Protocol):
   def release_kv(self, release: KvRelease) -> None: ...
```

**TDD behaviors:**
1. Release is path-, attempt-, session-, and placement-scoped.
2. Duplicate release is idempotent.
3. Old-attempt release cannot delete new-attempt KV.
4. Fake runtime proves one physical release per local placement.

### Task 6: Add distributed KV-release control frame

**Objective:** Deliver release to every path participant over existing transports.

**Files:**
- Modify: `mycelium_router/wire.py`
- Modify: `mycelium_router/relay.py`
- Modify: `mycelium_router/router.py`
- Modify: `mycelium_router/fakes.py`
- Modify: `mycelium_router/transports/loopback_socket.py`
- Test: `test_router_kv_release_wire.py` (NEW)

**TDD behaviors:**
1. `KvRelease` exact wire round-trip.
2. Unknown/malformed release fails closed.
3. In-process mesh reaches every path participant once.
4. Loopback TCP reaches every path participant once.
5. Release contains no prompt/token payload.

### Task 7: Integrate atomic session eviction into Entry

**Objective:** Connect policy decision to cancellation, KV deletion, capacity release, and cleanup.

**Files:**
- Modify: `mycelium_router/entry.py`
- Modify: `mycelium_router/router.py`
- Modify: `mycelium_router/relay.py`
- Test: `test_router_session_eviction.py` (NEW)

**TDD behaviors:**
1. Completed turn retains session/KV when configured.
2. Idle eviction releases each Runtime and capacity reservation once.
3. Explicit deletion uses same sequence.
4. Stalled active request cancels before release.
5. Late token/chunk/failure is ignored after `EVICTING`.
6. Cleanup removes sink and token references.
7. Repeated eviction returns existing result without side effects.

### Task 8: Add safe KV prefix-reuse validation

**Objective:** Reuse cached prefix only when submitted full context proves exact extension.

**Files:**
- Modify: `mycelium_router/sessions.py`
- Modify: `mycelium_router/entry.py`
- Create: `mycelium_router/fingerprints.py`
- Test: `test_router_session_reuse.py` (NEW)

**TDD behaviors:**
1. Exact token-prefix digest permits suffix-only prefill.
2. Changed prior user or assistant message forces full prefill.
3. Tokenizer/template/model/deployment/epoch mismatch forces full prefill.
4. Expired session performs full prefill with same public session ID or returns a new ID according to adapter policy.
5. Digest comparison is constant-time.
6. Router metadata does not retain plaintext messages.

**MVP recommendation:** Preserve public `session_id` after transparent rebuild unless explicit deletion produced a live tombstone.

### Task 9: Add stream event state machine and bounded sink

**Objective:** Decouple generation callbacks from client socket writes.

**Files:**
- Create: `mycelium_router/streaming.py`
- Modify: `mycelium_router/contracts.py`
- Modify: `mycelium_router/ports.py`
- Test: `test_router_streaming.py` (NEW)

**TDD behaviors:**
1. Open → zero or more deltas → exactly one finish/fail.
2. Delta after terminal is rejected.
3. Duplicate terminal is idempotent.
4. Queue never exceeds configured bound.
5. Slow consumer timeout emits stable `slow_consumer` reason.
6. Disconnect wakes blocked producer and cancellation callback fires once.

### Task 10: Add tokenizer/detokenizer port and fake

**Objective:** Convert API messages to token IDs and token events to text without backend coupling.

**Files:**
- Modify: `mycelium_router/ports.py`
- Modify: `mycelium_router/fakes.py`
- Test: `test_router_tokenizer_port.py` (NEW)

**TDD behaviors:**
1. Chat roles validate deterministically.
2. Full prompt token count is bounded before admission.
3. Incremental decode may buffer incomplete text.
4. Empty text delta is not sent as a user-visible SSE chunk.
5. Usage counts remain token-based and exact.

### Task 11: Stream Entry token events through internal sink

**Objective:** Replace direct two-argument sink call with typed stream lifecycle while retaining compatibility adapter.

**Files:**
- Modify: `mycelium_router/entry.py`
- Modify: `mycelium_router/router.py`
- Modify: `mycelium_router/fakes.py`
- Test: `test_router_entry_stream.py` (NEW)

**TDD behaviors:**
1. Each accepted token event emits one ordered text delta.
2. Duplicate/stale token event emits nothing.
3. Final token emits finish and usage exactly once.
4. Cancel emits `cancelled` terminal state.
5. Runtime failure emits typed failure terminal state.
6. Existing `InMemoryClientSink` tests remain compatible through adapter.

### Task 12: Build strict HTTP request parser

**Objective:** Validate user input before starting SSE or allocating Router resources.

**Files:**
- Create: `mycelium_router/adapters/__init__.py`
- Create: `mycelium_router/adapters/http_sse.py`
- Test: `test_router_http_request.py` (NEW)

**TDD behaviors:**
1. Accept supported chat roles and non-empty content.
2. Reject malformed JSON and unknown fields that alter semantics.
3. Reject `stream=false` in initial MVP.
4. Bound body bytes, message count, prompt tokens, and max output tokens.
5. Map interactive/batch QoS explicitly.
6. Never place content in exception/log string.

### Task 13: Implement SSE encoder

**Objective:** Produce OpenAI-shaped, flushable, no-content-log stream frames.

**Files:**
- Modify: `mycelium_router/adapters/http_sse.py`
- Test: `test_router_sse_encoding.py` (NEW)

**TDD behaviors:**
1. Role frame emitted first.
2. Text deltas preserve order and JSON escaping.
3. Finish frame precedes `[DONE]`.
4. Exactly one `[DONE]` on success.
5. Typed error frame after headers on failure.
6. No token IDs or prompt content enter logs.

### Task 14: Implement loopback HTTP/SSE server

**Objective:** Complete one real user request from HTTP body to streamed text.

**Files:**
- Modify: `mycelium_router/adapters/http_sse.py`
- Create: `test_router_http_sse_integration.py` (NEW)

**TDD behaviors:**
1. Server binds only `127.0.0.1` by default.
2. `/healthz` reports process/readiness separately.
3. Chat request streams role, token deltas, finish, and `[DONE]`.
4. Headers expose opaque request/session IDs.
5. Client disconnect cancels and evicts session once.
6. Slow consumer cannot grow memory unbounded.
7. Backpressure maps to `429` plus `Retry-After` before headers.
8. No feasible route maps to `503` before headers.

### Task 15: Add request and session deletion endpoints

**Objective:** Give users explicit stop/delete controls.

**Files:**
- Modify: `mycelium_router/adapters/http_sse.py`
- Modify: `mycelium_router/router.py`
- Test: `test_router_http_cancellation.py` (NEW)

**TDD behaviors:**
1. Delete active request cancels generation and closes stream.
2. Delete idle session releases distributed KV.
3. Duplicate delete is idempotent.
4. Unknown opaque ID returns `404` without revealing neighboring IDs.
5. Live tombstone returns stable response without resource resurrection.

### Task 16: Add deterministic sweep runner

**Objective:** Run lifecycle maintenance without embedding a thread in core policy.

**Files:**
- Create: `mycelium_router/maintenance.py`
- Modify: `mycelium_router/adapters/http_sse.py`
- Test: `test_router_maintenance.py` (NEW)

**TDD behaviors:**
1. `sweep(now)` is directly testable and deterministic.
2. Host runner invokes sweep at configured interval.
3. Shutdown stops runner and evicts retained sessions.
4. Sweep exception does not kill HTTP server; readiness becomes degraded.
5. Repeated shutdown is safe.

### Task 17: Add end-to-end multi-turn reuse and expiry proof

**Objective:** Prove user-visible behavior across turns, cache reuse, and idle expiry.

**Files:**
- Create: `test_router_user_mvp_end_to_end.py` (NEW)

**Scenario:**
1. Start three Router instances and loopback TCP mesh.
2. Start loopback HTTP/SSE server.
3. Send first five-token prompt with chunk size two.
4. Receive streamed assistant tokens and session ID.
5. Send full second-turn messages with same session ID.
6. Prove only verified suffix is prefetched and path/KV is reused.
7. Advance manual clock past idle TTL.
8. Run sweep; prove each placement releases KV once.
9. Send another full turn; prove transparent full prefill rebuild.
10. Prove no duplicate token and no leaked reservation.

### Task 18: Add failure and usability matrix

**Objective:** Make every user-visible terminal path deterministic.

**Files:**
- Create: `test_router_user_failure_matrix.py` (NEW)
- Update: `ROUTER_HANDOVER.md`

**Matrix:**
- malformed request;
- no capacity;
- queue backpressure;
- runtime failure before first token;
- runtime failure after streamed tokens;
- client disconnect;
- explicit cancel;
- slow consumer;
- session expiry between turns;
- model/deployment epoch change;
- duplicate request/idempotency key;
- late old-attempt token;
- shutdown with active and idle sessions.

**Gate:** Every row has one stable status/reason, one cleanup outcome, and no duplicate terminal event.

## 6. End-of-sprint verification bundle

Run from `/workspace/Projects/mycelium`:

```bash
python3 -m compileall -q mycelium_router
python3 -m unittest discover -s . -p 'test_router_*.py' -v
python3 -m unittest -v \
  test_router_user_mvp_end_to_end \
  test_router_user_failure_matrix \
  test_router_http_sse_integration
```

Repository-wide integration remains a separate gate:

```bash
python3 -m unittest -v
```

Record:

- exact Router test count;
- HTTP/SSE end-to-end duration;
- maximum bounded stream queue depth;
- retained session count before/after sweep;
- runtime KV release count by placement;
- capacity reservations before/after eviction;
- proof that logs contain no prompt, token ID, or streamed text.

## 7. MVP acceptance criteria

A local user can:

1. submit a chat request with one command/client;
2. receive text incrementally without waiting for full completion;
3. cancel by disconnecting or explicit DELETE;
4. continue a recent session and reuse verified KV;
5. continue after expiry by sending full messages and transparently rebuilding KV;
6. explicitly delete a session and release its resources;
7. receive stable overload/unavailable/error responses;
8. trust that idle sessions cannot retain KV indefinitely.

System guarantees:

- bounded request body, queue, session registry, tombstones, and idle KV lifetime;
- no decode before all chunked prefill completes;
- exactly-once accepted token streaming;
- exactly-once terminal stream event;
- idempotent cancellation and KV release;
- stale events cannot revive evicted state;
- active work is not displaced merely to preserve idle cache;
- no plaintext session history retained by Router metadata;
- loopback-only network exposure by default.

## 8. Honest blockers and dependencies

No blocker prevents Tasks 1–7 and 9–16 from being built now against deterministic fakes.

Real production behavior depends on:

1. **Runtime adapter:** Must preserve KV between chunk/turn executions, report cache liveness, and implement scoped idempotent release. Without this, Router can prove orchestration but not real GPU/MLX memory reclamation.
2. **Tokenizer adapter:** Required for real chat text and safe exact-prefix verification. Fake tokenizer is sufficient only for Router tests.
3. **Planner/Layer Builder adapter:** Required to consume live placement output instead of fixtures.
4. **Capacity adapter:** Required for real KV-byte accounting and pressure-triggered eviction.
5. **Fault-tolerance integration:** Heartbeat/dropout/layer duplication remains absent. Its invalidation signal must eventually mark affected sessions non-reusable or trigger replay.
6. **Process-isolated transport/auth:** Required before exposing service beyond loopback or calling it multi-host production.

These dependencies do not require waiting to define and test lifecycle, streaming, SSE, and release contracts.

## 9. Explicitly deferred beyond this MVP

- WebSocket transport; SSE is sufficient for one-way token streaming.
- Non-streaming chat aggregation endpoint.
- Durable sessions across Entry/process restart.
- Cross-Entry session migration or replicated checkpoint owner.
- Multi-tenant quotas, billing, and account database.
- Semantic prefix cache shared across unrelated sessions.
- Paged-attention block allocator implementation.
- Continuous batching and speculative decoding.
- Public internet binding, TLS termination, OAuth, or API-key management.
- Heartbeat, dropout recovery, layer duplication, and in-flight rerouting; separate fault-tolerance track.

## 10. Recommended execution order

Execute in four independently verifiable slices:

```text
Slice A: Tasks 1–6
  contracts + deterministic sessions + distributed KV release

Slice B: Tasks 7–11
  Entry eviction + safe reuse + typed bounded streaming

Slice C: Tasks 12–16
  strict HTTP/SSE usability + cancellation + sweeps

Slice D: Tasks 17–18
  real local end-to-end proof + failure matrix + handover
```

Do not start session reuse before distributed KV release is proven. Do not start the HTTP server before bounded stream/disconnect behavior is proven. Do not call cache eviction complete until Runtime and Capacity fakes both show exactly-once release across all path participants.
