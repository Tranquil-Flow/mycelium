# SPDX-License-Identifier: AGPL-3.0-only
"""A7 option (b): the A4 process-exit claim at the entry, and the old-path fence.

The route hands an exited non-entry participant to the request's entry with
``a7_source_lost``.  The entry accepts first (ownership only, no cutover work),
then reports the covered source PLACEMENT failure itself, which runs the
ordinary fenced KV-successor cutover or its truthful fallback.
"""

from __future__ import annotations

from pathlib import Path

from mycelium_a7_node_wiring import A7NodeWiring
from tests.a7_acceptance.test_entry_cutover import (
   _MAX_NEW,
   STANDBY_ID,
   _Harness,
   _reference_tokens,
   _request,
)
from mycelium_router.fakes import InMemoryClientSink


def _decode_until(harness: _Harness, request_id: str, sink, committed: int) -> None:
   harness.entry.start_locked_distributed_prefill(
      _request(request_id), sink, manifest=harness.manifest(request_id), pinned_deployment=harness.graph
   )
   while len(sink.token_ids) < committed:
      assert harness.entry.decode_one_distributed(request_id)


def _finish(harness: _Harness, request_id: str) -> None:
   for _ in range(4 * _MAX_NEW):
      if harness.entry.request_status(request_id) != "DECODING":
         return
      harness.entry.decode_one_distributed(request_id)


def _wiring(harness: _Harness) -> A7NodeWiring:
   wiring = object.__new__(A7NodeWiring)
   wiring.controller = harness.controller
   return wiring


