# SPDX-License-Identifier: AGPL-3.0-only
"""A11 native desktop draft runtime: a fast MLX Qwen2 forward that owns draft KV only.

The draft has no parity duty.  The target verifier decides every committed token
(specification section 5), so a draft only has to propose well and quickly.  For that
reason this runtime uses MLX's fused kernels (``mx.fast.rms_norm``, ``mx.fast.rope``,
``mx.fast.scaled_dot_product_attention``, optional ``mx.quantized_matmul``) instead of
the row-stable product stage kernels, which are engineered for cross-runtime parity
and cost an order of magnitude more per token.

Authority boundary:

* the draft owns its own request-local KV (``DraftKVCache``) and its uncommitted
  proposal, nothing else -- it never sees target KV and cannot emit output;
* ``commit_verified_prefix`` accepts only a target-committed sequence that starts
  with the draft's own proposal prefix; anything else is reported as not
  reproducible (``False``) and the caller falls back to target-only decode;
* the draft identity binds the exact config, weights (or verified artifact
  digests) and representation, so a plan can name exactly which draft ran.

KV convention (the same as the A10 target verifier): for a committed context of
``P`` tokens the cache holds positions ``0..P-2`` and token ``P-1`` is pending; a
proposal feeds the pending token first.

Claim boundary: an in-process draft runtime.  It is not a qualification, a
performance claim or an authorization to load any particular model; callers bind
the exact local artifact the owner authorised.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import mlx.core as mx

from mycelium_speculative_decode_execution import DraftProposal

PROTOCOL = "mycelium.a11_draft_runtime.v1"
SUPPORTED_ARCHITECTURES = frozenset({"qwen2"})
MAXIMUM_PROPOSAL_WIDTH = 16


class DraftRuntimeError(ValueError):
    """A coded, fail-closed draft-runtime refusal."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _reject(code: str, detail: str = "") -> None:
    raise DraftRuntimeError(code, detail)


def _canonical_digest(document: Any) -> str:
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


class DraftKVCache:
    """Per-layer key/value arrays of one draft request, truncatable."""

    def __init__(self, layer_count: int) -> None:
        self._layers: list[tuple[mx.array, mx.array] | None] = [None] * layer_count

    @property
    def length(self) -> int:
        first = self._layers[0] if self._layers else None
        return 0 if first is None else int(first[0].shape[2])

    def layer(self, index: int) -> tuple[mx.array, mx.array] | None:
        return self._layers[index]

    def store(self, index: int, keys: mx.array, values: mx.array) -> None:
        self._layers[index] = (keys, values)

    def truncate(self, length: int) -> None:
        if length < 0:
            _reject("invalid_draft_kv_length")
        if length >= self.length:
            return
        if length == 0:
            self._layers = [None] * len(self._layers)
            return
        self._layers = [
            None if item is None else (item[0][:, :, :length, :], item[1][:, :, :length, :])
            for item in self._layers
        ]

    def clear(self) -> None:
        self._layers = [None] * len(self._layers)


