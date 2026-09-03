from __future__ import annotations

from dataclasses import replace
import threading
import time
from typing import Any, Mapping

import pytest

import mycelium_live.route as route_module
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


def test_peer_counters_accept_signed_lean_cleanup_transport_counters() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._fatal = None
    route._peers = {"node-a": object()}
    route._last_snapshots = {
        "node-a": {
            "details": {
                "transport_counters": {
                    "remote_frames_sent": 17,
                    "remote_frames_received": 19,
                },
                "runtime": {"applied_operation_count": 23},
            }
        }
    }

    assert route._peer_counters() == {
        "node-a": {
            "frames_sent": 17,
            "frames_received": 19,
            "applied_operation_count": 23,
        }
    }
    counters = route.counters()
    assert counters.frames_sent == 17
    assert counters.frames_received == 19
    assert counters.applied_operation_count == 23


def test_peer_counters_retain_active_health_high_water_after_cleanup() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._fatal = None
    route._peers = {"node-a": object()}
    route._last_snapshots = {
        "node-a": {
            "details": {
                "transport_counters": {
                    "remote_frames_sent": 0,
                    "remote_frames_received": 0,
                },
                "runtime": {"applied_operation_count": 0},
            }
        }
    }
    route._last_health = {
        "node-a": {
            "details": {
                "transport_counters": {
                    "remote_frames_sent": 17,
                    "remote_frames_received": 19,
                },
                "runtime_counters": {"applied_operation_count": 23},
            }
        }
    }

    assert route._peer_counters() == {
        "node-a": {
            "frames_sent": 17,
            "frames_received": 19,
            "applied_operation_count": 23,
        }
    }


def test_live_attestation_refreshes_full_snapshot_after_lean_cleanup() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._request_outputs = {"request-a": (31,)}
    route.is_alive = lambda: True
    route._sessions = {"node-a": object()}
    route._last_snapshots = {
        "node-a": {
            "details": {
                "transport": {"remote_frames_sent": 0},
                "transport_counters": {
                    "remote_frames_sent": 1,
                    "remote_frames_received": 1,
                },
            }
        }
    }
    route._signed_observations = []
    refreshed: list[bool] = []

    def refresh() -> None:
        refreshed.append(True)
        route._last_snapshots["node-a"] = {
            "details": {"transport": {"remote_frames_sent": 1}}
        }

    route._snapshot_all = refresh

    with pytest.raises(
        RuntimeError, match="live_attestation_observation_window_incomplete"
    ):
        route.live_attestation(request_id="request-a")
    assert refreshed == [True]


def test_physical_cancellation_advances_control_on_every_participating_node() -> None:
    calls: list[tuple[str, dict[str, object]]] = []
    interrupted: list[tuple[str, str, str]] = []
    published_receipts: list[dict[str, object]] = []

    class Session:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def interrupt_command(self, command_id: str, *, code: str) -> bool:
            interrupted.append((self.node_id, command_id, code))
            return True

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "infer_cancel_wait"
            assert deadline_monotonic_s > time.monotonic()
            calls.append((self.node_id, dict(payload)))
            return {
                "details": {
                    "request_cleanup": {
                        **{
                            key: value
                            for key, value in payload.items()
                            if key != "deadline_budget_ms"
                        },
                        "runtime_clean": True,
                        "transport_clean": True,
                        "complete": True,
                    }
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    full_snapshots = {
        node_id: {
            "details": {
                "runtime": {
                    "mode": "stage_local_kv",
                    "backend": "numpy",
                }
            }
        }
        for node_id in ("node-a", "node-b")
    }
    route._last_snapshots = dict(full_snapshots)
    route._sessions = {node_id: Session(node_id) for node_id in ("node-a", "node-b")}
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
            "publish_cleanup_receipt": lambda receipt: published_receipts.append(
                dict(receipt)
            ),
        }
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    deadline = time.monotonic() + 2.0
    route._active_route_requests["request-a"]["inflight_commands"] = {
        "node-a": "node-a-infer-start-7"
    }

    assert route.cancel_request("request-a", deadline_monotonic_s=deadline) is True
    fanout = route._active_route_requests["request-a"]["cancellation_fanout_complete"]
    assert isinstance(fanout, threading.Event)
    assert fanout.wait(timeout=1.0)
    assert interrupted == [("node-a", "node-a-infer-start-7", "request_cancelled")]
    assert [node_id for node_id, _payload in calls] == ["node-a", "node-b"]
    assert all(payload["cancellation_generation"] == 1 for _, payload in calls)
    assert all(payload["deadline_budget_ms"] <= 2_000 for _, payload in calls)
    assert len(published_receipts) == 1
    assert published_receipts[0]["node_ids"] == ["node-a", "node-b"]
    assert route._last_snapshots == full_snapshots
    assert route._active_route_requests["request-a"]["cancellation_generation"] == 1
    # Publisher ownership is gateway state and may advance while terminal
    # delivery is in flight. Cleanup remains bound to the exact physical
    # control generation captured by the cancellation fanout.
    route._active_route_requests["request-a"]["publisher_generation"] = 4
    cleanup_subject = route._request_cleanup_subject("request-a")
    assert cleanup_subject["publisher_generation"] == 3
    route._snapshot_nodes_before = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("inline cleanup receipt issued a second control round trip")
    )
    route._wait_for_cancellation_cleanup(
        frozenset({"node-a", "node-b"}),
        deadline_monotonic_s=deadline,
        cleanup_subject=cleanup_subject,
        receipt_only=True,
    )


