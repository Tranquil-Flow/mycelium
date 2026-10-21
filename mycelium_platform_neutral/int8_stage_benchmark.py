# SPDX-License-Identifier: AGPL-3.0-only
"""A9 single-host int8 stage benchmark: reference vs candidate int8 linear provider.

This drives the *same* NumPy Qwen2 functions the live router port executes
(``numpy_runtime._qwen2_embedding``, ``_qwen2_block_with_kv``, ``_rms_norm``,
``_qwen2_linear_checkpointed``, ``mycelium_router.decoding.quantized_greedy_token_id``)
over the whole model on one host, with stage-local KV, and times the spec §3.4
compute components separately: embedding, decoder range, final normalization,
vocabulary head and token selection, for prefill and decode.

The reference mode is the qualified per-call ``int8 -> float32`` path (no
provider installed).  The candidate mode installs one int8 linear provider (the
digest-pinned native kernel).  Windows are paired and alternate
reference/candidate, the corpus is frozen in a manifest before measurement, and
the result is evaluated with the frozen thresholds of
:mod:`mycelium_platform_neutral.runtime_efficiency`.

Claim boundary: a single-host stage pre-qualification.  It is not the A9
product-route gate (M4 Pro -> Windows over the product browser), which the A9
gate driver runs separately.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct
import time
from typing import Any

import numpy as np

import numpy_runtime
from mycelium_router.decoding import quantized_greedy_token_id

PROTOCOL = "mycelium.a9_int8_stage_benchmark.v1"
MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"
MODEL_REVISION = "7ae557604adf67be50417f59c2c2f167def9a775"
REPRESENTATION = "int8-weight-only"
COMPONENTS = ("embedding", "decoder_range", "final_norm", "vocabulary_head", "token_selection")
MAX_SAFETENSORS_HEADER = 100 * 1024 * 1024
#: Two-sided 95% Student t critical values by degrees of freedom.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306,
        9: 2.262, 10: 2.228, 12: 2.179, 15: 2.131, 20: 2.086, 30: 2.042}


class StageBenchmarkError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# -- model loading -----------------------------------------------------------


def qwen2_model_config(config: Mapping[str, Any]) -> dict[str, Any]:
    if config.get("model_type") != "qwen2":
        raise StageBenchmarkError("model_architecture_unsupported")
    hidden = int(config["hidden_size"])
    heads = int(config["num_attention_heads"])
    return {
        "n_layer": int(config["num_hidden_layers"]),
        "n_embd": hidden,
        "n_head": heads,
        "n_kv_head": int(config["num_key_value_heads"]),
        "n_inner": int(config["intermediate_size"]),
        "vocab_size": int(config["vocab_size"]),
        "n_positions": int(config["max_position_embeddings"]),
        "rms_norm_epsilon": float(config["rms_norm_eps"]),
        "rope_theta": float(config["rope_theta"]),
        "head_dim": hidden // heads,
        "activation_function": "silu",
        "tie_word_embeddings": bool(config["tie_word_embeddings"]),
    }


def _decode_tensor(dtype: str, payload: bytes, shape: Sequence[int]) -> np.ndarray:
    if dtype == "BF16":
        raw = np.frombuffer(payload, dtype="<u2").astype(np.uint32) << 16
        return raw.view(np.float32).reshape(shape)
    if dtype == "F16":
        return np.frombuffer(payload, dtype="<f2").astype(np.float32).reshape(shape)
    if dtype == "F32":
        return np.frombuffer(payload, dtype="<f4").astype(np.float32).reshape(shape)
    raise StageBenchmarkError("tensor_dtype_unsupported")


def load_qwen2_int8_model(snapshot: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Load one HF snapshot and quantize every matrix to row-wise int8 at load."""

    snapshot = Path(snapshot)
    if snapshot.name != MODEL_REVISION:
        raise StageBenchmarkError("model_revision_mismatch")
    config = qwen2_model_config(json.loads((snapshot / "config.json").read_text("utf-8")))
    weights = snapshot / "model.safetensors"
    tensors: dict[str, Any] = {}
    digest = hashlib.sha256()
    with weights.open("rb") as stream:
        prefix = stream.read(8)
        digest.update(prefix)
        (length,) = struct.unpack("<Q", prefix)
        if length > MAX_SAFETENSORS_HEADER:
            raise StageBenchmarkError("safetensors_header_oversized")
        header_bytes = stream.read(length)
        digest.update(header_bytes)
        header = json.loads(header_bytes)
        base = 8 + length
        for key, entry in sorted(
            ((k, v) for k, v in header.items() if k != "__metadata__"),
            key=lambda item: item[1]["data_offsets"][0],
        ):
            start, end = entry["data_offsets"]
            stream.seek(base + start)
            payload = stream.read(end - start)
            digest.update(payload)
            value = _decode_tensor(entry["dtype"], payload, entry["shape"])
            tensors[key] = numpy_runtime.quantize_qwen2_numpy_tensor(key, value)
    files = {"model.safetensors": "sha256:" + digest.hexdigest()}
    return tensors, config, files


