# SPDX-License-Identifier: AGPL-3.0-only
"""A9 platform-neutral int8/activation reachability from the runtime path.

What this module proves, and what it refuses to claim
-----------------------------------------------------

GREEN half (the promotion): ``runtime_loader.load_assignment_stage`` accepts an
explicit runtime int8 execution selection and, for ``platform-neutral-int8``,
really dispatches the A9 platform-neutral persistent int8 kernel
(``mycelium_platform_neutral.int8_execution``) over the *loaded* row-wise int8
weights of a real qwen2 ``int8-weight-only`` stage, and really drives the
reduced-precision activation frame codec
(``mycelium_platform_neutral.activation_transport``) over the probe activation.
The measured result (per-kernel materialization ledgers, integer reference
parity, codec byte counters and projection) lands in the load proof under the
``a9_int8_execution`` key.

RED half (the anti-regression control): the default selection is the qualified
``weight-quantization`` path.  It still loads, still emits exactly the same
load-proof field set, still stores ``Int8RowwiseWeight`` tensors, and does not
dispatch A9 at all — asserted here with a spy installed over the dispatch
target, not merely by absence of the report key.

Reachability, not qualification
-------------------------------

``reachable`` is not ``qualified``, and this module asserts that boundary
instead of describing it: the A9 kernel host is the portable ``numpy`` reference
(correctness-only, never accelerated), the decoded activation is the loader's
deterministic probe ramp rather than a model activation, no physical round has
been executed, and the accelerated native Windows x86_64 device blocker stays
open.  The physical acceptance cases stay unearned and travel with the report in
``acceptance_coverage``.

RED→GREEN receipt (how the reachability test fails before the promotion): with
the base ``runtime_loader.py`` (commit ``0fe6037``) restored in place, all eight
items in this module fail in 0.41 s — the four A9 loads raise
``TypeError: load_assignment_stage() got an unexpected keyword argument
'int8_execution'``, :func:`resolve_int8_execution` is absent
(``AttributeError: module 'runtime_loader' has no attribute
'resolve_int8_execution'``), and the default-selection control fails because the
dispatch target it spies on does not exist either.  GREEN is the same module
against the promoted loader: 8 passed, and the qualified default path's canonical
load proof is byte-identical to the base tree's.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import runtime_loader
from layer_assignment import assignment_id_for
from runtime_loader import (
    RuntimeLoadError,
    canonical_json,
    load_assignment_stage,
)
from weight_provisioning import provision_assignment, sha256_file
from weight_quantization import Int8RowwiseWeight


def _promoted(name: str, default: Any) -> Any:
    """Read a symbol this promotion adds to ``runtime_loader``.

    The base tree simply does not have these names, so they are read with an
    explicit fallback: RED then stays a *behavioural* failure (the dispatch
    target is missing) instead of a collection error, and every GREEN assertion
    below is unchanged.
    """

    return getattr(runtime_loader, name, default)


def resolve_int8_execution(*args: Any, **kwargs: Any) -> Any:
    """Thin proxy so a missing promotion is a call-time failure, not an import one."""

    return runtime_loader.resolve_int8_execution(*args, **kwargs)


A9_INT8_EXECUTION_PROOF_KEY = _promoted("A9_INT8_EXECUTION_PROOF_KEY", "a9_int8_execution")
A9_INT8_RUNTIME_EXECUTION_PROTOCOL = _promoted(
    "A9_INT8_RUNTIME_EXECUTION_PROTOCOL", "mycelium.a9_int8_runtime_execution.v1"
)
PLATFORM_NEUTRAL_INT8_EXECUTION = _promoted(
    "PLATFORM_NEUTRAL_INT8_EXECUTION", "platform-neutral-int8"
)
QUALIFIED_INT8_EXECUTION = _promoted(
    "QUALIFIED_INT8_EXECUTION", "weight-quantization"
)
DEFAULT_INT8_EXECUTION = _promoted("DEFAULT_INT8_EXECUTION", "weight-quantization")
DEFAULT_ACTIVATION_FRAME_DTYPE = _promoted(
    "DEFAULT_ACTIVATION_FRAME_DTYPE", "float16"
)

DEPLOYMENT_ID = "5b4c0e2a-1d3f-4a6b-8c9d-0e1f2a3b4c5d"
MANIFEST_DIGEST = "sha256:" + "a" * 64
COMMIT = "c" * 40
SEED = 20260926

HIDDEN = 8
HEAD_DIM = 4
HEADS = 2
KV_HEADS = 1
#: The loader derives the K/V width as ``n_kv_head * head_dim`` (grouped-query).
KV_HIDDEN = KV_HEADS * HEAD_DIM
INNER = 16
VOCAB = 12
LAYERS = 2

#: The 21 load-proof fields the qualified path has always emitted.  A promotion
#: that widened this set for the default selection would be a regression.
QUALIFIED_PROOF_FIELDS = frozenset(
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

_DECODER_SUFFIXES = (
    "input_layernorm.weight",
    "self_attn.q_proj.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.weight",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.weight",
    "self_attn.v_proj.bias",
    "self_attn.o_proj.weight",
    "post_attention_layernorm.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
)

#: 1 embedding matrix + 7 projection matrices per layer, all row-wise int8.
EXPECTED_A9_KERNELS = 1 + LAYERS * 7


def _write_safetensors(path: Path, tensors: dict[str, np.ndarray]) -> None:
    """Write a minimal contiguous float32 Safetensors artifact without MLX."""

    header: dict[str, dict[str, object]] = {}
    payload = bytearray()
    offset = 0
    for name in sorted(tensors):
        array = np.ascontiguousarray(tensors[name], dtype=np.float32)
        data = array.tobytes()
        header[name] = {
            "dtype": "F32",
            "shape": list(array.shape),
            "data_offsets": [offset, offset + len(data)],
        }
        payload.extend(data)
        offset += len(data)
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    encoded += b" " * ((8 - len(encoded) % 8) % 8)
    path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + bytes(payload))


def _weights() -> dict[str, np.ndarray]:
    rng = np.random.default_rng(SEED)

    def matrix(rows: int, columns: int) -> np.ndarray:
        return (rng.standard_normal((rows, columns)) * 0.05).astype(np.float32)

    def norm(size: int) -> np.ndarray:
        return np.ones((size,), dtype=np.float32)

    tensors: dict[str, np.ndarray] = {
        "model.embed_tokens.weight": matrix(VOCAB, HIDDEN),
        "model.norm.weight": norm(HIDDEN),
    }
    for layer in range(LAYERS):
        prefix = f"model.layers.{layer}."
        tensors[prefix + "input_layernorm.weight"] = norm(HIDDEN)
        tensors[prefix + "self_attn.q_proj.weight"] = matrix(HIDDEN, HIDDEN)
        tensors[prefix + "self_attn.q_proj.bias"] = np.zeros((HIDDEN,), np.float32)
        tensors[prefix + "self_attn.k_proj.weight"] = matrix(KV_HIDDEN, HIDDEN)
        tensors[prefix + "self_attn.k_proj.bias"] = np.zeros((KV_HIDDEN,), np.float32)
        tensors[prefix + "self_attn.v_proj.weight"] = matrix(KV_HIDDEN, HIDDEN)
        tensors[prefix + "self_attn.v_proj.bias"] = np.zeros((KV_HIDDEN,), np.float32)
        tensors[prefix + "self_attn.o_proj.weight"] = matrix(HIDDEN, HIDDEN)
        tensors[prefix + "post_attention_layernorm.weight"] = norm(HIDDEN)
        tensors[prefix + "mlp.gate_proj.weight"] = matrix(INNER, HIDDEN)
        tensors[prefix + "mlp.up_proj.weight"] = matrix(INNER, HIDDEN)
        tensors[prefix + "mlp.down_proj.weight"] = matrix(HIDDEN, INNER)
    return tensors


def _component_tensor_keys() -> dict[str, list[str]]:
    return {
        "input_embedding": ["model.embed_tokens.weight"],
        "decoder": sorted(
            f"model.layers.{layer}.{suffix}"
            for layer in range(LAYERS)
            for suffix in _DECODER_SUFFIXES
        ),
        "final_norm": ["model.norm.weight"],
    }


def _runtime(quantization: str = "int8-weight-only") -> dict[str, object]:
    return {
        "backend": "numpy",
        "dtype": "float32",
        "quantization": quantization,
        "architecture": "qwen2",
        "model_config": {
            "n_layer": LAYERS,
            "n_embd": HIDDEN,
            "n_head": HEADS,
            "n_kv_head": KV_HEADS,
            "n_inner": INNER,
            "vocab_size": VOCAB,
            "n_positions": 8,
            "rms_norm_epsilon": 1e-6,
            "rope_theta": 10000.0,
            "head_dim": HEAD_DIM,
            "activation_function": "silu",
            "tie_word_embeddings": False,
        },
    }


def _case(
    tmp_path: Path, *, quantization: str = "int8-weight-only"
) -> tuple[dict[str, object], Path]:
    """A real artifact, a real assignment, and the production verification report."""

    artifact = tmp_path / "model.safetensors"
    _write_safetensors(artifact, _weights())
    component_keys = _component_tensor_keys()
    expected_keys = sorted(
        {key for keys in component_keys.values() for key in keys}
    )
    size = artifact.stat().st_size
    digest = "sha256:" + sha256_file(artifact)
    assignment: dict[str, object] = {
        "protocol": "mycelium.layer_assignment.v2",
        "deployment_id": DEPLOYMENT_ID,
        "deployment_epoch": 3,
        "node_id": "node-a9",
        "manifest_digest": MANIFEST_DIGEST,
        "model_id": "test/tiny-qwen2",
        "resolved_commit": COMMIT,
        "range": {
            "start_layer": 0,
            "end_layer_exclusive": LAYERS,
            "layer_count": LAYERS,
        },
        "components": ["input_embedding", "decoder", "final_norm"],
        "component_tensor_keys": component_keys,
        "component_aliases": {},
        "expected_tensor_prefixes": [
            f"model.layers.{layer}." for layer in range(LAYERS)
        ],
        "expected_tensor_keys": expected_keys,
        "files": [
            {"path": artifact.name, "size_bytes": size, "content_digest": digest}
        ],
        "artifact_cache_root": str(tmp_path),
        "runtime": _runtime(quantization),
        "control_plane_binding": {
            "protocol": "mycelium.control_plane_binding.v1",
            "evidence_bundle_digest": "sha256:" + "e" * 64,
            "planner_snapshot_digest": "sha256:" + "f" * 64,
            "snapshot_generation": 2,
            "swarm_id": "swarm-a9",
            "deployment_id": DEPLOYMENT_ID,
            "deployment_epoch": 3,
        },
        "route_ready": False,
        "claim_boundary": "test assignment; runtime not loaded",
    }
    assignment["assignment_id"] = assignment_id_for(assignment)
    return assignment, artifact


def _verified_report(assignment: dict[str, object], artifact: Path) -> dict[str, object]:
    """The production report builder over the local artifact (no network)."""

    def fetch(
        model_id: str,
        resolved_commit: str,
        filename: str,
        cache_root: Path,
        *,
        local_files_only: bool = False,
    ) -> tuple[Path, bool]:
        assert filename == artifact.name
        return artifact, True

    return provision_assignment(assignment, fetch_file=fetch, local_files_only=True)


def _a9_report(stage: Any) -> Any:
    report = stage.proof[A9_INT8_EXECUTION_PROOF_KEY]
    # The loader deep-freezes the proof: nested objects arrive as read-only
    # mappings and nested lists as tuples.
    assert isinstance(report, Mapping)
    return report


def test_platform_neutral_selection_dispatches_a9_kernels(tmp_path: Path) -> None:
    assignment, artifact = _case(tmp_path)
    report = _verified_report(assignment, artifact)

    stage = load_assignment_stage(
        assignment,
        report,
        load_generation=5,
        int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
    )

    a9 = _a9_report(stage)
    # The report round-trips through the loader's canonical serializer, so no
    # NumPy scalar leaked into the load proof.
    canonical = json.loads(canonical_json(stage.proof))
    assert canonical[A9_INT8_EXECUTION_PROOF_KEY]["protocol"] == (
        A9_INT8_RUNTIME_EXECUTION_PROTOCOL
    )

    quantized = {
        key: tensor
        for key, tensor in stage.tensors.items()
        if isinstance(tensor, Int8RowwiseWeight)
    }
    assert len(quantized) == EXPECTED_A9_KERNELS
    assert a9["kernels_executed"] == EXPECTED_A9_KERNELS
    assert a9["selection"] == PLATFORM_NEUTRAL_INT8_EXECUTION
    assert [row["tensor_key"] for row in a9["kernels"]] == sorted(quantized)
    # One declared warmup pass plus one bounded decode per kernel.
    assert a9["decode_calls"] == 2 * EXPECTED_A9_KERNELS

    for row in a9["kernels"]:
        ledger = row["ledger"]
        assert ledger["warmup_complete"] is True
        assert ledger["decode_calls"] == 2
        # A warm decode never converts the whole packed weight matrix.
        assert ledger["full_weight_materializations"] == 0
        assert ledger["bounded_slab_conversions"] >= 1
        assert row["warm_decode_proven"] is True
        assert row["integer_reference_parity"] is True
        assert row["packed_state_is_stable"] is True
        assert row["slab_rows"] < row["out_features"]
        assert (
            ledger["peak_weight_workspace_bytes"]
            <= row["workspace_bytes"]
            < row["out_features"] * row["in_features"] * 4
        )
        assert row["output_digest"].startswith("sha256:")
        # Persistent packed state stays int8 sized: no float32 expansion.
        assert row["packed_state_bytes"] <= (
            row["out_features"] * row["in_features"] + row["out_features"] * 4
        )


@pytest.mark.parametrize("frame_dtype", ("float16", "bfloat16"))
def test_activation_transport_measures_reduced_precision_frames(
    tmp_path: Path, frame_dtype: str
) -> None:
    assignment, artifact = _case(tmp_path)
    report = _verified_report(assignment, artifact)

    stage = load_assignment_stage(
        assignment,
        report,
        load_generation=6,
        int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
        activation_frame_dtype=frame_dtype,
    )

    transport = _a9_report(stage)["activation_transport"]
    assert transport["dtype"] == frame_dtype
    assert transport["frames_encoded"] == 1
    assert transport["frames_decoded"] == 1
    assert transport["frames_rejected"] == 0
    # Measured, not asserted: float32 in, 16-bit payload out.
    assert transport["bytes_before"] == 2 * transport["bytes_after"]
    assert transport["byte_reduction_ratio"] == pytest.approx(2.0)
    assert transport["reduced_precision_parity"] is True
    assert transport["counter_digest"].startswith("sha256:")
    assert transport["frame_digest"].startswith("sha256:")
    assert transport["projection"]["protocol"] == (
        "mycelium.activation_transport_projection.v1"
    )
    assert transport["projection"]["dtype"] == frame_dtype
    assert transport["projection_is_privacy_reduced"] is True
    assert transport["request_scope_cleaned"] is True
    assert transport["outstanding_frames_after_cleanup"] == 0
    assert transport["pending_scratch_bytes_after_cleanup"] == 0


def test_default_selection_keeps_the_qualified_path_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, artifact = _case(tmp_path)
    report = _verified_report(assignment, artifact)

    def _forbidden(**kwargs: object) -> dict[str, object]:
        raise AssertionError(
            "the qualified default selection dispatched the A9 kernel: "
            f"{sorted(kwargs)}"
        )

    monkeypatch.setattr(
        runtime_loader, "_a9_platform_neutral_execution_report", _forbidden
    )

    stage = load_assignment_stage(assignment, report, load_generation=5)

    assert A9_INT8_EXECUTION_PROOF_KEY not in stage.proof
    assert set(stage.proof) == QUALIFIED_PROOF_FIELDS
    quantized = {
        key: tensor
        for key, tensor in stage.tensors.items()
        if isinstance(tensor, Int8RowwiseWeight)
    }
    assert len(quantized) == EXPECTED_A9_KERNELS
    for key, tensor in quantized.items():
        assert str(tensor.dtype) == "int8", key
        assert tensor.values.shape[0] == tensor.scales.shape[0], key
        assert bool(np.isfinite(tensor.scales).all()), key
    # The qualified stage still probes through the NumPy runtime unchanged.
    assert stage.proof["runtime"]["quantization"] == "int8-weight-only"
    assert tuple(stage.proof["probe_shape"]) == (1, 3, HIDDEN)

    # An explicit default selection is the same qualified load.
    explicit = load_assignment_stage(
        assignment,
        report,
        load_generation=5,
        int8_execution=QUALIFIED_INT8_EXECUTION,
        activation_frame_dtype=DEFAULT_ACTIVATION_FRAME_DTYPE,
    )
    assert set(explicit.proof) == QUALIFIED_PROOF_FIELDS
    assert explicit.proof["probe_digest"] == stage.proof["probe_digest"]
    assert explicit.proof["loaded_tensor_digest"] == (
        stage.proof["loaded_tensor_digest"]
    )


def test_a9_report_carries_the_reachable_not_qualified_boundary(
    tmp_path: Path,
) -> None:
    assignment, artifact = _case(tmp_path)
    report = _verified_report(assignment, artifact)

    stage = load_assignment_stage(
        assignment,
        report,
        load_generation=7,
        int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
    )

    a9 = _a9_report(stage)
    assert a9["reachable"] is True
    assert a9["qualified"] is False
    assert a9["physical_round_executed"] is False
    assert a9["boundary"] == "reachable_not_qualified"
    assert "no physical round" in a9["boundary_note"]
    assert a9["activation_source"] == "loader_deterministic_probe_ramp"

    host = a9["kernel_host"]
    assert host["backend_id"] == "numpy"
    assert host["correctness_only"] is True
    assert host["accelerated"] is False
    assert host["qualification_claim"] is False
    assert host["windows_x86_64_qualified"] is False
    assert host["blocker"] == "windows_x86_64_provider_not_physically_qualified"
    assert host["workspace_budget_bytes"]

    coverage = a9["acceptance_coverage"]
    assert set(coverage) == {"int8_execution", "activation_transport"}
    for entries in coverage.values():
        assert entries, "the unearned physical acceptance cases must be reported"
        assert all(value.startswith("not_earned") for value in entries.values())


def test_platform_neutral_selection_requires_int8_weight_only(
    tmp_path: Path,
) -> None:
    assignment, artifact = _case(tmp_path, quantization="none")
    report = _verified_report(assignment, artifact)

    with pytest.raises(
        RuntimeLoadError, match="platform_neutral_int8_requires_int8_weight_only"
    ):
        load_assignment_stage(
            assignment,
            report,
            load_generation=5,
            int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
        )


def test_unknown_selection_and_frame_dtype_are_refused(tmp_path: Path) -> None:
    assignment, artifact = _case(tmp_path)
    report = _verified_report(assignment, artifact)

    with pytest.raises(RuntimeLoadError, match="unknown_int8_execution"):
        load_assignment_stage(
            assignment,
            report,
            load_generation=5,
            int8_execution="int4-weight-only",
        )
    with pytest.raises(RuntimeLoadError, match="activation_frame_dtype requires"):
        load_assignment_stage(
            assignment, report, load_generation=5, activation_frame_dtype="bfloat16"
        )
    with pytest.raises(RuntimeLoadError, match="unknown_activation_frame_dtype"):
        load_assignment_stage(
            assignment,
            report,
            load_generation=5,
            int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
            activation_frame_dtype="float32",
        )


def test_resolve_int8_execution_is_the_validation_surface() -> None:
    assert resolve_int8_execution() == (
        QUALIFIED_INT8_EXECUTION,
        DEFAULT_ACTIVATION_FRAME_DTYPE,
    )
    assert DEFAULT_INT8_EXECUTION == QUALIFIED_INT8_EXECUTION
    assert resolve_int8_execution(
        PLATFORM_NEUTRAL_INT8_EXECUTION, activation_frame_dtype="bfloat16"
    ) == (PLATFORM_NEUTRAL_INT8_EXECUTION, "bfloat16")
    with pytest.raises(RuntimeLoadError, match="unknown_int8_execution"):
        resolve_int8_execution(["platform-neutral-int8"])
    with pytest.raises(RuntimeLoadError, match="unknown_activation_frame_dtype"):
        resolve_int8_execution(PLATFORM_NEUTRAL_INT8_EXECUTION, activation_frame_dtype=16)
