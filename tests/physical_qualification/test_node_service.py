from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any
import uuid

import mlx.core as mx
import pytest

from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import (
    build_execution_graph,
    build_physical_device_states,
    prepare_physical_deployment,
)
from physical_inference_node import (
    NODE_CONTROL_PROTOCOL,
    NodeCommandError,
    PhysicalNodeService,
    execution_graph_from_document,
)
from runtime_loader import execute_loaded_stage, load_assignment_stage

ROOT = Path(__file__).resolve().parents[2]
NODE_SCRIPT = ROOT / "physical_inference_node.py"
SIDECAR_BINARY = (
    ROOT / "native" / "iroh_transport" / "target" / "debug" / "mycelium-iroh-sidecar"
)


def _command(
    command: str,
    *,
    command_id: str,
    run_id: str,
    deployment_id: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": NODE_CONTROL_PROTOCOL,
        "command_id": command_id,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "command": command,
        "payload": {} if payload is None else payload,
    }


def _send(process: subprocess.Popen[str], document: dict[str, Any]) -> dict[str, Any]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    )
    process.stdin.flush()
    line = process.stdout.readline()
    assert line, process.stderr.read() if process.stderr is not None else ""
    return json.loads(line)


def _all_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            keys.append(key)
            keys.extend(_all_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_all_keys(item))
    return keys


