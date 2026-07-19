"""Frozen canonical RouteQualificationV1 output contract."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from .evidence import canonical_json_bytes, is_sha256_ref

ROUTE_QUALIFICATION_PROTOCOL = "mycelium.route_qualification.v1"
QUALIFIER_AUTHORITY = "mycelium_qualification.qualifier:RouteQualificationV1"
_CONTRACT_FIELDS = frozenset(
    {
        "protocol",
        "qualification_id",
        "issued_at_unix_ms",
        "evidence_class",
        "route_ready",
        "reason_codes",
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "gossip_snapshot_digest",
        "gossip_signature_digest",
        "planner_snapshot_digest",
        "route_plan_digest",
        "assignments_digest",
        "provisioning_reports_digest",
        "load_proofs_digest",
        "load_proof_signatures_digest",
        "execution_graph_digest",
        "path_manifest_digest",
        "reservations_digest",
        "stage_bindings",
        "endpoint_set_digest",
        "process_set_digest",
        "tensor_scope_digest",
        "transport_digest",
        "timing_evidence_digest",
        "token_parity_digest",
        "numeric_parity_digest",
        "execution_trace_digest",
        "kv_ownership_digest",
        "lifecycle_evidence_digest",
        "negative_runs_digest",
        "source_provenance_digest",
        "source_manifest_digest",
        "environment_digest",
        "contract_manifest_digest",
        "dependency_lock_digests",
        "evidence_manifest_digest",
        "qualified_by",
        "claim_boundary",
    }
)
_STAGE_FIELDS = frozenset(
    {
        "stage_id",
        "placement_id",
        "assignment_id",
        "node_id",
        "stage_signature",
        "load_proof_digest",
        "stage_probe_result_digest",
        "endpoint_id",
        "process_id",
        "process_host_id",
        "tensor_scope_digest",
        "reservation_id",
    }
)
_DIGEST_FIELDS = (
    "manifest_digest",
    "gossip_snapshot_digest",
    "gossip_signature_digest",
    "planner_snapshot_digest",
    "route_plan_digest",
    "assignments_digest",
    "provisioning_reports_digest",
    "load_proofs_digest",
    "load_proof_signatures_digest",
    "execution_graph_digest",
    "path_manifest_digest",
    "reservations_digest",
    "endpoint_set_digest",
    "process_set_digest",
    "tensor_scope_digest",
    "transport_digest",
    "timing_evidence_digest",
    "token_parity_digest",
    "numeric_parity_digest",
    "execution_trace_digest",
    "kv_ownership_digest",
    "lifecycle_evidence_digest",
    "negative_runs_digest",
    "source_provenance_digest",
    "source_manifest_digest",
    "environment_digest",
    "contract_manifest_digest",
    "evidence_manifest_digest",
)


class QualificationContractError(ValueError):
    """Strict RouteQualificationV1 parsing or invariant error."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise QualificationContractError(code, detail)


@dataclass(frozen=True, slots=True)
class StageQualificationBinding:
    stage_id: str
    placement_id: str
    assignment_id: str
    node_id: str
    stage_signature: str
    load_proof_digest: str
    stage_probe_result_digest: str
    endpoint_id: str
    process_id: int
    process_host_id: str
    tensor_scope_digest: str
    reservation_id: str


@dataclass(frozen=True, slots=True, init=False)
class RouteQualificationV1:
    protocol: str
    qualification_id: str
    issued_at_unix_ms: int
    evidence_class: str
    route_ready: bool
    reason_codes: tuple[str, ...]
    deployment_id: str
    deployment_epoch: int
    topology_version: int
    model_id: str
    resolved_commit: str
    manifest_digest: str
    gossip_snapshot_digest: str
    gossip_signature_digest: str
    planner_snapshot_digest: str
    route_plan_digest: str
    assignments_digest: str
    provisioning_reports_digest: str
    load_proofs_digest: str
    load_proof_signatures_digest: str
    execution_graph_digest: str
    path_manifest_digest: str
    reservations_digest: str
    stage_bindings: tuple[StageQualificationBinding, ...]
    endpoint_set_digest: str
    process_set_digest: str
    tensor_scope_digest: str
    transport_digest: str
    timing_evidence_digest: str
    token_parity_digest: str
    numeric_parity_digest: str
    execution_trace_digest: str
    kv_ownership_digest: str
    lifecycle_evidence_digest: str
    negative_runs_digest: str
    source_provenance_digest: str
    source_manifest_digest: str
    environment_digest: str
    contract_manifest_digest: str
    dependency_lock_digests: tuple[str, ...]
    evidence_manifest_digest: str
    qualified_by: str | None
    claim_boundary: str

    def __new__(cls) -> "RouteQualificationV1":
        raise TypeError("RouteQualificationV1 records require the contract parser or qualifier authority")


