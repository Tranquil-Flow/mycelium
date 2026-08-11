from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import tempfile
from types import SimpleNamespace
from typing import Any
import uuid

import pytest

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import (
    build_execution_graph,
    build_physical_device_states,
    prepare_physical_deployment,
)
import physical_inference_node as node
from runtime_loader import load_assignment_stage

ROOT = Path(__file__).resolve().parents[2]
NODE_SCRIPT = ROOT / "physical_inference_node.py"
SIDECAR_BINARY = (
    ROOT / "native" / "iroh_transport" / "target" / "debug" / "mycelium-iroh-sidecar"
)


def _cli_argv(tmp_path: Path) -> list[str]:
    return [
        str(NODE_SCRIPT),
        "--run-id",
        "run-a",
        "--deployment-id",
        "deployment-a",
        "--node-id",
        "node-a",
        "--artifact-root",
        str(tmp_path),
        "--socket-root",
        str(tmp_path / "socket"),
        "--sidecar-binary",
        str(SIDECAR_BINARY),
    ]


def _service(
    tmp_path: Path,
    endpoint_secret_file: Path | None,
) -> node.PhysicalNodeService:
    return node.PhysicalNodeService(
        run_id="run-a",
        deployment_id="deployment-a",
        node_id="node-a",
        artifact_root=tmp_path,
        socket_root=tmp_path / "socket",
        sidecar_binary=Path("/bin/false"),
        sidecar_local_only=True,
        command_timeout=1.0,
        endpoint_secret_file=endpoint_secret_file,
        requested_decode_mode=None,
    )


def test_cli_parser_accepts_absent_and_absolute_endpoint_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(node.sys, "argv", _cli_argv(tmp_path))
    absent = node._parse_args()
    assert absent.endpoint_secret_file is None

    lexical = tmp_path / "identity" / ".." / "identity.key"
    monkeypatch.setattr(
        node.sys,
        "argv",
        [*_cli_argv(tmp_path), "--endpoint-secret-file", str(lexical)],
    )
    present = node._parse_args()
    assert present.endpoint_secret_file == lexical
    assert str(present.endpoint_secret_file) == str(lexical)


def test_main_passes_exact_endpoint_secret_path_to_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    endpoint_secret_file = tmp_path / "identity" / ".." / "identity.key"
    parsed = argparse.Namespace(
        run_id="run-a",
        deployment_id="deployment-a",
        node_id="node-a",
        artifact_root=tmp_path,
        socket_root=tmp_path / "socket",
        sidecar_binary=Path("/bin/false"),
        sidecar_local_only=True,
        command_timeout=1.0,
        endpoint_secret_file=endpoint_secret_file,
        decode_mode=None,
    )
    captured: dict[str, Any] = {}

    class FakeService:
        stop_requested = False

        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(node, "_parse_args", lambda: parsed)
    monkeypatch.setattr(node, "PhysicalNodeService", FakeService)
    monkeypatch.setattr(node.sys, "stdin", SimpleNamespace(buffer=()))
    assert node.main() == 0
    assert captured["endpoint_secret_file"] is endpoint_secret_file


def test_service_accepts_absent_and_exact_absolute_endpoint_secret(
    tmp_path: Path,
) -> None:
    assert _service(tmp_path, None).endpoint_secret_file is None
    lexical = tmp_path / "identity" / ".." / "identity.key"
    service = _service(tmp_path, lexical)
    assert service.endpoint_secret_file is lexical
    assert str(service.endpoint_secret_file) == str(lexical)


@pytest.mark.parametrize(
    "invalid",
    [
        Path("relative.key"),
        Path(""),
        Path("/tmp/line\nbreak"),
        Path("/tmp/nul\x00byte"),
        Path("/tmp/delete\x7fbyte"),
        Path("/tmp/c1\x85byte"),
    ],
)
def test_service_rejects_invalid_endpoint_secret_with_value_free_error(
    tmp_path: Path,
    invalid: Path,
) -> None:
    with pytest.raises(node.NodeCommandError) as raised:
        _service(tmp_path, invalid)
    assert raised.value.code == "invalid_endpoint_secret_file"
    assert str(raised.value) == "invalid_endpoint_secret_file"
    assert str(invalid) not in str(raised.value)