# -- decoding ------------------------------------------------------------------


def _elapsed(clock: Callable[[], float], start: float) -> float:
    return (clock() - start) * 1000.0


def greedy_decode(
    tensors: Mapping[str, Any],
    config: Mapping[str, Any],
    prompt_ids: Sequence[int],
    max_new_tokens: int,
    *,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    """Greedy decode with stage-local KV; returns tokens and component timings."""

    components = {phase: dict.fromkeys(COMPONENTS, 0.0) for phase in ("prefill", "decode")}
    head = tensors["model.embed_tokens.weight"] if config["tie_word_embeddings"] else tensors["lm_head.weight"]
    kv: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    tokens: list[int] = []
    position = 0
    step_ids = np.asarray([list(prompt_ids)], dtype=np.int64)
    started = clock()
    ttft_ms = 0.0
    decode_step_ms: list[float] = []
    for step in range(max_new_tokens):
        phase = "prefill" if step == 0 else "decode"
        step_start = clock()
        mark = clock()
        hidden = numpy_runtime._qwen2_embedding(tensors["model.embed_tokens.weight"], step_ids)
        components[phase]["embedding"] += _elapsed(clock, mark)
        mark = clock()
        for layer in range(int(config["n_layer"])):
            hidden, kv[layer] = numpy_runtime._qwen2_block_with_kv(
                hidden, tensors, f"model.layers.{layer}.", config, position, kv.get(layer), "qwen2"
            )
        components[phase]["decoder_range"] += _elapsed(clock, mark)
        hidden = hidden[:, -1:, :]
        mark = clock()
        hidden = numpy_runtime._rms_norm(hidden, tensors["model.norm.weight"], float(config["rms_norm_epsilon"]))
        components[phase]["final_norm"] += _elapsed(clock, mark)
        mark = clock()
        logits = numpy_runtime._qwen2_linear_checkpointed(hidden, head, checkpoint=None)
        components[phase]["vocabulary_head"] += _elapsed(clock, mark)
        mark = clock()
        row = np.asarray(logits, dtype=np.float32).reshape(-1)
        if not np.isfinite(row).all():
            raise StageBenchmarkError("nonfinite_logits")
        token = quantized_greedy_token_id(row.tolist())
        components[phase]["token_selection"] += _elapsed(clock, mark)
        tokens.append(int(token))
        position += int(step_ids.shape[1])
        step_ids = np.asarray([[token]], dtype=np.int64)
        if step == 0:
            ttft_ms = _elapsed(clock, started)
        else:
            decode_step_ms.append(_elapsed(clock, step_start))
    kv_layers = len(kv)
    kv.clear()  # request-scoped cleanup: no KV survives the request
    decode_ms = sum(decode_step_ms)
    return {
        "tokens": tokens,
        "ttft_ms": ttft_ms,
        "decode_tokens": len(decode_step_ms),
        "decode_ms": decode_ms,
        "decode_tokens_per_second": (len(decode_step_ms) * 1000.0 / decode_ms) if decode_ms > 0 else 0.0,
        "components_ms": components,
        "kv_layers_used": kv_layers,
        "kv_layers_after_cleanup": len(kv),
    }


# -- frozen manifest and paired benchmark -----------------------------------------


def frozen_manifest(
    *,
    vocab_size: int,
    paired_windows: int = 3,
    prompt_lengths: Sequence[int] = (16, 128),
    output_lengths: Sequence[int] = (16, 48),
    repeats_per_cell: int = 2,
    seed: int = 20261007,
) -> dict[str, Any]:
    """Freeze the corpus before any measurement (deterministic token-id prompts)."""

    rng = np.random.default_rng(seed)
    requests = []
    for prompt_length in prompt_lengths:
        for output_length in output_lengths:
            for repeat in range(repeats_per_cell):
                ids = rng.integers(0, min(vocab_size, 151_000), size=prompt_length).tolist()
                requests.append(
                    {
                        "request_id": f"p{prompt_length}-o{output_length}-r{repeat}",
                        "prompt_ids": [int(value) for value in ids],
                        "max_new_tokens": int(output_length),
                    }
                )
    manifest = {
        "protocol": PROTOCOL,
        "model_id": MODEL_ID,
        "revision": MODEL_REVISION,
        "representation": REPRESENTATION,
        "paired_windows": paired_windows,
        "order": "alternating_reference_first_then_candidate_first",
        "prompt_buckets": len(prompt_lengths),
        "output_buckets": len(output_lengths),
        "requests_per_window": requests,
        "warmup": {"requests": 1, "max_new_tokens": 4},
        "exclusions": "none: failed, cancelled and timed-out requests stay in reliability accounting",
    }
    manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return manifest


def paired_lower_bound(ratios: Sequence[float]) -> float:
    """Two-sided 95% lower bound of the geometric-mean paired ratio."""

    logs = [math.log(value) for value in ratios if value > 0]
    if len(logs) < 2 or len(logs) != len(ratios):
        return 0.0
    mean = statistics.fmean(logs)
    sd = statistics.stdev(logs)
    df = len(logs) - 1
    critical = next((value for key, value in sorted(_T95.items()) if key >= df), 1.96)
    return math.exp(mean - critical * sd / math.sqrt(len(logs)))


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.inf
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _weights_digest(tensors: Mapping[str, Any]) -> str:
    digest = hashlib.sha256()
    for key in sorted(tensors):
        value = tensors[key]
        arrays = (value.values, value.scales) if hasattr(value, "values") else (value,)
        for array in arrays:
            digest.update(key.encode())
            digest.update(np.ascontiguousarray(array).tobytes())
    return "sha256:" + digest.hexdigest()


def run_paired_benchmark(
    tensors: Mapping[str, Any],
    config: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    candidate_provider: Callable[[np.ndarray, np.ndarray, np.ndarray], np.ndarray],
    clock: Callable[[], float] = time.perf_counter,
    log: Callable[[str], None] = lambda _line: None,
) -> dict[str, Any]:
    """Alternate reference/candidate windows over the frozen corpus."""

    if numpy_runtime.installed_int8_linear_provider() is not None:
        raise StageBenchmarkError("provider_already_installed")
    packed_before = _weights_digest(tensors)
    requests = manifest["requests_per_window"]
    modes: dict[str, list[dict[str, Any]]] = {"reference": [], "candidate": []}
    window_rates: dict[str, list[float]] = {"reference": [], "candidate": []}
    failures = {"failures": 0, "cancellations": 0, "timeouts": 0, "excluded": 0}

    def run_mode(mode: str, window: int) -> None:
        numpy_runtime.install_int8_linear_provider(candidate_provider if mode == "candidate" else None)
        try:
            warm = manifest["warmup"]
            greedy_decode(tensors, config, requests[0]["prompt_ids"], warm["max_new_tokens"], clock=clock)
            tokens = 0
            millis = 0.0
            for request in requests:
                try:
                    result = greedy_decode(
                        tensors, config, request["prompt_ids"], request["max_new_tokens"], clock=clock
                    )
                except Exception:  # noqa: BLE001 - counted, never excluded
                    failures["failures"] += 1
                    continue
                result.update(request_id=request["request_id"], window=window, mode=mode)
                modes[mode].append(result)
                tokens += result["decode_tokens"]
                millis += result["decode_ms"]
                log(f"{mode} w{window} {request['request_id']} {result['decode_tokens_per_second']:.2f} tok/s ttft {result['ttft_ms']:.0f} ms")
            window_rates[mode].append(tokens * 1000.0 / millis if millis > 0 else 0.0)
        finally:
            numpy_runtime.install_int8_linear_provider(None)

    for window in range(int(manifest["paired_windows"])):
        order = ("reference", "candidate") if window % 2 == 0 else ("candidate", "reference")
        for mode in order:
            run_mode(mode, window)

    packed_after = _weights_digest(tensors)
    parity_mismatches = []
    reference_tokens = {(r["window"], r["request_id"]): r["tokens"] for r in modes["reference"]}
    for result in modes["candidate"]:
        expected = reference_tokens.get((result["window"], result["request_id"]))
        if expected != result["tokens"]:
            parity_mismatches.append({"window": result["window"], "request_id": result["request_id"]})
    ratios = [
        candidate / reference
        for candidate, reference in zip(window_rates["candidate"], window_rates["reference"])
        if reference > 0
    ]
    candidate_rates = [r["decode_tokens_per_second"] for r in modes["candidate"]]
    reference_rates = [r["decode_tokens_per_second"] for r in modes["reference"]]
    first, last = (window_rates["candidate"][0], window_rates["candidate"][-1]) if window_rates["candidate"] else (0, 0)

    def component_means(mode: str) -> dict[str, dict[str, float]]:
        rows = modes[mode]
        return {
            phase: {
                component: (statistics.fmean(r["components_ms"][phase][component] for r in rows) if rows else 0.0)
                for component in COMPONENTS
            }
            for phase in ("prefill", "decode")
        }

    return {
        "protocol": PROTOCOL,
        "manifest_digest": manifest["manifest_digest"],
        "paired_windows": int(manifest["paired_windows"]),
        "requests_per_mode": {mode: len(rows) for mode, rows in modes.items()},
        "generated_tokens_per_mode": {
            mode: sum(len(r["tokens"]) for r in rows) for mode, rows in modes.items()
        },
        "window_decode_tokens_per_second": window_rates,
        "median_decode_ratio": (statistics.median(candidate_rates) / statistics.median(reference_rates))
        if candidate_rates and reference_rates and statistics.median(reference_rates) > 0 else 0.0,
        "paired_window_ratios": ratios,
        "paired_confidence_lower_bound": paired_lower_bound(ratios),
        "median_decode_tokens_per_second": statistics.median(candidate_rates) if candidate_rates else 0.0,
        "reference_median_decode_tokens_per_second": statistics.median(reference_rates) if reference_rates else 0.0,
        "p95_ttft_ms": _p95([r["ttft_ms"] for r in modes["candidate"]]),
        "reference_p95_ttft_ms": _p95([r["ttft_ms"] for r in modes["reference"]]),
        "token_parity_mismatches": parity_mismatches,
        "reliability": failures,
        "cleanup": {
            "request_scoped_cleanup": all(r["kv_layers_after_cleanup"] == 0 for rows in modes.values() for r in rows),
            "stage_local_kv": all(r["kv_layers_used"] == int(config["n_layer"]) for rows in modes.values() for r in rows),
        },
        "packed_state_unchanged": packed_before == packed_after,
        "candidate_rate_drift": (last / first) if first else 0.0,
        # Component-sum prediction of each candidate request's decode time versus
        # the observed end-to-end decode time, per generated token.
        "component_reconciliation_error_ms": statistics.fmean(
            abs(sum(r["components_ms"]["decode"].values()) - r["decode_ms"]) / r["decode_tokens"]
            for r in modes["candidate"]
            if r["decode_tokens"]
        ) if any(r["decode_tokens"] for r in modes["candidate"]) else 0.0,
        "component_means_ms": {mode: component_means(mode) for mode in modes},
    }


__all__ = [
    "COMPONENTS",
    "MODEL_ID",
    "MODEL_REVISION",
    "PROTOCOL",
    "StageBenchmarkError",
    "frozen_manifest",
    "greedy_decode",
    "load_qwen2_int8_model",
    "paired_lower_bound",
    "qwen2_model_config",
    "run_paired_benchmark",
]
