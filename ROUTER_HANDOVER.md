# Mycelium Router Handover

## Status

Standalone request and inter-layer Router reference implemented under `mycelium_router/` and hardened through the first distributed execution slices.

The Router traverses an acyclic model-stage graph for one forward pass. The final-stage → stage-0 boundary is an explicit generation-cycle loopback transition, not a normal DAG routing edge. Physical connectivity may still contain cycles.

No whole-system simulator integration was introduced. That adapter remains a later orchestration task.

## Planner and wire contracts

The Router consumes `mycelium.execution_graph.v1`.

- Planner example: `docs/router-protocol-example.json`
- Parser/serializer: `mycelium_router/serialization.py`
- Structural validator: `mycelium_router/validation.py`
- Router wire protocol: `mycelium.router_wire.v1`
- Wire example: `docs/router-wire-example.hex`
- Wire codec: `mycelium_router/wire.py`
- Runtime layer ranges are half-open: `[start_layer, end_layer_exclusive)`.
- Inclusive `end_layer` input is rejected rather than silently converted.

The graph binds deployment epoch, topology version, model commit, manifest digest, assignments, stage signatures, load proofs, legal placement edges, and final-to-stage-0 loopbacks.

The current `layer_planner.py` and `planner_simulator.py` outputs are not silently reinterpreted. Later integration must use a tested adapter into `execution_graph.v1` or emit that contract directly.

## Implemented modules

- `contracts.py` — immutable graph, request, path, hop, failure, reservation, token, progressive-prefill, and chunk-completion contracts.
- `serialization.py` — strict planner graph parsing/serialization.
- `validation.py` — fail-closed graph and locked-manifest validation.
- `scoring.py` — SLA-normalized TTFT/TPOT scoring with transfer serialization and stale-state fallback.
- `routing.py` — progressive DAG construction, reservations, leases, rollback, and atomic manifest lock.
- `leases.py` — lease expiry and deployment-epoch validation.
- `scheduler.py` — device-wide QoS queue with bounded deduplication, deficit boost, starvation-preventing aging, and work/byte backpressure limits.
- `idempotency.py` — canonical attempt-scoped hop idempotency keys.
- `state.py` — fail-closed request, path, and hop state machines.
- `wire.py` — versioned control framing, progressive-prefill and manifest-lock codecs, strict schema checks, and SHA-256 payload integrity.
- `payloads.py` — versioned binary token-ID payloads for the distributed data plane.
- `transports/loopback_socket.py` — local-only loopback TCP transport using Router wire frames.
- `relay.py` — device-local hop execution, forwarding, progressive prefill, failure reporting, and bounded replay caches.
- `entry.py` — admission, pending progressive-prefill ownership, manifest-lock confirmation, decode, client delivery, cancellation, and recovery.
- `router.py` — public composition facade.
- `ports.py` — topology, state, capacity, transport, runtime, clock, ID, and client interfaces.
- `fakes.py` — deterministic in-memory collaborators plus synchronous placement-addressed `InProcessMesh`.

## Behaviors proven by tests

