# Mycelium A4 implementation packet

**Status:** `design_only`

**Acceptance base:** `4bde254f3f53bd77ce9a642397e8cfc297425334`

**Machine source:** `tests/a4_acceptance/implementation_packet.v1.json`

This packet is a handover boundary, not implementation or qualification. It names the
only production surfaces a future A4 implementation lane may change, the shared UI
surfaces reserved for the primary integrator, the legacy serialization mechanisms that
must actually be removed, and the tests and physical/browser observations required
before A4 can leave `design_only`.

No production implementation may branch directly from this packet's earlier source
commit `641eba4a78185fb23982bed1cdbe43b66b96983a`, or treat
`95fe9b77d9fb6f43e96cdd35e7d5ee02e8dc136d` alone as a sufficient base. Every A4 lane
must begin from, or rebase onto, the exact atomic A3 commit
`905786df41ffdad5718d3464733e2f5cb8727532` before its first production edit. The
packet and its earlier acceptance lineage are reviewed design inputs, not substitutes
for atomic A3 closure.

## Current integration-review authority

The 2026-08-18 integration review of the active dirty A4 substrate is authoritative over
read-only reports that inspected only the committed pre-A4 baseline. Before physical
qualification, the primary lane closes these deterministic P0s through the ordinary
gateway -> Router port -> physical route -> node -> Iroh path:

- wire the command controller and traffic-aware liveness detector as production owners;
- carry one original cancellation deadline through interruption, exact cleanup, backend
  release, and terminal publication;
- consume the canonical M16 path identity and propagate the complete deployment,
  qualification, request, path, command, cancellation, topology, and publisher identity;
- use owner-scoped, generation-fenced terminal/cleanup CAS receipts and prove exact cleanup
  while unrelated requests remain active;
- discard late command results without harming unrelated waiters or the shared node;
- route exact-subject Iroh receipts/failures through scoped liveness and apply the reviewed
  deployment-fatal allowlist; and
- fence SSE events, reconnect cursors, persistence, and all eight UI reducers by publisher
  generation.

A5 physical execution remains blocked. A8 retains a separate atomic commit and may not
regenerate shared contracts until A4 closes. A6/A7 must be rebased and redesigned after
A4 because their reviewed pre-A4 form minted Router-owned generations and bypassed A4
terminal/cleanup ownership.

## 1. Source boundary

The future backend implementation allowlist is exact:

- `mycelium_m16_runtime.py`
- `mycelium_live/health.py`
- `mycelium_live/registry.py`
- `mycelium_live/route.py`
- `mycelium_live/router_port.py`
- `mycelium_membership/contracts.py`
- `mycelium_node/process.py`
- `mycelium_request_gateway/asgi.py`
- `mycelium_request_gateway/backend.py`
- `mycelium_request_gateway/contracts.py`
- `mycelium_request_gateway/service.py`
- `mycelium_router/contracts.py`
- `mycelium_router/entry.py`
- `mycelium_router/live_ports.py`
- `mycelium_router/mlx_runtime.py`
- `mycelium_router/numpy_runtime.py`
- `mycelium_router/relay.py`
- `mycelium_router/transports/iroh.py`
- `mycelium_seed/state.py`
- `physical_inference_node.py`

Two new production modules may be introduced: `mycelium_live/command_controller.py` and
`mycelium_live/liveness.py`. No other new production path is in scope.

Two reviewed paths are conditional rather than generally open. `mycelium_router/router.py`
may change only if its public facade exposes attempt-aware A4 APIs.
`mycelium_mobile/pixel_runtime.py` may change only if the Pixel backend remains eligible
after proving bounded cooperative cancellation. The physical command boundary is
explicitly the controller/process transport in `mycelium_node/process.py`, the node
command service in `physical_inference_node.py`, and the physical Router data transport
in `mycelium_router/transports/iroh.py`; required correlation and interruption changes
must not be hidden in qualification-only code.

The backend implementation lane must not edit contract manifests or compatibility fixtures,
qualification/evidence directories, Planner/A5-A7 modules, speculation, physical-runner
infrastructure, release content, scripts, shared UI, or the qualification-only
`physical_inference_qualification.py` session. The exact prohibited patterns are frozen
in the machine source. If implementation discovers a necessary path outside the
allowlist, it stops for owner/integrator review rather than widening its own lane.

