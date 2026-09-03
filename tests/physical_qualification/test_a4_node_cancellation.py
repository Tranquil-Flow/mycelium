from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

import physical_inference_node as node_module
from physical_inference_node import NodeCommandError, PhysicalNodeService


def _request_control(*, cancellation_generation: int) -> dict[str, object]:
    return {
        "deployment_id": "deployment-a",
        "deployment_epoch": 1,
        "qualification_digest": "sha256:" + "1" * 64,
        "command_id": "command-a",
        "publisher_generation": 1,
        "absolute_deadline_ms": 9_000_000_000_000,
        "request_attempt": 1,
        "path_id": "path-a",
        "path_attempt": 1,
        "path_digest": "sha256:" + "2" * 64,
        "topology_generation": 1,
        "cancellation_generation": cancellation_generation,
    }


@pytest.mark.parametrize("operation", ["infer_start", "infer_decode"])
def test_owner_path_cancelled_is_request_cancelled_only_for_exact_newer_control(
    operation: str,
) -> None:
    request_id = "request-owner-cancelled"
    command_control = _request_control(cancellation_generation=0)
    owner_control = _request_control(cancellation_generation=1)
    service = SimpleNamespace(
        _control_lock=threading.RLock(),
        _cancellation_controls={request_id: owner_control},
        _request_cleanup_receipts={},
    )
    payload: dict[str, object] = {"control": command_control}
    if operation == "infer_start":
        payload["request"] = {"request_id": request_id}
    else:
        payload["request_id"] = request_id
    command = {"command": operation, "payload": payload}

    assert node_module._owner_cancellation_interrupted_inference(
        service,
        command,
        node_module.IrohTransportError("path_cancelled"),
    )

    service._cancellation_controls = {
        "different-request": owner_control,
    }
    assert not node_module._owner_cancellation_interrupted_inference(
        service,
        command,
        node_module.IrohTransportError("path_cancelled"),
    )


def test_sealed_cleanup_receipt_classifies_late_inference_unwind() -> None:
    request_id = "request-late-owner-cancelled"
    command_control = _request_control(cancellation_generation=0)
    cleanup_receipt = {
        **_request_control(cancellation_generation=1),
        "request_id": request_id,
        "runtime_clean": True,
        "transport_clean": True,
        "cancellation_worker_complete": True,
        "complete": True,
    }
    service = SimpleNamespace(
        _control_lock=threading.RLock(),
        _cancellation_controls={},
        _request_cleanup_receipts={request_id: cleanup_receipt},
    )
    command = {
        "command": "infer_start",
        "payload": {
            "request": {"request_id": request_id},
            "control": command_control,
        },
    }

    assert node_module._owner_cancellation_interrupted_inference(
        service,
        command,
        node_module.IrohTransportError("path_cancelled"),
    )

    cleanup_receipt["complete"] = False
    assert not node_module._owner_cancellation_interrupted_inference(
        service,
        command,
        node_module.IrohTransportError("path_cancelled"),
    )


def test_owner_path_cancelled_reclassification_rejects_non_owner_failures() -> None:
    request_id = "request-not-owner-cancelled"
    control = _request_control(cancellation_generation=0)
    service = SimpleNamespace(
        _control_lock=threading.RLock(),
        _cancellation_controls={
            request_id: _request_control(cancellation_generation=1),
        },
        _request_cleanup_receipts={},
    )

    assert not node_module._owner_cancellation_interrupted_inference(
        service,
        {
            "command": "snapshot",
            "payload": {"request_id": request_id, "control": control},
        },
        node_module.IrohTransportError("path_cancelled"),
    )
    assert not node_module._owner_cancellation_interrupted_inference(
        service,
        {
            "command": "infer_decode",
            "payload": {"request_id": request_id, "control": control},
        },
        node_module.IrohTransportError("delivery_timeout"),
    )


def test_cancel_wait_acknowledges_fence_before_cleanup_snapshot() -> None:
    service = object.__new__(PhysicalNodeService)
    snapshot_called = threading.Event()
    service._infer_cancel = lambda payload, **_kwargs: {
        "event": "inference_cancelled",
        "details": {
            "request_id": payload["request_id"],
            "status": "CANCELLING",
            "cleanup_pending": True,
        },
    }
    service._snapshot = lambda _payload: snapshot_called.set()
    result = service._infer_cancel_wait(
        {
            "request_id": "request-a",
            "path_id": "path-a",
            "deadline_budget_ms": 2_000,
        }
    )

    assert result["event"] == "inference_cancelled"
    assert result["details"]["status"] == "CANCELLING"
    assert result["details"]["cleanup_pending"] is True
    assert snapshot_called.is_set() is False


def test_cancel_wait_keeps_generation_fenced_teardown_in_reserved_worker() -> None:
    service = object.__new__(PhysicalNodeService)
    deferred_modes: list[bool] = []

    def cancel(payload, *, defer_cleanup):
        deferred_modes.append(defer_cleanup)
        return {
            "event": "inference_cancelled",
            "details": {
                "request_id": payload["request_id"],
                "status": "CANCELLING",
                "cleanup_pending": True,
            },
        }

    service._infer_cancel = cancel
    service._snapshot = lambda payload: {
        "event": "snapshot",
        "details": {
            "request_cleanup": {
                **payload["cleanup_subject"],
                "complete": False,
            }
        },
    }
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    result = service._infer_cancel_wait(
        {
            "request_id": "request-deferred-control",
            "path_id": "path-deferred-control",
            "deadline_budget_ms": 1,
        }
    )

    assert deferred_modes == [False]
    assert result["details"]["cleanup_pending"] is True


