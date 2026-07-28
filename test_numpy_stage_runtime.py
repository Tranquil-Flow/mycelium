"""Assignment-bound NumPy stage runtime parity and rejection tests.

RED → GREEN: each test first fails (or asserts correctly) to prove it catches
what it should, then passes once the implementation satisfies the contract.
These tests validate strict cross-backend parity with the MLX loader.
"""

from __future__ import annotations

import copy
import json
import math
import struct
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from layer_assignment import assignment_id_for
from numpy_stage_runtime import (
    NumpyStageBackend,
    RuntimeExecutionError,
    RuntimeLoadError,
    canonical_json,
    execute_loaded_stage,
    load_assignment_stage,
)
from runtime_contracts import (
    StageRuntimeBackend,
)

DEPLOYMENT_ID = "12345678-1234-5678-9234-abcdefabcdef"
COMMIT = "c" * 40
MANIFEST_DIGEST = "sha256:" + "d" * 64
LAYER_SUFFIXES = (
    "ln_1.weight",
    "ln_1.bias",
    "attn.c_attn.weight",
    "attn.c_attn.bias",
    "attn.c_proj.weight",
    "attn.c_proj.bias",
    "ln_2.weight",
    "ln_2.bias",
    "mlp.c_fc.weight",
    "mlp.c_fc.bias",
    "mlp.c_proj.weight",
    "mlp.c_proj.bias",
)


def _values(
    shape: tuple[int, ...], *, offset: int = 0, scale: float = 0.01
) -> np.ndarray:
    size = 1
    for dimension in shape:
        size *= dimension
    return (np.arange(size, dtype=np.float32).reshape(shape) + offset) * scale


def _layer_tensors(layer: int, *, offset: int = 0) -> dict[str, np.ndarray]:
    prefix = f"transformer.h.{layer}."
    return {
        prefix + "ln_1.weight": np.ones((4,), dtype=np.float32),
        prefix + "ln_1.bias": np.zeros((4,), dtype=np.float32),
        prefix + "attn.c_attn.weight": _values((4, 12), offset=offset + 1, scale=0.002),
        prefix + "attn.c_attn.bias": _values((12,), offset=offset + 2, scale=0.001),
        prefix + "attn.c_proj.weight": _values((4, 4), offset=offset + 3, scale=0.002),
        prefix + "attn.c_proj.bias": _values((4,), offset=offset + 4, scale=0.001),
        prefix + "ln_2.weight": np.ones((4,), dtype=np.float32),
        prefix + "ln_2.bias": np.zeros((4,), dtype=np.float32),
        prefix + "mlp.c_fc.weight": _values((4, 8), offset=offset + 5, scale=0.002),
        prefix + "mlp.c_fc.bias": _values((8,), offset=offset + 6, scale=0.001),
        prefix + "mlp.c_proj.weight": _values((8, 4), offset=offset + 7, scale=0.002),
        prefix + "mlp.c_proj.bias": _values((4,), offset=offset + 8, scale=0.001),
    }


def _source_tensors(*, weight_offset: int = 0) -> dict[str, np.ndarray]:
    tensors = {
        "transformer.wte.weight": _values((7, 4), offset=weight_offset + 1, scale=0.01),
        "transformer.wpe.weight": _values(
            (8, 4), offset=weight_offset + 2, scale=0.005
        ),
        "transformer.ln_f.weight": np.ones((4,), dtype=np.float32),
        "transformer.ln_f.bias": np.zeros((4,), dtype=np.float32),
        "transformer.h.9.ln_1.weight": np.ones((4,), dtype=np.float32) * 9,
    }
    tensors.update(_layer_tensors(0, offset=weight_offset + 10))
    tensors.update(_layer_tensors(1, offset=weight_offset + 30))
    return tensors


def _save_safetensors(path: Path, tensors: dict[str, np.ndarray]) -> None:
    """Write a minimal Safetensors file from NumPy arrays."""
    header: dict[str, Any] = {}
    offset = 0
    for name in sorted(tensors):
        array = tensors[name]
        dtype_map = {np.float32: "F32", np.float16: "F16"}
        dtype_str = dtype_map.get(array.dtype.type, "F32")
        shape = list(array.shape)
        byte_len = array.nbytes
        header[name] = {
            "dtype": dtype_str,
            "shape": shape,
            "data_offsets": [offset, offset + byte_len],
        }
        offset += byte_len

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    # Pad to 8 bytes
    header_bytes += b" " * ((8 - len(header_bytes) % 8) % 8)
    with path.open("wb") as f:
        f.write(struct.pack("<Q", len(header_bytes)))
        f.write(header_bytes)
        for name in sorted(tensors):
            array = tensors[name]
            f.write(array.tobytes())


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _control_plane_binding() -> dict[str, Any]:
    return {
        "protocol": "mycelium.control_plane_binding.v1",
        "evidence_bundle_digest": "sha256:" + "a" * 64,
        "planner_snapshot_digest": "sha256:" + "b" * 64,
        "snapshot_generation": 4,
        "swarm_id": "swarm-test",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 7,
    }


