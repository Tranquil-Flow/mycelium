#!/usr/bin/env python3
"""Executable qualification tests for two spawned assignment-bound MLX loads."""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import model_manifest
import weight_provisioning
from two_process_runtime_qualification import (
    CHILD_RESULT_FIELDS,
    LOAD_PROOF_FIELDS,
    NEGATIVE_CLAIMS,
    QUALIFICATION_PROTOCOL,
    QualificationError,
    _spawn_assignment_loads,
    run_qualification,
)


EXPECTED_RANGES = [
    {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
    {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
]
EXPECTED_SHARDS = [
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
]


def _never_respond(
    send_connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
) -> None:
    del send_connection, assignment, artifact_report, load_generation
    time.sleep(60)


def _assert_process_is_gone(pid: int) -> None:
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_two_spawned_processes_load_exact_disjoint_assignments_offline(
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
    assert "assignment-bound" in result["claim"]
    assert result["negative_claims"] == list(NEGATIVE_CLAIMS)
    assert any("distributed inference" in claim for claim in result["negative_claims"])
    assert any("memory residency" in claim for claim in result["negative_claims"])

    process_evidence = result["process_evidence"]
    assert process_evidence["start_method"] == "spawn"
    assert process_evidence["parent_pid"] == os.getpid()
    child_pids = process_evidence["child_pids"]
    assert len(child_pids) == len(set(child_pids)) == 2
    assert process_evidence["parent_pid"] not in child_pids
    assert process_evidence["exit_codes"] == [0, 0]

    assert result["coverage"] == {
        "num_layers": 2,
        "ranges": EXPECTED_RANGES,
        "disjoint": True,
        "complete": True,
    }
    assert result["model"]["format"] == "safetensors_sharded"
    assert result["model"]["num_layers"] == 2
    assert result["model"]["generated_locally"] is True

    offline = result["offline_evidence"]
    assert offline["local_files_only"] is True
    assert offline["artifact_reports_validated"] is True
    assert offline["worker_guard_scope"] == (
        "immediately_before_assignment_load_until_child_exit"
    )
    assert "socket.__new__" in offline["worker_denied_audit_events"]
    assert "socket.sendto" in offline["worker_denied_audit_events"]
    assert offline["network_download_bytes"] == 0
    assert offline["cache_hit_bytes"] == offline["expected_bytes"]
    assert offline["artifact_request_count"] == 2
    assert offline["requested_files"] == EXPECTED_SHARDS

    children = result["children"]
    assert [child["assigned_range"] for child in children] == EXPECTED_RANGES
    assert [child["pid"] for child in children] == child_pids
    assert [child["node_id"] for child in children] == ["node-a", "node-b"]
    assert set(children[0]["proof"]["loaded_tensor_keys"]).isdisjoint(
        children[1]["proof"]["loaded_tensor_keys"]
    )
    for child in children:
        assert set(child) == CHILD_RESULT_FIELDS
        assert child["audited_network_event_count"] == 0
        proof = child["proof"]
        assert set(proof) == LOAD_PROOF_FIELDS
        assert child["assignment_id"] == proof["assignment_id"]
        assert child["node_id"] == proof["node_id"]
        assert child["assigned_range"] == proof["loaded_range"]
        assert proof["load_generation"] == 17
        assert proof["route_ready"] is False
        assert proof["claim_boundary"].endswith(
            "no route challenge or distributed inference claim"
        )
        json.dumps(child, sort_keys=True, separators=(",", ":"), allow_nan=False)

    assert [child["proof"]["probe_shape"] for child in children] == [
        [1, 3, 4],
        [1, 3, 7],
    ]
    assert result["proof_shape"] == {
        "child_result_fields": sorted(CHILD_RESULT_FIELDS),
        "load_proof_fields": sorted(LOAD_PROOF_FIELDS),
        "probe_shapes_by_node": {"node-a": [1, 3, 4], "node-b": [1, 3, 7]},
    }

    artifact_root = tmp_path / "qualification"
    assert (artifact_root / "config.json").is_file()
    index = json.loads(
        (artifact_root / "model.safetensors.index.json").read_text(encoding="utf-8")
    )
    assert sorted(set(index["weight_map"].values())) == EXPECTED_SHARDS
    assert all((artifact_root / shard).is_file() for shard in EXPECTED_SHARDS)


def test_proof_shape_and_model_outputs_are_deterministic_across_runs(
    tmp_path: Path,
) -> None:
    first = run_qualification(tmp_path / "first", timeout_seconds=30.0)
    second = run_qualification(tmp_path / "second", timeout_seconds=30.0)

    assert first["proof_shape"] == second["proof_shape"]
    assert first["model"] == second["model"]
    for first_child, second_child in zip(first["children"], second["children"]):
        first_proof = first_child["proof"]
        second_proof = second_child["proof"]
        assert first_proof["loaded_tensor_keys"] == second_proof["loaded_tensor_keys"]
        assert first_proof["loaded_tensor_digest"] == second_proof["loaded_tensor_digest"]
        assert first_proof["probe_shape"] == second_proof["probe_shape"]
        assert first_proof["probe_digest"] == second_proof["probe_digest"]


def test_timeout_terminates_and_reaps_spawned_children() -> None:
    with pytest.raises(QualificationError, match="timed out") as raised:
        _spawn_assignment_loads(
            [({}, {})],
            load_generation=17,
            timeout_seconds=0.25,
            worker_target=_never_respond,
        )

    assert raised.value.child_pids
    for pid in raised.value.child_pids:
        _assert_process_is_gone(pid)
    assert all(
        child.pid not in raised.value.child_pids
        for child in multiprocessing.active_children()
    )


def test_json_cli_emits_one_complete_qualification_document(tmp_path: Path) -> None:
    script = Path(__file__).with_name("two_process_runtime_qualification.py")
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
        timeout=45,
    )

    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert document["protocol"] == QUALIFICATION_PROTOCOL
    assert document["qualified"] is True
    assert document["route_ready"] is False
    assert document["process_evidence"]["start_method"] == "spawn"
    assert len(document["children"]) == 2
    assert completed.stderr == ""


def test_worker_network_guard_rejects_socket_creation() -> None:
    script = """
import socket
from two_process_runtime_qualification import _install_network_audit_guard

events = []
_install_network_audit_guard(events)
try:
    socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
except RuntimeError as exc:
    print(str(exc))
    print(events[-1])
else:
    raise SystemExit("socket creation unexpectedly allowed")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "network access denied during offline load: socket.__new__",
        "socket.__new__",
    ]
