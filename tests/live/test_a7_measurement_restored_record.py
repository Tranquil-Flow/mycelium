# SPDX-License-Identifier: AGPL-3.0-only
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
      processes: tuple[dict[str, Any], dict[str, Any]] | None = None,
      old_exit_status: int | None = -15,
      replacement_state: dict[str, Any] | None = None,
      old_transport: str | None = "local",
   ) -> None:
      self._old_transport = old_transport
      self._processes = processes
      self._old_exit_status = old_exit_status
      self._replacement_state = replacement_state or {}
      self._fault = fault
      self._refused = refused_after_kill
      self._restored = restored
      self._live = live_result
      self._pre_kill_counters = pre_kill_counters
      self._before_counters = before_counters

   def _entry_process(self, *, serving: bool) -> dict[str, Any] | None:
      """The verified entry process identity; the replacement once killed unless ``serving``."""

      if self._processes is None:
         return None
      old, new = self._processes
      return old if serving or not self._fault.killed.is_set() else new

   def resources(self) -> dict[str, Any]:
      entry = {**_NODE, "a7_roles": ["entry", "standby"], "standby_runtime": dict(self._before_counters)}
      document = {
         "entry_node_id": "node-0",
         "exited_nodes": [],
         "gateway_reservations": 0,
         "route_request_registrations": 0,
         "nodes": {"node-0": entry, "node-2": {**_NODE, "a7_roles": ["source"], "standby_runtime": None}},
      }
      if self._processes is not None:
         entry["process"] = self._entry_process(serving=False)
         if self._fault.killed.is_set():
            document["exited_nodes"] = ["node-2"]
            del document["nodes"]["node-2"]
            old, new = self._processes
            if old != new:
               entry.update(self._replacement_state)
               document["retired_entry_processes"] = [
                  {
                     "node_id": "node-0",
                     **old,
                     "process_transport": self._old_transport,
                     "exit_status": self._old_exit_status,
                     "observed_by": "retired_route_session_exit_status",
                  }
               ]
      return document

   def _document(self, request_id: str, request: dict[str, Any] | None, counters: dict[str, int] | None) -> dict[str, Any]:
      document = {
         "request_id": request_id,
         "a7": {
            "request": request,
            "agent": {"shadow_checkpoint_count": 2, "shadow_checkpoint_payload_bytes": 64},
            "standby_runtime": None if counters is None else dict(counters),
         },
         "scoped_incidents": [],
      }
      if self._processes is not None:
         serving = request is not None and "restored_from" not in request
         document["entry_process"] = self._entry_process(serving=serving)
      return document

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


_OLD_PROCESS = {"host_id": "host-0", "process_id": 4100}
_NEW_PROCESS = {"host_id": "host-0", "process_id": 4200}
_NOT_MEASURED_CLEANUP = {"cleanup_measurement": "not_measured", "cleanup_unmeasured_reason": "entry_process_replaced"}


def _not_measured(cleanup: dict[str, Any]) -> dict[str, Any]:
   node = cleanup["node-0"]
   assert {key: node[key] for key in _NOT_MEASURED_CLEANUP} == _NOT_MEASURED_CLEANUP
   assert node["cleanup_proof"] == "process_exit"
   assert not any(key.endswith("_delta") for key in node)
   return node["cleanup_proof_evidence"]


@pytest.mark.parametrize("processes", [None, (_OLD_PROCESS, _NEW_PROCESS)])
def test_a_restored_record_reports_cleanup_as_not_measured(processes) -> None:
   fields, observations = _measure({"refused_after_kill": 2, "processes": processes})
   evidence = _not_measured(fields["cleanup"])
   assert observations["cleanup"]["cleanup_proof_evidence"] == evidence
   cleanup = observations["cleanup"]
   assert cleanup["cleanup_measurement"] == "not_measured"
   assert cleanup["cleanup_unmeasured_reason"] == "entry_process_replaced"
   assert cleanup["restored_from"] == _RESTORED
   if processes is not None:
      assert cleanup["entry_process_before"] == _OLD_PROCESS == cleanup["entry_process_at_kill"]
      assert cleanup["entry_process_after"] == _NEW_PROCESS
      assert cleanup["exited_nodes_after"] == ["node-2"]