def test_physical_cancellation_reconciles_late_exact_cleanup_proof() -> None:
    """Missing the owner deadline must not orphan a later exact receipt."""

    proof_ready = threading.Event()
    published = threading.Event()
    seal_calls: list[str] = []

    class Session:
        returncode = None

        def interrupt_command(self, _command_id: str, *, code: str) -> bool:
            assert code == "request_cancelled"
            return True

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "infer_cancel_wait"
            assert payload["request_id"] == "request-late-cleanup"
            assert deadline_monotonic_s > time.monotonic()
            # Physical work is cancelled, but the initial owner-budget response
            # does not yet include the exact signed cleanup proof.
            return {"details": {"event": "inference_cancelled"}}

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": Session(), "node-b": Session()}
    route._active_route_requests = {
        "request-late-cleanup": {
            "request_attempt": 1,
            "path_id": "path-late-cleanup",
            "path_attempt": 0,
            "path_digest": "sha256:" + "a" * 64,
            "deployment_id": "deployment-a",
            "deployment_epoch": 4,
            "qualification_digest": "sha256:" + "b" * 64,
            "topology_generation": 7,
            "command_id": "command-late-cleanup",
            "publisher_generation": 3,
            "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
            "entry_node_id": "node-a",
            "participating_node_ids": frozenset({"node-a", "node-b"}),
            "cancellation_generation": 0,
            "cancellation_requested": False,
            "cancellation_started_at": None,
            "cancellation_deadline": None,
            "terminal": False,
            "publish_cleanup_receipt": lambda _receipt: published.set(),
        }
    }
    route._command_id = lambda node_id, operation: f"{node_id}:{operation}"

    def verify_observation(
        node_id: str,
        response: Mapping[str, Any],
        *,
        event: str,
        require_known_key: bool = True,
    ) -> dict[str, Any]:
        return dict(response)

    route._verify_observation = verify_observation

    def seal(*, request_id, cleanup_subject, participating_node_ids):
        assert request_id == "request-late-cleanup"
        assert cleanup_subject["cancellation_generation"] == 1
        assert participating_node_ids == frozenset({"node-a", "node-b"})
        seal_calls.append(request_id)
        if not proof_ready.is_set():
            return None
        published.set()
        return {"request_id": request_id}

    def reconcile_wait(
        participating_node_ids,
        *,
        deadline_monotonic_s,
        cleanup_subject,
        receipt_only=False,
    ):
        assert participating_node_ids == frozenset({"node-a", "node-b"})
        assert cleanup_subject["request_id"] == "request-late-cleanup"
        assert receipt_only is True
        assert deadline_monotonic_s > time.monotonic()
        proof_ready.set()

    route._seal_and_publish_request_cleanup_receipt = seal
    route._wait_for_cancellation_cleanup = reconcile_wait

    assert (
        route.cancel_request(
            "request-late-cleanup",
            deadline_monotonic_s=time.monotonic() + 0.2,
        )
        is True
    )
    fanout = route._active_route_requests["request-late-cleanup"][
        "cancellation_fanout_complete"
    ]
    assert fanout.wait(timeout=1.0)
    assert published.wait(timeout=1.0)
    assert len(seal_calls) >= 2

    stop = getattr(route, "_cleanup_reconciliation_stop", None)
    if isinstance(stop, threading.Event):
        stop.set()
    executor = getattr(route, "_cleanup_reconciliation_executor", None)
    if executor is not None:
        executor.shutdown(wait=True, cancel_futures=True)


def test_cleanup_reconciliation_survives_513_late_response_lifecycles() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._closed = False
    route._cleanup_reconciliation_stop = threading.Event()
    route._active_route_requests = {}
    completed = {f"late-{index}": threading.Event() for index in range(513)}

    def reconcile_wait(
        participating_node_ids,
        *,
        deadline_monotonic_s,
        cleanup_subject,
        receipt_only=False,
    ):
        assert participating_node_ids == frozenset({"node-a"})
        assert deadline_monotonic_s > time.monotonic()
        assert receipt_only is True

    def seal(*, request_id, cleanup_subject, participating_node_ids):
        assert cleanup_subject["request_id"] == request_id
        assert participating_node_ids == frozenset({"node-a"})
        with route._lock:
            route._active_route_requests[request_id]["cleanup_receipt_published"] = True
        completed[request_id].set()
        return {"request_id": request_id}

    route._wait_for_cancellation_cleanup = reconcile_wait
    route._seal_and_publish_request_cleanup_receipt = seal
    absolute_deadline_ms = int(time.monotonic() * 1_000) + 30_000
    for request_id in completed:
        route._active_route_requests[request_id] = {
            "terminal": False,
            "cleanup_receipt_published": False,
        }
        route._schedule_cleanup_reconciliation(
            request_id=request_id,
            cleanup_subject={
                "request_id": request_id,
                "absolute_deadline_ms": absolute_deadline_ms,
            },
            participating_node_ids=frozenset({"node-a"}),
        )

    deadline = time.monotonic() + 5.0
    for event in completed.values():
        assert event.wait(timeout=max(0.0, deadline - time.monotonic()))
    assert route._cleanup_reconciliation_executor._max_workers == 4

    route._cleanup_reconciliation_stop.set()
    route._cleanup_reconciliation_executor.shutdown(
        wait=True,
        cancel_futures=True,
    )