def test_service_rejects_non_path_endpoint_secret(tmp_path: Path) -> None:
    with pytest.raises(node.NodeCommandError, match="^invalid_endpoint_secret_file$"):
        _service(tmp_path, "/tmp/identity.key")  # type: ignore[arg-type]


def test_native_sidecar_argv_and_service_factory_preserve_conduit(
    tmp_path: Path,
) -> None:
    endpoint_secret_file = tmp_path / "identity" / ".." / "identity.key"
    present = node.NativeSidecarProcess(
        binary=Path("/native/sidecar"),
        socket_root=tmp_path / "present",
        local_only=True,
        queue_capacity=128,
        startup_timeout=3.0,
        endpoint_secret_file=endpoint_secret_file,
    )
    present_argv = present._argv(41)
    assert present.endpoint_secret_file is endpoint_secret_file
    assert present_argv == [
        "/native/sidecar",
        "--uds",
        str(tmp_path / "present" / "i.sock"),
        "--bootstrap-fd",
        "41",
        "--queue-capacity",
        "128",
        "--local-only",
        "--endpoint-secret-file",
        str(endpoint_secret_file),
    ]
    assert present_argv.count("--endpoint-secret-file") == 1
    assert present_argv[present_argv.index("--bootstrap-fd") + 1] == "41"

    absent = node.NativeSidecarProcess(
        binary=Path("/native/sidecar"),
        socket_root=tmp_path / "absent",
        local_only=False,
        queue_capacity=7,
        startup_timeout=3.0,
        endpoint_secret_file=None,
    )
    assert absent._argv(42) == [
        "/native/sidecar",
        "--uds",
        str(tmp_path / "absent" / "i.sock"),
        "--bootstrap-fd",
        "42",
        "--queue-capacity",
        "7",
    ]

    sidecar = _service(tmp_path, endpoint_secret_file)._new_sidecar_process()
    assert sidecar.endpoint_secret_file is endpoint_secret_file
    assert sidecar.local_only is True
    assert sidecar.queue_capacity == 128


def test_native_sidecar_rejects_invalid_direct_path_without_disclosure(
    tmp_path: Path,
) -> None:
    marker = "private-endpoint-path-" + "x" * 120
    invalid = Path("/tmp") / f"{marker}\n"
    with pytest.raises(node.NodeCommandError) as raised:
        node.NativeSidecarProcess(
            binary=Path("/native/sidecar"),
            socket_root=tmp_path / "socket",
            local_only=True,
            queue_capacity=128,
            startup_timeout=3.0,
            endpoint_secret_file=invalid,
        )
    control = node._response(
        _service(tmp_path, None),
        command_id="invalid-secret",
        ok=False,
        error_code=raised.value.code,
    )
    public = json.dumps(control, sort_keys=True)
    assert raised.value.code == "invalid_endpoint_secret_file"
    assert marker not in str(raised.value)
    assert marker not in public
    assert str(invalid) not in public
    assert control["route_ready"] is False


