# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic dense Qwen checkpoints for the N-host/slice tests (no real weights).

The model contract is parameterised by ``model_type`` so the same fixtures run
for Qwen2 today and Qwen3 once its contract lands. Tensor bytes are distinct
per tensor (sha256 stream) so any mis-mapped byte range changes a digest.
"""
from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _payload(name: str, size: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < size:
        out.extend(hashlib.sha256(f"{name}:{counter}".encode()).digest())
        counter += 1
    return bytes(out[:size])


def _layer_tensors(index: int, hidden: int, inter: int) -> list[tuple[str, list[int]]]:
    p = f"model.layers.{index}."
    return [
        (p + "input_layernorm.weight", [hidden]),
        (p + "mlp.down_proj.weight", [hidden, inter]),
        (p + "mlp.gate_proj.weight", [inter, hidden]),
        (p + "mlp.up_proj.weight", [inter, hidden]),
        (p + "post_attention_layernorm.weight", [hidden]),
        (p + "self_attn.k_proj.weight", [hidden, hidden]),
        (p + "self_attn.o_proj.weight", [hidden, hidden]),
        (p + "self_attn.q_proj.weight", [hidden, hidden]),
        (p + "self_attn.v_proj.weight", [hidden, hidden]),
    ]


def write_safetensors(path: Path, tensors: list[tuple[str, list[int]]], dtype: str = "BF16") -> None:
    width = {"BF16": 2, "F32": 4}[dtype]
    header: dict[str, dict] = {}
    payload = bytearray()
    for name, shape in sorted(tensors):
        count = 1
        for dim in shape:
            count *= dim
        start = len(payload)
        payload.extend(_payload(name, count * width))
        header[name] = {"dtype": dtype, "shape": shape, "data_offsets": [start, len(payload)]}
    raw = json.dumps(header, separators=(",", ":")).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + payload)


def tiny_dense_snapshot(
    root: Path,
    *,
    model_type: str = "qwen2",
    num_layers: int = 6,
    hidden: int = 8,
    inter: int = 16,
    vocab: int = 32,
    tie_word_embeddings: bool = True,
    split_files: int = 1,
) -> Path:
    """Write an HF-style snapshot directory ``.../snapshots/<COMMIT>``."""
    snapshot = root / "snapshots" / COMMIT
    snapshot.mkdir(parents=True)
    config = {
        "architectures": ["Qwen3ForCausalLM" if model_type == "qwen3" else "Qwen2ForCausalLM"],
        "model_type": model_type,
        "torch_dtype": "bfloat16",
        "num_hidden_layers": num_layers,
        "hidden_size": hidden,
        "intermediate_size": inter,
        "num_attention_heads": 1,
        "num_key_value_heads": 1,
        "head_dim": hidden,
        "vocab_size": vocab,
        "max_position_embeddings": 32,
        "rms_norm_eps": 1e-6,
        "rope_theta": 10000.0,
        "hidden_act": "silu",
        "tie_word_embeddings": tie_word_embeddings,
    }
    (snapshot / "config.json").write_text(json.dumps(config), encoding="utf-8")
    tensors: list[tuple[str, list[int]]] = [("model.embed_tokens.weight", [vocab, hidden])]
    for index in range(num_layers):
        tensors.extend(_layer_tensors(index, hidden, inter))
    tensors.append(("model.norm.weight", [hidden]))
    if not tie_word_embeddings:
        tensors.append(("lm_head.weight", [vocab, hidden]))
    if split_files == 1:
        write_safetensors(snapshot / "model.safetensors", tensors)
    else:
        ordered = sorted(tensors)
        per = -(-len(ordered) // split_files)
        weight_map = {}
        for part in range(split_files):
            name = f"model-{part + 1:05d}-of-{split_files:05d}.safetensors"
            chunk = ordered[part * per:(part + 1) * per]
            write_safetensors(snapshot / name, chunk)
            weight_map.update({tensor: name for tensor, _ in chunk})
        (snapshot / "model.safetensors.index.json").write_text(
            json.dumps({"metadata": {}, "weight_map": weight_map}), encoding="utf-8")
    for name in ("tokenizer.json", "tokenizer_config.json", "generation_config.json"):
        (snapshot / name).write_text("{}", encoding="utf-8")
    return snapshot


def file_rows(snapshot: Path) -> list[dict]:
    rows = []
    for path in sorted(p for p in snapshot.rglob("*") if p.is_file()):
        rows.append({
            "path": path.relative_to(snapshot).as_posix(),
            "size_bytes": path.stat().st_size,
            "content_digest": {"algorithm": "sha256",
                               "value": hashlib.sha256(path.read_bytes()).hexdigest()},
        })
    return rows