def test_cleanup_reconciliation_services_waiting_sibling_within_owner_horizon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._closed = False
    route._cleanup_reconciliation_stop = threading.Event()
    route._cleanup_reconciliation_executor = route_module.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="cleanup-queue-deadline-test",
    )
    route._cleanup_reconciliation_futures = {}
    route._active_route_requests = {
        request_id: {
            "terminal": False,
            "cleanup_receipt_published": False,
        }
        for request_id in ("queue-owner-a", "queue-owner-b")
    }
    owner_a_entered = threading.Event()
    published = {
        request_id: threading.Event() for request_id in route._active_route_requests
    }
    incidents: list[str] = []

    def reconcile_wait(
        participating_node_ids,
        *,
        deadline_monotonic_s,
        cleanup_subject,
        receipt_only=False,
    ):
        assert participating_node_ids == frozenset({"node-a"})
        assert deadline_monotonic_s > time.monotonic()
        assert receipt_only is True
        if cleanup_subject["request_id"] == "queue-owner-a":
            owner_a_entered.set()
            time.sleep(0.05)
            raise TimeoutError("owner-a-proof-not-ready")

    def seal(*, request_id, cleanup_subject, participating_node_ids):
        assert cleanup_subject["request_id"] == request_id
        assert participating_node_ids == frozenset({"node-a"})
        with route._lock:
            route._active_route_requests[request_id]["cleanup_receipt_published"] = True
        published[request_id].set()
        return {"request_id": request_id}

    route._wait_for_cancellation_cleanup = reconcile_wait
    route._seal_and_publish_request_cleanup_receipt = seal
    route._record_scoped_runtime_incident = lambda *, request_id, **_kwargs: (
        incidents.append(request_id)
    )
    monkeypatch.setattr(
        route_module,
        "_CLEANUP_RECONCILIATION_HORIZON_SECONDS",
        0.2,
    )
    absolute_deadline_ms = int(time.monotonic() * 1_000) + 10_000

    try:
        route._schedule_cleanup_reconciliation(
            request_id="queue-owner-a",
            cleanup_subject={
                "request_id": "queue-owner-a",
                "absolute_deadline_ms": absolute_deadline_ms,
            },
            participating_node_ids=frozenset({"node-a"}),
        )
        assert owner_a_entered.wait(timeout=1.0)
        route._schedule_cleanup_reconciliation(
            request_id="queue-owner-b",
            cleanup_subject={
                "request_id": "queue-owner-b",
                "absolute_deadline_ms": absolute_deadline_ms,
            },
            participating_node_ids=frozenset({"node-a"}),
        )
        assert published["queue-owner-b"].wait(timeout=1.0)
        assert "queue-owner-b" not in incidents
    finally:
        route._cleanup_reconciliation_stop.set()
        route._cleanup_reconciliation_executor.shutdown(
            wait=True,
            cancel_futures=True,
        )


def test_cleanup_reconciliation_horizon_starts_after_data_request_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._closed = False
    route._cleanup_reconciliation_stop = threading.Event()
    route._active_route_requests = {
        "post-authority-owner": {
            "terminal": False,
            "cleanup_receipt_published": False,
        }
    }
    entered = threading.Event()
    published = threading.Event()

    def reconcile_wait(
        participating_node_ids,
        *,
        deadline_monotonic_s,
        cleanup_subject,
        receipt_only=False,
    ):
        assert participating_node_ids == frozenset({"node-a"})
        assert cleanup_subject["request_id"] == "post-authority-owner"
        assert deadline_monotonic_s > time.monotonic()
        assert receipt_only is True
        entered.set()

    def seal(*, request_id, cleanup_subject, participating_node_ids):
        assert request_id == "post-authority-owner"
        assert cleanup_subject["request_id"] == request_id
        assert participating_node_ids == frozenset({"node-a"})
        published.set()
        return {"request_id": request_id}

    route._wait_for_cancellation_cleanup = reconcile_wait
    route._seal_and_publish_request_cleanup_receipt = seal
    monkeypatch.setattr(
        route_module,
        "_CLEANUP_RECONCILIATION_HORIZON_SECONDS",
        0.2,
    )

    try:
        route._schedule_cleanup_reconciliation(
            request_id="post-authority-owner",
            cleanup_subject={
                "request_id": "post-authority-owner",
                "absolute_deadline_ms": int(time.monotonic() * 1_000) - 1,
            },
            participating_node_ids=frozenset({"node-a"}),
        )
        assert entered.wait(timeout=1.0)
        assert published.wait(timeout=1.0)
    finally:
        route._cleanup_reconciliation_stop.set()
        executor = getattr(route, "_cleanup_reconciliation_executor", None)
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)


