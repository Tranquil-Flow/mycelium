#!/usr/bin/env python3
"""Validate the live A3-A15 completion checklist without promoting its claims."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any


PROTOCOL = "mycelium.completion_checklist.v1"
DEFAULT_CHECKLIST = "docs/handover/mycelium-completion-checklist.v1.json"
PLAN_PATH = "docs/superpowers/plans/2026-08-11-mycelium-completion-plan.md"
EXPECTED_GATES = tuple(f"A{index}" for index in range(3, 16))
EXPECTED_DEPENDENCIES = {
    "A3": {"A2"},
    "A4": {"A3"},
    "A5": {"A4"},
    "A6": {"A4"},
    "A7": {"A6"},
    "A8": {"A3"},
    "A9": {"A8"},
    "A10": {"A4"},
    "A11": {"A10"},
    "A12": {"A4", "A9"},
    "A13": {"A12"},
    "A14": {"A8"},
    "A15": {f"A{index}" for index in range(3, 15)},
}
EVIDENCE_PROTOCOL = "mycelium.executed_requirement_evidence.v1"
EVIDENCE_PROVENANCE = {"source", "live", "replay", "fixture", "historical"}
EVIDENCE_SCOPES = {"source", "runtime", "model_runtime"}
EVIDENCE_FIELDS = {
    "protocol",
    "requirement",
    "artifact_reference",
    "artifact_digest",
    "provenance",
    "subject",
    "observed_at",
    "fresh_until",
    "binding_scope",
    "bindings",
}
BINDING_FIELDS = {
    "contract_digest",
    "model_digest",
    "representation_digest",
    "runtime_digest",
    "environment_digest",
    "authority_generation",
}
SOURCE_REQUIREMENTS = {
    "specification",
    "architecture_handover",
    "atomic_feature_commit",
}
LIVE_REQUIREMENTS = {
    "implementation",
    "product_integration",
    "physical_positive",
    "physical_negative",
    "ui_live_all_eight",
    "browser_navigation_reconnect",
    "browser_second_session",
    "full_regressions_audits",
}
MODEL_BOUND_GATES = {"A3", "A5", "A6", "A7", "A10", "A11", "A15"}
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
WORKSPACES = {
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
}
WORKSPACE_LABELS = {
    "inference": "Inference",
    "device_lab": "Device Lab",
    "network": "Network",
    "nodes": "Nodes",
    "plans": "Plans",
    "readiness": "Readiness",
    "incidents": "Incidents",
    "settings": "Settings",
}
REQUIREMENTS = {
    "specification",
    "implementation",
    "product_integration",
    "deterministic_positive",
    "deterministic_negative",
    "physical_positive",
    "physical_negative",
    "ui_live_all_eight",
    "browser_navigation_reconnect",
    "browser_second_session",
    "full_regressions_audits",
    "architecture_handover",
    "atomic_feature_commit",
}
STATES = {
    "design_only",
    "implemented_unintegrated",
    "integrated_unqualified",
    "physically_qualified",
    "registered",
    "selected",
    "observed",
    "complete",
}
TOP_FIELDS = {
    "protocol",
    "claim_boundary",
    "updated_on",
    "primary_gate",
    "workspace_ids",
    "closure_requirements",
    "gates",
}
GATE_FIELDS = {
    "gate_id",
    "name",
    "dependencies",
    "specification",
    "state",
    "blockers",
    "completed_requirements",
    "evidence_bindings",
    "partial_requirements",
    "pending_requirements",
    "ui_workspaces",
    "handover",
}


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _relative_file(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("path_invalid")
    relative = PurePosixPath(value)
    if relative.is_absolute() or relative.as_posix() != value or ".." in relative.parts:
        raise ValueError("path_invalid")
    path = root / value
    resolved = path.resolve(strict=True)
    resolved.relative_to(root)
    if path.is_symlink() or not resolved.is_file():
        raise ValueError("path_not_regular")
    return resolved


def _string_list(value: object, *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ValueError("string_list_invalid")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("timestamp_invalid")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("timestamp_invalid")
    return parsed


def _digest_is_valid(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _audit_completed_evidence(
    root: Path,
    gate_id: str,
    gate_state: str,
    completed: set[str],
    value: object,
    findings: list[str],
) -> None:
    prefix = f"gate:{gate_id}"
    if not isinstance(value, list):
        findings.append(f"{prefix}:evidence_bindings_invalid")
        return
    by_requirement: dict[str, dict[str, Any]] = {}
    for index, evidence in enumerate(value):
        evidence_prefix = f"{prefix}:evidence:{index}"
        if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_FIELDS:
            findings.append(f"{evidence_prefix}:shape_invalid")
            continue
        requirement = evidence.get("requirement")
        if not isinstance(requirement, str) or requirement not in REQUIREMENTS:
            findings.append(f"{evidence_prefix}:requirement_invalid")
            continue
        if requirement in by_requirement:
            findings.append(f"{prefix}:evidence_duplicate:{requirement}")
            continue
        by_requirement[requirement] = evidence
        if evidence.get("protocol") != EVIDENCE_PROTOCOL:
            findings.append(f"{prefix}:evidence_protocol_invalid:{requirement}")
        if evidence.get("subject") != f"{gate_id}:{requirement}":
            findings.append(f"{prefix}:evidence_subject_invalid:{requirement}")
        try:
            artifact_path = _relative_file(root, evidence.get("artifact_reference"))
        except (OSError, ValueError):
            findings.append(f"{prefix}:evidence_artifact_unavailable:{requirement}")
        else:
            expected_digest = "sha256:" + hashlib.sha256(
                artifact_path.read_bytes()
            ).hexdigest()
            if evidence.get("artifact_digest") != expected_digest:
                findings.append(f"{prefix}:evidence_digest_mismatch:{requirement}")
        if not _digest_is_valid(evidence.get("artifact_digest")):
            findings.append(f"{prefix}:evidence_digest_invalid:{requirement}")

        provenance = evidence.get("provenance")
        if provenance not in EVIDENCE_PROVENANCE:
            findings.append(f"{prefix}:evidence_provenance_invalid:{requirement}")
        scope = evidence.get("binding_scope")
        if scope not in EVIDENCE_SCOPES:
            findings.append(f"{prefix}:evidence_binding_scope_invalid:{requirement}")
        if requirement in SOURCE_REQUIREMENTS:
            if scope != "source" or provenance != "source":
                findings.append(f"{prefix}:source_evidence_invalid:{requirement}")
        else:
            if scope == "source" or provenance in {"source", "historical"}:
                findings.append(f"{prefix}:executed_evidence_required:{requirement}")
            if requirement in LIVE_REQUIREMENTS and provenance != "live":
                findings.append(f"{prefix}:live_evidence_required:{requirement}")
            if gate_id in MODEL_BOUND_GATES and scope != "model_runtime":
                findings.append(f"{prefix}:model_binding_required:{requirement}")

        bindings = evidence.get("bindings")
        if not isinstance(bindings, dict) or set(bindings) != BINDING_FIELDS:
            findings.append(f"{prefix}:evidence_bindings_shape_invalid:{requirement}")
        else:
            required_bindings: set[str]
            if scope == "source":
                required_bindings = set()
            elif scope == "runtime":
                required_bindings = {
                    "contract_digest",
                    "runtime_digest",
                    "environment_digest",
                    "authority_generation",
                }
            else:
                required_bindings = set(BINDING_FIELDS)
            for binding_name in BINDING_FIELDS - {"authority_generation"}:
                binding = bindings.get(binding_name)
                if binding_name in required_bindings:
                    if not _digest_is_valid(binding):
                        findings.append(
                            f"{prefix}:evidence_binding_invalid:{requirement}:{binding_name}"
                        )
                elif binding is not None:
                    findings.append(
                        f"{prefix}:evidence_binding_not_applicable:{requirement}:{binding_name}"
                    )
            generation = bindings.get("authority_generation")
            if "authority_generation" in required_bindings:
                if (
                    not isinstance(generation, int)
                    or isinstance(generation, bool)
                    or generation < 1
                ):
                    findings.append(
                        f"{prefix}:evidence_binding_invalid:{requirement}:authority_generation"
                    )
            elif generation is not None:
                findings.append(
                    f"{prefix}:evidence_binding_not_applicable:{requirement}:authority_generation"
                )

        try:
            observed_at = _timestamp(evidence.get("observed_at"))
        except (TypeError, ValueError):
            observed_at = None
            findings.append(f"{prefix}:evidence_observed_at_invalid:{requirement}")
        fresh_until_value = evidence.get("fresh_until")
        if scope == "source":
            if fresh_until_value is not None:
                findings.append(f"{prefix}:source_evidence_expiry_invalid:{requirement}")
        else:
            try:
                fresh_until = _timestamp(fresh_until_value)
            except (TypeError, ValueError):
                findings.append(f"{prefix}:evidence_fresh_until_invalid:{requirement}")
            else:
                if observed_at is not None and fresh_until <= observed_at:
                    findings.append(
                        f"{prefix}:evidence_freshness_window_invalid:{requirement}"
                    )
                if gate_state != "complete" and fresh_until <= datetime.now(timezone.utc):
                    findings.append(f"{prefix}:evidence_expired:{requirement}")

    actual_requirements = set(by_requirement)
    for requirement in sorted(completed - actual_requirements):
        findings.append(f"{prefix}:completed_evidence_missing:{requirement}")
    for requirement in sorted(actual_requirements - completed):
        findings.append(f"{prefix}:evidence_for_uncompleted_requirement:{requirement}")


def audit(repo_root: str | Path, checklist: str = DEFAULT_CHECKLIST) -> dict[str, Any]:
    root = Path(repo_root).resolve(strict=True)
    findings: list[str] = []
    try:
        checklist_path = _relative_file(root, checklist)
        document = json.loads(
            checklist_path.read_text("utf-8"), object_pairs_hook=_strict_object
        )
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        return {
            "checked_gates": 0,
            "findings": [f"checklist_unavailable:{type(exc).__name__}"],
            "ok": False,
            "primary_gate": None,
            "protocol": PROTOCOL,
        }

    if not isinstance(document, dict) or set(document) != TOP_FIELDS:
        findings.append("checklist_shape_invalid")
        document = document if isinstance(document, dict) else {}
    if document.get("protocol") != PROTOCOL:
        findings.append("checklist_protocol_invalid")
    if not isinstance(document.get("claim_boundary"), str) or not document.get(
        "claim_boundary"
    ):
        findings.append("claim_boundary_invalid")
    try:
        workspace_ids = set(_string_list(document.get("workspace_ids")))
    except ValueError:
        workspace_ids = set()
        findings.append("workspace_ids_invalid")
    if workspace_ids != WORKSPACES:
        findings.append("workspace_set_invalid")
    try:
        closure_requirements = set(
            _string_list(document.get("closure_requirements"))
        )
    except ValueError:
        closure_requirements = set()
        findings.append("closure_requirements_invalid")
    if closure_requirements != REQUIREMENTS:
        findings.append("closure_requirement_set_invalid")
    try:
        plan = _relative_file(root, PLAN_PATH).read_text("utf-8")
    except (OSError, UnicodeError, ValueError):
        plan = ""
        findings.append("governing_plan_unavailable")

    gates_value = document.get("gates")
    if not isinstance(gates_value, list):
        gates_value = []
        findings.append("gates_invalid")
    gate_ids: list[str] = []
    gate_states: dict[str, str] = {}
    gate_dependencies: dict[str, set[str]] = {}
    for index, gate in enumerate(gates_value):
        prefix = f"gate:{index}"
        if not isinstance(gate, dict) or set(gate) != GATE_FIELDS:
            findings.append(f"{prefix}:shape_invalid")
            continue
        gate_id = gate.get("gate_id")
        if (
            not isinstance(gate_id, str)
            or not gate_id.startswith("A")
            or not gate_id[1:].isdigit()
        ):
            findings.append(f"{prefix}:id_invalid")
            continue
        prefix = f"gate:{gate_id}"
        gate_ids.append(gate_id)
        state = gate.get("state")
        if state not in STATES:
            findings.append(f"{prefix}:state_invalid")
        else:
            gate_states[gate_id] = state
        if not isinstance(gate.get("name"), str) or not gate["name"]:
            findings.append(f"{prefix}:name_invalid")
        try:
            dependencies = _string_list(gate.get("dependencies"))
        except ValueError:
            dependencies = []
            findings.append(f"{prefix}:dependencies_invalid")
        gate_dependencies[gate_id] = set(dependencies)
        for dependency in dependencies:
            if not dependency.startswith("A") or not dependency[1:].isdigit():
                findings.append(f"{prefix}:dependency_id_invalid:{dependency}")
            elif int(dependency[1:]) >= int(gate_id[1:]):
                findings.append(f"{prefix}:dependency_order_invalid:{dependency}")
        try:
            specification = gate.get("specification")
            specification_path = _relative_file(root, specification)
            specification_text = specification_path.read_text("utf-8")
        except (OSError, UnicodeError, ValueError):
            findings.append(f"{prefix}:specification_unavailable")
        else:
            normalized_specification = " ".join(specification_text.split())
            if f"### {gate_id} —" not in plan:
                findings.append(f"{prefix}:plan_gate_unavailable")
            if specification not in plan:
                findings.append(f"{prefix}:plan_specification_unbound")
            if f"**Gate:** {gate_id}" not in specification_text:
                findings.append(f"{prefix}:specification_gate_unbound")
            if "claim boundary" not in specification_text.lower():
                findings.append(f"{prefix}:specification_claim_boundary_missing")
            if "physical positive" not in specification_text.lower():
                findings.append(f"{prefix}:specification_physical_positive_missing")
            if "physical negative" not in specification_text.lower():
                findings.append(f"{prefix}:specification_physical_negative_missing")
            if gate_id != "A3" and "`design_only`" not in specification_text:
                findings.append(f"{prefix}:specification_design_boundary_missing")
            if f"atomic {gate_id}" not in normalized_specification:
                findings.append(f"{prefix}:specification_atomic_commit_missing")
            for workspace_id, label in WORKSPACE_LABELS.items():
                if f"**{label}:**" not in specification_text:
                    findings.append(
                        f"{prefix}:specification_workspace_missing:{workspace_id}"
                    )
        handover = gate.get("handover")
        if handover is not None:
            try:
                _relative_file(root, handover)
            except (OSError, ValueError):
                findings.append(f"{prefix}:handover_unavailable")
        try:
            _string_list(
                gate.get("blockers"),
                allow_empty=gate.get("state") == "complete",
            )
        except ValueError:
            findings.append(f"{prefix}:blockers_invalid")
        try:
            completed = set(
                _string_list(gate.get("completed_requirements"), allow_empty=True)
            )
            partial = set(
                _string_list(gate.get("partial_requirements"), allow_empty=True)
            )
            pending = set(
                _string_list(gate.get("pending_requirements"), allow_empty=True)
            )
        except ValueError:
            findings.append(f"{prefix}:requirement_partition_invalid")
            completed, partial, pending = set(), set(), set()
        if (
            completed & partial
            or completed & pending
            or partial & pending
            or completed | partial | pending != REQUIREMENTS
        ):
            findings.append(f"{prefix}:requirement_partition_invalid")
        _audit_completed_evidence(
            root,
            gate_id,
            str(gate.get("state")),
            completed,
            gate.get("evidence_bindings"),
            findings,
        )
        if gate.get("state") == "design_only" and completed != {"specification"}:
            findings.append(f"{prefix}:design_only_completion_invalid")
        if gate.get("state") == "complete" and completed != REQUIREMENTS:
            findings.append(f"{prefix}:complete_requirements_invalid")
        if gate.get("state") != "complete" and "atomic_feature_commit" in completed:
            findings.append(f"{prefix}:premature_commit_completion")
        try:
            ui_workspaces = set(_string_list(gate.get("ui_workspaces")))
        except ValueError:
            ui_workspaces = set()
            findings.append(f"{prefix}:ui_workspaces_invalid")
        if ui_workspaces != WORKSPACES:
            findings.append(f"{prefix}:ui_workspace_set_invalid")

    if tuple(gate_ids) != EXPECTED_GATES:
        findings.append("gate_sequence_invalid")
    for gate_id in EXPECTED_GATES:
        actual = gate_dependencies.get(gate_id, set())
        expected = EXPECTED_DEPENDENCIES[gate_id]
        for dependency in sorted(expected - actual):
            findings.append(f"gate:{gate_id}:dependency_missing:{dependency}")
        for dependency in sorted(actual - expected):
            findings.append(f"gate:{gate_id}:dependency_extra:{dependency}")

    for gate_id, state in gate_states.items():
        if state == "complete":
            for dependency in gate_dependencies.get(gate_id, set()):
                if (
                    dependency in gate_states
                    and gate_states[dependency] != "complete"
                ):
                    findings.append(
                        f"completed_dependency_incomplete:{gate_id}:{dependency}"
                    )

    primary_gate = document.get("primary_gate")
    if primary_gate not in EXPECTED_GATES:
        findings.append("primary_gate_invalid")
    else:
        if gate_states.get(primary_gate) == "complete":
            findings.append(f"primary_gate_already_complete:{primary_gate}")
        for dependency in gate_dependencies.get(primary_gate, set()):
            if dependency in gate_states and gate_states[dependency] != "complete":
                findings.append(
                    f"primary_dependency_incomplete:{primary_gate}:{dependency}"
                )
        for gate_id, state in gate_states.items():
            if gate_id != primary_gate and state not in {"design_only", "complete"}:
                findings.append(f"non_primary_gate_in_progress:{gate_id}")

    return {
        "checked_gates": len(gate_ids),
        "findings": findings,
        "ok": not findings,
        "primary_gate": primary_gate,
        "protocol": PROTOCOL,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--checklist", default=DEFAULT_CHECKLIST)
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args(argv)
    result = audit(arguments.repo_root, arguments.checklist)
    if arguments.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif result["ok"]:
        print(
            "Mycelium completion audit OK: "
            f"{result['checked_gates']} gates, primary {result['primary_gate']}"
        )
    else:
        print("Mycelium completion audit FAILED", file=sys.stderr)
        for finding in result["findings"]:
            print(f"- {finding}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
