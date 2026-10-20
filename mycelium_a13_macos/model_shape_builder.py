# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build a ``model_resources.ModelShape`` from a pinned snapshot's metadata.

A shape used to be a hand-written table row (``MODEL_SHAPES``).  Everything in
it is a fact of the pinned revision, so it is derived here from the two files
every Hugging Face checkpoint carries:

* ``config.json``: layer count, hidden size, attention and KV heads, head
  dimension, tying and the ``model_type`` (dense ``qwen2`` / ``qwen3`` only);
* the safetensors headers: every tensor's dtype, shape and byte range, which
  give the vocabulary size, the feed-forward width, whether attention has
  biases or q/k norms, whether a tied ``lm_head`` is stored, the source dtype
  and the exact bytes of every layer, the embedding and the output head.

Nothing is trusted from config alone: each config number is cross-checked
against the tensor shapes, every layer must have the identical tensor set,
and the bytes the header reports for each layer, the embedding and the output
must equal what the built shape computes (the shape is the same arithmetic the
resource planner uses, so a shape that passes here reproduces the header
profile exactly).  Anything else (MoE experts, an unknown tensor, mixed
dtypes, an architecture outside the dense Qwen family) is refused by closed
code, never guessed.

Only headers and file sizes are read: tensor data is never loaded and nothing
is fetched.  ``files`` is the snapshot's file listing (name -> bytes), which
gives ``snapshot_bytes`` and ``non_weight_bytes``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .layer_split import QWEN_DENSE_NAMING, SplitRefused, _read_header, _weight_files
from .model_resources import ModelShape

#: Source dtypes the planner's bytes-per-parameter arithmetic covers.
_DTYPE_BYTES = {"BF16": 2, "F16": 2, "F32": 4}
_LAYER_TENSOR = re.compile(QWEN_DENSE_NAMING.layer_pattern + r"(.+)$")
_MAX_LAYERS = 1024

_ATTENTION = ("self_attn.q_proj.weight", "self_attn.k_proj.weight",
              "self_attn.v_proj.weight", "self_attn.o_proj.weight")
_BIASES = ("self_attn.q_proj.bias", "self_attn.k_proj.bias", "self_attn.v_proj.bias")
_QK_NORMS = ("self_attn.q_norm.weight", "self_attn.k_norm.weight")
_MLP = ("mlp.gate_proj.weight", "mlp.up_proj.weight", "mlp.down_proj.weight")
_NORMS = ("input_layernorm.weight", "post_attention_layernorm.weight")


class ShapeRefused(SplitRefused):
    """A closed refusal (``code``) for a snapshot whose shape cannot be built."""


def _refuse(code: str) -> ShapeRefused:
    return ShapeRefused(code)


def _positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _elements(shape: Any) -> int:
    count = 1
    for dimension in shape:
        count *= dimension
    return count


def _tensor(record: Any) -> tuple[str, tuple[int, ...], int]:
    """``(dtype, shape, nbytes)`` of one header record; the offsets must agree."""
    try:
        dtype, shape = record["dtype"], tuple(record["shape"])
        start, end = record["data_offsets"]
    except (KeyError, TypeError, ValueError):
        raise _refuse("model_header_invalid") from None
    if (dtype not in _DTYPE_BYTES or not shape or not all(_positive_int(d) for d in shape)
            or type(start) is not int or type(end) is not int or start < 0 or end <= start):
        raise _refuse("model_shape_dtype_unsupported" if dtype not in _DTYPE_BYTES
                      else "model_header_invalid")
    if end - start != _elements(shape) * _DTYPE_BYTES[dtype]:
        raise _refuse("model_header_invalid")
    return dtype, shape, end - start