def test_a_replaced_entry_process_without_a_restored_read_is_not_measured() -> None:
   # The original process answered with its own record, then the route was replaced.
   fields, observations = _measure({"refused_after_kill": 0, "live_result": True, "processes": (_OLD_PROCESS, _NEW_PROCESS)})
   _not_measured(fields["cleanup"])
   assert observations["cleanup"]["restored_from"] is None
   assert observations["cleanup"]["entry_process_after"] == _NEW_PROCESS


@pytest.mark.parametrize("processes", [None, (_OLD_PROCESS, _OLD_PROCESS)])
def test_same_process_cleanup_is_measured_exactly_as_before(processes) -> None:
   fields, observations = _measure({"refused_after_kill": 0, "live_result": True, "processes": processes})
   assert fields["cleanup"] == {
      "node-0": {
         "active_state_count_delta": 0,
         "active_kv_bytes_delta": 0,
         "shadow_bytes_delta": 0,
         "reservation_delta": 0,
         "fence_records_delta": 0,
      }
   }
   assert observations["cleanup"] == {"cleanup_measurement": "measured"}


def test_a_replaced_entry_records_the_observed_process_exit_evidence() -> None:
   fields, _observations = _measure({"refused_after_kill": 2, "processes": (_OLD_PROCESS, _NEW_PROCESS)})
   evidence = _not_measured(fields["cleanup"])
   assert evidence == {
      "old_entry_process": _OLD_PROCESS,
      "old_entry_exit": {**_OLD_PROCESS, "process_transport": "local", "exit_status": -15, "observed_by": "retired_route_session_exit_status"},
      "replacement_entry_process": _NEW_PROCESS,
      "replacement_terminal_state": {
         "active_state_count": 0,
         "active_kv_bytes": 0,
         "shadow_kv_bytes": 0,
         "shadow_checkpoint_count": 0,
         "entry_fence_record_count": 0,
         "route_request_registrations": 0,
         "gateway_reservations": 0,
         "request_live": False,
      },
      "old_entry_process_live": False,
   }


def test_process_exit_evidence_records_what_is_missing_without_concluding() -> None:
   fields, _observations = _measure(
      {
         "refused_after_kill": 2,
         "processes": (_OLD_PROCESS, _NEW_PROCESS),
         "old_exit_status": None,
         "replacement_state": {"active_state_count": 1},
      }
   )
   evidence = _not_measured(fields["cleanup"])
   assert evidence["old_entry_exit"]["exit_status"] is None
   assert evidence["replacement_terminal_state"]["active_state_count"] == 1


def test_measured_process_exit_proof_passes_the_gate_and_a_missing_exit_fails_it() -> None:
   from tests.a7_acceptance.test_kv_successor_gate import CANDIDATE, _record, gate

   plan = gate.build_plan(CANDIDATE)
   fields, _observations = _measure({"refused_after_kill": 2, "processes": (_OLD_PROCESS, _NEW_PROCESS)})
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=fields["cleanup"]), plan) == []
   fields, _observations = _measure({"refused_after_kill": 2, "processes": (_OLD_PROCESS, _NEW_PROCESS), "old_exit_status": None})
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=fields["cleanup"]), plan) == ["process_exit_not_observed"]
   fields, _observations = _measure({"refused_after_kill": 2})  # no verified process identity at all
   assert "process_exit_old_process_unobserved" in gate.evaluate_case(_record(plan, gate.ABORT, cleanup=fields["cleanup"]), plan)


def test_measured_ssh_entry_exit_is_remote_unconfirmed_at_the_gate() -> None:
   from tests.a7_acceptance.test_kv_successor_gate import CANDIDATE, _record, gate

   plan = gate.build_plan(CANDIDATE)
   fields, _observations = _measure({"refused_after_kill": 2, "processes": (_OLD_PROCESS, _NEW_PROCESS), "old_transport": "ssh"})
   assert fields["cleanup"]["node-0"]["cleanup_proof_evidence"]["old_entry_exit"]["process_transport"] == "ssh"
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=fields["cleanup"]), plan) == ["process_exit_remote_unconfirmed"]