def _construct_unqualified_route(**values: Any) -> RouteQualificationV1:
    """Construct only unqualified parser/fixture records; qualification lives elsewhere."""
    expected = {field.name for field in fields(RouteQualificationV1)}
    _require(set(values) == expected, "invalid_contract_field")
    _require(type(values.get("route_ready")) is bool, "invalid_route_ready")
    _require(values["route_ready"] is False, "qualified_record_requires_qualifier")
    record = object.__new__(RouteQualificationV1)
    for name, value in values.items():
        object.__setattr__(record, name, value)
    _validate_record(record)
    return record


def _stage_to_dict(stage: StageQualificationBinding) -> dict[str, Any]:
    return {
        "stage_id": stage.stage_id,
        "placement_id": stage.placement_id,
        "assignment_id": stage.assignment_id,
        "node_id": stage.node_id,
        "stage_signature": stage.stage_signature,
        "load_proof_digest": stage.load_proof_digest,
        "stage_probe_result_digest": stage.stage_probe_result_digest,
        "endpoint_id": stage.endpoint_id,
        "process_id": stage.process_id,
        "process_host_id": stage.process_host_id,
        "tensor_scope_digest": stage.tensor_scope_digest,
        "reservation_id": stage.reservation_id,
    }


def route_qualification_to_dict(record: RouteQualificationV1) -> dict[str, Any]:
    _validate_record(record)
    return {
        "protocol": record.protocol,
        "qualification_id": record.qualification_id,
        "issued_at_unix_ms": record.issued_at_unix_ms,
        "evidence_class": record.evidence_class,
        "route_ready": record.route_ready,
        "reason_codes": list(record.reason_codes),
        "deployment_id": record.deployment_id,
        "deployment_epoch": record.deployment_epoch,
        "topology_version": record.topology_version,
        "model_id": record.model_id,
        "resolved_commit": record.resolved_commit,
        "manifest_digest": record.manifest_digest,
        "gossip_snapshot_digest": record.gossip_snapshot_digest,
        "gossip_signature_digest": record.gossip_signature_digest,
        "planner_snapshot_digest": record.planner_snapshot_digest,
        "route_plan_digest": record.route_plan_digest,
        "assignments_digest": record.assignments_digest,
        "provisioning_reports_digest": record.provisioning_reports_digest,
        "load_proofs_digest": record.load_proofs_digest,
        "load_proof_signatures_digest": record.load_proof_signatures_digest,
        "execution_graph_digest": record.execution_graph_digest,
        "path_manifest_digest": record.path_manifest_digest,
        "reservations_digest": record.reservations_digest,
        "stage_bindings": [_stage_to_dict(stage) for stage in record.stage_bindings],
        "endpoint_set_digest": record.endpoint_set_digest,
        "process_set_digest": record.process_set_digest,
        "tensor_scope_digest": record.tensor_scope_digest,
        "transport_digest": record.transport_digest,
        "timing_evidence_digest": record.timing_evidence_digest,
        "token_parity_digest": record.token_parity_digest,
        "numeric_parity_digest": record.numeric_parity_digest,
        "execution_trace_digest": record.execution_trace_digest,
        "kv_ownership_digest": record.kv_ownership_digest,
        "lifecycle_evidence_digest": record.lifecycle_evidence_digest,
        "negative_runs_digest": record.negative_runs_digest,
        "source_provenance_digest": record.source_provenance_digest,
        "source_manifest_digest": record.source_manifest_digest,
        "environment_digest": record.environment_digest,
        "contract_manifest_digest": record.contract_manifest_digest,
        "dependency_lock_digests": list(record.dependency_lock_digests),
        "evidence_manifest_digest": record.evidence_manifest_digest,
        "qualified_by": record.qualified_by,
        "claim_boundary": record.claim_boundary,
    }


def _stage_from_dict(document: Any) -> StageQualificationBinding:
    _require(isinstance(document, dict), "invalid_stage_binding")
    _require(set(document) == _STAGE_FIELDS, "invalid_stage_binding")
    try:
        return StageQualificationBinding(**document)
    except TypeError as exc:
        raise QualificationContractError("invalid_stage_binding", str(exc)) from exc


