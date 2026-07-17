#!/usr/bin/env python3
"""Offline two-process qualification for assignment-bound MLX runtime loading.

The harness builds a deterministic two-layer GPT-2-compatible sharded
Safetensors checkpoint, compiles its real manifest and two layer assignments,
verifies local artifact reports, and loads each assignment in a separate Python
process created with the ``spawn`` start method.  Its claim stops at the local
stage-load evidence emitted by :mod:`runtime_loader`.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import multiprocessing
import os
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import mlx.core as mx

import runtime_loader
from layer_assignment import compile_layer_assignments, validate_assignment_identity
from model_manifest import compile_model_manifest, manifest_digest_ref
from runtime_contracts import GPT2_DECODER_TENSOR_SUFFIXES
from weight_provisioning import (
    artifact_report_errors,
    provision_assignment,
    sha256_file,
)


QUALIFICATION_PROTOCOL = "mycelium.two_process_runtime_load_qualification.v1"
MODEL_ID = "local/tiny-gpt2-qualification"
RESOLVED_COMMIT = "0123456789abcdef0123456789abcdef01234567"
DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
DEPLOYMENT_EPOCH = 1
LOAD_GENERATION = 17
SHARD_NAMES = (
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
)
CLAIM = (
    "Two independent spawned local Python processes each loaded and "
    "deterministically probed its exact assignment-bound MLX stage from "
    "verified, locally generated artifacts."
)
NEGATIVE_CLAIMS = (
    "No distributed inference was performed or demonstrated.",
    "No activation transfer or end-to-end route challenge was performed.",
    "No route readiness is claimed; route_ready remains false.",
    "No simultaneous, cross-process, or post-exit device-memory residency is claimed.",
    (
        "No complete OS-level network-isolation claim is made; the worker guard "
        "starts immediately before assignment loading."
    ),
)
LOAD_PROOF_FIELDS = frozenset(
    {
        "protocol",
        "deployment_id",
        "deployment_epoch",
        "assignment_id",
        "node_id",
        "model_id",
        "manifest_digest",
        "resolved_commit",
        "loaded_range",
        "loaded_components",
        "loaded_tensor_keys",
        "loaded_tensor_digest",
        "resolved_component_aliases",
        "runtime",
        "runtime_identity",
        "probe_shape",
        "probe_digest",
        "load_generation",
        "control_plane_binding",
        "route_ready",
        "claim_boundary",
    }
)
CHILD_RESULT_FIELDS = frozenset(
    {
        "pid",
        "assignment_id",
        "node_id",
        "assigned_range",
        "audited_network_event_count",
        "proof",
    }
)
_NETWORK_AUDIT_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.bind",
        "socket.connect",
        "socket.connect_ex",
        "socket.getaddrinfo",
        "socket.gethostbyaddr",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.getnameinfo",
        "socket.getservbyname",
        "socket.getservbyport",
        "socket.sendto",
        "urllib.Request",
    }
)


class QualificationError(RuntimeError):
    """A fail-closed qualification failure, optionally bound to child PIDs."""

    def __init__(self, message: str, *, child_pids: Sequence[int] = ()) -> None:
        super().__init__(message)
        self.child_pids = tuple(child_pids)


def _canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _shape_size(shape: tuple[int, ...]) -> int:
    size = 1
    for dimension in shape:
        size *= dimension
    return size


def _values(
    shape: tuple[int, ...], *, offset: int, scale: float
) -> mx.array:
    return (
        mx.arange(_shape_size(shape), dtype=mx.float32).reshape(shape) + offset
    ) * scale


def _layer_tensors(layer: int) -> dict[str, mx.array]:
    """Return all GPT-2 decoder tensors with deterministic finite values."""
    prefix = f"transformer.h.{layer}."
    offset = 100 * (layer + 1)
    tensors = {
        prefix + "ln_1.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_1.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "attn.c_attn.weight": _values(
            (4, 12), offset=offset + 1, scale=0.0002
        ),
        prefix + "attn.c_attn.bias": _values(
            (12,), offset=offset + 2, scale=0.0001
        ),
        prefix + "attn.c_proj.weight": _values(
            (4, 4), offset=offset + 3, scale=0.0002
        ),
        prefix + "attn.c_proj.bias": _values(
            (4,), offset=offset + 4, scale=0.0001
        ),
        prefix + "ln_2.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_2.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "mlp.c_fc.weight": _values(
            (4, 8), offset=offset + 5, scale=0.0002
        ),
        prefix + "mlp.c_fc.bias": _values(
            (8,), offset=offset + 6, scale=0.0001
        ),
        prefix + "mlp.c_proj.weight": _values(
            (8, 4), offset=offset + 7, scale=0.0002
        ),
        prefix + "mlp.c_proj.bias": _values(
            (4,), offset=offset + 8, scale=0.0001
        ),
    }
    expected = {prefix + suffix for suffix in GPT2_DECODER_TENSOR_SUFFIXES}
    if set(tensors) != expected:
        raise QualificationError("generated decoder tensor set is incomplete")
    return tensors


def _model_config() -> dict[str, Any]:
    return {
        "model_type": "gpt2",
        "architectures": ["GPT2LMHeadModel"],
        "n_layer": 2,
        "n_embd": 4,
        "n_head": 2,
        "n_inner": 8,
        "vocab_size": 7,
        "n_positions": 8,
        "layer_norm_epsilon": 1e-5,
        "activation_function": "gelu_new",
        "scale_attn_weights": True,
        "scale_attn_by_inverse_layer_idx": False,
        "reorder_and_upcast_attn": False,
        "add_cross_attention": False,
        "tie_word_embeddings": False,
    }


def _replace_file(path: Path, content: str) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    path.write_text(content, encoding="utf-8")


def _save_shard(path: Path, tensors: dict[str, mx.array]) -> None:
    if path.exists() or path.is_symlink():
        path.unlink()
    mx.save_safetensors(str(path), tensors)
    if not path.is_file() or path.stat().st_nlink != 1:
        raise QualificationError(f"generated shard is not an isolated regular file: {path}")


def _build_local_model(work_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    first_shard = {
        "transformer.wte.weight": _values((7, 4), offset=1, scale=0.01),
        "transformer.wpe.weight": _values((8, 4), offset=2, scale=0.005),
        **_layer_tensors(0),
    }
    second_shard = {
        **_layer_tensors(1),
        "transformer.ln_f.weight": mx.ones((4,), dtype=mx.float32),
        "transformer.ln_f.bias": mx.zeros((4,), dtype=mx.float32),
        "lm_head.weight": _values((7, 4), offset=3, scale=0.007),
    }
    shards = (first_shard, second_shard)
    for shard_name, tensors in zip(SHARD_NAMES, shards):
        _save_shard(work_dir / shard_name, tensors)

    config = _model_config()
    weight_map = {
        tensor_name: shard_name
        for shard_name, tensors in zip(SHARD_NAMES, shards)
        for tensor_name in sorted(tensors)
    }
    checkpoint_index = {
        "metadata": {
            "total_size": sum((work_dir / name).stat().st_size for name in SHARD_NAMES)
        },
        "weight_map": weight_map,
    }
    _replace_file(
        work_dir / "config.json", _canonical_json(config) + "\n"
    )
    _replace_file(
        work_dir / "model.safetensors.index.json",
        _canonical_json(checkpoint_index) + "\n",
    )

    # Compile from the serialized local model metadata rather than from test fixtures.
    serialized_config = json.loads(
        (work_dir / "config.json").read_text(encoding="utf-8")
    )
    serialized_index = json.loads(
        (work_dir / "model.safetensors.index.json").read_text(encoding="utf-8")
    )
    file_metadata = {
        name: {
            "size_bytes": (work_dir / name).stat().st_size,
            "sha256": sha256_file(work_dir / name),
        }
        for name in SHARD_NAMES
    }
    manifest = compile_model_manifest(
        model_id=MODEL_ID,
        requested_revision="offline-generated",
        resolved_commit=RESOLVED_COMMIT,
        config=serialized_config,
        checkpoint_index=serialized_index,
        file_metadata=file_metadata,
    )
    return manifest, checkpoint_index


def _route_for_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    ranges = (
        {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
        {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
    )
    nodes = ("node-a", "node-b")
    return {
        "ok": True,
        "protocol": "mycelium.manual_provisioning_route.v1",
        "claim_boundary": (
            "offline qualification route for assignment compilation only; "
            "not a route-readiness claim"
        ),
        "model": {
            "model_id": manifest["model_id"],
            "num_layers": manifest["num_layers"],
            "manifest_digest": manifest_digest_ref(manifest),
            "resolved_commit": manifest["resolved_commit"],
        },
        "route": [
            {"node_id": node, "range": copy.deepcopy(layer_range)}
            for node, layer_range in zip(nodes, ranges)
        ],
        "node_order": list(nodes),
    }


def _control_plane_binding() -> dict[str, Any]:
    return {
        "protocol": "mycelium.control_plane_binding.v1",
        "evidence_bundle_digest": "sha256:" + "a" * 64,
        "planner_snapshot_digest": "sha256:" + "b" * 64,
        "snapshot_generation": 1,
        "swarm_id": "offline-two-process-qualification",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": DEPLOYMENT_EPOCH,
    }


class _LocalOnlyFetcher:
    """Resolve generated files only; never delegate to a downloader."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)
        self.requests: list[str] = []

    def __call__(
        self,
        model_id: str,
        revision: str,
        filename: str,
        cache_root: str | Path,
        local_files_only: bool = False,
    ) -> tuple[Path, bool]:
        if local_files_only is not True:
            raise QualificationError("local artifact fetch was not fail-closed offline")
        if model_id != MODEL_ID or revision != RESOLVED_COMMIT:
            raise QualificationError("local artifact request identity mismatch")
        if Path(cache_root).resolve(strict=True) != self.root:
            raise QualificationError("local artifact request cache root mismatch")
        candidate = (self.root / filename).resolve(strict=True)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise QualificationError("local artifact request escaped the work directory") from exc
        if not candidate.is_file():
            raise QualificationError(f"generated artifact is unavailable: {filename}")
        self.requests.append(filename)
        return candidate, True


