# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: PhysicalNodeService wiring - idle commands, health detail, work gate, fail-closed reload."""
from __future__ import annotations

import threading

import pytest

from mycelium_idle import node_link
from mycelium_idle import policy as ip
from physical_inference_node import NodeCommandError, PhysicalNodeService


class FakeRuntime:
    def __init__(self):
        self.resident = True
        self.calls = []
        self.requests = 0
        self.ops = 0
        self.slow = threading.Event()
        self.slow.set()

    def release_weights(self):
        self.calls.append("release")
        self.resident = False

    def restore_weights(self, load):
        self.slow.wait(5)
        self.calls.append("restore")
        load("placement")
        self.resident = True

    @property
    def weights_resident(self):
        return self.resident

    def active_request_count(self):
        return self.requests

    def operation_counter_snapshot(self):
        return {"applied_operation_count": self.ops}

    def close(self, reason="x"):
        self.calls.append("close")


def service(tmp_path, runtime, loads):
    svc = object.__new__(PhysicalNodeService)
    svc.state = "RUNNING"
    svc.capacity_root = tmp_path
    svc.runtime = runtime
    svc.idle = None
    svc._idle_stop = threading.Event()
    svc._idle_thread = None
    svc.transport = None
    svc.sidecar = None
    svc.command_timeout = 5.0
    svc._signed_result = lambda event, details=None: {"event": event, "details": details}
    svc._attach_idle(runtime, load=lambda: loads.append("load"), start_thread=False)
    return svc


def test_runtime_without_weight_release_gets_no_idle_link(tmp_path):
    svc = object.__new__(PhysicalNodeService)
    svc.capacity_root = tmp_path
    svc.idle = None
    svc._idle_stop = threading.Event()
    svc._idle_thread = None
    svc._attach_idle(object(), load=lambda: None, start_thread=False)
    assert svc.idle is None


def test_health_carries_the_idle_residency_detail(tmp_path):
    svc = service(tmp_path, FakeRuntime(), [])
    detail = svc._health({})["details"]["idle_residency"]
    assert detail["state"] == "loaded"


def test_missing_policy_file_means_a_node_never_unloads(tmp_path):
    runtime = FakeRuntime()
    svc = service(tmp_path, runtime, [])
    svc.idle.residency._last_activity -= 10_000
    svc.idle.tick()
    assert runtime.calls == []


def test_idle_commands_through_the_service(tmp_path):
    runtime = FakeRuntime()
    ip.write_policy(tmp_path, 1)
    loads = []
    svc = service(tmp_path, runtime, loads)
    svc.idle.residency._last_activity -= 120
    svc.idle.tick()
    status = svc._idle_command("idle_status", {})["details"]
    assert status["state"] == "idle" and status["line"] == "idle (weights unloaded)"
    reply = svc._idle_command("idle_reload", {})["details"]
    svc.idle.residency.wait_loaded(5)
    assert svc._idle_command("idle_status", {})["details"]["state"] == "loaded"
    assert loads == ["load"] and runtime.calls == ["release", "restore"]
    assert reply["state"] in ("warming", "loaded")


def test_dispatch_routes_the_idle_commands(tmp_path):
    svc = service(tmp_path, FakeRuntime(), [])
    from physical_inference_node import NODE_CONTROL_PROTOCOL
    svc.run_id, svc.deployment_id = "r", "d"
    out = svc.dispatch({"protocol": NODE_CONTROL_PROTOCOL, "command_id": "c1", "run_id": "r",
                        "deployment_id": "d", "command": "idle_status", "payload": {}})
    assert out["details"]["state"] == "loaded"


def test_idle_command_refuses_extra_fields_and_unsupported_nodes(tmp_path):
    svc = service(tmp_path, FakeRuntime(), [])
    with pytest.raises(NodeCommandError) as error:
        svc._idle_command("idle_reload", {"x": 1})
    assert error.value.code == "invalid_idle_fields"
    svc.idle = None
    with pytest.raises(NodeCommandError) as error:
        svc._idle_command("idle_status", {})
    assert error.value.code == "idle_unload_unsupported"


def test_work_gate_reloads_inline_or_raises_member_warming(tmp_path):
    runtime = FakeRuntime()
    ip.write_policy(tmp_path, 1)
    svc = service(tmp_path, runtime, [])
    svc._idle_gate_timeout_s = 0.05
    svc.idle.residency._last_activity -= 120
    svc.idle.tick()
    runtime.slow.clear()
    with pytest.raises(NodeCommandError) as error:
        svc._idle_gate()
    assert error.value.code == "member_warming"
    assert svc.idle.residency.state == "warming"
    runtime.slow.set()
    svc.idle.residency.wait_loaded(5)
    svc._idle_gate()  # loaded: free


def test_work_gate_is_a_noop_without_idle_link(tmp_path):
    svc = object.__new__(PhysicalNodeService)
    svc.idle = None
    svc._idle_gate()


def test_ops_applied_by_hops_count_as_activity(tmp_path):
    runtime = FakeRuntime()
    ip.write_policy(tmp_path, 1)
    svc = service(tmp_path, runtime, [])
    svc.idle.residency._last_activity -= 50
    runtime.ops = 7  # a downstream stage served hops, no command ever reached this node
    svc.idle.tick()
    svc.idle.residency._last_activity -= 50
    svc.idle.tick()
    assert runtime.calls == []  # activity refreshed the clock at the first tick


def test_a_reload_that_fails_closes_the_node_and_terminates_the_process(tmp_path, monkeypatch):
    terminated = []
    monkeypatch.setattr(node_link, "terminate_self", lambda: terminated.append(True))
    runtime = FakeRuntime()
    ip.write_policy(tmp_path, 1)
    svc = object.__new__(PhysicalNodeService)
    svc.state = "RUNNING"
    svc.capacity_root = tmp_path
    svc.runtime = runtime
    svc.idle = None
    svc._idle_stop = threading.Event()
    svc._idle_thread = None
    svc.transport = None
    svc.sidecar = None
    svc.router = None
    def boom():
        raise OSError("disk gone")
    svc._attach_idle(runtime, load=boom, start_thread=False)
    svc.idle.residency._last_activity -= 120
    svc.idle.tick()
    svc.idle.command("idle_reload", {})
    svc.idle.residency.wait_loaded(5)
    assert svc.idle.residency.state == "failed"
    assert svc.state == "STOPPED" and terminated == [True]
    assert "close" in runtime.calls


def test_close_stops_the_idle_thread(tmp_path):
    svc = service(tmp_path, FakeRuntime(), [])
    svc.close()
    assert svc._idle_stop.is_set()
