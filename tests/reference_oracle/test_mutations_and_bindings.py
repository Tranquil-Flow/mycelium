from __future__ import annotations

import json
from pathlib import Path

import mlx.core as mx
import pytest

from conftest import (
    EIGHT_STEP_PROMPT,
    FIRST_TOKEN_PROMPT,
    MODEL_ID,
    RESOLVED_COMMIT,
)
from mycelium_reference_oracle.gpt2 import (
    OracleValidationError,
    load_gpt2_fixture,
    prompt_digest,
)


def load(root: Path, **kwargs):
    return load_gpt2_fixture(
        root,
        model_id=kwargs.pop("model_id", MODEL_ID),
        resolved_commit=RESOLVED_COMMIT,
        **kwargs,
    )


def mutate_tensor(
    root: Path,
    tensor_name: str,
    *,
    row: int,
    column: int,
    amount: float = 1.0,
) -> None:
    index = json.loads(
        (root / "model.safetensors.index.json").read_text(encoding="utf-8")
    )
    shard = root / index["weight_map"][tensor_name]
    tensors = dict(mx.load(str(shard)))
    mx.eval(*tensors.values())
    values = tensors[tensor_name].tolist()
    values[row][column] += amount
    tensors[tensor_name] = mx.array(values, dtype=mx.float32)
    replacement = shard.with_name(shard.name + ".replacement.safetensors")
    mx.save_safetensors(str(replacement), tensors)
    replacement.replace(shard)
    shard_names = sorted(set(index["weight_map"].values()))
    index["metadata"]["total_size"] = sum(
        (root / name).stat().st_size for name in shard_names
    )
    (root / "model.safetensors.index.json").write_text(
        json.dumps(index, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "tensor_name,row,column",
    (
        ("transformer.wte.weight", 1, 0),
        ("transformer.h.0.attn.c_attn.weight", 0, 8),
        ("transformer.h.0.mlp.c_fc.weight", 0, 0),
    ),
)
def test_embedding_attention_and_mlp_mutations_change_oracle_outputs(
    fixture_dir: Path,
    copy_fixture,
    tensor_name: str,
    row: int,
    column: int,
) -> None:
    baseline = load(fixture_dir)
    baseline_result = baseline.forward(FIRST_TOKEN_PROMPT)
    mutated_root = copy_fixture()
    mutate_tensor(mutated_root, tensor_name, row=row, column=column)
    mutated = load(mutated_root)
    mutated_result = mutated.forward(FIRST_TOKEN_PROMPT)

    assert mutated.identity.tensor_set_digest != baseline.identity.tensor_set_digest
    assert mutated.identity.tensor_value_digest != baseline.identity.tensor_value_digest
    assert mutated_result.activation_digests != baseline_result.activation_digests
    assert mutated_result.logits_digest != baseline_result.logits_digest


def test_tensor_mutation_is_rejected_when_bound_to_original_identity(
    fixture_dir: Path,
    copy_fixture,
) -> None:
    baseline = load(fixture_dir)
    mutated_root = copy_fixture()
    mutate_tensor(
        mutated_root,
        "transformer.h.0.attn.c_attn.weight",
        row=0,
        column=8,
    )

    with pytest.raises(OracleValidationError, match="tensor identity mismatch"):
        load(
            mutated_root,
            expected_tensor_set_digest=baseline.identity.tensor_set_digest,
        )


def test_rejects_prompt_identity_mismatch(fixture_dir: Path) -> None:
    current = load(fixture_dir)
    assert prompt_digest(EIGHT_STEP_PROMPT).startswith("sha256:")

    with pytest.raises(OracleValidationError, match="prompt identity mismatch"):
        current.greedy_decode(
            EIGHT_STEP_PROMPT,
            steps=8,
            expected_prompt_digest="sha256:" + "0" * 64,
        )


def test_rejects_config_identity_mismatch(fixture_dir: Path, copy_fixture) -> None:
    baseline = load(fixture_dir)
    changed_root = copy_fixture()
    config_path = changed_root / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["layer_norm_epsilon"] = 1e-4
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(OracleValidationError, match="config identity mismatch"):
        load(
            changed_root,
            expected_config_digest=baseline.identity.config_digest,
        )


def test_rejects_model_identity_mismatch(fixture_dir: Path) -> None:
    baseline = load(fixture_dir)

    with pytest.raises(OracleValidationError, match="model identity mismatch"):
        load(
            fixture_dir,
            model_id="different/model",
            expected_model_digest=baseline.identity.model_digest,
        )