The primary integrator exclusively owns `mycelium_request_gateway/service.py`, the A4
physical/fault/browser harness and evidence root, shared contract generation,
`mycelium_live/supervisor.py`, and all shared UI wiring. Leaf lanes consume frozen
interfaces and may not edit those surfaces.

## 2. Lock ownership and order

Each mutable subset has one lock owner. Locks detach state and release before admission
waits, queue waits, worker claims, stage commands, transport I/O, browser writes, joins,
or cleanup.

| Lock | Owner | Intended surface | Protected state |
| --- | --- | --- | --- |
| Authority | deployment registry and qualifier | `mycelium_live/registry.py` | selection, qualification generation, fatal allowlist |
| Session | request gateway | `mycelium_request_gateway/service.py` | subscriber, replay cursor, private ledger, terminal publication |
| Request | Router | `mycelium_live/router_port.py` | attempt, lifecycle sequence, worker claim, immutable path |
| Cancellation | command controller | `mycelium_live/command_controller.py` | deadline, cancellation generation, command terminal CAS |
| Stage | placement runtime | `physical_inference_node.py` | command state, capacity, stage-local KV ownership |
| Liveness | liveness detector | `mycelium_live/liveness.py` | freshness, misses, incident sequence |

The global acquisition order remains:

`authority -> deployment -> session -> request -> path -> placement -> transport -> detector`

Cancellation uses the owning request scope and generation; it is not a side lock that
may invert the order. The deterministic detector must reject an inversion or wait-for
cycle before a physical command, unwind only the owning request, and leave unrelated
dispatch live.

## 3. Required replacement map

The implementation is incomplete unless all of these legacy choke points are replaced:

| Current mechanism | Current problem | Required replacement |
| --- | --- | --- |
| `M16RuntimeCoordinator._lock` plus `_synchronized` | admission, queue, status, cancel, and completion share one route-wide lock | short deployment metadata plus request records and ledger-owned locks |
| `M16RuntimeCoordinator._active_request_id` | only one request can dispatch | bounded active claims keyed by request attempt and worker |
| `LiveRouterPort._dispatcher` / `_dispatch_loop` | one thread runs a route to terminal before the next claim | fixed worker pool over the existing bounded admission queue |
| `PhysicalLiveRoute._lock` around `infer()` | remote commands, decode, browser emission, snapshots, and cleanup hold one route lock | detached route metadata, request execution, and per-placement command slots |
| `NodeProcessSession._request_lock` in `physical_inference_qualification.py` | cancellation queues behind a blocked qualification command | production request-scoped command channels with out-of-band interrupt and deadlines |
| `PhysicalNodeProcess._exchange_lock` around `command()` in `mycelium_node/process.py` | one lock spans stdin write and the entire response wait | a short canonical-frame write lock plus command-ID-correlated waiters and one response demultiplexer |
| inline `service.dispatch()` in the `physical_inference_node.py` stdin loop | the node stops reading commands while runtime work executes | responsive stdin dispatch into bounded command workers with serialized command-ID-correlated responses |
| thread creation in `RequestGatewayService.submit()` | accepted sessions may create up to the session limit in daemon threads | bounded gateway submission executor with session-owned stream conditions |

No mutex, semaphore, condition, worker join, or advisory lease may recreate a
generation-long route-global lock under a new name. The qualification session is a
source to replace, not a production module to extend.

## 4. Bounded dispatcher and command interfaces

Initial frozen defaults are four workers, 256 queued requests, 64 MiB queued payload,
two commands per placement, 64 browser events per session, one 2,000 ms
request-scoped interruption-and-cleanup budget, and a 4,000 ms shutdown join. Every value has the closed
minimum/maximum range recorded in the machine source. Configuration changes apply only
to future request generations.

The queue/pool interface is closed to six operations:

1. `enqueue` accepts a validated request attempt with a complete-path reservation and
   returns a queued identity or bounded backpressure with retry hint.
2. `claim` atomically binds one worker to the expected request generation or rejects a
   stale, cancelled, expired, already-owned, or terminal request.
3. `dispatch_stage_command` carries request ID and attempt, path digest, absolute
   deadline, cancellation generation, idempotency digest, expected terminal CAS, and
   cleanup-result channel. It returns one command-ID-correlated terminal CAS and cleanup
   result.
4. `cancel` advances the request cancellation generation once and rejects stale or
   conflicting duplicates.
5. `complete` compare-and-swaps one terminal result and invokes owner-scoped cleanup.
6. `shutdown` stops admission, interrupts work, joins by the absolute deadline, and
   reports zero live resources or an explicit failure.