def test_cancel_wait_returns_receipt_sealed_in_reserved_worker() -> None:
    service = object.__new__(PhysicalNodeService)
    request_id = "request-lifecycle-receipt"
    cleanup_subject = {
        "request_id": request_id,
        "path_id": "path-lifecycle-receipt",
        "deadline_budget_ms": 2_000,
    }
    receipt = {
        "request_id": request_id,
        "path_id": "path-lifecycle-receipt",
        "runtime_clean": True,
        "transport_clean": True,
        "cancellation_worker_complete": True,
        "complete": True,
    }
    order: list[str] = []
    service._control_lock = threading.RLock()
    service._request_cleanup_receipts = {}
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    def cancel(_payload, *, defer_cleanup):
        assert defer_cleanup is False
        order.append("cancel")
        return {
            "event": "inference_cancelled",
            "details": {
                "request_id": request_id,
                "status": "CANCELLING",
                "cleanup_pending": True,
            },
        }

    def seal(_payload, *, deadline_monotonic_s):
        assert deadline_monotonic_s <= time.monotonic() + 2.0
        order.append("seal")
        with service._control_lock:
            service._request_cleanup_receipts[request_id] = dict(receipt)

    service._infer_cancel = cancel
    service._seal_cancellation_receipt_until_deadline = seal

    result = service._infer_cancel_wait(cleanup_subject)

    assert order == ["cancel", "seal"]
    assert result["event"] == "inference_cancelled"
    assert result["details"]["cleanup_pending"] is False
    assert result["details"]["request_cleanup"] == receipt


def test_cancel_wait_does_not_enter_blocking_snapshot_before_response() -> None:
    service = object.__new__(PhysicalNodeService)
    service._control_lock = threading.RLock()
    release_worker = threading.Event()

    def apply_cleanup() -> None:
        assert release_worker.wait(timeout=1.0)

    worker = threading.Thread(target=apply_cleanup)
    service._cancellation_workers = {"request-a": worker}
    service._infer_cancel = lambda _payload, **_kwargs: {
        "event": "inference_cancelled",
        "details": {"status": "CANCELLING", "cleanup_pending": True},
    }

    def snapshot(_payload):
        raise AssertionError("cancel acknowledgement entered cleanup snapshot")

    service._snapshot = snapshot
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    worker.start()
    result = service._infer_cancel_wait(
        {
            "request_id": "request-a",
            "path_id": "path-a",
            "deadline_budget_ms": 1,
        }
    )
    assert worker.is_alive()
    release_worker.set()
    worker.join(timeout=1.0)

    assert result["details"]["cleanup_pending"] is True


def test_cancel_wait_leaves_receipt_polling_to_parent_snapshot_loop() -> None:
    service = object.__new__(PhysicalNodeService)
    service._infer_cancel = lambda _payload, **_kwargs: {
        "event": "inference_cancelled",
        "details": {"status": "CANCELLING", "cleanup_pending": True},
    }

    snapshot_count = 0

    def snapshot(payload):
        nonlocal snapshot_count
        snapshot_count += 1
        return {
            "event": "snapshot",
            "details": {
                "request_cleanup": {
                    **payload["cleanup_subject"],
                    "complete": snapshot_count >= 3,
                }
            },
        }

    service._snapshot = snapshot
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    result = service._infer_cancel_wait(
        {
            "request_id": "request-a",
            "path_id": "path-a",
            "deadline_budget_ms": 800,
        }
    )
    assert snapshot_count == 0
    assert result["details"]["cleanup_pending"] is True


def test_cancel_wait_budget_starts_before_inline_teardown(monkeypatch) -> None:
    class Clock:
        now = 10.0
        sleeps: list[float] = []

        @classmethod
        def monotonic(cls) -> float:
            return cls.now

        @classmethod
        def sleep(cls, seconds: float) -> None:
            cls.sleeps.append(seconds)
            cls.now += seconds

    service = object.__new__(PhysicalNodeService)

    def cancel(payload, **_kwargs):
        # Synchronous generation-fenced teardown consumed more than the
        # receipt-poll portion of the controller's remaining budget.
        Clock.now += 0.2
        return {
            "event": "inference_cancelled",
            "details": {
                "request_id": payload["request_id"],
                "status": "CANCELLING",
                "cleanup_pending": True,
            },
        }

    service._infer_cancel = cancel
    service._snapshot = lambda payload: {
        "event": "snapshot",
        "details": {
            "request_cleanup": {
                **payload["cleanup_subject"],
                "complete": False,
            }
        },
    }
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    monkeypatch.setattr(node_module, "time", Clock)

    result = service._infer_cancel_wait(
        {
            "request_id": "request-budget-entry",
            "path_id": "path-budget-entry",
            "deadline_budget_ms": 300,
        }
    )

    assert result["details"]["cleanup_pending"] is True
    assert Clock.sleeps == []