def _install_network_audit_guard(observed_events: list[str]) -> None:
    """Deny audited socket, DNS, and URL activity for the remaining process life."""

    def reject_network(event: str, args: tuple[Any, ...]) -> None:
        del args
        if event in _NETWORK_AUDIT_EVENTS:
            observed_events.append(event)
            raise RuntimeError(f"network access denied during offline load: {event}")

    sys.addaudithook(reject_network)


def _load_assignment_worker(
    send_connection: Any,
    assignment: dict[str, Any],
    artifact_report: dict[str, Any],
    load_generation: int,
) -> None:
    """Load one stage and send only JSON-safe evidence to the parent."""
    network_attempts: list[str] = []
    _install_network_audit_guard(network_attempts)
    try:
        loaded = runtime_loader.load_assignment_stage(
            assignment,
            artifact_report,
            load_generation=load_generation,
        )
        proof = json.loads(runtime_loader.canonical_json(loaded.proof))
        result = {
            "pid": os.getpid(),
            "assignment_id": assignment["assignment_id"],
            "node_id": assignment["node_id"],
            "assigned_range": copy.deepcopy(assignment["range"]),
            "audited_network_event_count": len(network_attempts),
            "proof": proof,
        }
        _canonical_json(result)
        send_connection.send({"ok": True, "result": result})
    except BaseException as exc:
        error = {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "audited_network_event_count": len(network_attempts),
            },
        }
        try:
            _canonical_json(error)
            send_connection.send(error)
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        send_connection.close()


