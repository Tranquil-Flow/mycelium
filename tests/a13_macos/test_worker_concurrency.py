# SPDX-License-Identifier: AGPL-3.0-only
"""Managed-worker concurrency/fault controls; fakes never execute a model.

The one real-native case is explicitly labelled and exercises only hello plus a
semantic rejection before configure/load.  All overlap, deadline, and grant
faults use an in-memory session double.
"""
from __future__ import annotations

import threading
import time
from typing import Any, Mapping

import pytest

from mycelium_node.process import NodeProcessError
from mycelium_a13_macos.worker import WorkerError, WorkerLimits
from test_worker import worker


class FakeConcurrentSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.deadlines: list[float] = []
        self.interrupts: list[tuple[str, str]] = []
        self.returncode: int | None = None
        self.block_command: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()
        self.responses: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _success(command_id: str, command: str) -> dict[str, Any]:
        return {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": command_id,
            "node_id": "mac-test",
            "ok": True,
            "route_ready": False,
            "result": {"command": command},
        }

    def send(
        self, *, command_id: str, command: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((command_id, command, dict(payload)))
        if command == self.block_command:
            self.entered.set()
            if not self.release.wait(2.0):
                raise AssertionError("fake concurrency command was not released")
        return self.responses.get(command_id, self._success(command_id, command))

    def send_before(
        self,
        *,
        command_id: str,
        command: str,
        payload: Mapping[str, Any],
        deadline_monotonic_s: float,
    ) -> dict[str, Any]:
        self.deadlines.append(deadline_monotonic_s)
        if deadline_monotonic_s <= time.monotonic():
            raise TimeoutError("command_deadline_exceeded")
        return self.send(command_id=command_id, command=command, payload=payload)

    def interrupt_command(self, command_id: str, *, code: str) -> bool:
        self.interrupts.append((command_id, code))
        return True

    def close(self) -> None:
        self.returncode = 0
        self.release.set()


def attach_fake(w, session: FakeConcurrentSession, *, load_admitted: bool = False) -> None:
    w._session = session
    w._started_at = time.monotonic()
    w._load_admitted = load_admitted


def configure_payload() -> dict[str, Any]:
    return {
        "assignment_file": "assignment.json",
        "artifact_report_file": "artifact.json",
        "graph": {},
        "device_states": {},
        "load_generation": 1,
    }


def cancel_control_payload() -> dict[str, Any]:
    return {
        "request_id": "request-1",
        "deployment_id": "deployment-test",
        "deployment_epoch": 1,
        "qualification_digest": "sha256:" + "1" * 64,
        "command_id": "owner-command-1",
        "publisher_generation": 1,
        "absolute_deadline_ms": 1,
        "request_attempt": 1,
        "path_id": "path-1",
        "path_attempt": 0,
        "path_digest": "sha256:" + "2" * 64,
        "topology_generation": 1,
        "cancellation_generation": 1,
        "deadline_budget_ms": 100,
    }


def test_fake_fault_cleanup_overtakes_blocked_inference(tmp_path) -> None:
    session = FakeConcurrentSession()
    session.block_command = "infer_start"
    w = worker(tmp_path, authorize_load=lambda _envelope: True)
    attach_fake(w, session, load_admitted=True)
    outcome: dict[str, dict[str, Any]] = {}

    def infer() -> None:
        outcome["infer"] = w.send(
            command_id="infer-blocked",
            command="infer_start",
            payload={"request": {}},
        )

    thread = threading.Thread(target=infer, name="fake-blocked-inference")
    try:
        thread.start()
        assert session.entered.wait(1.0)
        cleanup = w.send_before(
            command_id="cleanup-priority",
            command="infer_cancel_wait",
            payload=cancel_control_payload(),
            deadline_monotonic_s=time.monotonic() + 0.5,
        )
        assert cleanup["ok"] is True
        assert thread.is_alive(), "cleanup waited behind the blocked inference"
        assert [call[1] for call in session.calls] == ["infer_start", "infer_cancel_wait"]
    finally:
        session.release.set()
        thread.join(timeout=2.0)
        w.close()
    assert not thread.is_alive()
    assert outcome["infer"]["ok"] is True


def test_fake_fault_command_accounting_is_atomic_without_serializing_request(tmp_path) -> None:
    session = FakeConcurrentSession()
    session.block_command = "hello"
    w = worker(
        tmp_path,
        limits=WorkerLimits(
            max_rss_bytes=2_000_000_000,
            max_disk_bytes=10_000_000,
            max_wall_seconds=30,
            max_commands=1,
        ),
    )
    attach_fake(w, session)
    first = threading.Thread(
        target=lambda: w.send(command_id="first", command="hello", payload={})
    )
    try:
        first.start()
        assert session.entered.wait(1.0)
        with pytest.raises(WorkerError, match="worker_command_limit_exceeded"):
            w.send(command_id="second", command="hello", payload={})
        assert w.status()["command_count"] == 1
        assert len(session.calls) == 1
    finally:
        session.release.set()
        first.join(timeout=2.0)
        w.close()


@pytest.mark.parametrize(
    ("command", "payload"),
    (
        ("infer_start", {"request": {}}),
        ("infer_decode", {"request_id": "request-1", "count": 1}),
        (
            "rotate",
            {
                "peer": {
                    "node_id": "peer-1",
                    "endpoint_id": "endpoint-1",
                    "endpoint_addr": "addr-1",
                    "generation": 1,
                }
            },
        ),
    ),
)
def test_fake_fault_inference_and_rotation_require_admitted_load(
    tmp_path, command: str, payload: dict[str, Any]
) -> None:
    session = FakeConcurrentSession()
    w = worker(tmp_path, authorize_load=lambda _envelope: True)
    attach_fake(w, session)
    try:
        with pytest.raises(WorkerError, match="model_load_not_admitted"):
            w.send(command_id=f"blocked-{command}", command=command, payload=payload)
        assert session.calls == []
        assert w.status()["command_count"] == 0
    finally:
        w.close()


def test_fake_fault_exact_grant_refusal_never_becomes_native_failure(tmp_path) -> None:
    session = FakeConcurrentSession()
    observed: list[dict[str, Any]] = []

    def refuse(envelope: Mapping[str, Any]) -> bool:
        observed.append(dict(envelope))
        return False

    w = worker(tmp_path, authorize_load=refuse)
    attach_fake(w, session, load_admitted=True)
    try:
        with pytest.raises(WorkerError, match="model_execution_not_authorized"):
            w.send(
                command_id="grant-refused",
                command="infer_decode",
                payload={"request_id": "request-1", "count": 1},
            )
        assert observed == [
            {
                "protocol": "mycelium.physical_node_control.v1",
                "command_id": "grant-refused",
                "run_id": "run-test",
                "deployment_id": "deployment-test",
                "command": "infer_decode",
                "payload": {"request_id": "request-1", "count": 1},
            }
        ]
        assert session.calls == []
        assert w.status()["command_count"] == 0
    finally:
        w.close()


def test_fake_fault_load_is_admitted_only_after_genuine_success(tmp_path) -> None:
    session = FakeConcurrentSession()
    session.responses["rejected-configure"] = {
        "protocol": "mycelium.physical_node_control.v1",
        "command_id": "rejected-configure",
        "node_id": "mac-test",
        "ok": False,
        "route_ready": False,
        "error": {"code": "assignment_rejected"},
    }
    w = worker(tmp_path, authorize_load=lambda _envelope: True)
    attach_fake(w, session)
    try:
        rejected = w.send(
            command_id="rejected-configure",
            command="configure",
            payload=configure_payload(),
        )
        assert rejected["error"] == {"code": "assignment_rejected"}
        assert w.status()["model_load_authorized"] is False
        with pytest.raises(WorkerError, match="model_load_not_admitted"):
            w.send(
                command_id="premature-start",
                command="start",
                payload={"peer": {}, "local_generation": 1},
            )
        accepted = w.send(
            command_id="accepted-configure",
            command="configure",
            payload=configure_payload(),
        )
        assert accepted["ok"] is True
        assert w.status()["model_load_authorized"] is True
    finally:
        w.close()


def test_fake_fault_required_live_route_operations_are_closed_and_supported(tmp_path) -> None:
    session = FakeConcurrentSession()
    w = worker(tmp_path, authorize_load=lambda _envelope: True)
    attach_fake(w, session, load_admitted=True)
    control = {key: value for key, value in cancel_control_payload().items() if key not in {"request_id", "deadline_budget_ms"}}
    operations = (
        ("bind_request_control", {"request_id": "request-1", "control": control}),
        ("update_request_control", {"request_id": "request-1", "control": control}),
        ("infer_start", {"request": {}, "control": control, "path_manifest": {}}),
        ("infer_decode", {"request_id": "request-1", "count": 1, "control": control}),
        ("infer_cancel", cancel_control_payload()),
        ("infer_cancel_wait", cancel_control_payload()),
        (
            "rotate",
            {
                "peer": {
                    "node_id": "peer-1",
                    "endpoint_id": "endpoint-1",
                    "endpoint_addr": "addr-1",
                    "generation": 1,
                }
            },
        ),
    )
    try:
        for index, (command, payload) in enumerate(operations):
            response = w.send(
                command_id=f"supported-{index}", command=command, payload=payload
            )
            assert response["ok"] is True
        assert [call[1] for call in session.calls] == [item[0] for item in operations]
    finally:
        w.close()


def test_fake_fault_arbitrary_execution_input_is_rejected_before_native(tmp_path) -> None:
    session = FakeConcurrentSession()
    w = worker(tmp_path, authorize_load=lambda _envelope: True)
    attach_fake(w, session, load_admitted=True)
    try:
        with pytest.raises(WorkerError, match="worker_command_invalid"):
            w.send(
                command_id="nested-argv",
                command="infer_start",
                payload={"request": {"argv": ["/usr/bin/true"]}},
            )
        assert session.calls == []
        assert w.status()["command_count"] == 0
    finally:
        w.close()


def test_fake_fault_absolute_deadline_is_forwarded_not_restarted(tmp_path) -> None:
    session = FakeConcurrentSession()
    w = worker(tmp_path)
    attach_fake(w, session)
    deadline = time.monotonic() + 0.5
    try:
        response = w.send_before(
            command_id="deadline-health",
            command="health",
            payload={},
            deadline_monotonic_s=deadline,
        )
        assert response["ok"] is True
        assert session.deadlines == [deadline]
    finally:
        w.close()


def test_fake_fault_expired_during_grant_never_reaches_native(tmp_path) -> None:
    session = FakeConcurrentSession()

    def slow_grant(_envelope: Mapping[str, Any]) -> bool:
        time.sleep(0.04)
        return True

    w = worker(tmp_path, authorize_load=slow_grant)
    attach_fake(w, session, load_admitted=True)
    try:
        with pytest.raises(TimeoutError, match="command_deadline_exceeded"):
            w.send_before(
                command_id="deadline-expired",
                command="infer_decode",
                payload={"request_id": "request-1", "count": 1},
                deadline_monotonic_s=time.monotonic() + 0.01,
            )
        assert session.calls == []
        assert w.status()["command_count"] == 0
    finally:
        w.close()


def test_fake_fault_deadline_bounds_guard_lock_admission(tmp_path) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_observer() -> dict[str, str]:
        entered.set()
        release.wait(2.0)
        return {"power": "external", "network": "unmetered", "thermal": "nominal"}

    session = FakeConcurrentSession()
    w = worker(tmp_path, _test_observer=blocked_observer)
    attach_fake(w, session)
    guard = threading.Thread(target=w._check, name="fake-blocked-worker-guard")
    try:
        guard.start()
        assert entered.wait(1.0)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match="command_deadline_exceeded"):
            w.send_before(
                command_id="guard-deadline",
                command="health",
                payload={},
                deadline_monotonic_s=started + 0.03,
            )
        assert time.monotonic() - started < 0.2
        assert session.calls == []
        assert w.status()["command_count"] == 0
    finally:
        release.set()
        guard.join(timeout=2.0)
        w.close()