def test_cancel_wait_retains_receipt_seal_window_after_contended_teardown(
    monkeypatch,
) -> None:
    """A saturated cleanup lane must still seal already-clean teardown.

    The route has already removed its own fallback slice before transmitting
    ``deadline_budget_ms``.  Model teardown consuming that node-local fallback
    interval and the first nonblocking receipt observation losing a runtime or
    transport lock to a sibling cancellation.  The ordered worker must retain
    enough of the transmitted budget to retry receipt sealing; otherwise the
    gateway sees ``command_cleanup_receipt_missing`` even though teardown is
    complete.
    """

    class Clock:
        now = 10.0
        sleeps: list[float] = []

        @classmethod
        def monotonic(cls) -> float:
            return cls.now

        @classmethod
        def sleep(cls, seconds: float) -> None:
            cls.sleeps.append(seconds)
            cls.now += seconds

    request_id = "request-contended-receipt-seal"
    payload = {
        "request_id": request_id,
        "path_id": "path-contended-receipt-seal",
        "deadline_budget_ms": 1_500,
    }
    receipt = {
        "request_id": request_id,
        "path_id": payload["path_id"],
        "runtime_clean": True,
        "transport_clean": True,
        "cancellation_worker_complete": True,
        "complete": True,
    }
    service = object.__new__(PhysicalNodeService)
    service.runtime = object()
    service._control_lock = threading.RLock()
    service._request_cleanup_receipts = {}
    snapshot_count = 0

    def cancel(_payload, *, defer_cleanup):
        assert defer_cleanup is False
        # Generation fencing and exact teardown consume the worker's first
        # 1,000 ms while sibling requests contend for the same physical node.
        Clock.now += 1.0
        return {
            "event": "inference_cancelled",
            "details": {
                "request_id": request_id,
                "status": "CANCELLING",
                "cleanup_pending": True,
            },
        }

    def snapshot(_payload):
        nonlocal snapshot_count
        snapshot_count += 1
        if snapshot_count == 1:
            return {
                "event": "snapshot",
                "details": {"request_cleanup": {**receipt, "complete": False}},
            }
        service._request_cleanup_receipts[request_id] = dict(receipt)
        return {
            "event": "snapshot",
            "details": {"request_cleanup": dict(receipt)},
        }

    service._infer_cancel = cancel
    service._snapshot = snapshot
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    monkeypatch.setattr(node_module, "time", Clock)

    result = service._infer_cancel_wait(payload)

    assert snapshot_count == 2
    assert Clock.sleeps == [node_module._CANCEL_RECEIPT_POLL_SECONDS]
    assert result["details"]["cleanup_pending"] is False
    assert result["details"]["request_cleanup"] == receipt


def test_cancel_wait_does_not_consume_parent_deadline_with_inline_poll(
    monkeypatch,
) -> None:
    class Clock:
        now = 10.0
        sleeps: list[float] = []

        @classmethod
        def monotonic(cls) -> float:
            return cls.now

        @classmethod
        def sleep(cls, seconds: float) -> None:
            cls.sleeps.append(seconds)
            cls.now += seconds

    service = object.__new__(PhysicalNodeService)
    service._infer_cancel = lambda _payload, **_kwargs: {
        "event": "inference_cancelled",
        "details": {"status": "CANCELLING", "cleanup_pending": True},
    }
    service._snapshot = lambda payload: {
        "event": "snapshot",
        "details": {
            "request_cleanup": {
                **payload["cleanup_subject"],
                "complete": False,
            }
        },
    }
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    monkeypatch.setattr(node_module, "time", Clock)

    result = service._infer_cancel_wait(
        {
            "request_id": "request-inline-cap",
            "path_id": "path-inline-cap",
            "deadline_budget_ms": 2_000,
        }
    )

    assert result["details"]["cleanup_pending"] is True
    assert Clock.sleeps == []


def test_cancel_wait_control_applies_cleanup_in_reserved_control_worker() -> None:
    request_id = "request-inline-control-cleanup"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-inline-control-cleanup",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-inline-control-cleanup",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    payload = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }
    caller = threading.current_thread()
    applied_by: list[threading.Thread] = []
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.router = object()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_controls = {request_id: dict(control)}
    untyped_service._pending_cancellations = {}
    untyped_service._cancellation_controls = {}
    untyped_service._cancellation_workers = {}
    untyped_service._cancellation_worker_errors = {}

    def apply_inline(_data: dict[str, Any]) -> None:
        current = threading.current_thread()
        applied_by.append(current)
        assert service._cancellation_workers[request_id] is current

    untyped_service._apply_generation_fenced_cancellation = apply_inline
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    result = service._infer_cancel(payload, defer_cleanup=False)

    assert applied_by == [caller]
    assert service._cancellation_workers == {}
    assert result["details"]["status"] == "CANCELLING"
    assert result["details"]["cleanup_pending"] is True


def test_deferred_cancel_retries_partial_teardown_inside_original_deadline() -> None:
    request_id = "request-retry-partial-teardown"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-retry-partial-teardown",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-retry-partial-teardown",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    payload = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }
    attempts: list[int] = []
    sealed_after_worker_retired: list[float] = []
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.router = object()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_controls = {request_id: dict(control)}
    untyped_service._pending_cancellations = {}
    untyped_service._cancellation_controls = {}
    untyped_service._cancellation_workers = {}
    untyped_service._cancellation_worker_errors = {}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    def flaky_teardown(data: dict[str, Any]) -> None:
        attempts.append(data["deadline_budget_ms"])
        if len(attempts) == 1:
            raise RuntimeError("transient_resource_release")

    untyped_service._apply_generation_fenced_cancellation = flaky_teardown

    def seal_receipt(
        _data: dict[str, Any],
        *,
        deadline_monotonic_s: float,
    ) -> None:
        assert request_id not in service._cancellation_workers
        sealed_after_worker_retired.append(deadline_monotonic_s)

    untyped_service._seal_cancellation_receipt_until_deadline = seal_receipt
    result = service._infer_cancel(payload)

    deadline = time.monotonic() + 1.0
    while request_id in service._cancellation_workers:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    assert result["details"]["cleanup_pending"] is True
    assert len(attempts) == 2
    assert 1 <= attempts[1] < attempts[0]
    assert service._cancellation_worker_errors == {}
    assert len(sealed_after_worker_retired) == 1
    assert sealed_after_worker_retired[0] <= time.monotonic() + 2.0


