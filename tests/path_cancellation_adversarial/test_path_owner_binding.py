"""A path id is bound to its owner (entry node, request) while it is live,
and its tombstones bind only that owner.

Before this, a peer that is a route's final placement could re-register a
live path id with a higher attempt for another request (same claimed entry),
and any node that registered and cancelled a path under some path id left
tombstones that refused another entry's later registration of that id.
"""

from __future__ import annotations

from dataclasses import replace

from mycelium_router.contracts import PathCancellation, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from test_router_receive_hop import locked_path


def _middle_router():
   graph, request, manifest = locked_path()
   placements = {p.placement_id: p for stage in graph.stages for p in stage.placements}
   nodes = [placements[hop.placement_id].node_id for hop in manifest.ordered_hops]
   router = Router(
      node_id=nodes[1],
      topology=FakeTopologyProvider(graph),
      device_states=FakeDeviceStateProvider({}),
      capacity=FakeCapacityPort(),
      runtime=FakeRuntimePort(),
      transport=FakeTransportPort(),
      clock=ManualClock(),
      id_source=SequenceIdSource(),
      config=RouterConfig(),
   )
   return router, graph, request, manifest, nodes


def _with_entry(graph, manifest, node_id: str):
   """The same route, but with its entry placement on ``node_id``."""
   first = manifest.ordered_hops[0].placement_id
   stages = tuple(
      replace(stage, placements=tuple(
         replace(p, node_id=node_id) if p.placement_id == first else p for p in stage.placements
      ))
      for stage in graph.stages
   )
   return replace(graph, stages=stages)


def test_a_live_path_is_never_replaced_by_another_request_or_entry() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   entry, final = nodes[0], nodes[-1]
   relay = router.relay
   assert relay.register_path(request, manifest, graph, entry_node_id=entry)
   other_request = replace(request, request_id="request-other")
   higher = replace(manifest, path_attempt=manifest.path_attempt + 1, request_id="request-other")
   # The final placement claims the same entry for another request: refused.
   assert not relay.register_path(other_request, higher, graph, source_node_id=final, entry_node_id=entry)
   # Another entry, any attempt: refused.
   evil = _with_entry(graph, manifest, "node-evil")
   assert not relay.register_path(request, replace(manifest, path_attempt=manifest.path_attempt + 5), evil, entry_node_id="node-evil")
   assert relay._paths[manifest.path_id][2].request_id == request.request_id
   # The owner itself still moves to a newer attempt (A6 replay).
   newer = replace(manifest, path_attempt=manifest.path_attempt + 1)
   assert relay.register_path(request, newer, graph, entry_node_id=entry)
   assert relay._paths[manifest.path_id][1].path_attempt == newer.path_attempt


def test_another_owners_tombstones_never_block_a_registration() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   entry = nodes[0]
   relay = router.relay
   evil_graph = _with_entry(graph, manifest, "node-evil")
   evil_request = replace(request, request_id="request-evil")
   evil_manifest = replace(manifest, path_attempt=manifest.path_attempt + 5, request_id="request-evil")
   assert relay.register_path(evil_request, evil_manifest, evil_graph, entry_node_id="node-evil")
   assert router.receive_path_cancellation(
      PathCancellation("request-evil", manifest.path_id, evil_manifest.path_attempt, graph.topology_version),
      source_node_id="node-evil",
   )
   # The victim's own registration of the same path id is not poisoned.
   assert relay.register_path(request, manifest, graph, entry_node_id=entry)
   # The evil owner's own tombstone still binds it.
   assert not relay.register_path(evil_request, evil_manifest, evil_graph, entry_node_id="node-evil")


def test_an_owners_own_tombstones_and_pending_cancellations_still_bind() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   entry = nodes[0]
   relay = router.relay
   assert relay.register_path(request, manifest, graph, entry_node_id=entry)
   cancel = PathCancellation(request.request_id, manifest.path_id, manifest.path_attempt, graph.topology_version)
   assert router.receive_path_cancellation(cancel, source_node_id=entry)
   assert not relay.register_path(request, manifest, graph, entry_node_id=entry)
   # A registration without a known entry inherits the recorded owner: still bound.
   assert not relay.register_path(request, manifest, graph)
   # A pending cancellation from the entry blocks a not-yet-registered path.
   router2, graph2, request2, manifest2, nodes2 = _middle_router()
   pending = PathCancellation(request2.request_id, manifest2.path_id, manifest2.path_attempt, graph2.topology_version)
   assert router2.receive_path_cancellation(pending, source_node_id=nodes2[0])
   assert not router2.relay.register_path(request2, manifest2, graph2, entry_node_id=nodes2[0])


def test_an_owner_controlled_tombstone_of_an_unknown_path_binds_everyone() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   cancel = PathCancellation(request.request_id, manifest.path_id, manifest.path_attempt, graph.topology_version)
   assert router.relay.apply_controlled_path_cancellation(cancel)
   assert not router.relay.register_path(request, manifest, graph, entry_node_id=nodes[0])


