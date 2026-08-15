"""Single-flight, local-only transition from feasible model to prepared route."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import hashlib
import json
import re
import threading
import time
from typing import Any, Callable, Mapping


PREPARATION_PROTOCOL = "mycelium.model_preparation.v1"
AUTHORIZATION_PROTOCOL = "mycelium.model_preparation_authorization.v1"
REPRESENTATION_DECISION_PROTOCOL = "mycelium.model_representation_decision.v1"
_PHASES = {
    "validating_capacity",
    "compiling_assignments",
    "verifying_local_artifacts",
    "staging_peers",
    "publishing_candidate",
}
_STATES = {"idle", "preparing", "succeeded", "failed"}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_SAFE_REASON = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_DECISION_FIELDS = {
    "protocol",
    "model_id",
    "revision",
    "source_quantization",
    "serving_dtype",
    "serving_quantization",
    "representation_digest",
    "conversion_authorized",
}


class ModelPreparationError(RuntimeError):
    def __init__(self, code: str) -> None:
        if _SAFE_REASON.fullmatch(code) is None:
            raise ValueError("model_preparation_error_code_invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparationResult:
    candidate_id: str
    topology_size: int
    transfer_bytes: int
    verified_bytes: int


Preparer = Callable[
    [Mapping[str, Any], Callable[[str, int | None, int | None], None]],
    PreparationResult,
]


def _detached(value: Mapping[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(dict(value))


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _public_reason(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    if not isinstance(code, str):
        code = str(exc).partition(":")[0]
    return code if _SAFE_REASON.fullmatch(code) else "model_preparation_failed"


def _authorization(
    operation: Mapping[str, Any],
    *,
    decision: Mapping[str, Any],
    now_unix_ms: int,
) -> dict[str, Any]:
    if set(decision) != _DECISION_FIELDS:
        raise ModelPreparationError("model_representation_decision_invalid")
    model_id = decision.get("model_id")
    revision = decision.get("revision")
    if (
        decision.get("protocol") != REPRESENTATION_DECISION_PROTOCOL
        or not isinstance(model_id, str)
        or not model_id
        or len(model_id) > 256
        or not isinstance(revision, str)
        or _REVISION.fullmatch(revision) is None
        or not isinstance(decision.get("source_quantization"), str)
        or not decision["source_quantization"]
        or not isinstance(decision.get("serving_dtype"), str)
        or not decision["serving_dtype"]
        or not isinstance(decision.get("serving_quantization"), str)
        or not decision["serving_quantization"]
        or not isinstance(decision.get("representation_digest"), str)
        or _DIGEST.fullmatch(decision["representation_digest"]) is None
        or type(decision.get("conversion_authorized")) is not bool
    ):
        raise ModelPreparationError("model_representation_decision_invalid")
    if operation.get("protocol") != "mycelium.model_operation.v1":
        raise ModelPreparationError("model_operation_unavailable")
    catalog = operation.get("catalog")
    entries = (
        catalog.get("entries")
        if isinstance(catalog, Mapping)
        else operation.get("entries")
    )
    reports = operation.get("feasibility_reports")
    if not isinstance(entries, list) or not isinstance(reports, list):
        raise ModelPreparationError("model_operation_invalid")
    matching_entries = [
        item
        for item in entries
        if isinstance(item, Mapping)
        and item.get("model_id") == model_id
        and item.get("revision") == revision
    ]
    if len(matching_entries) != 1:
        raise ModelPreparationError("model_identity_unknown")
    if matching_entries[0].get("state") != "compatible":
        raise ModelPreparationError("model_not_compatible")
    matching_reports = [
        item
        for item in reports
        if isinstance(item, Mapping)
        and item.get("model_id") == model_id
        and item.get("revision") == revision
    ]
    if len(matching_reports) != 1:
        raise ModelPreparationError("model_capacity_not_evaluated")
    report = matching_reports[0]
    if (
        report.get("state") != "feasible"
        or report.get("provisioning_authorized") is not True
    ):
        raise ModelPreparationError("model_does_not_fit")
    valid_until = report.get("evidence_valid_until_unix_ms")
    if type(valid_until) is not int or valid_until < now_unix_ms:
        raise ModelPreparationError("model_capacity_stale")
    stages = report.get("stages")
    if not isinstance(stages, list) or not stages:
        raise ModelPreparationError("model_stage_plan_invalid")
    frozen_stages: list[dict[str, Any]] = []
    cursor = 0
    for index, stage in enumerate(stages):
        if not isinstance(stage, Mapping):
            raise ModelPreparationError("model_stage_plan_invalid")
        start = stage.get("start_layer")
        end = stage.get("end_layer_exclusive")
        files = stage.get("assignment_files")
        if (
            stage.get("stage_index") != index
            or type(start) is not int
            or type(end) is not int
            or start != cursor
            or end <= start
            or not isinstance(stage.get("node_id"), str)
            or not isinstance(stage.get("backend"), str)
            or not isinstance(stage.get("decode_mode"), str)
            or not isinstance(files, list)
            or not all(isinstance(item, str) and item for item in files)
            or type(stage.get("assignment_artifact_bytes")) is not int
        ):
            raise ModelPreparationError("model_stage_plan_invalid")
        frozen_stages.append(
            {
                "stage_index": index,
                "node_id": stage["node_id"],
                "start_layer": start,
                "end_layer_exclusive": end,
                "backend": stage["backend"],
                "decode_mode": stage["decode_mode"],
                "assignment_files": list(files),
                "assignment_artifact_bytes": stage["assignment_artifact_bytes"],
            }
        )
        cursor = end
    operation_digest = operation.get("operation_digest")
    feasibility_digest = report.get("feasibility_digest")
    representation_digest = report.get("representation_digest")
    source_quantization = report.get("source_quantization")
    serving_dtype = report.get("serving_dtype")
    serving_quantization = report.get("serving_quantization")
    evidence_generation = report.get("evidence_generation")
    catalog_generation = operation.get("catalog_generation")
    if (
        not isinstance(operation_digest, str)
        or _DIGEST.fullmatch(operation_digest) is None
        or not isinstance(feasibility_digest, str)
        or _DIGEST.fullmatch(feasibility_digest) is None
        or not isinstance(representation_digest, str)
        or _DIGEST.fullmatch(representation_digest) is None
        or not isinstance(source_quantization, str)
        or not source_quantization
        or not isinstance(serving_dtype, str)
        or not serving_dtype
        or not isinstance(serving_quantization, str)
        or not serving_quantization
        or type(evidence_generation) is not int
        or type(catalog_generation) is not int
    ):
        raise ModelPreparationError("model_operation_invalid")
    representation_fields = (
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
        "representation_digest",
    )
    if any(decision[field] != report[field] for field in representation_fields):
        raise ModelPreparationError("model_representation_decision_mismatch")
    conversion_required = source_quantization != serving_quantization
    if conversion_required and decision["conversion_authorized"] is not True:
        raise ModelPreparationError("model_representation_conversion_not_authorized")
    representation_authority = report.get("representation_authority")
    inherited_owner_decision_digest = (
        representation_authority.get("owner_decision_digest")
        if isinstance(representation_authority, Mapping)
        and representation_authority.get("kind")
        == "approved_existing_immutable_representation"
        else None
    )
    if inherited_owner_decision_digest is not None:
        if (
            not isinstance(inherited_owner_decision_digest, str)
            or _DIGEST.fullmatch(inherited_owner_decision_digest) is None
        ):
            raise ModelPreparationError("model_operation_invalid")
        owner_decision_digest = inherited_owner_decision_digest
    else:
        owner_decision_digest = _digest(_detached(decision))
    preparation_binding_digest = _digest(
        {
            "feasibility_digest": feasibility_digest,
            "owner_decision_digest": owner_decision_digest,
            "representation_digest": representation_digest,
        }
    )
    return {
        "protocol": AUTHORIZATION_PROTOCOL,
        "model_id": model_id,
        "revision": revision,
        "catalog_generation": catalog_generation,
        "operation_digest": operation_digest,
        "feasibility_digest": feasibility_digest,
        "representation_digest": representation_digest,
        "source_quantization": source_quantization,
        "serving_dtype": serving_dtype,
        "serving_quantization": serving_quantization,
        "conversion_authorized": decision["conversion_authorized"],
        "owner_decision_digest": owner_decision_digest,
        "preparation_binding_digest": preparation_binding_digest,
        "evidence_generation": evidence_generation,
        "evidence_valid_until_unix_ms": valid_until,
        "stages": frozen_stages,
        "download_authorized": False,
    }


class LocalModelPreparation:
    """Validate fresh catalog authority and prepare one candidate asynchronously."""

    def __init__(
        self,
        *,
        operation_source: Callable[[], Mapping[str, Any] | None],
        preparer: Preparer,
        on_candidate_published: Callable[[], object] | None = None,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self._operation_source = operation_source
        self._preparer = preparer
        self._published = on_candidate_published
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._lock = threading.RLock()
        self._generation = 1
        self._state = "idle"
        self._phase: str | None = None
        self._model_id: str | None = None
        self._revision: str | None = None
        self._candidate_id: str | None = None
        self._topology_size: int | None = None
        self._transfer_bytes = 0
        self._verified_bytes = 0
        self._reason_code: str | None = None
        self._started_at: int | None = None
        self._completed_at: int | None = None
        self._authorization: Mapping[str, Any] | None = None
        self._worker: threading.Thread | None = None

    def _status_locked(self) -> dict[str, Any]:
        return {
            "protocol": PREPARATION_PROTOCOL,
            "generation": self._generation,
            "state": self._state,
            "phase": self._phase,
            "model_id": self._model_id,
            "revision": self._revision,
            "representation_digest": (
                None
                if self._authorization is None
                else self._authorization["representation_digest"]
            ),
            "owner_decision_digest": (
                None
                if self._authorization is None
                else self._authorization["owner_decision_digest"]
            ),
            "candidate_id": self._candidate_id,
            "topology_size": self._topology_size,
            "transfer_bytes": self._transfer_bytes,
            "verified_bytes": self._verified_bytes,
            "reason_code": self._reason_code,
            "started_at_unix_ms": self._started_at,
            "completed_at_unix_ms": self._completed_at,
            "download_authorized": False,
            "activation_started": False,
        }

    def status(self) -> Mapping[str, Any]:
        with self._lock:
            return _detached(self._status_locked())

    def _progress(
        self, phase: str, transfer_bytes: int | None, verified_bytes: int | None
    ) -> None:
        if phase not in _PHASES:
            raise ModelPreparationError("model_preparation_phase_invalid")
        with self._lock:
            self._phase = phase
            if transfer_bytes is not None:
                self._transfer_bytes = max(0, int(transfer_bytes))
            if verified_bytes is not None:
                self._verified_bytes = max(0, int(verified_bytes))
            self._generation += 1

    def start(self, decision: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(decision, Mapping):
            raise ModelPreparationError("model_representation_decision_invalid")
        model_id = decision.get("model_id")
        revision = decision.get("revision")
        with self._lock:
            if self._state == "preparing":
                if self._model_id == model_id and self._revision == revision:
                    return _detached(self._status_locked())
                raise ModelPreparationError("model_preparation_busy")
        operation = self._operation_source()
        if operation is None:
            raise ModelPreparationError("model_operation_unavailable")
        authorization = _authorization(
            operation,
            decision=decision,
            now_unix_ms=self._clock(),
        )
        with self._lock:
            self._state = "preparing"
            self._phase = "validating_capacity"
            self._model_id = model_id
            self._revision = revision
            self._candidate_id = None
            self._topology_size = len(authorization["stages"])
            self._transfer_bytes = sum(
                int(stage["assignment_artifact_bytes"])
                for stage in authorization["stages"]
            )
            self._verified_bytes = 0
            self._reason_code = None
            self._started_at = self._clock()
            self._completed_at = None
            self._authorization = authorization
            self._generation += 1
            worker = threading.Thread(
                target=self._run, name="mycelium-model-preparation", daemon=True
            )
            self._worker = worker
            worker.start()
            return _detached(self._status_locked())

    def _run(self) -> None:
        try:
            assert self._authorization is not None
            result = self._preparer(self._authorization, self._progress)
            if not isinstance(result, PreparationResult):
                raise ModelPreparationError("model_preparation_result_invalid")
            if self._published is not None:
                self._published()
            with self._lock:
                self._state = "succeeded"
                self._phase = None
                self._candidate_id = result.candidate_id
                self._topology_size = result.topology_size
                self._transfer_bytes = result.transfer_bytes
                self._verified_bytes = result.verified_bytes
                self._reason_code = None
                self._completed_at = self._clock()
                self._generation += 1
        except BaseException as exc:
            with self._lock:
                self._state = "failed"
                self._phase = None
                self._reason_code = _public_reason(exc)
                self._completed_at = self._clock()
                self._generation += 1
        finally:
            with self._lock:
                self._worker = None

    def close(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.join(timeout=5.0)


__all__ = [
    "AUTHORIZATION_PROTOCOL",
    "LocalModelPreparation",
    "ModelPreparationError",
    "PREPARATION_PROTOCOL",
    "REPRESENTATION_DECISION_PROTOCOL",
    "PreparationResult",
]
