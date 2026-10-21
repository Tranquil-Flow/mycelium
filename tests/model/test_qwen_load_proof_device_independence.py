# SPDX-License-Identifier: AGPL-3.0-only
"""A member's load proof must reproduce the compile-time placement proof.

Two-Mac run 14 (2026-10-13): the owner (M4 Pro) compiled the route and
recorded every stage's load proof; the joined MacBook Air (M1) then loaded its
own stage from byte-identical verified weights and refused ``configure`` with
``placement_load_proof_mismatch``.  The only field that differed was
``probe_digest``: it hashed the raw float32 bytes of the MLX probe output, and
MLX picks its GEMM tiling per GPU class (``GEMM_TPARAM_MACRO``: M1
``applegpu_g13g`` is a "small" device, nt matmul ``bk=32``; M4 Pro
``applegpu_g16s`` a "medium" one, ``bk=16``), so the float32 accumulation order
and therefore the last bits of the probe differ between Macs.

These tests drive the production manifest compiler, the assignment loader, the
execution-graph builder and the node's exact comparison
(``layer_load_proof_digest(loaded.proof) == placement.load_proof_digest``,
physical_inference_node.py) with the member's probe perturbed by one float32
ULP: the cross-device rounding the real run produced.  The same tests pin that
the check still refuses different weight bytes and a probe that computes a
different function.
"""

from __future__ import annotations

import json
from pathlib import Path

import mlx.core as mx
import numpy as np
import pytest

import runtime_loader
from mycelium_qualification.physical_deployment import (
    LocalModelSource,
    build_execution_graph,
    prepare_assignment_artifacts,
    prepare_physical_deployment,
)
from mycelium_router.layer_builder import layer_load_proof_digest
from runtime_loader import canonical_json, load_assignment_stage
from tests.model import qwen35_moe_tiny as moe_tiny
from tests.model.test_qwen3_dense_mlx_path import COMMIT, MODEL_ID, _snapshot