def _record_from_dict(document: dict[str, Any]) -> RouteQualificationV1:
    missing = _CONTRACT_FIELDS - set(document)
    unknown = set(document) - _CONTRACT_FIELDS
    _require(not missing, "missing_contract_field", ",".join(sorted(missing)))
    _require(not unknown, "unknown_contract_field", ",".join(sorted(unknown)))
    _require(isinstance(document["reason_codes"], list), "invalid_reason_codes")
    _require(
        isinstance(document["dependency_lock_digests"], list),
        "invalid_dependency_lock_digests",
    )
    _require(isinstance(document["stage_bindings"], list), "invalid_stage_bindings")
    try:
        return _construct_unqualified_route(
            protocol=document["protocol"],
            qualification_id=document["qualification_id"],
            issued_at_unix_ms=document["issued_at_unix_ms"],
            evidence_class=document["evidence_class"],
            route_ready=document["route_ready"],
            reason_codes=tuple(document["reason_codes"]),
            deployment_id=document["deployment_id"],
            deployment_epoch=document["deployment_epoch"],
            topology_version=document["topology_version"],
            model_id=document["model_id"],
            resolved_commit=document["resolved_commit"],
            manifest_digest=document["manifest_digest"],
            gossip_snapshot_digest=document["gossip_snapshot_digest"],
            gossip_signature_digest=document["gossip_signature_digest"],
            planner_snapshot_digest=document["planner_snapshot_digest"],
            route_plan_digest=document["route_plan_digest"],
            assignments_digest=document["assignments_digest"],
            provisioning_reports_digest=document["provisioning_reports_digest"],
            load_proofs_digest=document["load_proofs_digest"],
            load_proof_signatures_digest=document["load_proof_signatures_digest"],
            execution_graph_digest=document["execution_graph_digest"],
            path_manifest_digest=document["path_manifest_digest"],
            reservations_digest=document["reservations_digest"],
            stage_bindings=tuple(_stage_from_dict(item) for item in document["stage_bindings"]),
            endpoint_set_digest=document["endpoint_set_digest"],
            process_set_digest=document["process_set_digest"],
            tensor_scope_digest=document["tensor_scope_digest"],
            transport_digest=document["transport_digest"],
            timing_evidence_digest=document["timing_evidence_digest"],
            token_parity_digest=document["token_parity_digest"],
            numeric_parity_digest=document["numeric_parity_digest"],
            execution_trace_digest=document["execution_trace_digest"],
            kv_ownership_digest=document["kv_ownership_digest"],
            lifecycle_evidence_digest=document["lifecycle_evidence_digest"],
            negative_runs_digest=document["negative_runs_digest"],
            source_provenance_digest=document["source_provenance_digest"],
            source_manifest_digest=document["source_manifest_digest"],
            environment_digest=document["environment_digest"],
            contract_manifest_digest=document["contract_manifest_digest"],
            dependency_lock_digests=tuple(document["dependency_lock_digests"]),
            evidence_manifest_digest=document["evidence_manifest_digest"],
            qualified_by=document["qualified_by"],
            claim_boundary=document["claim_boundary"],
        )
    except (KeyError, TypeError) as exc:
        raise QualificationContractError("invalid_contract_field", str(exc)) from exc


def route_qualification_from_dict(document: dict[str, Any]) -> RouteQualificationV1:
    """Parse unqualified compatibility records; accepted records require qualifier validation."""
    _require(isinstance(document, dict), "invalid_contract_document")
    if document.get("route_ready") is True:
        raise QualificationContractError("qualified_record_requires_qualifier")
    record = _record_from_dict(document)
    _validate_record(record)
    return record


