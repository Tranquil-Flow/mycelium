from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.mycelium_completion_audit import (
    DEFAULT_CHECKLIST,
    EXPECTED_DEPENDENCIES,
    NARROWED_PART_DEPENDENCIES,
    PLAN_PATH,
    audit,
)


ROOT = Path(__file__).resolve().parents[2]
NULL_BINDINGS = {
    "contract_digest": None,
    "model_digest": None,
    "representation_digest": None,
    "runtime_digest": None,
    "environment_digest": None,
    "authority_generation": None,
}
MODEL_GATES = {"A3", "A5", "A6", "A7", "A9", "A10", "A11", "A15"}
SOURCE_REQUIREMENTS = {
    "specification",
    "architecture_handover",
    "atomic_feature_commit",
}
# 2026-09-29 owner ruling R1: A10 and A12 depend on A4 at the ledger and on the named
# non-Windows A9 parts, which each dependent specification records as ``A9.<part>``.
RULED_DEPENDENCIES = {"A10": ["A4"], "A12": ["A4"]}
A10_NARROWED_A9_PARTS = (
    "separated_capability_authority",
    "signed_capability_binding",
    "unknown_remains_ineligible",
    "native_synthetic_evidence_separation",
    "class_specific_qualification",
    "runtime_backend_registry",
    "persistent_int8_execution",
    "qualified_activation_precision",
    "component_aware_performance_authority",
)
A12_NARROWED_A9_PARTS = (
    "separated_capability_authority",
    "signed_capability_binding",
    "unknown_remains_ineligible",
    "native_synthetic_evidence_separation",
    "class_specific_qualification",
    "runtime_backend_registry",
    "dynamic_ui_projection",
)


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _minimal_repo(root: Path, checklist: dict) -> None:
    plan_lines: list[str] = []
    for gate in checklist["gates"]:
        # Synthetic repositories carry the ruled (R1) direct dependencies; the live
        # checklist row refresh is owner-applied and is checked by the live test.
        if gate["gate_id"] in RULED_DEPENDENCIES:
            gate["dependencies"] = list(RULED_DEPENDENCIES[gate["gate_id"]])
        specification = root / gate["specification"]
        specification.parent.mkdir(parents=True, exist_ok=True)
        workspace_lines = [
            f"- **{label}:** live authority"
            for label in (
                "Inference",
                "Device Lab",
                "Network",
                "Nodes",
                "Plans",
                "Readiness",
                "Incidents",
                "Settings",
            )
        ]
        content = "\n".join(
            [
                f"**Gate:** {gate['gate_id']}",
                "## Outcome and claim boundary",
                "`design_only`",
                "physical positive",
                "physical negative",
                *workspace_lines,
                f"one atomic {gate['gate_id']} feature commit",
                *(
                    f"`{narrowed}.{part}`"
                    for narrowed, parts in sorted(
                        NARROWED_PART_DEPENDENCIES.get(gate["gate_id"], {}).items()
                    )
                    for part in parts
                ),
            ]
        ).encode("utf-8")
        specification.write_bytes(content)
        gate["evidence_bindings"][0]["artifact_digest"] = _digest(content)
        plan_lines.extend(
            [f"### {gate['gate_id']} — {gate['name']}", gate["specification"]]
        )
        if gate["handover"] is not None:
            handover = root / gate["handover"]
            handover.parent.mkdir(parents=True, exist_ok=True)
            handover.write_text("progress\n", "utf-8")
    plan = root / PLAN_PATH
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("\n".join(plan_lines), "utf-8")
    for gate in checklist["gates"]:
        if gate["state"] == "complete":
            _complete_gate(root, checklist, gate["gate_id"])
        elif gate["state"] == "design_only" and set(
            gate["completed_requirements"]
        ) == set(checklist["closure_requirements"]):
            _conclude_gate_design_only(root, checklist, gate["gate_id"])


