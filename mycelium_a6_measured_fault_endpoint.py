# SPDX-License-Identifier: AGPL-3.0-only
"""Bridge the landed A6 fault control to a *measured* incident on a real request.

The direct-loopback receiver (``mycelium_a6_http_fault_control``) already exists
and is unmounted from the product.  What it never had was a measuring seam: the
fixture compositions bound it to a stub whose ``arm``/``apply`` echoed the
injection point's own expected state, so nothing was ever measured.

This module is that join, and only that join:

* it may only be constructed as an explicit ``test_only`` composition,
* it starts one ephemeral ``127.0.0.1`` direct-socket listener per construction
  with one use per case, so two engines can never share a receiver lifetime,
* the receipt it eventually emits is derived from what the seam *measured* --
  the real A4 scoped runtime incident, carrying the admitted request's own
  identity -- and is cross-checked against the route's independent incident
  record,
* every refusal path yields ``MeasuredFaultRefused`` and no receipt at all: a
  refused, disabled, unclosed, foreign or unmeasured case is never reported as
  a measured fault.

A valid receipt is controlled local composition evidence.  It is not physical
or browser qualification and never claims one.
"""
from __future__ import annotations

import json
import socket
import time
from typing import Any, Callable, Mapping

from mycelium_a6_direct_socket import A6DirectSocketServer
from mycelium_a6_fault_control import EXACT_FLEET_GRANT, FaultSeam
from mycelium_a6_http_fault_control import A6LocalFaultReceiver

RECEIPT_PROTOCOL = "mycelium.a6_measured_fault_receipt.v1"
DEFAULT_PATH = "/a6/controlled-fault"
IDENTITY_FIELDS = (
    "deployment_id", "deployment_epoch", "qualification_digest", "request_id",
    "request_attempt", "path_id", "path_attempt", "path_digest",
    "topology_generation", "command_id", "publisher_generation", "cancellation_generation",
)
_INCIDENT_FIELDS = ("incident_id", "subject_id", "reason", "scope", "fatal_accepted")
_MAX_BODY = 512


class MeasuredFaultRefused(Exception):
    """A bounded, non-disclosing refusal with no measured receipt."""


class _RecordingSeam:
    """Wrap a real seam and keep exactly what it measured, never what it should."""

    def __init__(self, seam: FaultSeam) -> None:
        self._seam = seam
        self.seam_id = seam.seam_id
        self.armed_states: list[tuple[str, dict[str, Any]]] = []
        self.post_states: list[tuple[str, dict[str, Any]]] = []

    def arm(self, *, case_id: str) -> Mapping[str, Any]:
        state = dict(self._seam.arm(case_id=case_id))
        self.armed_states.append((case_id, state))
        return state

    def apply(self, *, case_id: str) -> Mapping[str, Any]:
        state = dict(self._seam.apply(case_id=case_id))
        self.post_states.append((case_id, state))
        return state

    def measured_incidents(self) -> list[dict[str, Any]]:
        measured = []
        for case_id, state in self.post_states:
            incident = state.get("actual_incident")
            if not isinstance(incident, Mapping):
                continue
            measured.append({"case_id": case_id, **{field: incident[field]
                                                    for field in (*_INCIDENT_FIELDS, *IDENTITY_FIELDS)
                                                    if field in incident}})
        return measured