def _validate_record(record: RouteQualificationV1) -> None:
    _require(record.protocol == ROUTE_QUALIFICATION_PROTOCOL, "unsupported_qualification_protocol")
    for field in (
        "qualification_id",
        "deployment_id",
        "model_id",
        "resolved_commit",
        "claim_boundary",
    ):
        value = getattr(record, field)
        _require(isinstance(value, str) and bool(value.strip()), f"invalid_{field}")
    for field in ("issued_at_unix_ms", "deployment_epoch", "topology_version"):
        value = getattr(record, field)
        _require(
            isinstance(value, int) and not isinstance(value, bool) and value >= 0,
            f"invalid_{field}",
        )
    _require(type(record.route_ready) is bool, "invalid_route_ready")
    _require(
        record.evidence_class in {"physical_qualification", "synthetic_test_fixture"},
        "invalid_evidence_class",
    )
    for field in _DIGEST_FIELDS:
        _require(is_sha256_ref(getattr(record, field)), f"invalid_{field}")
    _require(
        isinstance(record.dependency_lock_digests, tuple)
        and bool(record.dependency_lock_digests)
        and all(is_sha256_ref(item) for item in record.dependency_lock_digests),
        "invalid_dependency_lock_digests",
    )
    _require(
        isinstance(record.reason_codes, tuple)
        and len(record.reason_codes) == len(set(record.reason_codes))
        and all(isinstance(code, str) and bool(code) for code in record.reason_codes),
        "invalid_reason_codes",
    )
    _require(
        isinstance(record.stage_bindings, tuple)
        and all(isinstance(stage, StageQualificationBinding) for stage in record.stage_bindings),
        "invalid_stage_bindings",
    )
    for stage in record.stage_bindings:
        for field in (
            "stage_id",
            "placement_id",
            "assignment_id",
            "node_id",
            "endpoint_id",
            "process_host_id",
            "reservation_id",
        ):
            value = getattr(stage, field)
            _require(isinstance(value, str) and bool(value.strip()), "invalid_stage_binding", field)
        _require(is_sha256_ref(stage.stage_signature), "invalid_stage_signature")
        _require(is_sha256_ref(stage.load_proof_digest), "invalid_load_proof_digest")
        _require(
            is_sha256_ref(stage.stage_probe_result_digest),
            "invalid_stage_probe_result_digest",
        )
        _require(is_sha256_ref(stage.tensor_scope_digest), "invalid_tensor_scope_digest")
        _require(
            isinstance(stage.process_id, int)
            and not isinstance(stage.process_id, bool)
            and stage.process_id > 0,
            "invalid_process_id",
        )
    if record.route_ready:
        _require(record.evidence_class == "physical_qualification", "qualified_evidence_not_physical")
        _require(not record.reason_codes, "qualified_record_has_reasons")
        _require(record.qualified_by == QUALIFIER_AUTHORITY, "qualified_record_wrong_authority")
        _require(bool(record.stage_bindings), "qualified_record_missing_stages")
    else:
        _require(bool(record.reason_codes), "unqualified_record_missing_reasons")
        _require(record.qualified_by is None, "unqualified_record_has_authority")
    canonical_json_bytes(
        {
            "protocol": record.protocol,
            "qualification_id": record.qualification_id,
            "route_ready": record.route_ready,
        }
    )


def synthetic_route_qualification_fixture() -> RouteQualificationV1:
    """Return an unmistakable unaccepted fixture; never physical run evidence."""
    zero = "sha256:" + "0" * 64
    return _construct_unqualified_route(
        protocol=ROUTE_QUALIFICATION_PROTOCOL,
        qualification_id="synthetic-test-fixture:route-qualification-v1",
        issued_at_unix_ms=0,
        evidence_class="synthetic_test_fixture",
        route_ready=False,
        reason_codes=("synthetic_test_fixture_not_accepted",),
        deployment_id="synthetic-test-fixture-no-deployment",
        deployment_epoch=0,
        topology_version=0,
        model_id="synthetic-test-fixture-no-model",
        resolved_commit="synthetic-test-fixture-no-revision",
        manifest_digest=zero,
        gossip_snapshot_digest=zero,
        gossip_signature_digest=zero,
        planner_snapshot_digest=zero,
        route_plan_digest=zero,
        assignments_digest=zero,
        provisioning_reports_digest=zero,
        load_proofs_digest=zero,
        load_proof_signatures_digest=zero,
        execution_graph_digest=zero,
        path_manifest_digest=zero,
        reservations_digest=zero,
        stage_bindings=(),
        endpoint_set_digest=zero,
        process_set_digest=zero,
        tensor_scope_digest=zero,
        transport_digest=zero,
        timing_evidence_digest=zero,
        token_parity_digest=zero,
        numeric_parity_digest=zero,
        execution_trace_digest=zero,
        kv_ownership_digest=zero,
        lifecycle_evidence_digest=zero,
        negative_runs_digest=zero,
        source_provenance_digest=zero,
        source_manifest_digest=zero,
        environment_digest=zero,
        contract_manifest_digest=zero,
        dependency_lock_digests=(zero,),
        evidence_manifest_digest=zero,
        qualified_by=None,
        claim_boundary=(
            "synthetic_test_fixture schema shape only; no physical qualification and route_ready remains false"
        ),
    )
