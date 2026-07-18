#!/usr/bin/env python3
"""Executable Phase 6 qualification tests for two spawned MLX Router workers."""

from __future__ import annotations

import json
import multiprocessing
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

import model_manifest
import two_process_inference_qualification as qualification
import weight_provisioning
from mycelium_router.contracts import HopHeader, RuntimeResult
from mycelium_router.payloads import decode_activation, encode_activation
from mycelium_router.transports.loopback_socket import LoopbackSocketMesh
from mycelium_router.wire import decode_frame, encode_frame
from two_process_inference_qualification import (
    CLAIM_BOUNDARY,
    NEGATIVE_CLAIMS,
    QUALIFICATION_PROTOCOL,
    QualificationError,
    _MLXWorkerProxy,
    _RuntimeWorkerSet,
    _WorkerChannel,
    _cleanup_processes,
    _require_parity,
    _spawn_runtime_workers,
    run_qualification,
)


EXPECTED_RANGES = [
    {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
    {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
]


def _never_respond(
    connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
) -> None:
    del connection, assignment, artifact_report, load_generation
    time.sleep(60)



def _crash_or_wait(
    connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
) -> None:
    del connection, artifact_report, load_generation
    if assignment["failure_mode"] == "crash":
        os._exit(23)
    time.sleep(60)


def _malformed_load_or_wait(
    connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
) -> None:
    del artifact_report, load_generation
    if assignment["failure_mode"] == "malformed":
        connection.send({"protocol": "attacker.v1", "kind": "load_proof"})
    time.sleep(60)


def _malformed_runtime_responder(connection: Any) -> None:
    connection.recv()
    connection.send({"protocol": "attacker.v1", "kind": "response"})
    time.sleep(60)


def _assert_process_is_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


class _ApparentlyLiveProcess:
    pid = 424242
    exitcode = None

    @staticmethod
    def is_alive() -> bool:
        return True


class _BlockingSendConnection:
    def __init__(self) -> None:
        self.release = threading.Event()

    def send(self, value: object) -> None:
        del value
        self.release.wait()

    def poll(self, timeout: float) -> bool:
        del timeout
        return False

    def close(self) -> None:
        self.release.set()


class _BlockingReceiveConnection:
    def __init__(self) -> None:
        self.release = threading.Event()

    def send(self, value: object) -> None:
        del value

    def poll(self, timeout: float) -> bool:
        del timeout
        return True

    def recv(self) -> object:
        self.release.wait()
        raise EOFError

    def close(self) -> None:
        self.release.set()


class _CleanupProbe:
    def __init__(self, pid: int, *, terminate_raises: bool = False) -> None:
        self.pid = pid
        self.terminate_raises = terminate_raises
        self.alive = True
        self.actions: list[str] = []

    def is_alive(self) -> bool:
        self.actions.append("is_alive")
        return self.alive

    def terminate(self) -> None:
        self.actions.append("terminate")
        if self.terminate_raises:
            self.alive = False
            raise ProcessLookupError
        self.alive = False

    def join(self, timeout: float) -> None:
        del timeout
        self.actions.append("join")

    def kill(self) -> None:
        self.actions.append("kill")
        self.alive = False


class _WrongCardinalityChannel:
    @staticmethod
    def request(operation: str, payload: object) -> tuple[RuntimeResult, ...]:
        del operation, payload
        return (RuntimeResult(True, token_id=1),)


class _TwoItemBatch:
    items = (object(), object())


class _PostCaptureMutationMesh(LoopbackSocketMesh):
    """Corrupt one valid activation after TCP capture but before Router dispatch."""

    mutated = False

    def _dispatch(self, node_id: str, action: int, frame: bytes) -> None:
        decoded = decode_frame(frame)
        if (
            not self.mutated
            and isinstance(decoded.message, HopHeader)
            and decoded.message.source_placement_id == "placement-000"
            and decoded.message.destination_placement_id == "placement-001"
            and decoded.payload
        ):
            envelope = decode_activation(decoded.payload)
            hidden = bytearray(envelope.data)
            first = struct.unpack("<f", hidden[:4])[0]
            hidden[:4] = struct.pack("<f", -first)
            frame = encode_frame(
                decoded.message,
                encode_activation(
                    dtype=envelope.dtype,
                    shape=envelope.shape,
                    data=bytes(hidden),
                ),
            )
            self.mutated = True
        super()._dispatch(node_id, action, frame)



def test_two_persistent_spawned_mlx_workers_execute_real_router_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def network_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(f"network resolver/download called: {args!r} {kwargs!r}")

    monkeypatch.setattr(
        model_manifest, "resolve_huggingface_manifest", network_forbidden
    )
    monkeypatch.setattr(
        weight_provisioning, "fetch_huggingface_file", network_forbidden
    )

    result = run_qualification(tmp_path / "qualification", timeout_seconds=30.0)

    assert result["protocol"] == QUALIFICATION_PROTOCOL
    assert result["qualified"] is True
    assert result["route_ready"] is False
    assert result["claim_boundary"] == CLAIM_BOUNDARY
    assert result["negative_claims"] == list(NEGATIVE_CLAIMS)
    assert "local loopback TCP and bounded parent/child pipes" in CLAIM_BOUNDARY
    assert any("authenticated multi-host" in item for item in NEGATIVE_CLAIMS)
    assert "stage-local KV" in CLAIM_BOUNDARY
    assert not any("complete token history" in item for item in NEGATIVE_CLAIMS)
    assert any("performance" in item for item in NEGATIVE_CLAIMS)

    processes = result["process_evidence"]
    assert processes["start_method"] == "spawn"
    assert processes["persistent_worker_count"] == 2
    assert processes["parent_pid"] == os.getpid()
    assert len(processes["child_pids"]) == len(set(processes["child_pids"])) == 2
    assert processes["parent_pid"] not in processes["child_pids"]
    assert processes["exit_codes"] == [0, 0]
    assert processes["clean_shutdown"] is True
    for pid in processes["child_pids"]:
        _assert_process_is_gone(pid)

    assignments = result["assignments"]
    assert [item["node_id"] for item in assignments] == ["node-a", "node-b"]
    assert [item["assigned_range"] for item in assignments] == EXPECTED_RANGES
    assert len({item["assignment_id"] for item in assignments}) == 2
    assert all(item["load_proof_immutable"] for item in assignments)
    assert all(item["runtime_bound"] for item in assignments)
    assert all(item["load_proof_digest"].startswith("sha256:") for item in assignments)

    path = result["router_path"]
    assert path["route_challenge_passed"] is True
    assert path["path_locked"] is True
    assert path["status_after_prefill"] == "DECODING"
    assert path["locked_nodes"] == ["node-a", "node-b"]
    assert path["locked_stage_ranges"] == EXPECTED_RANGES
    assert path["prefill_chunk_size_tokens"] == 0
    assert path["router_count"] == 2
    assert path["relay"] == "mycelium_router.relay.RelayEngine"
    assert path["topology_provider"] == "PublishedTopologyProvider"
    assert path["device_state_provider"] == "PublishedDeviceStateProvider"
    assert path["capacity_port"] == "InProcessLeaseCapacityPort"
    assert path["capacity_reservations_committed"] == 2

    parity = result["parity"]
    assert parity["all_passed"] is True
    assert parity["prefill"]["passed"] is True
    assert parity["prefill"]["actual_token"] == parity["prefill"]["reference_token"]
    assert len(parity["decode"]["actual_tokens"]) == 8
    assert parity["decode"]["actual_tokens"] == parity["decode"]["reference_tokens"]
    assert parity["decode"]["passed"] is True
    assert parity["decode"]["numeric_tolerance"] == 1e-5
    assert parity["decode"]["max_hidden_abs_error"] <= 1e-5
    assert parity["reference"]["parent_pid"] == os.getpid()
    assert parity["reference"]["api"] == "runtime_loader.execute_loaded_stage"
    assert parity["reference"]["kind"] == "independent_full_model_stage"
    assert parity["reference"]["assigned_range"] == {
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "layer_count": 2,
    }
    assert parity["reference"]["loaded_components"] == [
        "input_embedding",
        "decoder",
        "final_norm",
        "lm_head",
    ]

    wire = result["wire_evidence"]
    assert wire["protocol"] == "mycelium.router_wire.v1"
    assert wire["transport"] == "LoopbackSocketMesh"
    assert wire["bound_hosts"] == ["127.0.0.1"]
    assert wire["connection_count"] > 0
    assert wire["frame_count"] >= wire["activation_frame_count"]
    assert wire["activation_frame_count"] == 9
    assert wire["stage0_to_stage1_within_numeric_tolerance"] is True
    assert wire["numeric_tolerance"] == 1e-5
    assert wire["maximum_absolute_error"] <= wire["numeric_tolerance"]
    assert [item["phase"] for item in wire["activation_frames"]] == [
        "PREFILL",
        *("DECODE",) * 8,
    ]
    assert [item["token_index"] for item in wire["activation_frames"]] == [
        -1,
        *range(1, 9),
    ]
    assert all(
        item["within_numeric_tolerance"] for item in wire["activation_frames"]
    )
    for item in wire["activation_frames"]:
        assert item["maximum_absolute_error"] <= item["numeric_tolerance"]
        assert item["request_id"] == "phase6-local-route-challenge"
        assert item["path_id"] == path["path_id"]
        assert item["path_attempt"] == 0
        assert item["hop_index"] == 1
        assert item["topology_version"] == 1
        assert item["tcp_payload_sha256"].startswith("sha256:")
        assert item["reference_payload_sha256"].startswith("sha256:")
        assert item["tcp_payload_sha256"] == item["stage0_output_sha256"]
        assert item["tcp_payload_sha256"] == item["stage1_input_sha256"]
        assert item["tcp_frame_sha256"].startswith("sha256:")
        assert item["hidden_state_sha256"].startswith("sha256:")

    runtimes = result["runtime_evidence"]
    assert set(runtimes) == {"node-a", "node-b"}
    for node_id, child in runtimes.items():
        assert child["node_id"] == node_id
        assert child["pid"] in processes["child_pids"]
        assert child["runtime_bound"] is True
        assert child["phases"] == ["PREFILL", *("DECODE",) * 8]
        assert child["phase_counts"] == {"PREFILL": 1, "DECODE": 8}
        assert child["rpc_call_count"] == 9
        decode_calls = [call for call in child["calls"] if call["phase"] == "DECODE"]
        assert [call["input_sequence_tokens"] for call in decode_calls] == [1] * 8
        assert [call["position"] for call in decode_calls] == list(range(3, 11))
        assert child["kv"]["mode"] == "stage_local_kv"
        assert child["kv"]["active_state_count"] == 0
        assert child["kv"]["release_counts"]["normal_completion"] == 1

    kv_lifecycle = result["kv_lifecycle"]
    assert kv_lifecycle["prefill_active_states"] == {"node-a": 1, "node-b": 1}
    assert kv_lifecycle["prefill_cached_context_tokens"] == {"node-a": 3, "node-b": 3}
    assert kv_lifecycle["completion_active_states"] == {"node-a": 0, "node-b": 0}
    assert kv_lifecycle["capacity_after_completion"] == 0
    assert kv_lifecycle["cross_request_leakage"] is False

    cleanup = result["cleanup"]
    assert cleanup == {
        "request_completed": True,
        "capacity_released": True,
        "mesh_closed": True,
        "worker_connections_closed": True,
        "workers_reaped": True,
    }
    json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)


def test_timeout_terminates_and_reaps_exactly_two_spawned_workers() -> None:
    with pytest.raises(QualificationError, match="timed out") as raised:
        _spawn_runtime_workers(
            [({}, {}), ({}, {})],
            load_generation=17,
            timeout_seconds=0.25,
            worker_target=_never_respond,
        )

    assert len(raised.value.child_pids) == 2
    for pid in raised.value.child_pids:
        _assert_process_is_gone(pid)
    assert all(
        child.pid not in raised.value.child_pids
        for child in multiprocessing.active_children()
    )


def test_worker_crash_fails_closed_and_reaps_both_children() -> None:
    with pytest.raises(QualificationError, match="exited without load proof") as raised:
        _spawn_runtime_workers(
            [
                ({"failure_mode": "crash"}, {}),
                ({"failure_mode": "wait"}, {}),
            ],
            load_generation=17,
            timeout_seconds=3.0,
            worker_target=_crash_or_wait,
        )

    assert len(raised.value.child_pids) == 2
    for pid in raised.value.child_pids:
        _assert_process_is_gone(pid)
    assert all(
        child.pid not in raised.value.child_pids
        for child in multiprocessing.active_children()
    )


def test_malformed_load_ipc_fails_closed_and_reaps_both_children() -> None:
    with pytest.raises(QualificationError, match="malformed load proof") as raised:
        _spawn_runtime_workers(
            [
                ({"failure_mode": "malformed"}, {}),
                ({"failure_mode": "wait"}, {}),
            ],
            load_generation=17,
            timeout_seconds=3.0,
            worker_target=_malformed_load_or_wait,
        )

    assert len(raised.value.child_pids) == 2
    for pid in raised.value.child_pids:
        _assert_process_is_gone(pid)
    assert all(
        child.pid not in raised.value.child_pids
        for child in multiprocessing.active_children()
    )


def test_malformed_rpc_response_fails_closed_and_reaps_both_children() -> None:
    context = multiprocessing.get_context("spawn")
    processes: list[Any] = []
    channels: list[_WorkerChannel] = []
    for index in range(2):
        parent_connection, child_connection = context.Pipe(duplex=True)
        process = context.Process(
            target=_malformed_runtime_responder,
            args=(child_connection,),
            name=f"malformed-runtime-worker-{index}",
        )
        process.start()
        child_connection.close()
        processes.append(process)
        channels.append(
            _WorkerChannel(
                process=process,
                connection=parent_connection,
                timeout_seconds=1.0,
                proof_evidence={"node_id": f"node-{index}"},
            )
        )

    workers = _RuntimeWorkerSet(channels, processes)
    try:
        with pytest.raises(QualificationError, match="malformed runtime RPC response"):
            channels[0].request("execute", object())
    finally:
        workers.abort()

    assert all(channel.closed for channel in channels)
    for pid in workers.child_pids:
        _assert_process_is_gone(pid)
    assert all(
        child.pid not in workers.child_pids
        for child in multiprocessing.active_children()
    )


@pytest.mark.parametrize(
    "connection_factory",
    [_BlockingSendConnection, _BlockingReceiveConnection],
)
def test_rpc_deadline_bounds_blocking_pipe_io(connection_factory: type[Any]) -> None:
    connection = connection_factory()
    channel = _WorkerChannel(
        process=_ApparentlyLiveProcess(),
        connection=connection,
        timeout_seconds=0.05,
        proof_evidence={"node_id": "node-a"},
    )
    outcome: list[BaseException] = []
    completed = threading.Event()

    def invoke() -> None:
        try:
            channel.request("execute", object())
        except BaseException as exc:
            outcome.append(exc)
        finally:
            completed.set()

    caller = threading.Thread(target=invoke, daemon=True)
    started = time.monotonic()
    caller.start()
    bounded = completed.wait(timeout=0.25)
    elapsed = time.monotonic() - started
    connection.close()
    caller.join(timeout=0.25)

    assert bounded, f"RPC remained blocked for {elapsed:.3f}s"
    assert len(outcome) == 1
    assert isinstance(outcome[0], QualificationError)
    assert "timed out" in str(outcome[0])
    assert channel.closed is True


def test_process_cleanup_continues_after_benign_process_race() -> None:
    vanished = _CleanupProbe(111, terminate_raises=True)
    survivor = _CleanupProbe(222)

    _cleanup_processes([vanished, survivor])

    assert "terminate" in vanished.actions
    assert "terminate" in survivor.actions
    assert "join" in survivor.actions
    assert survivor.alive is False


def test_destination_worker_input_must_match_captured_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qualification, "LoopbackSocketMesh", _PostCaptureMutationMesh)

    with pytest.raises(
        QualificationError,
        match="destination runtime input differs from captured wire activation",
    ):
        run_qualification(tmp_path / "mutated", timeout_seconds=30.0)


