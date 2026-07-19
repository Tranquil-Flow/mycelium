from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Callable
import urllib.parse
import uuid

import mlx.core as mx
import pytest

from mycelium_mobile.pixel_client import PixelStageClient, PixelStageClientError
from mycelium_mobile.pixel_stage import PixelStage, build_stage_pack
from mycelium_mobile.termux_bridge import TermuxBridgeClient
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.physical_deployment import prepare_physical_deployment
from mycelium_router.mlx_runtime import _gpt2_block_with_kv
from runtime_loader import canonical_json, execute_loaded_stage, load_assignment_stage

ROOT = Path(__file__).resolve().parents[2]
HOST_WORKER = ROOT / "physical_pixel_host_stage.py"
HOST_PROTOCOL = "mycelium.pixel_host_stage_control.v1"


class _HostStageClient:
    def __init__(
        self,
        *,
        role: str,
        assignment_file: Path,
        report_file: Path,
    ) -> None:
        self.role = role
        self.next_id = 1
        self.process = subprocess.Popen(
            [
                "python3.14",
                str(HOST_WORKER),
                "--role",
                role,
                "--assignment-file",
                str(assignment_file),
                "--report-file",
                str(report_file),
                "--load-generation",
                "11",
            ],
            cwd=ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def execute(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        command_id = f"{self.role}-{self.next_id}"
        self.next_id += 1
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.process.stdin.write(
            json.dumps(
                {
                    "protocol": HOST_PROTOCOL,
                    "command_id": command_id,
                    "operation": operation,
                    "payload": payload,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        assert line, (
            self.process.stderr.read() if self.process.stderr is not None else ""
        )
        response = json.loads(line)
        assert response["ok"] is True, response
        assert response["command_id"] == command_id
        assert response["route_ready"] is False
        return response

    def stop(self) -> None:
        if self.process.poll() is None:
            self.execute("stop", {})
            self.process.wait(timeout=10)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


def _prepare(
    tmp_path: Path,
) -> tuple[Any, list[Any], Any, _HostStageClient, _HostStageClient]:
    deployment = prepare_physical_deployment(tmp_path / "deployment")
    loaded = [
        load_assignment_stage(assignment, report, load_generation=11)
        for assignment, report in zip(
            deployment.assignments, deployment.artifact_reports
        )
    ]
    reference = load_assignment_stage(
        deployment.reference_assignment,
        deployment.reference_report,
        load_generation=11,
    )
    paths: list[tuple[Path, Path]] = []
    for index, name in enumerate(("entry", "final")):
        assignment_file = tmp_path / f"{name}-assignment.json"
        report_file = tmp_path / f"{name}-report.json"
        assignment_file.write_bytes(canonical_json_bytes(deployment.assignments[index]))
        report_file.write_bytes(
            canonical_json_bytes(deployment.artifact_reports[index])
        )
        paths.append((assignment_file, report_file))
    entry = _HostStageClient(
        role="entry", assignment_file=paths[0][0], report_file=paths[0][1]
    )
    final = _HostStageClient(
        role="final", assignment_file=paths[1][0], report_file=paths[1][1]
    )
    return deployment, loaded, reference, entry, final


def _pixel_pack(deployment: Any, loaded: list[Any], run_id: str) -> dict[str, Any]:
    assignment = deployment.assignments[1]
    config = assignment["runtime"]["model_config"]
    tensors = {
        key: value.tolist()
        for key, value in loaded[1].tensors.items()
        if key.startswith("transformer.h.1.")
    }
    return build_stage_pack(
        run_id=run_id,
        deployment_id=assignment["deployment_id"],
        assignment_id=assignment["assignment_id"],
        stage_id="pixel-stage-001",
        model_id=assignment["model_id"],
        resolved_commit=assignment["resolved_commit"],
        manifest_digest=assignment["manifest_digest"],
        parent_assignment_digest="sha256:"
        + hashlib.sha256(canonical_json_bytes(assignment)).hexdigest(),
        parent_load_proof_digest="sha256:"
        + hashlib.sha256(canonical_json(loaded[1].proof).encode("utf-8")).hexdigest(),
        start_layer=1,
        end_layer_exclusive=2,
        n_head=assignment["runtime"]["model_config"]["n_head"],
        hidden_size=assignment["runtime"]["model_config"]["n_embd"],
        epsilon=assignment["runtime"]["model_config"]["layer_norm_epsilon"],
        activation_function=config["activation_function"],
        scale_attn_weights=config["scale_attn_weights"],
        scale_attn_by_inverse_layer_idx=config["scale_attn_by_inverse_layer_idx"],
        reorder_and_upcast_attn=config["reorder_and_upcast_attn"],
        add_cross_attention=config["add_cross_attention"],
        tensors=tensors,
    )


def _quantized_argmax(values: list[float], *, quantum: float = 1e-5) -> int:
    """Choose a cross-runtime-stable token after explicit logit quantization."""

    buckets = [round(float(value) / quantum) for value in values]
    return max(range(len(buckets)), key=buckets.__getitem__)


def _run_split(
    *,
    entry: _HostStageClient,
    final: _HostStageClient,
    reference: Any,
    pixel_reference_stage: Any,
    pixel_execute: Callable[[str, list[list[float]]], dict[str, Any]],
    steps: int = 4,
) -> dict[str, Any]:
    context = [1, 2, 3]
    quantized_actual_tokens: list[int] = []
    quantized_expected_tokens: list[int] = []
    max_logit_error = 0.0
    max_intermediate_error = 0.0
    pixel_results: list[dict[str, Any]] = []
    verification_records: list[dict[str, Any]] = []
    config = pixel_reference_stage.proof["runtime"]["model_config"]
    for index in range(steps):
        entry_result = entry.execute("entry", {"token_ids": context})
        entry_hidden = entry_result["result"]["output"]
        pixel_result = pixel_execute(f"token-{index}", entry_hidden)
        pixel_output = pixel_result["output"]
        pixel_results.append(pixel_result)

        expected_hidden_array, _ = _gpt2_block_with_kv(
            mx.array([entry_hidden], dtype=mx.float32),
            pixel_reference_stage.tensors,
            prefix="transformer.h.1.",
            n_head=int(config["n_head"]),
            epsilon=float(config["layer_norm_epsilon"]),
            past=None,
        )
        mx.eval(expected_hidden_array)
        expected_hidden = expected_hidden_array.tolist()[0]
        assert len(pixel_output) == len(expected_hidden) == len(context)
        assert all(
            len(actual_row) == len(expected_row) == int(config["n_embd"])
            for actual_row, expected_row in zip(pixel_output, expected_hidden)
        )
        hidden_error = max(
            abs(float(actual) - float(wanted))
            for actual_row, expected_row in zip(pixel_output, expected_hidden)
            for actual, wanted in zip(actual_row, expected_row)
        )
        max_intermediate_error = max(max_intermediate_error, hidden_error)

        final_result = final.execute("final", {"hidden": pixel_output})
        actual_logits = final_result["result"]["output"]
        expected = execute_loaded_stage(
            reference,
            token_ids=mx.array((tuple(context),), dtype=mx.uint32),
        )
        mx.eval(expected)
        expected_logits = expected.tolist()[0]
        assert len(actual_logits) == len(expected_logits) == len(context)
        assert all(
            len(actual_row) == len(expected_row)
            for actual_row, expected_row in zip(actual_logits, expected_logits)
        )
        logit_error = max(
            abs(float(actual) - float(wanted))
            for actual_row, expected_row in zip(actual_logits, expected_logits)
            for actual, wanted in zip(actual_row, expected_row)
        )
        max_logit_error = max(max_logit_error, logit_error)
        actual_token = _quantized_argmax(actual_logits[-1])
        expected_token = _quantized_argmax(expected_logits[-1])
        quantized_actual_tokens.append(actual_token)
        quantized_expected_tokens.append(expected_token)
        verification_records.append(
            {
                "context": list(context),
                "entry_hidden": entry_hidden,
                "pixel_output": pixel_output,
                "expected_pixel_output": expected_hidden,
                "actual_logits": actual_logits,
                "expected_logits": expected_logits,
                "intermediate_error": hidden_error,
                "logit_error": logit_error,
            }
        )
        context.append(actual_token)
    return {
        "quantized_actual_tokens": quantized_actual_tokens,
        "quantized_expected_tokens": quantized_expected_tokens,
        "token_selection": "round-to-even quantized argmax",
        "logit_quantum": 1e-5,
        "max_logit_error": max_logit_error,
        "max_intermediate_error": max_intermediate_error,
        "entry_process": entry.execute("entry", {"token_ids": [1]})["pid"],
        "final_process": final.execute(
            "final", {"hidden": pixel_results[0]["output"][:1]}
        )["pid"],
        "pixel_results": pixel_results,
        "verification_records": verification_records,
    }


def test_three_stage_split_matches_monolithic_reference_before_live_pixel(
    tmp_path: Path,
) -> None:
    deployment, loaded, reference, entry, final = _prepare(tmp_path)
    run_id = str(uuid.uuid4())
    stage = PixelStage.from_document(_pixel_pack(deployment, loaded, run_id))
    try:
        result = _run_split(
            entry=entry,
            final=final,
            reference=reference,
            pixel_reference_stage=loaded[1],
            pixel_execute=lambda request_id, hidden: {
                "output": stage.execute(
                    request_id=request_id,
                    assignment_id=stage.document["assignment_id"],
                    stage_id=stage.document["stage_id"],
                    hidden=hidden,
                ),
                "request_count": stage.request_count,
                "route_ready": False,
            },
        )
        assert (
            result["quantized_actual_tokens"] == result["quantized_expected_tokens"]
        ), (
            result["quantized_actual_tokens"],
            result["quantized_expected_tokens"],
            result["max_logit_error"],
        )
        assert result["max_intermediate_error"] < 1e-6
        assert result["max_logit_error"] < 1e-6
        assert result["entry_process"] != result["final_process"]
        assert stage.request_count == 4
    finally:
        entry.stop()
        final.stop()


def _bridge_ok(result: dict[str, Any]) -> str:
    assert result["exit_code"] == 0, {
        "exit_code": result["exit_code"],
        "stderr": result["stderr"][-1000:],
    }
    return result["stdout"]


def _deploy_bytes(
    bridge: TermuxBridgeClient,
    python: str,
    destination: str,
    content: bytes,
) -> None:
    encoded = base64.b64encode(content).decode("ascii")
    code = (
        "import base64,os,sys; from pathlib import Path; "
        "p=Path(sys.argv[1]); p.parent.mkdir(mode=0o700,parents=True,exist_ok=True); "
        "p.write_bytes(base64.b64decode(sys.argv[2],validate=True)); os.chmod(p,0o600)"
    )
    _bridge_ok(
        bridge.run_argv(
            [python, "-c", code, destination, encoded], timeout_seconds=20.0
        )
    )


def _new_stage_token(
    bridge: TermuxBridgeClient,
    python: str,
    token_path: str,
) -> str:
    code = (
        "import os,secrets,sys; from pathlib import Path; p=Path(sys.argv[1]); "
        "p.write_text(secrets.token_urlsafe(48),encoding='ascii'); os.chmod(p,0o600); "
        "print(p.read_text(encoding='ascii'),end='')"
    )
    token = _bridge_ok(
        bridge.run_argv([python, "-c", code, token_path], timeout_seconds=10.0)
    ).strip()
    assert len(token) >= 32
    return token


def _start_pixel_worker(
    bridge: TermuxBridgeClient,
    python: str,
    *,
    worker_path: str,
    pack_path: str,
    token_path: str,
    evidence_path: str,
    bind: str,
) -> int:
    result = bridge.run_argv(
        [
            python,
            worker_path,
            "--pack",
            pack_path,
            "--token-file",
            token_path,
            "--evidence-file",
            evidence_path,
            "--bind",
            bind,
            "--port",
            "9018",
        ],
        timeout_seconds=10.0,
        detach=True,
    )
    return int(result["pid"])


def _wait_pixel(client: PixelStageClient, timeout: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return client.health()
        except PixelStageClientError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def _read_phone_json(
    bridge: TermuxBridgeClient,
    python: str,
    path: str,
) -> dict[str, Any]:
    code = "import sys; from pathlib import Path; print(Path(sys.argv[1]).read_text(encoding='ascii'),end='')"
    raw = _bridge_ok(bridge.run_argv([python, "-c", code, path], timeout_seconds=10.0))
    value = json.loads(raw)
    assert isinstance(value, dict)
    return value


def _document_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _adb_identity(serial: str) -> dict[str, str]:
    completed = subprocess.run(
        [
            "adb",
            "-s",
            serial,
            "shell",
            "printf 'model='; getprop ro.product.model; printf 'abi='; getprop ro.product.cpu.abi; printf 'sdk='; getprop ro.build.version.sdk; printf 'serial='; getprop ro.boot.serialno; printf 'boot_id='; cat /proc/sys/kernel/random/boot_id",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    identity: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            identity[key] = value.strip()
    assert identity.get("model") == "Pixel 8 Pro"
    assert identity.get("abi") == "arm64-v8a"
    assert identity.get("serial") == serial
    return identity


def _bridge_device_identity(bridge: TermuxBridgeClient) -> dict[str, str]:
    properties = {
        "model": "ro.product.model",
        "abi": "ro.product.cpu.abi",
        "sdk": "ro.build.version.sdk",
    }
    identity = {
        name: _bridge_ok(
            bridge.run_argv(["/system/bin/getprop", property_name], timeout_seconds=5.0)
        ).strip()
        for name, property_name in properties.items()
    }
    identity["boot_id"] = _bridge_ok(
        bridge.run_argv(
            [
                "python",
                "-c",
                "from pathlib import Path; print(Path('/proc/sys/kernel/random/boot_id').read_text().strip(),end='')",
            ],
            timeout_seconds=5.0,
        )
    ).strip()
    assert identity["model"] == "Pixel 8 Pro"
    assert identity["abi"] == "arm64-v8a"
    assert identity["boot_id"]
    return identity


def _phone_pid_alive(bridge: TermuxBridgeClient, python: str, pid: int) -> bool:
    code = (
        "import os,sys; pid=int(sys.argv[1]); "
        "\ntry: os.kill(pid,0); print('alive',end='')"
        "\nexcept ProcessLookupError: print('dead',end='')"
    )
    return (
        _bridge_ok(
            bridge.run_argv([python, "-c", code, str(pid)], timeout_seconds=5.0)
        ).strip()
        == "alive"
    )


def _wait_phone_pid_exit(
    bridge: TermuxBridgeClient, python: str, pid: int, timeout: float = 10.0
) -> None:
    deadline = time.monotonic() + timeout
    while _phone_pid_alive(bridge, python, pid):
        if time.monotonic() >= deadline:
            raise AssertionError("pixel worker did not exit")
        time.sleep(0.1)


def _wait_worker_unreachable(client: PixelStageClient, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while True:
        try:
            client.health()
        except PixelStageClientError as exc:
            if str(exc) == "pixel_transport_failed":
                return str(exc)
        if time.monotonic() >= deadline:
            raise AssertionError("pixel worker remained reachable")
        time.sleep(0.1)


def _archive_evidence(
    directory: Path,
    *,
    documents: dict[str, dict[str, Any]],
) -> None:
    if directory.exists() or directory.is_symlink():
        raise AssertionError("evidence directory must be new")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    lines: list[str] = []
    for name, value in documents.items():
        assert name.endswith(".json") and "/" not in name
        raw = canonical_json_bytes(value)
        path = directory / name
        with path.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        lines.append(f"{hashlib.sha256(raw).hexdigest()}  {name}")
    checksums = directory / "SHA256SUMS"
    with checksums.open("x", encoding="ascii") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


@pytest.mark.skipif(
    os.environ.get("MYCELIUM_PIXEL8_LIVE") != "1",
    reason="requires explicitly enabled, authenticated Pixel 8 Pro",
)
def test_live_m4_pixel8_m4_distributed_inference_with_restart(
    tmp_path: Path,
) -> None:
    bridge_url = os.environ["MYCELIUM_PIXEL8_BRIDGE_URL"]
    worker_url = os.environ["MYCELIUM_PIXEL8_WORKER_URL"]
    bridge_token_file = Path(os.environ["MYCELIUM_PIXEL8_BRIDGE_TOKEN_FILE"])
    adb_serial = os.environ["MYCELIUM_PIXEL8_ADB_SERIAL"]
    evidence_directory = Path(
        os.environ.get("MYCELIUM_PIXEL8_EVIDENCE_DIR", str(tmp_path / "evidence"))
    )
    bridge_token = bridge_token_file.read_text(encoding="ascii").strip()
    bridge_host = urllib.parse.urlsplit(bridge_url).hostname
    worker_host = urllib.parse.urlsplit(worker_url).hostname
    assert bridge_host == worker_host and bridge_host is not None
    bridge = TermuxBridgeClient(bridge_url, token=bridge_token)
    bridge_health = bridge.health()
    assert bridge_health["allow_shell"] is False
    assert bridge.unauthenticated_rejected() is True
    bridge_identity = _bridge_device_identity(bridge)
    try:
        phone_identity = _adb_identity(adb_serial)
        assert all(
            phone_identity[key] == value for key, value in bridge_identity.items()
        )
        adb_live_identity_confirmed = True
    except subprocess.CalledProcessError:
        phone_identity = dict(bridge_identity)
        adb_live_identity_confirmed = False

    discovery = _bridge_ok(
        bridge.run_argv(
            [
                "python",
                "-c",
                "import json,os,platform,sys; print(json.dumps({'python':sys.executable,'home':os.path.expanduser('~'),'host':platform.node(),'machine':platform.machine(),'system':platform.system()}))",
            ],
            timeout_seconds=10.0,
        )
    )
    phone = json.loads(discovery)
    assert phone["machine"] in ("aarch64", "arm64")
    assert phone["system"] == "Android"
    python = phone["python"]
    run_id = str(uuid.uuid4())
    phone_root = f"{phone['home']}/mycelium-mobile-lab/current-integration-{run_id}"
    worker_path = f"{phone_root}/pixel_stage.py"
    pack_path = f"{phone_root}/stage-pack.json"
    token_path = f"{phone_root}/stage-token"
    evidence_path = f"{phone_root}/stage-evidence.json"
    worker_pid = 0
    entry: _HostStageClient | None = None
    final: _HostStageClient | None = None
    client: PixelStageClient | None = None
    try:
        deployment, loaded, reference, entry, final = _prepare(tmp_path)
        pack = _pixel_pack(deployment, loaded, run_id)
        worker_source = (ROOT / "mycelium_mobile" / "pixel_stage.py").read_bytes()
        worker_source_digest = "sha256:" + hashlib.sha256(worker_source).hexdigest()
        expected_identity = {
            "run_id": pack["run_id"],
            "deployment_id": pack["deployment_id"],
            "assignment_id": pack["assignment_id"],
            "stage_id": pack["stage_id"],
            "pack_digest": pack["pack_digest"],
            "parent_assignment_digest": pack["parent_assignment_digest"],
            "parent_load_proof_digest": pack["parent_load_proof_digest"],
            "worker_source_digest": worker_source_digest,
            "boot_id": bridge_identity["boot_id"],
        }
        _deploy_bytes(
            bridge,
            python,
            worker_path,
            worker_source,
        )
        _deploy_bytes(
            bridge,
            python,
            pack_path,
            canonical_json_bytes(pack),
        )

        lifecycle_results: list[dict[str, Any]] = []
        lifecycle_evidence: list[dict[str, Any]] = []
        lifecycle_health: list[dict[str, Any]] = []
        lifecycle_events: list[dict[str, Any]] = []
        runtime_ids: list[str] = []
        for lifecycle in range(2):
            stage_token = _new_stage_token(bridge, python, token_path)
            worker_pid = _start_pixel_worker(
                bridge,
                python,
                worker_path=worker_path,
                pack_path=pack_path,
                token_path=token_path,
                evidence_path=evidence_path,
                bind=worker_url.removeprefix("http://").split(":", 1)[0],
            )
            client = PixelStageClient(
                worker_url,
                token=stage_token,
                expected_identity=expected_identity,
                hidden_size=int(pack["hidden_size"]),
                timeout=20.0,
            )
            health = _wait_pixel(client)
            lifecycle_health.append(health)
            runtime_ids.append(health["runtime_instance_id"])
            assert health["request_count"] == 0

            unauthorized_client = PixelStageClient(
                worker_url,
                token="x" * 48,
                expected_identity=expected_identity,
                hidden_size=int(pack["hidden_size"]),
                timeout=5.0,
            )
            auth_status, auth_response = unauthorized_client._request("GET", "/health")
            assert auth_status == 401
            assert auth_response == {"error": "unauthorized"}
            lifecycle_events.append(
                {
                    "lifecycle": lifecycle,
                    "event": "authentication_rejected",
                    "http_status": auth_status,
                    "error": auth_response["error"],
                    "observed_unix_ns": time.time_ns(),
                }
            )
            wrong_hidden = [[0.0] * int(pack["hidden_size"])]
            status, rejection = client._request(
                "POST",
                "/execute",
                payload={
                    "protocol": "mycelium.pixel_stage_request.v1",
                    "request_id": "wrong-assignment",
                    "assignment_id": "wrong-assignment",
                    "stage_id": pack["stage_id"],
                    "hidden": wrong_hidden,
                    "input_digest": _document_digest(wrong_hidden),
                },
            )
            assert status == 400
            assert rejection["error"] == "request_assignment_mismatch"
            lifecycle_events.append(
                {
                    "lifecycle": lifecycle,
                    "event": "assignment_rejected",
                    "http_status": status,
                    "error": rejection["error"],
                    "request_count": 0,
                    "observed_unix_ns": time.time_ns(),
                }
            )
            assert client.health()["request_count"] == 0

            result = _run_split(
                entry=entry,
                final=final,
                reference=reference,
                pixel_reference_stage=loaded[1],
                pixel_execute=lambda request_id, hidden, lifecycle=lifecycle: (
                    client.execute(
                        request_id=f"lifecycle-{lifecycle}-{request_id}",
                        assignment_id=pack["assignment_id"],
                        stage_id=pack["stage_id"],
                        hidden=hidden,
                    )
                ),
            )
            assert (
                result["quantized_actual_tokens"] == result["quantized_expected_tokens"]
            )
            assert result["max_intermediate_error"] < 1e-6
            assert result["max_logit_error"] < 1e-6
            assert result["entry_process"] != result["final_process"]
            assert client.health()["request_count"] == 4
            evidence = _read_phone_json(bridge, python, evidence_path)
            assert evidence["process_id"] == worker_pid
            assert evidence["runtime_instance_id"] == runtime_ids[-1]
            assert evidence["request_count"] == 4
            assert evidence["route_ready"] is False
            assert evidence["process_host_id"] == phone["host"]
            assert evidence["machine"] == phone["machine"]
            assert all(
                evidence[field] == value for field, value in expected_identity.items()
            )
            assert (
                _document_digest(evidence)
                == result["pixel_results"][-1]["evidence_digest"]
            )
            lifecycle_results.append(result)
            lifecycle_evidence.append(evidence)
            old_pid = worker_pid
            old_client = client
            shutdown_response = client.shutdown()
            _wait_phone_pid_exit(bridge, python, old_pid)
            transport_error = _wait_worker_unreachable(old_client)
            lifecycle_events.append(
                {
                    "lifecycle": lifecycle,
                    "event": "worker_shutdown_verified",
                    "shutdown_response": shutdown_response,
                    "process_id": old_pid,
                    "pid_alive": False,
                    "endpoint_error": transport_error,
                    "runtime_instance_id": runtime_ids[-1],
                    "observed_unix_ns": time.time_ns(),
                }
            )
            client = None
            worker_pid = 0

        assert runtime_ids[0] != runtime_ids[1]
        assert (
            lifecycle_evidence[0]["process_id"] != lifecycle_evidence[1]["process_id"]
        )
        summary = {
            "protocol": "mycelium.pixel8_distributed_qualification.v1",
            "route_ready": False,
            "claim_boundary": "local MLX subprocess -> authenticated Pixel HTTP derived decoder substage -> local MLX subprocess; not production Router transport",
            "production_router_path": False,
            "transport_scheme": "stdin/http/stdin",
            "bootstrap_bridge_authority": "generic argv execution; RCE-equivalent credential; used only for ephemeral deployment and cleanup",
            "adb_live_identity_confirmed": adb_live_identity_confirmed,
            "bridge_worker_boot_id_bound": True,
            "run_id": run_id,
            "deployment_id": pack["deployment_id"],
            "pack_digest": pack["pack_digest"],
            "parent_assignment_digest": pack["parent_assignment_digest"],
            "parent_load_proof_digest": pack["parent_load_proof_digest"],
            "worker_source_digest": worker_source_digest,
            "pixel": {
                "model": phone_identity["model"],
                "abi": phone_identity["abi"],
                "sdk": phone_identity["sdk"],
                "tailscale_url": worker_url,
                "process_host_id": lifecycle_evidence[-1]["process_host_id"],
                "machine": lifecycle_evidence[-1]["machine"],
            },
            "local_entry_pid": lifecycle_results[-1]["entry_process"],
            "local_final_pid": lifecycle_results[-1]["final_process"],
            "pixel_runtime_instance_ids": runtime_ids,
            "pixel_process_ids": [item["process_id"] for item in lifecycle_evidence],
            "quantized_actual_tokens": [
                item["quantized_actual_tokens"] for item in lifecycle_results
            ],
            "quantized_expected_tokens": [
                item["quantized_expected_tokens"] for item in lifecycle_results
            ],
            "token_selection": "round-to-even quantized argmax",
            "logit_quantum": 1e-5,
            "maximum_intermediate_error": max(
                item["max_intermediate_error"] for item in lifecycle_results
            ),
            "maximum_logit_error": max(
                item["max_logit_error"] for item in lifecycle_results
            ),
            "negative_auth_rejected": sum(
                event["event"] == "authentication_rejected"
                and event["http_status"] == 401
                and event["error"] == "unauthorized"
                for event in lifecycle_events
            )
            == 2,
            "negative_assignment_rejected_by_worker": sum(
                event["event"] == "assignment_rejected" for event in lifecycle_events
            )
            == 2,
            "old_worker_exit_and_endpoint_unreachable_confirmed": sum(
                event["event"] == "worker_shutdown_verified"
                and event["pid_alive"] is False
                and event["endpoint_error"] == "pixel_transport_failed"
                for event in lifecycle_events
            )
            == 2,
            "restart_confirmed": len(set(runtime_ids)) == 2
            and len({item["process_id"] for item in lifecycle_evidence}) == 2,
        }
        for field in (
            "negative_auth_rejected",
            "negative_assignment_rejected_by_worker",
            "old_worker_exit_and_endpoint_unreachable_confirmed",
            "restart_confirmed",
        ):
            assert summary[field] is True
        _archive_evidence(
            evidence_directory,
            documents={
                "summary.json": summary,
                "derived-stage-pack.json": pack,
                "parent-assignment.json": deployment.assignments[1],
                "parent-load-proof.json": json.loads(canonical_json(loaded[1].proof)),
                "pixel-first-health.json": lifecycle_health[0],
                "pixel-second-health.json": lifecycle_health[1],
                "pixel-first-evidence.json": lifecycle_evidence[0],
                "pixel-second-evidence.json": lifecycle_evidence[1],
                "pixel-first-verification.json": lifecycle_results[0],
                "pixel-second-verification.json": lifecycle_results[1],
                "lifecycle-events.json": {
                    "protocol": "mycelium.pixel8_lifecycle_events.v1",
                    "events": lifecycle_events,
                },
            },
        )
    finally:
        if client is not None:
            try:
                client.shutdown()
            except PixelStageClientError:
                pass
        if worker_pid:
            kill_code = (
                "import os,signal,sys; pid=int(sys.argv[1]); "
                "os.kill(pid,signal.SIGTERM) if pid > 1 else None"
            )
            try:
                bridge.run_argv(
                    [python, "-c", kill_code, str(worker_pid)],
                    timeout_seconds=5.0,
                )
            except Exception:
                pass
        if entry is not None:
            entry.stop()
        if final is not None:
            final.stop()
        cleanup_code = (
            "import shutil,sys; from pathlib import Path; "
            "root=Path(sys.argv[1]).resolve(); allowed=(Path.home()/'mycelium-mobile-lab').resolve(); "
            "assert root.parent == allowed and root.name.startswith('current-integration-'); "
            "shutil.rmtree(root,ignore_errors=False)"
        )
        try:
            bridge.run_argv(
                [python, "-c", cleanup_code, phone_root], timeout_seconds=10.0
            )
        except Exception:
            pass
