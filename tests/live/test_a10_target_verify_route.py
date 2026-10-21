# SPDX-License-Identifier: AGPL-3.0-only
"""The route only carries an A10 target-verification operation to a live node.

``PhysicalLiveRoute.a10_target_verify`` forwards one private operation to an
existing node session and returns that node's own verification record.  It
never executes, retries or synthesizes a record, and fails closed otherwise.
These tests drive the method over a scripted session (never physical evidence).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

import mycelium_live.route as route_module

RECORD_PROTOCOL = "mycelium.target_verification_operation_record.v1"


class ScriptedSession:
    def __init__(self, result: Any, *, returncode: int | None = None,
                 error: BaseException | None = None) -> None:
        self.result = result
        self.returncode = returncode
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        self.calls.append({"command_id": command_id, "command": command,
                           "payload": dict(payload),
                           "deadline_monotonic_s": deadline_monotonic_s})
        if self.error is not None:
            raise self.error
        return {"protocol": "mycelium.physical_node_control.v1", "command_id": command_id,
                "node_id": "node-a", "ok": True, "route_ready": False,
                "result": self.result}

    def send(self, **_kwargs):  # pragma: no cover - the deadline path is required
        raise AssertionError("a10_target_verify must use the deadline-bound send")


def _route(sessions: dict[str, Any]) -> Any:
    route = object.__new__(route_module.PhysicalLiveRoute)
    route._lock = threading.RLock()
    route._sessions = sessions
    route._plan = {"run_id": "run-a10", "deployment_id": "deployment-a10"}
    route._command_sequence = 0
    return route


def _record(**overrides: Any) -> dict[str, Any]:
    record = {"protocol": RECORD_PROTOCOL, "execution_path": "product_dispatcher",
              "provenance": "live", "passed": True}
    record.update(overrides)
    return record


def test_forwards_the_operation_and_returns_the_nodes_own_record():
    session = ScriptedSession(_record())
    route = _route({"node-a": session})
    deadline = time.monotonic() + 30
    operation = {"verification_ref": "ref-1", "width": 4}
    assert route.a10_target_verify("node-a", operation=operation,
                                   deadline_monotonic_s=deadline) == _record()
    (call,) = session.calls
    assert call["command"] == "a10_target_verify"
    assert call["payload"] == operation
    assert call["deadline_monotonic_s"] == deadline
    assert call["command_id"].startswith("cmd-")


@pytest.mark.parametrize("sessions", [{}, {"node-a": ScriptedSession(_record(), returncode=1)}])
def test_refuses_an_unknown_or_exited_session(sessions):
    with pytest.raises(RuntimeError, match="a10_target_verify_node_unavailable"):
        _route(sessions).a10_target_verify("node-a", operation={},
                                           deadline_monotonic_s=time.monotonic() + 30)


@pytest.mark.parametrize("result", [
    None, "record", {}, _record(protocol="mycelium.other.v1"),
    _record(execution_path="direct_runtime"),
])
def test_refuses_anything_but_a_product_dispatched_node_record(result):
    route = _route({"node-a": ScriptedSession(result)})
    with pytest.raises(RuntimeError, match="a10_target_verify_response_invalid"):
        route.a10_target_verify("node-a", operation={},
                                deadline_monotonic_s=time.monotonic() + 30)


def test_a_missed_deadline_is_never_retried():
    session = ScriptedSession(_record(), error=TimeoutError("command_deadline_exceeded"))
    route = _route({"node-a": session})
    with pytest.raises(TimeoutError, match="command_deadline_exceeded"):
        route.a10_target_verify("node-a", operation={},
                                deadline_monotonic_s=time.monotonic() + 30)
    assert len(session.calls) == 1