def test_deferred_cancel_retries_async_teardown_until_resources_are_clean() -> None:
    """A successful sweep dispatch is not proof that its async work succeeded."""

    request_id = "request-retry-async-teardown"
    path_id = "path-retry-async-teardown"
    payload = {
        "request_id": request_id,
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-retry-async-teardown",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": path_id,
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 1,
        "deadline_budget_ms": 1_500,
    }

    class EventuallyCleanTransport:
        def __init__(self) -> None:
            self.attempts = 0
            self.clean = False

        def apply_controlled_path_cancellation(
            self,
            _cancellation: Any,
            *,
            entry_cancelled: bool,
            cleanup_deadline_monotonic_s: float,
        ) -> bool:
            assert entry_cancelled is False
            assert cleanup_deadline_monotonic_s > time.monotonic()
            self.attempts += 1
            # The first sweep successfully dispatches an asynchronous sidecar
            # cancellation, but that worker later leaves the subject retryable.
            self.clean = self.attempts >= 2
            return True

        def cancellation_cleanup_complete(
            self,
            observed_request_id: str,
            observed_path_id: str,
            observed_path_attempt: int,
        ) -> bool:
            assert observed_request_id == request_id
            assert observed_path_id == path_id
            assert observed_path_attempt == 1
            return self.clean

    class CleanRuntime:
        def __init__(self) -> None:
            self.cancelled_paths: list[str] = []

        def cancel(self, observed_path_id: str) -> None:
            self.cancelled_paths.append(observed_path_id)

        def kv_subject_clean(
            self,
            observed_request_id: str,
            observed_path_id: str,
            observed_path_attempt: int,
        ) -> bool:
            assert observed_request_id == request_id
            assert observed_path_id == path_id
            assert observed_path_attempt == 1
            return True

    class IdempotentRouter:
        def cancel_local(self, observed_request_id: str) -> bool:
            assert observed_request_id == request_id
            return True

    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    runtime = CleanRuntime()
    transport = EventuallyCleanTransport()
    untyped_service.runtime = runtime
    untyped_service.transport = transport
    untyped_service.router = IdempotentRouter()
    untyped_service._control_lock = threading.RLock()
    untyped_service._cancellation_worker_errors = {}

    service._apply_cancellation_until_deadline(payload)

    assert transport.attempts == 2
    assert transport.clean is True
    assert runtime.cancelled_paths == [path_id, path_id]
    assert service._cancellation_worker_errors == {}


def test_cancel_before_start_still_fences_registered_transport_path() -> None:
    """Data-plane registration may overtake generation-0 control binding."""

    request_id = "request-cancel-before-control-bind"
    path_id = "path-cancel-before-control-bind"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-cancel-before-control-bind",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": path_id,
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }
    transport_entry_registered = {request_id}
    cancellation_applied = threading.Event()

    class Router:
        def cancel_local(self, candidate: str) -> bool:
            assert candidate == request_id
            return False

    class Transport:
        def apply_controlled_path_cancellation(
            self,
            cancellation,
            *,
            entry_cancelled: bool,
            cleanup_deadline_monotonic_s: float | None,
        ) -> bool:
            assert cancellation.request_id == request_id
            assert cancellation.path_id == path_id
            assert cancellation.path_attempt == 1
            assert entry_cancelled is False
            assert cleanup_deadline_monotonic_s is not None
            transport_entry_registered.discard(cancellation.request_id)
            cancellation_applied.set()
            return True

    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    service.state = "RUNNING"
    service.router = Router()
    service.transport = Transport()
    service.runtime = SimpleNamespace(cancel=lambda candidate: candidate == path_id)
    service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    service._control_lock = threading.RLock()
    service._request_controls = {}
    service._pending_cancellations = {}
    service._cancellation_controls = {}
    service._cancellation_workers = {}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    result = service._infer_cancel({"request_id": request_id, **control})

    assert result["details"]["pending_start"] is True
    assert result["details"]["cleanup_pending"] is True
    assert cancellation_applied.wait(timeout=1.0)
    deadline = time.monotonic() + 1.0
    while request_id in service._cancellation_workers:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    assert transport_entry_registered == set()
    assert request_id in service._pending_cancellations


