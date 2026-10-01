"""A distributed decode driver never spins re-sending an outstanding hop.

``decode_one_distributed`` dispatches the next decode hop and returns.  When a
driver calls it again before that hop's token has come back, the hop is not
sent again (a replay would be an idempotent no-op that only burns CPU and
contends with the transport threads that must deliver the token); the call
waits briefly for the token or any state change and reports progress.
"""

import threading
import time
import unittest
from dataclasses import replace

from mycelium_router import entry as entry_module
import test_router_inprocess_mesh


class DecodeRedriveTests(unittest.TestCase):
   setUp = test_router_inprocess_mesh.InProcessMeshTests.setUp

   def _start_and_hold_decode(self):
      self.entry.start_distributed_prefill(
         self.request,
         self.sink,
         excluded_placements=frozenset({"node-b-stage-000"}),
      )
      self.assertEqual(self.sink.token_ids, [])
      transport = self.entry.entry.transport
      held = []
      original = transport.send_hop

      def hold(header, payload):
         held.append((header, payload))

      transport.send_hop = hold
      return transport, original, held

   def test_redrive_before_the_token_returns_does_not_resend_the_hop(self):
      transport, original, held = self._start_and_hold_decode()
      request_id = self.request.request_id

      self.assertTrue(self.entry.decode_one_distributed(request_id))
      self.assertEqual(len(held), 1)

      started = time.monotonic()
      self.assertTrue(self.entry.decode_one_distributed(request_id))
      waited = time.monotonic() - started
      self.assertEqual(len(held), 1, "an outstanding decode hop was sent again")
      self.assertGreaterEqual(waited, entry_module._DECODE_REDRIVE_WAIT_SECONDS * 0.8)

      # Delivering the held hop completes the token; the next call dispatches
      # the following token's hop.
      transport.send_hop = original
      original(*held[0])
      self.assertEqual(self.sink.token_ids, [101])
      transport.send_hop = lambda header, payload: held.append((header, payload))
      self.assertTrue(self.entry.decode_one_distributed(request_id))
      self.assertEqual(len(held), 2)
      self.assertEqual(held[1][0].token_index, 1)

   def test_redrive_wait_ends_as_soon_as_the_token_arrives(self):
      transport, original, held = self._start_and_hold_decode()
      request_id = self.request.request_id
      self.assertTrue(self.entry.decode_one_distributed(request_id))
      sends = []

      def counting(header, payload):
         sends.append(header)
         original(header, payload)

      transport.send_hop = counting
      errors = []

      def deliver_soon():
         time.sleep(0.01)
         try:
            original(*held[0])
         except BaseException as error:  # surfaced below
            errors.append(error)

      deliverer = threading.Thread(target=deliver_soon)
      deliverer.start()
      started = time.monotonic()
      self.assertTrue(self.entry.decode_one_distributed(request_id))
      waited = time.monotonic() - started
      deliverer.join(timeout=5)
      self.assertEqual(errors, [])
      self.assertEqual(self.sink.token_ids, [101])
      self.assertLess(waited, entry_module._DECODE_REDRIVE_WAIT_SECONDS)
      self.assertEqual(
         [header for header in sends if header.hop_index == 0],
         [],
         "the redrive re-sent the outstanding entry hop",
      )

   def test_a_new_path_attempt_dispatches_again(self):
      transport, original, held = self._start_and_hold_decode()
      request_id = self.request.request_id
      self.assertTrue(self.entry.decode_one_distributed(request_id))
      record = self.entry.entry.get_request(request_id)
      with record.lock:
         record.manifest = replace(
            record.manifest, path_attempt=record.manifest.path_attempt + 1
         )
      dispatched = []
      original_dispatch = self.entry.entry.relay.dispatch_if_current

      def recording_dispatch(**kwargs):
         dispatched.append(kwargs["path_attempt"])
         return True

      self.entry.entry.relay.dispatch_if_current = recording_dispatch
      try:
         started = time.monotonic()
         self.assertTrue(self.entry.decode_one_distributed(request_id))
         self.assertLess(
            time.monotonic() - started, entry_module._DECODE_REDRIVE_WAIT_SECONDS
         )
      finally:
         self.entry.entry.relay.dispatch_if_current = original_dispatch
      self.assertEqual(dispatched, [record.manifest.path_attempt])


if __name__ == "__main__":
   unittest.main()
