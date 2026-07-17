import unittest

from mycelium_router.contracts import RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   InMemoryClientSink,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.router import Router
from mycelium_router.state import (
   HopStateMachine,
   PathStateMachine,
   RequestStateMachine,
   StateTransitionError,
)
from test_router_contracts import graph_fixture
from test_router_policy import request_fixture, state_table


class RequestStateMachineTests(unittest.TestCase):
   def test_legal_request_lifecycle_reaches_completion(self):
      machine = RequestStateMachine(path_attempt=0)

      self.assertTrue(machine.transition("PREFILL", path_attempt=0))
      self.assertTrue(machine.transition("LOCKED", path_attempt=0))
      self.assertTrue(machine.transition("DECODING", path_attempt=0))
      self.assertTrue(machine.transition("COMPLETED", path_attempt=0))

      self.assertEqual(machine.state, "COMPLETED")

   def test_illegal_transition_fails_closed(self):
      machine = RequestStateMachine(path_attempt=0)

      with self.assertRaises(StateTransitionError) as caught:
         machine.transition("DECODING", path_attempt=0)

      self.assertEqual(caught.exception.code, "illegal_state_transition")
      self.assertEqual(machine.state, "ADMITTING")

   def test_duplicate_transition_is_idempotent(self):
      machine = RequestStateMachine(path_attempt=0)
      machine.transition("PREFILL", path_attempt=0)

      self.assertFalse(machine.transition("PREFILL", path_attempt=0))
      self.assertEqual(machine.state, "PREFILL")

   def test_stale_attempt_cannot_mutate_state(self):
      machine = RequestStateMachine(path_attempt=1, initial_state="DECODING")

      with self.assertRaises(StateTransitionError) as caught:
         machine.transition("FAILED", path_attempt=0)

      self.assertEqual(caught.exception.code, "stale_path_attempt")
      self.assertEqual(machine.state, "DECODING")

   def test_recovery_requires_exactly_next_attempt(self):
      machine = RequestStateMachine(path_attempt=1, initial_state="DECODING")

      self.assertTrue(machine.begin_recovery(path_attempt=2))
      self.assertEqual(machine.state, "PREFILL")
      self.assertEqual(machine.path_attempt, 2)
      with self.assertRaises(StateTransitionError):
         machine.begin_recovery(path_attempt=4)

   def test_entry_request_record_uses_state_machine_across_recovery(self):
      runtime = FakeRuntimePort()
      router = Router(
         node_id="entry-node",
         topology=FakeTopologyProvider(graph_fixture()),
         device_states=FakeDeviceStateProvider(state_table()),
         capacity=FakeCapacityPort(),
         runtime=runtime,
         transport=FakeTransportPort(),
         clock=ManualClock(),
         id_source=SequenceIdSource(),
         config=RouterConfig(),
      )
      request = request_fixture()
      router.admit(request, InMemoryClientSink())
      record = router.get_request(request.request_id)
      failed_placement = record.manifest.ordered_hops[1].placement_id
      runtime.fail_once(
         placement_id=failed_placement,
         phase="DECODE",
         token_index=0,
         scope="PLACEMENT",
      )

      self.assertTrue(router.decode_one(request.request_id))

      record = router.get_request(request.request_id)
      self.assertEqual(record.status, "DECODING")
      self.assertEqual(record.state_machine.state, "DECODING")
      self.assertEqual(record.state_machine.path_attempt, 1)


class PathAndHopStateMachineTests(unittest.TestCase):
   def test_path_locks_only_after_reservation(self):
      path = PathStateMachine(path_attempt=0)
      path.transition("RESERVED", path_attempt=0)
      path.transition("LOCKED", path_attempt=0)
      path.transition("RETIRING", path_attempt=0)
      self.assertEqual(path.state, "RETIRING")

   def test_hop_executes_then_forwards(self):
      hop = HopStateMachine(path_attempt=0)
      for state in ("QUEUED", "ACCEPTED", "EXECUTING", "FORWARDED"):
         hop.transition(state, path_attempt=0)
      self.assertEqual(hop.state, "FORWARDED")

   def test_terminal_hop_cannot_reenter_queue(self):
      hop = HopStateMachine(path_attempt=0)
      hop.transition("FAILED", path_attempt=0)
      with self.assertRaises(StateTransitionError):
         hop.transition("QUEUED", path_attempt=0)


if __name__ == "__main__":
   unittest.main()
