# SPDX-License-Identifier: AGPL-3.0-only
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
from unittest import mock

from mycelium_router import entry as entry_module
import test_router_inprocess_mesh


class _RedriveClock:
   """The entry module's clock for the redrive wait, advanced only by its polls.

   ``_await_decode_progress`` reads ``time.monotonic()`` for its deadline and
   sleeps one poll between looks at the record.  Here the deadline is measured
   in *polls*: ``monotonic`` returns a counter that only ``sleep`` advances, so
   a host that is slow to schedule either thread can neither expire the wait
   early nor let the token land before the waiter is parked.  ``sleep`` also
   reports that the waiter is parked (its first look found no token) and, on
   that first poll, holds until the test has delivered the token, which models
   a token that arrives while the redrive is waiting.
   """

   def __init__(self, hold_first_poll=False):
      self.now = 0.0
      self.sleeps = []
      self.parked = threading.Event()
      self.delivered = threading.Event()
      self._hold_first_poll = hold_first_poll

   def monotonic(self):
      return self.now

   def sleep(self, seconds):
      first = not self.sleeps
      self.sleeps.append(seconds)
      if first:
         self.parked.set()
         if self._hold_first_poll:
            self.delivered.wait()  # set by the deliverer's finally: never a hang
      self.now += seconds


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
      clock = _RedriveClock(hold_first_poll=True)

      def deliver_when_parked():
         # The token arrives only once the redrive is provably waiting for it.
         try:
            clock.parked.wait()
            original(*held[0])
         except BaseException as error:  # surfaced below
            errors.append(error)
         finally:
            clock.delivered.set()

      deliverer = threading.Thread(target=deliver_when_parked)
      deliverer.start()
      try:
         with mock.patch.object(entry_module, "time", clock):
            started = clock.monotonic()
            self.assertTrue(self.entry.decode_one_distributed(request_id))
            waited = clock.monotonic() - started
      finally:
         clock.parked.set()
         deliverer.join()
      self.assertEqual(errors, [])
      self.assertEqual(self.sink.token_ids, [101])
      self.assertLess(waited, entry_module._DECODE_REDRIVE_WAIT_SECONDS)
      self.assertEqual(
         clock.sleeps,
         [entry_module._DECODE_REDRIVE_POLL_SECONDS],
         "the wait kept polling after the token arrived",
      )
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
      clock = _RedriveClock()
      try:
         with mock.patch.object(entry_module, "time", clock):
            started = clock.monotonic()
            self.assertTrue(self.entry.decode_one_distributed(request_id))
            waited = clock.monotonic() - started
      finally:
         self.entry.entry.relay.dispatch_if_current = original_dispatch
      self.assertLess(waited, entry_module._DECODE_REDRIVE_WAIT_SECONDS)
      self.assertEqual(clock.sleeps, [], "a new path attempt waited instead of dispatching")
      self.assertEqual(dispatched, [record.manifest.path_attempt])


if __name__ == "__main__":
   unittest.main()
