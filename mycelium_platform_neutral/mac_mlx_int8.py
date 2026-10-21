# SPDX-License-Identifier: AGPL-3.0-only
"""Apple MLX per-row INT8 linear provider and the Mac stage-benchmark publisher.

The provider is the Mac counterpart of ``native_int8.NativeInt8LinearProvider``:
a ``numpy_runtime`` int8 linear provider.  Row-wise int8 weights (value ``q``,
row scale ``s``) are repacked once into MLX's 8-bit affine format
(``w = s * u + b`` with ``u = q + 128`` and ``b = -128 * s``), so a decode step
runs ``quantized_matmul`` and never builds a float copy of a weight matrix.  A
repack after :meth:`mark_warm` is counted as a full-weight materialization, and
the frozen A9 decision blocks it.  Nothing here claims acceleration: only the
measured paired benchmark through ``decide_efficiency`` can.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

from .seed_migration import observe_host_platform

BACKEND_ID = "mac_mlx_int8_provider"
DEVICE_CLASS = "apple_silicon_mac"
PROTOCOL = "mycelium.mac_mlx_int8_provider.v1"
_GROUP_SIZES = (128, 64, 32)


class MacMlxUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _load_mlx() -> Any:
    try:
        import mlx.core as mx  # noqa: PLC0415 - optional, Apple-only dependency
    except ImportError as exc:
        raise MacMlxUnavailable("mac_mlx_unavailable") from exc
    return mx


def is_apple_silicon() -> bool:
    return observe_host_platform() == ("macos", "arm64")


class MacMlxInt8LinearProvider:
    def __init__(self) -> None:
        self._mx = _load_mlx()
        self._packed: dict[tuple[int, int, int, int], tuple[Any, Any, Any, int, Any, Any]] = {}
        self._warm = False
        self.calls = 0
        self.packed_weight_conversions = 0
        self.full_weight_materializations = 0
        try:
            version = importlib.metadata.version("mlx")
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        self.mlx_version = version
        self.build_digest = "sha256:" + hashlib.sha256(json.dumps(
            {"backend": BACKEND_ID, "mlx": version, "group_sizes": _GROUP_SIZES, "bits": 8},
            sort_keys=True).encode()).hexdigest()

    def mark_warm(self) -> None:
        self._warm = True

    def _pack(self, values: np.ndarray, scales: np.ndarray) -> tuple[Any, Any, Any, int, Any, Any]:
        out_features, in_features = values.shape
        group = next((size for size in _GROUP_SIZES if in_features % size == 0), 0)
        if not group or in_features % 4:
            raise MacMlxUnavailable("mac_mlx_shape_unsupported")
        mx = self._mx
        unsigned = (values.astype(np.int16) + 128).astype(np.uint8)
        words = np.ascontiguousarray(unsigned.reshape(out_features, in_features // 4, 4)).view("<u4")
        words = words.reshape(out_features, in_features // 4)
        row_scales = np.repeat(scales.astype(np.float32)[:, None], in_features // group, axis=1)
        packed = mx.array(words)
        scale_array = mx.array(row_scales)
        bias_array = mx.array(-128.0 * row_scales)
        mx.eval(packed, scale_array, bias_array)
        # Hold the numpy operands so a recycled address cannot alias a cache entry.
        return packed, scale_array, bias_array, group, values, scales

    def __call__(self, hidden: np.ndarray, values: np.ndarray, scales: np.ndarray) -> np.ndarray:
        if (not isinstance(values, np.ndarray) or values.dtype != np.int8 or values.ndim != 2
                or not isinstance(scales, np.ndarray) or scales.shape != (values.shape[0],)
                or not isinstance(hidden, np.ndarray) or hidden.dtype != np.float32
                or hidden.ndim != 3 or hidden.shape[-1] != values.shape[1]):
            raise MacMlxUnavailable("mac_mlx_operand_invalid")
        key = (values.__array_interface__["data"][0], scales.__array_interface__["data"][0],
               values.shape[0], values.shape[1])
        entry = self._packed.get(key)
        if entry is None or entry[4] is not values or entry[5] is not scales:
            entry = self._pack(values, scales)
            self._packed[key] = entry
            self.packed_weight_conversions += 1
            if self._warm:
                self.full_weight_materializations += 1
        packed, scale_array, bias_array, group, _v, _s = entry
        mx = self._mx
        result = mx.quantized_matmul(mx.array(np.ascontiguousarray(hidden)), packed, scale_array,
                                     bias_array, transpose=True, group_size=group, bits=8)
        mx.eval(result)
        self.calls += 1
        return np.array(result, dtype=np.float32)

    def report(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL, "backend_id": BACKEND_ID, "build_digest": self.build_digest,
            "mlx_version": self.mlx_version, "calls": self.calls,
            "packed_weight_conversions": self.packed_weight_conversions,
            "full_weight_materializations": self.full_weight_materializations,
            "accelerated": False, "qualified": False,
            "claim_boundary": ("a bound MLX kernel path and counters; acceleration and "
                               "qualification are decided only by the measured A9 benchmark"),
        }


def run_mac_stage_benchmark(
    *, snapshot: Path, profile_digest: str, thermal_state: str | None, paired_windows: int,
    output_lengths: tuple[int, int], prompt_lengths: tuple[int, int], repeats_per_cell: int,
    conformance_host: bool, evidence_generation: int = 1, log_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(manifest, receipt_fields)``; the caller writes both receipts."""

    from . import int8_stage_benchmark as bench, thermal as thermal_module
    from .primitives import record_digest
    from .runtime_efficiency import THRESHOLDS, decide_efficiency
    import numpy_runtime

    mac = is_apple_silicon()
    if not mac and not conformance_host:
        raise MacMlxUnavailable("not_apple_silicon_host")
    provider = MacMlxInt8LinearProvider()
    profile_binding = "signed_member_profile"
    if profile_digest == "local-declaration":
        profile_binding = "unsigned_local_declaration"
        profile_digest = record_digest({
            "platform": list(observe_host_platform()), "build_digest": provider.build_digest,
            "processor": platform.processor(), "machine": platform.machine(),
        }, "local_declaration_noncanonical")
    started = time.perf_counter()
    tensors, config, files = bench.load_qwen2_int8_model(Path(snapshot).resolve())
    load_ms = (time.perf_counter() - started) * 1000.0
    manifest = bench.frozen_manifest(
        vocab_size=int(config["vocab_size"]), paired_windows=paired_windows,
        output_lengths=tuple(output_lengths), prompt_lengths=tuple(prompt_lengths),
        repeats_per_cell=repeats_per_cell,
    )
    # One pre-warm decode packs every matrix once; any later repack is a materialization.
    numpy_runtime.install_int8_linear_provider(provider)
    try:
        first = manifest["requests_per_window"][0]
        bench.greedy_decode(tensors, config, first["prompt_ids"], 2)
    finally:
        numpy_runtime.install_int8_linear_provider(None)
    provider.mark_warm()
    samples = [thermal_module.observe()]
    log_lines: list[str] = []
    result = bench.run_paired_benchmark(tensors, config, manifest, candidate_provider=provider,
                                        log=log_lines.append)
    if log_path is not None:
        Path(log_path).write_text("\n".join(log_lines) + "\n", encoding="utf-8")
    samples.append(thermal_module.observe())
    drift = result["candidate_rate_drift"]
    thermal = thermal_module.decide(samples, declared=thermal_state, rate_drift=drift)
    driver_digest = "sha256:" + hashlib.sha256(json.dumps({
        "python": platform.python_version(), "numpy": np.__version__, "system": platform.platform(),
        "mlx": provider.mlx_version, "machine": platform.machine(),
    }, sort_keys=True).encode()).hexdigest()
    now = time.time()
    failures = dict(result["reliability"])
    if result["token_parity_mismatches"]:
        failures["failures"] += len(result["token_parity_mismatches"])
    record = decide_efficiency({
        "protocol": "mycelium.runtime_efficiency_qualification.v1",
        "qualification_id": f"a9-mac-stage-{int(now)}",
        "provider_binding": {
            "backend_id": BACKEND_ID if mac else "numpy", "build_digest": provider.build_digest,
            "device_class": DEVICE_CLASS if mac else "conformance_host",
            "driver_runtime_digest": driver_digest,
        },
        "model_binding": {"model_id": bench.MODEL_ID, "revision": bench.MODEL_REVISION,
                          "representation": bench.REPRESENTATION, "profile_digest": profile_digest},
        "benchmark": {
            "manifest_digest": manifest["manifest_digest"], "paired_windows": result["paired_windows"],
            "requests_per_mode": min(result["requests_per_mode"].values()),
            "prompt_buckets": manifest["prompt_buckets"], "output_buckets": manifest["output_buckets"],
            "generated_tokens_per_mode": min(result["generated_tokens_per_mode"].values()),
            "thresholds": dict(THRESHOLDS), "thermal_state": thermal["state"], "measured_at": now,
        },
        "results": {key: float(result[key]) for key in (
            "median_decode_ratio", "paired_confidence_lower_bound",
            "median_decode_tokens_per_second", "p95_ttft_ms")},
        "reliability": failures, "cleanup": result["cleanup"],
        "materialization_proof": {
            "warm_decode_full_weight_materializations": provider.full_weight_materializations
            + (0 if result["packed_state_unchanged"] else 1),
            "warm_decode_windows": result["paired_windows"],
        },
        "planner_result": {"vocabulary_head_cost_explicit": True, "unknown_component_costs": [],
                           "prediction_error_ms": float(result["component_reconciliation_error_ms"])},
        "evidence_generation": evidence_generation, "issued_at": now, "expires_at": now + 7 * 24 * 3600,
    })
    decision = record.to_mapping() if hasattr(record, "to_mapping") else record
    manifest_doc = {**manifest, "model_files": files, "build_digest": provider.build_digest,
                    "frozen_at": now}
    return manifest_doc, {
        "mac_host": mac, "profile_binding": profile_binding, "load_ms": load_ms, "thermal": thermal,
        "candidate_rate_drift": drift, "result": result, "provider": provider.report(),
        "efficiency_qualification": decision,
    }
