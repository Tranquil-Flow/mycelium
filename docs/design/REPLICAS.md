# Whole-model replicas and request balancing (V2 PV8)

Status: design + two pure modules (`mycelium_replica_decision.py`,
`mycelium_replica_balancer.py`). **Nothing is wired into the live owner run.**
Base: `rel1.1/hardening` 99d24e17.

## 1. What a replica is here

A *replica* is a complete copy of the model served by its own independent
route: its own members, its own compile (layer split), its own A6/A7 recovery
scope, its own qualification. Requests are spread across copies (data
parallelism). One request runs on exactly one route from admission to its
terminal state; it is never migrated to another route in V2.

This is a different thing from what the planner and M18 call a replica (below).

## 2. What exists today (read, with file pointers)

### Planner
- `mycelium_layer_planner/replication.py::replicate_stages` replicates
  **stages** (layer ranges): for each unused node it tries adding a replica of
  each stage group, re-solves flow, and accepts the best candidate whose
  `robust_gain = gain * (1 - max(replica_uncertainty_fraction, 1 - node.calibration_confidence))`
  exceeds `max(eps, admitted * minimum_replica_gain_fraction)` (defaults 0.1
  and 0.05, `contracts.py` `PlanningPolicy`). Decisions are recorded as
  `ReplicaCandidateDecision` (`robust_gain_rps`, `minimum_required_gain_rps`,
  `failure_domain_warning`). `enumerate_legal_tracks` then lists complete
  paths through the replicated stage groups.
- A whole-model replica is the special case where every stage group is
  replicated on the same disjoint node set. The planner never asks "is another
  full copy better than a longer pipeline"; it only grows stage groups.

### M18 / A5 replica machinery (single route, single graph)
- `mycelium_m18_replication.py`: `build_replica_plan` (intent),
  `ReplicaRuntimeLedger.admit` (picks the track minimizing
  `(active+1)/traffic_fraction`; removal fails in-flight requests as
  `replica_lost_no_migration`). Used by evidence scripts only
  (`scripts/materialize_m18_replica_evidence.py`); not on the serving path.
- `mycelium_qualification/replica_track.py::select_track`: with
  `requested_track_id=None` returns `selectable[0]` of a list sorted by
  `(track_id, placement_id)`, i.e. **always the same track; no balancing**.
  `replica_track_backend.py` turns the choice into
  `excluded_placements` for one `RouterSessionBackend`.
- `mycelium_live/route.py` (`qualify_replica_concurrency`,
  `measure_replica_saturation`, `set_replica_track_qualification`) qualify and
  measure tracks inside ONE `LiveRoute` / one `ExecutionGraph`. Tracks share
  one deployment id, one qualification, one recovery authority: they are a
  throughput feature inside a route, not independent routes.

### Live product (one route)
- `mycelium_a13_macos/owner_run.py::OwnerRun` runs one linear step list
  (`model, contributor, descriptors, observe, compile, slices, publish,
  targets, activate, serving`) producing one `run_id` / `deployment_id`;
  status has one `route_ready` and one `participants`.
- `demo_chain.AppDemoChain._step_compile` compiles over **all** observed
  members (`nodes = sorted(app._observation_context.facts)`) into one split.
  `chain_compile.plan_member_split` and `layer_split.MIN_HOSTS = 2`,
  `MAX_HOSTS = 8`: a route needs at least 2 hosts (PV31 would lower this to 1,
  with an A6/A7 review for a one-node route).
- `layer_split.memory_proportional_split` + `model_resources.member_resources`
  give the memory model: usable = 3/4 RAM (`MEMBER_PEAK_MEMORY_FRACTION`),
  load peak = `LOAD_PEAK_FACTOR (2) x resident + 1 GiB + KV`, guard headroom
  15% (`GUARD_HEADROOM`), int8 resident factor 0.501.

