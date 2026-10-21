# SPDX-License-Identifier: AGPL-3.0-only
"""The entry announces an accepted request to its relay before the path registers.

Without the announcement a request accepted a millisecond after another is
invisible when the first one's prefill hop is queued, so pairing is a race
(A10 live finding f7b2).  The announcement ends when the start returns or fails,
and when the request's own hop is queued on the relay.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from mycelium_router.router import Router


class _Relay:
   def __init__(self):
      self.events = []

   def begin_admission(self, request_id):
      self.events.append(("begin", request_id))

   def end_admission(self, request_id):
      self.events.append(("end", request_id))


def _router(relay, entry):
   router = object.__new__(Router)
   router.relay = relay
   router.entry = entry
   return router


@pytest.mark.parametrize(
   "method", ["admit", "start_distributed_prefill", "start_locked_distributed_prefill"]
)
def test_every_entry_start_is_announced_before_it_runs_and_ended_after(method):
   relay = _Relay()

   def start(request, sink, **kwargs):
      relay.events.append(("start", request.request_id))
      return request.request_id

   router = _router(relay, SimpleNamespace(**{method: start}))
   request = SimpleNamespace(request_id="request-x")
   assert getattr(router, method)(request, object()) == "request-x"
   assert relay.events == [("begin", "request-x"), ("start", "request-x"), ("end", "request-x")]


def test_a_failed_start_ends_its_announcement():
   relay = _Relay()

   def start(request, sink, **kwargs):
      raise RuntimeError("boom")

   router = _router(relay, SimpleNamespace(start_locked_distributed_prefill=start))
   with pytest.raises(RuntimeError):
      router.start_locked_distributed_prefill(SimpleNamespace(request_id="request-x"), object())
   assert relay.events == [("begin", "request-x"), ("end", "request-x")]
