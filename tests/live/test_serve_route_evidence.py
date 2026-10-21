# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D, round 2: a failed startup qualification keeps both nodes' evidence.

When the startup challenge fails inside an open route, the serve prints one
``mycelium.live_serve_diagnostic.v1`` line with every node's bounded stderr
tail (relay-stall REPORT item 4) and then tears the route down.  It now also
asks every node for its evidence bundle over the existing control channel -
this is how the remote node's thread dump and transport state reach the
operator without ssh - and writes the bundles and the redacted parent-side
stderr tails below ``<product-state-root>/diagnostics/``, which the A14 window
keeps.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from mycelium_live import supervisor
from mycelium_live.route import PhysicalLiveRoute


ENDPOINT_ID = "ab" * 32


class _Route:
    def __init__(self) -> None:
        placements = (
            SimpleNamespace(node_id="node-0"),
            SimpleNamespace(node_id="node-2"),
        )
        self.execution_graph = SimpleNamespace(
            stages=(
                SimpleNamespace(placements=(placements[0],)),
                SimpleNamespace(placements=(placements[1],)),
            )
        )
        self.requests: list[tuple[str, str, float]] = []

    def diagnostic_stderr(self, node_id: str) -> str:
        return (
            f"MYCELIUM-SIDECAR-EVENT event=transport_connection_opened ({node_id})\n"
            f"RuntimeError: peer {ENDPOINT_ID}\n"
        )

    def diagnostic_evidence(self, node_id: str, *, reason: str, timeout_seconds: float):
        self.requests.append((node_id, reason, timeout_seconds))
        if node_id == "node-2":
            raise TimeoutError("node did not answer")
        return {
            "protocol": "mycelium.node_evidence_bundle.v1",
            "node_id": node_id,
            "reason": reason,
            "file": "evidence-node-0-1-route_qualify.json",
            "threads": [{"name": "MainThread", "stack": ["  File \"x\", line 1, in y"]}],
        }


def test_route_qualify_failure_writes_both_nodes_evidence_and_prints_both_tails(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = _Route()
    evidence_root = tmp_path / "product-state" / "diagnostics"

    supervisor._emit_serve_diagnostic(
        RuntimeError("prefill_transport_stalled"),
        stage="route_qualify",
        route=route,
        evidence_root=evidence_root,
    )

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    diagnostic = next(
        line for line in lines if line["protocol"] == "mycelium.live_serve_diagnostic.v1"
    )
    # relay-stall REPORT item 4: BOTH nodes' bounded tails are printed.
    assert set(diagnostic["node_stderr_tails"]) == {"node-0", "node-2"}
    collected = next(
        line for line in lines if line["protocol"] == "mycelium.live_serve_evidence.v1"
    )
    directory = Path(collected["directory"])
    assert directory.parent == evidence_root
    assert directory.name.startswith("route_qualify-")
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    names = sorted(path.name for path in directory.iterdir())
    assert names == [
        "index.json",
        "node-0.evidence.json",
        "node-0.stderr-tail.txt",
        "node-2.stderr-tail.txt",
    ]
    for path in directory.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    bundle = json.loads((directory / "node-0.evidence.json").read_text(encoding="utf-8"))
    assert bundle["reason"] == "route_qualify"
    tail = (directory / "node-2.stderr-tail.txt").read_text(encoding="utf-8")
    assert "MYCELIUM-SIDECAR-EVENT event=transport_connection_opened" in tail
    assert ENDPOINT_ID not in tail
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    assert index["stage"] == "route_qualify"
    assert index["nodes"]["node-0"]["evidence"] == "node-0.evidence.json"
    assert index["nodes"]["node-0"]["node_file"] == "evidence-node-0-1-route_qualify.json"
    assert index["nodes"]["node-2"]["evidence"] is None
    assert index["nodes"]["node-2"]["evidence_error"] == "TimeoutError"
    assert sorted(node for node, _, _ in route.requests) == ["node-0", "node-2"]
    assert all(reason == "route_qualify" for _, reason, _ in route.requests)
    assert all(0 < timeout <= 30.0 for _, _, timeout in route.requests)


def test_diagnostic_without_evidence_root_keeps_the_previous_single_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = _Route()
    supervisor._emit_serve_diagnostic(
        RuntimeError("x"), stage="route_qualify", route=route
    )
    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 1
    assert json.loads(lines[0])["protocol"] == "mycelium.live_serve_diagnostic.v1"
    assert route.requests == []


class _Process:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def command(self, operation, payload=None, **kwargs):
        self.calls.append((operation, dict(payload or {}), kwargs))
        return {"protocol": "mycelium.node_evidence_bundle.v1", "reason": payload["reason"]}


def test_physical_route_asks_a_node_for_evidence_on_a_bounded_control_command() -> None:
    route = object.__new__(PhysicalLiveRoute)
    process = _Process()
    route._lock = threading.RLock()
    route._command_sequence = 0
    route._plan = {"run_id": "run-1", "deployment_id": "deployment-1"}
    route._sessions = {"node-2": SimpleNamespace(_process=process)}

    result = route.diagnostic_evidence("node-2", reason="route_qualify", timeout_seconds=12.0)

    assert result == {"protocol": "mycelium.node_evidence_bundle.v1", "reason": "route_qualify"}
    ((operation, payload, kwargs),) = process.calls
    assert operation == "diagnostic_evidence"
    assert payload == {"reason": "route_qualify"}
    assert kwargs["timeout_seconds"] == 12.0
    assert kwargs["terminate_on_timeout"] is False
    assert kwargs["command_id"].startswith("cmd-")
    assert route.diagnostic_evidence("node-9", reason="route_qualify") is None