def test_inline_cancel_lifecycle_blocks_concurrent_cleanup_receipt() -> None:
    request_id = "request-inline-snapshot-race"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-inline-snapshot-race",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-inline-snapshot-race",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 0,
    }
    payload = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
        "deadline_budget_ms": 2_000,
    }
    cleanup_subject = {
        key: value for key, value in payload.items() if key != "deadline_budget_ms"
    }
    teardown_entered = threading.Event()
    release_teardown = threading.Event()
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.router = object()
    untyped_service.graph = SimpleNamespace(
        deployment_id="deployment-a",
        deployment_epoch=4,
        topology_version=7,
    )
    untyped_service.runtime = SimpleNamespace(
        kv_snapshot=lambda: {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "states": {},
            "active_state_count": 0,
            "active_kv_bytes": 0,
        }
    )
    untyped_service.transport = None
    untyped_service._last_cancellation = None
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_controls = {request_id: dict(control)}
    untyped_service._pending_cancellations = {}
    untyped_service._cancellation_controls = {}
    untyped_service._cancellation_workers = {}
    untyped_service._cancellation_worker_errors = {}
    untyped_service._cancellations_by_subject = set()
    untyped_service._request_cleanup_receipts = {}
    untyped_service._sinks = {}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    def apply_inline(_data: dict[str, Any]) -> None:
        teardown_entered.set()
        assert release_teardown.wait(timeout=1.0)

    untyped_service._apply_generation_fenced_cancellation = apply_inline
    cancel_thread = threading.Thread(
        target=service._infer_cancel,
        args=(payload,),
        kwargs={"defer_cleanup": False},
    )
    cancel_thread.start()
    assert teardown_entered.wait(timeout=1.0)

    racing = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )["details"]["request_cleanup"]
    assert racing["runtime_clean"] is True
    assert racing["transport_clean"] is True
    assert racing["cancellation_worker_complete"] is False
    assert racing["complete"] is False
    assert request_id not in service._request_cleanup_receipts

    release_teardown.set()
    cancel_thread.join(timeout=1.0)
    assert not cancel_thread.is_alive()
    service._cancellation_worker_errors[request_id] = (
        "RuntimeError:transient_resource_release"
    )
    failed = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )["details"]["request_cleanup"]
    assert failed["cancellation_worker_complete"] is False
    assert failed["cancellation_worker_error"] == (
        "RuntimeError:transient_resource_release"
    )
    assert failed["complete"] is False
    service._cancellation_worker_errors.pop(request_id)
    settled = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )["details"]["request_cleanup"]
    assert settled["cancellation_worker_complete"] is True
    assert settled["complete"] is True


def test_generation_fenced_cancel_fences_transport_before_router_teardown() -> None:
    runtime_marked = threading.Event()
    transport_fenced = threading.Event()
    operation_order: list[str] = []

    class Runtime:
        def cancel(self, path_id: str) -> None:
            assert path_id == "path-a"
            operation_order.append("runtime")
            runtime_marked.set()

    class Router:
        def cancel_local(self, request_id: str) -> bool:
            assert request_id == "request-a"
            assert runtime_marked.is_set()
            # A path operation can be blocked in a transport send while it
            # holds the relay's per-path lock.  The exact transport fence must
            # interrupt that send before Router teardown waits for the lock.
            assert transport_fenced.is_set()
            operation_order.append("router")
            return True

    class Transport:
        def apply_controlled_path_cancellation(
            self,
            cancellation,
            *,
            entry_cancelled: bool,
            cleanup_deadline_monotonic_s: float | None,
        ) -> bool:
            assert cancellation.path_id == "path-a"
            assert runtime_marked.is_set()
            assert entry_cancelled is False
            assert cleanup_deadline_monotonic_s is None
            operation_order.append("transport")
            transport_fenced.set()
            return True

    service = object.__new__(PhysicalNodeService)
    service.runtime = Runtime()
    service.router = Router()
    service.transport = Transport()

    service._apply_generation_fenced_cancellation(
        {
            "request_id": "request-a",
            "path_id": "path-a",
            "path_attempt": 0,
            "topology_generation": 1,
        }
    )

    assert runtime_marked.is_set()
    assert operation_order == ["runtime", "transport", "router"]


def test_generation_fenced_cancel_forwards_aged_owner_cleanup_deadline() -> None:
    observed_deadline: list[float | None] = []

    class Router:
        def cancel_local(self, _request_id: str) -> bool:
            return True

    class Transport:
        def apply_controlled_path_cancellation(
            self,
            _cancellation,
            *,
            entry_cancelled: bool,
            cleanup_deadline_monotonic_s: float | None,
        ) -> bool:
            assert entry_cancelled is False
            observed_deadline.append(cleanup_deadline_monotonic_s)
            return True

    service = object.__new__(PhysicalNodeService)
    service.runtime = SimpleNamespace(cancel=lambda _path_id: None)
    service.router = Router()
    service.transport = Transport()
    started_at = time.monotonic()
    service._apply_generation_fenced_cancellation(
        {
            "request_id": "request-owner-deadline",
            "path_id": "path-owner-deadline",
            "path_attempt": 0,
            "topology_generation": 1,
            "deadline_budget_ms": 1_500,
        }
    )

    assert len(observed_deadline) == 1
    assert observed_deadline[0] is not None
    assert started_at + 0.95 <= observed_deadline[0] <= time.monotonic() + 1.0