This remains concurrent sequential dispatch, not runtime batching, microbatching,
continuous batching, or pipeline overlap.

Command transport uses a short lock only to write one canonical frame. It never holds a
lock across request/response. A response reader demultiplexes by command ID into bounded
correlated waiters, so cancellation, deadline, cleanup, and unrelated commands cannot
queue behind a blocked response. The node keeps stdin responsive while bounded command
workers execute; response serialization preserves command identity without imposing
execution-order coupling.

Prefill and decode are divided into bounded cancellable work units. Cancellation is
cooperative and correlated by request, attempt, path digest, absolute deadline, and
cancellation generation. Interruption and all request-owned cleanup together must
complete within one 2,000 ms end-to-end bound. A backend that cannot prove that bound is A4-ineligible and remains
unavailable; the gate is never weakened for a non-cooperative backend. Killing a shared
node is never request-scoped cancellation because it destroys unrelated work and
ownership.

## 5. Reconnect and generation reset

A browser disconnect detaches only its subscription. The server-owned request and backend
continue. Reconnect authority requires the same authenticated session, request, and
captured generation; replay begins strictly after the last client-applied event sequence
and retains the original event identities. Only one live subscriber is allowed.

Future, expired, cross-session, or cross-generation cursors fail closed. Cancellation
after reconnect targets the same request attempt and advances its cancellation generation
once. A backend generation change leaves an accepted request pinned while new admission
uses the new generation. A publisher reset emits one full authoritative snapshot; clients
must never union the old and new generations. Exactly one terminal event survives
disconnect, replay, and reset.

The same-session reconnect behavior, publisher-generation interface, and generation
reset wiring are primary-integrator-only. An A4 backend lane may consume the pre-landed
interface but may not implement, revise, or bypass that ownership boundary.

## 6. Cancellation and cleanup ownership

The gateway authenticates browser cancellation and owns terminal publication. The command
controller owns cancellation generation/deadline. Router owns path cancellation. The M16
ledger owns complete-path reservations and the cleanup record. Placement runtime owns
capacity and KV. Transport owns request streams and receipts.

Cleanup requires the matching request, attempt, generation, and recorded owner. An exact
same-digest duplicate is idempotent; non-owner, stale, or conflicting cleanup is rejected
without releasing anything. Late results cannot mutate a newer generation. Retained
terminal history is accounted separately from live resources. Unknown exceptions fail
the owned request and do not latch deployment-fatal state outside the frozen allowlist.
Request cleanup and failure are keyed by request, path, and attempt; neither is promoted
automatically to deployment-global fatal state. A4 disables automatic replay and
recovery. An affected request terminates explicitly, while replay/recovery remains owned
by A6.

## 7. Migration and rollback

Migration order is fixed:

1. branch from, or rebase onto, exact atomic A3 commit
   `905786df41ffdad5718d3464733e2f5cb8727532`;
2. require the primary integrator to pre-land all five versioned A4 contracts, their
   compatibility fixtures and manifest entries, and the versioned session/publisher-
   generation interface;
3. freeze the deterministic harness against those pre-landed interfaces;
4. extract the asynchronous production command controller and responsive node service;
5. decompose route, coordinator, and owner locks;
6. install the bounded pool in one-worker compatibility mode;
7. enable multiworker dispatch and request-scoped cooperative cancellation;
8. install receipt-aware liveness and narrow failure scope;
9. let the primary integrator alone wire same-session reconnect, shared UI, and generation
   reset;
10. run focused, full, physical, and browser gates; and
11. activate only after owner review of observed evidence.

The rollback boundary is before owner-approved activation of the new A4 contract
generation. Before it, disable or revert the entire capability and retain the prior
qualified route. After it, rollback requires a new generation, requalification, and all
physical/browser gates again. Never partially restore the global lock while the pool is
live, mix request/cancellation generations, reuse pre-rollback evidence, or leave a new UI
projection on old backend contracts.

## 8. Acceptance-scenario implementation map

