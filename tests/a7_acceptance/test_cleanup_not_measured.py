# SPDX-License-Identifier: AGPL-3.0-only
"""A7: cleanup that could not be measured (replaced entry process) never passes.

When the route cold-restarts and replaces the entry process, the measurement
reports the case's cleanup as not measured instead of a delta across two
processes.  The gate requires a measured terminal resource delta for every
case (the abort case included), so the record is still written by the case
runner and the gate shows the requirement as unmet.  The evidence door names
the verified process that signed each snapshot, so a replacement is visible.
"""

from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from typing import Any

import pytest
from mycelium_a7_evidence_control import RouteA7Evidence
from tests.a7_acceptance.test_kv_successor_gate import CANDIDATE, _record, _records, gate

_RUNNER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_a7_kv_successor_case_runner.py"
_RUNNER_SPEC = importlib.util.spec_from_file_location("run_a7_kv_successor_case_runner_for_cleanup", _RUNNER_PATH)
runner = importlib.util.module_from_spec(_RUNNER_SPEC)
assert _RUNNER_SPEC.loader is not None
_RUNNER_SPEC.loader.exec_module(runner)

_NOT_MEASURED = {
   "node-0": {
      "cleanup_measurement": "not_measured",
      "cleanup_unmeasured_reason": "entry_process_replaced",
      "cleanup_proof": None,
      "cleanup_proof_evidence": None,
   }
}
_OLD = {"host_id": "host-0", "process_id": 4100}
_NEW = {"host_id": "host-0", "process_id": 4200}
_EVIDENCE = {
   "old_entry_process": _OLD,
   "old_entry_exit": {**_OLD, "process_transport": "local", "exit_status": -15, "observed_by": "retired_route_session_exit_status"},
   "replacement_entry_process": _NEW,
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


def _process_exit(**changes: Any) -> dict[str, Any]:
   evidence = copy.deepcopy(_EVIDENCE)
   for path, value in changes.items():
      target = evidence
      *parents, leaf = path.split("__")
      for parent in parents:
         target = target[parent]
      target[leaf] = value
   return {
      "node-0": {
         "cleanup_measurement": "not_measured",
         "cleanup_unmeasured_reason": "entry_process_replaced",
         "cleanup_proof": "process_exit",
         "cleanup_proof_evidence": evidence,
      }
   }


def test_not_measured_cleanup_fails_the_abort_case_with_an_explicit_reason() -> None:
   plan = gate.build_plan(CANDIDATE)
   records = _records(plan)
   records[gate.ABORT] = _record(plan, gate.ABORT, cleanup=_NOT_MEASURED)
   verdict = gate.evaluate(plan, records)
   assert verdict["cases"][gate.ABORT] == ["terminal_resource_delta_not_measured"]
   assert verdict["passed"] is False


def test_not_measured_cleanup_fails_every_case_and_is_never_a_zero_delta() -> None:
   plan = gate.build_plan(CANDIDATE)
   for case in gate.CASES:
      failures = gate.evaluate_case(_record(plan, case, cleanup=_NOT_MEASURED), plan)
      assert "terminal_resource_delta_not_measured" in failures
      assert "terminal_resource_delta" not in failures
   # A malformed marker is still an invalid delta, not "not measured".
   for malformed in (
      {"node-0": {"cleanup_measurement": "not_measured"}},
      {"node-0": {"cleanup_measurement": "not_measured", "cleanup_unmeasured_reason": "entry_process_replaced"}},
      {"node-0": dict(_NOT_MEASURED["node-0"], cleanup_proof="measured_delta")},
   ):
      failures = gate.evaluate_case(_record(plan, gate.ABORT, cleanup=malformed), plan)
      assert failures == ["terminal_resource_delta"]


def test_verified_process_exit_is_accepted_as_a_distinct_cleanup_proof() -> None:
   plan = gate.build_plan(CANDIDATE)
   records = _records(plan)
   records[gate.ABORT] = _record(plan, gate.ABORT, cleanup=_process_exit())
   verdict = gate.evaluate(plan, records)
   assert verdict["cases"][gate.ABORT] == []
   assert verdict["passed"] is True
   assert verdict["cleanup_proofs"][gate.ABORT] == "process_exit"
   assert verdict["cleanup_proofs"][gate.POSITIVE] == "measured_delta"


@pytest.mark.parametrize(
   ("changes", "failure"),
   [
      # Without the old identity its exit cannot be tied to it either.
      ({"old_entry_process": None}, "process_exit_old_process_unobserved+process_exit_not_observed"),
      ({"old_entry_process__process_id": "4100"}, "process_exit_old_process_unobserved+process_exit_not_observed"),
      ({"old_entry_exit": None}, "process_exit_not_observed"),
      ({"old_entry_exit__exit_status": None}, "process_exit_not_observed"),
      ({"old_entry_exit__process_id": 4999}, "process_exit_not_observed"),
      ({"old_entry_exit__observed_by": "absent_read"}, "process_exit_not_observed"),
      ({"replacement_entry_process": None}, "process_exit_replacement_unobserved"),
      ({"replacement_entry_process": dict(_OLD)}, "process_exit_replacement_unobserved"),
      ({"replacement_terminal_state__active_state_count": 1}, "process_exit_replacement_not_clean"),
      ({"replacement_terminal_state__shadow_kv_bytes": 64}, "process_exit_replacement_not_clean"),
      ({"replacement_terminal_state__gateway_reservations": None}, "process_exit_replacement_not_clean"),
      ({"replacement_terminal_state__request_live": True}, "process_exit_replacement_not_clean"),
      ({"old_entry_process_live": True}, "process_exit_old_process_live"),
      ({"old_entry_process_live": None}, "process_exit_old_process_live"),
   ],
)
def test_each_missing_process_exit_condition_fails_with_its_own_code(changes, failure) -> None:
   plan = gate.build_plan(CANDIDATE)
   failures = gate.evaluate_case(_record(plan, gate.ABORT, cleanup=_process_exit(**changes)), plan)
   assert failures == failure.split("+")


def test_process_exit_evidence_of_the_wrong_shape_or_reason_is_refused() -> None:
   plan = gate.build_plan(CANDIDATE)
   cleanup = _process_exit()
   del cleanup["node-0"]["cleanup_proof_evidence"]["old_entry_process_live"]
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=cleanup), plan) == ["process_exit_evidence_invalid"]
   cleanup = _process_exit()
   cleanup["node-0"]["cleanup_unmeasured_reason"] = "entry_process_identity_unobserved"
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=cleanup), plan) == ["terminal_resource_delta_not_measured"]