def test_another_owners_cancellation_never_weakens_an_owners_tombstone() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   entry = nodes[0]
   relay = router.relay
   # Owner A: attempt 7 registered, then cancelled (high-water mark 7).
   high = replace(manifest, path_attempt=7)
   assert relay.register_path(request, high, graph, entry_node_id=entry)
   assert router.receive_path_cancellation(
      PathCancellation(request.request_id, manifest.path_id, 7, graph.topology_version), source_node_id=entry
   )
   # Owner B registers the same path id at attempt 1 and cancels it.
   evil_graph = _with_entry(graph, manifest, "node-evil")
   evil_request = replace(request, request_id="request-evil")
   low = replace(manifest, path_attempt=1, request_id="request-evil")
   assert relay.register_path(evil_request, low, evil_graph, entry_node_id="node-evil")
   assert router.receive_path_cancellation(
      PathCancellation("request-evil", manifest.path_id, 1, graph.topology_version), source_node_id="node-evil"
   )
   # A's mark still refuses every A attempt up to 7 (5 was never registered).
   for attempt in (5, 7):
      assert not relay.register_path(request, replace(manifest, path_attempt=attempt), graph, entry_node_id=entry), attempt
   assert relay.register_path(request, replace(manifest, path_attempt=8), graph, entry_node_id=entry)


def test_tombstones_from_many_owners_stay_bounded() -> None:
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   for index in range(relay_module._MAX_TOMBSTONE_RECORDS + 50):
      relay._record_cancelled_attempt_locked(manifest.path_id, 1, (f"node-{index}", "request"))
   # Never past the absolute bound; evicted owners stay refused by their marks.
   assert len(relay._cancelled_path_attempts) <= relay_module._MAX_TOMBSTONE_RECORDS
   assert len(relay._cancelled_path_owners[manifest.path_id]) <= relay_module._MAX_TOMBSTONE_RECORDS
   assert len(relay._cancelled_recorded_at) == len(relay._cancelled_path_attempts)
   assert relay._history_evicted_locked(manifest.path_id, ("node-0", "request"))


# ---- review 2: sender authentication, owner inheritance, key collisions ----

def test_a_final_placement_cannot_create_a_path_in_another_entrys_name() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   entry, final = nodes[0], nodes[-1]
   # No provisional or registered path here: a final-relayed manifest that
   # claims the entry is refused (it only confirms an entry-chain path).
   assert not router.relay.register_path(request, manifest, graph, source_node_id=final, entry_node_id=entry)
   assert manifest.path_id not in router.relay._paths
   # The entry's own manifest registers.
   assert router.relay.register_path(request, manifest, graph, entry_node_id=entry)


def test_an_entryless_owner_is_no_wildcard_and_an_empty_entry_is_refused() -> None:
   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   # A local registration without a known entry records (None, request).
   assert relay.register_path(request, manifest, graph)
   # Another entry claiming the same request cannot take it over.
   newer = replace(manifest, path_attempt=manifest.path_attempt + 1)
   evil = _with_entry(graph, manifest, "node-evil")
   assert not relay.register_path(request, newer, evil, entry_node_id="node-evil")
   assert relay._paths[manifest.path_id][1].path_attempt == manifest.path_attempt
   # An empty entry never registers.
   router2, graph2, request2, manifest2, _ = _middle_router()
   assert not router2.relay.register_path(request2, manifest2, graph2, entry_node_id="")


def test_ids_containing_the_key_separator_never_share_a_hop_result() -> None:
   """``r:x`` / ``p`` and ``r`` / ``x:p`` join to the same idempotency key
   string; each path's hop must still run on its own."""
   from mycelium_router.idempotency import hop_idempotency_key
   from mycelium_router.contracts import HopHeader

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   runtime = relay.runtime
   placements = {p.placement_id: p for stage in graph.stages for p in stage.placements}

   def register(request_id: str, path_id: str):
      req = replace(request, request_id=request_id)
      man = replace(manifest, request_id=request_id, path_id=path_id)
      assert relay.register_path(req, man, graph, entry_node_id=nodes[0])
      return req, man

   def hop(req, man):
      fields = dict(request_id=req.request_id, path_id=man.path_id, path_attempt=man.path_attempt,
                    phase="DECODE", token_index=0, hop_index=1)
      header = HopHeader(
         **fields,
         source_placement_id=man.ordered_hops[0].placement_id,
         destination_placement_id=man.ordered_hops[1].placement_id,
         topology_version=man.topology_version,
         idempotency_key=hop_idempotency_key(**fields),
      )
      return header, relay.receive_hop(header, (1, 2), source_node_id=placements[man.ordered_hops[0].placement_id].node_id)

   first = register("r:x", "p")
   second = register("r", "x:p")
   header_one, result_one = hop(*first)
   header_two, result_two = hop(*second)
   assert header_one.idempotency_key == header_two.idempotency_key
   assert result_one.disposition == result_two.disposition == "FORWARDED"
   assert len(runtime.executed) == 2, "the second path's hop ran instead of reusing the first's result"