def build_model_shape(label: str, config: Mapping[str, Any], headers: Mapping[str, Any],
                      files: Mapping[str, int]) -> ModelShape:
    """The ``ModelShape`` of one snapshot from its config, tensor headers and file sizes.

    ``headers`` merges every safetensors header (tensor name -> record with
    ``dtype``, ``shape``, ``data_offsets``); ``files`` maps every file of the
    snapshot to its size in bytes.
    """
    if not isinstance(label, str) or not label:
        raise _refuse("model_shape_label_invalid")
    if (not isinstance(files, Mapping) or not files
            or not all(isinstance(n, str) and type(s) is int and s >= 0 for n, s in files.items())):
        raise _refuse("model_files_invalid")
    if not isinstance(config, Mapping) or config.get("model_type") not in QWEN_DENSE_NAMING.model_types:
        raise _refuse("model_architecture_unsupported")
    if any(key in config for key in ("num_experts", "num_local_experts", "n_routed_experts")):
        raise _refuse("model_architecture_unsupported")  # MoE is not a dense layer
    layers = config.get("num_hidden_layers")
    if type(layers) is not int or not 1 <= layers <= _MAX_LAYERS:
        raise _refuse("model_config_invalid")
    tied = config.get("tie_word_embeddings", False)
    if type(tied) is not bool:
        raise _refuse("model_config_invalid")
    for key in ("hidden_size", "intermediate_size", "num_attention_heads", "vocab_size"):
        if not _positive_int(config.get(key)):
            raise _refuse("model_config_invalid")
    heads = config["num_attention_heads"]
    kv_heads = config.get("num_key_value_heads", heads)
    head_dim = config.get("head_dim", None)
    if head_dim is None:
        if config["hidden_size"] % heads:
            raise _refuse("model_config_invalid")
        head_dim = config["hidden_size"] // heads
    if not _positive_int(kv_heads) or not _positive_int(head_dim) or heads % kv_heads:
        raise _refuse("model_config_invalid")
    hidden = config["hidden_size"]

    pattern = re.compile(QWEN_DENSE_NAMING.layer_pattern)
    per_layer: list[dict[str, tuple[tuple[int, ...], int]]] = [{} for _ in range(layers)]
    statics: dict[str, tuple[tuple[int, ...], int]] = {}
    dtypes: set[str] = set()
    for name, record in headers.items():
        dtype, shape, nbytes = _tensor(record)
        dtypes.add(dtype)
        matched = pattern.match(name)
        if matched is not None:
            index = int(matched.group(1))
            if index >= layers:
                raise _refuse("model_layer_out_of_range")
            per_layer[index][name[matched.end():]] = (shape, nbytes)
        elif name.startswith(QWEN_DENSE_NAMING.embedding_prefixes + QWEN_DENSE_NAMING.output_prefixes):
            statics[name] = (shape, nbytes)
        else:
            raise _refuse("model_tensor_unsupported")
    if len(dtypes) != 1:
        raise _refuse("model_shape_dtype_unsupported")
    dtype_bytes = _DTYPE_BYTES[next(iter(dtypes))]

    first = per_layer[0]
    attention_bias = any(name in first for name in _BIASES)
    qk_norm = any(name in first for name in _QK_NORMS)
    expected = set(_ATTENTION + _MLP + _NORMS)
    expected |= set(_BIASES) if attention_bias else set()
    expected |= set(_QK_NORMS) if qk_norm else set()
    for tensors in per_layer:
        if set(tensors) != expected:
            raise _refuse("model_tensor_unsupported" if set(tensors) - expected
                          else "model_header_invalid")

    embed = statics.get("model.embed_tokens.weight")
    if embed is None or len(embed[0]) != 2:
        raise _refuse("model_header_invalid")
    vocab = embed[0][0]
    if embed[0] != (vocab, hidden) or vocab != config["vocab_size"]:
        raise _refuse("model_config_mismatch")
    q_width, kv_width = heads * head_dim, kv_heads * head_dim
    inter = config["intermediate_size"]
    for tensors in per_layer:
        for name, shape in (("self_attn.q_proj.weight", (q_width, hidden)),
                            ("self_attn.k_proj.weight", (kv_width, hidden)),
                            ("self_attn.v_proj.weight", (kv_width, hidden)),
                            ("self_attn.o_proj.weight", (hidden, q_width)),
                            ("mlp.gate_proj.weight", (inter, hidden)),
                            ("mlp.up_proj.weight", (inter, hidden)),
                            ("mlp.down_proj.weight", (hidden, inter)),
                            ("input_layernorm.weight", (hidden,)),
                            ("post_attention_layernorm.weight", (hidden,))):
            if tensors[name][0] != shape:
                raise _refuse("model_config_mismatch")
        if attention_bias and (tensors["self_attn.q_proj.bias"][0] != (q_width,)
                               or tensors["self_attn.k_proj.bias"][0] != (kv_width,)
                               or tensors["self_attn.v_proj.bias"][0] != (kv_width,)):
            raise _refuse("model_config_mismatch")
        if qk_norm and any(tensors[n][0] != (head_dim,) for n in _QK_NORMS):
            raise _refuse("model_config_mismatch")

    stores_lm_head = "lm_head.weight" in statics
    if not tied and not stores_lm_head:
        raise _refuse("model_header_invalid")
    if stores_lm_head and statics["lm_head.weight"][0] != (vocab, hidden):
        raise _refuse("model_config_mismatch")
    if set(statics) - {"model.embed_tokens.weight", "model.norm.weight", "lm_head.weight"} \
            or statics.get("model.norm.weight", (None,))[0] != (hidden,):
        raise _refuse("model_tensor_unsupported")

    weight_files = sum(size for name, size in files.items() if name.endswith(".safetensors"))
    if weight_files <= 0:
        raise _refuse("model_files_invalid")
    snapshot_bytes = sum(files.values())
    shape = ModelShape(
        label, layers, hidden, inter, heads, kv_heads, head_dim, vocab, tied, attention_bias,
        qk_norm, snapshot_bytes=snapshot_bytes, non_weight_bytes=snapshot_bytes - weight_files,
        stores_tied_lm_head=tied and stores_lm_head, source_bytes_per_parameter=dtype_bytes)

    # The shape is the planner's arithmetic; it must equal the header profile byte for byte.
    layer_bytes = {sum(nbytes for _, nbytes in tensors.values()) for tensors in per_layer}
    embedding = statics["model.embed_tokens.weight"][1]
    output = sum(nbytes for name, (_, nbytes) in statics.items() if name != "model.embed_tokens.weight")
    if (layer_bytes != {shape.layer_source_bytes} or embedding != shape.embedding_source_bytes
            or output != shape.output_source_bytes):
        raise _refuse("model_shape_profile_mismatch")
    return shape