class Qwen2DraftModel:
    """A Qwen2 causal LM forward over MLX fused kernels (draft role only)."""

    def __init__(
        self,
        config: Mapping[str, Any],
        weights: Mapping[str, mx.array],
        *,
        quantize_bits: int | None = 8,
        group_size: int = 64,
        dtype: Any = None,
        weights_digest: str | None = None,
    ) -> None:
        if config.get("model_type") not in SUPPORTED_ARCHITECTURES:
            _reject("unsupported_draft_architecture", str(config.get("model_type")))
        if bool(config.get("use_sliding_window", False)):
            # An enabled sliding window changes effective attention; a disabled one
            # (Qwen2.5 default) is recorded but not a semantic difference.
            _reject("unsupported_draft_position_semantics")
        if quantize_bits not in (None, 4, 8):
            _reject("unsupported_draft_quantization")
        self.config = dict(config)
        self.hidden = int(config["hidden_size"])
        self.heads = int(config["num_attention_heads"])
        self.kv_heads = int(config["num_key_value_heads"])
        self.head_dim = self.hidden // self.heads
        self.layers = int(config["num_hidden_layers"])
        self.vocab_size = int(config["vocab_size"])
        self.eps = float(config["rms_norm_eps"])
        self.rope_theta = float(config["rope_theta"])
        self.scale = 1.0 / math.sqrt(self.head_dim)
        self.quantize_bits = quantize_bits
        self.group_size = int(group_size)
        embed = weights["model.embed_tokens.weight"]
        self.dtype = dtype if dtype is not None else embed.dtype
        self._identity = self._identity_document(weights, weights_digest)

        def cast(array: mx.array) -> mx.array:
            return array.astype(self.dtype)

        self._embed = cast(embed)
        self._norm = cast(weights["model.norm.weight"])
        head = weights.get("lm_head.weight")
        if head is None:
            if not bool(config.get("tie_word_embeddings", False)):
                _reject("draft_lm_head_missing")
            head = embed
        self._head = self._linear(cast(head))
        self._blocks: list[dict[str, Any]] = []
        for layer in range(self.layers):
            prefix = f"model.layers.{layer}."
            block = {
                "input_norm": cast(weights[prefix + "input_layernorm.weight"]),
                "post_norm": cast(weights[prefix + "post_attention_layernorm.weight"]),
            }
            for name in ("q_proj", "k_proj", "v_proj", "o_proj"):
                block[name] = self._linear(cast(weights[f"{prefix}self_attn.{name}.weight"]))
            for name in ("q_proj", "k_proj", "v_proj"):
                bias = weights.get(f"{prefix}self_attn.{name}.bias")
                block[name + "_bias"] = None if bias is None else cast(bias)
            for name in ("gate_proj", "up_proj", "down_proj"):
                block[name] = self._linear(cast(weights[f"{prefix}mlp.{name}.weight"]))
            self._blocks.append(block)
        mx.eval(self._embed, self._norm, self._head, self._blocks)

    # -- construction ---------------------------------------------------------
    @classmethod
    def from_snapshot(
        cls,
        root: str | Path,
        *,
        expected_sha256: Mapping[str, str] | None = None,
        quantize_bits: int | None = 8,
        group_size: int = 64,
        dtype: Any = mx.float16,
    ) -> "Qwen2DraftModel":
        """Load an already-local Safetensors snapshot (no network, no conversion on disk).

        ``expected_sha256`` (file name -> hex digest) is the owner-authorised identity;
        a mismatch refuses the load.  The resolved file digests bind the identity.
        """

        base = Path(root)
        if not (base / "config.json").is_file():
            _reject("draft_artifact_missing", "config.json")
        config = json.loads((base / "config.json").read_text())
        index = base / "model.safetensors.index.json"
        if index.is_file():
            files = sorted(set(json.loads(index.read_text())["weight_map"].values()))
        else:
            files = ["model.safetensors"]
        digests: dict[str, str] = {}
        weights: dict[str, mx.array] = {}
        for name in files:
            path = base / name
            if not path.is_file():
                _reject("draft_artifact_missing", name)
            digests[name] = _sha256_file(path)
            if expected_sha256 is not None and expected_sha256.get(name) != digests[name]:
                _reject("draft_artifact_digest_mismatch", name)
            weights.update(mx.load(str(path)))
        return cls(
            config,
            weights,
            quantize_bits=quantize_bits,
            group_size=group_size,
            dtype=dtype,
            weights_digest=_canonical_digest({"files": digests}),
        )

    def _linear(self, weight: mx.array) -> Any:
        if self.quantize_bits is None:
            return weight
        quantized, scales, biases = mx.quantize(weight, group_size=self.group_size, bits=self.quantize_bits)
        return (quantized, scales, biases)

    def _matmul(self, hidden: mx.array, weight: Any) -> mx.array:
        if isinstance(weight, tuple):
            quantized, scales, biases = weight
            return mx.quantized_matmul(
                hidden, quantized, scales, biases, transpose=True,
                group_size=self.group_size, bits=self.quantize_bits,
            )
        return hidden @ weight.T

    def _identity_document(self, weights: Mapping[str, mx.array], weights_digest: str | None) -> dict[str, Any]:
        if weights_digest is None:
            tensors = []
            for name in sorted(weights):
                array = weights[name]
                mx.eval(array)
                tensors.append([name, list(array.shape), str(array.dtype), hashlib.sha256(memoryview(array)).hexdigest()])
            weights_digest = _canonical_digest({"tensors": tensors})
        return {
            "protocol": PROTOCOL,
            "config": {key: self.config[key] for key in sorted(self.config) if not isinstance(self.config[key], (dict, list))},
            "weights": weights_digest,
            "representation": {
                "quantize_bits": self.quantize_bits,
                "group_size": self.group_size if self.quantize_bits else None,
                "compute_dtype": str(self.dtype),
            },
        }

    def identity_digest(self) -> str:
        return _canonical_digest(self._identity)

    def identity(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._identity))

    # -- forward ----------------------------------------------------------------
    def forward(self, tokens: Sequence[int], cache: DraftKVCache) -> mx.array:
        """Feed ``tokens`` after the cached prefix; return logits for every fed position."""

        if not tokens:
            _reject("empty_draft_tokens")
        offset = cache.length
        count = len(tokens)
        hidden = self._embed[mx.array([list(tokens)], dtype=mx.uint32)]
        mask: Any = None
        if count > 1:
            rows = mx.arange(count)[:, None] + offset
            cols = mx.arange(offset + count)[None, :]
            mask = mx.where(cols <= rows, 0.0, -math.inf).astype(self.dtype)
        for index, block in enumerate(self._blocks):
            normed = mx.fast.rms_norm(hidden, block["input_norm"], self.eps)
            queries = self._matmul(normed, block["q_proj"])
            keys = self._matmul(normed, block["k_proj"])
            values = self._matmul(normed, block["v_proj"])
            if block["q_proj_bias"] is not None:
                queries = queries + block["q_proj_bias"]
                keys = keys + block["k_proj_bias"]
                values = values + block["v_proj_bias"]
            queries = queries.reshape(1, count, self.heads, self.head_dim).transpose(0, 2, 1, 3)
            keys = keys.reshape(1, count, self.kv_heads, self.head_dim).transpose(0, 2, 1, 3)
            values = values.reshape(1, count, self.kv_heads, self.head_dim).transpose(0, 2, 1, 3)
            queries = mx.fast.rope(queries, self.head_dim, traditional=False, base=self.rope_theta, scale=1.0, offset=offset)
            keys = mx.fast.rope(keys, self.head_dim, traditional=False, base=self.rope_theta, scale=1.0, offset=offset)
            past = cache.layer(index)
            if past is not None:
                keys = mx.concatenate([past[0], keys], axis=2)
                values = mx.concatenate([past[1], values], axis=2)
            cache.store(index, keys, values)
            attended = mx.fast.scaled_dot_product_attention(queries, keys, values, scale=self.scale, mask=mask)
            attended = attended.transpose(0, 2, 1, 3).reshape(1, count, self.hidden)
            hidden = hidden + self._matmul(attended, block["o_proj"])
            normed = mx.fast.rms_norm(hidden, block["post_norm"], self.eps)
            gate = self._matmul(normed, block["gate_proj"])
            up = self._matmul(normed, block["up_proj"])
            hidden = hidden + self._matmul(nn_silu(gate) * up, block["down_proj"])
        hidden = mx.fast.rms_norm(hidden, self._norm, self.eps)
        return self._matmul(hidden, self._head)

    def next_token(self, tokens: Sequence[int], cache: DraftKVCache) -> int:
        logits = self.forward(tokens, cache)
        token = mx.argmax(logits[0, -1, :])
        return int(token.item())

    def full_context_logits(self, tokens: Sequence[int]) -> mx.array:
        """Independent authority for tests: one forward over the whole context."""

        return self.forward(tokens, DraftKVCache(self.layers))


