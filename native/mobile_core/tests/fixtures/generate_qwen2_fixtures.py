# SPDX-License-Identifier: AGPL-3.0-only
"""Regenerate the Qwen2 stage parity fixtures for mycelium-mobile-core.

Run from the repository root:
    python3 -B native/mobile_core/tests/fixtures/generate_qwen2_fixtures.py synthetic
    MYCELIUM_QWEN05_SNAPSHOT=/path/to/models--Qwen--Qwen2.5-0.5B-Instruct/snapshots/<commit> \
        python3 -B native/mobile_core/tests/fixtures/generate_qwen2_fixtures.py real

Every reference value comes from the repository's own NumPy serving path, the
one a live-route ``numpy`` node runs: ``runtime_loader._load_numpy_safetensors``
(exact bf16 -> f32, streaming row-wise int8 via ``quantize_qwen2_numpy_tensor``),
``numpy_runtime._qwen2_embedding`` and ``numpy_runtime._qwen2_block_with_kv`` at
position 0 with no past (complete-context replay) and a no-op checkpoint, so the
chunked projections match the router's call exactly.

``synthetic`` writes ``qwen2_synthetic.json``: a tiny deterministic Qwen2
(hidden 64, 4/2 heads, inner 128, two layers) as an embedded Safetensors file,
crafted quantizer edge rows, and the NumPy stage output. It needs no model.

``real`` writes ``qwen05_layer<L>.json``: the true input to layer L of
Qwen2.5-0.5B-Instruct for a fixed chat prompt (embedding + NumPy layers 0..L-1),
the NumPy output of layer L, and SHA-256 digests of the NumPy-quantized int8
values and scales of the layer's seven matrices. Weights are never copied into
the fixture; the Rust test reads them from the same snapshot.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

import numpy_runtime  # noqa: E402
import runtime_loader  # noqa: E402

OUT = Path(__file__).resolve().parent
MATRICES = (
    "self_attn.q_proj.weight",
    "self_attn.k_proj.weight",
    "self_attn.v_proj.weight",
    "self_attn.o_proj.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
)
VECTORS = (
    "input_layernorm.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.bias",
    "post_attention_layernorm.weight",
)
REAL_MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
REAL_LAYER = 12
# Fixed chat prompt (Qwen2.5 chat template, no system turn); 16 tokens or fewer.
REAL_PROMPT = "<|im_start|>user\nName three primary colours.<|im_end|>\n<|im_start|>assistant\n"


def b64_f32(array: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(array, dtype="<f4").tobytes()).decode()


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def f32_to_bf16_bits(values: np.ndarray) -> np.ndarray:
    """Round-to-nearest-even f32 -> bf16 (finite inputs only)."""
    bits = np.ascontiguousarray(values, dtype=np.float32).view(np.uint32).astype(np.uint64)
    rounded = (bits + 0x7FFF + ((bits >> 16) & 1)) >> 16
    return rounded.astype(np.uint16)


def write_safetensors(tensors: dict[str, tuple[str, list[int], bytes]]) -> bytes:
    header: dict[str, object] = {}
    offset = 0
    payload = bytearray()
    for name in sorted(tensors):
        dtype, shape, data = tensors[name]
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [offset, offset + len(data)]}
        payload += data
        offset += len(data)
    encoded = json.dumps(header, sort_keys=True, separators=(",", ":")).encode()
    encoded += b" " * (-len(encoded) % 8)
    return struct.pack("<Q", len(encoded)) + encoded + bytes(payload)


def load_numpy(path: Path, keys: set[str]) -> dict[str, object]:
    with path.open("rb") as handle:
        header, data_offset = runtime_loader._validate_safetensors_header(handle, str(path))
        return runtime_loader._load_numpy_safetensors(
            handle, header, data_offset, keys, np.dtype(np.float32), quantize_qwen=True
        )


def block_config(config: dict[str, object]) -> dict[str, object]:
    return {
        "n_head": int(config["num_attention_heads"]),
        "n_kv_head": int(config["num_key_value_heads"]),
        "head_dim": int(config["hidden_size"]) // int(config["num_attention_heads"]),
        "rms_norm_epsilon": float(config["rms_norm_eps"]),
        "rope_theta": float(config["rope_theta"]),
    }


def numpy_layer(hidden: np.ndarray, tensors: dict[str, object], layer: int, config: dict[str, object]) -> np.ndarray:
    out, _kv = numpy_runtime._qwen2_block_with_kv(
        hidden,
        tensors,
        f"model.layers.{layer}.",
        block_config(config),
        0,
        None,
        "qwen2",
        checkpoint=lambda: None,
    )
    return np.ascontiguousarray(out, dtype=np.float32)


def quantizer_digests(tensors: dict[str, object], layer: int) -> dict[str, dict[str, str]]:
    result = {}
    for suffix in MATRICES:
        weight = tensors[f"model.layers.{layer}.{suffix}"]
        result[suffix] = {
            "shape": list(weight.values.shape),
            "values_sha256": sha256(np.ascontiguousarray(weight.values).tobytes()),
            "scales_sha256": sha256(np.ascontiguousarray(weight.scales, dtype="<f4").tobytes()),
        }
    return result


def quantizer_edge_rows() -> dict[str, object]:
    """Rows exercising zero scale, exact .5 ties of either sign, extremes and subnormals."""
    rows = np.array(
        [
            [0.0] * 8,
            [127.0, 0.5, -0.5, 1.5, -1.5, 2.5, -2.5, 63.5],
            [-3.0, 1.0, 2.0, -1.0, 0.25, -0.75, 1e-30, -1e-30],
            [1e-40, -1e-40, 3e-39, 0.0, -0.0, 1e-45, 2e-45, -5e-41],
            [3.0e38, -1.0e38, 1.0, -1.0, 2.0e37, 7.0e30, -6.5e36, 1.0e-3],
            [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8],
        ],
        dtype=np.float32,
    )
    rng = np.random.default_rng(7)
    rows = np.concatenate([rows, rng.standard_normal((10, 8)).astype(np.float32) * 0.03])
    quantized = numpy_runtime.quantize_qwen2_numpy_tensor("edge.weight", rows)
    return {
        "rows_f32_b64": b64_f32(rows),
        "shape": list(rows.shape),
        "values": quantized.values.astype(int).tolist(),
        "scales_bits": [int(v) for v in quantized.scales.view(np.uint32)],
    }


def sum_cases() -> list[dict[str, object]]:
    """float32 ``np.sum`` over a contiguous axis (pairwise) for the Rust port."""
    rng = np.random.default_rng(11)
    cases = []
    for n in (3, 8, 13, 127, 128, 129, 300, 896, 4864):
        values = (rng.standard_normal(n) * np.exp(rng.uniform(-8, 8, n))).astype(np.float32)
        total = np.sum(values.reshape(1, 1, n), axis=-1)[0, 0]
        cases.append({"values_b64": b64_f32(values), "sum_bits": int(np.float32(total).view(np.uint32))})
    return cases


def synthetic() -> None:
    config = {
        "hidden_size": 64,
        "intermediate_size": 128,
        "num_attention_heads": 4,
        "num_key_value_heads": 2,
        "num_hidden_layers": 2,
        "rms_norm_eps": 1e-6,
        "rope_theta": 1000000.0,
    }
    hidden, inner, kv = 64, 128, 2 * 16
    rng = np.random.default_rng(20261007)
    tensors: dict[str, tuple[str, list[int], bytes]] = {}
    shapes = {
        "input_layernorm.weight": [hidden],
        "post_attention_layernorm.weight": [hidden],
        "self_attn.q_proj.weight": [hidden, hidden],
        "self_attn.q_proj.bias": [hidden],
        "self_attn.k_proj.weight": [kv, hidden],
        "self_attn.k_proj.bias": [kv],
        "self_attn.v_proj.weight": [kv, hidden],
        "self_attn.v_proj.bias": [kv],
        "self_attn.o_proj.weight": [hidden, hidden],
        "mlp.gate_proj.weight": [inner, hidden],
        "mlp.up_proj.weight": [inner, hidden],
        "mlp.down_proj.weight": [hidden, inner],
    }
    for layer in range(2):
        for suffix, shape in shapes.items():
            if suffix.endswith("layernorm.weight"):
                values = 1.0 + 0.1 * rng.standard_normal(shape)
            elif suffix.endswith(".bias"):
                values = 0.5 * rng.standard_normal(shape)
            else:
                values = rng.standard_normal(shape) / np.sqrt(shape[1])
            if suffix == "mlp.up_proj.weight" and layer == 1:
                values[3, :] = 0.0  # all-zero row: the scale falls back to 1.0
            bits = f32_to_bf16_bits(values.astype(np.float32))
            tensors[f"model.layers.{layer}.{suffix}"] = ("BF16", shape, bits.astype("<u2").tobytes())
    blob = write_safetensors(tensors)
    path = OUT / "qwen2_synthetic.tmp.safetensors"
    path.write_bytes(blob)
    try:
        loaded = load_numpy(path, set(tensors))
    finally:
        path.unlink()
    seq = 7
    hidden_in = (rng.standard_normal((1, seq, hidden)) * 2.0).astype(np.float32)
    hidden_in[0, 2, 5] = 40.0  # one large activation, as real Qwen residuals have
    out = hidden_in
    per_layer = []
    for layer in range(2):
        out = numpy_layer(out, loaded, layer, config)
        per_layer.append(b64_f32(out))
    document = {
        "protocol": "mycelium.mobile_qwen2_fixture.v1",
        "kind": "synthetic",
        "config": config,
        "start_layer": 0,
        "end_layer_exclusive": 2,
        "safetensors_b64": base64.b64encode(blob).decode(),
        "safetensors_sha256": sha256(blob),
        "seq": seq,
        "input_b64": b64_f32(hidden_in),
        "numpy_layer_outputs_b64": per_layer,
        "quantizer": {f"layer{layer}": quantizer_digests(loaded, layer) for layer in range(2)},
        "quantizer_edge": quantizer_edge_rows(),
        "sum_cases": sum_cases(),
        "numpy_version": np.__version__,
    }
    (OUT / "qwen2_synthetic.json").write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")


def real() -> None:
    snapshot = Path(os.environ["MYCELIUM_QWEN05_SNAPSHOT"])
    config = json.loads((snapshot / "config.json").read_text())
    weights = snapshot / "model.safetensors"
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(snapshot / "tokenizer.json"))
    ids = tokenizer.encode(REAL_PROMPT, add_special_tokens=False).ids
    if not 1 <= len(ids) <= 16:
        raise SystemExit(f"prompt is {len(ids)} tokens; the fixture bound is 16")
    keys = {"model.embed_tokens.weight"}
    for layer in range(REAL_LAYER + 1):
        keys |= {f"model.layers.{layer}.{s}" for s in MATRICES + VECTORS}
    loaded = load_numpy(weights, keys)
    hidden = numpy_runtime._qwen2_embedding(
        loaded["model.embed_tokens.weight"], np.asarray([ids], dtype=np.int64)
    ).astype(np.float32)
    for layer in range(REAL_LAYER):
        hidden = numpy_layer(hidden, loaded, layer, config)
    out = numpy_layer(hidden, loaded, REAL_LAYER, config)
    document = {
        "protocol": "mycelium.mobile_qwen2_fixture.v1",
        "kind": "real",
        "model_id": REAL_MODEL_ID,
        "resolved_commit": snapshot.name,
        "safetensors_sha256": sha256_file(weights),
        "config": {k: config[k] for k in (
            "hidden_size", "intermediate_size", "num_attention_heads", "num_key_value_heads",
            "num_hidden_layers", "rms_norm_eps", "rope_theta")},
        "layer": REAL_LAYER,
        "prompt": REAL_PROMPT,
        "token_ids": ids,
        "seq": len(ids),
        "input_b64": b64_f32(hidden),
        "input_max_abs": float(np.max(np.abs(hidden))),
        "numpy_output_b64": b64_f32(out),
        "numpy_output_max_abs": float(np.max(np.abs(out))),
        "quantizer": quantizer_digests(loaded, REAL_LAYER),
        "numpy_version": np.__version__,
    }
    (OUT / f"qwen05_layer{REAL_LAYER}.json").write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


if __name__ == "__main__":
    targets = sys.argv[1:] or ["synthetic"]
    for target in targets:
        {"synthetic": synthetic, "real": real}[target]()
