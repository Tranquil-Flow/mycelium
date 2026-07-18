"""Independent numerical GPT-2 oracle for Mycelium's tiny local fixture.

This module intentionally implements the model directly from config, checkpoint
index, and Safetensors artifacts.  It does not depend on Mycelium execution,
routing, assignment, or qualification code.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

import mlx.core as mx

ABSOLUTE_TOLERANCE = 2e-6
RELATIVE_TOLERANCE = 2e-5
IMPLEMENTATION_VERSION = "tiny-gpt2-mlx-fp32-v1"

_CONFIG_FILE = "config.json"
_INDEX_FILE = "model.safetensors.index.json"
_MAX_JSON_BYTES = 64 * 1024
_MAX_TENSOR_ARTIFACT_BYTES = 4 * 1024 * 1024
_MODEL_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
_EXPECTED_CONFIG: dict[str, Any] = {
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
_LAYER_SUFFIX_SHAPES: dict[str, tuple[int, ...]] = {
    "ln_1.weight": (4,),
    "ln_1.bias": (4,),
    "attn.c_attn.weight": (4, 12),
    "attn.c_attn.bias": (12,),
    "attn.c_proj.weight": (4, 4),
    "attn.c_proj.bias": (4,),
    "ln_2.weight": (4,),
    "ln_2.bias": (4,),
    "mlp.c_fc.weight": (4, 8),
    "mlp.c_fc.bias": (8,),
    "mlp.c_proj.weight": (8, 4),
    "mlp.c_proj.bias": (4,),
}


class OracleValidationError(ValueError):
    """The local fixture or an identity binding is invalid."""


def _fail(message: str) -> NoReturn:
    raise OracleValidationError(message)


def _canonical_bytes(document: Any) -> bytes:
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _reject_constant(value: str) -> NoReturn:
    _fail(f"non-finite JSON number is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            _fail(f"duplicate JSON key: {key}")
        document[key] = value
    return document


def _read_json(path: Path, label: str) -> tuple[dict[str, Any], bytes]:
    try:
        if path.stat().st_size > _MAX_JSON_BYTES:
            _fail(f"{label} exceeds the tiny-fixture size limit")
        raw = path.read_bytes()
    except OSError as exc:
        _fail(f"cannot read {label}: {exc}")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail(f"invalid {label} JSON: {exc}")
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value, raw


def _regular_file(root: Path, name: str, label: str) -> Path:
    if not name or name in {".", ".."} or Path(name).name != name:
        _fail(f"{label} must use a plain local filename")
    path = root / name
    if path.is_symlink() or not path.is_file():
        _fail(f"{label} must be a regular non-symlink file")
    return path


def expected_tensor_shapes() -> dict[str, tuple[int, ...]]:
    """Return the closed tensor-shape contract for the current tiny fixture."""

    shapes: dict[str, tuple[int, ...]] = {
        "transformer.wte.weight": (7, 4),
        "transformer.wpe.weight": (8, 4),
        "transformer.ln_f.weight": (4,),
        "transformer.ln_f.bias": (4,),
        "lm_head.weight": (7, 4),
    }
    for layer in range(2):
        prefix = f"transformer.h.{layer}."
        shapes.update(
            {prefix + suffix: shape for suffix, shape in _LAYER_SUFFIX_SHAPES.items()}
        )
    return shapes


def _validate_config(config: Mapping[str, Any]) -> None:
    if set(config) != set(_EXPECTED_CONFIG):
        _fail("config fields do not match the current tiny GPT-2 fixture")
    for key, expected in _EXPECTED_CONFIG.items():
        if config[key] != expected or type(config[key]) is not type(expected):
            _fail(f"unsupported current fixture config value: {key}")


def _validate_index(index: Mapping[str, Any], expected_names: set[str]) -> dict[str, str]:
    if set(index) != {"metadata", "weight_map"}:
        _fail("checkpoint index fields are invalid")
    metadata = index.get("metadata")
    weight_map = index.get("weight_map")
    if (
        not isinstance(metadata, dict)
        or set(metadata) != {"total_size"}
        or type(metadata["total_size"]) is not int
        or metadata["total_size"] <= 0
    ):
        _fail("checkpoint index metadata is invalid")
    if not isinstance(weight_map, dict) or set(weight_map) != expected_names:
        _fail("checkpoint index tensor names do not match the current fixture")
    normalized: dict[str, str] = {}
    for tensor_name, shard_name in weight_map.items():
        if not isinstance(tensor_name, str) or not isinstance(shard_name, str):
            _fail("checkpoint index tensor mapping must contain strings")
        if (
            not shard_name.endswith(".safetensors")
            or Path(shard_name).name != shard_name
            or shard_name in {".", ".."}
        ):
            _fail("checkpoint index shard reference is invalid")
        normalized[tensor_name] = shard_name
    if len(set(normalized.values())) != 2:
        _fail("current fixture must contain exactly two tensor artifacts")
    return normalized


def _tensor_bytes(tensor: mx.array) -> bytes:
    contiguous = mx.contiguous(tensor)
    mx.eval(contiguous)
    return bytes(contiguous)


def _tensor_value_digest(tensors: Mapping[str, mx.array]) -> str:
    digest = hashlib.sha256()
    for name in sorted(tensors):
        tensor = tensors[name]
        header = {
            "dtype": str(tensor.dtype),
            "name": name,
            "shape": [int(value) for value in tensor.shape],
        }
        encoded = _canonical_bytes(header)
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        raw = _tensor_bytes(tensor)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
    return "sha256:" + digest.hexdigest()


def array_digest(tensor: mx.array) -> str:
    """Hash one activation with explicit dtype and shape framing."""

    header = _canonical_bytes(
        {
            "dtype": str(tensor.dtype),
            "shape": [int(value) for value in tensor.shape],
        }
    )
    raw = _tensor_bytes(tensor)
    return _digest(len(header).to_bytes(8, "big") + header + raw)


def prompt_digest(token_ids: Sequence[int]) -> str:
    tokens = _validate_token_sequence(token_ids, vocab_size=None, positions=None)
    return _digest(_canonical_bytes({"token_ids": list(tokens)}))


@dataclass(frozen=True)
class FixtureIdentity:
    config_digest: str
    checkpoint_index_digest: str
    tensor_artifact_digests: tuple[str, ...]
    tensor_set_digest: str
    tensor_value_digest: str
    model_digest: str


@dataclass(frozen=True)
class ForwardPass:
    logits: mx.array
    layer_hidden_states: tuple[mx.array, ...]
    logits_digest: str
    activation_digests: tuple[str, ...]

    @property
    def greedy_token_id(self) -> int:
        return int(mx.argmax(self.logits[0, -1, :]).item())


@dataclass(frozen=True)
class DecodeStep:
    index: int
    token_id: int
    logits_digest: str
    activation_digests: tuple[str, ...]


@dataclass(frozen=True)
class Generation:
    prompt_digest: str
    generated_token_ids: tuple[int, ...]
    steps: tuple[DecodeStep, ...]


def _validate_token_sequence(
    token_ids: Sequence[int],
    *,
    vocab_size: int | None,
    positions: int | None,
) -> tuple[int, ...]:
    if isinstance(token_ids, (str, bytes)):
        _fail("prompt token IDs must be an integer sequence")
    tokens = tuple(token_ids)
    if not tokens:
        _fail("prompt token IDs must not be empty")
    if any(type(token) is not int or token < 0 for token in tokens):
        _fail("prompt token IDs must be non-negative integers")
    if vocab_size is not None and any(token >= vocab_size for token in tokens):
        _fail("prompt token ID exceeds the current fixture vocabulary")
    if positions is not None and len(tokens) > positions:
        _fail("prompt exceeds the current fixture position limit")
    return tokens


class GPT2FixtureOracle:
    """Direct FP32 GPT-2 implementation bound to one local fixture identity."""

    def __init__(
        self,
        *,
        config: dict[str, Any],
        tensors: dict[str, mx.array],
        identity: FixtureIdentity,
    ) -> None:
        self.config = MappingProxyType(config.copy())
        self._tensors = MappingProxyType(tensors.copy())
        self.identity = identity
        self.tensor_names = tuple(sorted(tensors))

    def _layer_norm(self, hidden: mx.array, prefix: str) -> mx.array:
        mean = mx.mean(hidden, axis=-1, keepdims=True)
        centered = hidden - mean
        variance = mx.mean(mx.square(centered), axis=-1, keepdims=True)
        normalized = centered * mx.rsqrt(
            variance + float(self.config["layer_norm_epsilon"])
        )
        return (
            normalized * self._tensors[prefix + "weight"]
            + self._tensors[prefix + "bias"]
        )

    @staticmethod
    def _gelu_new(hidden: mx.array) -> mx.array:
        coefficient = math.sqrt(2.0 / math.pi)
        return 0.5 * hidden * (
            1.0
            + mx.tanh(
                coefficient * (hidden + 0.044715 * mx.power(hidden, 3))
            )
        )

    def _decoder_layer(self, hidden: mx.array, layer: int) -> mx.array:
        prefix = f"transformer.h.{layer}."
        residual = hidden
        normalized = self._layer_norm(hidden, prefix + "ln_1.")
        qkv = (
            normalized @ self._tensors[prefix + "attn.c_attn.weight"]
            + self._tensors[prefix + "attn.c_attn.bias"]
        )
        query, key, value = mx.split(qkv, 3, axis=-1)
        batch, sequence, embedding = (int(value) for value in query.shape)
        heads = int(self.config["n_head"])
        head_size = embedding // heads

        query = query.reshape(batch, sequence, heads, head_size).transpose(0, 2, 1, 3)
        key = key.reshape(batch, sequence, heads, head_size).transpose(0, 2, 1, 3)
        value = value.reshape(batch, sequence, heads, head_size).transpose(0, 2, 1, 3)
        scores = (query @ key.transpose(0, 1, 3, 2)) / math.sqrt(head_size)
        causal_mask = mx.triu(
            mx.full((sequence, sequence), -mx.inf, dtype=hidden.dtype),
            k=1,
        )
        probabilities = mx.softmax(scores + causal_mask, axis=-1)
        attended = (
            (probabilities @ value)
            .transpose(0, 2, 1, 3)
            .reshape(batch, sequence, embedding)
        )
        hidden = (
            residual
            + attended @ self._tensors[prefix + "attn.c_proj.weight"]
            + self._tensors[prefix + "attn.c_proj.bias"]
        )

        residual = hidden
        normalized = self._layer_norm(hidden, prefix + "ln_2.")
        expanded = (
            normalized @ self._tensors[prefix + "mlp.c_fc.weight"]
            + self._tensors[prefix + "mlp.c_fc.bias"]
        )
        activated = self._gelu_new(expanded)
        return (
            residual
            + activated @ self._tensors[prefix + "mlp.c_proj.weight"]
            + self._tensors[prefix + "mlp.c_proj.bias"]
        )

    def _require_identity(
        self,
        *,
        expected_config_digest: str | None,
        expected_model_digest: str | None,
    ) -> None:
        if (
            expected_config_digest is not None
            and expected_config_digest != self.identity.config_digest
        ):
            _fail("config identity mismatch")
        if (
            expected_model_digest is not None
            and expected_model_digest != self.identity.model_digest
        ):
            _fail("model identity mismatch")

    def forward(self, token_ids: Sequence[int]) -> ForwardPass:
        tokens = _validate_token_sequence(
            token_ids,
            vocab_size=int(self.config["vocab_size"]),
            positions=int(self.config["n_positions"]),
        )
        token_array = mx.array((tokens,), dtype=mx.uint32)
        positions = mx.arange(len(tokens), dtype=mx.uint32)
        hidden = (
            self._tensors["transformer.wte.weight"][token_array]
            + self._tensors["transformer.wpe.weight"][positions][None, :, :]
        )
        layers: list[mx.array] = []
        for layer in range(int(self.config["n_layer"])):
            hidden = self._decoder_layer(hidden, layer)
            layers.append(hidden)
        normalized = self._layer_norm(hidden, "transformer.ln_f.")
        logits = normalized @ self._tensors["lm_head.weight"].T
        mx.eval(logits, *layers)
        activation_digests = tuple(array_digest(value) for value in layers)
        return ForwardPass(
            logits=logits,
            layer_hidden_states=tuple(layers),
            logits_digest=array_digest(logits),
            activation_digests=activation_digests,
        )

    def greedy_decode(
        self,
        prompt_token_ids: Sequence[int],
        *,
        steps: int,
        expected_prompt_digest: str | None = None,
        expected_config_digest: str | None = None,
        expected_model_digest: str | None = None,
    ) -> Generation:
        self._require_identity(
            expected_config_digest=expected_config_digest,
            expected_model_digest=expected_model_digest,
        )
        prompt = _validate_token_sequence(
            prompt_token_ids,
            vocab_size=int(self.config["vocab_size"]),
            positions=int(self.config["n_positions"]),
        )
        if type(steps) is not int or steps <= 0:
            _fail("greedy decode step count must be a positive integer")
        if len(prompt) + steps - 1 > int(self.config["n_positions"]):
            _fail("greedy decode exceeds the current fixture position limit")
        actual_prompt_digest = prompt_digest(prompt)
        if (
            expected_prompt_digest is not None
            and expected_prompt_digest != actual_prompt_digest
        ):
            _fail("prompt identity mismatch")

        context = list(prompt)
        generated: list[int] = []
        evidence: list[DecodeStep] = []
        for index in range(steps):
            result = self.forward(context)
            token_id = result.greedy_token_id
            generated.append(token_id)
            evidence.append(
                DecodeStep(
                    index=index,
                    token_id=token_id,
                    logits_digest=result.logits_digest,
                    activation_digests=result.activation_digests,
                )
            )
            context.append(token_id)
        return Generation(
            prompt_digest=actual_prompt_digest,
            generated_token_ids=tuple(generated),
            steps=tuple(evidence),
        )


def load_gpt2_fixture(
    model_root: str | Path,
    *,
    model_id: str,
    resolved_commit: str,
    expected_config_digest: str | None = None,
    expected_tensor_set_digest: str | None = None,
    expected_model_digest: str | None = None,
) -> GPT2FixtureOracle:
    """Load and identity-bind the exact current locally generated fixture."""

    if not isinstance(model_id, str) or _MODEL_ID_PATTERN.fullmatch(model_id) is None:
        _fail("model identity must include a canonical model ID")
    if (
        not isinstance(resolved_commit, str)
        or _COMMIT_PATTERN.fullmatch(resolved_commit) is None
    ):
        _fail("model identity must include a lowercase hexadecimal resolved commit")

    root = Path(model_root).expanduser()
    if root.is_symlink() or not root.is_dir():
        _fail("model root must be a regular local directory")
    root = root.resolve()
    config_path = _regular_file(root, _CONFIG_FILE, "config artifact")
    index_path = _regular_file(root, _INDEX_FILE, "checkpoint index artifact")
    config, config_raw = _read_json(config_path, "config")
    index, index_raw = _read_json(index_path, "checkpoint index")
    config_digest = _digest(config_raw)
    index_digest = _digest(index_raw)
    if expected_config_digest is not None and expected_config_digest != config_digest:
        _fail("config identity mismatch")
    _validate_config(config)

    shapes = expected_tensor_shapes()
    weight_map = _validate_index(index, set(shapes))
    shard_names = tuple(sorted(set(weight_map.values())))
    shard_paths = {
        name: _regular_file(root, name, "tensor artifact") for name in shard_names
    }
    if any(
        path.stat().st_size > _MAX_TENSOR_ARTIFACT_BYTES
        for path in shard_paths.values()
    ):
        _fail("tensor artifact exceeds the tiny-fixture size limit")
    actual_total_size = sum(path.stat().st_size for path in shard_paths.values())
    if index["metadata"]["total_size"] != actual_total_size:
        _fail("checkpoint index total_size does not match tensor artifacts")
    artifact_digests = tuple(
        _digest(shard_paths[name].read_bytes()) for name in shard_names
    )
    tensor_set_digest = _digest(
        _canonical_bytes(
            {
                "checkpoint_index_digest": index_digest,
                "artifacts": [
                    {"name": name, "digest": digest}
                    for name, digest in zip(shard_names, artifact_digests)
                ],
            }
        )
    )
    if (
        expected_tensor_set_digest is not None
        and expected_tensor_set_digest != tensor_set_digest
    ):
        _fail("tensor identity mismatch")

    tensors: dict[str, mx.array] = {}
    for shard_name in shard_names:
        expected_in_shard = {
            name for name, mapped_shard in weight_map.items() if mapped_shard == shard_name
        }
        try:
            loaded = dict(mx.load(str(shard_paths[shard_name])))
        except Exception as exc:
            _fail(f"cannot parse tensor artifact: {type(exc).__name__}: {exc}")
        if set(loaded) != expected_in_shard:
            _fail("tensor artifact contents do not match checkpoint index")
        for name, tensor in loaded.items():
            if name in tensors:
                _fail("tensor name appears in more than one artifact")
            if tuple(int(value) for value in tensor.shape) != shapes[name]:
                _fail(f"tensor shape mismatch: {name}")
            if tensor.dtype != mx.float32:
                _fail(f"tensor dtype mismatch: {name}")
            mx.eval(tensor)
            if not bool(mx.all(mx.isfinite(tensor)).item()):
                _fail(f"tensor contains non-finite values: {name}")
            tensors[name] = tensor
    if set(tensors) != set(shapes):
        _fail("loaded tensor names do not match the current fixture")

    if tuple(_digest(shard_paths[name].read_bytes()) for name in shard_names) != artifact_digests:
        _fail("tensor artifact changed while it was being loaded")
    tensor_value_digest = _tensor_value_digest(tensors)
    model_digest = _digest(
        _canonical_bytes(
            {
                "config_digest": config_digest,
                "model_id": model_id,
                "resolved_commit": resolved_commit,
                "tensor_set_digest": tensor_set_digest,
                "tensor_value_digest": tensor_value_digest,
            }
        )
    )
    if expected_model_digest is not None and expected_model_digest != model_digest:
        _fail("model identity mismatch")

    identity = FixtureIdentity(
        config_digest=config_digest,
        checkpoint_index_digest=index_digest,
        tensor_artifact_digests=artifact_digests,
        tensor_set_digest=tensor_set_digest,
        tensor_value_digest=tensor_value_digest,
        model_digest=model_digest,
    )
    return GPT2FixtureOracle(config=config, tensors=tensors, identity=identity)
