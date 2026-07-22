from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

import mycelium_qualification.contracts as contracts_module
from mycelium_qualification.contracts import (
    ROUTE_QUALIFICATION_PROTOCOL,
    QualificationContractError,
    RouteQualificationV1,
    route_qualification_from_dict,
    route_qualification_to_dict,
    synthetic_route_qualification_fixture,
)
from mycelium_qualification.evidence import canonical_json_bytes, sha256_document

ROOT = Path(__file__).resolve().parents[2]
EXPECTED_FIELDS = {
    "protocol",
    "qualification_id",
    "issued_at_unix_ms",
    "evidence_class",
    "route_ready",
    "reason_codes",
    "deployment_id",
    "deployment_epoch",
    "topology_version",
    "placement_provenance",
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
EXPECTED_STAGE_FIELDS = {
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


def test_synthetic_contract_fixture_is_canonical_frozen_and_unqualified() -> None:
    fixture = synthetic_route_qualification_fixture()
    document = route_qualification_to_dict(fixture)

    assert isinstance(fixture, RouteQualificationV1)
    assert set(document) == EXPECTED_FIELDS
    assert document["protocol"] == ROUTE_QUALIFICATION_PROTOCOL
    assert document["evidence_class"] == "synthetic_test_fixture"
    assert document["route_ready"] is False
    assert document["reason_codes"] == ["synthetic_test_fixture_not_accepted"]
    assert document["qualified_by"] is None
    assert "no physical qualification" in document["claim_boundary"]
    assert route_qualification_from_dict(document) == fixture
    with pytest.raises(TypeError, match="qualifier authority"):
        RouteQualificationV1()
    with pytest.raises(dataclasses.FrozenInstanceError):
        fixture.route_ready = True  # type: ignore[misc]
    assert isinstance(fixture.stage_bindings, tuple)
    with pytest.raises(AttributeError):
        fixture.stage_bindings.append(fixture.stage_bindings[0])  # type: ignore[attr-defined]


def test_contracts_module_exposes_no_qualification_capability_or_true_factory() -> None:
    assert not hasattr(contracts_module, "_QUALIFIER_CAPABILITY")
    assert not hasattr(contracts_module, "_construct_route_qualification")


def test_contract_serialization_is_canonical_and_sha256_stable() -> None:
    document = route_qualification_to_dict(synthetic_route_qualification_fixture())
    encoded = canonical_json_bytes(document)

    assert encoded == json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert b"\n" not in encoded
    assert sha256_document(document) == sha256_document(
        json.loads(encoded.decode("utf-8"))
    )


def test_contract_parser_rejects_unknown_missing_and_noncanonical_fields() -> None:
    fixture = route_qualification_to_dict(synthetic_route_qualification_fixture())

    unknown = dict(fixture, surprise=True)
    with pytest.raises(QualificationContractError, match="unknown_contract_field"):
        route_qualification_from_dict(unknown)

    missing = dict(fixture)
    missing.pop("manifest_digest")
    with pytest.raises(QualificationContractError, match="missing_contract_field"):
        route_qualification_from_dict(missing)

    invalid_digest = dict(fixture, manifest_digest="sha256:" + "A" * 64)
    with pytest.raises(QualificationContractError, match="invalid_manifest_digest"):
        route_qualification_from_dict(invalid_digest)

    missing_provenance = dict(fixture)
    missing_provenance.pop("placement_provenance")
    with pytest.raises(QualificationContractError, match="missing_contract_field"):
        route_qualification_from_dict(missing_provenance)


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("reason_codes", "not-an-array", "invalid_reason_codes"),
        (
            "dependency_lock_digests",
            "sha256:" + "0" * 64,
            "invalid_dependency_lock_digests",
        ),
        ("stage_bindings", {}, "invalid_stage_bindings"),
        ("route_ready", 0, "invalid_route_ready"),
    ],
)
def test_contract_parser_rejects_non_json_contract_types(
    field: str, value: object, code: str
) -> None:
    fixture = route_qualification_to_dict(synthetic_route_qualification_fixture())
    fixture[field] = value

    with pytest.raises(QualificationContractError, match=code):
        route_qualification_from_dict(fixture)


def test_public_parser_cannot_promote_a_document_to_route_ready() -> None:
    fixture = route_qualification_to_dict(synthetic_route_qualification_fixture())
    fixture["route_ready"] = True
    fixture["reason_codes"] = []

    with pytest.raises(QualificationContractError, match="qualified_record_requires_qualifier"):
        route_qualification_from_dict(fixture)


def test_only_qualifier_source_can_construct_route_ready_true() -> None:
    offenders: list[str] = []
    package = ROOT / "mycelium_qualification"
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "route_ready"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    offenders.append(path.name)
    assert offenders == ["qualifier.py"]


def test_checked_in_route_qualification_fixture_is_prominently_synthetic() -> None:
    path = ROOT / "contracts/compatibility-fixtures/route-qualification-v1.json"
    document = json.loads(path.read_text(encoding="utf-8"))

    assert document == route_qualification_to_dict(synthetic_route_qualification_fixture())
    assert document["evidence_class"] == "synthetic_test_fixture"
    assert document["route_ready"] is False
    assert document["qualification_id"].startswith("synthetic-test-fixture:")