def test_accept_first_then_the_entry_cuts_over_to_the_standby(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   fenced: list[tuple[str, str, int]] = []
   harness.controller.fence_old_path = lambda *scope: fenced.append(scope)
   request_id = "request:a7-source-lost"
   sink = InMemoryClientSink()
   _decode_until(harness, request_id, sink, 2)
   harness.source_port.killed = True
   old = harness.entry.entry.get_request(request_id).manifest

   decision = _wiring(harness).accept_source_lost(request_id, "node-b")

   # Accepting is ownership only: no fence, no cutover, still the old path.
   assert decision == {
      "accepted": True,
      "reason": "source_lost_accepted",
      "repeated": False,
      "source_placement_id": harness.source_id,
      "cutover_pending": True,
   }
   assert fenced == []
   assert harness.entry.entry.get_request(request_id).manifest is old
   assert harness.controller.snapshot(request_id)["finished"] is False

   assert _wiring(harness).cut_over_lost_source(harness.entry, request_id, "node-b") is True
   # A repeated claim after the cutover is the same ownership, with no new work.
   again = _wiring(harness).accept_source_lost(request_id, "node-b")
   assert again["accepted"] is True and again["repeated"] is True
   assert again["cutover_pending"] is False
   assert _wiring(harness).cut_over_lost_source(harness.entry, request_id, "node-b") is False
   _finish(harness, request_id)

   record = harness.entry.entry.get_request(request_id)
   assert harness.entry.request_status(request_id) == "COMPLETED"
   assert record.kv_successor_cutover["outcome"] == "fenced_kv_successor"
   assert record.kv_successor_cutover["recovery_prefill"] is False
   assert record.manifest.ordered_hops[1].placement_id == STANDBY_ID
   assert sink.token_ids == _reference_tokens(harness.case)
   assert sink.token_indexes == list(range(_MAX_NEW))
   assert harness.standby_port.kv_snapshot()["prefill_operation_count"] == 0
   # The old attempt's forwards were fenced exactly once, at cutover.
   assert fenced == [(request_id, old.path_id, old.path_attempt)]
   # Once released terminally the entry no longer owns the exit.
   assert _wiring(harness).accept_source_lost(request_id, "node-b") == {"accepted": False, "reason": "request_terminal"}


def test_claim_after_a_transport_observed_cutover_is_owned_without_new_work(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   request_id = "request:a7-observed-first"
   sink = harness.run(request_id, kill_after=2)
   assert harness.entry.entry.get_request(request_id).kv_successor_cutover["outcome"] == "fenced_kv_successor"
   assert sink.token_ids == _reference_tokens(harness.case)

   decision = harness.controller.accept_source_lost(request_id, "node-b")

   # finish() released the request terminally: the route keeps today's path.
   assert decision == {"accepted": False, "reason": "request_terminal"}


def test_incompatible_standby_claim_falls_back_truthfully_and_fences(tmp_path: Path) -> None:
   harness = _Harness(tmp_path, standby_dtype="float16")
   fenced: list[tuple[str, str, int]] = []
   harness.controller.fence_old_path = lambda *scope: fenced.append(scope)
   request_id = "request:a7-incompatible"
   sink = InMemoryClientSink()
   _decode_until(harness, request_id, sink, 2)
   harness.source_port.killed = True
   old = harness.entry.entry.get_request(request_id).manifest

   assert harness.controller.accept_source_lost(request_id, "node-b")["accepted"] is True
   _wiring(harness).cut_over_lost_source(harness.entry, request_id, "node-b")

   record = harness.entry.entry.get_request(request_id)
   result = record.kv_successor_cutover
   assert result["outcome"] == "full_context_replay"
   assert result["reason"] == "compatibility_incompatible"
   assert result["kv_continuity_claimed"] is False
   assert harness.standby_port.kv_snapshot()["decode_operation_count"] == 0
   assert fenced == [(request_id, old.path_id, old.path_attempt)]


def test_claims_the_entry_does_not_cover_are_refused(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   request_id = "request:a7-refusals"
   sink = InMemoryClientSink()
   _decode_until(harness, request_id, sink, 2)

   assert harness.controller.accept_source_lost("request:unknown", "node-b") == {
      "accepted": False,
      "reason": "request_not_covered",
   }
   for node in ("node-a", "node-c"):
      assert harness.controller.accept_source_lost(request_id, node) == {
         "accepted": False,
         "reason": "lost_node_not_covered_source",
      }
   assert _wiring(harness).cut_over_lost_source(harness.entry, request_id, "node-c") is False
   assert harness.controller.snapshot(request_id)["source_lost_accepted"] is False
   _finish(harness, request_id)
   assert sink.token_ids == _reference_tokens(harness.case)


def test_report_lost_source_needs_a_decoding_request_through_that_placement(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   request_id = "request:a7-report-guard"
   assert harness.entry.entry.report_lost_source("request:missing", harness.source_id) is False
   sink = InMemoryClientSink()
   _decode_until(harness, request_id, sink, 1)
   assert harness.entry.entry.report_lost_source(request_id, STANDBY_ID) is False
   assert harness.entry.request_status(request_id) == "DECODING"
   _finish(harness, request_id)
   assert harness.entry.entry.report_lost_source(request_id, harness.source_id) is False


def test_a_driver_decode_landing_mid_cutover_waits_for_the_successor_token(tmp_path: Path) -> None:
   """A liveness-reported cutover can begin between two driver decode commands.

   The decode the driver sends while the entry is mid-cutover (request in
   recovery, not DECODING) must report the step in flight: the successor
   re-sends it.  Reading the recovery state as "nothing dispatched" made the
   physical route raise ``route_decode_stalled`` after a successful cutover.
   """

   harness = _Harness(tmp_path)
   request_id = "request:a7-driver-mid-cutover"
   sink = InMemoryClientSink()
   _decode_until(harness, request_id, sink, 2)
   harness.source_port.killed = True
   observed: list[tuple[str, bool, int]] = []
   real_cutover = harness.controller.cutover

   def cutover(**kwargs):
      status = harness.entry.request_status(request_id)
      in_flight = harness.entry.decode_one_distributed(request_id)
      observed.append((status, in_flight, len(sink.token_ids)))
      return real_cutover(**kwargs)

   harness.controller.cutover = cutover
   assert harness.controller.accept_source_lost(request_id, "node-b")["accepted"] is True
   assert _wiring(harness).cut_over_lost_source(harness.entry, request_id, "node-b") is True

   # The mid-cutover decode dispatched nothing on the dead path and reported
   # the step in flight; the successor's re-sent step produced its token.
   assert observed == [("PREFILL", True, 2)]
   assert len(sink.token_ids) == 3
   _finish(harness, request_id)
   record = harness.entry.entry.get_request(request_id)
   assert record.kv_successor_cutover["outcome"] == "fenced_kv_successor"
   assert harness.entry.request_status(request_id) == "COMPLETED"
   assert sink.token_ids == _reference_tokens(harness.case)
   assert sink.token_indexes == list(range(_MAX_NEW))


def test_a_decode_before_the_first_token_is_not_reported_in_flight(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   request_id = "request:a7-no-token-yet"
   record_type = harness.entry.entry
   sink = InMemoryClientSink()
   _decode_until(harness, request_id, sink, 1)
   record = record_type.get_request(request_id)
   saved = list(record.generated_token_ids)
   with record.lock:
      record.generated_token_ids.clear()
      record.state_machine.begin_recovery(path_attempt=record.state_machine.path_attempt + 1)
   assert harness.entry.decode_one_distributed(request_id) is False
   with record.lock:
      record.generated_token_ids.extend(saved)