def _evidence(root: Path, gate_id: str, requirement: str) -> dict:
    reference = f"test-evidence/{gate_id}/{requirement}.json"
    content = json.dumps(
        {"gate_id": gate_id, "requirement": requirement, "executed": "synthetic"},
        sort_keys=True,
    ).encode("utf-8")
    path = root / reference
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if requirement in SOURCE_REQUIREMENTS:
        scope = "source"
        provenance = "source"
        bindings = dict(NULL_BINDINGS)
        fresh_until = None
    else:
        scope = "model_runtime" if gate_id in MODEL_GATES else "runtime"
        provenance = "live"
        bindings = {
            "contract_digest": _digest(b"contract"),
            "model_digest": _digest(b"model") if scope == "model_runtime" else None,
            "representation_digest": (
                _digest(b"representation") if scope == "model_runtime" else None
            ),
            "runtime_digest": _digest(b"runtime"),
            "environment_digest": _digest(b"environment"),
            "authority_generation": 1,
        }
        fresh_until = "2099-08-18T00:00:00Z"
    return {
        "protocol": "mycelium.executed_requirement_evidence.v1",
        "requirement": requirement,
        "artifact_reference": reference,
        "artifact_digest": _digest(content),
        "provenance": provenance,
        "subject": f"{gate_id}:{requirement}",
        "observed_at": "2026-08-18T00:00:00Z",
        "fresh_until": fresh_until,
        "binding_scope": scope,
        "bindings": bindings,
    }


def _complete_gate(root: Path, checklist: dict, gate_id: str) -> None:
    gate = next(item for item in checklist["gates"] if item["gate_id"] == gate_id)
    requirements = list(checklist["closure_requirements"])
    gate.update(
        {
            "state": "complete",
            "completed_requirements": requirements,
            "partial_requirements": [],
            "pending_requirements": [],
            "evidence_bindings": [
                _evidence(root, gate_id, requirement) for requirement in requirements
            ],
        }
    )


def _conclude_gate_design_only(root: Path, checklist: dict, gate_id: str) -> None:
    _complete_gate(root, checklist, gate_id)
    gate = next(item for item in checklist["gates"] if item["gate_id"] == gate_id)
    gate["state"] = "design_only"


def _reset_gate_to_specification(checklist: dict, gate_id: str) -> None:
    gate = next(item for item in checklist["gates"] if item["gate_id"] == gate_id)
    specification_evidence = next(
        evidence
        for evidence in gate["evidence_bindings"]
        if evidence["requirement"] == "specification"
    )
    gate.update(
        {
            "state": "design_only",
            "completed_requirements": ["specification"],
            "partial_requirements": [],
            "pending_requirements": [
                requirement
                for requirement in checklist["closure_requirements"]
                if requirement != "specification"
            ],
            "evidence_bindings": [specification_evidence],
        }
    )


def _reset_gate_to_design_only(checklist: dict, gate_id: str) -> None:
    gate = next(item for item in checklist["gates"] if item["gate_id"] == gate_id)
    requirements = list(checklist["closure_requirements"])
    gate.update(
        {
            "state": "design_only",
            "blockers": ["synthetic test blocker"],
            "completed_requirements": ["specification"],
            "evidence_bindings": [
                evidence
                for evidence in gate["evidence_bindings"]
                if evidence["requirement"] == "specification"
            ],
            "partial_requirements": [],
            "pending_requirements": [
                requirement for requirement in requirements if requirement != "specification"
            ],
        }
    )


def _write_checklist(root: Path, checklist: dict) -> None:
    path = root / DEFAULT_CHECKLIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checklist), "utf-8")


def test_current_mycelium_completion_checklist_is_closed_and_truthful() -> None:
    result = audit(ROOT)
    assert result == {
        "checked_gates": 13,
        "findings": [],
        "ok": True,
        "primary_gate": "A6",
        "protocol": "mycelium.completion_checklist.v1",
    }


