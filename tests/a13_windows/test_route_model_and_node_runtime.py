# SPDX-License-Identifier: AGPL-3.0-only
"""M6: a run pins one registered model; each managed node runs its own class's runtime.

The A9 route pins Qwen2.5-0.5B (A9_ROUTE_MODEL) with mlx on the macOS node and
numpy on the Windows node.  Owner-side plan validation, target-side work
consent and assignment provisioning all check the exact per-node runtime.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import model_manifest as mm
from mycelium_a13_macos import serving
from mycelium_a13_macos.serving import (
    A9_ROUTE_MODEL,
    CANDIDATE_MODEL,
    PreparedSource,
    ServingPreparationError,
    node_runtime,
    registered_model_contract,
)
from mycelium_a13_macos.work_authorization import WorkAuthorizationError, authorize_work_consent

from tests.a13_macos import test_work_authorization as base


def _manifest(model):
    weights = {name: "toy.safetensors" for name in (
        "model.embed_tokens.weight", "model.norm.weight", "lm_head.weight",
        *(f"model.layers.{i}.self_attn.q_proj.weight" for i in range(3)))}
    return mm.compile_model_manifest(
        model_id=model.model_id, requested_revision=model.resolved_commit,
        resolved_commit=model.resolved_commit,
        config={"model_type": "qwen2", "architectures": ["Qwen2ForCausalLM"],
                "num_hidden_layers": 3, "hidden_size": 4, "num_attention_heads": 1,
                "num_key_value_heads": 1, "head_dim": 4, "intermediate_size": 8,
                "vocab_size": 8, "max_position_embeddings": 16, "rms_norm_eps": 1e-6,
                "rope_theta": 10000.0, "hidden_act": "silu", "tie_word_embeddings": False},
        checkpoint_index={"metadata": {"total_size": 128}, "weight_map": weights},
        file_metadata={"toy.safetensors": {"size_bytes": 128, "sha256": "1" * 64}},
    )


def _assignment(staged, manifest, model, backend):
    document = base._assignment(staged, manifest, runtime={**manifest["runtime_model"], **node_runtime(model, backend)})
    from layer_assignment import assignment_id_for

    body = {key: value for key, value in document.items() if key not in {"assignment_id", "route_ready"}}
    body.update(model_id=model.model_id, resolved_commit=model.resolved_commit)
    return {**body, "assignment_id": assignment_id_for(body), "route_ready": False}


def _authorize(tmp_path, model, *, assigned_backend, target_backend):
    staged = (tmp_path / "staged")
    staged.mkdir(exist_ok=True)
    staged = staged.resolve()
    manifest = _manifest(model)
    assignment = _assignment(staged, manifest, model, assigned_backend)
    (staged / "control").mkdir(exist_ok=True)
    (staged / "deployment").mkdir(exist_ok=True)
    (staged / "control/execution-graph.json").write_text(json.dumps(base._graph(mm.manifest_digest_ref(manifest))))
    return authorize_work_consent(
        offer=base._offer(), acknowledged=True, limits=base._limits(), staged_root=staged,
        assignment=assignment, expected_assignment_digest=base._digest(assignment), manifest=manifest,
        runtime_backend=target_backend)


def test_registered_models_and_node_runtimes():
    assert registered_model_contract(A9_ROUTE_MODEL.model_id, A9_ROUTE_MODEL.resolved_commit) is A9_ROUTE_MODEL
    assert registered_model_contract(CANDIDATE_MODEL.model_id, CANDIDATE_MODEL.resolved_commit) is CANDIDATE_MODEL
    with pytest.raises(ServingPreparationError, match="model_contract_unregistered"):
        registered_model_contract(A9_ROUTE_MODEL.model_id, CANDIDATE_MODEL.resolved_commit)
    assert node_runtime(A9_ROUTE_MODEL, "numpy") == {"backend": "numpy", "dtype": "float32",
                                                     "quantization": "int8-weight-only"}
    with pytest.raises(ServingPreparationError, match="node_runtime_invalid"):
        node_runtime(A9_ROUTE_MODEL, "pixel-stdlib")


def test_windows_target_consents_to_a_numpy_assignment_of_the_a9_model(tmp_path):
    grant = _authorize(tmp_path, A9_ROUTE_MODEL, assigned_backend="numpy", target_backend="numpy")
    assert grant.model_id == A9_ROUTE_MODEL.model_id
    assert grant.resolved_commit == A9_ROUTE_MODEL.resolved_commit


@pytest.mark.parametrize("assigned,target", [("mlx", "numpy"), ("numpy", "mlx")])
def test_a_node_never_consents_to_another_class_runtime(tmp_path, assigned, target):
    with pytest.raises(WorkAuthorizationError, match="work_assignment_invalid"):
        _authorize(tmp_path, A9_ROUTE_MODEL, assigned_backend=assigned, target_backend=target)


def test_an_unregistered_model_is_refused_by_the_target(tmp_path):
    from mycelium_a13_macos.serving import ModelContract

    rogue = ModelContract(model_id="Qwen/Qwen2.5-0.5B-Instruct", resolved_commit="f" * 40,
                          source_representation="bfloat16-safetensors", runtime_backend="mlx",
                          runtime_dtype="float32", runtime_quantization="int8-weight-only")
    manifest = _manifest(rogue)
    staged = (tmp_path / "staged")
    staged.mkdir()
    staged = staged.resolve()
    assignment = base._assignment(staged, manifest)
    with pytest.raises(WorkAuthorizationError, match="work_manifest_invalid"):
        authorize_work_consent(offer=base._offer(), acknowledged=True, limits=base._limits(), staged_root=staged,
                               assignment=assignment, expected_assignment_digest=base._digest(assignment),
                               manifest=manifest, runtime_backend="mlx")


def _plan(tmp_path, model, manifest_digest, backends):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    nodes = []
    for node_id, backend in backends.items():
        name = f"{node_id}-assignment.json"
        (bundle / name).write_text(json.dumps({
            "model_id": model.model_id, "resolved_commit": model.resolved_commit,
            "manifest_digest": manifest_digest, "node_id": node_id,
            "runtime": {"architecture": "qwen2", **node_runtime(model, backend)}}))
        nodes.append({"node_id": node_id, "configure": {"assignment_file": name, "graph": {
            "model_id": model.model_id, "resolved_commit": model.resolved_commit,
            "manifest_digest": manifest_digest}}})
    plan = tmp_path / "operator-plan.json"
    plan.write_text(json.dumps({"controller": {"source_root": str(bundle), "run_plan": {
        "nodes": nodes, "request": {"sampling_seed": 0}}}}))
    return plan


def test_owner_plan_must_assign_each_node_its_class_runtime(tmp_path):
    digest = "sha256:" + hashlib.sha256(b"m").hexdigest()
    prepared = PreparedSource(model=A9_ROUTE_MODEL, source_root=Path(tmp_path), source_bytes=1,
                              manifest={}, manifest_digest=digest)
    plan = _plan(tmp_path, A9_ROUTE_MODEL, digest, {"node-mac": "mlx", "node-win": "numpy"})
    serving._validate_operator_plan(plan, prepared, {"node-mac": "mlx", "node-win": "numpy"})
    with pytest.raises(ServingPreparationError, match="representation_mismatch"):
        serving._validate_operator_plan(plan, prepared, {"node-mac": "mlx", "node-win": "mlx"})
    with pytest.raises(ServingPreparationError, match="representation_mismatch"):
        serving._validate_operator_plan(plan, prepared, {"node-mac": "mlx"})
    with pytest.raises(ServingPreparationError, match="representation_mismatch"):
        serving._validate_operator_plan(plan, prepared)  # default mlx everywhere


def test_run_model_selection_is_explicit_and_closed():
    from mycelium_a13_macos.serving import selected_run_model

    assert selected_run_model({}) is CANDIDATE_MODEL
    assert selected_run_model({"MYCELIUM_A13_RUN_MODEL": "a9-route"}) is A9_ROUTE_MODEL
    with pytest.raises(ServingPreparationError, match="run_model_selection_invalid"):
        selected_run_model({"MYCELIUM_A13_RUN_MODEL": "something-else"})


def test_acquisition_status_is_bound_to_its_own_model(tmp_path):
    from mycelium_a13_macos.acquisition import SelectedModelAcquisition

    acquisition = SelectedModelAcquisition(tmp_path / "a9", model=A9_ROUTE_MODEL)
    try:
        assert acquisition.status()["model"]["model_id"] == A9_ROUTE_MODEL.model_id
    finally:
        acquisition.close()