def test_cancel_observation_survives_router_retiring_exact_request() -> None:
    request_id = "request-retired-during-cancel"
    cancellation_entered = threading.Event()
    release_cancellation = threading.Event()

    class Router:
        def cancel(self, candidate: str) -> bool:
            assert candidate == request_id
            cancellation_entered.set()
            assert release_cancellation.wait(timeout=1.0)
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
        "status": "CANCELLING",
        "cleanup_pending": True,
    }
    assert cancellation_entered.wait(timeout=1.0)
    # The signed generation acknowledgment is not serialized behind teardown.
    assert request_id in service._cancellation_workers
    release_cancellation.set()
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
        cancellation_applied = threading.Event()

        def send_path_cancellation_if_entry(self, cancellation) -> bool:
            self.cancellation = cancellation
            self.cancellation_applied.set()
            return True

        def counter_snapshot(self) -> dict[str, int]:
            return {
                "remote_frames_sent": 5,
                "remote_frames_received": 5,
                "router_frames_dispatched": 5,
                "duplicate_frames": 0,
            }

        def cancellation_cleanup_complete(self, *_identity) -> bool:
            return True

        def cancellation_cleanup_state(self, *_identity) -> dict[str, Any]:
            return {
                "pending_delivery_count": 0,
                "inflight_received_count": 0,
                "forward_count": 0,
                "path_graph_registered": False,
                "participants_registered": False,
                "entry_registered": False,
                "cancellation_worker_active": False,
                "cancellation_observed": True,
            }

        def cancellation_observed(self, *_identity) -> bool:
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
    service._cancellations_by_subject = {}
    service._request_cleanup_receipts = {}
    service._sinks = {}
    untyped_service._last_cancellation = None
    untyped_service.runtime = SimpleNamespace(
        kv_snapshot=lambda: {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "states": {},
            "active_state_count": 0,
            "active_kv_bytes": 0,
        }
    )
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

    assert result["details"]["cancelled"] is True
    assert result["details"]["status"] == "CANCELLING"
    assert service.transport.cancellation_applied.wait(timeout=1.0)
    cancellation = service.transport.cancellation
    assert cancellation.request_id == request_id
    assert cancellation.path_id == control["path_id"]
    assert cancellation.path_attempt == control["path_attempt"]
    deadline = time.monotonic() + 1.0
    while request_id in service._cancellation_workers:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    snapshot = service._snapshot(
        {
            "cleanup_subject": {
                key: value
                for key, value in payload.items()
                if key != "deadline_budget_ms"
            },
            "receipt_only": True,
        }
    )
    assert snapshot["details"]["request_cleanup"]["complete"] is True
    assert snapshot["details"]["transport_counters"]["remote_frames_sent"] == 5
    assert request_id not in service._request_controls
    assert request_id in service._request_cleanup_receipts


def test_completed_entry_uses_controlled_teardown_without_relay_fallback() -> None:
    request_id = "request-completed-controlled-entry"

    class Router:
        def cancel_local(self, candidate: str) -> bool:
            assert candidate == request_id
            return False

    class Transport:
        controlled = None
        fallback_called = False

        def apply_controlled_path_cancellation(
            self,
            cancellation,
            *,
            entry_cancelled: bool,
            cleanup_deadline_monotonic_s: float | None,
        ) -> bool:
            assert entry_cancelled is False
            assert cleanup_deadline_monotonic_s is None
            self.controlled = cancellation
            return True

        def send_path_cancellation_if_entry(self, _cancellation) -> bool:
            self.fallback_called = True
            return True

    service = object.__new__(PhysicalNodeService)
    service.runtime = SimpleNamespace(cancel=lambda _path_id: None)
    service.router = Router()
    service.transport = Transport()
    service._apply_generation_fenced_cancellation(
        {
            "request_id": request_id,
            "path_id": "path-completed-controlled-entry",
            "path_attempt": 1,
            "topology_generation": 7,
        }
    )

    assert service.transport.controlled is not None
    assert service.transport.controlled.request_id == request_id
    assert service.transport.fallback_called is False


def test_legacy_terminal_decode_waits_for_exact_transport_retirement() -> None:
    request_id = "legacy-terminal-request"
    cleanup_checks = iter((False, True))
    cancellation = None

    class Router:
        def request_status(self, candidate: str) -> str:
            assert candidate == request_id
            return "COMPLETED"

        def decode_one_distributed(self, candidate: str) -> bool:
            assert candidate == request_id
            return False

        def get_request(self, candidate: str):
            assert candidate == request_id
            return SimpleNamespace(
                status="COMPLETED",
                manifest=SimpleNamespace(
                    path_id="path-legacy-terminal",
                    path_attempt=2,
                    topology_version=7,
                ),
            )

    class Transport:
        fatal_error = None

        def send_path_cancellation_if_entry(self, candidate) -> bool:
            nonlocal cancellation
            cancellation = candidate
            return True

        def cancellation_cleanup_complete(
            self,
            candidate_request: str,
            candidate_path: str,
            candidate_attempt: int,
        ) -> bool:
            assert candidate_request == request_id
            assert candidate_path == "path-legacy-terminal"
            assert candidate_attempt == 2
            return next(cleanup_checks)

    service = object.__new__(PhysicalNodeService)
    service.state = "RUNNING"
    service.router = Router()
    service.transport = Transport()
    service.command_timeout = 5.0
    service._sinks = {
        request_id: SimpleNamespace(
            token_ids=[11, 12],
            snapshot=lambda: {
                "token_indexes": [0, 1],
                "token_ids": [11, 12],
            },
        )
    }
    service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    result = service._infer_decode({"request_id": request_id, "count": 1})

    assert result["details"]["status"] == "COMPLETED"
    assert cancellation is not None
    assert cancellation.request_id == request_id
    assert cancellation.path_id == "path-legacy-terminal"
    assert cancellation.path_attempt == 2


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
    service._request_controls = {request_id: {**control, "cancellation_generation": 1}}
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
    untyped_service._apply_generation_fenced_cancellation = lambda _data: None
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
        key: value for key, value in cancellation.items() if key != "deadline_budget_ms"
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
        key: value for key, value in cancellation.items() if key != "deadline_budget_ms"
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
    untyped_service.capacity = SimpleNamespace(
        snapshot=lambda: (_ for _ in ()).throw(
            AssertionError("cleanup snapshot queried capacity")
        )
    )
    untyped_service.transport = None
    untyped_service.sidecar = SimpleNamespace(
        status=lambda: (_ for _ in ()).throw(
            AssertionError("cleanup snapshot queried sidecar")
        )
    )
    untyped_service._host_resources = lambda: (_ for _ in ()).throw(
        AssertionError("cleanup snapshot queried host resources")
    )
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    cancelled = service._infer_cancel(cancellation)
    assert cancelled["details"]["pending_start"] is True
    deadline = time.monotonic() + 1.0
    while request_id in service._cancellation_workers:
        assert time.monotonic() < deadline
        time.sleep(0.001)

    snapshot = service._snapshot(
        {
            "cleanup_subject": dict(cleanup_subject),
            "receipt_only": True,
        }
    )
    receipt = snapshot["details"]["request_cleanup"]
    assert receipt["complete"] is True
    assert request_id in service._request_cleanup_receipts
    # The receipt path retires the seeded control and the pending
    # cancellation: nothing request-scoped remains on the node.
    assert request_id not in service._request_controls
    assert request_id not in service._pending_cancellations
    assert request_id not in service._cancellation_controls

    late_bind = service._bind_request_control(
        {"request_id": request_id, "control": control}
    )
    assert late_bind["event"] == "request_control_bound"
    assert request_id not in service._request_controls
    assert request_id not in service._pending_cancellations
    assert request_id not in service._cancellation_controls


