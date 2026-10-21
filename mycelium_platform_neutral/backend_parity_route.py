"""Model-bound half of the backend parity gate: Qwen2 stage executors, route runner, oracle, battery.

Everything model- or backend-specific lives here so :mod:`backend_parity` stays a pure
NumPy contract.  Stage execution uses the *production* per-block kernels
(``numpy_runtime._qwen2_block_with_kv`` and
``mycelium_router.mlx_runtime._qwen2_block_with_kv``) with stage-local KV, joined by the
real ``MYAC`` float32 wire codec (``mycelium_router.payloads``).  It does not instantiate
the router ports (leases, load proofs, placements), so a record produced here claims
*kernel-level* route parity, not router-port or physical-route parity.

MLX, torch and transformers are imported lazily; this module imports on every platform.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from . import backend_parity as bp

# --------------------------------------------------------------------------- model loading

_SAFETENSORS_DTYPES = {"BF16": 2, "F16": 2, "F32": 4}


def config_from_hf(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a HF Qwen2 ``config.json`` to the kernels' config keys."""

    heads = int(raw["num_attention_heads"])
    hidden = int(raw["hidden_size"])
    return {
        "n_layer": int(raw["num_hidden_layers"]),
        "n_embd": hidden,
        "n_inner": int(raw["intermediate_size"]),
        "n_head": heads,
        "n_kv_head": int(raw["num_key_value_heads"]),
        "head_dim": int(raw.get("head_dim", hidden // heads)),
        "rms_norm_epsilon": float(raw["rms_norm_eps"]),
        "rope_theta": float(raw["rope_theta"]),
        "tie_word_embeddings": bool(raw.get("tie_word_embeddings", False)),
        "vocab_size": int(raw["vocab_size"]),
    }


def _decode_payload(payload: bytes, dtype: str) -> np.ndarray:
    if dtype == "BF16":
        bits = np.frombuffer(payload, dtype="<u2").astype(np.uint32) << 16
        return bits.view(np.float32)
    if dtype == "F16":
        return np.frombuffer(payload, dtype="<f2").astype(np.float32)
    if dtype == "F32":
        return np.frombuffer(payload, dtype="<f4").astype(np.float32, copy=True)
    raise bp.BackendParityError("parity_model_dtype_unsupported", dtype)


def read_safetensors_f32(path: Path, *, only: Callable[[str], bool] | None = None) -> dict[str, np.ndarray]:
    """Read a single-file safetensors checkpoint as float32 (no safetensors package needed)."""

    with Path(path).open("rb") as handle:
        (header_len,) = struct.unpack("<Q", handle.read(8))
        if header_len > 64 * 1024 * 1024:
            raise bp.BackendParityError("parity_model_header_invalid")
        header = json.loads(handle.read(header_len))
        base = 8 + header_len
        tensors: dict[str, np.ndarray] = {}
        for name, entry in header.items():
            if name == "__metadata__" or (only is not None and not only(name)):
                continue
            start, end = entry["data_offsets"]
            handle.seek(base + start)
            payload = handle.read(end - start)
            if len(payload) != end - start or entry["dtype"] not in _SAFETENSORS_DTYPES:
                raise bp.BackendParityError("parity_model_tensor_invalid", name)
            array = _decode_payload(payload, entry["dtype"]).reshape(tuple(entry["shape"]))
            array.flags.writeable = False
            tensors[name] = array
    return tensors


def load_snapshot(snapshot: Path) -> tuple[dict[str, Any], dict[str, np.ndarray], str]:
    """(kernel config, float32 tensors, source artifact digest) of a local HF snapshot."""

    snapshot = Path(snapshot)
    config = config_from_hf(json.loads((snapshot / "config.json").read_text(encoding="utf-8")))
    weights = snapshot / "model.safetensors"
    tensors = read_safetensors_f32(weights)
    return config, tensors, bp.file_digest(weights)


def _bf16_round(array: np.ndarray) -> np.ndarray:
    """Round float32 to the nearest bfloat16 (ties-to-even) and return float32."""

    a = np.ascontiguousarray(array, dtype=np.float32)
    bits = a.view(np.uint32)
    lsb = (bits >> 16) & 1
    rounded = (bits + 0x7FFF + lsb) & 0xFFFF0000
    out = rounded.astype(np.uint32).view(np.float32)
    return np.where(np.isfinite(a), out, a)


def _bf16_bits(array: np.ndarray) -> np.ndarray:
    a = np.ascontiguousarray(array, dtype=np.float32)
    bits = a.view(np.uint32)
    lsb = (bits >> 16) & 1
    return (((bits + 0x7FFF + lsb) >> 16) & 0xFFFF).astype("<u2")


def synthetic_model(
    seed: int = 20261021, *, layers: int = 4, hidden: int = 32, heads: int = 4, kv_heads: int = 2, inner: int = 64, vocab: int = 128
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """A tiny Qwen2-shaped model (q/k/v bias, tied embeddings, rope 1e6, eps 1e-6).

    Weights are bf16-representable like the real checkpoint, so a bf16 round trip of the
    weights is the identity here too.
    """

    rng = np.random.default_rng(seed)
    head_dim = hidden // heads
    config = {
        "n_layer": layers,
        "n_embd": hidden,
        "n_inner": inner,
        "n_head": heads,
        "n_kv_head": kv_heads,
        "head_dim": head_dim,
        "rms_norm_epsilon": 1e-6,
        "rope_theta": 1_000_000.0,
        "tie_word_embeddings": True,
        "vocab_size": vocab,
    }

    def normal(shape: tuple[int, ...], scale: float) -> np.ndarray:
        return _bf16_round(rng.normal(0.0, scale, shape).astype(np.float32))

    tensors: dict[str, np.ndarray] = {
        # small embeddings (like the real checkpoint) so an eps change is visible at layer 0,
        # and a large final-norm gain so the tied head produces peaked logits
        "model.embed_tokens.weight": normal((vocab, hidden), 0.05),
        "model.norm.weight": _bf16_round(rng.normal(8.0, 0.5, (hidden,)).astype(np.float32)),
    }
    q_width = heads * head_dim
    kv_width = kv_heads * head_dim
    for layer in range(layers):
        prefix = f"model.layers.{layer}."
        tensors.update(
            {
                prefix + "input_layernorm.weight": _bf16_round(rng.normal(1.0, 0.1, (hidden,)).astype(np.float32)),
                prefix + "self_attn.q_proj.weight": normal((q_width, hidden), 0.25),
                prefix + "self_attn.k_proj.weight": normal((kv_width, hidden), 0.25),
                prefix + "self_attn.v_proj.weight": normal((kv_width, hidden), 0.25),
                prefix + "self_attn.q_proj.bias": normal((q_width,), 0.5),
                prefix + "self_attn.k_proj.bias": normal((kv_width,), 0.5),
                prefix + "self_attn.v_proj.bias": normal((kv_width,), 0.5),
                prefix + "self_attn.o_proj.weight": normal((hidden, q_width), 0.25),
                prefix + "post_attention_layernorm.weight": _bf16_round(rng.normal(1.0, 0.1, (hidden,)).astype(np.float32)),
                prefix + "mlp.gate_proj.weight": normal((inner, hidden), 0.25),
                prefix + "mlp.up_proj.weight": normal((inner, hidden), 0.25),
                prefix + "mlp.down_proj.weight": normal((hidden, inner), 0.25),
            }
        )
    for array in tensors.values():
        array.flags.writeable = False
    return config, tensors


# --------------------------------------------------------------------------- representations


def fake_quant(weight: np.ndarray, *, bits: int, group: int | None) -> np.ndarray:
    """Symmetric round-to-nearest fake quantisation (float32 in, float32 out).

    ``group=None`` is one scale per output row; otherwise per ``group`` input columns.
    Used for the int4 seeded defects (a 4-bit weight stage claiming a Q0 class).
    """

    levels = float(2 ** (bits - 1) - 1)
    w = np.asarray(weight, dtype=np.float32)
    rows, cols = w.shape
    width = cols if group is None else group
    if cols % width:
        width = cols
    grouped = w.reshape(rows, cols // width, width)
    scale = np.max(np.abs(grouped), axis=2, keepdims=True) / levels
    scale = np.where(scale == 0.0, 1.0, scale).astype(np.float32)
    codes = np.clip(np.sign(grouped / scale) * np.floor(np.abs(grouped / scale) + 0.5), -levels, levels)
    return (codes * scale).reshape(rows, cols).astype(np.float32)


def _is_matrix(key: str, array: np.ndarray) -> bool:
    return array.ndim == 2 and key.endswith(".weight")


# --------------------------------------------------------------------------- stage plan


@dataclass
class StagePlan:
    """One stage: DECLARED identity plus the (possibly defective) ACTUAL execution."""

    backend: str
    layer_start: int
    layer_end: int
    representation: str = "none"
    # --- actual execution; defaults mean "exactly what was declared"
    exec_start: int | None = None
    exec_end: int | None = None
    actual_representation: str | None = None
    config_overrides: dict[str, Any] = field(default_factory=dict)
    transform: Callable[[dict[str, np.ndarray]], dict[str, np.ndarray]] | None = None
    head_override: np.ndarray | None = None
    position_offset: int = 0
    decode_position_offset: int = 0
    reset_kv_per_case: bool = True
    float64_matmul: bool = False
    skip_final_norm: bool = False
    leak_logits: bool = False

    def executed(self) -> tuple[int, int]:
        return (
            self.layer_start if self.exec_start is None else self.exec_start,
            self.layer_end if self.exec_end is None else self.exec_end,
        )


@dataclass
class RoutePlan:
    stages: list[StagePlan]
    wire_mode: str = "float32"

    def declared_representations(self) -> list[str]:
        return [stage.representation for stage in self.stages]

    def boundaries(self) -> list[int]:
        return [stage.layer_end for stage in self.stages[:-1]]


WIRE_MODES = (
    "float32",
    "bf16_round_f32",
    "fp16_round_f32",
    "bfloat16_declared",
    "bf16_bits_as_fp16",
)


@dataclass
class Context:
    config: dict[str, Any]
    base: Mapping[str, np.ndarray]
    corpus: dict[str, Any]
    quant_cache: dict[Any, Any] = field(default_factory=dict)
    mlx_cache: dict[Any, Any] = field(default_factory=dict)
    keepalive: list[Any] = field(default_factory=list)

    def purge_transient(self) -> None:
        """Drop cached conversions of arrays that are not the shared base tensors."""

        base_ids = {id(v) for v in self.base.values()}
        self.quant_cache = {k: v for k, v in self.quant_cache.items() if k[3] in base_ids}
        self.mlx_cache = {k: v for k, v in self.mlx_cache.items() if k in base_ids}
        self.keepalive = [a for a in self.keepalive if id(a) in base_ids]


_LAYER_SUFFIXES = (
    "input_layernorm.weight",
    "self_attn.q_proj.weight",
    "self_attn.k_proj.weight",
    "self_attn.v_proj.weight",
    "self_attn.q_proj.bias",
    "self_attn.k_proj.bias",
    "self_attn.v_proj.bias",
    "self_attn.o_proj.weight",
    "post_attention_layernorm.weight",
    "mlp.gate_proj.weight",
    "mlp.up_proj.weight",
    "mlp.down_proj.weight",
)


def _layer_keys(layer: int) -> list[str]:
    return [f"model.layers.{layer}.{suffix}" for suffix in _LAYER_SUFFIXES]


def _numpy_modules() -> Any:
    import numpy_runtime as module  # repo-root kernel module

    return module


def _mlx_modules() -> tuple[Any, Any, Any]:
    import mlx.core as mx

    from mycelium_router import mlx_runtime
    import runtime_loader

    return mx, mlx_runtime, runtime_loader


def _represent(ctx: Context, backend: str, representation: str, key: str, array: np.ndarray, *, tag: Any = None) -> Any:
    """Convert one float32 tensor into the backend's weight object for ``representation``."""

    cache_key = (backend, representation, key, id(array), tag)
    cached = ctx.quant_cache.get(cache_key)
    if cached is not None:
        return cached
    ctx.keepalive.append(array)
    if representation == "none":
        value: Any = array
    elif representation == "int8-weight-only":
        if backend == "numpy":
            value = _numpy_modules().quantize_qwen2_numpy_tensor(key, array)
        else:
            mx, _, loader = _mlx_modules()
            value = loader._quantize_qwen2_mlx_tensor(key, mx.array(array), mx)
    elif representation in ("int4-row", "int4-g128"):
        value = fake_quant(array, bits=4, group=None if representation == "int4-row" else 128) if _is_matrix(key, array) else array
    else:
        raise bp.BackendParityError("parity_representation_unknown", representation)
    ctx.quant_cache[cache_key] = value
    return value


def _to_backend(ctx: Context, backend: str, value: Any) -> Any:
    if backend == "numpy":
        return value
    mx, _, _ = _mlx_modules()
    if isinstance(value, np.ndarray):
        cached = ctx.mlx_cache.get(id(value))
        if cached is None:
            ctx.keepalive.append(value)
            cached = mx.array(value)
            mx.eval(cached)
            ctx.mlx_cache[id(value)] = cached
        return cached
    return value  # Int8RowwiseWeight built from mx arrays already


class _Stage:
    """A stage executor with stage-local KV (production kernel per block)."""

    def __init__(self, ctx: Context, plan: StagePlan, *, has_embed: bool, has_head: bool, head_representation: str | None = None) -> None:
        self.plan = plan
        self.backend = plan.backend
        self.has_embed = has_embed
        self.has_head = has_head
        self.config = {**ctx.config, **plan.config_overrides}
        start, end = plan.executed()
        self.layers = list(range(start, end))
        actual = plan.actual_representation or plan.representation
        base = ctx.base
        keys = [k for layer in self.layers for k in _layer_keys(layer)]
        subset = {k: np.asarray(base[k]) for k in keys}
        if plan.transform is not None:
            subset = plan.transform(subset)
        weights: dict[str, Any] = {}
        for key, array in subset.items():
            weights[key] = _to_backend(ctx, self.backend, _represent(ctx, self.backend, actual, key, array, tag=("t", id(plan.transform))))
        self.tensors = weights
        self.embed = None
        if has_embed:
            embed = np.asarray(base["model.embed_tokens.weight"])
            self.embed = _to_backend(ctx, self.backend, _represent(ctx, self.backend, actual, "model.embed_tokens.weight", embed))
        self.norm = None
        self.head = None
        if has_head:
            self.norm = _to_backend(ctx, self.backend, np.asarray(base["model.norm.weight"]))
            if plan.head_override is not None:
                head_array = np.asarray(plan.head_override, dtype=np.float32)
                head_key = "lm_head.weight"
            elif ctx.config["tie_word_embeddings"]:
                head_array = np.asarray(base["model.embed_tokens.weight"])
                head_key = "model.embed_tokens.weight"
            else:
                head_array = np.asarray(base["lm_head.weight"])
                head_key = "lm_head.weight"
            self.head = _to_backend(
                ctx,
                self.backend,
                _represent(ctx, self.backend, head_representation or actual, head_key, head_array, tag="head"),
            )
        self.kv: dict[int, Any] = {}

    def reset(self) -> None:
        self.kv = {}

    # -- NumPy ------------------------------------------------------------------------
    def _forward_numpy(self, x: np.ndarray, position: int, capture: dict[int, list[np.ndarray]] | None) -> np.ndarray:
        nrt = _numpy_modules()
        if self.has_embed:
            hidden = nrt._qwen2_embedding(self.embed, np.asarray(x, dtype=np.int64)).astype(np.float32, copy=False)
        else:
            hidden = np.asarray(x, dtype=np.float32)
        if self.plan.float64_matmul:
            hidden = hidden.astype(np.float64)
        for layer in self.layers:
            hidden, kv = nrt._qwen2_block_with_kv(
                hidden, self.tensors, f"model.layers.{layer}.", self.config, position, self.kv.get(layer)
            )
            self.kv[layer] = kv
            if capture is not None and (layer + 1) in capture:
                capture[layer + 1].append(np.array(hidden, dtype=np.float32, copy=True))
        hidden = np.asarray(hidden, dtype=np.float32)
        if not self.has_head:
            return hidden
        last = hidden[:, -1:, :]
        if not self.plan.skip_final_norm:
            last = nrt._rms_norm(last, self.norm, float(self.config["rms_norm_epsilon"]))
        logits = nrt._qwen2_linear(last, self.head)
        return np.asarray(logits, dtype=np.float32)[0, 0]

    # -- MLX --------------------------------------------------------------------------
    def _forward_mlx(self, x: np.ndarray, position: int) -> np.ndarray:
        mx, rt, _ = _mlx_modules()
        if self.has_embed:
            hidden = rt._qwen2_embedding(self.embed, mx.array(np.asarray(x, dtype=np.int32)))
            hidden = hidden.astype(mx.float32)
        else:
            hidden = mx.array(np.asarray(x, dtype=np.float32))
        for layer in self.layers:
            hidden, kv = rt._qwen2_block_with_kv(
                hidden, self.tensors, f"model.layers.{layer}.", self.config, position, self.kv.get(layer)
            )
            mx.eval(hidden, *kv)
            self.kv[layer] = kv
        if not self.has_head:
            return np.array(hidden, dtype=np.float32, copy=True)
        last = hidden[:, -1:, :]
        if not self.plan.skip_final_norm:
            last = rt._rms_norm(last, self.norm, float(self.config["rms_norm_epsilon"]))
        logits = rt._qwen2_linear(last, self.head)
        mx.eval(logits)
        return np.array(logits, dtype=np.float32, copy=True)[0, 0]

    def forward(self, x: np.ndarray, position: int, capture: dict[int, list[np.ndarray]] | None = None) -> np.ndarray:
        with np.errstate(all="ignore"):
            if self.backend == "numpy":
                return self._forward_numpy(x, position, capture)
            return self._forward_mlx(x, position)


# --------------------------------------------------------------------------- wire


def _wire_hop(hidden: np.ndarray, mode: str) -> tuple[np.ndarray, dict[str, Any]]:
    """Send one hop tensor through the real MYAC codec; return (received f32, frame meta)."""

    from mycelium_router.payloads import decode_activation, encode_activation

    array = np.ascontiguousarray(hidden, dtype=np.float32)
    shape = tuple(int(d) for d in array.shape)
    if mode == "float32":
        dtype, data = "float32", array.astype("<f4").tobytes()
    elif mode == "bf16_round_f32":
        dtype, data = "float32", _bf16_round(array).astype("<f4").tobytes()
    elif mode == "fp16_round_f32":
        dtype, data = "float32", array.astype(np.float16).astype(np.float32).astype("<f4").tobytes()
    elif mode in ("bfloat16_declared", "bf16_bits_as_fp16"):
        dtype = "bfloat16" if mode == "bfloat16_declared" else "float16"
        data = _bf16_bits(array).tobytes()
    else:
        raise bp.BackendParityError("parity_wire_mode_invalid", mode)
    frame = decode_activation(encode_activation(dtype=dtype, shape=shape, data=data))
    if frame.dtype == "float32":
        received = np.frombuffer(frame.data, dtype="<f4").astype(np.float32).reshape(frame.shape)
    elif frame.dtype == "float16":
        received = np.frombuffer(frame.data, dtype="<f2").astype(np.float32).reshape(frame.shape)
    else:  # bfloat16: raw bits, receiver upcasts correctly
        bits = np.frombuffer(frame.data, dtype="<u2").astype(np.uint32) << 16
        received = bits.view(np.float32).reshape(frame.shape)
    return received, {"dtype": frame.dtype, "shape": tuple(frame.shape)}


# --------------------------------------------------------------------------- route runner


def build_stages(ctx: Context, plan: RoutePlan) -> list[_Stage]:
    last = len(plan.stages) - 1
    stages = []
    for index, stage_plan in enumerate(plan.stages):
        stages.append(
            _Stage(
                ctx,
                stage_plan,
                has_embed=index == 0,
                has_head=index == last,
            )
        )
    return stages


def run_route(ctx: Context, plan: RoutePlan, *, stages: Sequence[_Stage] | None = None) -> bp.RouteCapture:
    """Teacher-forced prefill + stepwise decode of every corpus case over the stage chain."""

    stages = list(stages) if stages is not None else build_stages(ctx, plan)
    boundaries = plan.boundaries()
    cases: dict[str, bp.CaseCapture] = {}
    for case in ctx.corpus["cases"]:
        ids = np.asarray(case["token_ids"], dtype=np.int64)[None, :]
        prefill = case["prefill_len"]
        total = ids.shape[1]
        for stage in stages:
            if stage.plan.reset_kv_per_case:
                stage.reset()
        hop_rows: dict[int, list[np.ndarray]] = {b: [] for b in boundaries}
        hop_wire: dict[int, dict[str, Any]] = {}
        logits: list[np.ndarray] = []
        extra = 0

        def step(token_slice: slice, position: int, decode: bool) -> np.ndarray:
            nonlocal extra
            x: np.ndarray = ids[:, token_slice]
            for index, stage in enumerate(stages):
                pos = position + stage.plan.position_offset + (stage.plan.decode_position_offset if decode else 0)
                out = stage.forward(x, pos)
                if index == len(stages) - 1:
                    return out
                received, meta = _wire_hop(out, plan.wire_mode)
                boundary = boundaries[index]
                hop_rows[boundary].append(received[0])
                hop_wire.setdefault(boundary, meta)
                if stage.plan.leak_logits:
                    extra += 1
                x = received
            raise AssertionError("unreachable")

        logits.append(step(slice(0, prefill), 0, False))
        for t in range(prefill, total - 1):
            logits.append(step(slice(t, t + 1), t, True))
        cases[case["case_id"]] = bp.CaseCapture(
            case_id=case["case_id"],
            logits=np.stack(logits).astype(np.float32),
            hops={b: np.concatenate(rows, axis=0) for b, rows in hop_rows.items()},
            hop_wire=hop_wire,
            extra_outputs=extra,
        )
    return bp.RouteCapture(cases=cases, meta={"wire_mode": plan.wire_mode})


def run_monolith(ctx: Context, representations: Sequence[str], boundaries: Sequence[int], layer_splits: Sequence[int]) -> bp.RouteCapture:
    """Same-numerics reference: all layers in ONE NumPy stage (no hop, no wire).

    ``representations[i]`` applies to layers ``layer_splits[i-1]..layer_splits[i]`` (the declared
    stage ranges); the embedding takes stage 0's representation and the head the last stage's,
    exactly as the split route does.  Hidden states are captured at ``boundaries``.
    """

    n_layer = ctx.config["n_layer"]
    edges = [0, *layer_splits, n_layer]
    nrt = _numpy_modules()
    layers = list(range(n_layer))

    def rep_of(layer: int) -> str:
        for index in range(len(edges) - 1):
            if edges[index] <= layer < edges[index + 1]:
                return representations[index]
        raise AssertionError(layer)

    tensors: dict[str, Any] = {}
    for layer in layers:
        for key in _layer_keys(layer):
            tensors[key] = _represent(ctx, "numpy", rep_of(layer), key, np.asarray(ctx.base[key]))
    embed = _represent(ctx, "numpy", representations[0], "model.embed_tokens.weight", np.asarray(ctx.base["model.embed_tokens.weight"]))
    head_key = "model.embed_tokens.weight" if ctx.config["tie_word_embeddings"] else "lm_head.weight"
    head = _represent(ctx, "numpy", representations[-1], head_key, np.asarray(ctx.base[head_key]), tag="head")
    norm = np.asarray(ctx.base["model.norm.weight"])
    config = ctx.config
    cases: dict[str, bp.CaseCapture] = {}
    with np.errstate(all="ignore"):
        for case in ctx.corpus["cases"]:
            ids = np.asarray(case["token_ids"], dtype=np.int64)[None, :]
            prefill, total = case["prefill_len"], ids.shape[1]
            kv: dict[int, Any] = {}
            capture: dict[int, list[np.ndarray]] = {b: [] for b in boundaries}
            logits = []

            def step(token_slice: slice, position: int) -> np.ndarray:
                hidden = nrt._qwen2_embedding(embed, ids[:, token_slice]).astype(np.float32, copy=False)
                for layer in layers:
                    hidden, kv[layer] = nrt._qwen2_block_with_kv(
                        hidden, tensors, f"model.layers.{layer}.", config, position, kv.get(layer)
                    )
                    if (layer + 1) in capture:
                        capture[layer + 1].append(np.array(hidden[0], dtype=np.float32, copy=True))
                last = nrt._rms_norm(hidden[:, -1:, :], norm, float(config["rms_norm_epsilon"]))
                return np.asarray(nrt._qwen2_linear(last, head), dtype=np.float32)[0, 0]

            logits.append(step(slice(0, prefill), 0))
            for t in range(prefill, total - 1):
                logits.append(step(slice(t, t + 1), t))
            cases[case["case_id"]] = bp.CaseCapture(
                case_id=case["case_id"],
                logits=np.stack(logits).astype(np.float32),
                hops={b: np.concatenate(rows, axis=0) for b, rows in capture.items()},
                hop_wire={b: {"dtype": "float32", "shape": (1, int(rows[0].shape[0]), int(rows[0].shape[1]))} for b, rows in capture.items()},
            )
    return bp.RouteCapture(cases=cases, meta={"kind": "monolith", "representations": list(representations)})


# --------------------------------------------------------------------------- oracle


def generate_numpy_oracle(ctx: Context, boundaries: Sequence[int]) -> bp.RouteCapture:
    """Independent full-context (no KV, no stages) NumPy float32 oracle - tests / fallback only."""

    nrt = _numpy_modules()
    config = ctx.config
    tensors = {k: np.asarray(v) for k, v in ctx.base.items()}
    head = tensors["model.embed_tokens.weight"] if config["tie_word_embeddings"] else tensors["lm_head.weight"]
    cases: dict[str, bp.CaseCapture] = {}
    with np.errstate(all="ignore"):
        for case in ctx.corpus["cases"]:
            ids = np.asarray(case["token_ids"], dtype=np.int64)[None, :]
            hidden = nrt._qwen2_embedding(tensors["model.embed_tokens.weight"], ids).astype(np.float32)
            captured: dict[int, np.ndarray] = {}
            for layer in range(config["n_layer"]):
                hidden = nrt._qwen2_block(hidden, tensors, f"model.layers.{layer}.", config)
                if (layer + 1) in boundaries:
                    captured[layer + 1] = np.array(hidden[0, :-1], dtype=np.float32, copy=True)
            normed = nrt._rms_norm(hidden, tensors["model.norm.weight"], float(config["rms_norm_epsilon"]))
            full = np.asarray(nrt._qwen2_linear(normed, head), dtype=np.float32)[0]
            positions = bp.scored_positions(case)
            cases[case["case_id"]] = bp.CaseCapture(case["case_id"], full[positions], captured, {}, 0)
    return bp.RouteCapture(cases=cases, meta={"kind": "numpy-full-context-oracle"})


def save_oracle(path: Path, capture: bp.RouteCapture, identity: Mapping[str, Any]) -> dict[str, Any]:
    arrays: dict[str, np.ndarray] = {}
    for case_id, case in capture.cases.items():
        arrays[f"logits__{case_id}"] = np.asarray(case.logits, dtype=np.float32)
        for boundary, hidden in case.hops.items():
            arrays[f"hidden__{case_id}__{boundary}"] = np.asarray(hidden, dtype=np.float32)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        np.savez(handle, **arrays)
    info = {**identity, "archive_digest": bp.file_digest(path), "dtype": "float32"}
    return info


def load_oracle(path: Path, corpus: Mapping[str, Any], *, expected_digest: str | None = None) -> bp.RouteCapture:
    path = Path(path)
    if expected_digest is not None and bp.file_digest(path) != expected_digest:
        raise bp.BackendParityError("parity_oracle_digest_mismatch")
    cases: dict[str, bp.CaseCapture] = {}
    with np.load(path, allow_pickle=False) as archive:
        for case in corpus["cases"]:
            case_id = case["case_id"]
            key = f"logits__{case_id}"
            if key not in archive.files:
                raise bp.BackendParityError("parity_oracle_case_missing", case_id)
            hops = {}
            for name in archive.files:
                if name.startswith(f"hidden__{case_id}__"):
                    hops[int(name.rsplit("__", 1)[1])] = np.array(archive[name], dtype=np.float32)
            logits = np.array(archive[key], dtype=np.float32)
            if logits.shape[0] != len(bp.scored_positions(case)):
                raise bp.BackendParityError("parity_oracle_shape_mismatch", case_id)
            cases[case_id] = bp.CaseCapture(case_id, logits, hops, {}, 0)
    return bp.RouteCapture(cases=cases, meta={"kind": "oracle"})


# --------------------------------------------------------------------------- route identity


def _backend_identity(backend: str) -> tuple[str, str, str]:
    """(backend_version, build_digest, device_class) of the code that actually ran."""

    root = Path(__file__).resolve().parent.parent
    if backend == "numpy":
        return importlib.metadata.version("numpy"), bp.file_digest(root / "numpy_runtime.py"), "cpu"
    if backend == "mlx":
        return importlib.metadata.version("mlx"), bp.file_digest(root / "mycelium_router" / "mlx_runtime.py"), "metal"
    raise bp.BackendParityError("parity_backend_unknown", backend)


def stage_identity(backend: str) -> dict[str, str]:
    """Identity of the code that runs ``backend`` on THIS machine (travels in stage metadata)."""

    version, build, device = _backend_identity(backend)
    return {"backend": backend, "backend_version": version, "build_digest": build, "device_class": device}


def route_document(
    ctx: Context,
    plan: RoutePlan,
    *,
    model_id: str,
    revision: str,
    source_digest: str,
    identities: Sequence[Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """The declared route tuple.  ``identities`` (one per stage, from :func:`stage_identity` run on the
    machine that executed the stage) overrides this machine's identity; its backend must match the plan."""

    if identities is not None and len(identities) != len(plan.stages):
        raise bp.BackendParityError("parity_route_invalid", "identity count")
    stages = []
    for index, stage in enumerate(plan.stages):
        if identities is not None:
            ident = identities[index]
            if ident.get("backend") != stage.backend:
                raise bp.BackendParityError("parity_route_invalid", f"stage {index} ran backend {ident.get('backend')}, plan says {stage.backend}")
            version, build, device = ident["backend_version"], ident["build_digest"], ident["device_class"]
        else:
            version, build, device = _backend_identity(stage.backend)
        stages.append(
            {
                "stage_index": index,
                "layer_start": stage.layer_start,
                "layer_end": stage.layer_end,
                "backend": stage.backend,
                "backend_version": version,
                "build_digest": build,
                "device_class": device,
                "quantization": stage.representation,
            }
        )
    return {
        "model_id": model_id,
        "revision": revision,
        "source_artifact_digest": source_digest,
        "n_layer": ctx.config["n_layer"],
        "wire_dtype": "float32",  # what the route DECLARES; a defective wire is an actual-vs-declared mismatch
        "stages": stages,
    }


def two_stage_plan(n_layer: int, first: str, second: str, *, split: int | None = None, rep0: str = "none", rep1: str = "none") -> RoutePlan:
    cut = n_layer // 2 if split is None else split
    return RoutePlan(
        stages=[
            StagePlan(first, 0, cut, rep0),
            StagePlan(second, cut, n_layer, rep1),
        ]
    )


# --------------------------------------------------------------------------- evaluation of one plan


class Evaluator:
    """Runs a plan and judges it against the (cached) monolith and oracle."""

    def __init__(self, ctx: Context, policy: Mapping[str, Any], oracle: bp.RouteCapture) -> None:
        self.ctx = ctx
        self.policy = policy
        self.oracle = oracle
        self._mono: dict[Any, bp.RouteCapture] = {}

    def monolith(self, plan: RoutePlan) -> bp.RouteCapture:
        splits = tuple(s.layer_end for s in plan.stages[:-1])
        key = (tuple(plan.declared_representations()), splits)
        if key not in self._mono:
            self._mono[key] = run_monolith(self.ctx, plan.declared_representations(), tuple(plan.boundaries()), splits)
        return self._mono[key]

    def evaluate(self, plan: RoutePlan) -> tuple[dict[str, dict[str, Any]], bp.RouteCapture]:
        capture = run_route(self.ctx, plan)
        gates = bp.evaluate_route(capture, self.monolith(plan), self.oracle, self.policy, plan.declared_representations())
        return gates, capture


# --------------------------------------------------------------------------- scenarios


@dataclass(frozen=True)
class Scenario:
    name: str
    kind: str  # "defect" | "control"
    description: str
    mutate: Callable[[RoutePlan, Context], None]
    expect: str = ""  # which gate/metric is expected to fire (documentation, not enforced)


def _drop_bias(subset: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {k: (np.zeros_like(v) if k.endswith(("q_proj.bias", "k_proj.bias", "v_proj.bias")) else v) for k, v in subset.items()}


def _drop_bias_first_layer(subset: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    first = min(int(k.split(".")[2]) for k in subset)
    return {
        k: (np.zeros_like(v) if k.startswith(f"model.layers.{first}.") and k.endswith(("q_proj.bias", "k_proj.bias", "v_proj.bias")) else v)
        for k, v in subset.items()
    }


def _transpose_o_proj_first_layer(subset: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    first = min(int(k.split(".")[2]) for k in subset)
    target = f"model.layers.{first}.self_attn.o_proj.weight"
    out = dict(subset)
    out[target] = np.ascontiguousarray(subset[target].T)  # square: shape-valid, silently wrong
    return out


def _bf16_roundtrip_weights(subset: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    return {k: _bf16_round(v) for k, v in subset.items()}


def _set(stage_index: int, **fields: Any) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        for key, value in fields.items():
            setattr(plan.stages[stage_index], key, value)

    return mutate


def _all(*mutators: Callable[[RoutePlan, Context], None]) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        for mutator in mutators:
            mutator(plan, ctx)

    return mutate


def _head_random(plan: RoutePlan, ctx: Context) -> None:
    embed = np.asarray(ctx.base["model.embed_tokens.weight"])
    rng = np.random.default_rng(1234)
    plan.stages[-1].head_override = rng.normal(0.0, float(embed.std()), embed.shape).astype(np.float32)


def _swap_stages(plan: RoutePlan, ctx: Context) -> None:
    cut = plan.stages[0].layer_end
    n = ctx.config["n_layer"]
    plan.stages[0].exec_start, plan.stages[0].exec_end = cut, n
    plan.stages[1].exec_start, plan.stages[1].exec_end = 0, cut


def _swap_backends(plan: RoutePlan, ctx: Context) -> None:
    plan.stages[0].backend, plan.stages[1].backend = plan.stages[1].backend, plan.stages[0].backend


def _homogeneous(backend: str) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        for stage in plan.stages:
            stage.backend = backend

    return mutate


def _wire(mode: str) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        plan.wire_mode = mode

    return mutate


def _split_at(fraction_num: int, fraction_den: int) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        cut = max(1, min(ctx.config["n_layer"] - 1, (ctx.config["n_layer"] * fraction_num) // fraction_den))
        plan.stages[0].layer_end = cut
        plan.stages[1].layer_start = cut

    return mutate


def _first_layer_of_stage1(plan: RoutePlan) -> int:
    return plan.stages[1].layer_start


def _stage1_range(offset_start: int = 0, offset_end: int = 0) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        stage = plan.stages[1]
        stage.exec_start = stage.layer_start + offset_start
        stage.exec_end = stage.layer_end + offset_end

    return mutate


def _stage0_range(offset_end: int) -> Callable[[RoutePlan, Context], None]:
    def mutate(plan: RoutePlan, ctx: Context) -> None:
        plan.stages[0].exec_end = plan.stages[0].layer_end + offset_end

    return mutate


def default_scenarios() -> list[Scenario]:
    """The seeded-defect battery (plan §2.5 negative controls + the L1 brief) and legit-noise controls."""

    d = "defect"
    c = "control"
    return [
        # ---- legitimate noise / alternatives: must NOT fail ------------------------------------
        Scenario("control_fp64_matmul_stage1", c, "stage 1 matmuls accumulate in float64 (pure rounding noise)", _set(1, float64_matmul=True, backend="numpy")),
        Scenario("control_bf16_weight_roundtrip", c, "weights round-tripped through bf16 (the checkpoint is bf16: identity)", _all(_set(0, transform=_bf16_roundtrip_weights), _set(1, transform=_bf16_roundtrip_weights))),
        Scenario("control_reverse_backend_order", c, "NumPy stage 0 + MLX stage 1", _swap_backends),
        Scenario("control_homogeneous_mlx", c, "MLX on both stages", _homogeneous("mlx")),
        Scenario("control_homogeneous_numpy", c, "NumPy on both stages", _homogeneous("numpy")),
        Scenario("control_split_at_quarter", c, "boundary at n_layer/4 instead of n_layer/2", _split_at(1, 4)),
        Scenario("control_split_at_three_quarters", c, "boundary at 3*n_layer/4", _split_at(3, 4)),
        Scenario("control_int8_weight_only_stage1", c, "stage 1 declared and executed int8-weight-only (Q0 member)", _set(1, representation="int8-weight-only")),
        Scenario("control_int8_weight_only_both", c, "both stages int8-weight-only", _all(_set(0, representation="int8-weight-only"), _set(1, representation="int8-weight-only"))),
        # ---- seeded defects: every one must FAIL ----------------------------------------------
        Scenario("defect_rms_eps_1e-5_stage0", d, "rms_norm_eps 1e-5 instead of 1e-6 on stage 0 (small-norm embedding inputs see it)", _set(0, config_overrides={"rms_norm_epsilon": 1e-5}), "H"),
        Scenario("defect_rms_eps_1e-5_all", d, "rms_norm_eps 1e-5 on both stages", _all(_set(0, config_overrides={"rms_norm_epsilon": 1e-5}), _set(1, config_overrides={"rms_norm_epsilon": 1e-5})), "H"),
        Scenario("defect_rope_base_1e4_stage1", d, "swapped rope base: stage 1 uses 1e4 while stage 0 uses 1e6", _set(1, config_overrides={"rope_theta": 1e4}), "H"),
        Scenario("defect_rope_base_1e4_all", d, "rope_theta 1e4 on both stages", _all(_set(0, config_overrides={"rope_theta": 1e4}), _set(1, config_overrides={"rope_theta": 1e4})), "H,Q"),
        Scenario("defect_layer_range_short_stage0", d, "stage 0 runs [0,b-1) but declares [0,b) (off by one, one layer lost)", _stage0_range(-1), "H"),
        Scenario("defect_layer_range_overlap", d, "stage 1 starts one layer early (layer b-1 applied twice)", _stage1_range(offset_start=-1), "H"),
        Scenario("defect_layer_range_skip", d, "stage 1 starts one layer late (layer b skipped)", _stage1_range(offset_start=1), "H"),
        Scenario("defect_stages_swapped", d, "stage 0 runs the upper layers and stage 1 the lower layers", _swap_stages, "H"),
        Scenario("defect_qkv_bias_dropped_stage1", d, "q/k/v bias dropped on every stage-1 layer", _set(1, transform=_drop_bias), "H"),
        Scenario("defect_qkv_bias_dropped_one_layer", d, "q/k/v bias dropped on the first stage-1 layer only", _set(1, transform=_drop_bias_first_layer), "H"),
        Scenario("defect_o_proj_transposed_one_layer", d, "square o_proj weight transposed on one layer (shape-valid)", _set(1, transform=_transpose_o_proj_first_layer), "H"),
        Scenario("defect_head_untied_random", d, "tied embedding not loaded: random lm_head", _head_random, "H,Q"),
        Scenario("defect_final_norm_dropped", d, "final RMSNorm skipped before the head", _set(1, skip_final_norm=True), "H,Q"),
        Scenario("defect_position_offset_plus1_all", d, "every position +1 on both stages: the kernel's causal mask reads absolute position, so prefill sees future tokens", _all(_set(0, position_offset=1), _set(1, position_offset=1)), "H"),
        Scenario("defect_decode_position_plus1_stage1", d, "stage 1 decode position off by one (KV position offset +1)", _set(1, decode_position_offset=1), "H"),
        Scenario("defect_decode_position_plus1_all", d, "both stages decode at position+1", _all(_set(0, decode_position_offset=1), _set(1, decode_position_offset=1)), "H"),
        Scenario("defect_stale_kv_across_requests_stage1", d, "stage 1 does not clear its KV between requests", _set(1, reset_kv_per_case=False), "H"),
        Scenario("defect_hop_bf16_rounded_on_f32_wire", d, "hop rounded to bf16 precision but sent as float32", _wire("bf16_round_f32"), "H"),
        Scenario("defect_hop_bfloat16_frame", d, "hop sent as a bfloat16 frame on a float32-only route", _wire("bfloat16_declared"), "N,H"),
        Scenario("defect_hop_bf16_bits_read_as_fp16", d, "bf16 bits decoded as float16 (R-W3 mapping bug)", _wire("bf16_bits_as_fp16"), "N,H"),
        Scenario("defect_hop_fp16_rounded", d, "hop rounded to float16 precision but sent as float32", _wire("fp16_round_f32"), "H"),
        Scenario("defect_int4_rowwise_claimed_q0", d, "stage 1 executes row-wise int4 weights while declaring int8-weight-only (Q0)", _set(1, representation="int8-weight-only", actual_representation="int4-row"), "H,Q"),
        Scenario("defect_int4_g128_claimed_q0", d, "stage 1 executes group-128 int4 weights while declaring int8-weight-only (Q0)", _set(1, representation="int8-weight-only", actual_representation="int4-g128"), "H,Q"),
        Scenario("observation_rms_eps_1e-5_stage1_only", "observation", "rms_norm_eps 1e-5 on stage 1 only: the effect is eps/mean-square of a grown residual stream, reported for transparency", _set(1, config_overrides={"rms_norm_epsilon": 1e-5}), "H?"),
        Scenario("defect_non_final_stage_leaks_logits", d, "stage 0 emits an extra logits tensor next to the hidden state", _set(0, leak_logits=True), "N"),
    ]


def run_scenarios(
    ctx: Context,
    evaluator: Evaluator,
    base_plan_factory: Callable[[], RoutePlan],
    scenarios: Sequence[Scenario],
) -> list[dict[str, Any]]:
    rows = []
    for scenario in scenarios:
        plan = base_plan_factory()
        scenario.mutate(plan, ctx)
        started = time.perf_counter()
        gates, _ = evaluator.evaluate(plan)
        ctx.purge_transient()
        failed = {name: [c["metric"] for c in g["checks"] if not c["pass"]] or ([g["reason"]] if g["status"] == "fail" else []) for name, g in gates.items() if g["status"] == "fail"}
        rows.append(
            {
                "name": scenario.name,
                "kind": scenario.kind,
                "description": scenario.description,
                "expected_gate": scenario.expect,
                "verdict": bp.route_verdict(gates),
                "gate_status": {name: g["status"] for name, g in gates.items()},
                "failed": failed,
                "metrics": {
                    name: {k: v for k, v in g["metrics"].items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
                    for name, g in gates.items()
                    if g["status"] != "not_evaluated"
                },
                "seconds": round(time.perf_counter() - started, 2),
            }
        )
    return rows


def legit_samples(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(H samples, Q samples) for :func:`backend_parity.calibrate_policy` from control rows.

    Controls are the *calibration set*: that they pass afterwards is therefore by
    construction (with margin), not independent validation.  The independent evidence is
    that every seeded defect still fails and, later, the SB2 run.
    """

    h_rows, q_rows = [], []
    for row in rows:
        if row["kind"] != "control":
            continue
        q_name = next(name for name in row["metrics"] if name in ("Q0", "Q1"))
        h_metrics = dict(row["metrics"]["H"])
        h_metrics["activation_abs_max"] = row["metrics"]["N"]["activation_abs_max"]
        h_rows.append({"name": row["name"], "metrics": h_metrics})
        if q_name == "Q0":
            q_rows.append({"name": row["name"], "metrics": dict(row["metrics"]["Q0"])})
    return h_rows, q_rows


# --------------------------------------------------------------------------- file-based stage replay
#
# Teacher-forced inputs are fixed, so stage k's hop frames do not depend on stage k+1: each
# stage can run on its own machine, writing/reading real MYAC frames, and the combined result is
# the same numerics as a live route.  This is how one parity run spans the two Macs and SB2
# without a live network (see docs/design/PV29-SB2-FIRST-SLICE.md).

FRAMES_PROTOCOL = "mycelium.backend_parity_frames.v1"


def _case_steps(case: Mapping[str, Any]) -> list[tuple[slice, int]]:
    """(token slice, position) of the prefill step followed by one decode step per forced token."""

    prefill, total = case["prefill_len"], len(case["token_ids"])
    return [(slice(0, prefill), 0), *[(slice(t, t + 1), t) for t in range(prefill, total - 1)]]


def _encode_frame(hidden: np.ndarray, wire_mode: str) -> bytes:
    from mycelium_router.payloads import encode_activation

    array = np.ascontiguousarray(hidden, dtype=np.float32)
    if wire_mode != "float32":
        raise bp.BackendParityError("parity_wire_mode_invalid", "file replay carries honest float32 frames only")
    return encode_activation(dtype="float32", shape=tuple(int(d) for d in array.shape), data=array.astype("<f4").tobytes())


def _decode_frame(frame: bytes) -> tuple[np.ndarray, dict[str, Any]]:
    from mycelium_router.payloads import decode_activation

    envelope = decode_activation(bytes(frame))
    if envelope.dtype == "float32":
        array = np.frombuffer(envelope.data, dtype="<f4").astype(np.float32)
    elif envelope.dtype == "float16":
        array = np.frombuffer(envelope.data, dtype="<f2").astype(np.float32)
    else:
        array = (np.frombuffer(envelope.data, dtype="<u2").astype(np.uint32) << 16).view(np.float32)
    return array.reshape(envelope.shape), {"dtype": envelope.dtype, "shape": tuple(envelope.shape)}


def run_stage_frames(
    ctx: Context,
    plan: RoutePlan,
    index: int,
    frames_in: Mapping[str, list[bytes]] | None,
    *,
    timings: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, list[bytes]] | None, dict[str, np.ndarray] | None]:
    """Run stage ``index`` of ``plan`` over every case.

    ``timings`` (optional) receives one row per step: ``kind`` (prefill|decode), ``rows`` (tokens
    processed), ``compute_s`` (frame decode + stage forward) and ``frame_s`` (frame encode).

    Returns ``(frames_out, None)`` for a non-final stage and ``(None, logits_by_case)`` for the
    final one.  ``frames_in`` is ``None`` only for stage 0 (which reads the corpus tokens).
    """

    n_stages = len(plan.stages)
    if (index == 0) != (frames_in is None):
        raise bp.BackendParityError("parity_stage_input_invalid", "only stage 0 reads tokens")
    stage = _Stage(ctx, plan.stages[index], has_embed=index == 0, has_head=index == n_stages - 1)
    frames_out: dict[str, list[bytes]] = {}
    logits_out: dict[str, np.ndarray] = {}
    for case in ctx.corpus["cases"]:
        stage.reset()
        ids = np.asarray(case["token_ids"], dtype=np.int64)[None, :]
        steps = _case_steps(case)
        if frames_in is not None and len(frames_in[case["case_id"]]) != len(steps):
            raise bp.BackendParityError("parity_frames_case_mismatch", case["case_id"])
        out_frames: list[bytes] = []
        out_logits: list[np.ndarray] = []
        for number, (token_slice, position) in enumerate(steps):
            decode = number > 0
            started = time.perf_counter()
            if frames_in is None:
                x = ids[:, token_slice]
            else:
                x, _ = _decode_frame(frames_in[case["case_id"]][number])
            pos = position + stage.plan.position_offset + (stage.plan.decode_position_offset if decode else 0)
            out = stage.forward(x, pos)
            computed = time.perf_counter()
            frame_s = 0.0
            if index == n_stages - 1:
                out_logits.append(out)
            else:
                out_frames.append(_encode_frame(out, plan.wire_mode))
                frame_s = time.perf_counter() - computed
            if timings is not None:
                timings.append(
                    {
                        "case_id": case["case_id"],
                        "kind": "decode" if decode else "prefill",
                        "rows": int(token_slice.stop - token_slice.start),
                        "compute_s": computed - started,
                        "frame_s": frame_s,
                    }
                )
        if index == n_stages - 1:
            logits_out[case["case_id"]] = np.stack(out_logits).astype(np.float32)
        else:
            frames_out[case["case_id"]] = out_frames
    return (frames_out, None) if index < n_stages - 1 else (None, logits_out)


def save_frames(path: Path, frames: Mapping[str, list[bytes]], meta: Mapping[str, Any]) -> str:
    arrays: dict[str, np.ndarray] = {}
    for case_id, items in frames.items():
        for number, frame in enumerate(items):
            arrays[f"frame__{case_id}__{number:05d}"] = np.frombuffer(frame, dtype=np.uint8)
    arrays["meta"] = np.frombuffer(json.dumps({"protocol": FRAMES_PROTOCOL, **dict(meta)}, sort_keys=True).encode(), dtype=np.uint8)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        np.savez(handle, **arrays)
    return bp.file_digest(path)


def load_frames(path: Path, corpus: Mapping[str, Any]) -> tuple[dict[str, list[bytes]], dict[str, Any]]:
    with np.load(Path(path), allow_pickle=False) as archive:
        meta = json.loads(bytes(archive["meta"].tobytes()).decode())
        if meta.get("protocol") != FRAMES_PROTOCOL:
            raise bp.BackendParityError("parity_frames_protocol_invalid")
        frames: dict[str, list[bytes]] = {}
        for case in corpus["cases"]:
            count = len(_case_steps(case))
            items = []
            for number in range(count):
                key = f"frame__{case['case_id']}__{number:05d}"
                if key not in archive.files:
                    raise bp.BackendParityError("parity_frames_case_mismatch", case["case_id"])
                items.append(archive[key].tobytes())
            frames[case["case_id"]] = items
    return frames, meta


def capture_from_frames(
    ctx: Context,
    plan: RoutePlan,
    frames_by_boundary: Mapping[int, Mapping[str, list[bytes]]],
    logits_by_case: Mapping[str, np.ndarray],
) -> bp.RouteCapture:
    """Assemble a RouteCapture from per-stage frame files and the final stage's logits."""

    cases: dict[str, bp.CaseCapture] = {}
    for case in ctx.corpus["cases"]:
        case_id = case["case_id"]
        hops, wire = {}, {}
        for boundary, per_case in frames_by_boundary.items():
            decoded = [_decode_frame(frame) for frame in per_case[case_id]]
            hops[boundary] = np.concatenate([array[0] for array, _ in decoded], axis=0)
            wire[boundary] = decoded[0][1]
        cases[case_id] = bp.CaseCapture(case_id, np.asarray(logits_by_case[case_id], dtype=np.float32), hops, wire, 0)
    return bp.RouteCapture(cases=cases, meta={"assembled_from": "stage frames"})


def summarize_timings(timings: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Per-stage speed numbers from :func:`run_stage_frames` timing rows (compute only, no network)."""

    prefill = [t for t in timings if t["kind"] == "prefill"]
    decode = [t for t in timings if t["kind"] == "decode"]
    out: dict[str, Any] = {"steps": len(timings), "prefill_steps": len(prefill), "decode_steps": len(decode)}
    if prefill:
        rows = sum(t["rows"] for t in prefill)
        out["prefill_ms_per_token"] = round(1000 * sum(t["compute_s"] for t in prefill) / rows, 4)
    if decode:
        ordered = sorted(t["compute_s"] for t in decode)
        median = ordered[len(ordered) // 2]
        out["decode_ms_per_token_median"] = round(1000 * median, 4)
        out["decode_ms_per_token_mean"] = round(1000 * sum(ordered) / len(ordered), 4)
        out["decode_tokens_per_second_stage_local"] = round(1.0 / median, 3) if median > 0 else None
        out["frame_encode_ms_per_decode_step_median"] = round(1000 * sorted(t["frame_s"] for t in decode)[len(decode) // 2], 4)
    return out
