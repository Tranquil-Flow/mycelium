from __future__ import annotations

from dataclasses import replace
import threading
import time
from typing import Any, Mapping

import pytest

from mycelium_live.command_controller import (
    CleanupResult,
    CleanupStatus,
    CommandController,
    CommandEnvelope,
    CommandIdentity,
    CommandKind,
)
from mycelium_live.liveness import (
    LivenessSubject,
    ObservationSource,
    SubjectKind,
    TrafficAwareLivenessDetector,
)
from mycelium_live.route import PhysicalLiveRoute


def _identity() -> CommandIdentity:
    return CommandIdentity(
        deployment_id="deployment-owner-test",
        deployment_epoch=4,
        qualification_digest="sha256:" + "a" * 64,
        request_id="request-owner-test",
        request_attempt=2,
        path_id="path-owner-test",
        path_attempt=1,
        path_digest="sha256:" + "b" * 64,
        topology_generation=7,
        command_id="command-owner-test",
        publisher_generation=3,
        absolute_deadline_ms=10_000,
    )


def _cleanup() -> CleanupResult:
    return CleanupResult(
        status=CleanupStatus.COMPLETED,
        released_resource_count=2,
        result_digest="sha256:" + "c" * 64,
    )


def test_cross_owner_mutation_and_cleanup_fail_closed() -> None:
    controller = CommandController()
    identity = _identity()
    registered = controller.register(
        CommandEnvelope(
            identity=identity,
            stage_id="stage-owner-test",
            placement_id="placement-owner-test",
            assignment_id="assignment-owner-test",
            kind=CommandKind.PREFILL,
            issued_at_ms=1_000,
            idempotency_digest="sha256:" + "d" * 64,
            cleanup_owner_id="physical-live-route:deployment-owner-test",
            maximum_request_bytes=4_096,
            maximum_response_bytes=16_384,
        )
    )
    assert registered.accepted is True

    wrong_owner = controller.record_cleanup(
        identity,
        owner_id="unrelated-owner",
        result=_cleanup(),
        observed_at_ms=2_000,
    )
    stale_attempt = controller.record_cleanup(
        replace(identity, request_attempt=1),
        owner_id="physical-live-route:deployment-owner-test",
        result=_cleanup(),
        observed_at_ms=2_000,
    )
    stale_path = controller.record_cleanup(
        replace(identity, path_digest="sha256:" + "e" * 64),
        owner_id="physical-live-route:deployment-owner-test",
        result=_cleanup(),
        observed_at_ms=2_000,
    )

    assert wrong_owner.accepted is False
    assert wrong_owner.reason == "cleanup_owner_mismatch"
    assert stale_attempt.accepted is False
    assert stale_attempt.reason == "stale_attempt"
    assert stale_path.accepted is False
    assert stale_path.reason == "path_mismatch"

    snapshot = controller.snapshot(identity.request_id, request_attempt=2)
    assert len(snapshot) == 1
    assert snapshot[0].cleanup_result is None
    assert snapshot[0].cleanup_revision == 0

    accepted = controller.record_cleanup(
        identity,
        owner_id="physical-live-route:deployment-owner-test",
        result=_cleanup(),
        observed_at_ms=2_000,
    )
    assert accepted.accepted is True
    assert accepted.snapshot is not None
    assert accepted.snapshot.cleanup_revision == 1


def test_physical_release_is_exact_attempt_path_and_owner_scoped() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._request_inputs = {"request-a": (1, 2)}
    route._request_outputs = {"request-a": (3,)}
    route._request_limits = {"request-a": 1}
    route._request_entry_nodes = {"request-a": "node-a"}
    route._request_locks = {"request-a": threading.RLock()}
    route._active_route_requests = {
        "request-a": {
            "request_attempt": 2,
            "path_id": "path-a",
            "path_attempt": 3,
            "path_digest": "sha256:" + "a" * 64,
        }
    }
    route._request_cleanup_receipts = {
        "request-a": {
            "request_id": "request-a",
            "request_attempt": 2,
            "path_id": "path-a",
            "path_attempt": 3,
            "path_digest": "sha256:" + "a" * 64,
            "cleanup_owner_id": "physical-live-route:deployment-a",
        }
    }

    with pytest.raises(RuntimeError, match="cleanup_unproven"):
        route.release_request_scoped(
            "request-a",
            request_attempt=1,
            path_id="path-a",
            path_attempt=3,
            path_digest="sha256:" + "a" * 64,
            cleanup_owner_id="physical-live-route:deployment-a",
        )
    assert "request-a" in route._active_route_requests
    assert "request-a" in route._request_cleanup_receipts

    route.release_request_scoped(
        "request-a",
        request_attempt=2,
        path_id="path-a",
        path_attempt=3,
        path_digest="sha256:" + "a" * 64,
        cleanup_owner_id="physical-live-route:deployment-a",
    )
    assert "request-a" not in route._active_route_requests
    assert "request-a" not in route._request_cleanup_receipts