def test_receipt_only_snapshot_signs_transport_counters_without_full_evidence() -> None:
    cleanup_subject = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "request_id": "request-counter-snapshot",
        "request_attempt": 1,
        "path_id": "path-counter-snapshot",
        "path_attempt": 0,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "command_id": "command-counter-snapshot",
        "cancellation_generation": 1,
        "publisher_generation": 1,
        "absolute_deadline_ms": 999_999_999,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.runtime = SimpleNamespace(
        kv_snapshot=lambda: {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "states": {},
            "active_state_count": 0,
            "active_kv_bytes": 0,
        }
    )
    untyped_service._last_cancellation = None
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_cleanup_receipts = {
        cleanup_subject["request_id"]: {
            **cleanup_subject,
            "runtime_clean": True,
            "transport_clean": True,
            "complete": True,
        }
    }
    untyped_service.transport = SimpleNamespace(
        counter_snapshot=lambda: {
            "remote_frames_sent": 7,
            "remote_frames_received": 8,
            "router_frames_dispatched": 9,
            "duplicate_frames": 0,
        },
        evidence=lambda: (_ for _ in ()).throw(
            AssertionError("lean receipt queried full transport evidence")
        ),
    )
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    snapshot = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )

    assert snapshot["details"]["transport_counters"] == {
        "remote_frames_sent": 7,
        "remote_frames_received": 8,
        "router_frames_dispatched": 9,
        "duplicate_frames": 0,
    }


def test_concurrent_cleanup_snapshots_only_project_monotonic_complete_receipt() -> None:
    request_id = "request-concurrent-cleanup"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "command_id": "command-concurrent-cleanup",
        "publisher_generation": 3,
        "absolute_deadline_ms": int(time.monotonic() * 1_000) + 60_000,
        "request_attempt": 2,
        "path_id": "path-concurrent-cleanup",
        "path_attempt": 1,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "cancellation_generation": 1,
    }
    cleanup_subject = {"request_id": request_id, **control}
    control_probe_barrier = threading.Barrier(2)
    transport_snapshot_lock = threading.Lock()
    transport_snapshot_count = 0

    class Transport:
        def counter_snapshot(self) -> dict[str, int]:
            return {
                "remote_frames_sent": 1,
                "remote_frames_received": 1,
                "router_frames_dispatched": 1,
                "duplicate_frames": 0,
            }

        def cancellation_cleanup_complete(self, *_identity) -> bool:
            raise AssertionError("cleanup receipt used a second transport probe")

        def cancellation_cleanup_state(self, *_identity) -> dict[str, Any]:
            nonlocal transport_snapshot_count
            with transport_snapshot_lock:
                transport_snapshot_count += 1
            return {
                "pending_delivery_count": 0,
                "inflight_received_count": 0,
                "forward_count": 0,
                "path_graph_registered": False,
                "participants_registered": False,
                "entry_registered": False,
                "cancellation_worker_active": False,
                "cancellation_observed": True,
            }

        def cancellation_observed(self, *_identity) -> bool:
            # Both observers have already passed the initial stored-receipt
            # check and read cancellation control before either can commit.
            control_probe_barrier.wait(timeout=1.0)
            return True

    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.runtime = SimpleNamespace(
        decode_mode="stage_local_kv",
        kv_subject_clean=lambda *_identity: True,
        kv_snapshot_nonblocking=lambda: {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "states": {},
            "active_state_count": 0,
            "active_kv_bytes": 0,
        },
    )
    untyped_service.transport = Transport()
    untyped_service._last_cancellation = None
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_controls = {request_id: dict(control)}
    untyped_service._pending_cancellations = {}
    untyped_service._cancellation_controls = {request_id: dict(control)}
    untyped_service._cancellation_workers = {}
    untyped_service._cancellations_by_subject = {}
    untyped_service._request_cleanup_receipts = {}
    untyped_service._sinks = {request_id: object()}
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }
    results: list[dict[str, Any]] = []

    def snapshot() -> None:
        results.append(
            service._snapshot(
                {
                    "cleanup_subject": cleanup_subject,
                    "receipt_only": True,
                }
            )
        )

    workers = [threading.Thread(target=snapshot) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2.0)

    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2
    assert all(
        result["details"]["request_cleanup"]["complete"] is True for result in results
    )
    assert transport_snapshot_count == 2
    assert service._request_cleanup_receipts[request_id]["complete"] is True