class MeasuredFaultEndpoint:
    """One direct-loopback fault endpoint bound to one measuring seam set."""

    def __init__(self, *, seams: Mapping[str, FaultSeam], case_ids: tuple[str, ...],
                 expected_identity: Mapping[str, Any], test_only: bool = False,
                 clock_ms: Callable[[], int] | None = None, path: str = DEFAULT_PATH,
                 incident_source: Callable[[], Any] | None = None,
                 gateway_terminal_count: Callable[[], int] | None = None,
                 timeout_seconds: float = 3.0) -> None:
        if test_only is not True:
            raise ValueError("test_only_composition_required")
        if (not case_ids or len(set(case_ids)) != len(case_ids)
                or any(not isinstance(case, str) or not case for case in case_ids)):
            raise ValueError("measured_endpoint_case_set_invalid")
        if (not isinstance(expected_identity, Mapping)
                or any(not isinstance(expected_identity.get(field), (str, int))
                       for field in IDENTITY_FIELDS)):
            raise ValueError("measured_endpoint_expected_identity_invalid")
        if not 0 < timeout_seconds <= 5:
            raise ValueError("measured_endpoint_timeout_invalid")
        self._recorders = {seam_id: _RecordingSeam(seam) for seam_id, seam in seams.items()}
        self._case_ids = tuple(case_ids)
        self._expected = {field: expected_identity[field] for field in IDENTITY_FIELDS}
        self._incident_source = incident_source
        self._gateway_terminal_count = gateway_terminal_count
        self._timeout = timeout_seconds
        self._receiver = A6LocalFaultReceiver(
            seams={item.seam_id: item for item in self._recorders.values()},
            clock_ms=clock_ms if clock_ms is not None else lambda: 1000,
            path=path, test_only=True)
        self._server = A6DirectSocketServer(receiver=self._receiver, bind_host="127.0.0.1",
                                            port=0, test_only=True)
        self._path = path
        self._protocol = "HTTP/1.1"
        self._started = False
        self._torn_down = False
        self._statuses: list[tuple[str, int]] = []
        self._gateway_counts: dict[str, int] = {}

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self) -> MeasuredFaultEndpoint:
        if self._started or self._torn_down:
            raise MeasuredFaultRefused("measured_endpoint_lifecycle_invalid")
        self._server.__enter__()
        self._started = True
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._started and not self._torn_down:
            self._server.__exit__(None, None, None)
            self._torn_down = True

    @property
    def lifetime_id(self) -> str:
        if not self._started:
            raise MeasuredFaultRefused("measured_endpoint_not_started")
        return self._server.lifetime_id

    @property
    def host(self) -> str:
        return self._server.host

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def statuses(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._statuses)

    # -- transport ----------------------------------------------------------

    def post(self, *, case_id: str, grant: str = EXACT_FLEET_GRANT) -> tuple[int, dict[str, Any]]:
        """Send one bounded direct-loopback control request.  Never invents a result."""
        if not self._started or self._torn_down:
            raise MeasuredFaultRefused("measured_endpoint_not_closed")
        if case_id not in self._case_ids:
            raise MeasuredFaultRefused("measured_endpoint_case_unassigned")
        body = json.dumps({"action": "execute_controlled_source_failure",
                           "case_id": case_id}).encode("ascii")
        if len(body) > _MAX_BODY:
            raise MeasuredFaultRefused("measured_endpoint_body_invalid")
        head = (
            f"POST {self._path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"X-Mycelium-A6-Fleet-Grant: {grant}\r\n\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", self.port), timeout=self._timeout) as sock:
            sock.settimeout(self._timeout)
            sock.sendall(head + body)
            reply = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                reply.extend(chunk)
        raw = bytes(reply)
        status = int(raw.split(b"\r\n", 1)[0].split(b" ")[1])
        payload = json.loads(raw.split(b"\r\n\r\n", 1)[1].decode("ascii"))
        self._statuses.append((case_id, status))
        if status == 200 and self._gateway_terminal_count is not None:
            # Frozen at the instant the fault was measured: the gateway has not
            # published any terminal for the admitted request yet.
            self._gateway_counts[case_id] = int(self._gateway_terminal_count())
        return status, payload

    # -- receipt ------------------------------------------------------------

    def _observed_incident_ids(self) -> set[str]:
        if self._incident_source is None:
            return set()
        return {item["incident_id"] for item in self._incident_source()
                if isinstance(item, Mapping) and "incident_id" in item}

    def receipt(self) -> dict[str, Any]:
        """Return the measured receipt or refuse; a partial measurement is never a receipt."""
        if not self._started or not self._torn_down:
            raise MeasuredFaultRefused("measured_endpoint_not_closed")
        if {case for case, _status in self._statuses} != set(self._case_ids):
            raise MeasuredFaultRefused("measured_endpoint_case_set_mismatch")
        first: dict[str, int] = {}
        for case, status in self._statuses:
            if case in first:
                # A repeated case must be refused by the one-use receiver; a
                # second success (or any other status) is not one-use evidence.
                if status != 409:
                    raise MeasuredFaultRefused("measured_endpoint_case_not_one_use")
            else:
                first[case] = status
        if any(status != 200 for status in first.values()) or len(first) != len(self._case_ids):
            raise MeasuredFaultRefused("measured_endpoint_case_refused")
        if tuple(self._receiver.executed_case_ids) != self._case_ids:
            raise MeasuredFaultRefused("measured_endpoint_execution_mismatch")
        measured = self._recorders_incidents()
        for case_id in self._case_ids:
            matches = [incident for incident in measured if incident["case_id"] == case_id]
            if len(matches) != 1:
                raise MeasuredFaultRefused("measured_endpoint_incident_unmeasured")
            incident = matches[0]
            if any(incident.get(field) != self._expected[field] for field in IDENTITY_FIELDS):
                raise MeasuredFaultRefused("measured_endpoint_identity_mismatch")
            if (incident.get("reason") != "transport_not_running"
                    or incident.get("scope") != "request"
                    or incident.get("fatal_accepted") is not False):
                raise MeasuredFaultRefused("measured_endpoint_incident_unmeasured")
            if self._incident_source is not None and incident.get("incident_id") not in self._observed_incident_ids():
                raise MeasuredFaultRefused("measured_endpoint_incident_unobserved")
            if self._gateway_terminal_count is not None:
                if case_id not in self._gateway_counts:
                    raise MeasuredFaultRefused("measured_endpoint_gateway_count_unmeasured")
                incident["gateway_terminal_count"] = self._gateway_counts[case_id]
        return {
            "protocol": RECEIPT_PROTOCOL,
            "lifetime_id": self._server.lifetime_id,
            "case_ids": self._case_ids,
            "executed_case_ids": tuple(self._receiver.executed_case_ids),
            "statuses": tuple(self._statuses),
            "measured_incidents": measured,
            "request_identity_bound": True,
            "torn_down": True,
            "live_evidence": False,
            "fixture_or_unit_evidence": True,
            "physical_qualification": False,
            "qualification_claim": False,
            "claim_boundary": (
                "In-process controlled local composition over one ephemeral direct loopback "
                "socket: the measured incident is a real A4 scoped runtime incident on one "
                "admitted request. Never a physical, browser or three-engine qualification."
            ),
        }

    def _recorders_incidents(self) -> list[dict[str, Any]]:
        measured: list[dict[str, Any]] = []
        for recorder in self._recorders.values():
            measured.extend(recorder.measured_incidents())
        return measured


__all__ = [
    "IDENTITY_FIELDS",
    "MeasuredFaultEndpoint",
    "MeasuredFaultRefused",
    "RECEIPT_PROTOCOL",
]