def test_audit_rejects_a_second_gate_in_progress(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["gates"][4]["state"] = "implemented_unintegrated"
    relative = "second-in-progress.json"
    (tmp_path / relative).write_text(json.dumps(checklist), "utf-8")

    result = audit(tmp_path, relative)

    assert result["ok"] is False
    assert "non_primary_gate_in_progress:A7" in result["findings"]


def test_audit_rejects_missing_and_extra_direct_dependencies(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["gates"][9]["dependencies"].remove("A4")
    checklist["gates"][9]["dependencies"].append("A11")
    checklist["gates"][11]["dependencies"].append("A3")
    relative = "bad-dependencies.json"
    (tmp_path / relative).write_text(json.dumps(checklist), "utf-8")

    result = audit(tmp_path, relative)

    assert result["ok"] is False
    assert "gate:A12:dependency_missing:A4" in result["findings"]
    assert "gate:A12:dependency_extra:A11" in result["findings"]
    assert "gate:A14:dependency_extra:A3" in result["findings"]


def test_completed_requirement_without_evidence_is_rejected(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    gate = checklist["gates"][3]
    gate["state"] = "implemented_unintegrated"
    gate["completed_requirements"].append("deterministic_positive")
    source = (
        gate["partial_requirements"]
        if "deterministic_positive" in gate["partial_requirements"]
        else gate["pending_requirements"]
    )
    source.remove("deterministic_positive")
    relative = "unbound-completion.json"
    (tmp_path / relative).write_text(json.dumps(checklist), "utf-8")

    result = audit(tmp_path, relative)

    assert result["ok"] is False
    assert "gate:A6:completed_evidence_missing:deterministic_positive" in result[
        "findings"
    ]


def test_complete_gate_state_without_executed_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    gate = checklist["gates"][3]
    gate.update(
        {
            "state": "complete",
            "completed_requirements": list(checklist["closure_requirements"]),
            "partial_requirements": [],
            "pending_requirements": [],
        }
    )
    relative = "unbound-complete-gate.json"
    (tmp_path / relative).write_text(json.dumps(checklist), "utf-8")

    result = audit(tmp_path, relative)

    assert result["ok"] is False
    assert "gate:A6:completed_evidence_missing:physical_positive" in result["findings"]
    assert "gate:A6:completed_evidence_missing:atomic_feature_commit" in result[
        "findings"
    ]


def test_completed_evidence_validates_all_required_bindings(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    _minimal_repo(tmp_path, checklist)
    gate = checklist["gates"][3]
    gate["completed_requirements"].append("deterministic_positive")
    gate["pending_requirements"].remove("deterministic_positive")
    evidence = _evidence(tmp_path, "A6", "deterministic_positive")
    evidence["protocol"] = "untrusted.protocol"
    evidence["artifact_digest"] = "sha256:" + "0" * 64
    evidence["provenance"] = "historical"
    evidence["subject"] = "A6:wrong"
    evidence["fresh_until"] = "2026-08-18T00:00:00Z"
    evidence["bindings"]["model_digest"] = None
    gate["evidence_bindings"].append(evidence)
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is False
    assert "gate:A6:evidence_protocol_invalid:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A6:evidence_digest_mismatch:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A6:executed_evidence_required:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A6:evidence_subject_invalid:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A6:evidence_freshness_window_invalid:deterministic_positive" in result[
        "findings"
    ]
    assert (
        "gate:A6:evidence_binding_invalid:deterministic_positive:model_digest"
        in result["findings"]
    )


def test_audit_accepts_atomic_advance_to_the_next_primary_gate(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A5"
    _reset_gate_to_specification(checklist, "A5")
    _minimal_repo(tmp_path, checklist)
    _complete_gate(tmp_path, checklist, "A4")
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is True
    assert result["primary_gate"] == "A5"


def test_completed_gate_retains_expired_live_evidence_as_historical_proof(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A5"
    _reset_gate_to_specification(checklist, "A5")
    _minimal_repo(tmp_path, checklist)
    _complete_gate(tmp_path, checklist, "A4")
    gate = checklist["gates"][1]
    for evidence in gate["evidence_bindings"]:
        if evidence["binding_scope"] != "source":
            evidence["fresh_until"] = "2026-08-18T00:00:01Z"
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is True
    assert result["primary_gate"] == "A5"


def test_concluded_design_only_gate_retains_expired_executed_evidence(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A6"
    _minimal_repo(tmp_path, checklist)
    _conclude_gate_design_only(tmp_path, checklist, "A5")
    gate = checklist["gates"][2]
    for evidence in gate["evidence_bindings"]:
        if evidence["binding_scope"] != "source":
            evidence["fresh_until"] = "2026-08-18T00:00:01Z"
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is True
    assert result["primary_gate"] == "A6"


def test_audit_accepts_dependency_ready_parallel_primary_gate(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A8"
    _reset_gate_to_design_only(checklist, "A4")
    _reset_gate_to_design_only(checklist, "A8")
    _minimal_repo(tmp_path, checklist)
    _complete_gate(tmp_path, checklist, "A3")
    checklist["gates"][5].update(
        {
            "state": "integrated_unqualified",
            "blockers": ["synthetic pending physical qualification"],
            "completed_requirements": ["specification"],
            "partial_requirements": [],
            "pending_requirements": [
                requirement
                for requirement in checklist["closure_requirements"]
                if requirement != "specification"
            ],
        }
    )
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is True
    assert result["primary_gate"] == "A8"


def test_generic_a12_can_close_with_a4_while_a9_and_a11_are_incomplete(
    tmp_path: Path,
) -> None:
    """R1: A12 closes on A4 plus its narrowed A9 parts; A9's Windows track may lag."""

    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A13"
    _minimal_repo(tmp_path, checklist)
    for gate_id in ("A3", "A4", "A8", "A12"):
        _complete_gate(tmp_path, checklist, gate_id)
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert checklist["gates"][6]["gate_id"] == "A9"
    assert checklist["gates"][6]["state"] == "design_only"
    assert checklist["gates"][8]["gate_id"] == "A11"
    assert checklist["gates"][8]["state"] == "design_only"
    assert result["ok"] is True
    assert result["primary_gate"] == "A13"


def test_a10_can_close_with_a4_while_a9_is_incomplete(tmp_path: Path) -> None:
    """R1: A10 closes on A4 plus its narrowed A9 parts; A11 then depends on A10."""

    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A11"
    _minimal_repo(tmp_path, checklist)
    for gate_id in ("A3", "A4", "A10"):
        _complete_gate(tmp_path, checklist, gate_id)
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert checklist["gates"][6]["gate_id"] == "A9"
    assert checklist["gates"][6]["state"] == "design_only"
    assert result["ok"] is True
    assert result["primary_gate"] == "A11"


def test_ruled_dependencies_narrow_only_a10_and_a12_and_keep_a9_elsewhere() -> None:
    assert EXPECTED_DEPENDENCIES["A10"] == {"A4"}
    assert EXPECTED_DEPENDENCIES["A12"] == {"A4"}
    assert EXPECTED_DEPENDENCIES["A11"] == {"A10"}
    assert EXPECTED_DEPENDENCIES["A13"] == {"A12"}
    assert EXPECTED_DEPENDENCIES["A9"] == {"A8"}
    assert "A9" in EXPECTED_DEPENDENCIES["A15"]
    assert NARROWED_PART_DEPENDENCIES == {
        "A10": {"A9": A10_NARROWED_A9_PARTS},
        "A12": {"A9": A12_NARROWED_A9_PARTS},
    }
    a9_decisions = {
        decision["decision_id"]
        for decision in json.loads(
            (ROOT / "tests" / "a9_acceptance" / "inventory.v1.json").read_text("utf-8")
        )["decisions"]
    }
    for parts in (A10_NARROWED_A9_PARTS, A12_NARROWED_A9_PARTS):
        assert set(parts) <= a9_decisions
        # The native accelerated Windows provider is never a narrowed dependency.
        assert "accelerated_windows_backend" not in parts


def test_live_specifications_record_every_narrowed_a9_part() -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    for gate in checklist["gates"]:
        narrowed = NARROWED_PART_DEPENDENCIES.get(gate["gate_id"], {})
        text = " ".join((ROOT / gate["specification"]).read_text("utf-8").split())
        for dependency, parts in narrowed.items():
            for part in parts:
                assert f"{dependency}.{part}" in text, (gate["gate_id"], part)


def test_audit_rejects_a_whole_gate_a9_dependency_on_a10_and_a12(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    _minimal_repo(tmp_path, checklist)
    for gate in checklist["gates"]:
        if gate["gate_id"] in RULED_DEPENDENCIES:
            gate["dependencies"].append("A9")
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is False
    assert "gate:A10:dependency_extra:A9" in result["findings"]
    assert "gate:A12:dependency_extra:A9" in result["findings"]


def test_audit_rejects_a_narrowing_that_drops_a_named_a9_part(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    _minimal_repo(tmp_path, checklist)
    for gate_id, part in (
        ("A10", "component_aware_performance_authority"),
        ("A12", "signed_capability_binding"),
    ):
        gate = next(item for item in checklist["gates"] if item["gate_id"] == gate_id)
        specification = tmp_path / gate["specification"]
        content = specification.read_bytes().replace(
            f"`A9.{part}`".encode("utf-8"), b""
        )
        specification.write_bytes(content)
        gate["evidence_bindings"][0]["artifact_digest"] = _digest(content)
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is False
    assert (
        "gate:A10:narrowed_dependency_unrecorded:A9.component_aware_performance_authority"
        in result["findings"]
    )
    assert (
        "gate:A12:narrowed_dependency_unrecorded:A9.signed_capability_binding"
        in result["findings"]
    )
