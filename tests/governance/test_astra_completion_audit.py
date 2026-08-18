from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.astra_completion_audit import DEFAULT_CHECKLIST, PLAN_PATH, audit


ROOT = Path(__file__).resolve().parents[2]
NULL_BINDINGS = {
    "contract_digest": None,
    "model_digest": None,
    "representation_digest": None,
    "runtime_digest": None,
    "environment_digest": None,
    "authority_generation": None,
}
MODEL_GATES = {"A3", "A5", "A6", "A7", "A10", "A11", "A15"}
SOURCE_REQUIREMENTS = {
    "specification",
    "architecture_handover",
    "atomic_feature_commit",
}


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _minimal_repo(root: Path, checklist: dict) -> None:
    plan_lines: list[str] = []
    for gate in checklist["gates"]:
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


def _write_checklist(root: Path, checklist: dict) -> None:
    path = root / DEFAULT_CHECKLIST
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checklist), "utf-8")


def test_current_astra_completion_checklist_is_closed_and_truthful() -> None:
    result = audit(ROOT)
    assert result == {
        "checked_gates": 13,
        "findings": [],
        "ok": True,
        "primary_gate": "A3",
        "protocol": "mycelium.astra_completion_checklist.v1",
    }


def test_audit_rejects_a_second_gate_in_progress(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["gates"][1]["state"] = "implemented_unintegrated"
    relative = "second-in-progress.json"
    (tmp_path / relative).write_text(json.dumps(checklist), "utf-8")

    result = audit(tmp_path, relative)

    assert result["ok"] is False
    assert "non_primary_gate_in_progress:A4" in result["findings"]


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
    gate = checklist["gates"][0]
    gate["completed_requirements"].append("deterministic_positive")
    gate["partial_requirements"].remove("deterministic_positive")
    relative = "unbound-completion.json"
    (tmp_path / relative).write_text(json.dumps(checklist), "utf-8")

    result = audit(tmp_path, relative)

    assert result["ok"] is False
    assert "gate:A3:completed_evidence_missing:deterministic_positive" in result[
        "findings"
    ]


def test_complete_gate_state_without_executed_evidence_is_rejected(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    gate = checklist["gates"][0]
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
    assert "gate:A3:completed_evidence_missing:physical_positive" in result["findings"]
    assert "gate:A3:completed_evidence_missing:atomic_feature_commit" in result[
        "findings"
    ]


def test_completed_evidence_validates_all_required_bindings(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    _minimal_repo(tmp_path, checklist)
    gate = checklist["gates"][0]
    gate["completed_requirements"].append("deterministic_positive")
    gate["partial_requirements"].remove("deterministic_positive")
    evidence = _evidence(tmp_path, "A3", "deterministic_positive")
    evidence["protocol"] = "untrusted.protocol"
    evidence["artifact_digest"] = "sha256:" + "0" * 64
    evidence["provenance"] = "historical"
    evidence["subject"] = "A3:wrong"
    evidence["fresh_until"] = "2026-08-18T00:00:00Z"
    evidence["bindings"]["model_digest"] = None
    gate["evidence_bindings"].append(evidence)
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is False
    assert "gate:A3:evidence_protocol_invalid:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A3:evidence_digest_mismatch:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A3:executed_evidence_required:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A3:evidence_subject_invalid:deterministic_positive" in result[
        "findings"
    ]
    assert "gate:A3:evidence_freshness_window_invalid:deterministic_positive" in result[
        "findings"
    ]
    assert (
        "gate:A3:evidence_binding_invalid:deterministic_positive:model_digest"
        in result["findings"]
    )


def test_audit_accepts_atomic_advance_to_the_next_primary_gate(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A4"
    _minimal_repo(tmp_path, checklist)
    _complete_gate(tmp_path, checklist, "A3")
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert result["ok"] is True
    assert result["primary_gate"] == "A4"


def test_audit_accepts_dependency_ready_parallel_primary_gate(tmp_path: Path) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A8"
    _minimal_repo(tmp_path, checklist)
    _complete_gate(tmp_path, checklist, "A3")
    checklist["gates"][5].update(
        {
            "state": "integrated_unqualified",
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


def test_generic_a12_can_close_with_a4_and_a9_while_a11_is_incomplete(
    tmp_path: Path,
) -> None:
    checklist = json.loads((ROOT / DEFAULT_CHECKLIST).read_text("utf-8"))
    checklist["primary_gate"] = "A13"
    _minimal_repo(tmp_path, checklist)
    for gate_id in ("A3", "A4", "A8", "A9", "A12"):
        _complete_gate(tmp_path, checklist, gate_id)
    _write_checklist(tmp_path, checklist)

    result = audit(tmp_path)

    assert checklist["gates"][8]["gate_id"] == "A11"
    assert checklist["gates"][8]["state"] == "design_only"
    assert result["ok"] is True
    assert result["primary_gate"] == "A13"
