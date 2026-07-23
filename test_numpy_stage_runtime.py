#!/usr/bin/env python3
"""RED-GREEN tests for the optional-MLX assignment-local NumPy stage runtime.

These tests prove the assignment-bound NumPy stage backend:

- imports when ``mlx`` is absent (no fake module) and selects the real NumPy
  backend;
- rejects explicit MLX selection when MLX cannot be imported with a stable
  ``backend_unavailable`` error (not an import traceback);
- consumes an already authenticated ``LoadedStage`` (assignment-local tensors
  only) and executes the entry/intermediate/final roles;
- keeps ``route_ready`` false for any fallback path and never claims physical
  readiness;
- rejects invalid dtype/shape/nonfinite/assignment-mismatch/unverified-pack
  inputs **before** any execution;
- stays numerically within the frozen monolithic NumPy-vs-MLX tolerance and
  emits the exact greedy token parity versus the MLX reference.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import mlx.core as mx
import numpy as np
import pytest

import numpy_runtime
import runtime_loader
from numpy_runtime import NumpyGPT2Runtime, NumpyStageBackend, tensor_digest
from runtime_contracts import (
    MonolithicRuntimePort,
    StageRuntimeBackend,
    assignment_stage_role,
    validate_normalized_numpy_runtime,
    validate_normalized_runtime,
)
from runtime_loader import (
    LoadedStage,
    MLXStageBackend,
    RuntimeExecutionError,
    RuntimeLoadError,
    _numpy_runtime_dtypes,
    canonical_json,
    execute_loaded_numpy_stage,
    load_assignment_stage,
    select_stage_backend,
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


def _numpy_runtime() -> dict[str, Any]:
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


def _mlx_runtime() -> dict[str, Any]:
    runtime = _numpy_runtime()
    runtime["backend"] = "mlx"
    return runtime


def _run_without_mlx(source: str) -> None:
    """Run a cold import with every real ``mlx`` module blocked."""

    bootstrap = f"""
import importlib.abc
import sys

