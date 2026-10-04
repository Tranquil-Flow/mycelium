# SPDX-License-Identifier: AGPL-3.0-or-later
"""A restarted entry's durable cutover record supplies the record, never counters.

Pure fakes: no node, serve, sidecar or fleet.  The fakes reproduce the
``no_a6_successor_aborts`` shape: the request ends as soon as the source is
killed and the route cold-restarts, so the entry that answers later reads is a
new process.  It serves the durable cutover record (``restored_from``) beside
its own fresh runtime counters, which must never be compared with the
counters of the process that served the request.
"""
from __future__ import annotations

from typing import Any

import pytest

from mycelium_a7_physical_measurement import MeasurementRefused, MeasurementSettings, measure
from tests.live.test_a7_measurement_result_poll import _NODE, _Driver, _Fault

_RESTORED = "durable_last_cutover"
_SAME_PROCESS_COUNTERS = {"prefill_operation_count": 1, "prefill_input_token_count": 4, "decode_operation_count": 3}
_NEW_PROCESS_COUNTERS = {"prefill_operation_count": 0, "prefill_input_token_count": 0, "decode_operation_count": 0}
_RESULT = {
   "protocol": "mycelium.a7_entry_cutover.v1",
   "outcome": "aborted",
   "reason": "qualification_not_current",
   "gateway_committed_token_count": 3,
   "acknowledged_token_count": None,
   "cutover_ms": 1.5,
   "max_ack_wait_ms": 2.0,
   "kv_continuity_claimed": False,
   "terminal_released": True,
}
_SETTINGS = MeasurementSettings(result_timeout_s=3.0, settle_timeout_s=0.3, poll_s=0.01)


class _Evidence:
   """Entry evidence before and after a cold restart.

   ``refused_after_kill`` reads are refused (the route is being replaced);
   later reads come from the restarted entry.  ``restored`` decides whether
   the restarted entry serves the durable record; ``live_result`` instead
   keeps the original process answering with its own in-memory record.
   """

   def __init__(
      self,
      fault: _Fault,
      *,
      refused_after_kill: int,
      restored: bool = True,
      live_result: bool = False,
      pre_kill_counters: dict[str, int] | None = _SAME_PROCESS_COUNTERS,
      before_counters: dict[str, int] = _SAME_PROCESS_COUNTERS,
   ) -> None:
      self._fault = fault
      self._refused = refused_after_kill
      self._restored = restored
      self._live = live_result
      self._pre_kill_counters = pre_kill_counters
      self._before_counters = before_counters

   def resources(self) -> dict[str, Any]:
      return {
         "entry_node_id": "node-0",
         "exited_nodes": [],
         "gateway_reservations": 0,
         "route_request_registrations": 0,
         "nodes": {
            "node-0": {**_NODE, "a7_roles": ["entry", "standby"], "standby_runtime": dict(self._before_counters)},
            "node-2": {**_NODE, "a7_roles": ["source"], "standby_runtime": None},
         },
      }

   def _document(self, request_id: str, request: dict[str, Any] | None, counters: dict[str, int] | None) -> dict[str, Any]:
      return {
         "request_id": request_id,
         "a7": {
            "request": request,
            "agent": {"shadow_checkpoint_count": 2, "shadow_checkpoint_payload_bytes": 64},
            "standby_runtime": None if counters is None else dict(counters),
         },
         "scoped_incidents": [],
      }

   def request(self, request_id: str) -> dict[str, Any]:
      state = {"committed": 3, "acknowledged": -1, "ack_lag_events": 3, "commits_sent": 3, "result": None}
      if not self._fault.killed.is_set():
         return self._document(request_id, state, self._pre_kill_counters)
      if self._live:
         return self._document(request_id, {**state, "finished": True, "result": dict(_RESULT)}, self._pre_kill_counters)
      if self._refused > 0:
         self._refused -= 1
         raise MeasurementRefused("a7_evidence_route_unavailable")
      if not self._restored:
         return self._document(request_id, None, _NEW_PROCESS_COUNTERS)
      restored = {**state, "finished": True, "result": dict(_RESULT), "restored_from": _RESTORED}
      return self._document(request_id, restored, _NEW_PROCESS_COUNTERS)


def _measure(evidence_kwargs: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
   fault = _Fault()
   return measure(driver=_Driver(fault), evidence=_Evidence(fault, **evidence_kwargs), fault=fault, settings=_SETTINGS)


@pytest.mark.parametrize("refused_after_kill", [0, 5])
def test_restored_record_supplies_the_result_and_counters_stay_same_process(refused_after_kill: int) -> None:
   fields, observations = _measure({"refused_after_kill": refused_after_kill})
   assert fields["outcome"] == "aborted"
   assert fields["reason"] == "qualification_not_current"
   # Counters are the serving process's own (no delta across processes).
   assert observations["standby_counters"]["after"] == list(_SAME_PROCESS_COUNTERS.values())
   assert fields["successor_recovery_prefill_operations"] == 0
   assert fields["successor_decode_operations"] == 0
   reads = observations["evidence_reads"]
   assert reads["result_restored_from"] == _RESTORED
   assert reads["counter_read"] == "pre_kill"
   assert reads["result_read"] is not None
   assert observations["result_poll"]["restored_from"] == _RESTORED
   assert observations["a7_result_observed"] is True


def test_restored_record_without_a_same_process_counter_read_is_refused() -> None:
   with pytest.raises(MeasurementRefused) as caught:
      _measure({"refused_after_kill": 0, "pre_kill_counters": None, "before_counters": _NEW_PROCESS_COUNTERS})
   assert caught.value.code == "a7_standby_counters_unobserved"


def test_restarted_entry_without_a_record_is_still_unobserved() -> None:
   with pytest.raises(MeasurementRefused) as caught:
      _measure({"refused_after_kill": 2, "restored": False})
   assert caught.value.code == "a7_result_unobserved"
   assert caught.value.observations["evidence_reads"]["result_read"] is None


def test_a_live_same_process_record_behaves_as_before() -> None:
   fields, observations = _measure({"refused_after_kill": 0, "live_result": True})
   assert fields["outcome"] == "aborted"
   reads = observations["evidence_reads"]
   assert reads["result_restored_from"] is None
   assert reads["result_read"].startswith("poll:")
   assert reads["counter_read"] == "final" or reads["counter_read"].startswith("poll:")
   assert "restored_from" not in observations["result_poll"]
   assert observations["standby_counters"]["after"] == list(_SAME_PROCESS_COUNTERS.values())
