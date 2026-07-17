import threading
import unittest
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from typing import Any

from mycelium_router.contracts import ReservationRequest, RouterConfig
from mycelium_router.fakes import (
   FakeRuntimePort,
   FakeTransportPort,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.live_ports import (
   CAPACITY_CLAIM_BOUNDARY,
   InProcessLeaseCapacityPort,
   PublishedDeviceStateProvider,
   PublishedTopologyProvider,
)
from mycelium_router.router import Router
from mycelium_router.validation import ContractError
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state, state_table


NODE_BUDGETS = {
   "node-a": 100,
   "node-b": 100,
   "node-c": 100,
}


def reservation_request(**overrides):
   values = {
      "request_id": "request-1",
      "path_id": "path-1",
      "path_attempt": 0,
      "placement_id": "node-a-stage-000",
      "kv_bytes": 40,
      "deployment_epoch": 1,
      "lease_expires_at": 10.0,
   }
   values.update(overrides)
   return ReservationRequest(**values)


def capacity_fixture(*, budgets=None, graph=None, clock=None):
   topology = PublishedTopologyProvider(graph or graph_fixture())
   capacity = InProcessLeaseCapacityPort(
      topology,
      NODE_BUDGETS if budgets is None else budgets,
      clock=clock or ManualClock(),
      id_source=SequenceIdSource(),
   )
   return topology, capacity


class PublishedTopologyProviderTests(unittest.TestCase):
   def test_constructor_validates_initial_graph_and_snapshot_is_immutable(self):
      graph = graph_fixture()
      provider = PublishedTopologyProvider(graph)

      snapshot = provider.snapshot()

      self.assertEqual(snapshot, graph)
      self.assertIsInstance(snapshot.stages, tuple)
      self.assertIsInstance(snapshot.stages[0].placements, tuple)
      with self.assertRaises(FrozenInstanceError):
         snapshot.topology_version = 99
      with self.assertRaisesRegex(ContractError, "missing_stages"):
         PublishedTopologyProvider(replace(graph, stages=()))

   def test_publish_requires_strictly_increasing_topology_version(self):
      graph = graph_fixture()
      provider = PublishedTopologyProvider(graph)

      published = provider.publish(replace(graph, topology_version=4))

      self.assertEqual(published.topology_version, 4)
      self.assertEqual(provider.snapshot().topology_version, 4)
      with self.assertRaisesRegex(ValueError, "topology_version_not_increasing"):
         provider.publish(replace(graph, topology_version=4))
      with self.assertRaisesRegex(ValueError, "topology_version_not_increasing"):
         provider.publish(replace(graph, topology_version=2))
      with self.assertRaisesRegex(ValueError, "topology_version_not_increasing"):
         provider.publish(
            replace(
               graph,
               deployment_epoch=graph.deployment_epoch + 1,
               topology_version=published.topology_version,
            )
         )
      self.assertEqual(provider.snapshot(), published)

   def test_identity_change_requires_newer_deployment_epoch(self):
      graph = graph_fixture()
      provider = PublishedTopologyProvider(graph)

      with self.assertRaisesRegex(
         ValueError,
         "deployment_epoch_not_increasing_for_identity_change",
      ):
         provider.publish(
            replace(
               graph,
               topology_version=4,
               model_id="other/model",
            )
         )

      replacement = provider.publish(
         replace(
            graph,
            deployment_epoch=2,
            topology_version=4,
            model_id="other/model",
            resolved_commit="fedcba9876543210fedcba9876543210fedcba98",
            manifest_digest="sha256:other-model-manifest",
         )
      )
      self.assertEqual(replacement.deployment_epoch, 2)
      self.assertEqual(replacement.model_id, "other/model")

   def test_model_execution_shape_change_requires_newer_deployment_epoch(self):
      graph = graph_fixture()
      provider = PublishedTopologyProvider(graph)

      with self.assertRaisesRegex(
         ValueError,
         "deployment_epoch_not_increasing_for_identity_change",
      ):
         provider.publish(
            replace(
               graph,
               topology_version=graph.topology_version + 1,
               hidden_size=graph.hidden_size * 2,
            )
         )

   def test_all_deployment_identity_changes_fail_closed_within_epoch(self):
      graph = graph_fixture()
      identity_changes = {
         "deployment_id": "other-deployment",
         "model_id": "other/model",
         "resolved_commit": "fedcba9876543210fedcba9876543210fedcba98",
         "manifest_digest": "sha256:other-manifest",
         "hidden_size": graph.hidden_size + 1,
         "activation_bytes": graph.activation_bytes + 1,
         "token_envelope_bytes": graph.token_envelope_bytes + 1,
      }

      for field, value in identity_changes.items():
         with self.subTest(field=field):
            provider = PublishedTopologyProvider(graph)
            with self.assertRaisesRegex(
               ValueError,
               "deployment_epoch_not_increasing_for_identity_change",
            ):
               provider.publish(
                  replace(
                     graph,
                     topology_version=graph.topology_version + 100,
                     **{field: value},
                  )
               )

            self.assertEqual(provider.snapshot(), graph)
            accepted = provider.publish(
               replace(graph, topology_version=graph.topology_version + 1)
            )
            self.assertEqual(
               accepted.topology_version,
               graph.topology_version + 1,
            )

   def test_placement_identity_cannot_change_within_deployment_epoch(self):
      graph = graph_fixture()
      provider = PublishedTopologyProvider(graph)
      epoch_two = provider.publish(
         replace(
            graph,
            deployment_epoch=2,
            topology_version=graph.topology_version + 1,
         )
      )
      original = epoch_two.stages[0].placements[0]
      moved = replace(
         original,
         node_id="node-b",
         assignment_id="assignment-moved",
      )
      changed_stage = replace(
         epoch_two.stages[0],
         placements=(moved,) + epoch_two.stages[0].placements[1:],
      )

      with self.assertRaisesRegex(
         ValueError,
         "placement_identity_changed_within_epoch",
      ):
         provider.publish(
            replace(
               epoch_two.with_stages(
                  (changed_stage,) + epoch_two.stages[1:]
               ),
               topology_version=epoch_two.topology_version + 1,
            )
         )

      self.assertEqual(provider.snapshot(), epoch_two)

   def test_deployment_epoch_cannot_regress(self):
      graph = replace(graph_fixture(), deployment_epoch=2)
      provider = PublishedTopologyProvider(graph)

      with self.assertRaisesRegex(ValueError, "deployment_epoch_regression"):
         provider.publish(
            replace(
               graph,
               deployment_epoch=1,
               topology_version=graph.topology_version + 1,
            )
         )

   def test_malformed_mutable_graph_identity_is_rejected(self):
      graph = graph_fixture()

      with self.assertRaisesRegex(ValueError, "invalid_graph_field:model_id"):
         PublishedTopologyProvider(replace(graph, model_id=[]))

      placement = graph.stages[0].placements[0]
      malformed = replace(placement, runtime_endpoint=[])
      first_stage = replace(
         graph.stages[0],
         placements=(malformed,) + graph.stages[0].placements[1:],
      )
      with self.assertRaisesRegex(
         ValueError,
         "invalid_graph_field:runtime_endpoint",
      ):
         PublishedTopologyProvider(
            graph.with_stages((first_stage,) + graph.stages[1:])
         )

      with self.assertRaisesRegex(ValueError, "invalid_graph_field:model_id"):
         PublishedTopologyProvider(replace(graph, model_id=""))
      with self.assertRaisesRegex(
         ValueError,
         "invalid_graph_field:runtime_endpoint",
      ):
         empty_endpoint = replace(placement, runtime_endpoint="")
         first_stage = replace(
            graph.stages[0],
            placements=(empty_endpoint,) + graph.stages[0].placements[1:],
         )
         PublishedTopologyProvider(
            graph.with_stages((first_stage,) + graph.stages[1:])
         )


class PublishedDeviceStateProviderTests(unittest.TestCase):
   def setUp(self):
      self.topology = PublishedTopologyProvider(graph_fixture())

   def test_rejects_key_node_id_mismatch_and_unknown_nodes(self):
      with self.assertRaisesRegex(ValueError, "device_state_key_mismatch"):
         PublishedDeviceStateProvider(
            self.topology,
            {"node-a": state("node-b")},
         )
      with self.assertRaisesRegex(ValueError, "unknown_device_state_node"):
         PublishedDeviceStateProvider(
            self.topology,
            {"external-node": state("external-node")},
         )

      provider = PublishedDeviceStateProvider(
         self.topology,
         {"external-node": state("external-node")},
         allow_unknown_nodes=True,
      )
      self.assertIn("external-node", provider.snapshot())

   def test_publish_defensively_copies_outer_and_nested_maps(self):
      original = state_table()
      provider = PublishedDeviceStateProvider(self.topology, original)

      original["node-a"].neighbor_rtt_ms["node-b"] = 999.0
      original.pop("node-b")
      first = provider.snapshot()
      first["injected-node"] = state("injected-node")
      first.pop("node-b")
      first["node-a"].neighbor_rtt_ms["node-b"] = 8.0

      stored = provider.snapshot()
      self.assertEqual(stored["node-a"].neighbor_rtt_ms["node-b"], 2.0)
      self.assertIn("node-b", stored)
      self.assertNotIn("injected-node", stored)
      self.assertIsInstance(stored["node-a"].neighbor_rtt_ms, dict)

      replacement_states = {
         node_id: replace(item, state_seq=item.state_seq + 1)
         for node_id, item in state_table(slow_b_bandwidth=False).items()
      }
      provider.publish(replacement_states)
      replacement_states["node-a"].neighbor_bandwidth_bytes_per_second[
         "node-b"
      ] = 1.0
      self.assertEqual(
         provider.snapshot()[
            "node-a"
         ].neighbor_bandwidth_bytes_per_second["node-b"],
         1_000_000_000.0,
      )

   def test_malformed_or_non_finite_state_is_rejected(self):
      with self.assertRaisesRegex(ValueError, "invalid_device_state:availability"):
         PublishedDeviceStateProvider(
            self.topology,
            {"node-a": replace(state("node-a"), availability=[])},
         )
      with self.assertRaisesRegex(
         ValueError,
         "invalid_device_state:compute_units_per_second",
      ):
         PublishedDeviceStateProvider(
            self.topology,
            {
               "node-a": replace(
                  state("node-a"),
                  compute_units_per_second=float("nan"),
               )
            },
         )
      with self.assertRaisesRegex(
         ValueError,
         "invalid_device_state:neighbor_rtt_ms",
      ):
         PublishedDeviceStateProvider(
            self.topology,
            {
               "node-a": replace(
                  state("node-a"),
                  neighbor_rtt_ms={"node-b": []},
               )
            },
         )

   def test_state_semantic_domains_are_validated(self):
      invalid_states = {
         "node_id": replace(state("node-a"), node_id=""),
         "availability": replace(state("node-a"), availability="UNKNOWN"),
         "state_seq": replace(state("node-a"), state_seq=-1),
         "last_updated": replace(state("node-a"), last_updated=-1.0),
         "compute_units_per_second": replace(
            state("node-a"), compute_units_per_second=-1.0
         ),
         "free_compute_fraction": replace(
            state("node-a"), free_compute_fraction=1.1
         ),
         "available_kv_bytes": replace(state("node-a"), available_kv_bytes=-1),
         "pending_hop_queue_depth": replace(
            state("node-a"), pending_hop_queue_depth=-1
         ),
         "neighbor_rtt_ms": replace(
            state("node-a"), neighbor_rtt_ms={"node-b": -1.0}
         ),
         "neighbor_bandwidth_bytes_per_second": replace(
            state("node-a"),
            neighbor_bandwidth_bytes_per_second={"node-b": -1.0},
         ),
      }
      for field, item in invalid_states.items():
         with self.subTest(field=field):
            with self.assertRaisesRegex(
               ValueError,
               f"invalid_device_state:{field}",
            ):
               PublishedDeviceStateProvider(self.topology, {"node-a": item})

   def test_validates_the_exact_nested_map_copy_that_is_published(self):
      class ChangingMap(Mapping):
         def __init__(self):
            self.calls = 0

         def __getitem__(self, key):
            if key != "node-b":
               raise KeyError(key)
            self.calls += 1
            return 2.0 if self.calls == 1 else []

         def __iter__(self):
            return iter(("node-b",))

         def __len__(self):
            return 1

      changing = ChangingMap()
      candidate = replace(state("node-a"), neighbor_rtt_ms=changing)
      provider = PublishedDeviceStateProvider(
         self.topology,
         {"node-a": candidate},
      )

      self.assertEqual(changing.calls, 1)
      self.assertEqual(
         provider.snapshot()["node-a"].neighbor_rtt_ms,
         {"node-b": 2.0},
      )

   def test_state_sequence_replay_conflict_and_regression_fail_closed(self):
      initial = replace(
         state("node-a"),
         state_seq=7,
         last_updated=10.0,
      )
      provider = PublishedDeviceStateProvider(
         self.topology,
         {"node-a": initial},
      )

      replay = provider.publish({"node-a": initial})
      self.assertEqual(replay["node-a"], initial)

      with self.assertRaisesRegex(
         ValueError,
         "device_state_seq_conflict:node-a",
      ):
         provider.publish(
            {
               "node-a": replace(
                  initial,
                  available_kv_bytes=initial.available_kv_bytes - 1,
               )
            }
         )
      with self.assertRaisesRegex(
         ValueError,
         "device_state_seq_regression:node-a",
      ):
         provider.publish(
            {
               "node-a": replace(
                  initial,
                  state_seq=initial.state_seq - 1,
                  last_updated=initial.last_updated + 1.0,
               )
            }
         )

      self.assertEqual(provider.snapshot(), {"node-a": initial})

   def test_state_sequence_high_watermark_survives_omission(self):
      initial = replace(state("node-a"), state_seq=7)
      provider = PublishedDeviceStateProvider(
         self.topology,
         {"node-a": initial},
      )
      provider.publish({})

      with self.assertRaisesRegex(
         ValueError,
         "device_state_seq_regression:node-a",
      ):
         provider.publish({"node-a": replace(initial, state_seq=6)})
      with self.assertRaisesRegex(
         ValueError,
         "device_state_seq_replay_after_omission:node-a",
      ):
         provider.publish({"node-a": initial})

      self.assertEqual(provider.snapshot(), {})
      fresh = replace(initial, state_seq=8, available_kv_bytes=123)
      self.assertEqual(provider.publish({"node-a": fresh}), {"node-a": fresh})

   def test_mixed_fresh_and_stale_whole_map_is_rejected_atomically(self):
      initial = {
         node_id: replace(item, state_seq=5)
         for node_id, item in state_table().items()
      }
      provider = PublishedDeviceStateProvider(self.topology, initial)
      candidate = {
         "node-a": replace(initial["node-a"], state_seq=6),
         "node-b": replace(initial["node-b"], state_seq=4),
         "node-c": replace(initial["node-c"], state_seq=6),
      }

      with self.assertRaisesRegex(
         ValueError,
         "device_state_seq_regression:node-b",
      ):
         provider.publish(candidate)

      self.assertEqual(provider.snapshot(), initial)
      provider.publish(
         {
            node_id: replace(item, state_seq=6)
            for node_id, item in initial.items()
         }
      )

   def test_whole_state_maps_publish_atomically_under_concurrent_access(self):
      initial = state_table()
      provider = PublishedDeviceStateProvider(self.topology, initial)
      start = threading.Barrier(2)
      observed = []

      def writer():
         start.wait()
         for sequence in range(2, 1_002):
            provider.publish(
               {
                  node_id: replace(
                     item,
                     state_seq=sequence,
                     last_updated=float(sequence),
                  )
                  for node_id, item in initial.items()
               }
            )

      def reader():
         start.wait()
         for _ in range(1_000):
            observed.append(
               tuple(
                  sorted(
                     item.state_seq
                     for item in provider.snapshot().values()
                  )
               )
            )

      with ThreadPoolExecutor(max_workers=2) as executor:
         futures = (executor.submit(writer), executor.submit(reader))
         for future in futures:
            future.result()

      self.assertTrue(observed)
      self.assertTrue(
         all(len(item) == 3 and len(set(item)) == 1 for item in observed)
      )


class InProcessLeaseCapacityPortTests(unittest.TestCase):
   def test_valid_reserve_commit_release_lifecycle_and_read_only_snapshot(self):
      clock = ManualClock()
      _, capacity = capacity_fixture(clock=clock)

      reserved = capacity.reserve(reservation_request())
      committed = capacity.commit(
         (reserved.reservation_id,),
         deployment_epoch=1,
      )
      clock.advance(20.0)
      committed_snapshot = capacity.snapshot()

      self.assertTrue(reserved.accepted)
      self.assertTrue(committed.accepted)
      self.assertEqual(committed_snapshot.claim_boundary, CAPACITY_CLAIM_BOUNDARY)
      self.assertEqual(committed_snapshot.node_reserved_kv_bytes["node-a"], 40)
      self.assertEqual(
         committed_snapshot.reservations[reserved.reservation_id].status,
         "COMMITTED",
      )
      with self.assertRaises(TypeError):
         committed_snapshot.node_capacity_kv_bytes["node-a"] = 1
      with self.assertRaises(TypeError):
         committed_snapshot.reservations["other"] = None

      capacity.release((reserved.reservation_id,))
      capacity.release((reserved.reservation_id,))
      released_snapshot = capacity.snapshot()
      self.assertEqual(released_snapshot.node_reserved_kv_bytes["node-a"], 0)
      self.assertEqual(
         released_snapshot.reservations[reserved.reservation_id].status,
         "RELEASED",
      )

   def test_exact_idempotent_replay_returns_original_without_double_charge(self):
      _, capacity = capacity_fixture()
      request = reservation_request()

      first = capacity.reserve(request)
      replay = capacity.reserve(request)

      self.assertTrue(first.accepted)
      self.assertEqual(replay, first)
      snapshot = capacity.snapshot()
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-a"], 40)
      self.assertEqual(len(snapshot.reservations), 1)

   def test_committed_replay_after_lease_deadline_cannot_release_charge(self):
      clock = ManualClock()
      _, capacity = capacity_fixture(clock=clock)
      request = reservation_request(lease_expires_at=2.0)
      reserved = capacity.reserve(request)
      self.assertTrue(
         capacity.commit((reserved.reservation_id,), deployment_epoch=1).accepted
      )
      clock.advance(2.0)

      replay = capacity.reserve(request)

      self.assertFalse(replay.accepted)
      self.assertEqual(replay.reason, "reservation_already_committed")
      self.assertEqual(replay.reservation_id, "")
      snapshot = capacity.snapshot()
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-a"], 40)
      self.assertEqual(
         snapshot.reservations[reserved.reservation_id].status,
         "COMMITTED",
      )

   def test_commit_replay_after_lease_deadline_remains_successful(self):
      clock = ManualClock()
      _, capacity = capacity_fixture(clock=clock)
      reserved = capacity.reserve(reservation_request(lease_expires_at=2.0))
      first = capacity.commit((reserved.reservation_id,), deployment_epoch=1)
      clock.advance(2.0)

      replay = capacity.commit((reserved.reservation_id,), deployment_epoch=1)

      self.assertTrue(first.accepted)
      self.assertTrue(replay.accepted)
      snapshot = capacity.snapshot()
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-a"], 40)
      self.assertEqual(
         snapshot.reservations[reserved.reservation_id].status,
         "COMMITTED",
      )

   def test_same_identity_with_changed_parameters_fails_closed(self):
      _, capacity = capacity_fixture()
      first = capacity.reserve(reservation_request())

      conflict = capacity.reserve(
         reservation_request(path_id="different-path", kv_bytes=41)
      )

      self.assertTrue(first.accepted)
      self.assertFalse(conflict.accepted)
      self.assertEqual(conflict.reason, "idempotency_conflict")
      self.assertEqual(capacity.snapshot().node_reserved_kv_bytes["node-a"], 40)

   def test_unknown_placement_epoch_mismatch_and_negative_values_are_rejected(self):
      topology, capacity = capacity_fixture()

      unknown = capacity.reserve(
         reservation_request(placement_id="unknown-placement")
      )
      stale = capacity.reserve(reservation_request(deployment_epoch=0))
      negative = capacity.reserve(reservation_request(kv_bytes=-1))
      negative_attempt = capacity.reserve(reservation_request(path_attempt=-1))

      self.assertEqual(unknown.reason, "unknown_placement")
      self.assertEqual(stale.reason, "deployment_epoch_mismatch")
      self.assertEqual(negative.reason, "invalid_kv_bytes")
      self.assertEqual(negative_attempt.reason, "invalid_path_attempt")
      self.assertFalse(any((unknown.accepted, stale.accepted, negative.accepted)))

      graph = topology.snapshot()
      with self.assertRaisesRegex(ValueError, "negative_node_capacity"):
         InProcessLeaseCapacityPort(
            topology,
            {"node-a": -1, "node-b": 1, "node-c": 1},
            clock=ManualClock(),
            id_source=SequenceIdSource(),
         )
      with self.assertRaisesRegex(ValueError, "missing_node_capacity"):
         InProcessLeaseCapacityPort(
            PublishedTopologyProvider(graph),
            {"node-a": 1},
            clock=ManualClock(),
            id_source=SequenceIdSource(),
         )

   def test_reserve_rejects_expired_lease_and_commit_reaps_expired_reservation(self):
      clock = ManualClock(now=5.0)
      _, capacity = capacity_fixture(clock=clock)

      already_expired = capacity.reserve(
         reservation_request(lease_expires_at=5.0)
      )
      live = capacity.reserve(
         reservation_request(
            request_id="request-live",
            lease_expires_at=6.0,
         )
      )
      clock.advance(1.0)
      committed = capacity.commit(
         (live.reservation_id,),
         deployment_epoch=1,
      )

      self.assertEqual(already_expired.reason, "reservation_expired")
      self.assertFalse(committed.accepted)
      self.assertEqual(committed.reason, "reservation_expired")
      snapshot = capacity.snapshot()
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-a"], 0)
      self.assertEqual(snapshot.reservations[live.reservation_id].status, "EXPIRED")

   def test_multi_reservation_commit_failure_is_atomic(self):
      clock = ManualClock()
      _, capacity = capacity_fixture(clock=clock)
      first = capacity.reserve(
         reservation_request(
            request_id="request-a",
            kv_bytes=30,
            lease_expires_at=100.0,
         )
      )
      second = capacity.reserve(
         reservation_request(
            request_id="request-b",
            placement_id="node-b-stage-001",
            kv_bytes=20,
            lease_expires_at=5.0,
         )
      )
      clock.advance(5.0)

      result = capacity.commit(
         (first.reservation_id, second.reservation_id),
         deployment_epoch=1,
      )

      self.assertFalse(result.accepted)
      self.assertEqual(result.reason, "reservation_expired")
      snapshot = capacity.snapshot()
      self.assertEqual(snapshot.reservations[first.reservation_id].status, "RESERVED")
      self.assertEqual(snapshot.reservations[second.reservation_id].status, "EXPIRED")
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-a"], 30)
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-b"], 0)

   def test_capacity_is_aggregated_by_node_and_reclaimed_after_release_or_expiry(self):
      clock = ManualClock()
      _, capacity = capacity_fixture(
         budgets={"node-a": 10, "node-b": 10, "node-c": 10},
         clock=clock,
      )
      first = capacity.reserve(
         reservation_request(kv_bytes=6, lease_expires_at=2.0)
      )
      overcommit = capacity.reserve(
         reservation_request(
            request_id="request-2",
            placement_id="node-a-stage-002",
            kv_bytes=5,
            lease_expires_at=2.0,
         )
      )
      capacity.release((first.reservation_id,))
      after_release = capacity.reserve(
         reservation_request(
            request_id="request-3",
            placement_id="node-a-stage-002",
            kv_bytes=10,
            lease_expires_at=2.0,
         )
      )
      clock.advance(2.0)
      after_expiry = capacity.reserve(
         reservation_request(
            request_id="request-4",
            kv_bytes=10,
            lease_expires_at=4.0,
         )
      )

      self.assertTrue(first.accepted)
      self.assertEqual(overcommit.reason, "capacity_exceeded")
      self.assertTrue(after_release.accepted)
      self.assertTrue(after_expiry.accepted)
      self.assertEqual(capacity.snapshot().node_reserved_kv_bytes["node-a"], 10)

   def test_current_topology_epoch_controls_reserve_and_commit(self):
      topology, capacity = capacity_fixture()
      reservation = capacity.reserve(reservation_request())
      graph = topology.snapshot()
      topology.publish(
         replace(
            graph,
            deployment_epoch=2,
            topology_version=graph.topology_version + 1,
         )
      )

      stale_reserve = capacity.reserve(
         reservation_request(request_id="request-new")
      )
      stale_commit = capacity.commit(
         (reservation.reservation_id,),
         deployment_epoch=1,
      )

      self.assertEqual(stale_reserve.reason, "deployment_epoch_mismatch")
      self.assertEqual(stale_commit.reason, "deployment_epoch_mismatch")
      self.assertEqual(capacity.snapshot().node_reserved_kv_bytes["node-a"], 40)

   def test_same_epoch_placement_identity_change_fails_closed(self):
      topology, capacity = capacity_fixture()
      graph = topology.snapshot()
      original = graph.stages[0].placements[0]
      moved = replace(
         original,
         node_id="node-b",
         assignment_id="assignment-moved",
      )
      changed_stage = replace(
         graph.stages[0],
         placements=(moved,) + graph.stages[0].placements[1:],
      )
      with self.assertRaisesRegex(
         ValueError,
         "placement_identity_changed_within_epoch",
      ):
         topology.publish(
            replace(
               graph.with_stages((changed_stage,) + graph.stages[1:]),
               topology_version=graph.topology_version + 1,
            )
         )

      snapshot = capacity.snapshot()
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-a"], 0)
      self.assertEqual(snapshot.node_reserved_kv_bytes["node-b"], 0)

   def test_reentrant_id_source_cannot_overcommit_capacity(self):
      topology = PublishedTopologyProvider(graph_fixture())
      clock = ManualClock()

      class ReentrantIdSource:
         def __init__(self):
            self.capacity: Any = None
            self.inner_result: Any = None
            self.counter = 0
            self.reentered = False

         def new(self, prefix):
            if not self.reentered:
               self.reentered = True
               self.inner_result = self.capacity.reserve(
                  reservation_request(
                     request_id="inner",
                     placement_id="node-a-stage-002",
                     kv_bytes=10,
                  )
               )
            self.counter += 1
            return f"{prefix}-{self.counter}"

      ids = ReentrantIdSource()
      capacity = InProcessLeaseCapacityPort(
         topology,
         {"node-a": 10, "node-b": 10, "node-c": 10},
         clock=clock,
         id_source=ids,
      )
      ids.capacity = capacity

      outer = capacity.reserve(reservation_request(kv_bytes=10))

      self.assertTrue(ids.inner_result.accepted)
      self.assertFalse(outer.accepted)
      self.assertEqual(outer.reason, "capacity_exceeded")
      self.assertEqual(capacity.snapshot().node_reserved_kv_bytes["node-a"], 10)

   def test_injected_callbacks_cannot_deadlock_capacity_and_id_locks(self):
      class BlockingIdSource:
         def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.counter = 0

         def new(self, prefix):
            self.entered.set()
            if not self.release.wait(timeout=2.0):
               raise RuntimeError("test_id_source_timeout")
            self.counter += 1
            return f"{prefix}-{self.counter}"

      class ReenteringClock:
         def __init__(self):
            self.capacity: Any = None
            self.trigger = False
            self.reentered = threading.Event()
            self.inner_result: Any = None

         def now(self):
            if self.trigger:
               self.trigger = False
               self.reentered.set()
               self.inner_result = self.capacity.reserve(
                  reservation_request(
                     request_id="callback-inner",
                     placement_id="node-a-stage-002",
                     kv_bytes=10,
                  )
               )
            return 0.0

      ids = BlockingIdSource()
      clock = ReenteringClock()
      capacity = InProcessLeaseCapacityPort(
         PublishedTopologyProvider(graph_fixture()),
         {"node-a": 10, "node-b": 10, "node-c": 10},
         clock=clock,
         id_source=ids,
      )
      clock.capacity = capacity
      results: dict[str, Any] = {}

      outer_thread = threading.Thread(
         target=lambda: results.setdefault(
            "outer",
            capacity.reserve(reservation_request(kv_bytes=10)),
         ),
         daemon=True,
      )
      outer_thread.start()
      self.assertTrue(ids.entered.wait(timeout=1.0))

      clock.trigger = True
      snapshot_thread = threading.Thread(
         target=lambda: results.setdefault("snapshot", capacity.snapshot()),
         daemon=True,
      )
      snapshot_thread.start()
      self.assertTrue(clock.reentered.wait(timeout=1.0))
      ids.release.set()
      outer_thread.join(timeout=2.0)
      snapshot_thread.join(timeout=2.0)

      if outer_thread.is_alive() or snapshot_thread.is_alive():
         self.fail("capacity callback lock inversion deadlocked")
      accepted = (results["outer"], clock.inner_result)
      self.assertEqual(sum(item.accepted for item in accepted), 1)
      self.assertEqual(
         capacity.snapshot().node_reserved_kv_bytes["node-a"],
         10,
      )

   def test_concurrent_oversubscription_accepts_exactly_one_reservation(self):
      _, capacity = capacity_fixture(
         budgets={"node-a": 10, "node-b": 10, "node-c": 10}
      )
      barrier = threading.Barrier(2)

      def reserve(request):
         barrier.wait()
         return capacity.reserve(request)

      requests = (
         reservation_request(request_id="race-a", kv_bytes=10),
         reservation_request(
            request_id="race-b",
            placement_id="node-a-stage-002",
            kv_bytes=10,
         ),
      )
      with ThreadPoolExecutor(max_workers=2) as executor:
         results = tuple(executor.map(reserve, requests))

      self.assertEqual(sum(item.accepted for item in results), 1)
      self.assertEqual(
         sum(item.reason == "capacity_exceeded" for item in results),
         1,
      )
      self.assertEqual(capacity.snapshot().node_reserved_kv_bytes["node-a"], 10)


class RouterLivePortAdmissionTests(unittest.TestCase):
   def test_router_admission_generation_and_cleanup_use_live_ports(self):
      graph = graph_fixture()
      clock = ManualClock()
      topology = PublishedTopologyProvider(graph)
      states = PublishedDeviceStateProvider(topology, state_table())
      capacity = InProcessLeaseCapacityPort(
         topology,
         {"node-a": 20_000, "node-b": 20_000, "node-c": 20_000},
         clock=clock,
         id_source=SequenceIdSource(),
      )
      sink = InMemoryClientSink()
      router = Router(
         node_id="entry-node",
         topology=topology,
         device_states=states,
         capacity=capacity,
         runtime=FakeRuntimePort(token_base=100),
         transport=FakeTransportPort(),
         clock=clock,
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )

      request_id = router.admit(
         request_fixture(),
         sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      router.generate(request_id, token_count=1)
      admitted = capacity.snapshot()

      self.assertEqual(sink.token_ids, [101])
      self.assertEqual(len(admitted.reservations), 3)
      self.assertTrue(
         all(item.status == "COMMITTED" for item in admitted.reservations.values())
      )
      self.assertEqual(admitted.node_reserved_kv_bytes["node-a"], 7_680)
      self.assertEqual(admitted.node_reserved_kv_bytes["node-c"], 3_840)

      self.assertTrue(router.cancel(request_id))
      cleaned = capacity.snapshot()
      self.assertTrue(
         all(value == 0 for value in cleaned.node_reserved_kv_bytes.values())
      )
      self.assertTrue(
         all(item.status == "RELEASED" for item in cleaned.reservations.values())
      )


if __name__ == "__main__":
   unittest.main()