### Admission / backpressure
- Router: `mycelium_router/scheduler.py::HopScheduler.enqueue` raises
  `BackpressureError(reason, retry_after_seconds)` on `maximum_pending_hops` /
  `maximum_pending_bytes`. It is per device, per hop, not per route.
- Gateway: `RequestGatewayService` has `max_concurrent_requests` (executor
  size) and `max_pending_requests` (BoundedSemaphore); exhaustion is
  `AdmissionError("gateway_capacity_exhausted")`. One service, one backend
  (`RouterSessionBackend(router=...)`), one qualification source.
- `QualificationGate.capture/revalidate` compares the client's submitted
  binding to the *one* current `RouteQualificationV1`. A source may answer
  per request through `current_for_request(request_id)`; this is the only
  existing seam for "this request belongs to that route".

### Health and recovery scope
- `mycelium_live/health.py::RouteHealthSource` is per route: `current()`
  becomes `None` the instant `LiveRoute.is_alive()` is false. This is the
  health input per replica.
- A6/A7 recovery (`mycelium_live/a6_recovery.py::LiveA6RecoveryQualifier`,
  `mycelium_a6_recovery.py`, `mycelium_a7_*`) is bound to a route's
  deployment id, epoch, graph digest and path manifest. It is therefore
  per route already; nothing in it needs to know that other routes exist.

## 3. What the live product lacks for PV8

1. Multi-route compile: `AppDemoChain` / `OwnerRun` produce exactly one route
   over all members. Replicas need N disjoint member groups, N splits, N
   slice plans, N publish/targets/activate/qualify sequences, N run/deployment
   ids, and teardown/rebuild per route.
2. A route-set object that owns N `LiveRoute` + N `RouteHealthSource`, and a
   `current_for_request` source mapping a request to its route.
3. Gateway: one backend, one qualification binding. Clients submit the
   binding they read; with N routes the gate must accept any member route's
   binding (or a replica-set binding that resolves to a member route at
   admission). Today `QualificationGate._compare` would reject a request that
   the balancer sent to route B if the client bound to route A.
4. A request balancer (this change adds the pure part).
5. Status/UI: `route_ready`, `participants` are scalar; a per-route list and a
   copy count are needed.
6. Single-host route (PV31) for the common home case.

## 4. Design

### 4.1 Shape
```
client -> RequestGatewayService (one admission, one session store)
            -> ReplicaBalancer (select route, per-route admission)
                 -> RouterSessionBackend[route i] -> LiveRoute i (own members, A6/A7)
```
N independent routes, each a complete pipeline over its own disjoint hosts.
Replicas never share a host: two copies on one Mac only add memory pressure
(decode is bandwidth-bound on one GPU) so a host belongs to at most one route.

### 4.2 Balancer (`mycelium_replica_balancer.py`)
- Eligible route: health `ready`, and room (`outstanding_requests <
  max_outstanding_requests` and, if set, `outstanding_tokens + cost <=
  max_outstanding_tokens`). `recovering` and `down` routes get no new
  requests.
- Selection: join-shortest-queue by least outstanding tokens normalized by
  route weight (the planner's per-route capacity): minimize
  `(outstanding_tokens + cost) / weight`. Ties: fewer outstanding requests,
  then least recently selected (round-robin, so equal routes alternate and no
  route is starved), then `route_id`. Fully deterministic.
- Per-route admission is the `max_outstanding_*` bound per route. When every
  healthy route is full the balancer rejects with `replica_capacity_exhausted`
  (+ `retry_after_s`); when no route is healthy it rejects with
  `no_healthy_replica`. It does not queue; the gateway's own pending bound
  and the router's `BackpressureError` stay the queueing/backpressure layers.
- Binding: `admit` records `request_id -> route_id` until `release`. Health
  loss excludes the route from new selection only; requests already on it
  stay bound and follow that route's A6/A7 recovery (resume on a qualified
  successor inside the same route) or fail with that route's own terminal
  code. `route_lost` lists the bound requests so the caller can report them.
  There is no cross-route migration in V2.
