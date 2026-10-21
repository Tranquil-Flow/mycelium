# SPDX-License-Identifier: AGPL-3.0-only
"""The ``a7_source_lost`` node command: exact control, accept first, cut over after."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from physical_inference_node import NodeCommandError, PhysicalNodeService

_CONTROL = {
   "deployment_id": "deployment-a7",
   "deployment_epoch": 3,
   "qualification_digest": "sha256:" + "a" * 64,
   "command_id": "command-a7",
   "publisher_generation": 1,
   "absolute_deadline_ms": 10**12,
   "request_attempt": 1,
   "path_id": "path-a7",
   "path_attempt": 0,
   "path_digest": "sha256:" + "b" * 64,
   "topology_generation": 5,
   "cancellation_generation": 0,
}


class _Wiring:
   def __init__(self, decision: dict[str, Any]) -> None:
      self.decision = decision
      self.claims: list[tuple[str, str]] = []
      self.cutovers: list[tuple[Any, str, str]] = []
      self.cut = threading.Event()

   def accept_source_lost(self, request_id: str, lost_node_id: str) -> dict[str, Any]:
      self.claims.append((request_id, lost_node_id))
      return dict(self.decision)

   def cut_over_lost_source(self, router: Any, request_id: str, lost_node_id: str) -> bool:
      self.cutovers.append((router, request_id, lost_node_id))
      self.cut.set()
      return True


def _service(wiring: _Wiring | None) -> PhysicalNodeService:
   service = object.__new__(PhysicalNodeService)
   service.node_id = "node-0"
   service.state = "RUNNING"
   service.router = SimpleNamespace(name="router")
   service.graph = SimpleNamespace(deployment_id="deployment-a7", deployment_epoch=3, topology_version=5)
   service._a7_wiring = wiring
   service._control_lock = threading.RLock()
   service._request_controls = {"request-a7": dict(_CONTROL)}
   service._after_response = threading.local()
   service._signed_result = lambda event, details: {"event": event, "details": dict(details)}
   return service


def _payload(**overrides: Any) -> dict[str, Any]:
   return {"request_id": "request-a7", "control": dict(_CONTROL), "lost_node_id": "node-2", **overrides}


_ACCEPTED = {
   "accepted": True,
   "reason": "source_lost_accepted",
   "repeated": False,
   "source_placement_id": "placement-001",
   "cutover_pending": True,
}


def test_accepts_for_the_exact_bound_control_and_cuts_over_only_after_the_response() -> None:
   wiring = _Wiring(_ACCEPTED)
   service = _service(wiring)

   result = service._a7_source_lost(_payload())

   assert result["event"] == "a7_source_lost"
   assert result["details"] == {"request_id": "request-a7", "lost_node_id": "node-2", "control": dict(_CONTROL), **_ACCEPTED}
   assert wiring.claims == [("request-a7", "node-2")]
   # Accept first: nothing runs until the response has been emitted.
   time.sleep(0.05)
   assert wiring.cutovers == []
   service.run_after_response()
   assert wiring.cut.wait(2.0)
   assert wiring.cutovers == [(service.router, "request-a7", "node-2")]
   # The deferred work runs once.
   service.run_after_response()
   time.sleep(0.05)
   assert len(wiring.cutovers) == 1


def test_a_failed_response_discards_the_deferred_cutover() -> None:
   wiring = _Wiring(_ACCEPTED)
   service = _service(wiring)
   service._a7_source_lost(_payload())
   service.discard_after_response()
   service.run_after_response()
   time.sleep(0.05)
   assert wiring.cutovers == []


def test_a_repeated_claim_is_answered_without_new_cutover_work() -> None:
   wiring = _Wiring({**_ACCEPTED, "repeated": True, "cutover_pending": False})
   service = _service(wiring)
   result = service._a7_source_lost(_payload())
   assert result["details"]["accepted"] is True
   service.run_after_response()
   time.sleep(0.05)
   assert wiring.cutovers == []


@pytest.mark.parametrize(
   ("mutate", "reason"),
   (
      (lambda service, payload: payload.update(lost_node_id="node-0"), "lost_node_is_entry"),
      (lambda service, payload: payload["control"].update(publisher_generation=2), "stale_request_control"),
      (lambda service, payload: service._request_controls.clear(), "stale_request_control"),
   ),
)
def test_a_claim_without_the_exact_bound_control_is_refused(mutate, reason) -> None:
   wiring = _Wiring(_ACCEPTED)
   service = _service(wiring)
   payload = _payload()
   mutate(service, payload)

   result = service._a7_source_lost(payload)

   assert result["details"]["accepted"] is False
   assert result["details"]["reason"] == reason
   assert wiring.claims == []
   service.run_after_response()
   time.sleep(0.05)
   assert wiring.cutovers == []


def test_the_entry_refusal_is_reported_and_nothing_is_deferred() -> None:
   wiring = _Wiring({"accepted": False, "reason": "request_not_covered"})
   service = _service(wiring)
   result = service._a7_source_lost(_payload())
   assert result["details"] == {
      "request_id": "request-a7",
      "lost_node_id": "node-2",
      "control": dict(_CONTROL),
      "accepted": False,
      "reason": "request_not_covered",
   }
   service.run_after_response()
   time.sleep(0.05)
   assert wiring.cutovers == []


@pytest.mark.parametrize(
   ("payload", "service_change", "code"),
   (
      ({"request_id": "request-a7", "control": dict(_CONTROL)}, None, "invalid_a7_source_lost_fields"),
      (_payload(extra=True), None, "invalid_a7_source_lost_fields"),
      (_payload(request_id=""), None, "invalid_a7_source_lost_fields"),
      (_payload(control={**_CONTROL, "deployment_epoch": 4}), None, "invalid_a7_source_lost_control"),
      (_payload(), ("state", "STARTING"), "invalid_state_for_a7_source_lost"),
      (_payload(), ("_a7_wiring", None), "a7_not_configured"),
   ),
)
def test_malformed_or_unconfigured_claims_are_rejected(payload, service_change, code) -> None:
   service = _service(_Wiring(_ACCEPTED))
   if service_change is not None:
      setattr(service, *service_change)
   with pytest.raises(NodeCommandError) as raised:
      service._a7_source_lost(payload)
   assert raised.value.code == code


def test_the_command_runs_on_the_control_lane_not_behind_inference() -> None:
   from physical_inference_node import _command_uses_cleanup_response_priority, _command_uses_inference_lane

   document = {"command": "a7_source_lost", "payload": _payload()}
   assert _command_uses_inference_lane(document) is False
   assert _command_uses_cleanup_response_priority(document) is False