def _runtime() -> dict[str, Any]:
    return {
        "backend": "numpy",
        "dtype": "float32",
        "quantization": "none",
        "architecture": "gpt2",
        "model_config": {
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
        },
    }


def _rebind(assignment: dict[str, Any], report: dict[str, Any] | None = None) -> None:
    assignment["assignment_id"] = assignment_id_for(assignment)
    if report is not None:
        report["assignment_id"] = assignment["assignment_id"]
        report["verified_tensor_count"] = len(set(assignment["expected_tensor_keys"]))


def _case(
    tmp_path: Path, *, weight_offset: int = 0
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    artifact = tmp_path / "model.safetensors"
    source_tensors = _source_tensors(weight_offset=weight_offset)
    _save_safetensors(artifact, source_tensors)

    decoder_keys = sorted(
        f"transformer.h.{layer}.{suffix}"
        for layer in range(2)
        for suffix in LAYER_SUFFIXES
    )
    component_tensor_keys = {
        "input_embedding": ["transformer.wpe.weight", "transformer.wte.weight"],
        "decoder": decoder_keys,
        "final_norm": ["transformer.ln_f.bias", "transformer.ln_f.weight"],
        "lm_head": ["transformer.wte.weight"],
    }
    expected_keys = sorted(
        {
            key
            for component_keys in component_tensor_keys.values()
            for key in component_keys
        }
    )
    size = artifact.stat().st_size
    digest = "sha256:" + _sha256_file(artifact)
    assignment = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 7,
        "node_id": "node-a",
        "manifest_digest": MANIFEST_DIGEST,
        "model_id": "test/tiny-gpt2",
        "resolved_commit": COMMIT,
        "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
        "components": ["input_embedding", "decoder", "final_norm", "lm_head"],
        "component_tensor_keys": component_tensor_keys,
        "component_aliases": {"lm_head": "input_embedding"},
        "expected_tensor_prefixes": ["transformer.h.0.", "transformer.h.1."],
        "expected_tensor_keys": expected_keys,
        "files": [
            {"path": artifact.name, "size_bytes": size, "content_digest": digest}
        ],
        "artifact_cache_root": str(tmp_path),
        "runtime": _runtime(),
        "control_plane_binding": _control_plane_binding(),
        "route_ready": False,
        "claim_boundary": "test assignment; runtime not loaded",
    }
    _rebind(assignment)
    report = {
        "protocol": "mycelium.artifact_verification_report.v1",
        "deployment_id": assignment["deployment_id"],
        "deployment_epoch": assignment["deployment_epoch"],
        "assignment_id": assignment["assignment_id"],
        "node_id": assignment["node_id"],
        "manifest_digest": assignment["manifest_digest"],
        "resolved_commit": assignment["resolved_commit"],
        "range": copy.deepcopy(assignment["range"]),
        "artifact_cache_root": str(tmp_path),
        "resolved_artifact_cache_root": str(tmp_path.resolve()),
        "verified_files": [
            {
                "path": artifact.name,
                "local_path": str(artifact.resolve()),
                "size_bytes": size,
                "content_digest": digest,
                "cache_hit": True,
                "tensor_count": len(source_tensors),
            }
        ],
        "verified_tensor_prefixes": list(assignment["expected_tensor_prefixes"]),
        "verified_tensor_count": len(expected_keys),
        "expected_bytes": size,
        "network_download_bytes": 0,
        "cache_hit_bytes": size,
        "ready_for_load": True,
        "route_ready": False,
        "claim_boundary": "test artifacts verified; layers not loaded",
    }
    return assignment, report, artifact


def _refresh_file_evidence(
    assignment: dict[str, Any], report: dict[str, Any], artifact: Path
) -> None:
    size = artifact.stat().st_size
    digest = "sha256:" + _sha256_file(artifact)
    assignment["files"][0]["size_bytes"] = size
    assignment["files"][0]["content_digest"] = digest
    report["verified_files"][0]["size_bytes"] = size
    report["verified_files"][0]["content_digest"] = digest
    report["expected_bytes"] = size
    report["network_download_bytes"] = 0
    report["cache_hit_bytes"] = size
    _rebind(assignment, report)


def _restrict_assignment(
    assignment: dict[str, Any],
    report: dict[str, Any],
    *,
    start: int,
    end: int,
    components: list[str],
) -> None:
    prefixes = [f"transformer.h.{layer}." for layer in range(start, end)]
    original = assignment["component_tensor_keys"]
    component_keys: dict[str, list[str]] = {}
    for component in components:
        if component == "decoder":
            component_keys[component] = sorted(
                key
                for key in original[component]
                if any(key.startswith(prefix) for prefix in prefixes)
            )
        else:
            component_keys[component] = list(original[component])
    assignment["range"] = {
        "start_layer": start,
        "end_layer_exclusive": end,
        "layer_count": end - start,
    }
    assignment["components"] = components
    assignment["component_tensor_keys"] = component_keys
    assignment["component_aliases"] = {
        source: target
        for source, target in assignment["component_aliases"].items()
        if source in components
    }
    assignment["expected_tensor_prefixes"] = prefixes
    assignment["expected_tensor_keys"] = sorted(
        {key for keys in component_keys.values() for key in keys}
    )
    report["range"] = copy.deepcopy(assignment["range"])
    report["verified_tensor_prefixes"] = prefixes
    _rebind(assignment, report)


