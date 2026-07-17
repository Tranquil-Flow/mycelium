from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CalibrationArtifact:
    model_id: str
    revision: str
    backend: str
    device_id: str
    layer_start: int
    layer_end: int
    precision: str
    prefill_samples_ms: tuple[float, ...]
    decode_samples_ms: tuple[float, ...]
    payload_bytes: tuple[int, ...]
    batch_context_points: tuple[tuple[int, int], ...]
    measured_at: str
    environment: Mapping[str, str]
    evidence: str
    measurement_command: str | None = None

    def __post_init__(self) -> None:
        if not all((self.model_id, self.revision, self.backend, self.device_id, self.precision, self.measured_at)):
            raise ValueError("calibration identity and timestamp fields are required")
        if self.layer_start < 0 or self.layer_end <= self.layer_start:
            raise ValueError("calibration layer range must be positive and half-open")
        if not self.prefill_samples_ms or not self.decode_samples_ms:
            raise ValueError("prefill and decode timing distributions are required")
        if any(value < 0 for value in self.prefill_samples_ms + self.decode_samples_ms):
            raise ValueError("timing samples must be non-negative")
        if not self.payload_bytes or any(value <= 0 for value in self.payload_bytes):
            raise ValueError("positive payload measurements are required")
        if not self.batch_context_points or any(batch <= 0 or context < 0 for batch, context in self.batch_context_points):
            raise ValueError("valid batch/context calibration points are required")
        if not self.environment:
            raise ValueError("calibration environment is required")
        if self.evidence not in {"measured", "heuristic"}:
            raise ValueError("calibration evidence must be measured or heuristic")
        if self.evidence == "measured" and not self.measurement_command:
            raise ValueError("measured calibration requires a reproducible measurement command")

    @property
    def is_measured(self) -> bool:
        return self.evidence == "measured" and bool(self.measurement_command)


def ingest_calibration(data: Mapping[str, Any]) -> CalibrationArtifact:
    return CalibrationArtifact(
        model_id=str(data["model_id"]),
        revision=str(data["revision"]),
        backend=str(data["backend"]),
        device_id=str(data["device_id"]),
        layer_start=int(data["layer_start"]),
        layer_end=int(data["layer_end"]),
        precision=str(data["precision"]),
        prefill_samples_ms=tuple(float(value) for value in data["prefill_samples_ms"]),
        decode_samples_ms=tuple(float(value) for value in data["decode_samples_ms"]),
        payload_bytes=tuple(int(value) for value in data["payload_bytes"]),
        batch_context_points=tuple((int(point[0]), int(point[1])) for point in data["batch_context_points"]),
        measured_at=str(data["measured_at"]),
        environment={str(key): str(value) for key, value in data["environment"].items()},
        evidence=str(data["evidence"]),
        measurement_command=data.get("measurement_command"),
    )
