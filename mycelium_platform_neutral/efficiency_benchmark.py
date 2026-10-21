# SPDX-License-Identifier: AGPL-3.0-only
"""Honest local benchmark harness for the int8 kernels and activation codec.

Run:

    python -m mycelium_platform_neutral.efficiency_benchmark

This harness exists so the source tranche ships with real measured numbers
instead of adjectives.  It is deliberately narrow about what those numbers mean:

* It measures *synthetic random tensors* on this host through the portable
  reference kernel (``numpy``) and the in-process activation frame codec.
* It is **not** the frozen A9 benchmark: no reference/candidate runtime pair, no
  three paired alternating windows, no 24 requests per mode, no prompt/output
  buckets, no thermal envelope, no reliability accounting, no physical device.
* It therefore evaluates **no** frozen threshold (2x median decode rate, paired
  95% lower bound, 8 tokens/s, 1,500 ms p95 TTFT).  ``thresholds_evaluated`` is
  ``False`` and the threshold names are absent from the report, so a reader
  cannot mistake a local microbenchmark for a qualification claim.
* The Windows x86_64 device blocker is named in ``blocker`` and stays open.

Reported per-linear numbers are per-operation rates, not model decode rates.
"""

from __future__ import annotations

import json
import platform
import sys
import time
from typing import Any, Iterable, Sequence

import numpy as np

from .activation_transport import (
    REJECTION_CLASSES,
    ActivationFrameCodec,
    ActivationFrameRejected,
)
from .int8_execution import (
    DEFAULT_WORKSPACE_BYTES,
    KERNEL_HOST_BACKEND,
    Int8KernelUnavailable,
    Int8LinearKernel,
    PerCallFullWeightCastLinear,
    backend_is_correctness_only,
    quantized_reference_linear,
)

#: Label that makes the report's scope unmistakable.
BENCHMARK_KIND = "synthetic_local_numpy_microbenchmark"
#: The A9 accelerated Windows device blocker, carried in every report.
BLOCKER_CODE = "windows_x86_64_provider_not_physically_qualified"
#: Frozen A9 thresholds this harness must NOT evaluate or report as satisfied.
FROZEN_A9_THRESHOLD_KEYS = (
    "median_decode_rate_multiple",
    "paired_confidence_lower_bound",
    "min_decode_tokens_per_second",
    "interactive_p95_ttft_ms",
    "reliability_regression_allowed",
)
_CLAIM_KEYS = (
    "hardware_qualified",
    "qualification_claim",
    "frozen_a9_benchmark",
    "windows_x86_64_qualified",
    "thresholds_evaluated",
)
#: Qwen2.5-0.5B-shaped decoder projections: (out_features, in_features).
DEFAULT_SHAPES = ((4864, 896), (896, 896), (151936, 896))


def _timed(call, repetitions: int) -> tuple[float, Any]:
    best = float("inf")
    result = None
    for _ in range(repetitions):
        started = time.perf_counter()
        result = call()
        best = min(best, time.perf_counter() - started)
    return best, result


def _slab_exactness(
    kernel: Int8LinearKernel, weight: np.ndarray, scales: np.ndarray, activations: np.ndarray
) -> bool:
    """Bitwise exactness of the integer path, checked on one workspace-bounded slab."""

    slab_rows = min(kernel.slab_rows, weight.shape[0])
    observed = kernel.linear(activations)[:, :slab_rows]
    expected = quantized_reference_linear(
        activations, weight[:slab_rows], scales[:slab_rows]
    )
    return bool(np.array_equal(observed, expected))