def test_response_reader_failure_becomes_request_scoped_route_incident() -> None:
    recorded: list[tuple[str, str, str]] = []

    class ReaderFailed(RuntimeError):
        code = "response_command_mismatch"

    class Session:
        def send(self, **_kwargs):
            raise ReaderFailed

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": Session()}
    route._active_route_requests = {
        "request-reader-failed": {
            "terminal": False,
            "inflight_commands": {},
        }
    }
    route._record_active_runtime_failure = (
        lambda *, request_id, node_id, reason, observed_at_monotonic_s: recorded.append(
            (request_id, node_id, reason)
        )
    )

    with pytest.raises(ReaderFailed):
        route._send_request_command(
            request_id="request-reader-failed",
            node_id="node-a",
            command_id="command-reader-failed",
            command="infer_start",
            payload={},
        )

    assert recorded == [
        (
            "request-reader-failed",
            "node-a",
            "response_command_mismatch",
        )
    ]


def test_cleanup_proof_does_not_wait_for_delayed_cancellation_acknowledgement() -> None:
    node_a_cancelled = threading.Event()
    node_b_cancel_entered = threading.Event()
    release_node_b = threading.Event()
    snapshot_started = threading.Event()

    class Session:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "infer_cancel_wait"
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
    route._sessions = {node_id: Session(node_id) for node_id in ("node-a", "node-b")}
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
    cleanup_thread.join(timeout=0.2)
    assert not cleanup_thread.is_alive()
    assert not snapshot_started.is_set()

    release_node_b.set()
    cancel_thread.join(timeout=1.0)
    assert not cancel_thread.is_alive()
    fanout = route._active_route_requests["request-a"]["cancellation_fanout_complete"]
    assert isinstance(fanout, threading.Event)
    assert fanout.wait(timeout=1.0)


def test_owner_cancellation_fanout_overtakes_unfinished_request_control_bind() -> None:
    control_bound = threading.Event()
    cancel_sent = threading.Event()
    interrupted: list[tuple[str, str]] = []

    class Session:
        def interrupt_command(self, command_id: str, *, code: str) -> bool:
            interrupted.append((command_id, code))
            return True

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "infer_cancel_wait"
            assert deadline_monotonic_s > time.monotonic()
            cancel_sent.set()
            return {
                "details": {
                    "request_cleanup": {
                        **{
                            key: value
                            for key, value in payload.items()
                            if key != "deadline_budget_ms"
                        },
                        "runtime_clean": True,
                        "transport_clean": True,
                        "cancellation_worker_complete": True,
                        "complete": True,
                    }
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": Session()}
    route._last_snapshots = {}
    route._last_health = {}
    route._active_route_requests = {
        "request-a": {
            "request_attempt": 1,
            "path_id": "path-a",
            "path_attempt": 0,
            "path_digest": "sha256:" + "a" * 64,
            "deployment_id": "deployment-a",
            "deployment_epoch": 1,
            "qualification_digest": "sha256:" + "b" * 64,
            "topology_generation": 1,
            "command_id": "command-a",
            "publisher_generation": 1,
            "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
            "entry_node_id": "node-a",
            "participating_node_ids": frozenset({"node-a"}),
            "cancellation_generation": 0,
            "cancellation_requested": False,
            "cancellation_started_at": None,
            "cancellation_deadline": None,
            "terminal": False,
            "control_bound": False,
            "control_bound_event": control_bound,
            "inflight_commands": {"node-a": "node-a-bind-request-control"},
        }
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response

    assert route.cancel_request(
        "request-a",
        deadline_monotonic_s=time.monotonic() + 1.0,
    )
    fanout_complete = route._active_route_requests["request-a"][
        "cancellation_fanout_complete"
    ]
    assert isinstance(fanout_complete, threading.Event)
    assert cancel_sent.wait(timeout=1.0)
    assert fanout_complete.wait(timeout=1.0)
    assert interrupted == []
    assert control_bound.is_set() is False


def test_cleanup_waits_for_owner_fanout_before_fallback_snapshot() -> None:
    fanout_complete = threading.Event()
    snapshot_started = threading.Event()
    proof_complete = threading.Event()
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._active_route_requests = {
        "request-a": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": fanout_complete,
            "cancellation_started_at": time.monotonic(),
        }
    }

    def cleanup_complete(*_args, **_kwargs) -> bool:
        return proof_complete.is_set()

    def snapshot(*_args, **_kwargs) -> None:
        snapshot_started.set()
        proof_complete.set()

    route._cancellation_cleanup_complete = cleanup_complete
    route._snapshot_nodes_before = snapshot

    cleanup_thread = threading.Thread(
        target=route._wait_for_cancellation_cleanup,
        args=(frozenset({"node-a"}),),
        kwargs={
            "deadline_monotonic_s": time.monotonic() + 1.0,
            "cleanup_subject": {"request_id": "request-a"},
            "receipt_only": True,
        },
    )
    cleanup_thread.start()
    time.sleep(0.1)
    assert cleanup_thread.is_alive()
    assert snapshot_started.is_set() is False

    fanout_complete.set()
    cleanup_thread.join(timeout=1.0)

    assert cleanup_thread.is_alive() is False
    assert snapshot_started.is_set()


def test_cleanup_fallback_starts_after_inline_grace_without_global_fanout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "mycelium_live.route._CLEANUP_INLINE_RECEIPT_GRACE_SECONDS",
        0.02,
    )
    fanout_complete = threading.Event()
    proof_complete = threading.Event()
    snapshot_started_at: list[float] = []
    started = time.monotonic()
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._active_route_requests = {
        "request-a": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": fanout_complete,
            "cancellation_started_at": started,
        }
    }
    route._cancellation_cleanup_complete = lambda *_args, **_kwargs: (
        proof_complete.is_set()
    )

    def snapshot(*_args, **_kwargs) -> None:
        snapshot_started_at.append(time.monotonic())
        proof_complete.set()

    route._snapshot_nodes_before = snapshot

    route._wait_for_cancellation_cleanup(
        frozenset({"node-a"}),
        deadline_monotonic_s=started + 0.3,
        cleanup_subject={"request_id": "request-a"},
        receipt_only=True,
    )

    assert fanout_complete.is_set() is False
    assert len(snapshot_started_at) == 1
    assert snapshot_started_at[0] >= started + 0.015
    assert snapshot_started_at[0] < started + 0.2