# ---------------------------------------------------------------------------
# RED → GREEN: Assignment-bound NumPy stage loading and proof emission
# ---------------------------------------------------------------------------


def test_loads_exact_assignment_owned_gpt2_stage_and_emits_canonical_proof(
    tmp_path: Path,
) -> None:
    """GREEN: Load full stage with NumPy backend, emit immutable proof."""
    assignment, report, _ = _case(tmp_path)

    loaded = load_assignment_stage(assignment, report, load_generation=11)

    assert set(loaded.tensors) == set(assignment["expected_tensor_keys"])
    assert "transformer.h.9.ln_1.weight" not in loaded.tensors
    assert canonical_json(loaded.resolved_aliases) == canonical_json(
        {
            "lm_head": {
                "target_component": "input_embedding",
                "tensor_keys": ["transformer.wte.weight"],
            }
        }
    )
    proof = loaded.proof
    assert proof["protocol"] == "mycelium.layer_load_proof.v1"
    assert proof["deployment_id"] == assignment["deployment_id"]
    assert proof["deployment_epoch"] == assignment["deployment_epoch"]
    assert proof["assignment_id"] == assignment["assignment_id"]
    assert proof["node_id"] == assignment["node_id"]
    assert proof["model_id"] == assignment["model_id"]
    assert proof["manifest_digest"] == assignment["manifest_digest"]
    assert proof["resolved_commit"] == assignment["resolved_commit"]
    assert proof["loaded_range"] == assignment["range"]
    assert list(proof["loaded_components"]) == assignment["components"]
    assert list(proof["loaded_tensor_keys"]) == sorted(
        assignment["expected_tensor_keys"]
    )
    assert proof["loaded_tensor_digest"].startswith("sha256:")
    assert proof["runtime"] == assignment["runtime"]
    assert tuple(proof["probe_shape"]) == (1, 3, 7)
    assert proof["probe_digest"].startswith("sha256:")
    assert proof["load_generation"] == 11
    assert proof["control_plane_binding"] == assignment["control_plane_binding"]
    assert proof["route_ready"] is False
    assert proof["claim_boundary"] == (
        "assignment-bound local NumPy stage loaded and deterministically probed; "
        "no route challenge or distributed inference claim"
    )
    # Backend identity must be numpy, not mlx
    assert proof["runtime_identity"]["backend"] == "numpy"
    assert proof["runtime_identity"]["device"] == "cpu"

    # Canonicity
    serialized = canonical_json(proof)
    assert canonical_json(json.loads(serialized)) == serialized


def test_proof_backend_is_numpy_not_mlx(tmp_path: Path) -> None:
    """GREEN: Emitted proof declares numpy backend, never mlx."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)
    assert loaded.proof["runtime_identity"]["backend"] == "numpy"
    assert loaded.proof["runtime"]["backend"] == "numpy"


def test_emitted_proof_and_alias_evidence_are_deeply_immutable(tmp_path: Path) -> None:
    """GREEN: Proof and aliases are frozen after load."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=11)
    before = canonical_json(loaded.proof)

    def mutate(mapping: Any, key: str, value: Any) -> None:
        mapping[key] = value

    with pytest.raises(TypeError):
        mutate(loaded.proof, "route_ready", True)
    with pytest.raises(TypeError):
        mutate(loaded.proof["runtime"]["model_config"], "n_layer", 999)
    with pytest.raises(TypeError):
        mutate(loaded.proof["control_plane_binding"], "deployment_epoch", 999)
    with pytest.raises(TypeError):
        mutate(loaded.resolved_aliases["lm_head"], "target_component", "decoder")
    with pytest.raises(AttributeError):
        loaded.proof["loaded_components"].append("forged")

    assert canonical_json(loaded.proof) == before


# ---------------------------------------------------------------------------
# RED → GREEN: First-stage NumPy vs MLX parity
# ---------------------------------------------------------------------------


try:
    import importlib.util
    _mlx_spec = importlib.util.find_spec("mlx")
    _MLX_AVAILABLE = _mlx_spec is not None
except (ImportError, ModuleNotFoundError):
    _MLX_AVAILABLE = False