| A4 scenario | Intended production surfaces | Focused test |
| --- | --- | --- |
| `overlapping_requests` | M16 coordinator, live Router port, live route | `tests/live/test_a4_concurrency.py::test_overlapping_requests_advance_independently` |
| `route_global_lock_absence` | M16 coordinator, live Router port, live route | `tests/live/test_a4_concurrency.py::test_every_blocking_boundary_allows_other_dispatch_and_cancel` |
| `lock_order_deadlock_detection` | M16 coordinator, live Router port, Router relay | `tests/live/test_a4_concurrency.py::test_lock_inversion_fails_before_physical_command` |
| `cancel_isolation` | command controller, live Router port, request backend/service | `tests/request_gateway/test_a4_cancellation.py::test_cancel_one_request_does_not_mutate_another` |
| `participating_active_disconnect` | command controller, liveness, live route, Iroh transport, node runtime | `tests/live/test_a4_liveness.py::test_active_disconnect_interrupts_owned_command_within_budget` |
| `participating_idle_staleness` | liveness, membership contracts, Iroh transport | `tests/live/test_a4_liveness.py::test_idle_subject_quarantines_only_after_frozen_threshold` |
| `nonparticipating_peer_exit` | liveness, registry, seed state | `tests/live/test_a4_liveness.py::test_nonparticipating_peer_exit_does_not_mutate_active_route` |
| `one_missed_receipt` | liveness, Iroh transport | `tests/live/test_a4_liveness.py::test_one_missed_receipt_is_suspect_only` |
| `stale_incarnation_receipt` | liveness, membership contracts, Iroh transport | `tests/live/test_a4_liveness.py::test_stale_incarnation_receipt_cannot_refresh_subject` |
| `late_command_result` | command controller, live Router port, node runtime | `tests/live/test_a4_commands.py::test_late_result_cannot_mutate_new_request_generation` |
| `queue_saturation` | M16 coordinator, live Router port | `tests/live/test_a4_concurrency.py::test_queue_saturation_returns_bounded_backpressure_without_leak` |
| `worker_exit` | M16 coordinator, live Router port | `tests/live/test_a4_concurrency.py::test_worker_exit_fails_only_owned_request_and_releases_resources` |
| `bounded_shutdown` | command controller, live Router port, live route, node runtime | `tests/live/test_a4_concurrency.py::test_shutdown_interrupts_joins_and_returns_all_counters_to_zero` |
| `fatal_allowlist_rejection` | health, liveness, registry | `tests/live/test_a4_liveness.py::test_unknown_worker_exception_cannot_latch_deployment_fatal` |
| `active_request_reconnect` | request ASGI/contracts/service | `tests/request_gateway/test_a4_reconnect.py::test_mid_request_reconnect_replays_without_duplicate_or_cancel` |
| `state_subset_ownership_boundaries` | health, liveness, registry, live Router port, gateway service | `tests/live/test_a4_ownership.py::test_cross_owner_mutation_and_cleanup_fail_closed` |
| `second_session_privacy` | request ASGI/contracts/service | `tests/request_gateway/test_a4_reconnect.py::test_second_session_cannot_observe_or_resume_private_request` |

The JSON inventory additionally binds each scenario to exact source paths and primary
integrator UI paths. It must remain a one-to-one mapping with `scenarios.v1.json`.

## 9. Shared UI integration reservation

All `mycelium_ui_gateway/**` and `ui/**` edits are prohibited in backend lanes. The
primary integrator alone owns the exact reserved paths listed in the machine source.
Those paths cover Inference, Device Lab, Network, Nodes, Plans, Readiness, Incidents,
and Settings.
Backend lanes publish only same-generation, privacy-reduced snapshots and do not add
temporary UI adapters, fixtures, or milestone-labelled panels.

## 10. Regression and execution gates

Packet checks:

```bash
python3 -m pytest -q tests/a4_acceptance
ruff check tests/a4_acceptance
```

Focused implementation checks are the six planned A4 test modules plus the existing M16,
Router-port, live-route, gateway, Router, membership, and UI-gateway suites. Full gates are:

```bash
python3 -m pytest -q
cd ui/web && npm run check
cd ui/web && npm run test:e2e
```

Physical execution must separately prove overlapping ordinary gateway requests, scoped
active-disconnect interruption and cleanup together within one 2,000 ms bound, and negative scope,
queue, worker, fatal-allowlist, and bounded-shutdown behavior. The browser gate disconnects
during waiting, prefill, decode, and terminal boundaries, then proves exact replay,
cancellation, second-session privacy, and one public generation across all eight
workspaces. None of those gates is executed or satisfied by this packet.

Every packet result retains `qualification_claim=false` and
`promotion_authorized=false`. A4 remains `design_only` until implementation, regression,
physical, browser, evidence, and owner-review gates all complete.
