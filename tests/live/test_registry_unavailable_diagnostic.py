# SPDX-License-Identifier: AGPL-3.0-only
"""A deployment whose runtime cannot be built must SAY WHY, not just "unavailable".

Chain run 63r3 (2026-09-16) stopped because the registry could not build a runtime for its
only deployment. The registry reported health "unavailable" with qualified_at_unix_ms 0 and
deployment_count 0, while the bounded reason code was computed inside the startup loop and
then DROPPED - no artifact named the cause, and the root-cause search had to proceed by
elimination. This pins the registry-path diagnostic, mirroring
`test_physical_server_logs_node_stderr_diagnostic_before_cleanup_mask` for the
single-server path.
"""

import json

from mycelium_live import supervisor as supervisor_module


class _RejectingNode(RuntimeError):
    """Minimal stand-in for a node rejection carrying its own stderr tail."""

    def __init__(self, code: str, diagnostic: str) -> None:
        super().__init__(code)
        self.code = code
        self.diagnostic = diagnostic


def _deployment():
    from types import SimpleNamespace

    return SimpleNamespace(
        deployment_id="deployment-63r3",
        model_id="Qwen/Qwen2.5-7B-Instruct",
        model_revision="a09a35458c702b33eeacc393d103063234e8bc28",
        quantization="int8-weight-only",
        topology_size=2,
    )


def test_unavailable_deployment_is_recorded_with_a_bounded_reason_code(capsys) -> None:
    unavailable = []
    supervisor_module._record_unavailable_deployment(
        _deployment(), RuntimeError("route_open_rejected"), unavailable=unavailable
    )

    assert len(unavailable) == 1, "the deployment must still be recorded as unavailable"
    recorded = unavailable[0]
    assert recorded.reason_code == "route_open_rejected", (
        "the bounded reason code must be kept, not dropped"
    )
    assert recorded.deployment_id == "deployment-63r3"
    assert recorded.model_id == "Qwen/Qwen2.5-7B-Instruct"

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines, "the registry path must emit a stage diagnostic, as the single-server path does"
    document = json.loads(lines[0])
    assert document["protocol"] == "mycelium.live_serve_diagnostic.v1"
    assert document["stage"] == "qualified_runtime"
    assert isinstance(document["emitted_at_unix_ms"], int)


def test_unavailable_deployment_diagnostic_carries_the_node_stderr_tail(capsys) -> None:
    unavailable = []
    exc = _RejectingNode(
        "node_command_failed",
        "node node-b stderr (tail):\nTraceback (most recent call last):\n"
        "ValueError: stage pack digest mismatch\n",
    )
    supervisor_module._record_unavailable_deployment(
        _deployment(), exc, unavailable=unavailable
    )

    assert unavailable[0].reason_code == "node_command_failed"
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    chain = json.loads(lines[0])["cause_chain"]
    assert chain[0]["code"] == "node_command_failed"
    assert "stage pack digest mismatch" in chain[0]["node_stderr_tail"], (
        "the rejecting node's own stderr must reach the serve log"
    )
