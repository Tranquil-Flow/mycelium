# SPDX-License-Identifier: AGPL-3.0-only
"""A7: a retired entry stays observable when no replacement route resolves.

A failed cold restart leaves the product without a current route.  The evidence
door must still name the retired entry process with its integer exit status
(the process-exit cleanup proof), while the gate keeps failing the missing
replacement.
"""

from __future__ import annotations

from typing import Any

import mycelium_a7_physical_measurement as measurement
from mycelium_a7_evidence_control import A7EvidenceUnavailable, RouteA7Evidence
from tests.a7_acceptance.test_cleanup_not_measured import (
   _OLD,
   _identified,
   _process_exit,
   gate,
   CANDIDATE,
   _record,
)


class _UnavailableOwner:
   def __init__(self, route: Any) -> None:
      self.current: Any = route

   def _current_route_or_raise(self) -> Any:
      if self.current is None:
         raise RuntimeError("route_not_open")
      return self.current


def _retired_without_replacement() -> tuple[RouteA7Evidence, dict[str, Any]]:
   old = _identified(4100, None)
   owner = _UnavailableOwner(old)
   door = RouteA7Evidence(owner)
   door.resources()
   # The supervisor retires the route: it exits, and no replacement opens.
   old._sessions["node-0"] = type("Exited", (), {"returncode": -15})()
   owner.current = None
   return door, owner


def test_retired_entry_is_reported_when_no_replacement_route_resolves() -> None:
   door, _owner = _retired_without_replacement()
   resources = door.resources()
   assert resources["nodes"] == {}
   assert resources["retired_entry_processes"] == [
      {
         "node_id": "node-0",
         **_OLD,
         "process_transport": "local",
         "exit_status": -15,
         "observed_by": "retired_route_session_exit_status",
      }
   ]


def test_retired_entry_is_reported_once_and_followed_by_a_later_replacement() -> None:
   door, owner = _retired_without_replacement()
   assert len(door.resources()["retired_entry_processes"]) == 1
   assert len(door.resources()["retired_entry_processes"]) == 1
   owner.current = _identified(4200, None)
   resources = door.resources()
   assert [item["process_id"] for item in resources["retired_entry_processes"]] == [4100]
   assert resources["nodes"]["node-0"]["process"]["process_id"] == 4200


def test_no_route_ever_resolved_still_refuses() -> None:
   door = RouteA7Evidence(_UnavailableOwner(None))
   try:
      door.resources()
   except A7EvidenceUnavailable as exc:
      assert exc.code == "a7_evidence_route_unavailable"
   else:  # pragma: no cover
      raise AssertionError("a door that never saw a route must refuse")


def test_request_reads_still_refuse_without_a_current_route() -> None:
   door, _owner = _retired_without_replacement()
   try:
      door.request("request-a")
   except A7EvidenceUnavailable as exc:
      assert exc.code == "a7_evidence_route_unavailable"
   else:  # pragma: no cover
      raise AssertionError("a request read needs a live route")


def test_measurement_proves_the_old_exit_and_the_gate_fails_only_the_missing_replacement() -> None:
   door, _owner = _retired_without_replacement()
   after = door.resources()
   before = {"nodes": {"node-0": {"process": dict(_OLD)}}}
   pre_kill = {"entry_process": dict(_OLD)}
   assert (
      measurement._entry_process_replaced("node-0", before, pre_kill, after, restored_seen=False)
      == measurement.ENTRY_PROCESS_REPLACED
   )
   evidence = measurement._process_exit_evidence("node-0", before, pre_kill, after, replacement_request_live=False)
   assert type(evidence["old_entry_exit"]["exit_status"]) is int
   assert evidence["replacement_entry_process"] is None
   plan = gate.build_plan(CANDIDATE)
   cleanup = _process_exit(**{"old_entry_exit": evidence["old_entry_exit"], "replacement_entry_process": None})
   assert gate.evaluate_case(_record(plan, gate.ABORT, cleanup=cleanup), plan) == ["process_exit_replacement_unobserved"]
