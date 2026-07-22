from __future__ import annotations

import json
import sys

import pytest

from physical_inference_qualification import ControllerError, NodeProcessSession


NODE_SCRIPT = r'''
import json,sys
for line in sys.stdin.buffer:
    command=json.loads(line)
    response={
        "command_id":command["command_id"],
        "node_id":"node-1",
        "ok":True,
        "protocol":"mycelium.physical_node_control.v1",
        "result":{"command":command["command"],"state":"NEW"},
        "route_ready":False,
    }
    sys.stdout.write(json.dumps(response,sort_keys=True,separators=(",",":"))+"\n")
    sys.stdout.flush()
'''


def test_node_process_session_correlates_canonical_command_and_response() -> None:
    session = NodeProcessSession(
        argv=(sys.executable, "-c", NODE_SCRIPT),
        node_id="node-1",
        run_id="run-1",
        deployment_id="deployment-1",
        timeout_seconds=2.0,
    )
    try:
        response = session.send(
            command_id="command-1",
            command="hello",
            payload={},
        )
    finally:
        session.close()

    assert response == {
        "command_id": "command-1",
        "node_id": "node-1",
        "ok": True,
        "protocol": "mycelium.physical_node_control.v1",
        "result": {"command": "hello", "state": "NEW"},
        "route_ready": False,
    }
    assert session.returncode is not None


def test_node_process_session_rejects_mismatched_command_id() -> None:
    script = NODE_SCRIPT.replace(
        '"command_id":command["command_id"]',
        '"command_id":"wrong-command"',
    )
    session = NodeProcessSession(
        argv=(sys.executable, "-c", script),
        node_id="node-1",
        run_id="run-1",
        deployment_id="deployment-1",
        timeout_seconds=2.0,
    )
    try:
        with pytest.raises(ControllerError, match="node_response_correlation_invalid"):
            session.send(
                command_id="command-1",
                command="hello",
                payload={},
            )
    finally:
        session.close()


def test_node_process_session_rejects_route_ready_claim() -> None:
    script = NODE_SCRIPT.replace('"route_ready":False', '"route_ready":True')
    session = NodeProcessSession(
        argv=(sys.executable, "-c", script),
        node_id="node-1",
        run_id="run-1",
        deployment_id="deployment-1",
        timeout_seconds=2.0,
    )
    try:
        with pytest.raises(ControllerError, match="node_response_readiness_invalid"):
            session.send(
                command_id="command-1",
                command="hello",
                payload={},
            )
    finally:
        session.close()


def test_node_process_session_rejects_noncanonical_response() -> None:
    script = NODE_SCRIPT.replace(
        'json.dumps(response,sort_keys=True,separators=(",",":"))',
        'json.dumps(response,sort_keys=True)',
    )
    session = NodeProcessSession(
        argv=(sys.executable, "-c", script),
        node_id="node-1",
        run_id="run-1",
        deployment_id="deployment-1",
        timeout_seconds=2.0,
    )
    try:
        with pytest.raises(ControllerError, match="node_response_noncanonical"):
            session.send(
                command_id="command-1",
                command="hello",
                payload={},
            )
    finally:
        session.close()