def test_receipt_snapshot_returns_pending_without_waiting_for_runtime() -> None:
    cleanup_subject = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "request_id": "request-runtime-busy",
        "request_attempt": 1,
        "path_id": "path-runtime-busy",
        "path_attempt": 0,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "command_id": "command-runtime-busy",
        "cancellation_generation": 1,
        "publisher_generation": 1,
        "absolute_deadline_ms": 999_999_999,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.runtime = SimpleNamespace(
        decode_mode="stage_local_kv",
        kv_snapshot_nonblocking=lambda: None,
        kv_snapshot=lambda: (_ for _ in ()).throw(
            AssertionError("receipt snapshot waited for runtime")
        ),
    )
    untyped_service._last_cancellation = None
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_cleanup_receipts = {}
    untyped_service.transport = SimpleNamespace(
        counter_snapshot=lambda: {
            "remote_frames_sent": 1,
            "remote_frames_received": 1,
            "router_frames_dispatched": 1,
            "duplicate_frames": 0,
        },
        cancellation_cleanup_state=lambda *_identity: {
            "pending_delivery_count": 0,
            "inflight_received_count": 0,
            "forward_count": 0,
            "path_graph_registered": False,
            "participants_registered": False,
            "entry_registered": False,
            "cancellation_worker_active": False,
            "cancellation_observed": True,
        },
    )
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    snapshot = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )

    assert snapshot["details"]["runtime_cleanup_observation_complete"] is False
    assert snapshot["details"]["request_cleanup"]["runtime_clean"] is False
    assert snapshot["details"]["request_cleanup"]["complete"] is False


def test_receipt_snapshot_returns_pending_without_waiting_for_transport() -> None:
    cleanup_subject = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "request_id": "request-transport-busy",
        "request_attempt": 1,
        "path_id": "path-transport-busy",
        "path_attempt": 0,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "command_id": "command-transport-busy",
        "cancellation_generation": 1,
        "publisher_generation": 1,
        "absolute_deadline_ms": 999_999_999,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.runtime = SimpleNamespace(
        decode_mode="stage_local_kv",
        kv_subject_clean=lambda *_identity: True,
        kv_snapshot_nonblocking=lambda: {
            "backend": "numpy",
            "mode": "stage_local_kv",
            "states": {},
            "active_state_count": 0,
            "active_kv_bytes": 0,
        },
    )
    untyped_service._last_cancellation = None
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_cleanup_receipts = {}
    untyped_service._request_controls = {
        cleanup_subject["request_id"]: {
            key: value for key, value in cleanup_subject.items() if key != "request_id"
        }
    }
    untyped_service._pending_cancellations = {}
    untyped_service._cancellation_controls = {}
    untyped_service._cancellation_workers = {}
    untyped_service._cancellations_by_subject = {}
    untyped_service._sinks = {cleanup_subject["request_id"]: object()}
    untyped_service.transport = SimpleNamespace(
        cancellation_cleanup_observation_nonblocking=lambda *_identity: None,
        counter_snapshot=lambda: (_ for _ in ()).throw(
            AssertionError("receipt snapshot waited for transport counters")
        ),
        cancellation_cleanup_state=lambda *_identity: (_ for _ in ()).throw(
            AssertionError("receipt snapshot waited for transport state")
        ),
    )
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    snapshot = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )

    assert snapshot["details"]["transport_cleanup_observation_complete"] is False
    assert snapshot["details"]["request_cleanup"]["transport_clean"] is False
    assert snapshot["details"]["request_cleanup"]["complete"] is False


def test_clean_resources_remain_pending_until_cancellation_generation_is_proven() -> (
    None
):
    request_id = "request-clean-before-cancel-control"
    control = {
        "deployment_id": "deployment-a",
        "deployment_epoch": 4,
        "qualification_digest": "sha256:" + "a" * 64,
        "request_attempt": 1,
        "path_id": "path-clean-before-cancel-control",
        "path_attempt": 0,
        "path_digest": "sha256:" + "b" * 64,
        "topology_generation": 7,
        "command_id": "command-clean-before-cancel-control",
        "cancellation_generation": 0,
        "publisher_generation": 1,
        "absolute_deadline_ms": 999_999_999,
    }
    cleanup_subject = {
        "request_id": request_id,
        **control,
        "cancellation_generation": 1,
    }
    service = object.__new__(PhysicalNodeService)
    untyped_service = cast(Any, service)
    untyped_service.state = "RUNNING"
    untyped_service.runtime = SimpleNamespace(
        decode_mode="stage_local_kv",
        kv_snapshot_nonblocking=lambda: None,
        kv_subject_clean=lambda observed_request_id, path_id, path_attempt: (
            observed_request_id == request_id
            and path_id == cleanup_subject["path_id"]
            and path_attempt == cleanup_subject["path_attempt"]
        ),
    )
    untyped_service._last_cancellation = None
    untyped_service._control_lock = threading.RLock()
    untyped_service._request_controls = {request_id: dict(control)}
    untyped_service._pending_cancellations = {}
    untyped_service._cancellation_controls = {}
    untyped_service._cancellation_workers = {}
    untyped_service._cancellations_by_subject = {}
    untyped_service._request_cleanup_receipts = {}
    untyped_service._sinks = {request_id: object()}
    untyped_service.transport = None
    untyped_service._signed_result = lambda event, details: {
        "event": event,
        "details": details,
    }

    snapshot = service._snapshot(
        {"cleanup_subject": cleanup_subject, "receipt_only": True}
    )

    assert snapshot["details"]["runtime_cleanup_observation_complete"] is False
    assert snapshot["details"]["request_cleanup"]["runtime_clean"] is True
    assert snapshot["details"]["request_cleanup"]["complete"] is False
    assert service._request_controls[request_id]["cancellation_generation"] == 0
    assert service._request_cleanup_receipts == {}
    assert request_id in service._sinks