def _compiled(tmp_path: Path, quantization: str):
    """Owner side: compile, load every stage locally, publish the graph."""

    deployment = prepare_physical_deployment(
        tmp_path / f"deployment-{quantization}",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=_snapshot(tmp_path), model_id=MODEL_ID, resolved_commit=COMMIT
        ),
        runtime_dtype="float32",
        runtime_quantization=quantization,
    )
    proofs = [
        load_assignment_stage(assignment, report, load_generation=17).proof
        for assignment, report in zip(
            deployment.assignments, deployment.artifact_reports, strict=True
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
        proofs,
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    return deployment, proofs, graph


def _member_digest(loaded) -> str:
    # Exactly the node's configure comparison (physical_inference_node.py).
    return layer_load_proof_digest(json.loads(canonical_json(loaded.proof)))


def _one_ulp_up(output):
    """The other Mac's float32 rounding: every element moved by one ULP."""

    values = np.array(output, dtype=np.float32)
    return mx.array(np.nextafter(values, np.float32(np.inf), dtype=np.float32))


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only"])
def test_member_on_another_gpu_class_reproduces_the_placement_load_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quantization: str
) -> None:
    deployment, proofs, graph = _compiled(tmp_path, quantization)
    real_probe = runtime_loader._run_qwen2_probe
    raw_digests: list[tuple[str, str]] = []

    def other_gpu_probe(*args, **kwargs):
        output = real_probe(*args, **kwargs)
        moved = _one_ulp_up(output)
        raw_digests.append(
            (runtime_loader._digest_array(output), runtime_loader._digest_array(moved))
        )
        return moved

    monkeypatch.setattr(runtime_loader, "_run_qwen2_probe", other_gpu_probe)
    for index, (assignment, report) in enumerate(
        zip(deployment.assignments, deployment.artifact_reports, strict=True)
    ):
        member = load_assignment_stage(assignment, report, load_generation=17)
        placement = graph.stages[index].placements[0]
        assert placement.node_id == assignment["node_id"]
        # Same verified bytes: the weight identity is bit-exact on every Mac.
        assert member.proof["loaded_tensor_digest"] == proofs[index]["loaded_tensor_digest"]
        assert _member_digest(member) == placement.load_proof_digest
    # Precondition: the perturbation really changed the raw probe bytes.
    assert raw_digests and all(before != after for before, after in raw_digests)


def test_member_with_different_weight_bytes_still_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment, _proofs, graph = _compiled(tmp_path, "int8-weight-only")
    real_load = runtime_loader._load_exact_tensors

    def tampered_load(*args, **kwargs):
        tensors = dict(real_load(*args, **kwargs))
        key = sorted(key for key in tensors if key.endswith("norm.weight"))[0]
        tensors[key] = tensors[key] * mx.array(1.0001, dtype=mx.float32)
        mx.eval(tensors[key])
        return tensors

    monkeypatch.setattr(runtime_loader, "_load_exact_tensors", tampered_load)
    member = load_assignment_stage(
        deployment.assignments[0], deployment.artifact_reports[0], load_generation=17
    )
    assert _member_digest(member) != graph.stages[0].placements[0].load_proof_digest


def test_member_probe_computing_a_different_function_still_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment, _proofs, graph = _compiled(tmp_path, "int8-weight-only")
    real_probe = runtime_loader._run_qwen2_probe

    def wrong_function_probe(*args, **kwargs):
        values = np.array(real_probe(*args, **kwargs), dtype=np.float32)
        lowest = np.argsort(values, axis=-1, kind="stable")[..., :1]
        np.put_along_axis(values, lowest, np.float32(1e6), axis=-1)
        return mx.array(values)

    monkeypatch.setattr(runtime_loader, "_run_qwen2_probe", wrong_function_probe)
    for index, (assignment, report) in enumerate(
        zip(deployment.assignments, deployment.artifact_reports, strict=True)
    ):
        member = load_assignment_stage(assignment, report, load_generation=17)
        assert _member_digest(member) != graph.stages[index].placements[0].load_proof_digest


def _compiled_moe(tmp_path: Path, quantization: str):
    """Owner side for a Qwen3.5 MoE (``qwen3_5_moe``) two-stage route."""

    source = tmp_path / "moe-source"
    moe_tiny.write_checkpoint(source)
    prepared = prepare_assignment_artifacts(
        tmp_path / f"moe-deployment-{quantization}",
        node_ids=("node-000", "node-001"),
        model_source=LocalModelSource(
            root=source, model_id="synthetic/Qwen3.5-MoE-tiny", resolved_commit="f" * 40
        ),
        runtime_dtype="float32",
        runtime_quantization=quantization,
        layer_ranges=(range(0, 2), range(2, 4)),
    )
    proofs = [
        load_assignment_stage(assignment, report, load_generation=17).proof
        for assignment, report in zip(prepared.assignments, prepared.reports, strict=True)
    ]
    graph = build_execution_graph(
        prepared.assignments, proofs, link_scheme="iroh", runtime_scheme="iroh"
    )
    return prepared, proofs, graph


@pytest.mark.parametrize("quantization", ["none", "int8-weight-only"])
def test_moe_member_on_another_gpu_class_reproduces_the_placement_load_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quantization: str
) -> None:
    prepared, proofs, graph = _compiled_moe(tmp_path, quantization)
    assert prepared.assignments[0]["runtime"]["architecture"] == "qwen3_5_moe"
    real_probe = runtime_loader._run_qwen35_probe
    raw_digests: list[tuple[str, str]] = []

    def other_gpu_probe(*args, **kwargs):
        output = real_probe(*args, **kwargs)
        moved = _one_ulp_up(output)
        raw_digests.append(
            (runtime_loader._digest_array(output), runtime_loader._digest_array(moved))
        )
        return moved

    monkeypatch.setattr(runtime_loader, "_run_qwen35_probe", other_gpu_probe)
    for index, (assignment, report) in enumerate(
        zip(prepared.assignments, prepared.reports, strict=True)
    ):
        member = load_assignment_stage(assignment, report, load_generation=17)
        placement = graph.stages[index].placements[0]
        assert member.proof["loaded_tensor_digest"] == proofs[index]["loaded_tensor_digest"]
        assert _member_digest(member) == placement.load_proof_digest
    assert raw_digests and all(before != after for before, after in raw_digests)


def test_moe_member_probe_computing_a_different_function_still_mismatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared, _proofs, graph = _compiled_moe(tmp_path, "int8-weight-only")
    real_probe = runtime_loader._run_qwen35_probe

    def wrong_function_probe(*args, **kwargs):
        values = np.array(real_probe(*args, **kwargs), dtype=np.float32)
        lowest = np.argsort(values, axis=-1, kind="stable")[..., :1]
        np.put_along_axis(values, lowest, np.float32(1e6), axis=-1)
        return mx.array(values)

    monkeypatch.setattr(runtime_loader, "_run_qwen35_probe", wrong_function_probe)
    for index, (assignment, report) in enumerate(
        zip(prepared.assignments, prepared.reports, strict=True)
    ):
        member = load_assignment_stage(assignment, report, load_generation=17)
        assert _member_digest(member) != graph.stages[index].placements[0].load_proof_digest
