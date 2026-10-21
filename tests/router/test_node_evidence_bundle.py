# SPDX-License-Identifier: AGPL-3.0-only
"""A14 class D, round 2: a node's evidence bundle survives a failed startup.

A14 r1 failed startup qualification after a 364 s prefill stall.  The serve
then tore both nodes down: only ~1.4 KB of stderr survived, no thread dump
existed, and the transport state that would have separated "waiting for the
data client" from "the peer never replied" was gone.

Each node now writes a bounded, privacy-reduced evidence bundle *outside* its
staging root (``<staging_root>.evidence/``, which neither the remote cleanup
nor the next staging touches) on a prefill stall of its own, and on demand
through the ``diagnostic_evidence`` control command the route sends to every
node when startup qualification fails.  The bundle holds the node's own
stderr ring (redacted), an all-threads stack dump and the transport's
diagnostic state.
"""

from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import threading
from typing import Any

import pytest

import physical_inference_node as node_module
from physical_inference_node import NodeCommandError
from test_node_prefill_stall_wait import (  # noqa: F401 - fixture re-export
    _payload,
    _Router,
    _service,
    _Transport,
    _VirtualTime,
    clock,
)


ENDPOINT_ID = "9f" * 32
RELAY_ADDRESS = "91.99.237.97:443"


def _evidence_dir(artifact_root: Path) -> Path:
    return artifact_root.parent / f"{artifact_root.name}.evidence"


def _bundles(artifact_root: Path) -> list[Path]:
    directory = _evidence_dir(artifact_root)
    if not directory.exists():
        return []
    return sorted(directory.glob("evidence-*.json"))


@pytest.fixture
def stderr_ring(monkeypatch: pytest.MonkeyPatch):
    """Install the node's stderr ring exactly as ``main()`` does.

    pytest re-points ``sys.stderr`` when the call phase starts, so the test
    body installs the ring itself; ``monkeypatch`` restores both afterwards.
    """

    def install() -> Any:
        monkeypatch.setattr(sys, "stderr", sys.stderr)
        monkeypatch.setattr(node_module, "_STDERR_RING", None)
        return node_module._install_stderr_ring(limit_bytes=4096)

    return install


class _DiagnosableTransport(_Transport):
    def diagnostic_state(self) -> dict[str, Any]:
        return {
            "send_operation": {
                "held": True,
                "holder_kind": "TokenEvent",
                "holder_message": "0123abcd",
                "holder_age_ms": 31_000,
                "waiters": 1,
            },
            "pending_sends": [],
            "dispatcher": {"phase": "idle", "phase_age_ms": 5},
        }