def test_cleanup_snapshot_retries_response_timeout_inside_owner_deadline() -> None:
    fanout_complete = threading.Event()
    fanout_complete.set()
    proof_complete = threading.Event()
    attempt_deadlines: list[float] = []
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._active_route_requests = {
        "request-a": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": fanout_complete,
            "cancellation_started_at": time.monotonic(),
        }
    }
    route._cleanup_blockers = lambda *_args, **_kwargs: "node-a=receipt_missing"
    route._cancellation_cleanup_complete = lambda *_args, **_kwargs: (
        proof_complete.is_set()
    )

    def snapshot(*_args, deadline_monotonic_s, **_kwargs) -> None:
        attempt_deadlines.append(deadline_monotonic_s)
        if len(attempt_deadlines) == 1:
            raise RuntimeError("node_response_timeout@node-a")
        proof_complete.set()

    route._snapshot_nodes_before = snapshot
    started = time.monotonic()
    owner_deadline = started + 1.5

    route._wait_for_cancellation_cleanup(
        frozenset({"node-a"}),
        deadline_monotonic_s=owner_deadline,
        cleanup_subject={"request_id": "request-a"},
        receipt_only=True,
    )

    assert len(attempt_deadlines) == 2
    assert all(started < deadline <= owner_deadline for deadline in attempt_deadlines)
    assert attempt_deadlines[0] <= started + 0.55


def test_cleanup_fallback_snapshots_only_nodes_missing_fanout_receipts() -> None:
    fanout_complete = threading.Event()
    fanout_complete.set()
    cleanup_subject = {"request_id": "request-a", "path_id": "path-a"}
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._active_route_requests = {
        "request-a": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": fanout_complete,
        }
    }
    route._cleanup_snapshots_by_request = {
        "request-a": {
            "node-a": {
                "details": {"request_cleanup": {**cleanup_subject, "complete": True}}
            }
        }
    }
    snapshot_node_sets: list[frozenset[str]] = []

    def snapshot(node_ids, **_kwargs) -> None:
        snapshot_node_sets.append(node_ids)
        with route._lock:
            route._cleanup_snapshots_by_request["request-a"]["node-b"] = {
                "details": {"request_cleanup": {**cleanup_subject, "complete": True}}
            }

    route._snapshot_nodes_before = snapshot

    route._wait_for_cancellation_cleanup(
        frozenset({"node-a", "node-b"}),
        deadline_monotonic_s=time.monotonic() + 1.0,
        cleanup_subject=cleanup_subject,
        receipt_only=True,
    )

    assert snapshot_node_sets == [frozenset({"node-b"})]


def test_cleanup_proof_does_not_wait_for_an_unrelated_request_fanout() -> None:
    unrelated_fanout = threading.Event()
    own_fanout = threading.Event()
    own_fanout.set()

    class Session:
        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "snapshot"
            assert deadline_monotonic_s > time.monotonic()
            return {
                "details": {
                    "event": "snapshot",
                    "request_cleanup": {
                        **payload["cleanup_subject"],
                        "complete": True,
                    },
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": Session()}
    route._last_snapshots = {}
    route._last_health = {}
    route._active_route_requests = {
        "request-own": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": own_fanout,
        },
        "request-unrelated": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": unrelated_fanout,
        },
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._ingest_transport_scoped_events = lambda *_args, **_kwargs: None
    cleanup_subject = {"request_id": "request-own"}

    route._wait_for_cancellation_cleanup(
        frozenset({"node-a"}),
        deadline_monotonic_s=time.monotonic() + 0.2,
        cleanup_subject=cleanup_subject,
        receipt_only=True,
    )

    assert unrelated_fanout.is_set() is False
    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}),
        cleanup_subject=cleanup_subject,
    )


def test_cleanup_proof_rechecks_concurrent_receipt_after_snapshot_error() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": object()}
    route._active_route_requests = {}
    route._cleanup_snapshots_by_request = {}
    cleanup_subject = {"request_id": "request-a", "path_id": "path-a"}

    def failing_snapshot(*_args, **_kwargs) -> None:
        with route._lock:
            route._cleanup_snapshots_by_request["request-a"] = {
                "node-a": {
                    "details": {
                        "request_cleanup": {
                            **cleanup_subject,
                            "complete": True,
                        }
                    }
                }
            }
        raise TimeoutError("redundant_snapshot_timed_out")

    route._snapshot_nodes_before = failing_snapshot

    route._wait_for_cancellation_cleanup(
        frozenset({"node-a"}),
        deadline_monotonic_s=time.monotonic() + 1.0,
        cleanup_subject=cleanup_subject,
        receipt_only=True,
    )


