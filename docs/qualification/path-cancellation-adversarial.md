# PathCancellation adversarial qualification

## Scope and claim boundary

Base: `62a0127`

Source commit `120d6fff` was read-only with respect to production. Integration subsequently used its five deterministic REDs to repair three production defects. Evidence remains local process evidence from Entry, Relay, `InProcessMesh`, loopback TCP, and `IrohTransport` with an in-memory sidecar client. It is not remote-host, cross-machine, native-sidecar, credential, protected-edit, tensor-correctness, or physical-network qualification.

`route_ready=false` throughout this lane as a qualification claim boundary. Iroh tests assert its explicit `route_ready is False`; fake and loopback transports expose no readiness attribute, and tests assert cancellation does not create one. Passing tests do not upgrade route readiness.

## Corpus

34 collected cases:

| Surface | Adversarial behavior | Evidence |
|---|---|---|
| Wire | canonical PathCancellation has four scalar identity fields and empty payload | pass |
| Wire | bool substituted for `path_attempt` | pass: rejected, PC-BOOL-1 regression |
| Wire | bool substituted for `topology_version` | pass: rejected, PC-BOOL-1 regression |
| Relay | wrong `request_id` | pass, no release |
| Relay | wrong `path_id` / unknown path | pass, no release |
| Relay | wrong `path_attempt` | pass, no release |
| Relay | wrong `topology_version` | pass, no release |
| Relay | already-released and duplicate cancellation | pass, one runtime cancel |
| Relay | non-entry participant source and unregistered non-participant source | pass, no release |
| Relay | bool `path_attempt=False` against attempt 0 | pass: rejected without release, PC-BOOL-1 regression |
| Entry | cancellation before manifest lock | pass, one release and one cancellation |
| In-process mesh | participant fan-out excludes source | pass |
| In-process mesh | exactly-once local release at all three participants | pass |
| In-process mesh | cancellation during blocked decode | pass: bounded join, no client token |
| In-process mesh | blocked decode must not forward bytes after cancellation | pass: zero later hops, PC-RACE-1 regression |
| Entry/mesh | cancellation racing completion blocked in client sink | pass: cancellation wins without worker exception, PC-RACE-2 regression |
| Entry/Relay | cancellation during local scheduler enqueue | pass: stale generation rejected before runtime start |
| Relay | stale attempt-scoped release after newer registration | pass: newer attempt remains active and is not cancelled |
| Relay | runtime cancellation concurrent with newer registration | pass: registration waits for old-attempt runtime cancellation |
| Relay | cancelled-attempt replay after exact tombstone eviction | pass: bounded filter rejects replay |
| Relay | scheduler/runtime cleanup callbacks re-enter path-state reads | pass: callbacks run outside the path-state lock |
| Entry/Relay | registration followed by generation capture | pass: one operation returns the exact generation permit |
| Relay | cancelled-attempt filter saturation | pass: two bounded windows rotate instead of accumulating bits forever |
| Relay | unknown unscoped release flood | pass: generation metadata remains bounded |
| Loopback TCP | cancellation frames only, empty payload, source excluded | pass |
| Loopback TCP | bounded connection count, close latency, server-thread cleanup | pass |
| Iroh adapter | unknown path, wrong source, unbound participant | 3 pass cases; no cancellation worker created |
| Iroh adapter | outbound control-only cancellation and replay after forget | pass |
| Iroh adapter | duplicate inbound message-id replay | pass, one router dispatch |
| Iroh adapter | duplicate pending, queue bound, cancellation/close race | pass, two-worker cap and bounded cleanup |

The cancellation frame contains no protected-edit operation and no tensor bytes. PC-RACE-1 covered a separate post-cancel data-plane forwarding defect; the integration repair now prevents that forwarding.

## Production REDs found and repaired during integration

Source commit `120d6fff` carried five strict xfails. The integration driver first reproduced all five, then changed production code. The strict markers produced five failures/XPASS-shape changes after the repair, proving the targeted behavior changed. The markers and defect-only exception scaffolding were then removed, leaving ordinary permanent regression tests.

### PC-BOOL-1 — bool accepted as integer identity

Minimal reproductions:

```text
python3.14 -m pytest -q --runxfail \
  tests/path_cancellation_adversarial/test_contract_entry_relay.py -k bool
```

Original observation:

- Wire decoder accepts JSON `true` for both `path_attempt` and `topology_version`; no `WireError` is raised.
- Relay accepts `PathCancellation(path_attempt=False)` for active attempt `0` because Python equality treats `False == 0`, then releases the path.

Repair: wire validation and direct Relay cancellation validation now require exact `int` types for `path_attempt` and `topology_version`. Two wire cases and one Relay case now pass without releasing the path.