WorkerTarget = Callable[[Any, dict[str, Any], dict[str, Any], int], None]


def _cleanup_processes(processes: Sequence[Any]) -> None:
    for process in processes:
        if process.is_alive():
            process.terminate()
    for process in processes:
        if process.pid is not None:
            process.join(timeout=1.0)
    for process in processes:
        if process.is_alive():
            process.kill()
    for process in processes:
        if process.pid is not None:
            process.join(timeout=1.0)


def _spawn_assignment_loads(
    jobs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    *,
    load_generation: int,
    timeout_seconds: float,
    worker_target: WorkerTarget = _load_assignment_worker,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Run assignment loads under spawn with bounded wait and hard cleanup."""
    if (
        not isinstance(timeout_seconds, (int, float))
        or isinstance(timeout_seconds, bool)
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise QualificationError("timeout_seconds must be positive and finite")
    if not jobs:
        raise QualificationError("at least one assignment load job is required")

    context = multiprocessing.get_context("spawn")
    processes: list[Any] = []
    receivers: list[Any] = []
    senders: list[Any] = []
    child_pids: list[int] = []
    deadline = time.monotonic() + float(timeout_seconds)
    succeeded = False
    try:
        for index, (assignment, report) in enumerate(jobs):
            receive_connection, send_connection = context.Pipe(duplex=False)
            process = context.Process(
                target=worker_target,
                args=(send_connection, assignment, report, load_generation),
                name=f"mlx-assignment-load-{index}",
                daemon=False,
            )
            receivers.append(receive_connection)
            senders.append(send_connection)
            processes.append(process)
            process.start()
            if process.pid is None:
                raise QualificationError("spawned child has no PID")
            child_pids.append(process.pid)
            # The spawned child owns its duplicated sending endpoint now.
            send_connection.close()

        envelopes: list[dict[str, Any]] = []
        for process, receiver in zip(processes, receivers):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise QualificationError("assignment load qualification timed out")
                if receiver.poll(min(0.05, remaining)):
                    try:
                        envelope = receiver.recv()
                    except EOFError as exc:
                        raise QualificationError(
                            f"child {process.pid} exited without load evidence"
                        ) from exc
                    break
                if not process.is_alive():
                    raise QualificationError(
                        f"child {process.pid} exited without load evidence"
                    )
            if not isinstance(envelope, dict):
                raise QualificationError(
                    f"child {process.pid} returned non-object evidence"
                )
            _canonical_json(envelope)
            envelopes.append(envelope)

        for process in processes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise QualificationError("assignment load qualification timed out")
            process.join(timeout=remaining)
            if process.is_alive():
                raise QualificationError("assignment load qualification timed out")
        raw_exit_codes = [process.exitcode for process in processes]
        if any(code != 0 for code in raw_exit_codes):
            raise QualificationError(
                f"spawned child exit codes are not clean: {raw_exit_codes}"
            )
        exit_codes = [
            code for code in raw_exit_codes if isinstance(code, int)
        ]
        if len(exit_codes) != len(processes):
            raise QualificationError("spawned child exit code is unavailable")

        results: list[dict[str, Any]] = []
        for process, envelope in zip(processes, envelopes):
            if envelope.get("ok") is not True:
                error = envelope.get("error")
                raise QualificationError(
                    f"child {process.pid} rejected assignment load: {error}"
                )
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise QualificationError(
                    f"child {process.pid} omitted JSON-safe load evidence"
                )
            if result.get("pid") != process.pid:
                raise QualificationError(
                    f"child {process.pid} returned mismatched PID evidence"
                )
            results.append(result)
        succeeded = True
        return results, exit_codes
    except QualificationError as exc:
        raise QualificationError(str(exc), child_pids=child_pids) from exc
    except BaseException:
        raise
    finally:
        for connection in receivers:
            connection.close()
        for connection in senders:
            try:
                connection.close()
            except OSError:
                pass
        if not succeeded:
            _cleanup_processes(processes)


def _validate_and_shape_result(
    *,
    manifest: dict[str, Any],
    route: dict[str, Any],
    assignments: list[dict[str, Any]],
    reports: list[dict[str, Any]],
    children: list[dict[str, Any]],
    exit_codes: list[int],
    fetcher: _LocalOnlyFetcher,
) -> dict[str, Any]:
    if len(assignments) != 2 or len(reports) != 2 or len(children) != 2:
        raise QualificationError("qualification requires exactly two assignment loads")
    if exit_codes != [0, 0]:
        raise QualificationError("both spawned assignment loads must exit cleanly")

    expected_start = 0
    ranges: list[dict[str, Any]] = []
    for assignment, stage in zip(assignments, route["route"]):
        validate_assignment_identity(assignment)
        if assignment["node_id"] != stage["node_id"]:
            raise QualificationError("assignment node does not match route stage")
        layer_range = assignment["range"]
        if layer_range != stage["range"]:
            raise QualificationError("assignment range does not match route stage")
        if layer_range["start_layer"] != expected_start:
            raise QualificationError("assignment ranges overlap or contain a gap")
        expected_start = layer_range["end_layer_exclusive"]
        ranges.append(copy.deepcopy(layer_range))
    if expected_start != manifest["num_layers"]:
        raise QualificationError("assignment ranges do not exactly cover model layers")

    network_download_bytes = 0
    cache_hit_bytes = 0
    expected_bytes = 0
    for assignment, report in zip(assignments, reports):
        errors = artifact_report_errors(assignment, report)
        if errors:
            raise QualificationError(
                "artifact verification report failed validation: " + "; ".join(errors)
            )
        if assignment["route_ready"] is not False or report["route_ready"] is not False:
            raise QualificationError("pre-load evidence made a route-readiness claim")
        network_download_bytes += report["network_download_bytes"]
        cache_hit_bytes += report["cache_hit_bytes"]
        expected_bytes += report["expected_bytes"]
    if network_download_bytes != 0 or cache_hit_bytes != expected_bytes:
        raise QualificationError("offline artifact byte accounting is invalid")

    pids: list[int] = []
    probe_shapes_by_node: dict[str, list[int]] = {}
    expected_tensor_sets: list[set[str]] = []
    for assignment, child in zip(assignments, children):
        if set(child) != CHILD_RESULT_FIELDS:
            raise QualificationError("child result fields are not deterministic")
        _canonical_json(child)
        pid = child["pid"]
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise QualificationError("child returned an invalid PID")
        pids.append(pid)
        if child["audited_network_event_count"] != 0:
            raise QualificationError(
                "child observed an audited network event during load"
            )
        if child["assignment_id"] != assignment["assignment_id"]:
            raise QualificationError("child assignment identity mismatch")
        if child["node_id"] != assignment["node_id"]:
            raise QualificationError("child node identity mismatch")
        if child["assigned_range"] != assignment["range"]:
            raise QualificationError("child assigned range mismatch")

        proof = child["proof"]
        if not isinstance(proof, dict) or set(proof) != LOAD_PROOF_FIELDS:
            raise QualificationError("load proof fields are not deterministic")
        identity_fields = (
            "deployment_id",
            "deployment_epoch",
            "assignment_id",
            "node_id",
            "model_id",
            "manifest_digest",
            "resolved_commit",
            "control_plane_binding",
        )
        for field in identity_fields:
            if proof[field] != assignment[field]:
                raise QualificationError(f"load proof identity mismatch: {field}")
        if proof["loaded_range"] != assignment["range"]:
            raise QualificationError("load proof range mismatch")
        if proof["protocol"] != runtime_loader.LAYER_LOAD_PROOF_PROTOCOL:
            raise QualificationError("load proof protocol mismatch")
        if proof["loaded_components"] != assignment["components"]:
            raise QualificationError("load proof component ownership mismatch")
        if proof["loaded_tensor_keys"] != sorted(assignment["expected_tensor_keys"]):
            raise QualificationError("load proof tensor ownership mismatch")
        if proof["runtime"] != assignment["runtime"]:
            raise QualificationError("load proof runtime identity mismatch")
        if proof["load_generation"] != LOAD_GENERATION:
            raise QualificationError("load proof generation mismatch")
        if proof["route_ready"] is not False:
            raise QualificationError("load proof made a route-readiness claim")
        expected_probe_width = (
            assignment["runtime"]["model_config"]["vocab_size"]
            if "lm_head" in assignment["components"]
            else assignment["runtime"]["model_config"]["n_embd"]
        )
        expected_probe_shape = [1, 3, expected_probe_width]
        if proof["probe_shape"] != expected_probe_shape:
            raise QualificationError("load proof probe shape mismatch")
        probe_shapes_by_node[assignment["node_id"]] = expected_probe_shape
        expected_tensor_sets.append(set(assignment["expected_tensor_keys"]))
    if len(set(pids)) != 2 or os.getpid() in pids:
        raise QualificationError("assignment loads did not use two distinct child PIDs")
    if not expected_tensor_sets[0].isdisjoint(expected_tensor_sets[1]):
        raise QualificationError("compiled assignments do not own disjoint tensor sets")

    proof_shape = {
        "child_result_fields": sorted(CHILD_RESULT_FIELDS),
        "load_proof_fields": sorted(LOAD_PROOF_FIELDS),
        "probe_shapes_by_node": probe_shapes_by_node,
    }
    document = {
        "protocol": QUALIFICATION_PROTOCOL,
        "qualified": True,
        "claim": CLAIM,
        "negative_claims": list(NEGATIVE_CLAIMS),
        "model": {
            "model_id": manifest["model_id"],
            "resolved_commit": manifest["resolved_commit"],
            "manifest_digest": manifest_digest_ref(manifest),
            "format": manifest["format"],
            "num_layers": manifest["num_layers"],
            "generated_locally": True,
        },
        "process_evidence": {
            "start_method": "spawn",
            "parent_pid": os.getpid(),
            "child_pids": pids,
            "exit_codes": exit_codes,
        },
        "coverage": {
            "num_layers": manifest["num_layers"],
            "ranges": ranges,
            "disjoint": True,
            "complete": True,
        },
        "offline_evidence": {
            "local_files_only": True,
            "artifact_reports_validated": True,
            "worker_guard_scope": (
                "immediately_before_assignment_load_until_child_exit"
            ),
            "worker_denied_audit_events": sorted(_NETWORK_AUDIT_EVENTS),
            "artifact_request_count": len(fetcher.requests),
            "requested_files": list(fetcher.requests),
            "network_download_bytes": network_download_bytes,
            "cache_hit_bytes": cache_hit_bytes,
            "expected_bytes": expected_bytes,
        },
        "children": children,
        "proof_shape": proof_shape,
        "route_ready": False,
    }
    _canonical_json(document)
    return document


def run_qualification(
    work_dir: str | Path, *, timeout_seconds: float = 30.0
) -> dict[str, Any]:
    """Build and execute one complete offline two-process qualification."""
    root = Path(work_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest, _ = _build_local_model(root)
    route = _route_for_manifest(manifest)
    runtime_by_node = {
        node: {"backend": "mlx", "dtype": "float32", "quantization": "none"}
        for node in route["node_order"]
    }
    assignments = compile_layer_assignments(
        route_plan=route,
        manifest=manifest,
        deployment_id=DEPLOYMENT_ID,
        deployment_epoch=DEPLOYMENT_EPOCH,
        cache_roots={node: str(root) for node in route["node_order"]},
        runtime_by_node=runtime_by_node,
        control_plane_binding=_control_plane_binding(),
    )

    fetcher = _LocalOnlyFetcher(root)
    reports = [
        provision_assignment(
            assignment,
            fetch_file=fetcher,
            local_files_only=True,
        )
        for assignment in assignments
    ]
    children, exit_codes = _spawn_assignment_loads(
        list(zip(assignments, reports)),
        load_generation=LOAD_GENERATION,
        timeout_seconds=timeout_seconds,
    )
    return _validate_and_shape_result(
        manifest=manifest,
        route=route,
        assignments=assignments,
        reports=reports,
        children=children,
        exit_codes=exit_codes,
        fetcher=fetcher,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build a local tiny sharded GPT-2 model and qualify two spawned "
            "assignment-bound MLX stage loads."
        )
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the complete JSON-safe qualification evidence document",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="retain generated local model artifacts in this directory",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="total timeout for spawned assignment loads (default: 30)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.work_dir is None:
            with tempfile.TemporaryDirectory(
                prefix="mycelium-two-process-qualification-"
            ) as temporary:
                result = run_qualification(
                    temporary, timeout_seconds=args.timeout_seconds
                )
        else:
            result = run_qualification(
                args.work_dir, timeout_seconds=args.timeout_seconds
            )
    except (QualificationError, OSError, ValueError) as exc:
        print(f"qualification failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(_canonical_json(result))
    else:
        print("QUALIFIED: " + result["claim"])
        for negative_claim in result["negative_claims"]:
            print("NOT CLAIMED: " + negative_claim)
        print("route_ready=false")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