- Status: `status()` returns per-route state plus `copies_configured`,
  `copies_serving` and a plain line such as "2 copies of the model are
  serving; balancing requests" / "1 of 2 copies is serving; the other is
  recovering" / "1 copy of the model is serving".

### 4.3 When replicas are chosen (`mycelium_replica_decision.py`)
Two gates, both must pass for k >= 2; otherwise k = 1 (today's behavior).
1. Memory: k disjoint host groups, each of `min_hosts_per_route..max_hosts`
   hosts, each able to hold a full copy:
   `sum_i(usable_i - fixed) >= (load_peak_factor x resident + kv) x (1 + headroom)`
   with `usable_i = memory_i x 3/4`. With proportional layer split this equals
   the per-member check. It is a pre-filter: the real compile, resource check
   and qualification stay authoritative (layer granularity, tied embedding).
2. Planner score: the best k-way layout's robust capacity beats the best
   single route over all hosts by more than `max(eps, single x
   minimum_gain_fraction)`, using the planner's own formulas (robust =
   capacity x (1 - max(uncertainty, 1 - confidence)); defaults 0.05 / 0.1).
   Capacity per host group comes from an injected estimator (the planner or
   the calibration table), so no estimate means no replicas.
   Bounded demand (`offered_demand_rps`) caps both sides: when one route
   already meets demand the gain is zero and k stays 1.
Ties prefer fewer copies, then lexicographic layout. More than 10 hosts is
refused (`hosts_out_of_bounds`) rather than searched; the product caps a
route at 8 hosts.

### 4.4 Failover
Per route: unchanged A6/A7. A dead route's in-flight requests recover inside
that route or fail with that route's code. New requests go to the surviving
routes immediately (health exclusion). When the route is rebuilt and
re-qualifies it re-enters selection. If all routes are down the gateway
reports `no_healthy_replica`. Cross-route migration of a half-finished
request needs KV transfer or full replay on another route: out of scope.

### 4.5 Status and UI
Owner status gets `routes: [{route_id, state, participants, outstanding}]`
and `copies_serving`; the single `route_ready` stays meaning "at least one
copy can accept requests". One line for people: "2 copies of the model are
serving; balancing requests".

## 5. When replicas help on our hardware

- 2 Macs, 0.5B model: a copy fits on one Mac easily, so two copies means two
  single-host routes. Today `MIN_HOSTS = 2`, so a 2-Mac setup cannot form two
  routes: **not applicable until PV31 (single-host route) lands**.
- 2 Macs, two 2-Mac routes would need 4 Macs; not applicable.
- 2 Macs, 7B int8: the 7B route already splits across both (a 7B int8 stage
  measured 7.4 GB group RSS, `model_resources.LOAD_PEAK_FACTOR`), so a second
  copy does not fit. k = 1 (`single_copy_only_memory`).
- 4+ Macs with a model that fits on a pair: two 2-Mac routes become possible
  with multi-route compile.
- Gain is real only when demand exceeds one route's capacity; for one
  interactive chat it is zero, which the `offered_demand_rps` cap expresses.

## 6. Next steps (not done here)
1. PV31 single-host route (prerequisite for the 2-Mac 0.5B case).
2. Multi-route compile in `AppDemoChain` / `OwnerRun` (N groups from
   `choose_replica_layout`, N sequences, per-route teardown/rebuild).
3. Route-set object + `current_for_request` source; gateway gate accepts any
   member route's binding (touches pinned gateway files, needs re-pin).
4. Wire `ReplicaBalancer` in front of per-route `RouterSessionBackend`s;
   feed `health` from `RouteHealthSource`, `weight` from the planner.
5. Status/UI fields, then a physical gate with two routes (kill one route's
   member mid-stream, check survivors keep serving and the dead route's
   request follows its own recovery).
