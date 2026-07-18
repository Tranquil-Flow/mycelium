from __future__ import annotations

import json
import math
import shutil
from pathlib import Path
from typing import Any

import mlx.core as mx
import pytest

MODEL_ID = "local/tiny-gpt2-qualification"
RESOLVED_COMMIT = "0123456789abcdef0123456789abcdef01234567"
FIRST_TOKEN_PROMPT = (1, 2, 3)
EIGHT_STEP_PROMPT = (1,)
EXPECTED_EIGHT_TOKENS = (6, 6, 6, 2, 0, 0, 0, 0)
EXPECTED_CONFIG_DIGEST = (
    "sha256:05bb0ae803af9010a31517c3e20b3b1d958a5edcc4a866d1189a2cc00bb02310"
)
EXPECTED_TENSOR_SET_DIGEST = (
    "sha256:6dd2fc5f64df4eaeeb515ecdaaab859c94d3ca4a91490d5643d138adcb442bb0"
)
EXPECTED_MODEL_DIGEST = (
    "sha256:694a1f6113bc2c6fbb312f42a114b7c56ff7708ef70d68ce437031ba21756729"
)
EXPECTED_PROMPT_DIGEST = (
    "sha256:9ffdd78c7d7eb75b7c2e088fc79e845d4dae354824e2bd0be3e2e0be4eaf7325"
)
SHARD_NAMES = (
    "model-00001-of-00002.safetensors",
    "model-00002-of-00002.safetensors",
)


def canonical_json(document: Any) -> str:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def values(shape: tuple[int, ...], *, offset: int, scale: float) -> mx.array:
    return (
        mx.arange(math.prod(shape), dtype=mx.float32).reshape(shape) + offset
    ) * scale


def layer_tensors(layer: int) -> dict[str, mx.array]:
    prefix = f"transformer.h.{layer}."
    offset = 100 * (layer + 1)
    return {
        prefix + "ln_1.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_1.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "attn.c_attn.weight": values(
            (4, 12), offset=offset + 1, scale=0.0002
        ),
        prefix + "attn.c_attn.bias": values(
            (12,), offset=offset + 2, scale=0.0001
        ),
        prefix + "attn.c_proj.weight": values(
            (4, 4), offset=offset + 3, scale=0.0002
        ),
        prefix + "attn.c_proj.bias": values(
            (4,), offset=offset + 4, scale=0.0001
        ),
        prefix + "ln_2.weight": mx.ones((4,), dtype=mx.float32),
        prefix + "ln_2.bias": mx.zeros((4,), dtype=mx.float32),
        prefix + "mlp.c_fc.weight": values(
            (4, 8), offset=offset + 5, scale=0.0002
        ),
        prefix + "mlp.c_fc.bias": values(
            (8,), offset=offset + 6, scale=0.0001
        ),
        prefix + "mlp.c_proj.weight": values(
            (8, 4), offset=offset + 7, scale=0.0002
        ),
        prefix + "mlp.c_proj.bias": values(
            (4,), offset=offset + 8, scale=0.0001
        ),
    }


def make_current_fixture(root: Path) -> Path:
    root.mkdir(parents=True)
    shards = (
        {
            "transformer.wte.weight": values((7, 4), offset=1, scale=0.01),
            "transformer.wpe.weight": values((8, 4), offset=2, scale=0.005),
            **layer_tensors(0),
        },
        {
            **layer_tensors(1),
            "transformer.ln_f.weight": mx.ones((4,), dtype=mx.float32),
            "transformer.ln_f.bias": mx.zeros((4,), dtype=mx.float32),
            "lm_head.weight": values((7, 4), offset=3, scale=0.007),
        },
    )
    for shard_name, tensors in zip(SHARD_NAMES, shards):
        mx.save_safetensors(str(root / shard_name), tensors)

    config = {
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
    weight_map = {
        tensor_name: shard_name
        for shard_name, tensors in zip(SHARD_NAMES, shards)
        for tensor_name in sorted(tensors)
    }
    index = {
        "metadata": {
            "total_size": sum((root / name).stat().st_size for name in SHARD_NAMES)
        },
        "weight_map": weight_map,
    }
    (root / "config.json").write_text(canonical_json(config) + "\n", encoding="utf-8")
    (root / "model.safetensors.index.json").write_text(
        canonical_json(index) + "\n", encoding="utf-8"
    )
    return root


@pytest.fixture
def fixture_dir(tmp_path: Path) -> Path:
    return make_current_fixture(tmp_path / "tiny-gpt2")


@pytest.fixture
def copy_fixture(fixture_dir: Path, tmp_path: Path):
    counter = 0

    def copy() -> Path:
        nonlocal counter
        counter += 1
        destination = tmp_path / f"fixture-copy-{counter}"
        shutil.copytree(fixture_dir, destination)
        return destination

    return copy