class BlockMLX(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "mlx" or fullname.startswith("mlx."):
            raise ModuleNotFoundError("mlx blocked by optional-import probe")
        return None

sys.meta_path.insert(0, BlockMLX())
{textwrap.dedent(source)}
"""
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=Path(__file__).parent,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def _mlx_values(
    shape: tuple[int, ...], *, offset: int = 0, scale: float = 0.01
) -> mx.array:
    size = 1
    for dimension in shape:
        size *= dimension
    return (mx.arange(size, dtype=mx.float32).reshape(shape) + offset) * scale


def _layer_tensors(layer: int, *, offset: int = 0) -> dict[str, mx.array]:
    prefix = f"transformer.h.{layer}."
    return {
        prefix + "ln_1.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_1.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "attn.c_attn.weight": _mlx_values((4, 12), offset=offset + 1, scale=0.002),
        prefix + "attn.c_attn.bias": _mlx_values((12,), offset=offset + 2, scale=0.001),
        prefix + "attn.c_proj.weight": _mlx_values((4, 4), offset=offset + 3, scale=0.002),
        prefix + "attn.c_proj.bias": _mlx_values((4,), offset=offset + 4, scale=0.001),
        prefix + "ln_2.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_2.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "mlp.c_fc.weight": _mlx_values((4, 8), offset=offset + 5, scale=0.002),
        prefix + "mlp.c_fc.bias": _mlx_values((8,), offset=offset + 6, scale=0.001),
        prefix + "mlp.c_proj.weight": _mlx_values((8, 4), offset=offset + 7, scale=0.002),
        prefix + "mlp.c_proj.bias": _mlx_values((4,), offset=offset + 8, scale=0.001),
    }


def _source_tensors(*, weight_offset: int = 0) -> dict[str, mx.array]:
    token_values = _mlx_values(
        (7, 4), offset=weight_offset + 1, scale=0.07
    )
    tensors: dict[str, mx.array] = {
        "transformer.wte.weight": (
            mx.sin(token_values) + 0.05 * mx.cos(3.0 * token_values)
        ),
        "transformer.wpe.weight": _mlx_values(
            (8, 4), offset=weight_offset + 2, scale=0.005
        ),
        "transformer.ln_f.weight": mx.ones((4,), dtype=mx.float32),
        "transformer.ln_f.bias": mx.zeros((4,), dtype=mx.float32),
        # Stock shards may overfetch other stages; must never enter the loaded stage.
        "transformer.h.9.ln_1.weight": mx.ones((4,), dtype=mx.float32) * 9,
    }
    tensors.update(_layer_tensors(0, offset=weight_offset + 10))
    tensors.update(_layer_tensors(1, offset=weight_offset + 30))
    return tensors


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


def _rebind(assignment: dict[str, Any], report: dict[str, Any] | None = None) -> None:
    from layer_assignment import assignment_id_for

    assignment["assignment_id"] = assignment_id_for(assignment)
    if report is not None:
        report["assignment_id"] = assignment["assignment_id"]
        report["verified_tensor_count"] = len(set(assignment["expected_tensor_keys"]))


def _case(
    tmp_path: Path,
    *,
    backend: str = "mlx",
    weight_offset: int = 0,
) -> tuple[dict[str, Any], dict[str, Any], Path]:
    artifact = tmp_path / "model.safetensors"
    source_tensors = _source_tensors(weight_offset=weight_offset)
    mx.save_safetensors(artifact, source_tensors)

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
        {key for component_keys in component_tensor_keys.values() for key in component_keys}
    )
    size = artifact.stat().st_size
    from weight_provisioning import sha256_file

    digest = "sha256:" + sha256_file(artifact)
    runtime = _numpy_runtime() if backend == "numpy" else _mlx_runtime()
    assignment: dict[str, Any] = {
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
        "runtime": runtime,
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
        "range": {**assignment["range"]},
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


def _refresh(
    assignment: dict[str, Any], report: dict[str, Any], artifact: Path
) -> None:
    from weight_provisioning import sha256_file

    size = artifact.stat().st_size
    digest = "sha256:" + sha256_file(artifact)
    assignment["files"][0]["size_bytes"] = size
    assignment["files"][0]["content_digest"] = digest
    report["verified_files"][0]["size_bytes"] = size
    report["verified_files"][0]["content_digest"] = digest
    report["expected_bytes"] = size
    report["network_download_bytes"] = 0
    report["cache_hit_bytes"] = size
    _rebind(assignment, report)


def _restrict(
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
    report["range"] = {**assignment["range"]}
    report["verified_tensor_prefixes"] = prefixes
    _rebind(assignment, report)


# ---------------------------------------------------------------------------
# Contracts and lazy import surface
# ---------------------------------------------------------------------------


def test_assignment_stage_role_dispatches_components() -> None:
    assert assignment_stage_role({"input_embedding", "decoder"}) == "entry"
    assert (
        assignment_stage_role({"decoder", "final_norm", "lm_head"})
        == "final"
    )
    assert assignment_stage_role({"decoder"}) == "intermediate"
    assert assignment_stage_role({"input_embedding", "decoder", "final_norm"}) == "entry"
    with pytest.raises(ValueError):
        assignment_stage_role({"decoder", "unknown_component"})


def test_stage_runtime_backend_protocol_recognizes_existing_adapters() -> None:
    mlx = MLXStageBackend()
    numpy = NumpyStageBackend()
    assert isinstance(mlx, StageRuntimeBackend)
    assert isinstance(numpy, StageRuntimeBackend)
    assert mlx.backend == "mlx"
    assert numpy.backend == "numpy"


def test_numpy_runtime_dtypes_are_backend_neutral() -> None:
    dtypes = _numpy_runtime_dtypes()
    assert set(dtypes) == {"float32"}


def test_numpy_runtime_contract_rejects_unproven_float16_capability() -> None:
    runtime = _numpy_runtime()
    runtime["dtype"] = "float16"
    with pytest.raises(ValueError, match="expected float32"):
        validate_normalized_numpy_runtime(runtime)


def test_runtime_contracts_import_without_mlx() -> None:
    """Importing the contracts must not require mlx to be importable."""
    _run_without_mlx(
        f"""
import runtime_contracts
runtime = {_numpy_runtime()!r}
assert runtime_contracts.validate_normalized_numpy_runtime(runtime) == runtime
assert "mlx" not in sys.modules
"""
    )


def test_numpy_runtime_imports_when_mlx_absent() -> None:
    """``numpy_runtime`` must import even when ``mlx`` is unavailable."""
    _run_without_mlx(
        """
import numpy_runtime
assert numpy_runtime.NumpyStageBackend().backend == "numpy"
assert "mlx" not in sys.modules
"""
    )


def test_runtime_loader_imports_when_mlx_absent() -> None:
    """``runtime_loader`` must cold-import without creating a fake ``mlx``."""
    _run_without_mlx(
        """
import runtime_loader
assert runtime_loader.MLXStageBackend().backend == "mlx"
assert runtime_loader.NumpyStageBackend().backend == "numpy"
assert "mlx" not in sys.modules
"""
    )


def test_explicit_mlx_selection_absent_raises_stable_error() -> None:
    """Selecting the MLX backend when MLX is unimportable must surface as a
    stable ``backend_unavailable`` code, not an ImportError traceback."""
    _run_without_mlx(
        f"""
import runtime_loader
runtime = {_mlx_runtime()!r}
try:
    runtime_loader.select_stage_backend(runtime=runtime, prefer="mlx")
except runtime_loader.RuntimeLoadError as exc:
    assert str(exc).startswith("backend_unavailable")
else:
    raise AssertionError("explicit MLX selection unexpectedly succeeded")
"""
    )


def test_auto_selection_with_numpy_runtime_picks_numpy_backend() -> None:
    _run_without_mlx(
        f"""
import runtime_loader
runtime = {_numpy_runtime()!r}
backend = runtime_loader.select_stage_backend(runtime=runtime, prefer="auto")
assert backend.backend == "numpy"
assert "mlx" not in sys.modules
"""
    )


def test_select_stage_backend_rejects_unknown_prefer() -> None:
    with pytest.raises(RuntimeLoadError, match="unknown_prefer"):
        select_stage_backend(runtime=_numpy_runtime(), prefer="unknown")  # type: ignore[arg-type]


def test_select_stage_backend_rejects_runtime_mismatch() -> None:
    with pytest.raises(RuntimeLoadError, match="runtime_mismatch"):
        select_stage_backend(runtime=_mlx_runtime(), prefer="numpy")


# ---------------------------------------------------------------------------
# Stage backend execution on already authenticated assignment-local tensors
# ---------------------------------------------------------------------------


def _load_loaded_stage(tmp_path: Path, *, backend: str = "numpy") -> LoadedStage:
    assignment, report, _ = _case(tmp_path, backend=backend)
    return load_assignment_stage(assignment, report, load_generation=11)


def test_loaded_stage_consumes_only_assignment_owned_tensors(
    tmp_path: Path,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    assert set(loaded.tensors) == set(loaded.proof["loaded_tensor_keys"])
    for key in loaded.tensors:
        assert key in loaded.proof["loaded_tensor_keys"]


def test_loaded_numpy_stage_executes_entry_and_final_roles(
    tmp_path: Path,
) -> None:
    assignment, report, _ = _case(tmp_path, backend="numpy")
    _restrict(
        assignment,
        report,
        start=0,
        end=1,
        components=["input_embedding", "decoder"],
    )
    loaded = load_assignment_stage(assignment, report, load_generation=11)
    backend = NumpyStageBackend()
    tokens = mx.array([[1, 2, 3]], dtype=mx.int32)
    hidden = execute_loaded_numpy_stage(loaded, token_ids=tokens)
    assert isinstance(backend, StageRuntimeBackend)
    assert hidden.shape == (1, 3, _numpy_runtime()["model_config"]["n_embd"])
    assert np.isfinite(np.asarray(hidden)).all()

    # A full entry-to-terminal assignment produces logits in one execution.
    terminal = _load_loaded_stage(tmp_path, backend="numpy")
    logits = execute_loaded_numpy_stage(terminal, token_ids=tokens)
    assert logits.shape == (1, 3, _numpy_runtime()["model_config"]["vocab_size"])
    assert np.isfinite(np.asarray(logits)).all()


def test_loaded_numpy_stage_executes_intermediate_role(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path, backend="numpy")
    _restrict(assignment, report, start=0, end=1, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=2)
    hidden_in = np.ones(
        (1, 3, _numpy_runtime()["model_config"]["n_embd"]), dtype=np.float32
    )
    hidden_out = execute_loaded_numpy_stage(loaded, hidden_states=hidden_in)
    assert hidden_out.shape == hidden_in.shape
    assert np.isfinite(hidden_out).all()


def test_loaded_numpy_stage_executes_terminal_role(tmp_path: Path) -> None:
    assignment, report, _ = _case(tmp_path, backend="numpy")
    _restrict(
        assignment,
        report,
        start=1,
        end=2,
        components=["decoder", "final_norm", "lm_head"],
    )
    loaded = load_assignment_stage(assignment, report, load_generation=3)
    hidden_in = np.ones(
        (1, 3, _numpy_runtime()["model_config"]["n_embd"]), dtype=np.float32
    )
    logits = execute_loaded_numpy_stage(loaded, hidden_states=hidden_in)
    assert logits.shape == (1, 3, _numpy_runtime()["model_config"]["vocab_size"])
    assert np.isfinite(logits).all()


def test_loaded_numpy_stage_rejects_invalid_role(tmp_path: Path) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    hidden = np.ones(
        (1, 3, _numpy_runtime()["model_config"]["n_embd"]), dtype=np.float32
    )
    # Entry stage with hidden_states is invalid.
    with pytest.raises(RuntimeLoadError, match="entry_stage_requires_token_ids"):
        execute_loaded_numpy_stage(loaded, hidden_states=hidden)
    # And passing both token_ids AND hidden_states is invalid.
    with pytest.raises(RuntimeLoadError, match="entry_stage_requires_token_ids"):
        execute_loaded_numpy_stage(
            loaded,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
            hidden_states=hidden,
        )


def test_loaded_numpy_stage_rejects_unverified_stage_pack(
    tmp_path: Path,
) -> None:
    assignment, report, _ = _case(tmp_path, backend="numpy")
    report["stage_pack_digest"] = "sha256:" + "1" * 64
    report["stage_pack_verification_digest"] = "sha256:" + "2" * 64
    with pytest.raises(RuntimeLoadError, match="stage-pack evidence must be complete"):
        load_assignment_stage(assignment, report, load_generation=11)


# ---------------------------------------------------------------------------
# Numerical parity and tamper rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("advertised_dtype", sorted(_numpy_runtime_dtypes()))
def test_loaded_numpy_stage_matches_mlx_within_frozen_tolerance_for_every_advertised_dtype(
    tmp_path: Path, advertised_dtype: str
) -> None:
    """Same assignment must produce numerically equivalent hidden states on
    the NumPy stage backend and the MLX reference within the frozen
    monolithic NumPy-vs-MLX tolerance and yield exact greedy token parity.
    """
    assignment, report, _ = _case(tmp_path, backend="numpy")
    assignment["runtime"]["dtype"] = advertised_dtype
    _rebind(assignment, report)
    _restrict(
        assignment,
        report,
        start=0,
        end=1,
        components=["input_embedding", "decoder"],
    )
    loaded = load_assignment_stage(assignment, report, load_generation=1)
    tokens = mx.array([[1, 2, 3]], dtype=mx.int32)
    numpy_hidden = execute_loaded_numpy_stage(loaded, token_ids=tokens)

    reference_assignment, reference_report, _ = _case(tmp_path, backend="mlx")
    reference_assignment["runtime"]["dtype"] = advertised_dtype
    _rebind(reference_assignment, reference_report)
    _restrict(
        reference_assignment,
        reference_report,
        start=0,
        end=1,
        components=["input_embedding", "decoder"],
    )
    reference_loaded = load_assignment_stage(
        reference_assignment, reference_report, load_generation=1
    )
    mlx_hidden = runtime_loader.execute_loaded_stage(
        reference_loaded, token_ids=tokens
    )

    np.testing.assert_allclose(
        np.asarray(numpy_hidden), np.asarray(mlx_hidden), rtol=2e-5, atol=2e-6
    )

    assignment, report, _ = _case(tmp_path, backend="numpy")
    assignment["runtime"]["dtype"] = advertised_dtype
    _rebind(assignment, report)
    loaded = load_assignment_stage(assignment, report, load_generation=1)
    numpy_logits = execute_loaded_numpy_stage(loaded, token_ids=tokens)
    reference_assignment, reference_report, _ = _case(tmp_path, backend="mlx")
    reference_assignment["runtime"]["dtype"] = advertised_dtype
    _rebind(reference_assignment, reference_report)
    reference_loaded = load_assignment_stage(
        reference_assignment, reference_report, load_generation=1
    )
    mlx_logits = runtime_loader.execute_loaded_stage(
        reference_loaded, token_ids=tokens
    )
    np.testing.assert_allclose(
        np.asarray(numpy_logits), np.asarray(mlx_logits), rtol=2e-5, atol=2e-6
    )
    assert np.argmax(np.asarray(numpy_logits), axis=-1).tolist() == np.argmax(
        np.asarray(mlx_logits), axis=-1
    ).tolist()


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
@pytest.mark.parametrize(
    ("proof_field", "expected_error"),
    [
        ("load_generation", "load_generation_mismatch"),
        ("runtime_identity", "runtime_identity_mismatch"),
    ],
)
def test_stage_execution_rejects_proof_only_authentication_tampering(
    tmp_path: Path,
    backend: str,
    proof_field: str,
    expected_error: str,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend=backend)
    tampered_proof = json.loads(canonical_json(loaded.proof))
    if proof_field == "load_generation":
        tampered_proof[proof_field] = -1
    else:
        tampered_proof[proof_field]["backend"] = (
            "mlx" if backend == "numpy" else "numpy"
        )
    tampered = replace(loaded, proof=tampered_proof)
    tokens = mx.array([[1, 2, 3]], dtype=mx.int32)
    error = RuntimeLoadError if backend == "numpy" else RuntimeExecutionError
    execute = (
        execute_loaded_numpy_stage
        if backend == "numpy"
        else runtime_loader.execute_loaded_stage
    )
    with pytest.raises(error, match=expected_error):
        execute(tampered, token_ids=tokens)


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
def test_stage_execution_authenticates_loaded_tensor_digest_before_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend=backend)
    tampered_proof = json.loads(canonical_json(loaded.proof))
    tampered_proof["loaded_tensor_digest"] = "sha256:" + "0" * 64
    tampered = replace(loaded, proof=tampered_proof)

    class BackendComputeReached(AssertionError):
        pass

    def reject_backend_compute(*args: Any, **kwargs: Any) -> None:
        raise BackendComputeReached("backend_compute_reached")

    if backend == "numpy":
        monkeypatch.setattr(numpy_runtime, "_gpt2_block", reject_backend_compute)
        execute = execute_loaded_numpy_stage
        error = RuntimeLoadError
    else:
        monkeypatch.setattr(runtime_loader, "_gpt2_block", reject_backend_compute)
        execute = runtime_loader.execute_loaded_stage
        error = RuntimeExecutionError

    with pytest.raises(error, match=r"^loaded_tensor_digest_mismatch$"):
        execute(
            tampered,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
@pytest.mark.parametrize(
    "tamper_case",
    [
        "resolved_only",
        "proof_only",
        "resolved_and_proof",
        "resolved_missing",
        "proof_missing",
        "authenticated_missing",
        "resolved_malformed",
        "proof_malformed",
        "authenticated_malformed",
        "resolved_serialization_failure",
        "proof_serialization_failure",
        "authenticated_serialization_failure",
    ],
)
def test_stage_execution_authenticates_resolved_aliases_before_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    tamper_case: str,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend=backend)
    alternate_aliases = {
        "lm_head": {
            "target_component": "input_embedding",
            "tensor_keys": ["transformer.wpe.weight"],
        }
    }
    tampered_proof = json.loads(canonical_json(loaded.proof))
    replacements: dict[str, Any] = {"proof": tampered_proof}

    if tamper_case in {"resolved_only", "resolved_and_proof"}:
        replacements["resolved_aliases"] = alternate_aliases
    if tamper_case in {"proof_only", "resolved_and_proof"}:
        tampered_proof["resolved_component_aliases"] = alternate_aliases
    if tamper_case == "resolved_missing":
        replacements["resolved_aliases"] = None
    elif tamper_case == "proof_missing":
        del tampered_proof["resolved_component_aliases"]
    elif tamper_case == "authenticated_missing":
        replacements["authenticated_resolved_aliases"] = None
    elif tamper_case == "resolved_malformed":
        replacements["resolved_aliases"] = {"lm_head": "malformed"}
    elif tamper_case == "proof_malformed":
        tampered_proof["resolved_component_aliases"] = {"lm_head": "malformed"}
    elif tamper_case == "authenticated_malformed":
        replacements["authenticated_resolved_aliases"] = {
            "lm_head": "malformed"
        }
    elif tamper_case == "resolved_serialization_failure":
        replacements["resolved_aliases"] = {1: "non-string-key"}
    elif tamper_case == "proof_serialization_failure":
        tampered_proof["resolved_component_aliases"] = {
            "lm_head": {"target_component": object()}
        }
    elif tamper_case == "authenticated_serialization_failure":
        replacements["authenticated_resolved_aliases"] = {
            1: "non-string-key"
        }
    tampered = replace(loaded, **replacements)

    class BackendComputeReached(AssertionError):
        pass

    def reject_backend_compute(*args: Any, **kwargs: Any) -> None:
        raise BackendComputeReached("backend_compute_reached")

    if backend == "numpy":
        monkeypatch.setattr(numpy_runtime, "_gpt2_block", reject_backend_compute)
        execute = execute_loaded_numpy_stage
        error = RuntimeLoadError
    else:
        monkeypatch.setattr(runtime_loader, "_gpt2_block", reject_backend_compute)
        execute = runtime_loader.execute_loaded_stage
        error = RuntimeExecutionError

    with pytest.raises(error, match=r"^resolved_aliases_mismatch$"):
        execute(
            tampered,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
def test_loaded_stage_executes_with_authenticated_tied_lm_head_alias(
    tmp_path: Path,
    backend: str,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend=backend)

    assert loaded.resolved_aliases is not loaded.authenticated_resolved_aliases
    assert (
        loaded.resolved_aliases["lm_head"]
        is not loaded.authenticated_resolved_aliases["lm_head"]
    )
    assert canonical_json(loaded.resolved_aliases) == canonical_json(
        loaded.authenticated_resolved_aliases
    )
    assert canonical_json(loaded.proof["resolved_component_aliases"]) == canonical_json(
        loaded.authenticated_resolved_aliases
    )
    with pytest.raises(TypeError):
        loaded.authenticated_resolved_aliases["lm_head"]["target_component"] = (
            "decoder"
        )
    logits = (
        execute_loaded_numpy_stage(
            loaded,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )
        if backend == "numpy"
        else runtime_loader.execute_loaded_stage(
            loaded,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )
    )
    assert logits.shape == (1, 3, _numpy_runtime()["model_config"]["vocab_size"])
    assert np.isfinite(np.asarray(logits)).all()


@pytest.mark.parametrize(
    "authenticated_digest",
    [None, "not-a-digest", "sha256:" + "0" * 64],
)
def test_loaded_mlx_stage_authenticates_frozen_tensor_digest_before_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    authenticated_digest: str | None,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="mlx")
    tampered = replace(
        loaded,
        authenticated_tensor_digest=authenticated_digest,
    )

    def reject_backend_compute(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("backend_compute_reached")

    monkeypatch.setattr(runtime_loader, "_gpt2_block", reject_backend_compute)
    with pytest.raises(
        RuntimeExecutionError,
        match=r"^loaded_tensor_digest_mismatch$",
    ):
        runtime_loader.execute_loaded_stage(
            tampered,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


@pytest.mark.parametrize(
    "proof_digest",
    [None, "not-a-digest"],
)
def test_loaded_mlx_stage_rejects_missing_or_malformed_proof_tensor_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof_digest: str | None,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="mlx")
    tampered_proof = json.loads(canonical_json(loaded.proof))
    if proof_digest is None:
        del tampered_proof["loaded_tensor_digest"]
    else:
        tampered_proof["loaded_tensor_digest"] = proof_digest
    tampered = replace(loaded, proof=tampered_proof)

    def reject_backend_compute(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("backend_compute_reached")

    monkeypatch.setattr(runtime_loader, "_gpt2_block", reject_backend_compute)
    with pytest.raises(
        RuntimeExecutionError,
        match=r"^loaded_tensor_digest_mismatch$",
    ):
        runtime_loader.execute_loaded_stage(
            tampered,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


def test_loaded_mlx_stage_authenticates_materialized_tensors_before_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="mlx")
    tensors = dict(loaded.tensors)
    tensor_key = "transformer.h.0.ln_1.weight"
    tensors[tensor_key] = tensors[tensor_key] + mx.ones_like(tensors[tensor_key])
    tampered = replace(loaded, tensors=MappingProxyType(tensors))

    def reject_backend_compute(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("backend_compute_reached")

    monkeypatch.setattr(runtime_loader, "_gpt2_block", reject_backend_compute)
    with pytest.raises(
        RuntimeExecutionError,
        match=r"^loaded_tensor_digest_mismatch$",
    ):
        runtime_loader.execute_loaded_stage(
            tampered,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


def test_loaded_mlx_stage_maps_tensor_digest_recomputation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="mlx")

    def reject_digest_recomputation(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("raw_backend_digest_detail")

    monkeypatch.setattr(runtime_loader, "_digest_arrays", reject_digest_recomputation)
    with pytest.raises(
        RuntimeExecutionError,
        match=r"^loaded_tensor_digest_mismatch$",
    ):
        runtime_loader.execute_loaded_stage(
            loaded,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
@pytest.mark.parametrize(
    ("tamper_case", "expected_error"),
    [
        ("terminal_to_entry", "loaded_components_mismatch"),
        ("entry_to_intermediate", "loaded_components_mismatch"),
        ("final_to_intermediate", "loaded_components_mismatch"),
        ("tied_lm_head_removal", "loaded_components_mismatch"),
        ("component_order_change", "loaded_components_mismatch"),
        ("legal_loaded_range_change", "loaded_range_mismatch"),
    ],
)
def test_stage_execution_authenticates_exact_loaded_role_evidence_before_compute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    tamper_case: str,
    expected_error: str,
) -> None:
    assignment, report, _ = _case(tmp_path, backend=backend)
    if tamper_case == "entry_to_intermediate":
        _restrict(
            assignment,
            report,
            start=0,
            end=1,
            components=["input_embedding", "decoder"],
        )
    elif tamper_case == "final_to_intermediate":
        _restrict(
            assignment,
            report,
            start=1,
            end=2,
            components=["decoder", "final_norm", "lm_head"],
        )
    elif tamper_case == "legal_loaded_range_change":
        _restrict(
            assignment,
            report,
            start=0,
            end=2,
            components=["decoder"],
        )

    loaded = load_assignment_stage(assignment, report, load_generation=12)
    tampered_proof = json.loads(canonical_json(loaded.proof))
    if tamper_case == "terminal_to_entry":
        tampered_proof["loaded_components"] = ["input_embedding", "decoder"]
    elif tamper_case in {"entry_to_intermediate", "final_to_intermediate"}:
        tampered_proof["loaded_components"] = ["decoder"]
    elif tamper_case == "tied_lm_head_removal":
        tampered_proof["loaded_components"].remove("lm_head")
    elif tamper_case == "component_order_change":
        tampered_proof["loaded_components"].reverse()
    else:
        tampered_proof["loaded_range"] = {
            "start_layer": 0,
            "end_layer_exclusive": 1,
            "layer_count": 1,
        }
    tampered = replace(loaded, proof=tampered_proof)

    class BackendComputeReached(AssertionError):
        pass

    def reject_backend_compute(*args: Any, **kwargs: Any) -> None:
        raise BackendComputeReached("backend_compute_reached")

    execute: Any
    error: type[Exception]
    if backend == "numpy":
        monkeypatch.setattr(numpy_runtime, "_gpt2_block", reject_backend_compute)
        execute = execute_loaded_numpy_stage
        error = RuntimeLoadError
    else:
        monkeypatch.setattr(runtime_loader, "_gpt2_block", reject_backend_compute)
        execute = runtime_loader.execute_loaded_stage
        error = RuntimeExecutionError

    if "input_embedding" in tampered_proof["loaded_components"]:
        execution_input = {
            "token_ids": mx.array([[1, 2, 3]], dtype=mx.int32),
        }
    else:
        execution_input = {
            "hidden_states": mx.ones(
                (1, 3, _numpy_runtime()["model_config"]["n_embd"]),
                dtype=mx.float32,
            ),
        }
    with pytest.raises(error, match=expected_error):
        execute(tampered, **execution_input)


@pytest.mark.parametrize("backend", ["numpy", "mlx"])
def test_loaded_stage_freezes_exact_role_authentication_fields(
    tmp_path: Path,
    backend: str,
) -> None:
    assignment, report, _ = _case(tmp_path, backend=backend)
    loaded = load_assignment_stage(assignment, report, load_generation=13)

    assert loaded.authenticated_loaded_components == tuple(assignment["components"])
    assert loaded.authenticated_loaded_range == assignment["range"]
    with pytest.raises(TypeError):
        loaded.authenticated_loaded_range["start_layer"] = 1


def test_loaded_numpy_stage_revalidates_terminal_layer_boundary_at_execution(
    tmp_path: Path,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    tensors = {
        key: value
        for key, value in loaded.tensors.items()
        if not key.startswith("transformer.h.1.")
    }
    digest = tensor_digest(tensors)
    tampered_proof = json.loads(canonical_json(loaded.proof))
    tampered_proof["loaded_range"] = {
        "start_layer": 0,
        "end_layer_exclusive": 1,
        "layer_count": 1,
    }
    tampered_proof["loaded_tensor_keys"] = sorted(tensors)
    tampered_proof["loaded_tensor_digest"] = digest
    nonterminal = replace(
        loaded,
        tensors=tensors,
        proof=tampered_proof,
        authenticated_tensor_digest=digest,
        authenticated_loaded_range=MappingProxyType(
            dict(tampered_proof["loaded_range"])
        ),
    )

    with pytest.raises(RuntimeLoadError, match="invalid_loaded_stage_boundaries"):
        execute_loaded_numpy_stage(
            nonterminal,
            token_ids=mx.array([[1, 2, 3]], dtype=mx.int32),
        )


def test_loaded_numpy_stage_rejects_invalid_dtype_for_entry(
    tmp_path: Path,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    with pytest.raises(RuntimeLoadError, match="invalid_token_id_dtype"):
        execute_loaded_numpy_stage(
            loaded, token_ids=np.array([[1, 2, 3]], dtype=np.float32)
        )


def test_loaded_numpy_stage_rejects_invalid_shape_for_entry(
    tmp_path: Path,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    with pytest.raises(RuntimeLoadError, match="invalid_token_id_shape"):
        execute_loaded_numpy_stage(
            loaded, token_ids=mx.array([1, 2, 3], dtype=mx.int32)
        )


def test_loaded_numpy_stage_rejects_nonfinite_hidden_states(
    tmp_path: Path,
) -> None:
    assignment, report, _ = _case(tmp_path, backend="numpy")
    _restrict(assignment, report, start=0, end=1, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=4)
    bad = np.ones(
        (1, 3, _numpy_runtime()["model_config"]["n_embd"]), dtype=np.float32
    )
    bad[0, 0, 0] = float("inf")
    with pytest.raises(RuntimeLoadError, match="nonfinite_hidden_states"):
        execute_loaded_numpy_stage(loaded, hidden_states=bad)


def test_loaded_numpy_stage_rejects_dtype_mismatch_for_hidden_states(
    tmp_path: Path,
) -> None:
    assignment, report, _ = _case(tmp_path, backend="numpy")
    _restrict(assignment, report, start=0, end=1, components=["decoder"])
    loaded = load_assignment_stage(assignment, report, load_generation=4)
    bad = np.ones(
        (1, 3, _numpy_runtime()["model_config"]["n_embd"]), dtype=np.float16
    )
    with pytest.raises(RuntimeLoadError, match="hidden_state_dtype_mismatch"):
        execute_loaded_numpy_stage(loaded, hidden_states=bad)


def test_loaded_numpy_stage_rejects_assignment_identity_mismatch(
    tmp_path: Path,
) -> None:
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    tampered_proof = json.loads(canonical_json(loaded.proof))
    tampered_proof["assignment_id"] = "attacker-assignment"
    tampered = LoadedStage(
        tensors=loaded.tensors,
        resolved_aliases=loaded.resolved_aliases,
        probe_output=loaded.probe_output,
        proof=tampered_proof,
    )
    with pytest.raises(RuntimeLoadError, match="assignment_id"):
        execute_loaded_numpy_stage(
            tampered, token_ids=mx.array([[1, 2, 3]], dtype=mx.int32)
        )


def test_fallback_never_claims_route_or_physical_readiness(
    tmp_path: Path,
) -> None:
    """The NumPy fallback must always set ``route_ready`` false and never
    claim physical readiness even after a successful stage execution."""
    loaded = _load_loaded_stage(tmp_path, backend="numpy")
    assert loaded.proof["route_ready"] is False
    backend = NumpyStageBackend()
    assert backend.backend == "numpy"
    identity = backend.runtime_identity() if hasattr(backend, "runtime_identity") else {}
    assert identity.get("route_ready", False) is False
    assert identity.get("claim_boundary", "").startswith(
        "assignment-bound local NumPy"
    ) or "no route challenge" in identity.get("claim_boundary", "")


def test_numpy_runtime_monolithic_identity_unchanged() -> None:
    """The pre-existing monolithic NumPy contract must remain intact."""
    runtime = _numpy_runtime()
    weights = {
        "transformer.wte.weight": np.ones(
            (runtime["model_config"]["vocab_size"], runtime["model_config"]["n_embd"]),
            dtype=np.float32,
        )
        * 0.1,
        "transformer.wpe.weight": np.ones(
            (runtime["model_config"]["n_positions"], runtime["model_config"]["n_embd"]),
            dtype=np.float32,
        )
        * 0.05,
        "transformer.ln_f.weight": np.ones(
            (runtime["model_config"]["n_embd"],), dtype=np.float32
        ),
        "transformer.ln_f.bias": np.zeros(
            (runtime["model_config"]["n_embd"],), dtype=np.float32
        ),
    }
    for layer in range(2):
        prefix = f"transformer.h.{layer}."
        weights.update(
            {
                prefix + "ln_1.weight": np.ones((4,), dtype=np.float32),
                prefix + "ln_1.bias": np.zeros((4,), dtype=np.float32),
                prefix + "attn.c_attn.weight": np.full((4, 12), 0.01, dtype=np.float32),
                prefix + "attn.c_attn.bias": np.zeros((12,), dtype=np.float32),
                prefix + "attn.c_proj.weight": np.full((4, 4), 0.01, dtype=np.float32),
                prefix + "attn.c_proj.bias": np.zeros((4,), dtype=np.float32),
                prefix + "ln_2.weight": np.ones((4,), dtype=np.float32),
                prefix + "ln_2.bias": np.zeros((4,), dtype=np.float32),
                prefix + "mlp.c_fc.weight": np.full((4, 8), 0.01, dtype=np.float32),
                prefix + "mlp.c_fc.bias": np.zeros((8,), dtype=np.float32),
                prefix + "mlp.c_proj.weight": np.full((8, 4), 0.01, dtype=np.float32),
                prefix + "mlp.c_proj.bias": np.zeros((4,), dtype=np.float32),
            }
        )
    backend = NumpyGPT2Runtime(runtime=runtime, tensors=weights)
    assert isinstance(backend, MonolithicRuntimePort)
    assert backend.runtime_identity["route_ready"] is False
    logits = backend.forward_token_ids(np.array([[1, 2, 3]], dtype=np.int64))
    assert np.isfinite(logits).all()


def test_stage_backend_exposes_runtime_identity_for_stage_path() -> None:
    backend = NumpyStageBackend()
    identity = backend.runtime_identity()
    assert identity["backend"] == "numpy"
    assert identity["device"] == "cpu"
    assert identity["route_ready"] is False


def test_validate_normalized_runtime_dispatches_numpy() -> None:
    runtime = _numpy_runtime()
    assert validate_normalized_runtime(runtime, expected_backend="numpy") == runtime
    assert validate_normalized_numpy_runtime(runtime) == runtime