def nn_silu(value: mx.array) -> mx.array:
    return value * mx.sigmoid(value)


class MLXDraftRuntime:
    """``DraftRuntimePort`` over a ``Qwen2DraftModel``: one request's draft KV."""

    def __init__(
        self,
        model: Qwen2DraftModel,
        *,
        incarnation: str,
        kv_ref: str = "draft-kv",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(model, Qwen2DraftModel):
            _reject("invalid_draft_model")
        if not isinstance(incarnation, str) or not incarnation.startswith("sha256:") or len(incarnation) != 71:
            _reject("invalid_runtime_incarnation")
        self._model = model
        self._incarnation = incarnation
        self._kv_ref = kv_ref
        self._clock = clock
        self._cache = DraftKVCache(model.layers)
        self._context: tuple[int, ...] = ()
        self._proposal: tuple[int, ...] = ()
        self._fed = 0
        self._lost = False
        self._proposals = 0
        self._rolled_back = 0
        self._resets = 0
        self._committed_rounds = 0

    @property
    def draft_runtime_incarnation(self) -> str:
        return self._incarnation

    @property
    def model(self) -> Qwen2DraftModel:
        return self._model

    def mark_lost(self) -> None:
        self._lost = True

    def close(self) -> None:
        """Release only this request's draft KV; never mutate the target cache."""
        self._cache.clear()
        self._context = ()
        self._proposal = ()
        self._fed = 0

    def committed_context(self) -> tuple[int, ...]:
        return self._context

    # -- context ----------------------------------------------------------------
    def sync(self, context: Sequence[int]) -> None:
        """Make the draft's committed context exactly ``context`` (target-committed)."""

        tokens = tuple(int(token) for token in context)
        if not tokens:
            _reject("empty_draft_context")
        if any(token < 0 or token >= self._model.vocab_size for token in tokens):
            _reject("draft_token_bounds_exceeded")
        self._discard_tentative()
        if tokens[: len(self._context)] != self._context or len(tokens) < len(self._context):
            self._cache.clear()
            self._resets += 1
        self._context = tokens
        self._catch_up()

    def _catch_up(self) -> None:
        missing = self._context[self._cache.length : len(self._context) - 1]
        if missing:
            logits = self._model.forward(missing, self._cache)
            mx.eval(logits)

    def _discard_tentative(self) -> None:
        if self._context:
            self._cache.truncate(min(self._cache.length, len(self._context) - 1))
        self._proposal = ()
        self._fed = 0

    # -- DraftRuntimePort ---------------------------------------------------------
    def propose(
        self, *, prefix_position: int, prefix_digest: str, width: int, deadline_ms: float
    ) -> DraftProposal:
        self._proposals += 1
        if self._lost:
            return DraftProposal(lost=True)
        if not self._context:
            _reject("draft_context_not_synced")
        if prefix_position != len(self._context):
            _reject("draft_prefix_position_mismatch", f"{prefix_position}!={len(self._context)}")
        if isinstance(width, bool) or not isinstance(width, int) or width < 1 or width > MAXIMUM_PROPOSAL_WIDTH:
            _reject("invalid_draft_width")
        self._discard_tentative()
        self._catch_up()
        started = self._clock()
        previous = started
        tokens: list[int] = []
        feed = self._context[-1]
        budget_s = float(deadline_ms) / 1000.0
        elapsed = 0.0
        for _ in range(width):
            token = self._model.next_token((feed,), self._cache)
            self._fed += 1
            now = self._clock()
            if now - started > budget_s:
                break
            elapsed = now - started
            tokens.append(token)
            feed = token
            step = now - previous
            previous = now
            if elapsed + step > budget_s:
                break
        if not tokens:
            self._discard_tentative()
            return DraftProposal(unavailable=True, draft_compute_ms=round(elapsed * 1000.0, 6))
        self._proposal = tuple(tokens)
        return DraftProposal(
            tokens=self._proposal,
            draft_queue_ms=0.0,
            draft_compute_ms=round(elapsed * 1000.0, 6),
        )

    def commit_verified_prefix(self, *, tokens: Sequence[int], correction_token: int | None) -> bool:
        accepted = tuple(int(token) for token in tokens)
        if len(accepted) > len(self._proposal) or self._proposal[: len(accepted)] != accepted:
            self._discard_tentative()
            return False
        committed = self._context + accepted
        if correction_token is not None:
            committed = committed + (int(correction_token),)
        keep = min(self._cache.length, len(committed) - 1)
        self._cache.truncate(keep)
        self._context = committed
        self._proposal = ()
        self._fed = 0
        self._committed_rounds += 1
        return True

    def rollback_suffix(self, count: int) -> None:
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            _reject("invalid_rollback_count")
        floor = max(0, len(self._context) - 1)
        self._cache.truncate(max(floor, self._cache.length - count))
        self._rolled_back += count
        self._proposal = ()
        self._fed = 0

    def snapshot(self) -> dict[str, Any]:
        """Privacy-reduced counters: no token ids, no logits, no KV bytes."""

        return {
            "protocol": PROTOCOL,
            "role": "draft",
            "kv_ref": self._kv_ref,
            "draft_runtime_incarnation": self._incarnation,
            "draft_identity_digest": self._model.identity_digest(),
            "committed_tokens": len(self._context),
            "kv_positions": self._cache.length,
            "tentative_tokens": len(self._proposal),
            "proposals": self._proposals,
            "committed_rounds": self._committed_rounds,
            "rolled_back_positions": self._rolled_back,
            "resets": self._resets,
            "lost": self._lost,
            "wrote_target_state": False,
        }


__all__ = [
    "DraftKVCache",
    "DraftRuntimeError",
    "MLXDraftRuntime",
    "PROTOCOL",
    "Qwen2DraftModel",
]