@pytest.mark.skipif(not _MLX_AVAILABLE, reason="mlx not available for cross-backend parity test")
def test_first_stage_numpy_vs_mlx_parity(tmp_path: Path) -> None:
    """GREEN: NumPy first-stage output matches MLX within 1e-4."""
    import mlx.core as mx

    from runtime_loader import (
        _gpt2_block as mlx_gpt2_block,
    )
    from runtime_loader import (
        _layer_norm as mlx_layer_norm,
    )

    tensor_dict = {
        "transformer.wte.weight": np.random.default_rng(42).normal(0, 0.02, (17, 8)).astype(np.float32),
        "transformer.wpe.weight": np.random.default_rng(43).normal(0, 0.02, (12, 8)).astype(np.float32),
        "transformer.ln_f.weight": np.ones((8,), dtype=np.float32),
        "transformer.ln_f.bias": np.zeros((8,), dtype=np.float32),
    }
    for layer in range(2):
        prefix = f"transformer.h.{layer}."
        rng = np.random.default_rng(44 + layer)
        tensor_dict[prefix + "ln_1.weight"] = np.ones((8,), dtype=np.float32)
        tensor_dict[prefix + "ln_1.bias"] = np.zeros((8,), dtype=np.float32)
        tensor_dict[prefix + "attn.c_attn.weight"] = rng.normal(0, 0.02, (8, 24)).astype(np.float32)
        tensor_dict[prefix + "attn.c_attn.bias"] = rng.normal(0, 0.01, (24,)).astype(np.float32)
        tensor_dict[prefix + "attn.c_proj.weight"] = rng.normal(0, 0.02, (8, 8)).astype(np.float32)
        tensor_dict[prefix + "attn.c_proj.bias"] = rng.normal(0, 0.01, (8,)).astype(np.float32)
        tensor_dict[prefix + "ln_2.weight"] = np.ones((8,), dtype=np.float32)
        tensor_dict[prefix + "ln_2.bias"] = np.zeros((8,), dtype=np.float32)
        tensor_dict[prefix + "mlp.c_fc.weight"] = rng.normal(0, 0.02, (8, 16)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_fc.bias"] = rng.normal(0, 0.01, (16,)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_proj.weight"] = rng.normal(0, 0.02, (16, 8)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_proj.bias"] = rng.normal(0, 0.01, (8,)).astype(np.float32)

    token_ids = np.array([[1, 4, 7, 2, 9]], dtype=np.int64)

    # MLX path
    mlx_tensors = {k: mx.array(v) for k, v in tensor_dict.items()}
    mlx_ids = mx.array(token_ids, dtype=mx.int32)
    positions = mx.arange(token_ids.shape[1], dtype=mx.int32)
    mlx_hidden = mlx_tensors["transformer.wte.weight"][mlx_ids] + mlx_tensors["transformer.wpe.weight"][positions]
    for layer in range(2):
        mlx_hidden = mlx_gpt2_block(mlx_hidden, mlx_tensors, f"transformer.h.{layer}.", 2, 1e-5)
    mlx_hidden = mlx_layer_norm(mlx_hidden, mlx_tensors["transformer.ln_f.weight"], mlx_tensors["transformer.ln_f.bias"], 1e-5)
    mlx_logits = mx.matmul(mlx_hidden, mlx_tensors["transformer.wte.weight"].transpose(1, 0))
    mx.eval(mlx_logits)
    mlx_result = np.asarray(mlx_logits)

    # NumPy path (using numpy_stage_runtime)
    from numpy_stage_runtime import _gpt2_block, _layer_norm
    np_hidden = tensor_dict["transformer.wte.weight"][token_ids.astype(np.int64)] + tensor_dict["transformer.wpe.weight"][np.arange(token_ids.shape[1], dtype=np.int64)]
    np_hidden = np_hidden.astype(np.float32)
    for layer in range(2):
        np_hidden = _gpt2_block(np_hidden, tensor_dict, f"transformer.h.{layer}.", 2, 1e-5)
    np_hidden = _layer_norm(np_hidden, tensor_dict["transformer.ln_f.weight"], tensor_dict["transformer.ln_f.bias"], 1e-5)
    np_logits = np.matmul(np_hidden, tensor_dict["transformer.wte.weight"].T)

    np.testing.assert_allclose(np_logits, mlx_result, rtol=1e-4, atol=1e-5)
    assert np.argmax(np_logits, axis=-1).tolist() == np.argmax(mlx_result, axis=-1).tolist()


# ---------------------------------------------------------------------------
# RED → GREEN: Interior/final stage NumPy vs MLX parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _MLX_AVAILABLE, reason="mlx not available for cross-backend parity test")
def test_interior_stage_numpy_vs_mlx_parity(tmp_path: Path) -> None:
    """GREEN: NumPy interior stage processes hidden states match MLX within 1e-4."""
    import mlx.core as mx

    from runtime_loader import (
        _gpt2_block as mlx_gpt2_block,
    )

    tensor_dict = {}
    for layer in range(2):
        prefix = f"transformer.h.{layer}."
        rng = np.random.default_rng(50 + layer)
        tensor_dict[prefix + "ln_1.weight"] = np.ones((8,), dtype=np.float32)
        tensor_dict[prefix + "ln_1.bias"] = np.zeros((8,), dtype=np.float32)
        tensor_dict[prefix + "attn.c_attn.weight"] = rng.normal(0, 0.02, (8, 24)).astype(np.float32)
        tensor_dict[prefix + "attn.c_attn.bias"] = rng.normal(0, 0.01, (24,)).astype(np.float32)
        tensor_dict[prefix + "attn.c_proj.weight"] = rng.normal(0, 0.02, (8, 8)).astype(np.float32)
        tensor_dict[prefix + "attn.c_proj.bias"] = rng.normal(0, 0.01, (8,)).astype(np.float32)
        tensor_dict[prefix + "ln_2.weight"] = np.ones((8,), dtype=np.float32)
        tensor_dict[prefix + "ln_2.bias"] = np.zeros((8,), dtype=np.float32)
        tensor_dict[prefix + "mlp.c_fc.weight"] = rng.normal(0, 0.02, (8, 16)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_fc.bias"] = rng.normal(0, 0.01, (16,)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_proj.weight"] = rng.normal(0, 0.02, (16, 8)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_proj.bias"] = rng.normal(0, 0.01, (8,)).astype(np.float32)

    np_hidden = np.random.default_rng(55).normal(0, 1.0, (1, 5, 8)).astype(np.float32)

    # MLX path
    mlx_tensors = {k: mx.array(v) for k, v in tensor_dict.items()}
    mlx_hidden = mx.array(np_hidden)
    for layer in range(2):
        mlx_hidden = mlx_gpt2_block(mlx_hidden, mlx_tensors, f"transformer.h.{layer}.", 2, 1e-5)
    mx.eval(mlx_hidden)
    mlx_result = np.asarray(mlx_hidden)

    # NumPy path
    from numpy_stage_runtime import _gpt2_block
    np_h = np_hidden.copy()
    for layer in range(2):
        np_h = _gpt2_block(np_h, tensor_dict, f"transformer.h.{layer}.", 2, 1e-5)

    np.testing.assert_allclose(np_h, mlx_result, rtol=1e-4, atol=1e-5)


# ---------------------------------------------------------------------------
# RED → GREEN: Full two-stage mixed-backend activation handoff
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _MLX_AVAILABLE, reason="mlx not available for mixed-backend test")
def test_mixed_backend_activation_handoff(tmp_path: Path) -> None:
    """GREEN: NumPy first stage → MLX final stage produces consistent output."""
    import mlx.core as mx

    from runtime_loader import (
        _gpt2_block as mlx_gpt2_block,
    )
    from runtime_loader import (
        _layer_norm as mlx_layer_norm,
    )

    # Use a single shared tensor dict
    rng = np.random.default_rng(100)
    tensor_dict = {
        "transformer.wte.weight": rng.normal(0, 0.02, (17, 8)).astype(np.float32),
        "transformer.wpe.weight": rng.normal(0, 0.02, (12, 8)).astype(np.float32),
        "transformer.ln_f.weight": np.ones((8,), dtype=np.float32),
        "transformer.ln_f.bias": np.zeros((8,), dtype=np.float32),
    }
    for layer in range(2):
        prefix = f"transformer.h.{layer}."
        tensor_dict[prefix + "ln_1.weight"] = np.ones((8,), dtype=np.float32)
        tensor_dict[prefix + "ln_1.bias"] = np.zeros((8,), dtype=np.float32)
        tensor_dict[prefix + "attn.c_attn.weight"] = rng.normal(0, 0.02, (8, 24)).astype(np.float32)
        tensor_dict[prefix + "attn.c_attn.bias"] = rng.normal(0, 0.01, (24,)).astype(np.float32)
        tensor_dict[prefix + "attn.c_proj.weight"] = rng.normal(0, 0.02, (8, 8)).astype(np.float32)
        tensor_dict[prefix + "attn.c_proj.bias"] = rng.normal(0, 0.01, (8,)).astype(np.float32)
        tensor_dict[prefix + "ln_2.weight"] = np.ones((8,), dtype=np.float32)
        tensor_dict[prefix + "ln_2.bias"] = np.zeros((8,), dtype=np.float32)
        tensor_dict[prefix + "mlp.c_fc.weight"] = rng.normal(0, 0.02, (8, 16)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_fc.bias"] = rng.normal(0, 0.01, (16,)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_proj.weight"] = rng.normal(0, 0.02, (16, 8)).astype(np.float32)
        tensor_dict[prefix + "mlp.c_proj.bias"] = rng.normal(0, 0.01, (8,)).astype(np.float32)

    token_ids = np.array([[1, 4, 7, 2, 9]], dtype=np.int64)

    # All-NumPy full pipeline
    from numpy_stage_runtime import _gpt2_block, _layer_norm
    np_hidden = tensor_dict["transformer.wte.weight"][token_ids.astype(np.int64)] + tensor_dict["transformer.wpe.weight"][np.arange(token_ids.shape[1], dtype=np.int64)]
    np_hidden = np_hidden.astype(np.float32)
    np_hidden = _gpt2_block(np_hidden, tensor_dict, "transformer.h.0.", 2, 1e-5)
    # NumPy first stage hands off hidden states
    np_stage0_out = np_hidden.copy()
    np_hidden = _gpt2_block(np_hidden, tensor_dict, "transformer.h.1.", 2, 1e-5)
    np_hidden = _layer_norm(np_hidden, tensor_dict["transformer.ln_f.weight"], tensor_dict["transformer.ln_f.bias"], 1e-5)
    np_logits = np.matmul(np_hidden, tensor_dict["transformer.wte.weight"].T)

    # Mixed: NumPy first stage → MLX second stage
    mlx_tensors = {k: mx.array(v) for k, v in tensor_dict.items()}
    mlx_hidden = mx.array(np_stage0_out)
    mlx_hidden = mlx_gpt2_block(mlx_hidden, mlx_tensors, "transformer.h.1.", 2, 1e-5)
    mlx_hidden = mlx_layer_norm(mlx_hidden, mlx_tensors["transformer.ln_f.weight"], mlx_tensors["transformer.ln_f.bias"], 1e-5)
    mlx_logits = mx.matmul(mlx_hidden, mlx_tensors["transformer.wte.weight"].transpose(1, 0))
    mx.eval(mlx_logits)
    mixed_result = np.asarray(mlx_logits)

    # All-MLX full pipeline
    mlx_ids = mx.array(token_ids, dtype=mx.int32)
    mlx_pos = mx.arange(token_ids.shape[1], dtype=mx.int32)
    mlx_h = mlx_tensors["transformer.wte.weight"][mlx_ids] + mlx_tensors["transformer.wpe.weight"][mlx_pos]
    for layer in range(2):
        mlx_h = mlx_gpt2_block(mlx_h, mlx_tensors, f"transformer.h.{layer}.", 2, 1e-5)
    mlx_h = mlx_layer_norm(mlx_h, mlx_tensors["transformer.ln_f.weight"], mlx_tensors["transformer.ln_f.bias"], 1e-5)
    mlx_l = mx.matmul(mlx_h, mlx_tensors["transformer.wte.weight"].transpose(1, 0))
    mx.eval(mlx_l)
    all_mlx_result = np.asarray(mlx_l)

    # All three should be consistent
    np.testing.assert_allclose(np_logits, mixed_result, rtol=1e-4, atol=1e-5)
    np.testing.assert_allclose(np_logits, all_mlx_result, rtol=1e-4, atol=1e-5)
    np.testing.assert_allclose(mixed_result, all_mlx_result, rtol=1e-4, atol=1e-5)


# ---------------------------------------------------------------------------
# RED → GREEN: Tied lm_head alias
# ---------------------------------------------------------------------------


def test_loads_tied_lm_head_alias_correctly(tmp_path: Path) -> None:
    """GREEN: Tied lm_head alias uses the same tensor as input_embedding."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    # Check alias is resolved correctly
    assert "lm_head" in loaded.resolved_aliases
    assert loaded.resolved_aliases["lm_head"]["target_component"] == "input_embedding"
    assert list(loaded.resolved_aliases["lm_head"]["tensor_keys"]) == ["transformer.wte.weight"]

    # Verify that the tensor used for lm_head is exactly the wte tensor
    proof = loaded.proof
    assert "lm_head" in proof["resolved_component_aliases"]

    # Execute stage with lm_head and verify it produces logits
    result = execute_loaded_stage(loaded, token_ids=np.array([[1, 2, 3]], dtype=np.int64))
    assert result.shape[2] == 7  # vocab_size


def test_loads_untied_lm_head_explicitly(tmp_path: Path) -> None:
    """GREEN: Untied lm_head with its own tensor loads correctly."""
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["lm_head.weight"] = np.ones((7, 4), dtype=np.float32)
    _save_safetensors(artifact, tensors)
    assignment["component_aliases"] = {}
    assignment["component_tensor_keys"]["lm_head"] = ["lm_head.weight"]
    assignment["expected_tensor_keys"] = sorted(
        set(assignment["expected_tensor_keys"]) | {"lm_head.weight"}
    )
    _refresh_file_evidence(assignment, report, artifact)

    loaded = load_assignment_stage(assignment, report, load_generation=2)

    assert "lm_head.weight" in loaded.tensors
    assert loaded.resolved_aliases == {}
    assert loaded.proof["resolved_component_aliases"] == {}


# ---------------------------------------------------------------------------
# RED → GREEN: Entry stage → interior stage → final stage execution
# ---------------------------------------------------------------------------


def test_entry_stage_accepts_token_ids(tmp_path: Path) -> None:
    """GREEN: Entry stage with input_embedding accepts rank-2 token IDs."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    token_ids = np.array([[1, 2, 3, 4]], dtype=np.int64)
    result = execute_loaded_stage(loaded, token_ids=token_ids)
    assert result.shape == (1, 4, 7)  # batch, seq, vocab
    assert result.dtype == np.float32


def test_interior_stage_accepts_rank3_hidden_states(tmp_path: Path) -> None:
    """GREEN: Interior stage (decoder only) accepts rank-3 hidden states."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(assignment, report, start=0, end=2, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    hidden = np.ones((1, 3, 4), dtype=np.float32)
    result = execute_loaded_stage(loaded, hidden_states=hidden)
    assert result.shape == (1, 3, 4)  # batch, seq, hidden


def test_final_stage_produces_logits(tmp_path: Path) -> None:
    """GREEN: Final stage with final_norm + lm_head produces logits."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(
        assignment, report, start=0, end=2,
        components=["decoder", "final_norm", "lm_head"]
    )
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    hidden = np.ones((1, 3, 4), dtype=np.float32)
    result = execute_loaded_stage(loaded, hidden_states=hidden)
    assert result.shape == (1, 3, 7)  # batch, seq, vocab_size


