"""Fail-closed binding to the frozen RouteQualificationV1 authority."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mycelium_qualification.contracts import RouteQualificationV1

from .contracts import AdmissionError, QualificationBinding, qualification_binding


class QualificationSource(Protocol):
    """Read-only source of the qualifier authority's current exact record."""

    def current(self) -> RouteQualificationV1 | None: ...


@dataclass(frozen=True, slots=True)
class CapturedQualification:
    qualification: RouteQualificationV1
    binding: QualificationBinding


class QualificationGate:
    """Capture and continuously revalidate one exact qualification identity."""

    def __init__(self, source: QualificationSource) -> None:
        self._source = source

    def capture(self, requested: QualificationBinding) -> CapturedQualification:
        current = self._read_current()
        actual = qualification_binding(current)
        self._compare(requested, actual)
        return CapturedQualification(qualification=current, binding=actual)

    def revalidate(self, captured: CapturedQualification) -> None:
        current = self._read_current()
        actual = qualification_binding(current)
        self._compare(captured.binding, actual)

    def current_projection_source(self) -> RouteQualificationV1:
        """Return current record for an allowlisted projection, fail closed if absent."""
        return self._read_current()

    def _read_current(self) -> RouteQualificationV1:
        try:
            current = self._source.current()
        except Exception as exc:
            raise AdmissionError("qualification_unavailable") from exc
        if current is None:
            raise AdmissionError("route_dropped")
        if not isinstance(current, RouteQualificationV1):
            raise AdmissionError("qualification_unavailable")
        if current.route_ready is not True:
            raise AdmissionError("readiness_revoked")
        return current

    @staticmethod
    def _compare(requested: QualificationBinding, actual: QualificationBinding) -> None:
        if requested.deployment_id != actual.deployment_id:
            raise AdmissionError("qualification_mismatch")
        if requested.deployment_epoch != actual.deployment_epoch:
            raise AdmissionError("deployment_epoch_changed")
        if (
            requested.topology_version != actual.topology_version
            or requested.path_manifest_digest != actual.path_manifest_digest
        ):
            raise AdmissionError("path_changed")
        if requested.qualification_id != actual.qualification_id:
            raise AdmissionError("stale_qualification")
        if (
            requested.model_id != actual.model_id
            or requested.resolved_commit != actual.resolved_commit
            or requested.manifest_digest != actual.manifest_digest
            or requested.stage_load_proof_digests
            != actual.stage_load_proof_digests
            or requested.qualification_digest != actual.qualification_digest
        ):
            raise AdmissionError("qualification_mismatch")