def test_batch_rpc_rejects_wrong_result_cardinality() -> None:
    proxy = _MLXWorkerProxy(cast(Any, _WrongCardinalityChannel()))

    with pytest.raises(QualificationError, match="batch result cardinality"):
        proxy.execute_batch(cast(Any, _TwoItemBatch()))


def test_parity_mismatch_fails_closed() -> None:
    with pytest.raises(QualificationError, match="decode token parity mismatch"):
        _require_parity([1, 2], [1, 3], "decode token")
    _require_parity(RuntimeResult(True, token_id=2), RuntimeResult(True, token_id=2), "runtime")


def test_worker_count_other_than_two_is_rejected_before_spawn() -> None:
    with pytest.raises(QualificationError, match="exactly two runtime worker jobs"):
        _spawn_runtime_workers(
            [({}, {})],
            load_generation=17,
            timeout_seconds=1.0,
        )


def test_json_cli_emits_complete_local_route_qualification(tmp_path: Path) -> None:
    script = Path(__file__).with_name("two_process_inference_qualification.py")
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--json",
            "--work-dir",
            str(tmp_path / "cli"),
            "--timeout-seconds",
            "30",
        ],
        cwd=script.parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["protocol"] == QUALIFICATION_PROTOCOL
    assert document["qualified"] is True
    assert document["router_path"]["route_challenge_passed"] is True
    assert document["parity"]["all_passed"] is True
    assert document["process_evidence"]["exit_codes"] == [0, 0]
    assert completed.stderr == ""
