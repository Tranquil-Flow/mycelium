# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D: a stalled prefill transport is a transport failure.

``prefill_transport_stalled`` is the entry node's verdict that the request's
link stopped making progress (after the transport's own bounded resends).  The
route must project it onto request-scoped transport liveness - like
``delivery_deadline_exceeded`` - and not record it as a worker exception.
"""

from __future__ import annotations

from mycelium_live.route import PhysicalLiveRoute


class _Liveness:
    def __init__(self) -> None:
        self.active_failures: list[dict] = []
        self.worker_exceptions: list[dict] = []

    def record_active_failure(self, subject, **kwargs) -> None:
        self.active_failures.append({"subject": subject, **kwargs})

    def record_worker_exception(self, subject, **kwargs) -> None:
        self.worker_exceptions.append({"subject": subject, **kwargs})


def _route() -> tuple[PhysicalLiveRoute, _Liveness, list[dict]]:
    route = object.__new__(PhysicalLiveRoute)
    liveness = _Liveness()
    incidents: list[dict] = []
    route._liveness = liveness  # type: ignore[attr-defined]
    route._liveness_subjects = {"node-0": "subject-node-0"}  # type: ignore[attr-defined]
    route._record_scoped_runtime_incident = (  # type: ignore[method-assign]
        lambda **kwargs: incidents.append(kwargs)
    )
    return route, liveness, incidents


def test_prefill_transport_stalled_is_recorded_as_a_request_scoped_transport_failure() -> None:
    route, liveness, incidents = _route()

    route._record_active_runtime_failure(
        request_id="request-relay",
        node_id="node-0",
        reason="prefill_transport_stalled",
        observed_at_monotonic_s=12.5,
    )

    assert liveness.worker_exceptions == []
    assert len(liveness.active_failures) == 1
    failure = liveness.active_failures[0]
    assert failure["subject"] == "subject-node-0"
    assert failure["scope"] == "request"
    assert failure["affected_track_ids"] == ("request-relay",)
    assert incidents == [
        {
            "request_id": "request-relay",
            "subject_id": "node-0",
            "scope": "request",
            "reason": "prefill_transport_stalled",
        }
    ]


def test_a_generic_prefill_timeout_stays_a_worker_exception() -> None:
    route, liveness, _incidents = _route()

    route._record_active_runtime_failure(
        request_id="request-relay",
        node_id="node-0",
        reason="prefill_completion_timeout",
        observed_at_monotonic_s=12.5,
    )

    assert liveness.active_failures == []
    assert len(liveness.worker_exceptions) == 1
