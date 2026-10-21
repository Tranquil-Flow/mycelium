# SPDX-License-Identifier: AGPL-3.0-only
"""The product dispatcher admits one A10 verification operation exclusively.

``LiveRouterPort.a10_target_verify`` validates the operation, admits it only on
an open, alive, idle route (no request can be admitted while it runs), sends it
to a node of the current deployment through the route, and accepts only the
record for exactly that operation.  ``ColdRouteRestartOwner`` forwards it to the
ready route only.  Scripted routes; never physical evidence.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_live import supervisor as supervisor_module
from mycelium_live.route import FakeLiveRoute
from mycelium_live.router_port import LiveRouterPort
from mycelium_router.a10_product_verifier import operation_payload
from mycelium_router.a10_qualification import VERIFIER_OPERATION_PROTOCOL
from mycelium_router.a10_verifier_matrix import MatrixPlan

BINDING = {
   "runtime_digest": "sha256:" + "1" * 64,
   "model_digest": "sha256:" + "2" * 64,
   "representation_digest": "sha256:" + "3" * 64,
   "backend": "numpy",
}
PLAN = MatrixPlan(widths={"one": 1, "qualified_default": 3, "qualified_maximum": 6},
                  prefix_buckets={"short": (2, 3), "long": (7, 8)},
                  operations=6, vocab_size=32)
GRAPH = SimpleNamespace(
   deployment_id="deployment-a10-verify",
   stages=(SimpleNamespace(placements=(SimpleNamespace(node_id="node-0"),)),
           SimpleNamespace(placements=(SimpleNamespace(node_id="node-2"),))),
)


def _negative_envelope(case_id: str) -> dict[str, Any]:
   return {"protocol": VERIFIER_OPERATION_PROTOCOL, "execution_path": "product_dispatcher",
           "provenance": "live", "negative": {"case_id": case_id, "refusal_code": "stale_prefix",
                                              "prefix_unchanged": True, "open_transactions": 0}}


class VerifyingRoute(FakeLiveRoute):
   def __init__(self, result: Any) -> None:
      super().__init__(scripted_tokens=(1,))
      self.result = result
      self.calls: list[tuple[str, dict, float]] = []
      self.entered = threading.Event()
      self.release = threading.Event()
      self.release.set()

   def a10_target_verify(self, node_id, *, operation, deadline_monotonic_s):
      self.calls.append((node_id, dict(operation), deadline_monotonic_s))
      self.entered.set()
      assert self.release.wait(timeout=5.0)
      return self.result


def _port(route) -> LiveRouterPort:
   route.open()
   return LiveRouterPort(route=route, execution_graph=GRAPH)


def test_dispatches_one_operation_to_a_deployment_node_and_unwraps_it():
   payload = operation_payload(PLAN, case_id="stale_prefix", **BINDING)
   route = VerifyingRoute(_negative_envelope("stale_prefix"))
   port = _port(route)
   before = time.monotonic()
   kind, negative = port.a10_target_verify("node-0", payload, timeout_s=30.0)
   assert kind == "negative" and negative["case_id"] == "stale_prefix"
   ((node_id, operation, deadline),) = route.calls
   assert node_id == "node-0" and operation == payload
   assert before + 29.0 <= deadline <= time.monotonic() + 30.0


def test_refuses_before_contact_when_the_operation_or_target_is_invalid():
   route = VerifyingRoute(_negative_envelope("stale_prefix"))
   port = _port(route)
   payload = operation_payload(PLAN, case_id="stale_prefix", **BINDING)
   with pytest.raises(RuntimeError, match="a10_verify_node_not_in_deployment"):
      port.a10_target_verify("node-9", payload, timeout_s=30.0)
   with pytest.raises(Exception, match="a10_product_payload_invalid"):
      port.a10_target_verify("node-0", {**payload, "extra": 1}, timeout_s=30.0)
   for timeout in (0.0, -1.0, 121.0, float("nan")):
      with pytest.raises(ValueError, match="a10_verify_deadline_invalid"):
         port.a10_target_verify("node-0", payload, timeout_s=timeout)
   assert route.calls == []


def test_refuses_a_route_that_cannot_verify_or_is_not_open():
   payload = operation_payload(PLAN, case_id="stale_prefix", **BINDING)
   plain = FakeLiveRoute(scripted_tokens=(1,))
   with pytest.raises(RuntimeError, match="a10_verify_route_unsupported"):
      _port(plain).a10_target_verify("node-0", payload, timeout_s=30.0)
   route = VerifyingRoute(_negative_envelope("stale_prefix"))
   port = _port(route)
   route.close()
   with pytest.raises(RuntimeError, match="route_not_open"):
      port.a10_target_verify("node-0", payload, timeout_s=30.0)
   assert route.calls == []


def test_a_foreign_record_is_refused():
   payload = operation_payload(PLAN, case_id="stale_prefix", **BINDING)
   route = VerifyingRoute(_negative_envelope("position_bound"))
   with pytest.raises(Exception, match="a10_product_result_invalid"):
      _port(route).a10_target_verify("node-0", payload, timeout_s=30.0)


def test_a_busy_route_is_refused_and_no_request_is_admitted_while_verifying(request_factory,
                                                                              recording_sink):
   payload = operation_payload(PLAN, case_id="stale_prefix", **BINDING)
   route = VerifyingRoute(_negative_envelope("stale_prefix"))
   port = _port(route)
   request_id = port.admit(request_factory("request-busy"), recording_sink)
   with pytest.raises(RuntimeError, match="a10_verify_route_busy"):
      port.a10_target_verify("node-0", payload, timeout_s=30.0)
   while port.decode_one(request_id):
      pass
   port.release_request(request_id)
   assert port.is_idle()

   route.release.clear()
   worker = threading.Thread(
      target=lambda: port.a10_target_verify("node-0", payload, timeout_s=30.0))
   worker.start()
   assert route.entered.wait(timeout=5.0)
   admitted = threading.Event()
   admission = threading.Thread(
      target=lambda: (port.admit(request_factory("request-during"), recording_sink),
                      admitted.set()))
   admission.start()
   time.sleep(0.2)
   assert not admitted.is_set()  # admission waits for the verification to finish
   route.release.set()
   worker.join(timeout=5.0)
   admission.join(timeout=5.0)
   assert admitted.is_set()


def test_the_restart_owner_forwards_only_to_the_ready_route():
   payload = operation_payload(PLAN, case_id="stale_prefix", **BINDING)
   route = VerifyingRoute(_negative_envelope("stale_prefix"))
   route.open()
   owner = object.__new__(supervisor_module.ColdRouteRestartOwner)
   owner._lock = threading.RLock()
   owner._current = route
   owner._state = "ready"
   assert owner.a10_target_verify("node-0", operation=payload,
                                  deadline_monotonic_s=time.monotonic() + 5) \
      == _negative_envelope("stale_prefix")
   for state in ("restarting", "failed", "closed"):
      owner._state = state
      with pytest.raises(RuntimeError, match="route_not_open"):
         owner.a10_target_verify("node-0", operation=payload,
                                 deadline_monotonic_s=time.monotonic() + 5)
   owner._state = "ready"
   owner._current = FakeLiveRoute(scripted_tokens=(1,))
   with pytest.raises(RuntimeError, match="a10_verify_route_unsupported"):
      owner.a10_target_verify("node-0", operation=payload,
                              deadline_monotonic_s=time.monotonic() + 5)