def _configure_payload(artifact_root: Path) -> tuple[str, dict[str, Any]]:
    deployment = prepare_physical_deployment(artifact_root / "deployment")
    loaded = [
        load_assignment_stage(assignment, report, load_generation=7)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
            strict=True,
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    assignment_name = "node-a-assignment.json"
    report_name = "node-a-artifact-report.json"
    (artifact_root / assignment_name).write_bytes(
        canonical_json_bytes(deployment.assignments[0])
    )
    (artifact_root / report_name).write_bytes(
        canonical_json_bytes(deployment.artifact_reports[0])
    )
    payload = {
        "assignment_file": assignment_name,
        "artifact_report_file": report_name,
        "graph": json.loads(json.dumps(asdict(graph))),
        "device_states": {
            node_id: asdict(state)
            for node_id, state in build_physical_device_states(graph).items()
        },
        "load_generation": 7,
    }
    return graph.deployment_id, payload


def _command(
    *,
    command: str,
    command_id: str,
    run_id: str,
    deployment_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": node.NODE_CONTROL_PROTOCOL,
        "command_id": command_id,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "command": command,
        "payload": {} if payload is None else payload,
    }


def _send_bounded(
    process: subprocess.Popen[bytes],
    document: dict[str, Any],
    *,
    timeout: float,
) -> tuple[dict[str, Any], bytes]:
    assert process.stdin is not None
    assert process.stdout is not None
    encoded = canonical_json_bytes(document) + b"\n"
    process.stdin.write(encoded)
    process.stdin.flush()
    readable, _, _ = select.select([process.stdout], [], [], timeout)
    assert readable, "node response timeout"
    line = process.stdout.readline()
    assert line, "node exited before response"
    return json.loads(line), line


def _close_process(process: subprocess.Popen[bytes]) -> bytes:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    stderr = b""
    if process.stderr is not None:
        stderr = process.stderr.read()
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()
    return stderr


def _run_configured_node(
    *,
    artifact_root: Path,
    socket_root: Path,
    endpoint_secret_file: Path,
    deployment_id: str,
    configure_payload: dict[str, Any],
) -> tuple[str, bytes]:
    run_id = str(uuid.uuid4())
    process = subprocess.Popen(
        [
            "python3.14",
            str(NODE_SCRIPT),
            "--run-id",
            run_id,
            "--deployment-id",
            deployment_id,
            "--node-id",
            "node-a",
            "--artifact-root",
            str(artifact_root),
            "--socket-root",
            str(socket_root),
            "--sidecar-binary",
            str(SIDECAR_BINARY),
            "--sidecar-local-only",
            "--command-timeout",
            "30",
            "--endpoint-secret-file",
            str(endpoint_secret_file),
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    output = bytearray()
    endpoint_id: str | None = None
    try:
        hello, raw = _send_bounded(
            process,
            _command(
                command="hello",
                command_id="hello",
                run_id=run_id,
                deployment_id=deployment_id,
            ),
            timeout=5,
        )
        output.extend(raw)
        assert hello["ok"] is True
        assert hello["route_ready"] is False
        assert hello["result"]["route_ready"] is False

        configured, raw = _send_bounded(
            process,
            _command(
                command="configure",
                command_id="configure",
                run_id=run_id,
                deployment_id=deployment_id,
                payload=configure_payload,
            ),
            timeout=35,
        )
        output.extend(raw)
        assert configured["ok"] is True
        assert configured["route_ready"] is False
        observation = configured["result"]["observation"]
        assert observation["route_ready"] is False
        endpoint_id = observation["details"]["endpoint_addr"]["id"]

        stopped, raw = _send_bounded(
            process,
            _command(
                command="stop",
                command_id="stop",
                run_id=run_id,
                deployment_id=deployment_id,
            ),
            timeout=10,
        )
        output.extend(raw)
        assert stopped["ok"] is True
        assert stopped["route_ready"] is False
        process.wait(timeout=10)
        assert process.returncode == 0
    finally:
        output.extend(_close_process(process))
    assert endpoint_id is not None
    return endpoint_id, bytes(output)


def test_host_node_service_reuses_endpoint_identity_without_public_leakage(
    tmp_path: Path,
) -> None:
    assert SIDECAR_BINARY.is_file()
    secret = bytes(range(32))
    endpoint_secret_file = tmp_path / (
        "private-endpoint-secret-" + "x" * 120 + ".key"
    )
    descriptor = os.open(
        endpoint_secret_file,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(secret)
    metadata = endpoint_secret_file.stat()
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_uid == os.getuid()
    assert metadata.st_size == 32

    deployment_id, configure_payload = _configure_payload(tmp_path)
    endpoint_ids: list[str] = []
    public_output = bytearray()
    with tempfile.TemporaryDirectory(prefix="myc-endpoint-", dir="/tmp") as socket_base:
        for index in range(2):
            endpoint_id, output = _run_configured_node(
                artifact_root=tmp_path,
                socket_root=Path(socket_base) / f"service-{index}",
                endpoint_secret_file=endpoint_secret_file,
                deployment_id=deployment_id,
                configure_payload=configure_payload,
            )
            endpoint_ids.append(endpoint_id)
            public_output.extend(output)

    assert endpoint_ids[0] == endpoint_ids[1]
    assert os.fsencode(endpoint_secret_file) not in public_output
    assert secret not in public_output
