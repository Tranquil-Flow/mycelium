from __future__ import annotations

import json
from pathlib import Path

import mlx.core as mx
import pytest

from conftest import (
    EXPECTED_CONFIG_DIGEST,
    EXPECTED_MODEL_DIGEST,
    EXPECTED_TENSOR_SET_DIGEST,
    MODEL_ID,
    RESOLVED_COMMIT,
)
from mycelium_reference_oracle.gpt2 import (
    OracleValidationError,
    expected_tensor_shapes,
    load_gpt2_fixture,
)


def load(root: Path, **kwargs):
    return load_gpt2_fixture(
        root,
        model_id=MODEL_ID,
        resolved_commit=RESOLVED_COMMIT,
        **kwargs,
    )


def rewrite_tensor(root: Path, tensor_name: str, replacement: mx.array) -> None:
    index_path = root / "model.safetensors.index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    shard = root / index["weight_map"][tensor_name]
    tensors = dict(mx.load(str(shard)))
    mx.eval(*tensors.values())
    tensors[tensor_name] = replacement
    temporary = shard.with_name(shard.name + ".replacement.safetensors")
    mx.save_safetensors(str(temporary), tensors)
    temporary.replace(shard)
    shard_names = sorted(set(index["weight_map"].values()))
    index["metadata"]["total_size"] = sum(
        (root / name).stat().st_size for name in shard_names
    )
    index_path.write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def test_parses_current_config_and_tensor_artifacts_deterministically(
    fixture_dir: Path,
) -> None:
    first = load(fixture_dir)
    second = load(fixture_dir)

    assert dict(first.config) == {
        "activation_function": "gelu_new",
        "add_cross_attention": False,
        "architectures": ["GPT2LMHeadModel"],
        "layer_norm_epsilon": 1e-5,
        "model_type": "gpt2",
        "n_embd": 4,
        "n_head": 2,
        "n_inner": 8,
        "n_layer": 2,
        "n_positions": 8,
        "reorder_and_upcast_attn": False,
        "scale_attn_by_inverse_layer_idx": False,
        "scale_attn_weights": True,
        "tie_word_embeddings": False,
        "vocab_size": 7,
    }
    assert first.tensor_names == tuple(sorted(expected_tensor_shapes()))
    assert first.identity == second.identity
    assert first.identity.config_digest == EXPECTED_CONFIG_DIGEST
    assert first.identity.tensor_set_digest == EXPECTED_TENSOR_SET_DIGEST
    assert first.identity.model_digest == EXPECTED_MODEL_DIGEST
    assert first.identity.checkpoint_index_digest.startswith("sha256:")
    assert len(first.identity.tensor_artifact_digests) == 2
    assert first.identity.tensor_set_digest.startswith("sha256:")
    assert first.identity.tensor_value_digest.startswith("sha256:")
    assert first.identity.model_digest.startswith("sha256:")


def test_expected_config_tensor_and_model_digests_are_enforced(fixture_dir: Path) -> None:
    baseline = load(fixture_dir)
    loaded = load(
        fixture_dir,
        expected_config_digest=baseline.identity.config_digest,
        expected_tensor_set_digest=baseline.identity.tensor_set_digest,
        expected_model_digest=baseline.identity.model_digest,
    )

    assert loaded.identity == baseline.identity

    for field in (
        "expected_config_digest",
        "expected_tensor_set_digest",
        "expected_model_digest",
    ):
        with pytest.raises(OracleValidationError, match="identity mismatch"):
            load(fixture_dir, **{field: "sha256:" + "0" * 64})


def test_rejects_duplicate_config_keys(fixture_dir: Path) -> None:
    config_path = fixture_dir / "config.json"
    document = json.loads(config_path.read_text(encoding="utf-8"))
    encoded = json.dumps(document, separators=(",", ":"))
    config_path.write_text(
        encoded[:-1] + ',"n_layer":2}',
        encoding="utf-8",
    )

    with pytest.raises(OracleValidationError, match="duplicate JSON key"):
        load(fixture_dir)


def test_rejects_index_tensor_mapping_drift(fixture_dir: Path) -> None:
    index_path = fixture_dir / "model.safetensors.index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    del index["weight_map"]["transformer.h.0.attn.c_attn.weight"]
    index_path.write_text(json.dumps(index), encoding="utf-8")

    with pytest.raises(OracleValidationError, match="tensor names"):
        load(fixture_dir)


@pytest.mark.parametrize(
    "replacement,expected_error",
    (
        (mx.zeros((6, 4), dtype=mx.float32), "tensor shape mismatch"),
        (mx.zeros((7, 4), dtype=mx.float16), "tensor dtype mismatch"),
        (mx.full((7, 4), float("nan"), dtype=mx.float32), "non-finite"),
    ),
)
def test_rejects_wrong_shape_dtype_and_nonfinite_tensor_values(
    fixture_dir: Path,
    replacement: mx.array,
    expected_error: str,
) -> None:
    rewrite_tensor(fixture_dir, "transformer.wte.weight", replacement)

    with pytest.raises(OracleValidationError, match=expected_error):
        load(fixture_dir)


def test_rejects_malformed_caller_provenance(fixture_dir: Path) -> None:
    for invalid_model_id in ("", "   ", " leading", "trailing "):
        with pytest.raises(OracleValidationError, match="model ID"):
            load_gpt2_fixture(
                fixture_dir,
                model_id=invalid_model_id,
                resolved_commit=RESOLVED_COMMIT,
            )
    for invalid_commit in ("x" * 40, "A" * 40, "0" * 39, "0" * 41):
        with pytest.raises(OracleValidationError, match="resolved commit"):
            load_gpt2_fixture(
                fixture_dir,
                model_id=MODEL_ID,
                resolved_commit=invalid_commit,
            )