def test_case_runner_writes_the_not_measured_record_for_the_gate_to_fail() -> None:
   plan = gate.build_plan(CANDIDATE)
   template = _record(plan, gate.ABORT, cleanup=_NOT_MEASURED)
   fields = {key: value for key, value in template.items() if key not in runner._BINDING_FIELDS}
   record = runner._measured_record(plan, gate.ABORT, fields)
   assert record["cleanup"] == _NOT_MEASURED
   assert gate.evaluate_case(record, plan) == ["terminal_resource_delta_not_measured"]


class _Session:
   returncode = None


class _Route:
   """The concrete-route surface the door reads, answering as one signed process."""

   def __init__(self, process_id: int) -> None:
      self._sessions = {"node-0": _Session()}
      self._peers = {"node-0": type("Peer", (), {"process_transport": "local"})()}
      self._plan = {"entry_node_id": "node-0"}
      self._process_id = process_id

   def _command_id(self, node_id: str, command: str) -> str:
      return f"{node_id}:{command}"

   def _send_before(self, node_id: str, **_kwargs: Any) -> dict[str, Any]:
      return {"node_id": node_id}

   def _verify_observation(self, node_id: str, _response: Any, *, event: str) -> dict[str, Any]:
      return {
         "event": event,
         "node_id": node_id,
         "host_id": "host-0",
         "process_id": self._process_id,
         "details": {"roles": ["entry", "standby"], "agent": {}, "entry_fence_record_count": 0},
      }

   def public_status(self) -> dict[str, Any]:
      return {"peers": [{"node_id": "node-0", "active_kv_state_count": 0, "active_kv_bytes": 0}]}


