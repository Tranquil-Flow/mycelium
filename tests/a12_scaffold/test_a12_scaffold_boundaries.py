# SPDX-License-Identifier: AGPL-3.0-only
"""A12 scaffold boundary suite: explicit, honest not-implemented seams.

GREEN DISCIPLINE (read before trusting a pass in this file): every test here passes
because the refusals and honesty guards that remain exist — not because any A12
behaviour works.  Five execution seams are still deliberately unimplemented
(physical operation qualification, optional draft binding, runtime observation,
product projection, physical gate); these tests pin that they cannot silently
succeed, that their coded errors are closed and unique, and that the guards reject
the shortcuts the A12 specification and acceptance inventory forbid.

The three deterministic rung seams (enrollment, capability eligibility, artifact
readiness) landed in the deterministic half: they author closed rung-proof records
and are asserted absent from ``a12.BOUNDARIES`` below.  LANDING ONE OF THE REMAINING
FIVE is what removes it from this table — never a relaxed assertion here.

Nothing in this file is closure evidence.  ``EARNED_CLOSURE_REQUIREMENTS`` must stay
empty against the thirteen canonical names of ``scripts/mycelium_completion_audit.py``.

Run with:

    env -u PYTHONPATH python3.14 -m pytest tests/a12_scaffold -q

``PYTHONPATH`` is cleared because this host sets it to a Python 3.11 virtualenv whose
pytest plugins cannot load under Python 3.14; the flag changes nothing about the
code under test.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import mycelium_mobile.a12_activation as a12
from scripts.mycelium_completion_audit import REQUIREMENTS

ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION_PATH = ROOT / a12.SPECIFICATION
INVENTORY_PATH = ROOT / a12.ACCEPTANCE_INVENTORY
SCENARIOS_PATH = ROOT / a12.ACCEPTANCE_SCENARIOS

OBSERVATION: dict[str, object] = {
    "protocol": "mycelium.mobile_runtime_observation.v1",
    "platform_family": "android",
    "runtime_build_digest": "sha256:" + "1" * 64,
    "profile_generation": 3,
    "evidence_generation": 4,
    "source_kind": "native_device_observation",
    "observed_at_unix_ms": 1_754_000_000_000.0,
    "measurement_duration_ms": 600_000.0,
    "sample_count": 60,
    "authority": "a9_role_class_qualifier",
    "metrics": {
        "sustainable_memory_bytes": 1.5e9,
        "thermal_state": "unknown",
    },
}

CURRENT_PRIOR_RUNGS = {
    "signed_membership": "eligible",
    "capability_eligibility": "eligible",
    "artifact_readiness": "eligible",
}

# The rung seams that landed with the deterministic half.  They author closed rung
# documents instead of refusing, so they must be absent from ``a12.BOUNDARIES`` and
# their plausible payloads must not raise ``A12NotImplementedError`` any more.
LANDED_SEAMS = ("enroll_mobile_member", "qualify_capability_eligibility", "promote_assignment_artifact")

# How many execution seams are still deliberately unimplemented.
DECLARED_BOUNDARY_COUNT = 5

# One plausible payload per declared entry point.  Each must pass shape validation
# and then hit the coded refusal; none may return a value.  The three landed rung
# seams keep their payload too: it is used to prove they no longer refuse.
BOUNDARY_PAYLOADS: dict[str, dict[str, object]] = {
    "enroll_mobile_member": {
        "member_ref": "member:1",
        "platform_family": "android",
        "source_kind": "native_device_observation",
        "route_ready": False,
    },
    "qualify_capability_eligibility": {
        "member_ref": "member:1",
        "platform_family": "android",
        "role": "probe_contributor",
        "observation": OBSERVATION,
    },
    "promote_assignment_artifact": {
        "member_ref": "member:1",
        "assignment_ref": "assignment:1",
        "artifact_digest": "sha256:" + "0" * 64,
    },
    "qualify_mobile_operation": {
        "member_ref": "member:1",
        "role": "qualified_model_stage",
        "platform_family": "android",
        "runtime_build_digest": "sha256:" + "1" * 64,
        "prior_rung_decisions": CURRENT_PRIOR_RUNGS,
    },
    "bind_speculative_draft_worker": {
        "member_ref": "member:1",
        "a11_binding_state": "design_only",
    },
    "observe_mobile_runtime": {
        "member_ref": "member:1",
        "platform_family": "android",
        "runtime_build_digest": "sha256:" + "1" * 64,
    },
    "project_mobile_rungs": {"workspace": "device_lab", "member_ref": "member:1"},
    "run_mobile_physical_gate": {
        "platform_family": "android",
        "roles": ("probe_contributor",),
    },
}


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# The boundary itself
# ---------------------------------------------------------------------------


def test_scaffold_status_is_design_only_and_earns_no_closure_requirement() -> None:
    status = a12.scaffold_status()

    assert status["protocol"] == "mycelium.a12_scaffold.v1"
    assert status["gate_id"] == "A12"
    assert status["state"] == "design_only"
    assert status["earned_closure_requirements"] == []
    assert status["declared_contract_protocols"] == list(a12.DECLARED_CONTRACT_PROTOCOLS)
    assert "never returns success" in status["claim_boundary"]
    assert status["claim_boundary"] == a12.CLAIM_BOUNDARY


def test_boundary_codes_are_closed_unique_and_declared() -> None:
    names = [name for name, _ in a12.BOUNDARIES]
    codes = [code for _, code in a12.BOUNDARIES]

    assert len(names) == len(set(names)) == DECLARED_BOUNDARY_COUNT
    assert len(codes) == len(set(codes))
    assert set(codes) == set(a12.BOUNDARY_CODES)
    assert set(names) == set(BOUNDARY_PAYLOADS) - set(LANDED_SEAMS)
    for name in names:
        function = getattr(a12, name)
        assert function.__module__ == a12.__name__
        assert callable(function)


@pytest.mark.parametrize("entry_point,code", a12.BOUNDARIES)
def test_every_declared_boundary_refuses_with_its_coded_error(
    entry_point: str, code: str
) -> None:
    function = getattr(a12, entry_point)
    payload = BOUNDARY_PAYLOADS[entry_point]

    with pytest.raises(a12.A12NotImplementedError) as error:
        function(**payload)

    assert error.value.code == f"not_implemented:{code}"
    assert str(error.value) == f"not_implemented:{code}"
    assert isinstance(error.value, a12.A12ScaffoldError)
    assert not isinstance(error.value, a12.A12ContractError)


@pytest.mark.parametrize("entry_point", sorted(name for name, _ in a12.BOUNDARIES))
def test_no_boundary_returns_a_success_document(entry_point: str) -> None:
    function = getattr(a12, entry_point)
    returned: object = None
    raised = False

    try:
        returned = function(**BOUNDARY_PAYLOADS[entry_point])
    except a12.A12NotImplementedError:
        raised = True

    assert raised is True
    assert returned is None


@pytest.mark.parametrize("entry_point", LANDED_SEAMS)
def test_landed_rung_seams_author_closed_documents_instead_of_refusing(
    entry_point: str,
) -> None:
    """A landed seam leaves the boundary; it must author a closed rung document."""

    function = getattr(a12, entry_point)
    document: object = None
    refused = False

    try:
        document = function(**BOUNDARY_PAYLOADS[entry_point])
    except a12.A12NotImplementedError:
        refused = True

    assert refused is False
    assert isinstance(document, dict)
    assert document["protocol"] == a12.RUNG_PROOF_PROTOCOL
    assert document["physical_execution"] is False
    assert a12.validate_rung_proof(document) == document
    assert entry_point not in {name for name, _ in a12.BOUNDARIES}


def test_declared_contract_protocols_are_the_specifications_three_records() -> None:
    specification = SPECIFICATION_PATH.read_text("utf-8")

    assert len(a12.DECLARED_CONTRACT_PROTOCOLS) == 3
    for protocol in a12.DECLARED_CONTRACT_PROTOCOLS:
        assert protocol in specification
    assert "mycelium.mobile_runtime_observation.v1" in specification
    assert "mycelium.mobile_role_qualification.v1" in specification
    assert "mycelium.mobile_activation_qualification.v1" in specification


def test_pinned_digests_match_the_frozen_inputs() -> None:
    assert _digest(SPECIFICATION_PATH) == a12.SPECIFICATION_DIGEST
    assert _digest(INVENTORY_PATH) == a12.ACCEPTANCE_INVENTORY_DIGEST
    assert _digest(SCENARIOS_PATH) == a12.ACCEPTANCE_SCENARIOS_DIGEST


def test_scaffold_vocabulary_matches_the_frozen_inventory() -> None:
    inventory = json.loads(INVENTORY_PATH.read_text("utf-8"))
    rungs = inventory["eligibility_rungs"]

    assert tuple(rung["rung_id"] for rung in rungs) == a12.RUNG_IDS
    assert {rung["rung_id"]: rung["authority"] for rung in rungs} == a12.RUNG_AUTHORITIES
    assert {
        rung["rung_id"]: rung["grants"][0] for rung in rungs
    } == a12.RUNG_GRANTS
    assert tuple(inventory["required_gate_families"]) == a12.REQUIRED_GATE_FAMILIES
    assert tuple(inventory["ui_projections"][index]["workspace_id"] for index in range(8)) == (
        a12.WORKSPACES
    )
    android = inventory["platform_requirements"]["android"]
    assert android["architecture"] == a12.ANDROID_ARCHITECTURE
    assert android["minimum_device_families"] == a12.MINIMUM_ANDROID_FAMILIES
    apple = inventory["platform_requirements"]["apple_mobile"]
    assert tuple(apple["platform_families"]) == a12.APPLE_PLATFORM_FAMILIES
    assert apple["initial_inference_eligibility"] == a12.APPLE_INITIAL_ELIGIBILITY
    assert tuple(inventory["platform_requirements"]["excluded_normal_product_paths"]) == (
        a12.EXCLUDED_NORMAL_PRODUCT_PATHS
    )


def test_scaffold_earns_no_closure_requirement_against_the_completion_audit() -> None:
    assert len(REQUIREMENTS) == 13
    assert a12.EARNED_CLOSURE_REQUIREMENTS == frozenset()
    assert a12.EARNED_CLOSURE_REQUIREMENTS <= REQUIREMENTS
    for name in (
        "specification",
        "implementation",
        "physical_positive",
        "physical_negative",
        "ui_live_all_eight",
        "browser_second_session",
        "full_regressions_audits",
        "architecture_handover",
        "atomic_feature_commit",
    ):
        assert name in REQUIREMENTS
    assert a12.SCAFFOLD_STATE == "design_only"


# ---------------------------------------------------------------------------
# Implemented guards: the shortcuts A12 forbids are rejected, not ignored
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_kind,code",
    [
        ("native_device_observation", None),
        ("synthetic_conformance", "non_native_source_cannot_qualify"),
        ("termux_conformance", "non_native_source_cannot_qualify"),
        ("operator_declared", "non_native_source_cannot_qualify"),
        ("unknown", "source_kind_unknown_blocks_qualification"),
        ("simulator", "source_kind_invalid"),
        ("", "source_kind_invalid"),
        (None, "source_kind_invalid"),
    ],
)
def test_native_rungs_require_a_native_source_kind(
    source_kind: object, code: str | None
) -> None:
    if code is None:
        assert a12.require_native_observation(source_kind) == a12.NATIVE_SOURCE_KIND
        return
    with pytest.raises(a12.A12ContractError) as error:
        a12.require_native_observation(source_kind)
    assert error.value.code == code


def test_android_general_claim_requires_two_distinct_families() -> None:
    one = ["family:" + "a" * 16]
    duplicate = ["family:" + "a" * 16, "family:" + "a" * 16]
    two = ["family:" + "a" * 16, "sha256:" + "b" * 64]

    assert a12.require_android_family_evidence(two) == tuple(two)
    for families, code in (
        (one, "android_general_claim_requires_two_families"),
        (duplicate, "android_family_evidence_duplicated"),
        ([], "android_family_evidence_invalid"),
        (["pixel-8-pro"], "android_family_reference_invalid"),
        ("family:" + "a" * 16, "android_family_evidence_invalid"),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.require_android_family_evidence(families)
        assert error.value.code == code


def test_apple_platform_carryover_is_forbidden() -> None:
    assert a12.require_apple_platform_isolation("ios") == "ios"
    assert a12.require_apple_platform_isolation("ipados", "ineligible") == "ipados"
    for family, sibling, code in (
        ("android", None, "apple_platform_family_invalid"),
        ("ios", "qualified", "apple_platform_carryover_forbidden"),
        ("ipados", "eligible", "apple_platform_carryover_forbidden"),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.require_apple_platform_isolation(family, sibling)
        assert error.value.code == code


def test_excluded_development_paths_cannot_qualify() -> None:
    assert (
        a12.require_undivided_product_path("signed_native_application")
        == "signed_native_application"
    )
    assert (
        a12.require_undivided_product_path("embedded_iroh_transport")
        == "embedded_iroh_transport"
    )
    for path_kind, code in (
        ("termux", "excluded_development_path_cannot_qualify"),
        ("adb", "excluded_development_path_cannot_qualify"),
        ("ssh", "excluded_development_path_cannot_qualify"),
        ("manual_copy", "product_path_kind_invalid"),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.require_undivided_product_path(path_kind)
        assert error.value.code == code


def test_prior_rungs_must_be_current_and_later_rungs_cannot_author_earlier() -> None:
    assert (
        a12.require_rung_isolation("operation_qualification", CURRENT_PRIOR_RUNGS)
        == "operation_qualification"
    )
    assert (
        a12.require_rung_isolation(
            "artifact_readiness", {"signed_membership": "eligible"}
        )
        == "artifact_readiness"
    )
    assert (
        a12.require_rung_isolation(
            "operation_qualification",
            {
                "signed_membership": "eligible",
                "capability_eligibility": "eligible",
                "artifact_readiness": "not_applicable",
            },
        )
        == "operation_qualification"
    )
    for rung_id, prior, code in (
        ("not_a_rung", {}, "rung_id_invalid"),
        ("capability_eligibility", {}, "prior_rung_not_current"),
        (
            "operation_qualification",
            {"signed_membership": "eligible", "capability_eligibility": "stale"},
            "prior_rung_not_current",
        ),
        (
            "operation_qualification",
            {"signed_membership": "eligible", "capability_eligibility": "unknown"},
            "prior_rung_not_current",
        ),
        ("capability_eligibility", {"signed_membership": "maybe"}, "rung_decision_invalid"),
        (
            "capability_eligibility",
            {"operation_qualification": "eligible"},
            "later_rung_cannot_author_earlier",
        ),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.require_rung_isolation(rung_id, prior)
        assert error.value.code == code


def test_probe_role_records_closed_not_applicable_artifact_reason() -> None:
    record = a12.require_artifact_rung_decision(
        "probe_contributor", "not_applicable", "artifact_free_probe_role"
    )
    assert record == {
        "rung_id": "artifact_readiness",
        "decision": "not_applicable",
        "reason": "artifact_free_probe_role",
    }
    assert a12.require_artifact_rung_decision(
        "qualified_model_stage", "eligible"
    )["decision"] == "eligible"
    for role, decision, reason, code in (
        ("probe_contributor", "eligible", None, "artifact_free_probe_must_record_closed_not_applicable"),
        ("probe_contributor", "not_applicable", None, "artifact_not_applicable_reason_missing"),
        (
            "qualified_model_stage",
            "not_applicable",
            "artifact_free_probe_role",
            "artifact_required_role_cannot_record_not_applicable",
        ),
        ("tourist_role", "eligible", None, "mobile_role_invalid"),
        ("probe_contributor", "maybe", None, "rung_decision_invalid"),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.require_artifact_rung_decision(role, decision, reason)
        assert error.value.code == code


def test_route_ready_must_start_false() -> None:
    assert a12.require_route_ready_is_false(False) is False
    assert a12.ROUTE_READY_AT_ENROLLMENT is False
    for value in (True, None, 0, "false"):
        with pytest.raises(a12.A12ContractError) as error:
            a12.require_route_ready_is_false(value)
        assert error.value.code == "route_ready_must_start_false"


def test_observation_shape_validation_is_closed() -> None:
    assert a12.validate_mobile_runtime_observation(OBSERVATION) == OBSERVATION

    def mutate(**changes: object) -> dict[str, object]:
        document = dict(OBSERVATION)
        document.update(changes)
        return document

    unknown_field = mutate(device_brand="Pixel")
    nan_metric = mutate(metrics={"sustainable_memory_bytes": float("nan")})
    infinite_metric = mutate(metrics={"runtime_workspace_bytes": float("inf")})
    bad_source = mutate(source_kind="simulator")
    bad_protocol = mutate(protocol="mycelium.mobile_runtime_observation.v2")
    bad_platform = mutate(platform_family="windows")
    stale_generation = mutate(evidence_generation=0)
    instant = mutate(measurement_duration_ms=0.0)
    no_samples = mutate(sample_count=0)
    bad_build = mutate(runtime_build_digest="sha256:short")

    for document, code in (
        (unknown_field, "mobile_observation_fields_invalid"),
        (nan_metric, "mobile_observation_metric_invalid"),
        (infinite_metric, "mobile_observation_metric_invalid"),
        (bad_source, "mobile_observation_source_kind_invalid"),
        (bad_protocol, "mobile_observation_protocol_invalid"),
        (bad_platform, "mobile_observation_platform_invalid"),
        (stale_generation, "mobile_observation_generation_invalid"),
        (instant, "mobile_observation_measurement_invalid"),
        (no_samples, "mobile_observation_measurement_invalid"),
        (bad_build, "mobile_observation_build_invalid"),
    ):
        with pytest.raises(a12.A12ContractError) as error:
            a12.validate_mobile_runtime_observation(document)
        assert error.value.code == code

    missing_field = dict(OBSERVATION)
    del missing_field["source_kind"]
    with pytest.raises(a12.A12ContractError) as error:
        a12.validate_mobile_runtime_observation(missing_field)
    assert error.value.code == "mobile_observation_fields_invalid"


def test_optional_draft_claim_fails_closed_without_a_physically_closed_a11() -> None:
    for state in ("design_only", "missing", "stale", "disabled", "foreign_workload"):
        decision = a12.evaluate_optional_draft_claim(a11_binding_state=state)
        assert decision["claim_id"] == "speculative_draft_worker"
        assert decision["decision"] == "ineligible"
        assert decision["reason"] == "a11_target_authoritative_binding_absent"
        assert decision["failure_mode"] == "claim_rejected_gate_closure_unchanged"
        assert decision["other_roles_unchanged"] is True

    with pytest.raises(a12.A12NotImplementedError) as error:
        a12.evaluate_optional_draft_claim(
            a11_binding_state="physically_closed",
            a11_binding_digest="sha256:" + "0" * 64,
        )
    assert error.value.code == "not_implemented:draft_worker_qualification_absent"


def test_scaffold_changes_no_shared_surface() -> None:
    """This lane must not have touched contracts, the manifest or the checklist."""

    import subprocess

    changed = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    protected = [
        line
        for line in changed.splitlines()
        if "contracts/" in line
        or "completion-checklist" in line
        or "contract-manifest" in line
    ]
    assert protected == []
