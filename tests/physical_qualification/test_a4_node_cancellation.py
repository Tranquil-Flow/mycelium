from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from physical_inference_node import NodeCommandError, PhysicalNodeService


def test_cancel_observation_survives_router_retiring_exact_request() -> None:
    request_id = "request-retired-during-cancel"

    class Router:
        def cancel(self, candidate: str) -> bool:
            assert candidate == request_id
            return True

        def request_status(self, candidate: str) -> str:
            assert candidate == request_id
            raise KeyError(candidate)

    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-a",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    service.state = "RUNNING"
    service.router = Router()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service._control_lock = threading.RLock()
    service._request_controls = {request_id: dict(control)}
    service._pending_cancellations = {}
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    payload = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }

    result = service._infer_cancel(payload)

    assert result["event"] == "inference_cancelled"
    assert result["details"] == {
        "request_id": request_id,
        "cancelled": True,
        "status": "CANCELLED",
    }
    assert service._request_controls[request_id]["cancellation_generation"] == 1


def test_completed_router_record_still_issues_exact_transport_teardown() -> None:
    request_id = "request-completed-before-cleanup"

    class Router:
        def cancel(self, candidate: str) -> bool:
            assert candidate == request_id
            return False

        def request_status(self, candidate: str) -> str:
            assert candidate == request_id
            raise KeyError(candidate)

    class Transport:
        cancellation = None

        def send_path_cancellation_if_entry(self, cancellation) -> bool:
            self.cancellation = cancellation
            return True

    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-completed",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    service.state = "RUNNING"
    service.router = Router()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service.transport = Transport()
    service._control_lock = threading.RLock()
    service._request_controls = {request_id: dict(control)}
    service._pending_cancellations = {}
    service._cancellation_controls = {}
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    payload = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }

    result = service._infer_cancel(payload)

    assert result["details"]["cancelled"] is False
    cancellation = service.transport.cancellation
    assert cancellation.request_id == request_id
    assert cancellation.path_id == control["path_id"]
    assert cancellation.path_attempt == control["path_attempt"]


def test_generation_fenced_cancel_can_win_concurrent_infer_start() -> None:
    request_id = "request-cancel-before-start"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-a",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    service = object.__new__(PhysicalNodeService)
    service.state = "RUNNING"
    service.router = object()
    service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service._clock = SimpleNamespace(now=lambda: 10.0)
    service._sinks = {}
    service._control_lock = threading.RLock()
    service._request_controls = {
        request_id: {**control, "cancellation_generation": 1}
    }
    service._pending_cancellations = {}
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    request = {
        "request_id": request_id,
        "prompt_token_ids": [1, 2, 3],
        "max_new_tokens": 4,
        "expected_new_tokens": 4,
        "qos_class": "interactive",
        "admitted_at": 0.0,
        "target_ttft_ms": 1_000.0,
        "target_tpot_ms": 100.0,
        "target_tokens_per_second": 10.0,
        "sampling_seed": 0,
        "generation_config_digest": "sha256:" + "c" * 64,
    }

    result = service._infer_start(
        {
            "request": request,
            "control": control,
            "path_manifest": {},
        }
    )

    assert result["event"] == "inference_started"
    assert result["details"] == {
        "request_id": request_id,
        "status": "CANCELLED",
        "output": {"token_indexes": [], "token_ids": []},
        "path": None,
    }
    assert service._request_controls[request_id]["cancellation_generation"] == 1
    assert request_id not in service._sinks


def test_generation_fenced_cancel_can_win_concurrent_participant_control_bind() -> None:
    request_id = "request-cancel-before-participant-bind"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-a",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    cancellation = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    service.state = "RUNNING"
    untyped_service.router = object()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service._control_lock = threading.RLock()
    service._request_controls = {}
    service._pending_cancellations = {}
    service._cancellation_controls = {}
    service._request_cleanup_receipts = {}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    cancelled = service._infer_cancel(cancellation)
    bound = service._bind_request_control(
        {"request_id": request_id, "control": control}
    )
    retried = service._bind_request_control(
        {"request_id": request_id, "control": control}
    )

    assert cancelled["details"]["pending_start"] is True
    assert bound["event"] == "request_control_bound"
    assert retried == bound
    assert service._request_controls[request_id]["cancellation_generation"] == 1
    assert request_id not in service._pending_cancellations
    assert service._cancellation_controls[request_id] == {
        key: value
        for key, value in cancellation.items()
        if key != "deadline_budget_ms"
    }


def test_pending_cancel_rejects_noncanonical_topology_generation() -> None:
    request_id = "request-invalid-cancel-control"
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    service.state = "RUNNING"
    untyped_service.router = object()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service._control_lock = threading.RLock()
    service._request_controls = {}
    service._pending_cancellations = {}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    payload = {
        "request_id": request_id,
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-a",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 0,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }

    with pytest.raises(NodeCommandError, match="invalid_infer_cancel_control"):
        service._infer_cancel(payload)

    assert service._pending_cancellations == {}


def test_cleanup_snapshot_seeds_control_from_pending_cancel_before_start() -> None:
    request_id = "request-cancel-before-start-snapshot"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-a",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-a",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    cancellation = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }
    cleanup_subject = {
        key: value for key, value in cancellation.items()
        if key != "deadline_budget_ms"
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    service.state = "RUNNING"
    untyped_service.router = object()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service._control_lock = threading.RLock()
    service._request_controls = {}
    service._pending_cancellations = {}
    service._cancellation_controls = {}
    service._cancellations_by_subject = {}
    service._request_cleanup_receipts = {}
    service._sinks = {}
    untyped_service._last_cancellation = None
    untyped_service.router = SimpleNamespace(
        cancel=lambda request_id: True,
        request_status=lambda request_id: "CANCELLED",
    )
    untyped_service.runtime = SimpleNamespace(
        kv_snapshot=lambda: {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "active_state_count": 0,
            "active_kv_bytes": 0,
        }
    )
    untyped_service.capacity = None
    untyped_service.transport = None
    untyped_service.sidecar = None
    untyped_service._host_resources = lambda: {}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    cancelled = service._infer_cancel(cancellation)
    assert cancelled["details"]["pending_start"] is True

    snapshot = service._snapshot({"cleanup_subject": dict(cleanup_subject)})
    receipt = snapshot["details"]["request_cleanup"]
    assert receipt["complete"] is True
    assert request_id in service._request_cleanup_receipts
    # The receipt path retires the seeded control and the pending
    # cancellation: nothing request-scoped remains on the node.
    assert request_id not in service._request_controls
    assert request_id not in service._pending_cancellations
    assert request_id not in service._cancellation_controls