- gap-free half-open stage ranges;
- DAG edges only between adjacent logical stages;
- explicit legal final-to-stage-0 loopback;
- one reserved placement per stage in locked manifests;
- bandwidth serialization can reverse a latency-only route choice;
- stale optimistic state receives conservative fallback values;
- rejected reservations exclude the candidate and rescore;
- accepted but stale-epoch reservations are released before rescoring;
- reservations carry expiry and deployment epoch;
- path commit is all-or-nothing and rolls back on rejection;
- completion, failure, cancellation, and failed path construction release resources;
- idempotency caches and scheduler deduplication are TTL- and size-bounded;
- forwarded headers and work items share one attempt-scoped key schema;
- wire payload length and SHA-256 mismatches fail closed;
- progressive-prefill and locked-manifest state round-trip through the versioned wire format;
- distributed prompt/checkpoint token IDs use a versioned binary payload rather than Python objects;
- unknown wire versions and message types fail closed;
- illegal request/path/hop state transitions fail closed;
- `Router.receive_hop()` executes only the destination Router's local placement;
- stale, duplicate, and misdirected hops do not execute model work;
- runtime failure emits one scoped failure report and does not forward;
- distributed progressive prefill begins with only stage 0 selected by Entry;
- each current Router chooses and reserves the next stage from its own live snapshot;
- each extension emits one manifest delta and forwards once;
- final prefill atomically locks the path and emits lock confirmation;
- opt-in chunked prefill sends the first prompt chunk while building the path, then sends remaining chunks in order over that same locked path;
- `PrefillChunkCompleted` frames carry chunk index and token count across both in-process and loopback TCP transports;
- Entry remains `LOCKED` and rejects decode until every configured prompt chunk completes;
- Entry blocks decode until it accepts that lock confirmation;
- cancellation during distributed progressive prefill releases the partial path;
- duplicate progressive-prefill delivery cannot re-execute or extend twice;
- an in-process mesh automatically broadcasts the locked manifest, resolves placement-to-node delivery, and routes token events to Entry;
- three independent Routers complete `node-a → node-c → node-d` progressive prefill and locked distributed decode;
- every participating runtime executes exactly one assigned decode stage;
- a loopback TCP mesh carries versioned bytes across real OS sockets for three Router instances;
- TCP listeners bind only to `127.0.0.1`, making the MVP adapter explicitly local-only;
- the same-process TCP harness closes each receipt socket before dispatch waits on an in-memory delivery event, bounding active descriptors during long chunked prefill while preserving synchronous error propagation;
- scheduler work-count and payload-byte limits reject overload with retry metadata;
- rejected overload does not poison hop idempotency, and popped work releases byte accounting;
- Relay work carries an exact `RuntimeBatchKey` binding deployment epoch, model/manifest, placement assignment, stage/load proof, backend, phase, activation shape, and token span;
- the scheduler forms deterministic bounded batches around the highest-priority item and fails closed to singleton execution when compatibility identity is absent;
- prefill chunks batch only at equal token spans, while decode positions remain per-member metadata;
- `RuntimePort.execute_batch()` and its deterministic fake preserve one result per member, but no asynchronous collection window or fused backend is claimed;
- legacy decode replays its locked path despite dynamic-state changes;
- recovery rebuilds KV state from prompt plus committed tokens;
- stale token events cannot duplicate client output;
- scheduler boost is capped and aging prevents batch starvation.

Router-focused verification passes 94 Router tests. Python compilation also passes.

Repository-wide discovery must be interpreted separately: Layer Planner tests still depend on modules outside the Router package. The verification section records the current live result rather than treating those failures as Router regressions.

## Verification

Run from `/workspace/Projects/mycelium`:

```bash
python3 -m compileall -q mycelium_router
python3 -m unittest discover -s . -p 'test_router_*.py' -v
python3 -m unittest -v  # repository-wide; currently exposes unrelated Layer Planner blockers
```

## Public API sketches

Single-process compatibility path:

```python
request_id = router.admit(request, client_sink)
router.generate(request_id, token_count=16)
```

Distributed progressive prefill path:

```python
# Zero preserves one-shot prefill; positive values enable ordered prompt chunks.
config = RouterConfig(prefill_chunk_size_tokens=256)
request_id = entry_router.start_distributed_prefill(request, client_sink)

# Transport delivers each HopHeader + ProgressivePrefillContext to its destination.
result = destination_router.receive_progressive_prefill(header, context)

# Final Router emits ManifestLocked; transport returns it to Entry.
entry_router.receive_manifest_locked(result.confirmation)
```

Locked one-device execution and forwarding:

```python
router.register_path(request, manifest, graph)
result = router.receive_hop(header, activation_payload)
```

In-process distributed proof:

```python
mesh.register_router(node_id, router)
entry_router.start_distributed_prefill(request, client_sink)
entry_router.decode_one_distributed(request.request_id)
```

Local MVP loopback TCP proof:

```python
mesh = LoopbackSocketMesh()
transport = mesh.transport_for(node_id)
mesh.register_router(node_id, router)
mesh.start()
try:
    entry_router.start_distributed_prefill(request, client_sink)
    entry_router.decode_one_distributed(request.request_id)
finally:
    mesh.close()
```

The loopback TCP adapter accepts and emits only versioned Router bytes and binds to `127.0.0.1`. It deliberately does not claim multi-host authentication or process-isolated capacity coordination.

## MVP exclusions and v1 optimization scope

The Router retains `interactive` and `batch` QoS classes. One device-wide scheduler arbitrates work across requests and local placements; bounded priority boost plus aging prevents batch starvation. Work-count and payload-byte limits now bound pending memory.

The Router now has compatibility-safe batch formation: exact runtime keys, bounded deterministic selection, per-member positions, and an `execute_batch` port. This is still not true continuous batching because `receive_hop()` drains synchronously; an asynchronous collection window/runtime pump and a fused backend implementation remain required.

Chunked prefill has been promoted into the Router MVP as an opt-in orchestration feature. `prefill_chunk_size_tokens=0` preserves one-shot prefill; a positive value sends ordered chunks over one path and gates decode on explicit completion. Production usefulness still depends on a model-runtime adapter that preserves per-layer KV state between those chunk executions.

The remaining optimizations are explicit v1 optimization candidates and are not required to declare the Router MVP complete:

- asynchronous continuous/dynamic batch collection and fused runtime execution;
- paged attention;
- prefix/KV cache reuse;
- speculative decoding;
- tensor parallel execution;
- quantization, kernel fusion, and graph capture.

These primarily belong to Runtime/Capacity adapters. If promoted into MVP later, Router integration must expose compatible batch keys, deadlines, cancellation masks, per-request token positions, and KV ownership without letting one request poison a batch.

## Fault-tolerance boundary

Heartbeat, layer duplication, dropout detection, distributed recomputation, and rerouting now belong to the separate fault-tolerance track. They are not part of this build stream.

The existing single-process compatibility recovery remains tested, but this document makes no production fault-tolerance claim. The Layer Allocator owns long-lived placement repair; the Router fault-tolerance track owns in-flight recovery. The `distributed dropout recovery` plan item remains recorded for that separate track rather than active MVP implementation here.

## Remaining MVP integration gates

1. Add the minimum planner adapter that emits or strictly converts into `mycelium.execution_graph.v1`.
2. Connect real gossip/device-state, capacity, and model-runtime adapters behind existing ports.
3. Define process-isolated capacity reservation/commit coordination before claiming a multi-process mesh.
4. Add the separate whole-system simulator adapter only after component integration; do not couple simulator internals into Router core.

## Deferred v1 gates

1. Replace the local-only loopback TCP harness with authenticated multi-host transport.
2. Add production transfer acknowledgements, execution deadlines, and remote retry policy.
3. Add deterministic sampling ownership and replay counters if required by the separate fault-tolerance design.
4. Add the optimization metadata needed by any selected v1 batching/runtime features.
5. Add weight-provisioning lifecycle integration beyond the minimum MVP runtime hook.

## Claim boundary

This is a deterministic standalone Router reference with both in-process and real loopback TCP proofs for three Router instances. The TCP proof uses versioned bytes and real OS sockets, but instances still share one process and test capacity collaborator. It is not yet a process-isolated or multi-host production runtime.

The legacy `admit()` / `generate()` path remains for single-process regression compatibility and still uses the whole-manifest reference executor. Distributed execution uses `start_distributed_prefill()`, manifest broadcast, `decode_one_distributed()`, and device-local `receive_hop()`.

Project directory currently has no Git metadata, so this handover records filesystem artifacts and executed verification rather than a commit.