class _NodeClient:
    def __init__(
        self,
        *,
        node_id: str,
        run_id: str,
        deployment_id: str,
        artifact_root: Path,
        socket_root: Path,
    ) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.next_id = 1
        self.socket_path = socket_root / "i.sock"
        self._streams_closed = False
        self._stop_complete = False
        self.process = subprocess.Popen(
            [
                "python3.14",
                str(NODE_SCRIPT),
                "--run-id",
                run_id,
                "--deployment-id",
                deployment_id,
                "--node-id",
                node_id,
                "--artifact-root",
                str(artifact_root),
                "--socket-root",
                str(socket_root),
                "--sidecar-binary",
                str(SIDECAR_BINARY),
                "--sidecar-local-only",
                "--command-timeout",
                "30",
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.process_group_id = os.getpgid(self.process.pid)
        self.session_id = os.getsid(self.process.pid)
        if (
            self.process_group_id != self.process.pid
            or self.session_id != self.process.pid
            or self.process_group_id == os.getpgrp()
        ):
            raise RuntimeError("node_client_process_group_identity_invalid")

    def raw_command(
        self, name: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        command_id = f"{self.node_id}-{self.next_id}"
        self.next_id += 1
        response = _send(
            self.process,
            _command(
                name,
                command_id=command_id,
                run_id=self.run_id,
                deployment_id=self.deployment_id,
                payload=payload,
            ),
        )
        assert response["command_id"] == command_id
        return response

    def command(self, name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.raw_command(name, payload)
        diagnostics = ""
        if response["ok"] is not True and self.process.stderr is not None:
            time.sleep(0.1)
            descriptor = self.process.stderr.fileno()
            os.set_blocking(descriptor, False)
            try:
                diagnostics = os.read(descriptor, 64 * 1024).decode(errors="replace")
            except BlockingIOError:
                pass
            finally:
                os.set_blocking(descriptor, True)
        assert response["ok"] is True, (response, diagnostics)
        assert response["route_ready"] is False
        return response["result"]

    def stop(self) -> None:
        if self._stop_complete:
            return
        try:
            if self.process.poll() is None:
                try:
                    self.command("stop")
                except BaseException:
                    pass
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            if self._owned_process_group_exists():
                self._signal_owned_process_group(signal.SIGTERM)
                if not self._wait_for_owned_process_group_exit(timeout=2):
                    self._signal_owned_process_group(signal.SIGKILL)
                    if not self._wait_for_owned_process_group_exit(timeout=2):
                        raise RuntimeError(
                            "node_client_process_group_shutdown_timeout"
                        )
            if self.process.poll() is None:
                try:
                    self.process.wait(timeout=1)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError(
                        "node_client_wrapper_reap_timeout"
                    ) from error
            try:
                self.socket_path.unlink(missing_ok=True)
            except OSError as error:
                raise RuntimeError("node_client_socket_cleanup_failed") from error
            if os.path.lexists(self.socket_path):
                raise RuntimeError("node_client_socket_cleanup_failed")
            if self._owned_process_group_exists():
                raise RuntimeError("node_client_process_group_shutdown_timeout")
            self._stop_complete = True
        finally:
            self._close_streams()

    def _validate_owned_process_group(self) -> None:
        if (
            self.process_group_id != self.process.pid
            or self.session_id != self.process.pid
            or self.process_group_id == os.getpgrp()
        ):
            raise RuntimeError("node_client_process_group_identity_invalid")

    def _owned_process_group_exists(self) -> bool:
        self._validate_owned_process_group()
        self.process.poll()
        try:
            os.killpg(self.process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError as error:
            raise RuntimeError(
                "node_client_process_group_identity_unverifiable"
            ) from error
        except OSError as error:
            raise RuntimeError(
                "node_client_process_group_identity_unverifiable"
            ) from error
        return True

    def _signal_owned_process_group(self, group_signal: signal.Signals) -> None:
        self._validate_owned_process_group()
        try:
            os.killpg(self.process_group_id, group_signal)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise RuntimeError("node_client_process_group_signal_failed") from error

    def _wait_for_owned_process_group_exit(self, *, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while self._owned_process_group_exists():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.02, remaining))
        return True

    def _close_streams(self) -> None:
        if self._streams_closed:
            return
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        self._streams_closed = True


def _process_group_members(process_group_id: int) -> tuple[tuple[int, str], ...]:
    inventory = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    members: list[tuple[int, str]] = []
    for line in inventory.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) < 2:
            continue
        try:
            process_id = int(fields[0])
            candidate_group = int(fields[1])
        except ValueError:
            continue
        if candidate_group == process_group_id:
            members.append(
                (process_id, fields[2] if len(fields) == 3 else "")
            )
    return tuple(members)


def _configure_and_start_pair(
    clients: tuple[_NodeClient, _NodeClient],
    *,
    graph_document: dict[str, Any],
    state_document: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    configured: dict[str, dict[str, Any]] = {}
    for client in clients:
        result = client.command(
            "configure",
            {
                "assignment_file": f"{client.node_id}-assignment.json",
                "manifest_file": "model-manifest.json",
                "stage_pack_file": f"{client.node_id}-stage-pack.json",
                "graph": graph_document,
                "device_states": state_document,
                "load_generation": 7,
            },
        )
        assert result["observation"]["event"] == "configured"
        configured[client.node_id] = result["observation"]["details"]
    for client, peer in ((clients[0], clients[1]), (clients[1], clients[0])):
        peer_details = configured[peer.node_id]
        started = client.command(
            "start",
            {
                "peer": {
                    "node_id": peer.node_id,
                    "endpoint_id": peer_details["endpoint_addr"]["id"],
                    "endpoint_addr": peer_details["endpoint_addr"],
                    "generation": 1,
                }
            },
        )
        assert started["observation"]["event"] == "started"
    return configured


def test_execution_graph_document_round_trip_is_strict(tmp_path: Path) -> None:
    deployment = prepare_physical_deployment(tmp_path / "deployment")
    loaded = [
        load_assignment_stage(assignment, report, load_generation=1)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    document = json.loads(json.dumps(asdict(graph)))
    assert execution_graph_from_document(document) == graph

    document["unexpected"] = True
    with pytest.raises(NodeCommandError, match="invalid_execution_graph_fields"):
        execution_graph_from_document(document)


def test_safe_document_rejects_nested_symlinks_and_hardlinks(tmp_path: Path) -> None:
    service = PhysicalNodeService(
        run_id=str(uuid.uuid4()),
        deployment_id=str(uuid.uuid4()),
        node_id="node-a",
        artifact_root=tmp_path,
        socket_root=tmp_path / "socket",
        sidecar_binary=Path("/bin/false"),
        sidecar_local_only=True,
        command_timeout=1.0,
    )
    real = tmp_path / "real"
    real.mkdir()
    document = real / "assignment.json"
    document.write_bytes(canonical_json_bytes({"safe": True}))
    assert service._safe_document("real/assignment.json", "invalid_document") == {
        "safe": True
    }

    (tmp_path / "nested").symlink_to(real.name, target_is_directory=True)
    with pytest.raises(NodeCommandError, match="invalid_document"):
        service._safe_document("nested/assignment.json", "invalid_document")

    hardlink = tmp_path / "hardlink.json"
    os.link(document, hardlink)
    with pytest.raises(NodeCommandError, match="invalid_document"):
        service._safe_document("hardlink.json", "invalid_document")


def test_node_subprocess_binds_every_command_and_never_serializes_secrets(
    tmp_path: Path,
) -> None:
    run_id = str(uuid.uuid4())
    deployment_id = str(uuid.uuid4())
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
            str(tmp_path),
            "--socket-root",
            str(tmp_path / "socket"),
            "--sidecar-binary",
            "/bin/false",
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    responses: list[dict[str, Any]] = []
    try:
        responses.append(
            _send(
                process,
                _command(
                    "hello",
                    command_id="hello-1",
                    run_id=run_id,
                    deployment_id=deployment_id,
                ),
            )
        )
        hello = responses[-1]
        assert hello["ok"] is True
        assert hello["route_ready"] is False
        assert hello["result"]["state"] == "NEW"
        assert hello["result"]["process_id"] == process.pid

        responses.append(
            _send(
                process,
                _command(
                    "snapshot",
                    command_id="wrong-run",
                    run_id=str(uuid.uuid4()),
                    deployment_id=deployment_id,
                ),
            )
        )
        assert responses[-1]["ok"] is False
        assert responses[-1]["error"]["code"] == "run_id_mismatch"

        extra = _command(
            "hello",
            command_id="extra-fields",
            run_id=run_id,
            deployment_id=deployment_id,
        )
        extra["extra"] = True
        responses.append(_send(process, extra))
        assert responses[-1]["ok"] is False
        assert responses[-1]["error"]["code"] == "invalid_command_fields"

        responses.append(
            _send(
                process,
                _command(
                    "stop",
                    command_id="stop-1",
                    run_id=run_id,
                    deployment_id=deployment_id,
                ),
            )
        )
        assert responses[-1]["ok"] is True
        assert responses[-1]["result"]["state"] == "STOPPED"
        process.wait(timeout=5)
        assert process.returncode == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    forbidden = ("private", "secret", "bootstrap", "credential", "token")
    for response in responses:
        encoded = json.dumps(response, sort_keys=True).lower()
        assert not any(fragment in encoded for fragment in forbidden)
        assert all(
            not any(fragment in key.lower() for fragment in forbidden)
            for key in _all_keys(response)
        )


def test_two_node_subprocesses_run_distributed_inference_over_native_iroh(
    tmp_path: Path,
) -> None:
    assert SIDECAR_BINARY.is_file()
    deployment = prepare_physical_deployment(tmp_path / "deployment")
    loaded = [
        load_assignment_stage(assignment, report, load_generation=7)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    graph_document = json.loads(json.dumps(asdict(graph)))
    state_document = json.loads(
        json.dumps(
            {
                node_id: asdict(state)
                for node_id, state in build_physical_device_states(graph).items()
            }
        )
    )
    (tmp_path / "model-manifest.json").write_bytes(
        canonical_json_bytes(deployment.manifest)
    )
    for index, node_id in enumerate(("node-a", "node-b")):
        (tmp_path / f"{node_id}-assignment.json").write_bytes(
            canonical_json_bytes(deployment.assignments[index])
        )
        (tmp_path / f"{node_id}-stage-pack.json").write_bytes(
            canonical_json_bytes(deployment.stage_packs[index])
        )

    run_id = str(uuid.uuid4())
    socket_base = Path(tempfile.mkdtemp(prefix="myc-node-test-", dir="/tmp"))
    first = _NodeClient(
        node_id="node-a",
        run_id=run_id,
        deployment_id=graph.deployment_id,
        artifact_root=tmp_path,
        socket_root=socket_base / "a",
    )
    second = _NodeClient(
        node_id="node-b",
        run_id=run_id,
        deployment_id=graph.deployment_id,
        artifact_root=tmp_path,
        socket_root=socket_base / "b",
    )
    try:
        configured = _configure_and_start_pair(
            (first, second),
            graph_document=graph_document,
            state_document=state_document,
        )
        for index, node_id in enumerate(("node-a", "node-b")):
            assert configured[node_id]["stage_pack_digest"] == deployment.stage_packs[
                index
            ]["stage_pack_digest"]
            assert configured[node_id]["stage_pack_verification_digest"] == (
                deployment.stage_pack_verifications[index][
                    "stage_pack_verification_digest"
                ]
            )
        assert first.process.pid != second.process.pid
        assert configured["node-a"]["endpoint_addr"]["id"] != configured["node-b"][
            "endpoint_addr"
        ]["id"]

        request_id = str(uuid.uuid4())
        try:
            started = first.command(
                "infer_start",
                {
                    "request": {
                        "request_id": request_id,
                        "prompt_token_ids": [1, 2, 3],
                        "max_new_tokens": 4,
                        "expected_new_tokens": 4,
                        "qos_class": "interactive",
                        "admitted_at": 0.0,
                        "target_ttft_ms": 1_000.0,
                        "target_tpot_ms": 1_000.0,
                        "target_tokens_per_second": 1.0,
                        "sampling_seed": 17,
                        "generation_config_digest": "sha256:" + "a" * 64,
                    }
                },
            )
        except AssertionError as exc:
            first_failure = first.command("snapshot")
            second_failure = second.command("snapshot")
            first_details = first_failure["observation"]["details"]
            second_details = second_failure["observation"]["details"]
            pytest.fail(
                f"{exc!r}; "
                f"first_transport={first_details['transport']!r}; "
                f"first_phase={first_details['transport_dispatcher_phase']!r}; "
                f"first_trace={first_details['transport_outbound_trace']!r}; "
                f"first_fatal={first_details['transport_fatal_error']!r}; "
                f"second_transport={second_details['transport']!r}; "
                f"second_phase={second_details['transport_dispatcher_phase']!r}; "
                f"second_trace={second_details['transport_outbound_trace']!r}; "
                f"second_fatal={second_details['transport_fatal_error']!r}"
            )
        start_details = started["observation"]["details"]
        assert start_details["status"] == "DECODING"
        assert start_details["output"]["token_indexes"] == [0]
        decoded = first.command("infer_decode", {"request_id": request_id, "count": 3})
        decode_details = decoded["observation"]["details"]
        assert decode_details["dispatched"] == 3
        assert decode_details["status"] == "COMPLETED"
        assert decode_details["output"]["token_indexes"] == [0, 1, 2, 3]

        reference = load_assignment_stage(
            deployment.reference_assignment,
            deployment.reference_report,
            load_generation=7,
        )
        context = [1, 2, 3]
        expected_tokens: list[int] = []
        for _ in range(4):
            logits = execute_loaded_stage(
                reference,
                token_ids=mx.array((tuple(context),), dtype=mx.uint32),
            )
            mx.eval(logits)
            next_token = int(mx.argmax(logits[0, -1, :]).item())
            expected_tokens.append(next_token)
            context.append(next_token)
        assert decode_details["output"]["token_ids"] == expected_tokens

        cancelled_request_id = str(uuid.uuid4())
        cancellation_started = first.command(
            "infer_start",
            {
                "request": {
                    "request_id": cancelled_request_id,
                    "prompt_token_ids": [3, 2, 1],
                    "max_new_tokens": 4,
                    "expected_new_tokens": 4,
                    "qos_class": "interactive",
                    "admitted_at": 0.0,
                    "target_ttft_ms": 1_000.0,
                    "target_tpot_ms": 1_000.0,
                    "target_tokens_per_second": 1.0,
                    "sampling_seed": 19,
                    "generation_config_digest": "sha256:" + "b" * 64,
                }
            },
        )["observation"]["details"]
        assert cancellation_started["status"] == "DECODING"
        cancelled = first.command(
            "cancel", {"request_id": cancelled_request_id}
        )["observation"]["details"]
        assert cancelled["result"] is True
        deadline = time.monotonic() + 5
        second_after_cancel: dict[str, Any] = {}
        while time.monotonic() < deadline:
            second_after_cancel = second.command("snapshot")["observation"]["details"]
            if second_after_cancel["runtime"]["active_state_count"] == 0:
                break
            time.sleep(0.02)
        assert second_after_cancel["runtime"]["active_state_count"] == 0

        first_snapshot = first.command("snapshot")["observation"]["details"]
        second_snapshot = second.command("snapshot")["observation"]["details"]
        assert first_snapshot["transport"]["remote_frames_sent"] > 0
        assert second_snapshot["transport"]["remote_frames_received"] > 0
        assert first_snapshot["runtime"]["active_state_count"] == 0
        assert second_snapshot["runtime"]["active_state_count"] == 0

        old_first_endpoint = configured["node-a"]["endpoint_addr"]["id"]
        old_second_endpoint = configured["node-b"]["endpoint_addr"]["id"]
        disconnected_wrapper_id = second.process.pid
        disconnected_group_id = second.process_group_id
        disconnected_socket = socket_base / "b" / "i.sock"
        group_before_disconnect = _process_group_members(disconnected_group_id)
        assert second.session_id == disconnected_group_id == disconnected_wrapper_id
        assert disconnected_socket.is_socket()
        assert disconnected_wrapper_id in {
            process_id for process_id, _command_line in group_before_disconnect
        }
        assert len(group_before_disconnect) >= 2
        second.process.kill()
        second.process.wait(timeout=10)
        disconnected_returncode = second.process.returncode
        group_after_wrapper_exit = _process_group_members(disconnected_group_id)
        assert disconnected_wrapper_id not in {
            process_id for process_id, _command_line in group_after_wrapper_exit
        }
        assert group_after_wrapper_exit
        second.stop()
        group_after_stop = _process_group_members(disconnected_group_id)
        assert group_after_stop == (), (
            disconnected_group_id,
            group_before_disconnect,
            group_after_wrapper_exit,
            group_after_stop,
        )
        assert not disconnected_socket.exists()
        disconnected_request_id = str(uuid.uuid4())
        disconnected = first.raw_command(
            "infer_start",
            {
                "request": {
                    "request_id": disconnected_request_id,
                    "prompt_token_ids": [1, 1, 2],
                    "max_new_tokens": 2,
                    "expected_new_tokens": 2,
                    "qos_class": "interactive",
                    "admitted_at": 0.0,
                    "target_ttft_ms": 1_000.0,
                    "target_tpot_ms": 1_000.0,
                    "target_tokens_per_second": 1.0,
                    "sampling_seed": 23,
                    "generation_config_digest": "sha256:" + "c" * 64,
                }
            },
        )
        assert disconnected["ok"] is False
        assert disconnected["route_ready"] is False
        first.stop()

        first = _NodeClient(
            node_id="node-a",
            run_id=run_id,
            deployment_id=graph.deployment_id,
            artifact_root=tmp_path,
            socket_root=socket_base / "a-restarted",
        )
        second = _NodeClient(
            node_id="node-b",
            run_id=run_id,
            deployment_id=graph.deployment_id,
            artifact_root=tmp_path,
            socket_root=socket_base / "b-restarted",
        )
        restarted = _configure_and_start_pair(
            (first, second),
            graph_document=graph_document,
            state_document=state_document,
        )
        assert disconnected_returncode != 0
        assert restarted["node-a"]["endpoint_addr"]["id"] != old_first_endpoint
        assert restarted["node-b"]["endpoint_addr"]["id"] != old_second_endpoint
        recovered_request_id = str(uuid.uuid4())
        recovered = first.command(
            "infer_start",
            {
                "request": {
                    "request_id": recovered_request_id,
                    "prompt_token_ids": [2, 3, 5],
                    "max_new_tokens": 2,
                    "expected_new_tokens": 2,
                    "qos_class": "interactive",
                    "admitted_at": 0.0,
                    "target_ttft_ms": 1_000.0,
                    "target_tpot_ms": 1_000.0,
                    "target_tokens_per_second": 1.0,
                    "sampling_seed": 29,
                    "generation_config_digest": "sha256:" + "d" * 64,
                }
            },
        )["observation"]["details"]
        assert recovered["status"] == "DECODING"
        recovered_decode = first.command(
            "infer_decode", {"request_id": recovered_request_id, "count": 1}
        )["observation"]["details"]
        assert recovered_decode["status"] == "COMPLETED"
        assert recovered_decode["output"]["token_indexes"] == [0, 1]
    finally:
        first.stop()
        second.stop()
        shutil.rmtree(socket_base, ignore_errors=True)
    assert first.process.returncode == 0
    assert second.process.returncode == 0
