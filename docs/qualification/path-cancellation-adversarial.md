# PathCancellation adversarial qualification

## Scope and claim boundary

Base: `62a0127`

This lane is read-only with respect to production. It adds only deterministic tests and this report. Evidence is local process evidence from Entry, Relay, `InProcessMesh`, loopback TCP, and `IrohTransport` with an in-memory sidecar client. It is not remote-host, cross-machine, native-sidecar, credential, protected-edit, tensor-correctness, or physical-network qualification.

`route_ready=false` throughout this lane as a qualification claim boundary. Iroh tests assert its explicit `route_ready is False`; fake and loopback transports expose no readiness attribute, and tests assert cancellation does not create one. Passing tests do not upgrade route readiness.

## Corpus

23 collected cases:

| Surface | Adversarial behavior | Evidence |
|---|---|---|
| Wire | canonical PathCancellation has four scalar identity fields and empty payload | pass |
| Wire | bool substituted for `path_attempt` | strict xfail, PC-BOOL-1 |
| Wire | bool substituted for `topology_version` | strict xfail, PC-BOOL-1 |
| Relay | wrong `request_id` | pass, no release |
| Relay | wrong `path_id` / unknown path | pass, no release |
| Relay | wrong `path_attempt` | pass, no release |
| Relay | wrong `topology_version` | pass, no release |
| Relay | already-released and duplicate cancellation | pass, one runtime cancel |
| Relay | non-entry participant source and unregistered non-participant source | pass, no release |
| Relay | bool `path_attempt=False` against attempt 0 | strict xfail, PC-BOOL-1 |
| Entry | cancellation before manifest lock | pass, one release and one cancellation |
| In-process mesh | participant fan-out excludes source | pass |
| In-process mesh | exactly-once local release at all three participants | pass |
| In-process mesh | cancellation during blocked decode | pass: bounded join, no client token |
| In-process mesh | blocked decode must not forward bytes after cancellation | strict xfail, PC-RACE-1 |
| Entry/mesh | cancellation racing completion blocked in client sink | strict xfail, PC-RACE-2 |
| Loopback TCP | cancellation frames only, empty payload, source excluded | pass |
| Loopback TCP | bounded connection count, close latency, server-thread cleanup | pass |
| Iroh adapter | unknown path, wrong source, unbound participant | 3 pass cases; no cancellation worker created |
| Iroh adapter | outbound control-only cancellation and replay after forget | pass |
| Iroh adapter | duplicate inbound message-id replay | pass, one router dispatch |
| Iroh adapter | duplicate pending, queue bound, cancellation/close race | pass, two-worker cap and bounded cleanup |

The cancellation frame contains no protected-edit operation and no tensor bytes. Race PC-RACE-1 separately records an existing post-cancel data-plane forwarding defect; this is not behavior encoded in PathCancellation itself.

## Genuine production REDs

All RED tests use `pytest.mark.xfail(strict=True)`: current defects remain visible without making this read-only lane uncommittable, and a future fix becomes XPASS/failure until the marker is removed.

### PC-BOOL-1 — bool accepted as integer identity

Minimal reproductions:

```text
python3.14 -m pytest -q --runxfail \
  tests/path_cancellation_adversarial/test_contract_entry_relay.py -k bool
```

Observed:

- Wire decoder accepts JSON `true` for both `path_attempt` and `topology_version`; no `WireError` is raised.
- Relay accepts `PathCancellation(path_attempt=False)` for active attempt `0` because Python equality treats `False == 0`, then releases the path.

Boundary: two wire cases and one Relay case, one root type-validation defect. No other cancellation identity mismatch released the path.

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

Observed delta: one captured post-cancel remote hop from `node-a`, carrying a non-empty byte payload. Root boundary is missing post-runtime path/cancellation revalidation before forwarding.

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

Observed exception: `StateTransitionError('illegal_state_transition: CANCELLED->COMPLETED')`. Cleanup still occurs once; worker exception is the RED.

## Boundedness and cleanup

- Test barriers use `threading.Event` with 1-second deadlines; no unbounded waits.
- In-process race workers are joined and asserted dead.
- Loopback mesh closes in `finally`, closes servers, joins captured server threads, and asserts zero active connections.
- Iroh transport closes in `finally`; fake clients are closed, receiver and cancellation workers are joined, and worker maps are empty.
- Iroh cancellation slots cap the adversarial run at two concurrent workers; third distinct cancellation fails with `path_cancellation_queue_full`.
- Cancellation racing Iroh close finishes below the asserted 1-second local bound.

## Verification

Required commands:

```text
python3.14 -m pytest -q tests/path_cancellation_adversarial
python3.14 -m compileall -q tests/path_cancellation_adversarial
git diff --check
```

Qualification result: 18 passed, 5 strict xfailed, 0 failed; 23 collected. Claim remains local evidence only, `route_ready=false`.