def test_aggregate_cleanup_receipt_remains_authoritative_for_late_waiter() -> None:
    fanout_complete = threading.Event()
    fanout_complete.set()
    cleanup_subject = {
        "request_id": "request-a",
        "request_attempt": 1,
        "path_id": "path-a",
        "path_attempt": 0,
        "path_digest": "sha256:" + "a" * 64,
        "deployment_id": "deployment-a",
    }
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": object(), "node-b": object()}
    route._active_route_requests = {
        "request-a": {
            "cancellation_requested": True,
            "cancellation_fanout_complete": fanout_complete,
        }
    }
    route._request_cleanup_receipts = {}
    route._cleanup_snapshots_by_request = {
        "request-a": {
            node_id: {
                "details": {"request_cleanup": {**cleanup_subject, "complete": True}}
            }
            for node_id in route._sessions
        }
    }
    route._store_request_cleanup_receipt(
        request_id="request-a",
        cleanup_subject=cleanup_subject,
        participating_node_ids=frozenset(route._sessions),
        deployment_id="deployment-a",
    )
    assert route._cleanup_snapshots_by_request == {}

    def stale_snapshot(*_args, **_kwargs) -> None:
        raise AssertionError("sealed cleanup proof must not be re-observed")

    route._snapshot_nodes_before = stale_snapshot
    route._wait_for_cancellation_cleanup(
        frozenset(route._sessions),
        deadline_monotonic_s=time.monotonic() - 1.0,
        cleanup_subject=cleanup_subject,
        receipt_only=True,
    )

    assert route._cancellation_cleanup_complete(
        frozenset(route._sessions), cleanup_subject=cleanup_subject
    )
    assert not route._cancellation_cleanup_complete(
        frozenset(route._sessions),
        cleanup_subject={**cleanup_subject, "path_id": "path-other"},
    )


def test_concurrent_cleanup_receipts_do_not_overwrite_other_requests() -> None:
    class Session:
        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            assert command_id
            assert command == "snapshot"
            assert deadline_monotonic_s > time.monotonic()
            return {
                "details": {
                    "request_cleanup": {
                        **payload["cleanup_subject"],
                        "complete": True,
                    },
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": Session()}
    route._last_snapshots = {}
    route._cleanup_snapshots_by_request = {}
    route._last_health = {}
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._ingest_transport_scoped_events = lambda *_args, **_kwargs: None
    subject_a = {"request_id": "request-a", "path_id": "path-a"}
    subject_b = {"request_id": "request-b", "path_id": "path-b"}
    deadline = time.monotonic() + 1.0

    route._snapshot_nodes_before(
        frozenset({"node-a"}),
        deadline_monotonic_s=deadline,
        cleanup_subject=subject_a,
        receipt_only=True,
    )
    route._snapshot_nodes_before(
        frozenset({"node-a"}),
        deadline_monotonic_s=deadline,
        cleanup_subject=subject_b,
        receipt_only=True,
    )

    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject=subject_a
    )
    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject=subject_b
    )


def test_incomplete_fallback_snapshot_cannot_overwrite_complete_inline_receipt() -> (
    None
):
    cleanup_subject = {"request_id": "request-a", "path_id": "path-a"}
    observations = iter(
        (
            {"details": {"request_cleanup": {**cleanup_subject, "complete": True}}},
            {"details": {"request_cleanup": {**cleanup_subject, "complete": False}}},
        )
    )

    class Session:
        def send_before(self, **_kwargs):
            return next(observations)

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": Session()}
    route._last_snapshots = {}
    route._cleanup_snapshots_by_request = {}
    route._last_health = {}
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._ingest_transport_scoped_events = lambda *_args, **_kwargs: None
    deadline = time.monotonic() + 1.0

    for _ in range(2):
        route._snapshot_nodes_before(
            frozenset({"node-a"}),
            deadline_monotonic_s=deadline,
            cleanup_subject=cleanup_subject,
            receipt_only=True,
        )

    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject=cleanup_subject
    )


