#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Build the owner-private A6 physical execution plan the recovery gate admits.

Network-free and execution-free: it reads the serving tree's source-owned digests
(the same paths ``mycelium_a6_case_observations`` binds every observation to),
emits the sixteen cases and three cleanup checks with the gate's trusted runner
argv, and validates the result with ``run_a6_recovery_gate.validate_plan`` before
writing it. Run it with the same interpreter that will run the gate, because the
gate pins every argv to its own ``sys.executable``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mycelium_a6_case_observations import (  # noqa: E402
    CONTRACT_MANIFEST_PATH,
    SOURCE_MANIFEST_PATH,
    SPEC_PATH,
)
from scripts.run_a6_recovery_gate import (  # noqa: E402
    EXPECTED_ORIGIN,
    ADMISSIBLE_PLACEMENTS,
    EXPECTED_PLACEMENTS,
    REQUIRED_CASES,
    REQUIRED_CLEANUP_CHECKS,
    TRUSTED_CASE_RUNNER,
    TRUSTED_CASE_RUNNER_V2,
    V2_CASES,
    bound_placements,
    validate_plan,
)

PLAN_PROTOCOL = "mycelium.a6_physical_recovery_execution.v1"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _case_argv(case_id: str, operator_plan: str) -> list[str]:
    if case_id in V2_CASES:
        return [
            sys.executable,
            str(TRUSTED_CASE_RUNNER_V2),
            "--case",
            case_id,
            "--contract-version",
            "v2",
            "--operator-plan",
            operator_plan,
        ]
    return [
        sys.executable,
        str(TRUSTED_CASE_RUNNER),
        "--case",
        case_id,
        "--operator-plan",
        operator_plan,
    ]


def build_plan(
    *,
    repo_root: Path,
    operator_plan: Path,
    evidence_root: Path,
    placements: Sequence[str] | None = None,
    role_binding: Any = None,
) -> dict[str, Any]:
    """Build the A6 execution plan.

    With ``role_binding`` (a verified binding of the pinned A6 requirements
    spec) the placements are exactly the bound members and the binding is
    carried in the plan; otherwise the legacy fleet placements apply.
    """
    if not Path(operator_plan).is_absolute():
        raise ValueError("a6_plan_operator_plan_not_absolute")
    if not Path(evidence_root).is_absolute():
        raise ValueError("a6_plan_evidence_root_not_absolute")
    manifest = json.loads((repo_root / SOURCE_MANIFEST_PATH).read_text("utf-8"))
    plan_path = str(operator_plan)
    document = {
        "protocol": PLAN_PROTOCOL,
        "source_digest": manifest["source_digest"],
        "spec_digest": _file_digest(repo_root / SPEC_PATH),
        "contract_digest": _file_digest(repo_root / CONTRACT_MANIFEST_PATH),
        "product_origin": EXPECTED_ORIGIN,
        "operator_plan": plan_path,
        "evidence_root": str(evidence_root),
        "expected_placements": (
            bound_placements(role_binding) if role_binding is not None
            else list(placements if placements is not None else EXPECTED_PLACEMENTS)
        ),
        "cases": [
            {
                "case_id": case_id,
                "argv": _case_argv(case_id, plan_path),
                "evidence_file": str(Path(evidence_root) / f"{case_id}.json"),
            }
            for case_id in REQUIRED_CASES
        ],
        "cleanup_checks": [
            {
                "check_id": check_id,
                "argv": [
                    sys.executable,
                    str(TRUSTED_CASE_RUNNER),
                    "--cleanup-check",
                    check_id,
                    "--operator-plan",
                    plan_path,
                ],
            }
            for check_id in REQUIRED_CLEANUP_CHECKS
        ],
    }
    if role_binding is not None:
        if placements is not None:
            raise ValueError("a6_plan_placements_conflict_with_role_binding")
        document["role_binding"] = json.loads(json.dumps(role_binding))
    validate_plan(document)
    return document


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    placement = parser.add_mutually_exclusive_group()
    placement.add_argument(
        "--placements",
        choices=[",".join(item) for item in ADMISSIBLE_PLACEMENTS],
        default=None,
        help="LEGACY fleet placement nodes, in plan order (default: "
        + ",".join(EXPECTED_PLACEMENTS) + ")",
    )
    placement.add_argument(
        "--role-binding",
        type=Path,
        help="verified role binding of the pinned A6 requirements spec; its "
        "entry and successors become the placements",
    )
    parser.add_argument(
        "--create-evidence-root",
        action="store_true",
        help="create the evidence root with mode 0700; refuses an existing root",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.output.exists():
        print("a6_plan_output_exists", file=sys.stderr)
        return 2
    if args.create_evidence_root and args.evidence_root.exists():
        print("a6_plan_evidence_root_exists", file=sys.stderr)
        return 2
    try:
        document = build_plan(
            repo_root=args.repo_root,
            operator_plan=args.operator_plan,
            evidence_root=args.evidence_root,
            placements=tuple(args.placements.split(",")) if args.placements else None,
            role_binding=(json.loads(args.role_binding.read_text("utf-8"))
                          if args.role_binding is not None else None),
        )
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    if args.create_evidence_root:
        args.evidence_root.mkdir(mode=0o700, parents=True)
        os.chmod(args.evidence_root, 0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(args.output), "source_digest": document["source_digest"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