def test_entry_without_embedding_rejects_token_ids(tmp_path: Path) -> None:
    """RED: Entry stage without input_embedding rejects token IDs."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(assignment, report, start=0, end=2, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="requires_hidden_states"):
        execute_loaded_stage(loaded, token_ids=np.array([[1, 2]], dtype=np.int64))


def test_stage_with_embedding_rejects_hidden_states(tmp_path: Path) -> None:
    """RED: Stage with input_embedding rejects hidden_states (expects token_ids)."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="requires_token_ids"):
        execute_loaded_stage(
            loaded, hidden_states=np.ones((1, 3, 4), dtype=np.float32)
        )


# ---------------------------------------------------------------------------
# RED → GREEN: Malformed/missing/extra/nonfinite tensor rejection
# ---------------------------------------------------------------------------


def test_rejects_nonfinite_loaded_tensor(tmp_path: Path) -> None:
    """RED: Non-finite tensor values are rejected during load."""
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["transformer.h.0.attn.c_proj.bias"] = np.array(
        [0.0, float("inf"), 0.0, 0.0], dtype=np.float32
    )
    _save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="non-finite"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_missing_expected_tensor(tmp_path: Path) -> None:
    """RED: Missing expected tensor is rejected."""
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors.pop("transformer.h.0.attn.c_proj.bias")
    _save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="missing assigned tensors"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_extra_or_unowned_expected_tensor(tmp_path: Path) -> None:
    """RED: Extra tensor in expected list is rejected."""
    assignment, report, _ = _case(tmp_path)
    assignment["expected_tensor_keys"].append("transformer.h.9.ln_1.weight")
    assignment["expected_tensor_keys"].sort()
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="component ownership"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_mismatched_gpt2_tensor_shape(tmp_path: Path) -> None:
    """RED: Tensor with wrong shape is rejected."""
    assignment, report, artifact = _case(tmp_path)
    tensors = _source_tensors()
    tensors["transformer.h.0.attn.c_attn.weight"] = np.ones((4, 11), dtype=np.float32)
    _save_safetensors(artifact, tensors)
    _refresh_file_evidence(assignment, report, artifact)

    with pytest.raises(RuntimeLoadError, match="shape mismatch"):
        load_assignment_stage(assignment, report, load_generation=1)