### PC-RACE-1 — completed blocked runtime forwards after cancellation

Minimal reproduction:

```text
python3.14 -m pytest -q --runxfail \
  tests/path_cancellation_adversarial/test_inprocess_loopback.py \
  -k blocked_decode_cancellation_sends_no_later_remote_tensor_hop
```

Deterministic sequence:

1. First participant enters blocked decode runtime.
2. Entry cancellation releases every registered participant.
3. Runtime barrier is released.
4. Relay forwards one later remote hop with the runtime byte payload despite its path having been released.
5. Destination rejects the hop as unknown, and no client token is emitted.

Original delta: one captured post-cancel remote hop from `node-a`, carrying a non-empty byte payload. Repair: Relay revalidates the exact path registration after runtime execution and before forwarding any result. The regression now observes zero post-cancel hops.

### PC-RACE-2 — completion transition races cancellation

Minimal reproduction:

```text
python3.14 -m pytest -q --runxfail \
  tests/path_cancellation_adversarial/test_inprocess_loopback.py \
  -k cancellation_wins_when_completion_is_blocked_inside_sink
```

Deterministic sequence:

1. Final token is appended and client sink `emit` blocks.
2. Cancellation transitions request to `CANCELLED` and performs cleanup.
3. Sink barrier is released.
4. Completion attempts `CANCELLED -> COMPLETED`.

Original exception: `StateTransitionError('illegal_state_transition: CANCELLED->COMPLETED')`. Repair: token handling rechecks request state after the potentially blocking client-sink emit; cancellation remains terminal and no completion transition is attempted. Cleanup remains exactly once.

### Review follow-up — lock boundary, generation capture, and replay saturation

An incomplete external review left three concrete counterexamples. Integration
reproduced all three before changing production:

1. Relay held its path-state lock while invoking scheduler cleanup and
   `runtime.cancel(path_id)`. A callback waiting for a worker that needed a
   path-state read could deadlock. Path mutation now completes under the state
   lock, while scheduler/runtime cleanup runs afterward under a fixed striped
   per-path operation lock. Attempt replacement remains serialized without
   exposing the state lock to foreign code.
2. Entry registered a path and then read its generation in a second operation.
   Cancellation or replacement could linearize between those calls. Relay now
   returns the accepted generation from `register_path_with_generation`, and
   Entry stores that permit on the request record for all later execution and
   send boundaries. The boolean `register_path` API remains as a compatibility
   wrapper for transport callers that need acceptance only.
3. The original set-only Bloom filter could only accumulate bits, eventually
   rejecting every fresh path attempt. The filter now keeps current and previous
   fixed-size windows and rotates after 100,000 insertions. A deterministic
   small-filter regression verifies previous-window retention and continued
   fresh-path availability across many rotations.

Review also found one foreign `batch_scheduler.release_path` call in the queued
hop cancellation branch under the path-state lock. It now runs after the lock is
released. No state-tuple refactor or transport-send helper consolidation was
accepted: review found no independently reproducible correctness defect in
either shape.

## Boundedness and cleanup

- Test barriers use `threading.Event` with 1-second deadlines; no unbounded waits.
- In-process race workers are joined and asserted dead.
- Loopback mesh closes in `finally`, closes servers, joins captured server threads, and asserts zero active connections.
- Iroh transport closes in `finally`; fake clients are closed, receiver and cancellation workers are joined, and worker maps are empty.
- Iroh cancellation slots cap the adversarial run at two concurrent workers; third distinct cancellation fails with `path_cancellation_queue_full`.
- Cancellation racing Iroh close finishes below the asserted 1-second local bound.
- Cancelled-attempt replay memory uses two 1 MiB windows with five digest-derived
  positions and 100,000 insertions per window. At capacity, the theoretical
  combined false-positive probability is approximately `1.30e-6` (about one in
  770,838) under standard Bloom assumptions. False positives fail closed.
- The rotating filter is intentionally not permanent replay authority. An
  identity ages out after two rotations, and all filter state is lost on process
  restart. Exact metadata protects the 4,096 most recent terminal path IDs.

## Verification

Required commands:

```text
python3.14 -m pytest -q tests/path_cancellation_adversarial
python3.14 -m compileall -q tests/path_cancellation_adversarial
git diff --check
```

Integration result: 34 passed, 0 xfailed, 0 failed. The three newly integrated adversarial lanes pass 120 tests together. Adjacent Router/Iroh/request verification passes 370 tests and 46 subtests. Full Python verification passes 1,653 tests, skips 2, and passes 121 subtests. Claim remains local evidence only, `route_ready=false`.