def test_snapshot_fanout_commits_successful_receipt_before_sibling_error() -> None:
    node_a_returned = threading.Event()
    cleanup_subject = {"request_id": "request-a", "path_id": "path-a"}

    class Session:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            del command_id, deadline_monotonic_s
            assert command == "snapshot"
            if self.node_id == "node-b":
                assert node_a_returned.wait(timeout=1.0)
                raise TimeoutError("node-b snapshot delayed")
            node_a_returned.set()
            return {
                "details": {
                    "request_cleanup": {
                        **payload["cleanup_subject"],
                        "complete": True,
                    }
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {node_id: Session(node_id) for node_id in ("node-a", "node-b")}
    route._last_snapshots = {}
    route._cleanup_snapshots_by_request = {}
    route._last_health = {}
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._ingest_transport_scoped_events = lambda *_args, **_kwargs: None

    with pytest.raises(RuntimeError, match="node-b"):
        route._snapshot_nodes_before(
            frozenset({"node-a", "node-b"}),
            deadline_monotonic_s=time.monotonic() + 1.0,
            cleanup_subject=cleanup_subject,
            receipt_only=True,
        )

    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject=cleanup_subject
    )


def test_cancellation_fanout_commits_successful_receipt_before_sibling_error() -> None:
    node_a_returned = threading.Event()

    class Session:
        def __init__(self, node_id: str) -> None:
            self.node_id = node_id

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            del command_id, deadline_monotonic_s
            assert command == "infer_cancel_wait"
            if self.node_id == "node-b":
                assert node_a_returned.wait(timeout=1.0)
                raise TimeoutError("node-b cancellation delayed")
            node_a_returned.set()
            return {
                "details": {
                    "request_cleanup": {
                        **{
                            key: value
                            for key, value in payload.items()
                            if key != "deadline_budget_ms"
                        },
                        "complete": True,
                    }
                }
            }

    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._last_snapshots = {}
    route._cleanup_snapshots_by_request = {}
    route._sessions = {node_id: Session(node_id) for node_id in ("node-a", "node-b")}
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

    assert route.cancel_request(
        "request-a", deadline_monotonic_s=time.monotonic() + 1.0
    )
    barrier = route._active_route_requests["request-a"]["cancellation_fanout_complete"]
    assert barrier.wait(timeout=1.0)
    cleanup_subject = route._request_cleanup_subject("request-a")
    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}), cleanup_subject=cleanup_subject
    )
    assert (
        route._active_route_requests["request-a"]["cancellation_fanout_error"]
        == "node-b cancellation delayed"
    )


def test_cancellation_fanout_reserves_owner_budget_for_fallback_snapshot() -> None:
    cleanup_subject: dict[str, object] = {}
    command_deadlines: dict[str, float] = {}
    command_budget_expiries: dict[str, float] = {}
    published_receipts: list[dict[str, object]] = []
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._last_snapshots = {}
    route._cleanup_snapshots_by_request = {}
    route._last_health = {}
    route._sessions = {"node-a": object()}
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
            "participating_node_ids": frozenset({"node-a"}),
            "cancellation_generation": 0,
            "cancellation_requested": False,
            "cancellation_started_at": None,
            "cancellation_deadline": None,
            "terminal": False,
            "publish_cleanup_receipt": lambda receipt: published_receipts.append(
                dict(receipt)
            ),
        }
    }
    route._command_id = lambda node_id, action: f"{node_id}:{action}"
    route._verify_observation = lambda node_id, response, **kwargs: response
    route._ingest_transport_scoped_events = lambda *_args, **_kwargs: None

    def send_before(
        _node_id,
        *,
        command_id,
        command,
        payload,
        deadline_monotonic_s,
    ):
        del command_id
        command_deadlines[command] = deadline_monotonic_s
        if command == "infer_cancel_wait":
            command_budget_expiries[command] = time.monotonic() + (
                payload["deadline_budget_ms"] / 1_000.0
            )
            cleanup_subject.update(
                {
                    key: value
                    for key, value in payload.items()
                    if key != "deadline_budget_ms"
                }
            )
            raise TimeoutError("node_response_timeout")
        assert command == "snapshot"
        return {
            "details": {
                "request_cleanup": {
                    **payload["cleanup_subject"],
                    "complete": True,
                }
            }
        }

    route._send_before = send_before
    owner_deadline = time.monotonic() + 1.5

    assert (
        route.cancel_request("request-a", deadline_monotonic_s=owner_deadline) is True
    )
    route._wait_for_cancellation_cleanup(
        frozenset({"node-a"}),
        deadline_monotonic_s=owner_deadline,
        cleanup_subject=route._request_cleanup_subject("request-a"),
        receipt_only=True,
    )

    assert cleanup_subject == route._request_cleanup_subject("request-a")
    assert command_deadlines["infer_cancel_wait"] <= owner_deadline - 0.49
    assert (
        command_budget_expiries["infer_cancel_wait"]
        <= command_deadlines["infer_cancel_wait"] + 0.01
    )
    assert command_deadlines["snapshot"] <= owner_deadline
    assert route._cancellation_cleanup_complete(
        frozenset({"node-a"}),
        cleanup_subject=cleanup_subject,
    )
    assert len(published_receipts) == 1
    assert published_receipts[0]["request_id"] == "request-a"
    assert published_receipts[0]["node_ids"] == ["node-a"]