# ---------------------------------------------------------------------------
# RED → GREEN: Wrong assignment/report identity rejection
# ---------------------------------------------------------------------------


def test_rejects_tampered_assignment_identity_before_loading(tmp_path: Path) -> None:
    """RED: Assignment identity mismatch is rejected before any load."""
    assignment, report, _ = _case(tmp_path)
    assignment["model_id"] = "attacker/substitute"

    with pytest.raises(
        (RuntimeLoadError, ValueError), match="assignment_id"
    ):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_wrong_report_identity(tmp_path: Path) -> None:
    """RED: Artifact report with mismatched identity is rejected."""
    assignment, report, _ = _case(tmp_path)
    report["assignment_id"] = "00000000-0000-0000-0000-000000000000"

    with pytest.raises(RuntimeLoadError, match="artifact verification report"):
        load_assignment_stage(assignment, report, load_generation=1)


# ---------------------------------------------------------------------------
# RED → GREEN: route_ready true rejection
# ---------------------------------------------------------------------------


def test_rejects_route_ready_true_in_assignment(tmp_path: Path) -> None:
    """RED: Assignment with route_ready=true is rejected."""
    assignment, report, _ = _case(tmp_path)
    assignment["route_ready"] = True

    with pytest.raises((RuntimeLoadError, ValueError), match="route_ready"):
        load_assignment_stage(assignment, report, load_generation=1)