def test_physical_cancellation_advances_control_on_every_participating_node() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    interrupted: list[tuple[str, str, str]] = []

    class Session:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def interrupt_command(self, command_id: str, *, code: str) -> bool:
            interrupted.append((self.node_id, command_id, code))
            return True

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "infer_cancel"
            assert deadline_monotonic_s > time.monotonic()
            calls.append((self.node_id, dict(payload)))
            return {"details": {"event": "inference_cancelled"}}

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {
        node_id: Session(node_id) for node_id in ("node-a", "node-b")
    }
    route._active_route_requests = {
        "request-a": {
            "request_attempt": 2,
            "path_id": "path-a",
            "path_attempt": 3,
            "path_digest": "sha256:" + "a" * 64,
            "deployment_id": "deployment-a",
            "deployment_epoch": 4,
            "qualification_digest": "sha256:" + "b" * 64,
            "topology_generation": 7,
            "command_id": "command-a",
            "publisher_generation": 3,
            "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
            "entry_node_id": "node-a",
            "participating_node_ids": frozenset({"node-a", "node-b"}),
            "cancellation_generation": 0,
            "cancellation_requested": False,
            "cancellation_started_at": None,
            "cancellation_deadline": None,
            "terminal": False,
        }
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    deadline = time.monotonic() + 2.0
    route._active_route_requests["request-a"]["inflight_commands"] = {
        "node-a": "node-a-infer-start-7"
    }

    assert route.cancel_request("request-a", deadline_monotonic_s=deadline) is True
    assert interrupted == [
        ("node-a", "node-a-infer-start-7", "request_cancelled")
    ]
    assert [node_id for node_id, _payload in calls] == ["node-a", "node-b"]
    assert all(payload["cancellation_generation"] == 1 for _, payload in calls)
    assert all(payload["deadline_budget_ms"] <= 2_000 for _, payload in calls)
    assert route._active_route_requests["request-a"]["cancellation_generation"] == 1


def test_cleanup_snapshot_waits_for_every_cancellation_fanout_receipt() -> None:
    node_a_cancelled = threading.Event()
    node_b_cancel_entered = threading.Event()
    release_node_b = threading.Event()
    snapshot_started = threading.Event()

    class Session:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "infer_cancel"
            assert payload["cancellation_generation"] == 1
            assert deadline_monotonic_s > time.monotonic()
            if self.node_id == "node-a":
                node_a_cancelled.set()
            else:
                node_b_cancel_entered.set()
                assert release_node_b.wait(timeout=1.0)
            return {"details": {"event": "inference_cancelled"}}

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {
        node_id: Session(node_id) for node_id in ("node-a", "node-b")
    }
    route._active_route_requests = {
        "request-a": {
            "request_attempt": 2,
            "path_id": "path-a",
            "path_attempt": 3,
            "path_digest": "sha256:" + "a" * 64,
            "deployment_id": "deployment-a",
            "deployment_epoch": 4,
            "qualification_digest": "sha256:" + "b" * 64,
            "topology_generation": 7,
            "command_id": "command-a",
            "publisher_generation": 3,
            "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
            "entry_node_id": "node-a",
            "participating_node_ids": frozenset({"node-a", "node-b"}),
            "cancellation_generation": 0,
            "cancellation_requested": False,
            "cancellation_started_at": None,
            "cancellation_deadline": None,
            "terminal": False,
        }
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._snapshot_nodes_before = lambda *args, **kwargs: snapshot_started.set()
    route._cancellation_cleanup_complete = lambda *args, **kwargs: True
    deadline = time.monotonic() + 1.0

    cancel_thread = threading.Thread(
        target=route.cancel_request,
        args=("request-a",),
        kwargs={"deadline_monotonic_s": deadline},
    )
    cancel_thread.start()
    assert node_a_cancelled.wait(timeout=1.0)
    assert node_b_cancel_entered.wait(timeout=1.0)

    cleanup_thread = threading.Thread(
        target=route._wait_for_cancellation_cleanup,
        args=(frozenset({"node-a", "node-b"}),),
        kwargs={
            "deadline_monotonic_s": deadline,
            "cleanup_subject": {"request_id": "request-a"},
        },
    )
    cleanup_thread.start()
    assert not snapshot_started.wait(timeout=0.05)

    release_node_b.set()
    cancel_thread.join(timeout=1.0)
    cleanup_thread.join(timeout=1.0)
    assert not cancel_thread.is_alive()
    assert not cleanup_thread.is_alive()
    assert snapshot_started.is_set()


def test_failed_peer_cancellation_still_closes_the_fanout_barrier() -> None:
    class Session:
        def __init__(self, node_id: str, *, failed: bool = False) -> None:
            self.node_id = node_id
            self.failed = failed

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            del command_id, payload, deadline_monotonic_s
            assert command == "infer_cancel"
            if self.failed:
                raise RuntimeError("node_process_exited")
            return {"details": {"event": "inference_cancelled"}}

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {
        "node-a": Session("node-a"),
        "node-b": Session("node-b", failed=True),
    }
    route._active_route_requests = {
        "request-a": {
            "request_attempt": 2,
            "path_id": "path-a",
            "path_attempt": 3,
            "path_digest": "sha256:" + "a" * 64,
            "deployment_id": "deployment-a",
            "deployment_epoch": 4,
            "qualification_digest": "sha256:" + "b" * 64,
            "topology_generation": 7,
            "command_id": "command-a",
            "publisher_generation": 3,
            "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
            "entry_node_id": "node-a",
            "participating_node_ids": frozenset({"node-a", "node-b"}),
            "cancellation_generation": 0,
            "cancellation_requested": False,
            "cancellation_started_at": None,
            "cancellation_deadline": None,
            "terminal": False,
        }
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response

    with pytest.raises(RuntimeError, match="node_process_exited"):
        route.cancel_request(
            "request-a",
            deadline_monotonic_s=time.monotonic() + 1.0,
        )

    barrier = route._active_route_requests["request-a"][
        "cancellation_fanout_complete"
    ]
    assert isinstance(barrier, threading.Event)
    assert barrier.is_set()


def test_lost_peer_interrupts_only_requests_using_that_peer() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._open = True
    route._closed = False
    route._sessions = {
        "node-a": type("Session", (), {"returncode": None})(),
        "node-b": type("Session", (), {"returncode": 1})(),
    }
    route._active_route_requests = {
        "affected": {
            "participating_node_ids": frozenset({"node-a", "node-b"}),
            "terminal": False,
            "cancellation_requested": False,
        },
        "unaffected": {
            "participating_node_ids": frozenset({"node-a"}),
            "terminal": False,
            "cancellation_requested": False,
        },
    }
    route._liveness = TrafficAwareLivenessDetector()
    subject = LivenessSubject("node-b", SubjectKind.PEER, 9)
    route._liveness.register_subject(subject, observed_at_ms=1)
    route._liveness_subjects = {"node-b": subject}
    cancelled: list[tuple[str, float]] = []
    incidents: list[tuple[str, str, str | None]] = []
    route.cancel_request = lambda request_id, *, deadline_monotonic_s: (
        cancelled.append((request_id, deadline_monotonic_s)) or True
    )
    route._record_incident = lambda *, state, reason, request_id: incidents.append(
        (state, reason, request_id)
    )

    started = time.monotonic()
    route._handle_lost_peer_processes()

    assert [request_id for request_id, _deadline in cancelled] == ["affected"]
    assert 0.0 < cancelled[0][1] - started <= 2.01
    liveness_incident = route._liveness.incidents()[0]
    assert liveness_incident.affected_track_ids == ("affected",)
    assert liveness_incident.scope == "peer"
    assert incidents == [
        ("peer_process_lost", "route_peer_process_lost", None)
    ]


@pytest.mark.parametrize(
    "failure_details",
    (
        {"transport_fatal_error": {"code": "sidecar_closed"}},
        {
            "transport_fatal_error": None,
            "sidecar_process": {"started": True, "alive": False, "returncode": 9},
        },
    ),
)
def test_active_liveness_monitor_interrupts_request_on_transport_fatal(
    failure_details: dict[str, object],
) -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._open = True
    route._closed = False
    route._sessions = {"node-a": object()}
    route._last_snapshots = {}
    route._last_health = {}
    route._active_route_requests = {
        "request-affected": {
            "participating_node_ids": frozenset({"node-a"}),
            "cancellation_requested": False,
            "terminal": False,
        }
    }
    route._liveness = TrafficAwareLivenessDetector()
    subject = LivenessSubject("node-a", SubjectKind.PEER, 1)
    route._liveness.register_subject(subject, observed_at_ms=1)
    route._liveness_subjects = {"node-a": subject}
    route._record_incident = lambda **_kwargs: None

    def fail_scoped_runtime_incident(**_kwargs: object) -> None:
        raise RuntimeError("incident_projection_failed")

    route._record_scoped_runtime_incident = fail_scoped_runtime_incident
    cancelled: list[tuple[str, float]] = []

    def cancel_request(
        request_id: str,
        *,
        deadline_monotonic_s: float | None = None,
    ) -> bool:
        assert deadline_monotonic_s is not None
        cancelled.append((request_id, deadline_monotonic_s))
        return True

    route.cancel_request = cancel_request

    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"
    route._send_before = lambda node_id, **_kwargs: {
        "node_id": node_id,
        "details": failure_details,
    }

    def verify_observation(
        node_id: str,
        response: Mapping[str, Any],
        *,
        event: str,
        require_known_key: bool = True,
    ) -> dict[str, Any]:
        del node_id, event, require_known_key
        route._liveness.observe_receipt(
            subject,
            observed_at_ms=int(time.monotonic() * 1_000),
            source=ObservationSource.APPLICATION_RECEIPT,
            signed=True,
        )
        return dict(response)

    route._verify_observation = verify_observation
    started = time.monotonic()

    route._monitor_active_liveness_once()

    assert [request_id for request_id, _deadline in cancelled] == [
        "request-affected"
    ]
    assert 0.0 < cancelled[0][1] - started <= 2.01
    liveness_incident = route._liveness.incidents()[0]
    assert liveness_incident.affected_track_ids == ("request-affected",)
    assert liveness_incident.scope == "request"