def _shape_report(
    out_features: int,
    in_features: int,
    *,
    tokens: int,
    workspace_bytes: int,
    repetitions: int,
    seed: int,
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    weight = rng.integers(
        -127, 128, size=(out_features, in_features), dtype=np.int16
    ).astype(np.int8)
    scales = (rng.random(out_features, dtype=np.float64) * 0.01 + 0.001).astype(np.float32)
    activations = (rng.standard_normal((tokens, in_features)) * 3.0).astype(np.float32)

    kernel = Int8LinearKernel(weight, scales, workspace_bytes=workspace_bytes)
    kernel.warmup(activations)
    int8_seconds, observed = _timed(lambda: kernel.linear(activations), repetitions)

    materialized = (weight.astype(np.float32) * scales[:, None])
    reference_seconds, reference = _timed(lambda: activations @ materialized.T, repetitions)

    shortcut = PerCallFullWeightCastLinear(weight, scales)
    shortcut_seconds, _ = _timed(lambda: shortcut.linear(activations), repetitions)
    del materialized

    ledger = kernel.ledger()
    if ledger.full_weight_materializations != 0:
        # A weight that fits one slab is a materializing configuration; report the
        # refusal instead of publishing "zero materializations" for it.
        raise Int8KernelUnavailable("materializing_shape_rejected")
    kernel.assert_warm_decode()
    difference = np.abs(observed - reference)
    scale = float(np.abs(reference).max()) or 1.0
    exact = _slab_exactness(kernel, weight, scales, activations)
    if not exact:
        raise Int8KernelUnavailable("warm_decode_not_proven")
    return {
        "out_features": out_features,
        "in_features": in_features,
        "tokens": tokens,
        "macs": int(out_features) * int(in_features) * int(tokens),
        "int8_seconds": round(int8_seconds, 6),
        "float32_reference_seconds": round(reference_seconds, 6),
        "per_call_full_weight_cast_seconds": round(shortcut_seconds, 6),
        "int8_tokens_per_second": round(tokens / int8_seconds, 3),
        "reference_tokens_per_second": round(tokens / reference_seconds, 3),
        "speedup_vs_float32_reference": round(reference_seconds / int8_seconds, 4),
        "speedup_vs_per_call_cast": round(shortcut_seconds / int8_seconds, 4),
        "max_abs_error": float(difference.max()),
        "mean_abs_error": float(difference.mean()),
        "relative_error": float(difference.max() / scale),
        "integer_accumulation_exact": exact,
        "full_weight_materializations": ledger.full_weight_materializations,
        "bounded_slab_conversions": ledger.bounded_slab_conversions,
        "packed_state_bytes": kernel.packed_state_bytes,
        "whole_weight_float32_bytes": int(weight.size * 4),
        "peak_weight_workspace_bytes": ledger.peak_weight_workspace_bytes,
        "peak_activation_workspace_bytes": ledger.peak_activation_workspace_bytes,
        "rejected_shortcut_peak_bytes": shortcut.ledger().peak_weight_workspace_bytes,
        "rejected_shortcut_full_weight_materializations": (
            shortcut.ledger().full_weight_materializations
        ),
    }


def _codec_report(
    *, tokens: int, hidden_size: int, repetitions: int, seed: int
) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    values = (rng.standard_normal((1, tokens, hidden_size)) * 3.0).astype(np.float32)
    codec = ActivationFrameCodec(dtype="float16", generation=1)
    frame = codec.encode(values, frame_id="benchmark-frame")
    payload_bytes = len(frame.split(b"\n", 1)[1])

    def _decode_once() -> np.ndarray:
        # A fresh receiver per attempt: the replay fence is real, so a second
        # decode of the same frame by the same codec is refused by design.
        return ActivationFrameCodec(dtype="float16", generation=1).decode(
            frame, expect_shape=values.shape, expect_dtype="float16"
        )

    float16_seconds, decoded = _timed(
        _decode_once, repetitions
    )
    encode_seconds, _ = _timed(
        lambda: ActivationFrameCodec(dtype="float16", generation=1).encode(
            values, frame_id="benchmark-frame-2"
        ),
        repetitions,
    )
    battery = codec.negative_battery(values)
    reduction = codec.byte_reduction()
    comparison = values.astype(np.float16).astype(np.float32)
    exact_16 = bool(np.array_equal(decoded, comparison))

    bf16_codec = ActivationFrameCodec(dtype="bfloat16", generation=1)
    bf16_frame = bf16_codec.encode(values, frame_id="benchmark-bf16")
    bf16_decoded = bf16_codec.decode(
        bf16_frame, expect_shape=values.shape, expect_dtype="bfloat16"
    )
    bf16_error = np.abs(bf16_decoded - values)
    bf16_scale = float(np.abs(values).max()) or 1.0

    return {
        "dtype": "float16",
        "float32_bytes": int(values.size * 4),
        "float16_bytes": payload_bytes,
        "bytes_before": reduction["bytes_before"],
        "bytes_after": reduction["bytes_after"],
        "byte_reduction_ratio": round(
            reduction["bytes_before"] / reduction["bytes_after"], 6
        ),
        "counter_digest": reduction["counter_digest"],
        "samples": reduction["samples"],
        "encode_mb_per_second": round(
            (values.size * 4 / (1 << 20)) / encode_seconds, 3
        ),
        "decode_mb_per_second": round((payload_bytes / (1 << 20)) / float16_seconds, 3),
        "decode_seconds": round(float16_seconds, 6),
        "float16_round_trip_exact": exact_16,
        "float16_round_trip_max_abs_error": float(np.abs(decoded - values).max()),
        "bfloat16_round_trip_relative_error": float(bf16_error.max() / bf16_scale),
        "bfloat16_relative_error_bound": 2 ** -8,
        "rejections_observed": len(battery),
        "rejection_classes": sorted(REJECTION_CLASSES),
        "negative_digests": battery,
        "privacy_reduced_projection": codec.projection(),
    }


def run_benchmark(
    *,
    iterations: int = 3,
    shapes: Sequence[tuple[int, int]] = DEFAULT_SHAPES,
    tokens: int = 8,
    workspace_bytes: int = DEFAULT_WORKSPACE_BYTES,
    seed: int = 20260926,
) -> dict[str, Any]:
    """Measure the reference kernel and codec; never emit a qualification claim."""

    if not shapes:
        raise Int8KernelUnavailable("workspace_budget_invalid")
    per_shape = [
        _shape_report(
            int(out_features),
            int(in_features),
            tokens=tokens,
            workspace_bytes=workspace_bytes,
            repetitions=max(1, int(iterations)),
            seed=seed,
        )
        for out_features, in_features in shapes
    ]
    widest = max(per_shape, key=lambda item: item["macs"])
    codec = _codec_report(
        tokens=tokens, hidden_size=shapes[-1][1], repetitions=max(1, int(iterations)), seed=seed
    )
    total_int8 = sum(item["int8_seconds"] for item in per_shape)
    total_reference = sum(item["float32_reference_seconds"] for item in per_shape)
    portable_faster = total_int8 < total_reference
    report: dict[str, Any] = {
        "benchmark_kind": BENCHMARK_KIND,
        "qualification_claim": False,
        "hardware_qualified": False,
        "frozen_a9_benchmark": False,
        "windows_x86_64_qualified": False,
        "thresholds_evaluated": False,
        "reliability_accounting": None,
        "blocker": BLOCKER_CODE,
        "kernel_host_backend": KERNEL_HOST_BACKEND,
        "correctness_only_host": backend_is_correctness_only(KERNEL_HOST_BACKEND),
        "accelerated": False,
        "scope_note": (
            "per-linear synthetic microbenchmark on a portable reference host; "
            "not model decode rate, not the frozen A9 benchmark, not a physical "
            "qualification, and no frozen threshold is evaluated"
        ),
        "host": {
            "platform": platform.system(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "numpy": np.__version__,
        },
        "iterations": max(1, int(iterations)),
        "tokens_per_call": int(tokens),
        "workspace_bytes": int(workspace_bytes),
        "seed": int(seed),
        "int8_linear": {
            "aggregate_of": "widest_projection",
            "full_weight_materializations": widest["full_weight_materializations"],
            "bounded_slab_conversions": widest["bounded_slab_conversions"],
            "packed_state_bytes": widest["packed_state_bytes"],
            "whole_weight_float32_bytes": widest["whole_weight_float32_bytes"],
            "peak_workspace_bytes": widest["peak_weight_workspace_bytes"],
            "decode_token_rate_per_second": widest["int8_tokens_per_second"],
            "reference_token_rate_per_second": widest["reference_tokens_per_second"],
            "decode_speedup_vs_reference": widest["speedup_vs_float32_reference"],
            "max_abs_error": widest["max_abs_error"],
            "mean_abs_error": widest["mean_abs_error"],
            "relative_error": widest["relative_error"],
            "integer_accumulation_exact": widest["integer_accumulation_exact"],
        },
        "per_shape": per_shape,
        "totals": {
            "shapes": len(per_shape),
            "macs": sum(item["macs"] for item in per_shape),
            "int8_seconds": round(total_int8, 6),
            "float32_reference_seconds": round(total_reference, 6),
            "aggregate_speedup_vs_reference": round(total_reference / total_int8, 4),
        },
        "portable_path_faster_than_blas": portable_faster,
        "interpretation": (
            "the portable integer path accumulates int8 products in int32 with a "
            "bounded slab workspace and no native int8 GEMM; on a host whose "
            "float32 path is BLAS-accelerated it is expected to be slower than "
            "the float32 reference, which is exactly why the frozen "
            "accelerated_windows_backend decision requires a physically qualified "
            "native provider: no portable host can produce an acceleration "
            "qualification.  A speedup above 1.0 here would not qualify hardware "
            "either."
            if not portable_faster
            else "the portable integer path beat the float32 reference on this "
            "host; that is a scheduling and memory-traffic observation, not an "
            "acceleration qualification."
        ),
        "activation_codec": codec,
    }
    assert_no_qualification_claim(report)
    return report


def assert_no_qualification_claim(report: dict[str, Any]) -> None:
    """Refuse any report that reads as a qualification or threshold claim."""

    for key in _CLAIM_KEYS:
        if report.get(key):
            raise Int8KernelUnavailable("benchmark_must_not_claim_qualification")
    if report.get("blocker") != BLOCKER_CODE:
        raise Int8KernelUnavailable("benchmark_must_not_claim_qualification")
    for key in FROZEN_A9_THRESHOLD_KEYS:
        if key in report:
            raise Int8KernelUnavailable("benchmark_must_not_claim_qualification")
        if key in report.get("int8_linear", {}):
            raise Int8KernelUnavailable("benchmark_must_not_claim_qualification")


def _strip_unserialisable(report: dict[str, Any]) -> dict[str, Any]:
    voided: dict[str, Any] = {}
    for key, value in report.items():
        if isinstance(value, dict):
            voided[key] = _strip_unserialisable(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            voided[key] = value
        elif isinstance(value, (list, tuple)):
            voided[key] = list(value)
        else:
            raise ActivationFrameRejected("battery_incomplete")
    return voided


def main(argv: Iterable[str] | None = None) -> int:
    report = run_benchmark()
    print(json.dumps(_strip_unserialisable(report), indent=2, sort_keys=True))
    print(
        "UNQUALIFIED LOCAL MICROBENCHMARK — not the frozen A9 benchmark; "
        f"blocker: {BLOCKER_CODE}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(main())