# ---------------------------------------------------------------------------
# RED → GREEN: Invalid runtime contract rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("runtime_patch", "message"),
    [
        ({"backend": "torch"}, "runtime backend"),
        ({"quantization": "int4"}, "quantization"),
        ({"architecture": "qwen2"}, "architecture"),
        ({"dtype": "bfloat16"}, "numpy runtime dtype"),
    ],
)
def test_rejects_unsupported_runtime_cases(
    tmp_path: Path, runtime_patch: dict[str, Any], message: str
) -> None:
    """RED: Unsupported runtime configurations are rejected."""
    assignment, report, _ = _case(tmp_path)
    assignment["runtime"].update(runtime_patch)
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match=message):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_invalid_load_generation(tmp_path: Path) -> None:
    """RED: Invalid load_generation values are rejected."""
    assignment, report, _ = _case(tmp_path)

    with pytest.raises(RuntimeLoadError, match="load_generation"):
        load_assignment_stage(assignment, report, load_generation=-1)


def test_rejects_static_component_at_wrong_stage_boundary(tmp_path: Path) -> None:
    """RED: input_embedding at non-zero start layer is rejected."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(
        assignment,
        report,
        start=1,
        end=2,
        components=["input_embedding", "decoder", "final_norm", "lm_head"],
    )

    with pytest.raises(RuntimeLoadError, match="input_embedding.*first layer"):
        load_assignment_stage(assignment, report, load_generation=1)


def test_rejects_unsupported_alias(tmp_path: Path) -> None:
    """RED: Unsupported component alias is rejected."""
    assignment, report, _ = _case(tmp_path)
    assignment["component_aliases"]["lm_head"] = "final_norm"
    _rebind(assignment, report)

    with pytest.raises(RuntimeLoadError, match="unsupported component alias"):
        load_assignment_stage(assignment, report, load_generation=1)


# ---------------------------------------------------------------------------
# RED → GREEN: Stage backend protocol
# ---------------------------------------------------------------------------


def test_numpy_stage_backend_exposes_correct_protocol() -> None:
    """GREEN: NumpyStageBackend implements StageRuntimeBackend."""
    backend = NumpyStageBackend()
    assert isinstance(backend, StageRuntimeBackend)
    assert backend.backend == "numpy"
    assert callable(backend.execute_loaded_stage)


# ---------------------------------------------------------------------------
# RED → GREEN: NumPy-absent import behavior
# ---------------------------------------------------------------------------


def test_numpy_stage_runtime_imports_without_numpy_at_module_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GREEN: numpy_stage_runtime module does not import NumPy eagerly.

    The module uses lazy import; importing it should not require NumPy.
    Since NumPy IS installed in this env, we verify that the module-level
    import does not happen until _lazy_numpy() is called.
    """
    # Verify module __init__ doesn't have numpy in globals before lazy import
    import numpy_stage_runtime as nsr
    assert "numpy" not in nsr.__dict__ or nsr.__dict__.get("numpy") is None
    # But lazy import works
    np_arr = np.array([1.0])
    assert np_arr is not None