# ---- review 3: eviction fails closed ----

def _churn(relay, graph, request, manifest, count: int, prefix: str) -> int:
   """Register and cancel ``count`` fresh paths of the same owner; returns how
   many registrations the relay accepted."""
   accepted = 0
   for index in range(count):
      path_id = f"{prefix}-{index}"
      req = replace(request, request_id=f"{prefix}-request-{index}")
      man = replace(manifest, request_id=req.request_id, path_id=path_id)
      if relay.register_path(req, man, graph, entry_node_id="node-churn"):
         accepted += 1
         relay._release_path_state_locked(path_id, path_attempt=man.path_attempt)
   return accepted


def test_one_creators_tombstone_churn_never_blocks_another_entry() -> None:
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   entry = nodes[0]
   relay = router.relay
   # Entry A: attempt 5 registered and cancelled (attempt 3 is refused only by
   # the high-water mark).
   assert relay.register_path(request, replace(manifest, path_attempt=5), graph, entry_node_id=entry)
   assert router.receive_path_cancellation(
      PathCancellation(request.request_id, manifest.path_id, 5, graph.topology_version), source_node_id=entry
   )
   churn_graph = _with_entry(graph, manifest, "node-churn")
   quota = relay_module._TOMBSTONE_QUOTA_PER_CREATOR
   accepted = _churn(relay, churn_graph, request, manifest, quota + 50, "churn")
   # The churning creator is refused at its own quota...
   assert accepted == quota
   # ...while entry A keeps registering new paths and its mark still holds.
   assert relay.register_path(request, replace(manifest, path_id="a-fresh"), graph, entry_node_id=entry)
   assert not relay.register_path(request, replace(manifest, path_attempt=3), graph, entry_node_id=entry)


def test_an_evicted_tombstone_refuses_instead_of_reopening() -> None:
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   churn_graph = _with_entry(graph, manifest, "node-churn")
   quota = relay_module._TOMBSTONE_QUOTA_PER_CREATOR
   assert _churn(relay, churn_graph, request, manifest, quota, "churn") == quota
   # Past the retention horizon the creator's oldest records may be evicted
   # to admit new paths; each eviction leaves a mark that refuses the old
   # path id for every attempt, including ones below its old high-water mark.
   relay.clock.advance(relay_module._TOMBSTONE_RETENTION_SECONDS + 1)
   assert _churn(relay, churn_graph, request, manifest, 10, "later") == 10
   evicted = replace(manifest, request_id="churn-request-0", path_id="churn-0")
   evicted_request = replace(request, request_id="churn-request-0")
   assert ("churn-0", ("node-churn", "churn-request-0")) not in relay._cancelled_path_attempts
   for attempt in (evicted.path_attempt, evicted.path_attempt + 1):
      assert not relay.register_path(
         evicted_request, replace(evicted, path_attempt=attempt), churn_graph, entry_node_id="node-churn"
      ), attempt


def test_the_absolute_bound_evicts_only_with_marks() -> None:
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   total = relay_module._MAX_TOMBSTONE_RECORDS
   for index in range(total + 5):
      owner = (f"node-{index % 16}", f"request-{index}")
      relay._record_cancelled_attempt_locked(f"bound-{index}", 1, owner, creator=f"node-{index % 16}")
   assert len(relay._cancelled_path_attempts) <= total
   # The first records were evicted with marks: their paths stay refused.
   assert relay._history_evicted_locked("bound-0", ("node-0", "request-0"))


def test_a_peer_flooding_pending_cancellations_cannot_evict_the_entrys() -> None:
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   entry = nodes[0]
   assert router.receive_path_cancellation(
      PathCancellation(request.request_id, manifest.path_id, manifest.path_attempt, graph.topology_version),
      source_node_id=entry,
   )
   for index in range(relay_module._MAX_PENDING_PER_SOURCE + 100):
      router.receive_path_cancellation(
         PathCancellation("flood", f"flood-{index}", 1, graph.topology_version), source_node_id="node-flood"
      )
   # The flooder only displaced its own entries.
   assert router.relay._pending_by_source["node-flood"] == relay_module._MAX_PENDING_PER_SOURCE
   # The entry's pending tombstone is still there: its path never registers.
   assert not router.relay.register_path(request, manifest, graph, entry_node_id=entry)


def test_a_full_pending_table_refuses_new_cancellations() -> None:
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   for source in range(relay_module._MAX_PENDING_TOTAL // relay_module._MAX_PENDING_PER_SOURCE):
      for index in range(relay_module._MAX_PENDING_PER_SOURCE):
         relay._remember_pending_cancellation_locked(
            PathCancellation("r", f"p-{source}-{index}", 1, graph.topology_version), f"node-{source}"
         )
   assert len(relay._pending_path_cancellations) == relay_module._MAX_PENDING_TOTAL
   assert not router.receive_path_cancellation(
      PathCancellation(request.request_id, manifest.path_id, 1, graph.topology_version), source_node_id="node-new"
   )