def test_evidence_door_names_the_verified_entry_process() -> None:
   before, after = RouteA7Evidence(_Route(4100)), RouteA7Evidence(_Route(4200))
   assert before.request("request-a")["entry_process"] == {"host_id": "host-0", "process_id": 4100}
   assert before.resources()["nodes"]["node-0"]["process"] == {"host_id": "host-0", "process_id": 4100}
   assert after.resources()["nodes"]["node-0"]["process"] == {"host_id": "host-0", "process_id": 4200}


class _Owner:
   """A cold-restart owner: the door resolves the current route on every read."""

   def __init__(self, route: _Route) -> None:
      self.current = route

   def _current_route_or_raise(self) -> _Route:
      return self.current


def _identified(process_id: int, exit_status: int | None) -> _Route:
   route = _Route(process_id)
   route._identities = {"node-0": {"host_id": "host-0", "process_id": process_id}}
   route._sessions["node-0"].returncode = exit_status
   return route


def test_evidence_door_keeps_the_replaced_entry_process_exit_status() -> None:
   old = _identified(4100, None)
   owner = _Owner(old)
   door = RouteA7Evidence(owner)
   assert door.resources()["retired_entry_processes"] == []
   # The supervisor closes the old route (its entry exits) and opens a replacement.
   old._sessions["node-0"] = type("Exited", (), {"returncode": -15})()
   owner.current = _identified(4200, None)
   resources = door.resources()
   assert resources["nodes"]["node-0"]["process"] == {"host_id": "host-0", "process_id": 4200}
   assert resources["retired_entry_processes"] == [
      {
         "node_id": "node-0",
         "host_id": "host-0",
         "process_id": 4100,
         "process_transport": "local",
         "exit_status": -15,
         "observed_by": "retired_route_session_exit_status",
      }
   ]


def test_a_retired_entry_without_an_exit_status_is_reported_unproven() -> None:
   old = _identified(4100, None)
   owner = _Owner(old)
   door = RouteA7Evidence(owner)
   door.resources()
   owner.current = _identified(4200, None)
   assert door.resources()["retired_entry_processes"][0]["exit_status"] is None


def test_a_remote_entry_exit_is_not_accepted_without_remote_confirmation() -> None:
   plan = gate.build_plan(CANDIDATE)
   for transport in ("ssh", "managed_https"):
      cleanup = _process_exit(old_entry_exit__process_transport=transport)
      assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=cleanup), plan) == ["process_exit_remote_unconfirmed"]


def test_a_missing_exit_transport_is_invalid_evidence() -> None:
   plan = gate.build_plan(CANDIDATE)
   for transport in (None, ""):
      cleanup = _process_exit(old_entry_exit__process_transport=transport)
      assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=cleanup), plan) == ["process_exit_evidence_invalid"]
   cleanup = _process_exit()
   del cleanup["node-0"]["cleanup_proof_evidence"]["old_entry_exit"]["process_transport"]
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=cleanup), plan) == ["process_exit_evidence_invalid"]


def test_evidence_door_records_the_retired_entry_transport() -> None:
   old = _identified(4100, -15)
   old._peers = {"node-0": type("Peer", (), {"process_transport": "ssh"})()}
   owner = _Owner(old)
   door = RouteA7Evidence(owner)
   door.resources()
   owner.current = _identified(4200, None)
   assert door.resources()["retired_entry_processes"][0]["process_transport"] == "ssh"
   bare = _identified(4300, -15)
   bare._peers = {"node-0": object()}
   owner.current = bare
   door.resources()
   owner.current = _identified(4400, None)
   assert door.resources()["retired_entry_processes"][-1]["process_transport"] is None
