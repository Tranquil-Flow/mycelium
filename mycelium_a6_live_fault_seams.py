# SPDX-License-Identifier: AGPL-3.0-only
"""Private in-process A6 measured source-failure seam, never physical evidence.

Only a route constructed with its explicit private test slot can be armed. No
HTTP endpoint, env switch, public route toggle, or synthetic incident writer.
"""
from __future__ import annotations

import math
import time
from typing import Callable

from mycelium_a6_fault_control import FaultRejected, INJECTION_POINTS
from mycelium_live.route import PhysicalLiveRoute, _TestOnlyTransportFault
from mycelium_request_gateway.contracts import AdmissionError

_CASE = "ordinary_browser_source_failure_recovery"
_FIELDS = (
    "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
    "request_attempt", "path_id", "path_attempt", "path_digest",
    "topology_generation", "command_id", "publisher_generation", "cancellation_generation",
)

class MeasuredSourceFailureSeam:
    seam_id = INJECTION_POINTS[_CASE].seam_id

    def __init__(self, *, service, router, route: PhysicalLiveRoute,
                 expected_request_id: str, clock_monotonic: Callable[[], float] = time.monotonic,
                 release_source: Callable[[], None] | None = None) -> None:
        self._service = service
        self._router = router
        self._route = route
        self._request_id = expected_request_id
        self._clock = clock_monotonic
        self._release_source = release_source
        self._armed_identity: dict | None = None
        self._used = False

    def _binding(self) -> dict:
        request_id = self._request_id
        if not isinstance(request_id, str) or not request_id:
            raise FaultRejected("measured_fault_identity_unavailable")
        try:
            if self._service.terminal_event_count(request_id) != 0:
                raise FaultRejected("measured_fault_gateway_terminal")
        except AdmissionError as error:
            raise FaultRejected("measured_fault_gateway_unavailable") from error
        binding = self._router._a6_test_only_source_fault_binding(request_id)
        now = self._clock()
        if (
            binding is None or binding["request_id"] != request_id
            or not binding["source_node_id"]
            or binding["path_digest"] != binding["path_manifest_digest"]
            or binding["source_attempt"] != 1
            or not isinstance(now, (float, int)) or not math.isfinite(now)
            or now * 1000 >= binding["absolute_deadline_ms"]
        ):
            raise FaultRejected("measured_fault_identity_unavailable")
        with self._route._lock:
            active = self._route._active_route_requests.get(request_id)
            qualification = self._route._deployment_qualification
            if (
                active is None or active.get("terminal") is not False
                or active.get("control_bound") is not True
                or active.get("cancellation_requested") is not False
                or active.get("entry_node_id") != binding["source_node_id"]
                or active.get("cancellation_generation") != 0
                or request_id in self._route._request_cleanup_receipts
                or qualification is None or getattr(qualification, "route_ready", False) is not True
                or any(active.get(field) != binding[field] for field in _FIELDS
                       if field != "request_id")
            ):
                raise FaultRejected("measured_fault_route_unavailable")
            from mycelium_request_gateway.contracts import qualification_digest
            if qualification_digest(qualification) != binding["qualification_digest"]:
                raise FaultRejected("measured_fault_qualification_revoked")
        return binding

    def arm(self, *, case_id: str) -> dict:
        if case_id != _CASE or self._used:
            raise FaultRejected("measured_fault_case_unavailable")
        binding = self._binding()
        if self._armed_identity is not None and self._armed_identity != binding:
            raise FaultRejected("measured_fault_binding_changed")
        self._armed_identity = binding
        return {**binding, "admitted_through_ordinary_gateway": True,
                "request_in_flight": True, "source_attempt": 1}

    def apply(self, *, case_id: str) -> dict:
        if case_id != _CASE or self._used or self._armed_identity is None:
            raise FaultRejected("measured_fault_not_armed")
        binding = self._binding()
        if binding != self._armed_identity:
            raise FaultRejected("measured_fault_binding_changed")
        self._used = True  # one attempt even if route rejects or cleanup fails
        deadline = min(int(self._clock() * 1000) + 2000,
                       binding["absolute_deadline_ms"])
        try:
            fault = _TestOnlyTransportFault(
                identity={field: binding[field] for field in _FIELDS},
                source_node_id=binding["source_node_id"], command="infer_start",
                deadline_monotonic_ms=deadline,
            )
        except ValueError as error:
            raise FaultRejected("measured_fault_deadline_invalid") from error
        baseline = {item["incident_id"] for item in self._route.a4_scoped_runtime_incidents()}
        if not self._route._install_test_only_transport_fault(fault):
            raise FaultRejected("measured_fault_install_refused")
        if self._release_source is not None:
            self._release_source()
        until = min(self._clock() + 2, binding["absolute_deadline_ms"] / 1000)
        while self._clock() < until:
            for incident in self._route.a4_scoped_runtime_incidents():
                if (incident["incident_id"] not in baseline
                    and all(incident.get(field) == binding[field] for field in _FIELDS)
                    and incident.get("subject_id") == binding["source_node_id"]
                    and incident.get("reason") == "transport_not_running"
                    and incident.get("scope") == "request"
                    and incident.get("fatal_accepted") is False
                    and self._route._test_only_transport_fault_consumed
                    and self._router._a6_test_only_source_failure_observed(binding)):
                    return {"source_failure_observed": True, "recovery_required": True,
                            "actual_incident": incident}
            time.sleep(.005)
        raise FaultRejected("measured_fault_actual_incident_missing")