def read_snapshot_metadata(root: str | Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, int]]:
    """``(config, merged safetensors headers, {file: bytes})`` of a snapshot directory."""
    root = Path(root)
    try:
        config = json.loads((root / "config.json").read_text("utf-8"))
    except (OSError, ValueError):
        raise _refuse("model_config_invalid") from None
    headers: dict[str, Any] = {}
    for path in _weight_files(root):
        for name, record in _read_header(path).items():
            if name in headers:
                raise _refuse("model_header_invalid")
            headers[name] = record
    try:
        files = {entry.name: entry.stat().st_size for entry in sorted(root.iterdir())
                 if entry.is_file()}
    except OSError:
        raise _refuse("model_files_invalid") from None
    return config, headers, files


def shape_from_snapshot(label: str, root: str | Path) -> ModelShape:
    """The ``ModelShape`` of a local pinned snapshot directory (headers only)."""
    return build_model_shape(label, *read_snapshot_metadata(root))


def shape_literal(shape: ModelShape) -> str:
    """A ``MODEL_SHAPES`` row for ``shape`` (for pinning a derived shape in the table)."""
    fields = [shape.label, shape.num_layers, shape.hidden_size, shape.intermediate_size,
              shape.num_attention_heads, shape.num_key_value_heads, shape.head_dim, shape.vocab_size,
              shape.tie_word_embeddings, shape.attention_bias, shape.qk_norm]
    extras = [f"snapshot_bytes={shape.snapshot_bytes:_}", f"non_weight_bytes={shape.non_weight_bytes:_}"]
    if shape.stores_tied_lm_head:
        extras.append("stores_tied_lm_head=True")
    if shape.source_bytes_per_parameter != 2:
        extras.append(f"source_bytes_per_parameter={shape.source_bytes_per_parameter}")
    return "ModelShape(" + ", ".join([repr(f) for f in fields] + extras) + ")"


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Print the ModelShape of a pinned local snapshot directory.")
    parser.add_argument("label")
    parser.add_argument("snapshot")
    args = parser.parse_args(argv)
    try:
        print(shape_literal(shape_from_snapshot(args.label, args.snapshot)))
    except SplitRefused as refusal:
        print(f"refused: {refusal.code}")
        return 1
    return 0


__all__ = ["ShapeRefused", "build_model_shape", "main", "read_snapshot_metadata", "shape_from_snapshot",
           "shape_literal"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