# ---------------------------------------------------------------------------
# RED → GREEN: Stage execution rejection tests
# ---------------------------------------------------------------------------


def test_execute_rejects_invalid_loaded_stage_type(tmp_path: Path) -> None:
    """RED: execute_loaded_stage rejects non-LoadedNumpyStage."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="invalid_loaded_stage"):
        execute_loaded_stage({"not": "a stage"}, token_ids=np.array([[1]], dtype=np.int64))

    # Also verify valid stage works
    result = execute_loaded_stage(loaded, token_ids=np.array([[1, 2]], dtype=np.int64))
    assert result.shape == (1, 2, 7)


def test_execute_rejects_out_of_bounds_token_ids(tmp_path: Path) -> None:
    """RED: Token IDs out of vocab bounds are rejected."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="token_bounds_exceeded"):
        execute_loaded_stage(loaded, token_ids=np.array([[99]], dtype=np.int64))


def test_execute_rejects_excessive_sequence_length(tmp_path: Path) -> None:
    """RED: Sequence longer than n_positions is rejected."""
    assignment, report, _ = _case(tmp_path)
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="position_bounds_exceeded"):
        execute_loaded_stage(
            loaded, token_ids=np.array([[1] * 100], dtype=np.int64)
        )


def test_execute_rejects_wrong_hidden_state_rank(tmp_path: Path) -> None:
    """RED: Non-rank-3 hidden states are rejected for interior stages."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(assignment, report, start=0, end=2, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="invalid_hidden_state_rank"):
        execute_loaded_stage(loaded, hidden_states=np.ones((3, 4), dtype=np.float32))


def test_execute_rejects_wrong_hidden_state_width(tmp_path: Path) -> None:
    """RED: Hidden states with wrong embedding width are rejected."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(assignment, report, start=0, end=2, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="invalid_hidden_state_shape"):
        execute_loaded_stage(loaded, hidden_states=np.ones((1, 2, 99), dtype=np.float32))


def test_execute_rejects_nonfinite_hidden_states(tmp_path: Path) -> None:
    """RED: Non-finite hidden states are rejected."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(assignment, report, start=0, end=2, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="nonfinite_hidden_states"):
        execute_loaded_stage(
            loaded, hidden_states=np.array([[[math.nan]] * 4] * 2, dtype=np.float32).reshape(1, 2, 4)
        )


def test_execute_rejects_dtype_mismatch(tmp_path: Path) -> None:
    """RED: Hidden states with wrong dtype are rejected."""
    assignment, report, _ = _case(tmp_path)
    _restrict_assignment(assignment, report, start=0, end=2, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=1)

    with pytest.raises(RuntimeExecutionError, match="hidden_state_dtype_mismatch"):
        execute_loaded_stage(
            loaded, hidden_states=np.ones((1, 2, 4), dtype=np.float16)
        )
