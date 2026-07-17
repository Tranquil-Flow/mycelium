# Router Phase-Aware Runtime Batching

## Scope

The Router has a latency-first deferred ingress path for real local runtime
microbatches. It preserves the original synchronous `receive_hop()` behavior for
existing transports and tests.

Implemented batching applies to validated hops on an already registered path:

- `DECODE`: dispatch a singleton immediately; combine compatible requests only
  when they are already queued at the same drain point.
- `PREFILL` / `PREFILL_CHUNK`: interactive work dispatches immediately; batch-QoS
  work may wait for a bounded collection window, but an earliest TTFT deadline
  always wins.
- Other phases fail closed to singleton execution.

Progressive first-pass prefill remains synchronous because each hop extends and
reserves the route. Locked-path prefill chunks use the batching path.

This feature fuses local runtime execution through `RuntimePort.execute_batch()`.
Each result still forwards through its existing per-request transport message.
It does not claim QUIC-level tensor coalescing; that requires a separate
transport batch frame and destination-compatible output contract.

## Event-loop integration

A transport adapter should use:

```python
accepted = router.enqueue_hop(header, payload)
completed = router.drain_ready_batches()
wake_at = router.next_batch_deadline()
```

If `wake_at` is not `None`, schedule another non-blocking drain at that monotonic
time. The Router never sleeps and never creates a scheduling thread. On shutdown
or a controlled drain, call:

```python
completed = router.drain_ready_batches(force=True)
```

Do not mix deferred and synchronous ingress for the same hop. Duplicate deferred
hops return `QUEUED:duplicate_pending`; completed duplicates return their cached
result.

## Compatibility boundary

A runtime batch key includes:

- deployment and epoch;
- model commit and manifest digest;
- placement, assignment, stage signature, and load proof;
- runtime backend;
- phase;
- hidden size, activation width, and token span;
- speculative role and width.

Missing identity fails closed to singleton. Prefill token spans must match.
Per-request results retain their own path, attempt, token index, forwarding, and
failure outcome.

## Adaptive policy

Defaults:

| Setting | Default |
|---|---:|
| Maximum common batch | 20 items |
| Decode target | 8 items |
| Prefill target | 20 items |
| Maximum batch payload | 2,400,000 bytes |
| Batch-prefill collection window | 2 ms |
| Deadline guard | 1 ms |
| BDP multiplier | 2.0 |

`BatchNetworkStats` supplies one-way p95, effective goodput, loss rate, receiver
queue delay, and observation time. The controller estimates a byte target from
BDP, reduces it under loss, and clamps it by phase/count/byte limits. Stale stats
fall back to conservative Router defaults.

Stats are keyed by local runtime placement. If one placement can forward to
multiple downstream edges, its adapter should publish a conservative aggregate
for the currently routable edge set. Per-edge output coalescing belongs in the
future transport-batch contract.

Successful executions update an EWMA keyed by `(phase, batch_size)`. When
profiles exist, the controller chooses the observed shape with lowest predicted
runtime-plus-transfer latency rather than blindly maximizing batch size.

Public evidence APIs:

```python
router.batch_decisions()
router.batch_execution_profiles()
router.batch_network_stats()
router.pending_batch_hops()
```

Decision history contains only counts, timing predictions, phase, and reason;
it never retains activation contents.

## Runtime requirement

A production `RuntimePort.execute_batch()` must perform real backend batching
and return exactly one isolated `RuntimeResult` per member in deterministic batch
order. Returning the wrong result count fails every member with
`runtime_batch_result_count_mismatch`.

The test fake loops over `execute()` only to prove contracts. That fake behavior
must not be mistaken for a compute-throughput implementation.
