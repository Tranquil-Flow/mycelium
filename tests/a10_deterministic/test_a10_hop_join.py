# SPDX-License-Identifier: AGPL-3.0-only
"""Smart live-hop collection: wait only while another request can still join.

``HopArrivalTracker`` predicts, from arrivals this node observed, which other
active requests may send a hop inside the collection window.  The live transport
drains at once when there is no such partner, so a request alone on a node
never pays the window, and requests already in step (all queued together) never
wait either.
"""

from __future__ import annotations

import time

import pytest

from mycelium_router.hop_join import HopArrivalTracker

from tests.a10_deterministic.test_a10_live_hop_batching import (  # noqa: F401
   _CountingRouter,
   _transport,
   _Hub,
   runtime_case,
)

ACTIVE = frozenset({"path-a", "path-b", "path-c"})


def _due(tracker, *, now, window=0.05, queued=(), active=ACTIVE):
   return tracker.partners_due(
      now=now,
      window_seconds=window,
      queued_paths=frozenset(queued),
      active_paths=active,
   )


def test_nobody_seen_means_nobody_to_wait_for():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0)
   assert _due(tracker, now=10.0) == 0


def test_a_request_seen_once_recently_may_join_unless_queued_inactive_or_terminal():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0)
   tracker.record("path-b", at=9.0, terminal=False)
   assert _due(tracker, now=10.0) == 1
   assert _due(tracker, now=10.0, queued={"path-b"}) == 0
   assert _due(tracker, now=10.0, active=frozenset({"path-a"})) == 0
   tracker.record("path-c", at=9.5, terminal=True)
   assert _due(tracker, now=10.0) == 1, "a request whose last hop was terminal never joins"


def test_a_stale_request_is_pruned_and_never_waited_for():
   tracker = HopArrivalTracker(join_horizon_seconds=2.0)
   tracker.record("path-b", at=1.0, terminal=False)
   assert _due(tracker, now=4.0) == 0
   assert tracker.tracked() == 0


def test_a_periodic_request_is_waited_for_only_when_due_inside_the_window():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0)
   tracker.record("path-b", at=10.0, terminal=False)
   tracker.record("path-b", at=10.5, terminal=False)  # period 0.5 s, next 11.0
   assert _due(tracker, now=10.96, window=0.05) == 1, "due within the window"
   assert _due(tracker, now=10.6, window=0.05) == 0, "due well after the window"
   assert _due(tracker, now=11.04, window=0.05) == 1, "a little late (jitter)"
   assert _due(tracker, now=11.2, window=0.05) == 1, (
      "overdue: its hop is most likely waiting behind the batch just executed"
   )
   assert _due(tracker, now=16.0, window=0.05) == 0, "past the join horizon"


def test_forget_and_bound():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0, maximum_paths=2)
   for index, path in enumerate(("path-a", "path-b", "path-c")):
      tracker.record(path, at=float(index), terminal=False)
   assert tracker.tracked() == 2
   tracker.forget("path-c")
   assert tracker.tracked() == 1


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_invalid_horizon_is_refused(bad):
   with pytest.raises(ValueError, match="invalid_join_horizon"):
      HopArrivalTracker(join_horizon_seconds=bad)


class _JoinRouter(_CountingRouter):
   def __init__(self, pending, cap, partners, window=0.5):
      super().__init__(pending, cap, window)
      self.partners = partners

   def hop_join_partners_due(self):
      return self.partners


def test_transport_drains_at_once_when_no_partner_can_join(
   runtime_case,  # noqa: F811 - pytest fixture
):
   transport = _transport(_Hub(), runtime_case)
   router = _JoinRouter(pending=1, cap=4, partners=0)
   transport._queue_hop_for_batch(router)
   assert (router.pumps, transport._hop_collection_deadline) == (1, None)


def test_transport_collects_while_a_partner_is_due_and_drains_when_none_remains(
   runtime_case,  # noqa: F811 - pytest fixture
):
   transport = _transport(_Hub(), runtime_case)
   router = _JoinRouter(pending=1, cap=4, partners=1, window=0.5)
   opened = time.monotonic()
   transport._queue_hop_for_batch(router)
   deadline = transport._hop_collection_deadline
   assert router.pumps == 0
   assert deadline is not None and deadline >= opened + 0.5
   # The partner's hop arrives: nobody else is due, so the batch drains now,
   # long before the window would have closed.
   router.pending = 2
   router.partners = 0
   transport._queue_hop_for_batch(router)
   assert (router.pumps, transport._hop_collection_deadline) == (1, None)


def test_a_registered_path_is_expected_once_and_registration_never_yields_a_period():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0)
   tracker.record_registration("path-b", at=9.0)
   assert _due(tracker, now=10.0) == 1, "a just-registered path's first hop is due"
   tracker.record("path-b", at=12.0, terminal=False)
   # The first real hop has no period (registration is not a hop) ...
   assert _due(tracker, now=12.1, window=0.01) == 1
   tracker.record("path-b", at=12.5, terminal=False)
   # ... the second one does: next hop predicted at 13.0.
   assert _due(tracker, now=12.6, window=0.05) == 0
   assert _due(tracker, now=12.97, window=0.05) == 1
   # A later registration of a known path changes nothing.
   tracker.record_registration("path-b", at=12.6)
   assert _due(tracker, now=12.6, window=0.05) == 0


def test_an_admission_in_flight_is_a_partner_until_it_registers_ends_or_goes_stale():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0)
   tracker.begin_admission("request-b", at=10.0)
   assert _due(tracker, now=10.001, active=frozenset()) == 1
   # Registration is the same request id ending its admission.
   tracker.end_admission("request-b")
   assert _due(tracker, now=10.002, active=frozenset()) == 0
   # An admission nobody ended is dropped at the join horizon.
   tracker.begin_admission("request-c", at=10.0)
   assert _due(tracker, now=14.9, active=frozenset()) == 1
   assert _due(tracker, now=15.1, active=frozenset()) == 0
   assert tracker.admitting() == 0


def test_admissions_are_bounded():
   tracker = HopArrivalTracker(join_horizon_seconds=5.0, maximum_paths=3)
   for index in range(10):
      tracker.begin_admission(f"request-{index}", at=1.0)
   assert tracker.admitting() == 3