def test_busy_thread_capture_keeps_the_emitting_thread_within_the_existing_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior live route may leave workers; evidence must retain its emitter."""
    current = threading.current_thread()
    assert current.ident is not None
    frame = sys._getframe()
    # Put a long-stack worker first in the previous lexicographic ident order.
    earlier_idents = [
        ident for ident in range(1, 1000)
        if str(ident) < str(current.ident)
    ][:4]
    assert len(earlier_idents) == 4
    monkeypatch.setattr(node_module.sys, "_current_frames", lambda: {
        **{ident: frame for ident in earlier_idents}, current.ident: frame,
    })
    monkeypatch.setattr(node_module, "_EVIDENCE_STACK_BYTES", 1024)
    stacks = node_module._thread_stacks()
    emitter = next(thread for thread in stacks if thread["name"] == current.name)
    assert any("test_busy_thread_capture_keeps_the_emitting_thread" in line
               for line in emitter["stack"])


def test_prefill_stall_writes_an_evidence_bundle_outside_the_staging_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
    stderr_ring: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    artifact_root = tmp_path / "mycelium-w6-n0"
    artifact_root.mkdir()
    service = _service(monkeypatch, artifact_root)
    service.router = _Router(clock, completes_at=None)  # type: ignore[assignment]
    service.transport = _DiagnosableTransport(clock, chunk_offsets=[0.0])  # type: ignore[assignment]
    stderr_ring()
    # A traceback-like line on the node's own stderr carrying identifiers.
    print(f"RuntimeError: peer {ENDPOINT_ID} via {RELAY_ADDRESS}", file=sys.stderr)

    with pytest.raises(NodeCommandError) as caught:
        service._infer_start(_payload("request-stall-bundle", 74))

    assert caught.value.code == "prefill_transport_stalled"
    # Nothing was written inside the staging root itself.
    assert list(artifact_root.rglob("evidence-*")) == []
    (bundle,) = _bundles(artifact_root)
    directory = bundle.parent
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600
    document = json.loads(bundle.read_text(encoding="utf-8"))
    assert document["protocol"] == "mycelium.node_evidence_bundle.v1"
    assert document["node_id"] == "node-0"
    assert document["reason"] == "prefill_transport_stalled"
    assert document["request_id"] == "request-stall-bundle"
    # The prefill diagnostic line is in the ring; identifiers are not.
    text = document["stderr"]["text"]
    assert "MYCELIUM-PREFILL-STALL-DIAG code=prefill_transport_stalled" in text
    assert ENDPOINT_ID not in text and "91.99.237.97" not in text
    assert "RuntimeError" in text
    # All threads, with this test's own frame in the current thread's stack.
    names = {thread["name"] for thread in document["threads"]}
    assert threading.current_thread().name in names
    current = next(
        thread
        for thread in document["threads"]
        if thread["name"] == threading.current_thread().name
    )
    assert any(
        "test_prefill_stall_writes_an_evidence_bundle_outside_the_staging_root" in line
        for line in current["stack"]
    )
    assert document["transport"]["send_operation"]["holder_kind"] == "TokenEvent"
    assert document["prefill"]["idle_ms"] >= 124_000
    # One allowlisted pointer line on stderr names the file (no path).
    pointers = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("MYCELIUM-EVIDENCE-BUNDLE ")
    ]
    assert pointers == [
        f"MYCELIUM-EVIDENCE-BUNDLE reason=prefill_transport_stalled file={bundle.name}"
    ]


def test_diagnostic_evidence_command_returns_and_writes_the_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock: _VirtualTime,
    stderr_ring: Any,
) -> None:
    del clock
    artifact_root = tmp_path / "mycelium-w6-n2"
    artifact_root.mkdir()
    service = _service(monkeypatch, artifact_root)
    service.transport = _DiagnosableTransport(  # type: ignore[assignment]
        node_module.time, chunk_offsets=[]
    )
    stderr_ring()
    print("MYCELIUM-SIDECAR-EVENT event=transport_connection_opened", file=sys.stderr)

    result = service.dispatch(
        {
            "protocol": node_module.NODE_CONTROL_PROTOCOL,
            "command_id": "cmd-evidence",
            "run_id": service.run_id,
            "deployment_id": service.deployment_id,
            "command": "diagnostic_evidence",
            "payload": {"reason": "route_qualify"},
        }
    )

    assert result["protocol"] == "mycelium.node_evidence_bundle.v1"
    assert result["reason"] == "route_qualify"
    assert "MYCELIUM-SIDECAR-EVENT event=transport_connection_opened" in result["stderr"]["text"]
    (bundle,) = _bundles(artifact_root)
    assert result["file"] == bundle.name
    assert json.loads(bundle.read_text(encoding="utf-8"))["reason"] == "route_qualify"
    with pytest.raises(NodeCommandError, match="invalid_diagnostic_evidence_fields"):
        service._diagnostic_evidence({"reason": "x", "extra": 1})
    with pytest.raises(NodeCommandError, match="invalid_diagnostic_evidence_reason"):
        service._diagnostic_evidence({"reason": "Not A Code!"})


def test_evidence_bundles_are_bounded_and_pruned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stderr_ring: Any,
) -> None:
    artifact_root = tmp_path / "mycelium-w6-n0"
    artifact_root.mkdir()
    service = _service(monkeypatch, artifact_root)
    stderr_ring()
    for index in range(node_module._EVIDENCE_BUNDLES_KEPT + 4):
        print("x" * 1000 + f" line {index}", file=sys.stderr)
        service.write_evidence_bundle("route_qualify")
    bundles = _bundles(artifact_root)
    assert len(bundles) == node_module._EVIDENCE_BUNDLES_KEPT
    for bundle in bundles:
        assert bundle.stat().st_size <= node_module._EVIDENCE_BUNDLE_MAX_BYTES
        document = json.loads(bundle.read_text(encoding="utf-8"))
        assert len(document["stderr"]["text"].encode("utf-8")) <= 4096


def test_evidence_bundle_never_raises_when_the_directory_cannot_be_written(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "mycelium-w6-n0"
    artifact_root.mkdir()
    service = _service(monkeypatch, artifact_root)
    _evidence_dir(artifact_root).write_text("not a directory", encoding="utf-8")
    written = service.write_evidence_bundle("route_qualify")
    assert written["file"] is None
    assert written["document"]["reason"] == "route_qualify"


def test_evidence_text_redaction_keeps_diagnostics_and_drops_identifiers() -> None:
    text = "\n".join(
        (
            "MYCELIUM-SIDECAR-EVENT event=transport_connection_opened connection_generation=142",
            "Traceback (most recent call last):",
            '  File "/Users/member/mycelium-w6-n2/physical_inference_node.py", line 10, in run',
            "    send(frame)",
            f"mycelium_router.transports.iroh.IrohTransportError: peer {ENDPOINT_ID} at {RELAY_ADDRESS}",
            f"some free text mentioning {RELAY_ADDRESS} and fe80::1ff:fe23:4567:890a",
            "physical-node command failed: IrohTransportError",
        )
    )
    redacted = node_module.redact_evidence_text(text).splitlines()
    assert redacted[0].startswith("MYCELIUM-SIDECAR-EVENT event=transport_connection_opened")
    assert redacted[1] == "Traceback (most recent call last):"
    assert redacted[2] == '  File "/Users/<user>/mycelium-w6-n2/physical_inference_node.py", line 10, in run'
    assert redacted[3] == "    send(frame)"
    assert redacted[4].startswith("mycelium_router.transports.iroh.IrohTransportError: <redacted ")
    assert redacted[5].startswith("<redacted line ")
    assert redacted[6] == "physical-node command failed: IrohTransportError"
    joined = "\n".join(redacted)
    for secret in (ENDPOINT_ID, "91.99.237.97", "fe80::", "member"):
        assert secret not in joined


def test_transport_stall_line_names_the_send_lock_holder() -> None:
    lines: list[str] = []
    original = node_module._emit_stderr_line
    node_module._emit_stderr_line = lines.append  # type: ignore[assignment]
    try:
        node_module._print_transport_stall_event(
            "prefill_stall_send_lock",
            {
                "attempts": 1,
                "stall_window_ms": 30_000,
                "holder_kind": "TokenEvent",
                "holder_age_ms": 31_000,
            },
        )
        node_module._print_transport_stall_event(
            "prefill_stall_send_lock",
            {"attempts": 2, "holder_kind": "evil value=1", "holder_age_ms": -5},
        )
    finally:
        node_module._emit_stderr_line = original  # type: ignore[assignment]
    assert lines == [
        "MYCELIUM-TRANSPORT-STALL event=prefill_stall_send_lock attempts=1 "
        "stall_window_ms=30000 holder_kind=TokenEvent holder_age_ms=31000",
        "MYCELIUM-TRANSPORT-STALL event=prefill_stall_send_lock attempts=2",
    ]
