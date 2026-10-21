# Router lifecycle deterministic conformance

## Scope and provenance

This tranche adds an independent deterministic model-based check of Router
request-lifecycle behavior. The original seven-file harness was imported from
the read-only `test/router-lifecycle-conformance` source worktree rooted at
`b3e2062e72a9e905698c5d538b4ea569d66bfe8a`. At that source baseline the files
were untracked and intentionally preserved three production counterexamples.
The source worktree was not modified.

The integration repair changes only replay identity in
`mycelium_router/idempotency.py` and `mycelium_router/relay.py`, plus focused
Router tests. It adds no dependency and does not import production transition
code into the reference model. Production is exercised through existing Router
interfaces and deterministic fake ports.

No runtime loader, native transport, physical qualifier, request gateway,
Observatory backend, UI, contract fixture, or registry authority is changed by
this tranche.

## Harness

- `mycelium_conformance/router_model.py`: immutable reference automaton.
- `mycelium_conformance/trace_generator.py`: pure-stdlib symbolic trace
  enumeration, stable JSON encoding, and deterministic deletion-1 minimization.
- `tests/conformance/test_router_model.py`: model and invariant tests.
- `tests/conformance/test_trace_generator.py`: generator/minimizer tests.
- `tests/conformance/test_production_conformance.py`: production cross-check,
  phase gates, recovery-context checks, exact-replay checks, and preserved
  counterexample regressions.

The model observes phase, path attempt, next sequence, emitted tokens, active
reservations, releases, runtime cancellation, terminal and recovery counts,
preserved recovery context, accepted event identity, and hop execution count.
Rejected model events return the original immutable state object. Production
checks additionally require path identity, reservation IDs, release calls, and
runtime-cancel calls to remain unchanged after fail-closed rejection.

## Deterministic trace set

The fixed tail alphabet contains 16 actions: duplicate admission; next, exact,
conflicting, future, stale-attempt, future-attempt, non-final-peer, and off-path
token events; current, stale-sequence, future-attempt, non-owner, and off-path
failure events; failed recovery prefill; and cancellation.

Enumeration covers every admitted Cartesian product at tail depths zero through
three plus one pre-admission probe per action:

`1 + 16 + 16^2 + 16^3 + 16 = 4,385 traces`

Maximum trace depth is four actions. Tuple order and `itertools.product` make
execution deterministic; hash iteration and randomness are absent. Each
reference trace is replayed twice, and production cross-checks start from fresh
deterministic ports.

## Replay repair contract

Attempt-scoped keys remain derived from request, path, attempt, phase, token
index, and hop index. A separate value-stable payload digest now determines
whether reuse of that identity is exact or conflicting.

The implementation:

1. snapshots accepted bytes, byte arrays, memory views, primitive values, lists,
   tuples, and dictionaries before execution;
2. rejects recursive or unsupported payload structures without runtime or
   transport effects;
3. stores the digest beside manifest, progressive-prefill, completed-hop, and
   pending-hop replay entries;
4. returns cached success only when both identity and payload digest match;
5. returns `REJECTED`/failure reason `idempotency_payload_mismatch` when identity
   matches but payload differs;
6. applies the same rule to synchronous receipt, queued receipt, progressive
   prefill, and direct manifest execution;
7. preserves bounded retention, path release, and queue cardinality behavior;
8. returns exact cached results in both `complete_context_replay` and
   `stage_local_kv` modes, preventing duplicate scheduler/runtime execution.

Mutable queued inputs are detached before acceptance, so caller mutation cannot
change the payload later presented to the runtime.

## Repaired minimal counterexamples

All three original baseline traces remain regression tests and pass after the
repair:

1. Locked decode hop conflict:
   `admit_and_register -> hop_activation_a -> hop_activation_b`.
2. Progressive-prefill conflict:
   `start_prefill -> deliver_prefill_a -> deliver_prefill_b`.
3. Pending batched-hop conflict:
   `admit_and_register -> enqueue_activation_a -> enqueue_activation_b`.

At `b3e2062`, these produced cached/duplicate-pending success for different
payloads. The baseline focused result was `3 failed, 28 passed`; the non-
counterexample subset was `28 passed, 3 deselected`. The minimizer proves that
deleting any action removes each discrepancy.

The repaired tests require stable rejection plus unchanged runtime execution,
forwarding, token, failure, and pending-work counts. Additional regressions cover
stage-local exact replay, direct-manifest conflicts, completed queued replay,
and mutable pending payload snapshots.

## Observed local verification

From the integration worktree before request-gateway reconciliation:

- conformance: `35 passed`;
- focused Router/KV replay group: `43 passed`;
- independent reference oracle: `24 passed`;
- qualification group: `88 passed`;
- release doctor: `73 passed`;
- full Python suite: `925 passed, 2 skipped, 117 subtests passed`;
- `git diff --check`: passed.

Reference-oracle and qualification directories are run as separate focused
pytest invocations because both contain a top-level module named `conftest` and
an explicit mixed-directory invocation can select the wrong bare
`from conftest import ...` module during collection. The repository-wide pytest
invocation is authoritative and passed.

## Honest boundary

Evidence is deterministic and local. It proves the bounded lifecycle and replay
properties above, not model-output parity, authenticated transport, iroh
delivery, physical-host execution, distributed cancellation fanout, transferred
KV continuity, request-gateway semantics, or accepted route readiness.
Stage-local tests here cover replay control flow with a contract-shaped fake;
numerical KV behavior remains owned by the dedicated MLX/runtime qualification
suite. Observatory remains read-only. `route_ready=false`.