def test_fake_fault_transport_error_is_not_synthesized_as_native_failure(tmp_path) -> None:
    class TransportFailureSession(FakeConcurrentSession):
        def send(
            self, *, command_id: str, command: str, payload: Mapping[str, Any]
        ) -> dict[str, Any]:
            raise NodeProcessError("node_response_timeout")

    session = TransportFailureSession()
    w = worker(tmp_path)
    attach_fake(w, session)
    try:
        with pytest.raises(NodeProcessError) as caught:
            w.send(command_id="transport-timeout", command="health", payload={})
        assert caught.value.code == "node_response_timeout"
        assert w.status()["running"] is True
        assert w.status()["command_count"] == 1
    finally:
        w.close()


def test_fake_fault_interrupt_is_request_scoped_and_delegated(tmp_path) -> None:
    session = FakeConcurrentSession()
    w = worker(tmp_path)
    attach_fake(w, session)
    try:
        assert w.interrupt_command("request-1-infer", code="request_cancelled") is True
        assert session.interrupts == [("request-1-infer", "request_cancelled")]
        assert w.status()["command_count"] == 0
    finally:
        w.close()


def test_real_native_semantic_error_is_preserved_without_model_load(tmp_path) -> None:
    w = worker(tmp_path)
    try:
        w.start()
        response = w.send(
            command_id="real-native-cancel-rejection",
            command="cancel",
            payload={"request_id": "never-started"},
        )
        assert response == {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": "real-native-cancel-rejection",
            "node_id": "mac-test",
            "ok": False,
            "route_ready": False,
            "error": {"code": "invalid_state_for_cancel"},
        }
        assert w.status()["running"] is True
        assert w.status()["model_load_authorized"] is False
    finally:
        w.close()
