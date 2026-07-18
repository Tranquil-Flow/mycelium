from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping


_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def _require_nonempty(name: str, value: str) -> None:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise ValueError(f"{name} must be a non-empty bounded string")


def _require_finite_nonnegative(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite and non-negative")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def _is_failure_marker(observation: "CapacityObservation") -> bool:
    return (
        observation.oom
        or observation.thermal_throttled
        or observation.peak_memory_bytes > observation.memory_budget_bytes
    )


@dataclass(frozen=True)
class CapacityProfileKey:
    model_digest: str
    source_evidence_digest: str
    quantization: str
    backend: str
    runtime_build: str
    hardware_class: str
    power_mode: str
    context_bucket: str
    kv_mode: str

    def __post_init__(self) -> None:
        for name in ("model_digest", "source_evidence_digest"):
            value = getattr(self, name)
            if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
                raise ValueError(f"{name} must be a lowercase sha256 digest")
        for name in (
            "quantization",
            "backend",
            "runtime_build",
            "hardware_class",
            "power_mode",
            "context_bucket",
            "kv_mode",
        ):
            _require_nonempty(name, getattr(self, name))

    def to_document(self) -> dict[str, str]:
        return {
            "model_digest": self.model_digest,
            "source_evidence_digest": self.source_evidence_digest,
            "quantization": self.quantization,
            "backend": self.backend,
            "runtime_build": self.runtime_build,
            "hardware_class": self.hardware_class,
            "power_mode": self.power_mode,
            "context_bucket": self.context_bucket,
            "kv_mode": self.kv_mode,
        }


@dataclass(frozen=True)
class CapacityProfilePolicy:
    ttft_p95_slo_ms: float
    tpot_p95_slo_ms: float
    min_samples: int

    def __post_init__(self) -> None:
        _require_finite_nonnegative("ttft_p95_slo_ms", self.ttft_p95_slo_ms)
        _require_finite_nonnegative("tpot_p95_slo_ms", self.tpot_p95_slo_ms)
        if self.ttft_p95_slo_ms <= 0 or self.tpot_p95_slo_ms <= 0:
            raise ValueError("latency SLOs must be positive")
        if (
            isinstance(self.min_samples, bool)
            or not isinstance(self.min_samples, int)
            or self.min_samples <= 0
        ):
            raise ValueError("min_samples must be a positive integer")

    def to_document(self) -> dict[str, int | float]:
        return {
            "ttft_p95_slo_ms": float(self.ttft_p95_slo_ms),
            "tpot_p95_slo_ms": float(self.tpot_p95_slo_ms),
            "min_samples": self.min_samples,
        }


@dataclass(frozen=True)
class CapacityObservation:
    concurrency: int
    sample_count: int
    p95_ttft_ms: float | None
    p95_tpot_ms: float | None
    aggregate_output_tps: float | None
    peak_memory_bytes: int
    memory_budget_bytes: int
    oom: bool = False
    thermal_throttled: bool = False

    def __post_init__(self) -> None:
        for name in ("concurrency", "sample_count", "memory_budget_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.peak_memory_bytes, bool)
            or not isinstance(self.peak_memory_bytes, int)
            or self.peak_memory_bytes < 0
        ):
            raise ValueError("peak_memory_bytes must be a non-negative integer")
        if not isinstance(self.oom, bool) or not isinstance(
            self.thermal_throttled, bool
        ):
            raise ValueError("oom and thermal_throttled must be boolean")

        metrics = (
            self.p95_ttft_ms,
            self.p95_tpot_ms,
            self.aggregate_output_tps,
        )
        if any(value is None for value in metrics):
            if not all(value is None for value in metrics):
                raise ValueError("latency and throughput metrics must all be present or omitted")
            if not _is_failure_marker(self):
                raise ValueError("successful observations require latency and throughput metrics")
            return

        for name, value in zip(
            ("p95_ttft_ms", "p95_tpot_ms", "aggregate_output_tps"),
            metrics,
            strict=True,
        ):
            assert value is not None
            _require_finite_nonnegative(name, value)
        ttft, tpot, aggregate_tps = metrics
        assert ttft is not None and tpot is not None and aggregate_tps is not None
        if not _is_failure_marker(self) and (
            ttft <= 0 or tpot <= 0 or aggregate_tps <= 0
        ):
            raise ValueError("successful observations require positive latency and throughput metrics")

    @property
    def is_safe(self) -> bool:
        return not _is_failure_marker(self)


@dataclass(frozen=True)
class EvaluatedCapacityObservation:
    concurrency: int
    sample_count: int
    p95_ttft_ms: float | None
    p95_tpot_ms: float | None
    aggregate_output_tps: float | None
    peak_memory_bytes: int
    memory_budget_bytes: int
    oom: bool
    thermal_throttled: bool
    safe: bool
    interactive_slo_met: bool

    @classmethod
    def from_observation(
        cls,
        observation: CapacityObservation,
        policy: CapacityProfilePolicy,
    ) -> "EvaluatedCapacityObservation":
        safe = observation.is_safe
        interactive_slo_met = False
        if safe:
            assert observation.p95_ttft_ms is not None
            assert observation.p95_tpot_ms is not None
            interactive_slo_met = (
                observation.p95_ttft_ms <= policy.ttft_p95_slo_ms
                and observation.p95_tpot_ms <= policy.tpot_p95_slo_ms
            )
        return cls(
            concurrency=observation.concurrency,
            sample_count=observation.sample_count,
            p95_ttft_ms=(
                None
                if observation.p95_ttft_ms is None
                else float(observation.p95_ttft_ms)
            ),
            p95_tpot_ms=(
                None
                if observation.p95_tpot_ms is None
                else float(observation.p95_tpot_ms)
            ),
            aggregate_output_tps=(
                None
                if observation.aggregate_output_tps is None
                else float(observation.aggregate_output_tps)
            ),
            peak_memory_bytes=observation.peak_memory_bytes,
            memory_budget_bytes=observation.memory_budget_bytes,
            oom=observation.oom,
            thermal_throttled=observation.thermal_throttled,
            safe=safe,
            interactive_slo_met=interactive_slo_met,
        )

    def as_observation(self) -> CapacityObservation:
        return CapacityObservation(
            concurrency=self.concurrency,
            sample_count=self.sample_count,
            p95_ttft_ms=self.p95_ttft_ms,
            p95_tpot_ms=self.p95_tpot_ms,
            aggregate_output_tps=self.aggregate_output_tps,
            peak_memory_bytes=self.peak_memory_bytes,
            memory_budget_bytes=self.memory_budget_bytes,
            oom=self.oom,
            thermal_throttled=self.thermal_throttled,
        )

    def to_document(self) -> Mapping[str, Any]:
        return {
            "concurrency": self.concurrency,
            "sample_count": self.sample_count,
            "p95_ttft_ms": self.p95_ttft_ms,
            "p95_tpot_ms": self.p95_tpot_ms,
            "aggregate_output_tps": self.aggregate_output_tps,
            "peak_memory_bytes": self.peak_memory_bytes,
            "memory_budget_bytes": self.memory_budget_bytes,
            "oom": self.oom,
            "thermal_throttled": self.thermal_throttled,
            "safe": self.safe,
            "interactive_slo_met": self.interactive_slo_met,
        }