def test_concurrent_cleanup_waiters_publish_one_identical_route_receipt() -> None:
    request_id = "request-concurrent-publication"
    cleanup_subject = {
        "deployment_id": "deployment-a",
        "request_id": request_id,
        "path_id": "path-a",
    }
    publisher_entered = threading.Event()
    release_publisher = threading.Event()
    published_receipts: list[dict[str, object]] = []
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = {"node-a": object()}
    route._request_cleanup_receipts = {}
    route._cleanup_snapshots_by_request = {
        request_id: {
            "node-a": {
                "details": {
                    "request_cleanup": {
                        **cleanup_subject,
                        "complete": True,
                    }
                }
            }
        }
    }

    def publish(receipt) -> None:
        published_receipts.append(dict(receipt))
        publisher_entered.set()
        assert release_publisher.wait(timeout=1.0)

    route._active_route_requests = {
        request_id: {
            "deployment_id": "deployment-a",
            "publish_cleanup_receipt": publish,
            "cleanup_receipt_publication_event": threading.Event(),
            "cleanup_receipt_publication_started": False,
            "cleanup_receipt_published": False,
        }
    }
    results: list[dict[str, object] | None] = []

    def seal() -> None:
        results.append(
            route._seal_and_publish_request_cleanup_receipt(
                request_id=request_id,
                cleanup_subject=cleanup_subject,
                participating_node_ids=frozenset({"node-a"}),
            )
        )

    workers = [threading.Thread(target=seal) for _ in range(2)]
    workers[0].start()
    assert publisher_entered.wait(timeout=1.0)
    workers[1].start()
    release_publisher.set()
    for worker in workers:
        worker.join(timeout=1.0)

    assert all(not worker.is_alive() for worker in workers)
    assert len(published_receipts) == 1
    assert len(results) == 2
    assert results[0] == results[1] == published_receipts[0]


def test_failed_peer_cancellation_still_closes_the_fanout_barrier() -> None:
    class Session:
        def __init__(self, node_id: str, *, failed: bool = False) -> None:
            self.node_id = node_id
            self.failed = failed

        def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
            del command_id, payload, deadline_monotonic_s
            assert command == "infer_cancel_wait"
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

    assert (
        route.cancel_request(
            "request-a",
            deadline_monotonic_s=time.monotonic() + 1.0,
        )
        is True
    )

    barrier = route._active_route_requests["request-a"]["cancellation_fanout_complete"]
    assert isinstance(barrier, threading.Event)
    assert barrier.wait(timeout=1.0)
    assert (
        route._active_route_requests["request-a"]["cancellation_fanout_error"]
        == "node_process_exited"
    )


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
    assert incidents == [("peer_process_lost", "route_peer_process_lost", None)]


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

    assert [request_id for request_id, _deadline in cancelled] == ["request-affected"]
    assert 0.0 < cancelled[0][1] - started <= 2.01
    liveness_incident = route._liveness.incidents()[0]
    assert liveness_incident.affected_track_ids == ("request-affected",)
    assert liveness_incident.scope == "request"


def test_active_liveness_monitor_does_not_probe_cancelling_requests() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._open = True
    route._closed = False
    route._active_route_requests = {
        "request-cancelling": {
            "participating_node_ids": frozenset({"node-a", "node-b"}),
            "cancellation_requested": True,
            "terminal": False,
        }
    }
    route._send_before = lambda *_args, **_kwargs: pytest.fail(
        "cancellation cleanup must own the control lane"
    )

    route._monitor_active_liveness_once()


def test_observed_owner_cancellation_is_installed_after_route_registration() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._active_route_requests = {
        "request-raced": {
            "terminal": False,
            "cancellation_requested": False,
            "cancellation_deadline": None,
        }
    }
    authorized_deadline = time.monotonic() + 1.0
    authorizations: list[float] = []
    cancellations: list[tuple[str, float | None]] = []

    def authorize_cleanup(proposed_deadline: float) -> float:
        authorizations.append(proposed_deadline)
        return authorized_deadline

    def cancel_request(
        request_id: str,
        *,
        deadline_monotonic_s: float | None = None,
    ) -> bool:
        cancellations.append((request_id, deadline_monotonic_s))
        route._active_route_requests[request_id].update(
            cancellation_requested=True,
            cancellation_deadline=deadline_monotonic_s,
        )
        return True

    route.cancel_request = cancel_request

    observed_deadline = route._install_observed_owner_cancellation(
        "request-raced",
        authorize_cleanup=authorize_cleanup,
    )

    assert observed_deadline == authorized_deadline
    assert len(authorizations) == 1
    assert cancellations == [("request-raced", authorized_deadline)]

    # Once the route owns the physical generation, repeated callback
    # observations still re-enter the idempotent controller authorization.
    # This is required because route liveness may have initiated teardown
    # before the gateway command controller observed the failure.  The route
    # itself is not cancelled again and the controller callback returns the
    # same immutable deadline.
    assert (
        route._install_observed_owner_cancellation(
            "request-raced",
            authorize_cleanup=authorize_cleanup,
        )
        == authorized_deadline
    )
    assert authorizations == [authorizations[0], authorized_deadline]
    assert cancellations == [("request-raced", authorized_deadline)]


def test_failed_request_cleanup_authorizes_route_liveness_generation() -> None:
    route = object.__new__(PhysicalLiveRoute)
    route._lock = threading.RLock()
    route_deadline = time.monotonic() + 1.0
    route._active_route_requests = {
        "request-route-first": {
            "terminal": False,
            "cancellation_requested": True,
            "cancellation_deadline": route_deadline,
        }
    }
    authorizations: list[float] = []

    def authorize_cleanup(proposed_deadline: float) -> float:
        authorizations.append(proposed_deadline)
        return route_deadline

    route.cancel_request = lambda *_args, **_kwargs: pytest.fail(
        "route-first cancellation must not be issued a second time"
    )

    cleanup_deadline = route._authorize_failed_request_cleanup(
        "request-route-first",
        failure_observed_at=time.monotonic(),
        authorize_cleanup=authorize_cleanup,
    )

    assert cleanup_deadline == route_deadline
    assert authorizations == [route_deadline]
