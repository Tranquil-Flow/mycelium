#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Validate the live A3-A15 completion checklist without promoting its claims.

A blocker may name an artifact and assert something checkable about that
artifact inside a claim clause::

    [[claim:<kind> <argument>[ <argument>]]]

    path_exists <path>            a directory entry exists at <path>
    path_absent <path>            no directory entry exists at <path>
    marker_present <path> <token> the regular file at <path> contains <token>
    marker_absent <path> <token>  the regular file at <path> does not contain <token>
    commit_present <sha>          the commit is in the local object database
    commit_reachable <sha>        the commit is HEAD or an ancestor of HEAD
    commit_unreachable <sha>      the commit exists and is neither HEAD nor an ancestor

A claim clause is optional, because blockers are also free prose. A blocker that
carries no clause is not checked here, so a green audit does not mean every
blocker is true: it means every clause that was written down holds. A clause
that IS written down must parse and must hold against the artifact it names.
Contradicted, undecidable and malformed clauses are findings, reported as
``gate:<id>:blocker:<index>:claim_contradicted|claim_unresolved|claim_invalid``.

Paths are repository-relative inside the audited root (a claim never reaches
outside it); marker clauses compare against the file's whitespace-normalized
text; Git clauses are answered by the work tree that contains the audited root,
and a root that is not a Git work tree with a readable HEAD makes them
undecidable rather than true.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
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
    "A12": {"A4"},
    "A13": {"A12"},
    "A14": {"A8"},
    "A15": {f"A{index}" for index in range(3, 15)},
}
# 2026-09-29 owner ruling R1: A10's and A12's dependency on A9 is narrowed to A9's
# non-Windows parts; the native accelerated Windows provider moves to the Windows
# track.  The gate-level ledger dependency on A9 is therefore gone for both, but the
# consumed parts are not: each dependent specification must name every consumed part
# by its frozen A9 decision ID (``tests/a9_acceptance/inventory.v1.json``) as
# ``A9.<decision_id>``, so the narrowing can never silently become a deletion.
NARROWED_PART_DEPENDENCIES = {
    "A10": {
        "A9": (
            "separated_capability_authority",
            "signed_capability_binding",
            "unknown_remains_ineligible",
            "native_synthetic_evidence_separation",
            "class_specific_qualification",
            "runtime_backend_registry",
            "persistent_int8_execution",
            "qualified_activation_precision",
            "component_aware_performance_authority",
        ),
    },
    "A12": {
        "A9": (
            "separated_capability_authority",
            "signed_capability_binding",
            "unknown_remains_ineligible",
            "native_synthetic_evidence_separation",
            "class_specific_qualification",
            "runtime_backend_registry",
            "dynamic_ui_projection",
        ),
    },
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
MODEL_BOUND_GATES = {"A3", "A5", "A6", "A7", "A9", "A10", "A11", "A15"}
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
# A gate row may carry an optional closed ``slices`` list (lead ruling R5,
# 2026-10-07): a later slice of the same gate (``A14b`` of A14) is tracked under
# its parent row instead of as a new gate id.  Its id is the parent id plus one
# lowercase letter from ``b`` (the row itself is the first slice), its
# dependencies are the parent's, and it is audited like a gate row (state,
# requirement partition, executed evidence, blockers, workspaces) against its own
# specification, which must name the slice and its parent specification.
# A gate row with slices is complete only when every one of its slices is
# complete (lead ruling, 2026-10-07: the owner wants all A tasks done, so a
# parent row never closes over an unfinished slice).
SLICES_FIELD = "slices"
SLICE_FIELDS = (GATE_FIELDS - {"gate_id", "dependencies"}) | {"slice_id"}
SLICE_SUFFIX = re.compile(r"[b-z]\Z")
BLOCKER_CLAIM_MARKER = "[[claim:"
BLOCKER_CLAIM_SPAN = re.compile(r"\[\[claim:([^\[\]]*)\]\]")
BLOCKER_CLAIM_ARITY = {
    "path_exists": 1,
    "path_absent": 1,
    "marker_present": 2,
    "marker_absent": 2,
    "commit_present": 1,
    "commit_reachable": 1,
    "commit_unreachable": 1,
}
GIT_CLAIM_KINDS = frozenset(
    {"commit_present", "commit_reachable", "commit_unreachable"}
)
GIT_COMMIT = re.compile(r"[0-9a-f]{7,64}")


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


def blocker_claims(text: object) -> list[tuple[str, str, tuple[str, ...]]]:
    """Return the machine-checkable claim clauses of one blocker, in text order.

    Each entry is ``(status, kind, arguments)``. ``status`` is ``"claim"`` for a
    well-formed clause, which must then be resolved against the repository, or
    ``"invalid"`` for a claim marker that does not parse -- an unknown kind, the
    wrong number of arguments, a malformed Git revision, a path that is not a
    clean repository-relative path, or a marker that is never closed. ``kind``
    then carries the offending kind, or ``"malformed"`` when the marker itself
    could not be read.
    """

    if not isinstance(text, str):
        return []
    clauses: list[tuple[str, str, tuple[str, ...]]] = []
    spans: list[tuple[int, int]] = []
    for match in BLOCKER_CLAIM_SPAN.finditer(text):
        spans.append((match.start(), match.end()))
        parts = match.group(1).strip().split()
        if not parts:
            clauses.append(("invalid", "malformed", ()))
            continue
        kind = parts[0]
        if kind not in BLOCKER_CLAIM_ARITY:
            clauses.append(("invalid", kind, ()))
            continue
        arguments = tuple(parts[1:])
        if len(arguments) != BLOCKER_CLAIM_ARITY[kind]:
            clauses.append(("invalid", kind, ()))
            continue
        if kind in GIT_CLAIM_KINDS:
            if GIT_COMMIT.fullmatch(arguments[0]) is None:
                clauses.append(("invalid", kind, ()))
                continue
        else:
            relative = PurePosixPath(arguments[0])
            if (
                relative.is_absolute()
                or relative.as_posix() != arguments[0]
                or ".." in relative.parts
            ):
                clauses.append(("invalid", kind, ()))
                continue
        clauses.append(("claim", kind, arguments))
    for marker in re.finditer(re.escape(BLOCKER_CLAIM_MARKER), text):
        if not any(start <= marker.start() < end for start, end in spans):
            clauses.append(("invalid", "malformed", ()))
    return clauses


def _claim_relative_path(root: Path, value: str) -> Path | None:
    """Resolve a claim argument inside the repository, needing nothing to exist.

    ``None`` means the argument would name a path outside the audited root, which
    makes the clause invalid rather than false.
    """

    try:
        resolved = (root / value).resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return root / value


def _git_command(
    root: Path, *arguments: str
) -> subprocess.CompletedProcess[bytes] | None:
    """Run one read-only Git query inside the audited root, or ``None`` if it cannot run."""

    try:
        return subprocess.run(
            ["git", "--no-replace-objects", "-C", str(root), *arguments],
            env={
                key: value
                for key, value in os.environ.items()
                if not key.startswith("GIT_")
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_head(root: Path, cache: dict[str, object]) -> str | None:
    """Return the HEAD revision of the work tree containing the root, cached per audit."""

    if "head" not in cache:
        cache["head"] = None
        inside = _git_command(root, "rev-parse", "--is-inside-work-tree")
        if (
            inside is not None
            and inside.returncode == 0
            and inside.stdout.strip() == b"true"
        ):
            head = _git_command(root, "rev-parse", "--verify", "HEAD")
            if head is not None and head.returncode == 0:
                resolved = head.stdout.decode("utf-8", "replace").strip()
                cache["head"] = resolved or None
    value = cache["head"]
    return value if isinstance(value, str) else None


def _blocker_claim_holds(
    root: Path,
    kind: str,
    arguments: tuple[str, ...],
    cache: dict[str, object],
) -> bool | None:
    """Decide one claim against the repository; ``None`` when it cannot be decided.

    ``None`` is not a pass: the caller reports it as ``claim_unresolved``.
    """

    if kind in GIT_CLAIM_KINDS:
        if _git_head(root, cache) is None:
            return None
        commit = arguments[0]
        exists = _git_command(root, "cat-file", "-e", f"{commit}^{{commit}}")
        if exists is None:
            return None
        if kind == "commit_present":
            return exists.returncode == 0
        if exists.returncode != 0:
            # An object that is absent locally is not HEAD and not an ancestor.
            return False
        ancestor = _git_command(root, "merge-base", "--is-ancestor", commit, "HEAD")
        if ancestor is None or ancestor.returncode not in (0, 1):
            return None
        if kind == "commit_reachable":
            return ancestor.returncode == 0
        return ancestor.returncode != 0

    path = _claim_relative_path(root, arguments[0])
    if path is None:
        raise ValueError("claim_path_outside_repository")
    if kind == "path_exists":
        return path.exists() or path.is_symlink()
    if kind == "path_absent":
        return not (path.exists() or path.is_symlink())
    try:
        artifact = _relative_file(root, arguments[0]).read_text("utf-8")
    except (OSError, UnicodeError, ValueError):
        return None
    contained = arguments[1] in " ".join(artifact.split())
    return contained if kind == "marker_present" else not contained


def audit_blocker_claims(
    root: Path,
    gate_id: str,
    blockers: list[str],
    git_state: dict[str, object] | None = None,
) -> list[str]:
    """Return findings for the checkable claims carried by one gate's blockers.

    This checks truth against the artifacts a blocker names. It does not check
    prose, and it does not check a blocker that names nothing.
    """

    cache = {} if git_state is None else git_state
    findings: list[str] = []
    for index, blocker in enumerate(blockers):
        prefix = f"gate:{gate_id}:blocker:{index}"
        for status, kind, arguments in blocker_claims(blocker):
            if status == "invalid":
                findings.append(f"{prefix}:claim_invalid:{kind}")
                continue
            try:
                holds = _blocker_claim_holds(root, kind, arguments, cache)
            except ValueError:
                findings.append(f"{prefix}:claim_invalid:{kind}")
                continue
            if holds is None:
                findings.append(f"{prefix}:claim_unresolved:{kind}")
            elif not holds:
                findings.append(f"{prefix}:claim_contradicted:{kind}")
    return findings


def _audit_completed_evidence(
    root: Path,
    gate_id: str,
    gate_state: str,
    completed: set[str],
    value: object,
    findings: list[str],
) -> None:
    prefix = f"gate:{gate_id}"
    terminal_outcome = gate_state == "complete" or (
        gate_state == "design_only" and completed == REQUIREMENTS
    )
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
                if not terminal_outcome and fresh_until <= datetime.now(timezone.utc):
                    findings.append(f"{prefix}:evidence_expired:{requirement}")

    actual_requirements = set(by_requirement)
    for requirement in sorted(completed - actual_requirements):
        findings.append(f"{prefix}:completed_evidence_missing:{requirement}")
    for requirement in sorted(actual_requirements - completed):
        findings.append(f"{prefix}:evidence_for_uncompleted_requirement:{requirement}")


def _audit_row(
    root: Path,
    row_id: str,
    row: dict[str, Any],
    git_state: dict[str, object],
    findings: list[str],
) -> None:
    """Handover, blockers, requirement partition, evidence and workspaces of one row."""

    prefix = f"gate:{row_id}"
    handover = row.get("handover")
    if handover is not None:
        try:
            _relative_file(root, handover)
        except (OSError, ValueError):
            findings.append(f"{prefix}:handover_unavailable")
    try:
        blockers = _string_list(
            row.get("blockers"),
            allow_empty=row.get("state") == "complete",
        )
    except ValueError:
        findings.append(f"{prefix}:blockers_invalid")
    else:
        findings.extend(
            audit_blocker_claims(root, row_id, blockers, git_state)
        )
    try:
        completed = set(
            _string_list(row.get("completed_requirements"), allow_empty=True)
        )
        partial = set(
            _string_list(row.get("partial_requirements"), allow_empty=True)
        )
        pending = set(
            _string_list(row.get("pending_requirements"), allow_empty=True)
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
        row_id,
        str(row.get("state")),
        completed,
        row.get("evidence_bindings"),
        findings,
    )
    if row.get("state") == "design_only" and completed not in (
        {"specification"},
        REQUIREMENTS,
    ):
        findings.append(f"{prefix}:design_only_completion_invalid")
    if row.get("state") == "complete" and completed != REQUIREMENTS:
        findings.append(f"{prefix}:complete_requirements_invalid")
    if "atomic_feature_commit" in completed and completed != REQUIREMENTS:
        findings.append(f"{prefix}:premature_commit_completion")
    try:
        ui_workspaces = set(_string_list(row.get("ui_workspaces")))
    except ValueError:
        ui_workspaces = set()
        findings.append(f"{prefix}:ui_workspaces_invalid")
    if ui_workspaces != WORKSPACES:
        findings.append(f"{prefix}:ui_workspace_set_invalid")


def _audit_slices(
    root: Path,
    gate_id: str,
    gate: dict[str, Any],
    git_state: dict[str, object],
    findings: list[str],
) -> list[tuple[str, str]]:
    """Audit a gate row's optional ``slices``; returns ``(slice_id, state)`` pairs."""

    if SLICES_FIELD not in gate:
        return []
    value = gate[SLICES_FIELD]
    if not isinstance(value, list) or not value:
        findings.append(f"gate:{gate_id}:slices_invalid")
        return []
    parent_specification = gate.get("specification")
    parent_name = (
        PurePosixPath(parent_specification).name
        if isinstance(parent_specification, str)
        else None
    )
    audited: list[tuple[str, str]] = []
    previous = ""
    for index, row in enumerate(value):
        prefix = f"gate:{gate_id}:slice:{index}"
        if not isinstance(row, dict) or set(row) != SLICE_FIELDS:
            findings.append(f"{prefix}:shape_invalid")
            continue
        slice_id = row.get("slice_id")
        if (
            not isinstance(slice_id, str)
            or not slice_id.startswith(gate_id)
            or not SLICE_SUFFIX.fullmatch(slice_id[len(gate_id) :])
        ):
            findings.append(f"{prefix}:id_invalid")
            continue
        if slice_id <= previous:
            findings.append(f"{prefix}:order_invalid:{slice_id}")
            continue
        previous = slice_id
        prefix = f"gate:{slice_id}"
        state = row.get("state")
        if state not in STATES:
            findings.append(f"{prefix}:state_invalid")
        else:
            audited.append((slice_id, state))
        if not isinstance(row.get("name"), str) or not row["name"]:
            findings.append(f"{prefix}:name_invalid")
        try:
            specification_text = _relative_file(root, row.get("specification")).read_text(
                "utf-8"
            )
        except (OSError, UnicodeError, ValueError):
            findings.append(f"{prefix}:specification_unavailable")
        else:
            if f"**Gate:** {slice_id}" not in specification_text:
                findings.append(f"{prefix}:specification_gate_unbound")
            if parent_name is None or f"**Parent:** `{parent_name}`" not in specification_text:
                findings.append(f"{prefix}:specification_parent_unbound")
            if "claim boundary" not in specification_text.lower():
                findings.append(f"{prefix}:specification_claim_boundary_missing")
            if "`design_only`" not in specification_text:
                findings.append(f"{prefix}:specification_design_boundary_missing")
        _audit_row(root, slice_id, row, git_state, findings)
    return audited


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
    slice_states: dict[str, tuple[str, str]] = {}
    git_state: dict[str, object] = {}
    for index, gate in enumerate(gates_value):
        prefix = f"gate:{index}"
        if not isinstance(gate, dict) or set(gate) not in (
            GATE_FIELDS,
            GATE_FIELDS | {SLICES_FIELD},
        ):
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
            for narrowed_gate, parts in sorted(
                NARROWED_PART_DEPENDENCIES.get(gate_id, {}).items()
            ):
                for part in parts:
                    if f"{narrowed_gate}.{part}" not in normalized_specification:
                        findings.append(
                            f"{prefix}:narrowed_dependency_unrecorded:"
                            f"{narrowed_gate}.{part}"
                        )
        _audit_row(root, gate_id, gate, git_state, findings)
        for slice_id, slice_state in _audit_slices(root, gate_id, gate, git_state, findings):
            slice_states[slice_id] = (gate_id, slice_state)

    if tuple(gate_ids) != EXPECTED_GATES:
        findings.append("gate_sequence_invalid")
    for gate_id in EXPECTED_GATES:
        actual = gate_dependencies.get(gate_id, set())
        expected = EXPECTED_DEPENDENCIES[gate_id]
        for dependency in sorted(expected - actual):
            findings.append(f"gate:{gate_id}:dependency_missing:{dependency}")
        for dependency in sorted(actual - expected):
            findings.append(f"gate:{gate_id}:dependency_extra:{dependency}")

    for slice_id, (parent_id, state) in slice_states.items():
        if gate_states.get(parent_id) == "complete" and state != "complete":
            findings.append(f"completed_gate_slice_incomplete:{parent_id}:{slice_id}")

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

    terminal_design_only = {
        gate.get("gate_id")
        for gate in gates_value
        if isinstance(gate, dict)
        and gate.get("state") == "design_only"
        and set(gate.get("completed_requirements", ())) == REQUIREMENTS
    }
    primary_gate = document.get("primary_gate")
    if primary_gate not in EXPECTED_GATES:
        findings.append("primary_gate_invalid")
    else:
        if gate_states.get(primary_gate) == "complete":
            findings.append(f"primary_gate_already_complete:{primary_gate}")
        if primary_gate in terminal_design_only:
            findings.append(f"primary_gate_already_concluded:{primary_gate}")
        for dependency in gate_dependencies.get(primary_gate, set()):
            if dependency in gate_states and gate_states[dependency] != "complete":
                findings.append(
                    f"primary_dependency_incomplete:{primary_gate}:{dependency}"
                )
        for gate_id, state in gate_states.items():
            if gate_id != primary_gate and state not in {"design_only", "complete"}:
                findings.append(f"non_primary_gate_in_progress:{gate_id}")
        for slice_id, (parent_id, state) in slice_states.items():
            if parent_id != primary_gate and state not in {"design_only", "complete"}:
                findings.append(f"non_primary_gate_in_progress:{slice_id}")

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
