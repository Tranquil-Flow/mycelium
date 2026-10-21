#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Fail-closed A6 physical recovery qualification coordinator.

``--plan`` is network-free preparation. ``--execute`` remains inaccessible
without the exact fleet grant and an owner-private, exact-candidate plan.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

EXACT_FLEET_GRANT = "FLEET GRANTED TO A6"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
REQUIRED_CASES = (
    "ordinary_browser_source_failure_recovery",
    "incompatible_successor",
    "stale_qualification",
    "foreign_qualification",
    "unexecuted_qualification",
    "mismatched_path_attempt",
    "no_successor",
    "successor_prefill_failure",
    "cancellation_before_recovery",
    "cancellation_during_prefill",
    "timeout_during_recovery",
    "exhausted_recovery_attempts",
    "stale_source_output_after_fencing",
    "cleanup_reservation_release",
    "cleanup_authority_failure",
    "shutdown_during_recovery",
)
REQUIRED_CLEANUP_CHECKS = (
    "local_product_port",
    "physical_processes",
    "reservations",
)
# LEGACY fleet placements (owner ruling 2026-10-05: the R7 fleet's third member
# is the third A6 placement, so the exhausted-attempts case has a real second
# disjoint successor; the two-node topology stays admissible and can only fail
# that case).  Kept so the old run-day sheets still validate.  A plan built
# with ``--role-binding`` instead carries a verified binding of the pinned A6
# requirements spec (``mycelium_gate_requirements/specs/a6.v1.json``: one
# entry and one or two successors, each able to hold the complete model) and
# its placements are exactly the bound members, whatever devices they are.
LEGACY_EXPECTED_PLACEMENTS = ("node-0", "node-2", "node-3")
LEGACY_ADMISSIBLE_PLACEMENTS = (("node-0", "node-2"), LEGACY_EXPECTED_PLACEMENTS)
EXPECTED_PLACEMENTS = LEGACY_EXPECTED_PLACEMENTS
ADMISSIBLE_PLACEMENTS = LEGACY_ADMISSIBLE_PLACEMENTS
REQUIREMENTS_SPEC = "mycelium_gate_requirements/specs/a6.v1.json"
EXPECTED_ORIGIN = "http://127.0.0.1:8787"
TRUSTED_CASE_RUNNER = Path(__file__).resolve().with_name("run_a6_recovery_case.py")
# Owner ruling R6: the cleanup-authority-failure case is admitted under the
# owner-approved v2 contract (docs/superpowers/specs/
# 2026-09-25-mycelium-a6-cleanup-authority-v2.md, clause amended 2026-09-27).
# The frozen v1 runner stays admissible for it; its v1 predicate is unchanged.
TRUSTED_CASE_RUNNER_V2 = Path(__file__).resolve().with_name("run_a6_recovery_case_v2.py")
V2_CASES = ("cleanup_authority_failure",)
V1_RECORD_PROTOCOL = "mycelium.a6_physical_recovery_case.v1"
V2_RECORD_PROTOCOL = "mycelium.a6_physical_recovery_case.v2"


def _fail(code: str) -> None:
    raise ValueError(code)


def _absolute_path(value: object, code: str) -> Path:
    if not isinstance(value, str) or not value:
        _fail(code)
    path = Path(value)
    if not path.is_absolute():
        _fail(code)
    return path


def _argv(value: object, code: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item for item in value)
    ):
        _fail(code)
    if not Path(value[0]).is_absolute():
        _fail(code)
    return tuple(value)


def _gate_requirements():
    root = str(Path(__file__).resolve().parents[1])
    if root not in sys.path:
        sys.path.insert(0, root)
    from mycelium_gate_requirements import binding, spec

    return binding, spec


def bound_placements(role_binding: Any) -> list[str]:
    """Placements of a verified binding of the pinned A6 spec (entry first)."""

    binding_module, spec_module = _gate_requirements()
    try:
        binding = binding_module.verify_binding(
            role_binding, spec=spec_module.load_pinned_spec("A6"))
    except ValueError:
        _fail("physical_plan_role_binding_invalid")
    entry = binding_module.role_node_ids(binding, "entry")
    successors = binding_module.role_node_ids(binding, "successor")
    placements = [*entry, *successors]
    if len(entry) != 1 or not successors or len(set(placements)) != len(placements):
        _fail("physical_plan_role_binding_invalid")
    return placements


def validate_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    if document.get("protocol") != "mycelium.a6_physical_recovery_execution.v1":
        _fail("physical_plan_protocol_invalid")
    for field in ("source_digest", "spec_digest", "contract_digest"):
        if not isinstance(document.get(field), str) or DIGEST.fullmatch(document[field]) is None:
            _fail(f"physical_plan_{field}_invalid")
    if document.get("product_origin") != EXPECTED_ORIGIN:
        _fail("physical_plan_product_origin_invalid")
    _absolute_path(document.get("operator_plan"), "physical_plan_operator_plan_invalid")
    evidence_root = _absolute_path(
        document.get("evidence_root"), "physical_plan_evidence_root_invalid"
    )
    if "role_binding" in document:
        if document.get("expected_placements") != bound_placements(document["role_binding"]):
            _fail("physical_plan_placements_invalid")
    elif document.get("expected_placements") not in [
        list(placements) for placements in LEGACY_ADMISSIBLE_PLACEMENTS
    ]:
        _fail("physical_plan_placements_invalid")

    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list):
        _fail("physical_cases_invalid")
    case_ids = tuple(
        item.get("case_id") if isinstance(item, Mapping) else None for item in raw_cases
    )
    if case_ids != REQUIRED_CASES:
        _fail("physical_case_set_mismatch")
    cases: list[dict[str, Any]] = []
    for item in raw_cases:
        assert isinstance(item, Mapping)
        case_id = str(item["case_id"])
        evidence_file = _absolute_path(
            item.get("evidence_file"), f"physical_case_evidence_invalid:{case_id}"
        )
        if evidence_file.parent != evidence_root:
            _fail(f"physical_case_evidence_root_mismatch:{case_id}")
        argv = _argv(item.get("argv"), f"physical_case_argv_invalid:{case_id}")
        expected_argv = (
            sys.executable,
            str(TRUSTED_CASE_RUNNER),
            "--case",
            case_id,
            "--operator-plan",
            str(document["operator_plan"]),
        )
        expected_v2_argv = (
            sys.executable,
            str(TRUSTED_CASE_RUNNER_V2),
            "--case",
            case_id,
            "--contract-version",
            "v2",
            "--operator-plan",
            str(document["operator_plan"]),
        )
        if argv == expected_argv:
            contract_version = "v1"
        elif case_id in V2_CASES and argv == expected_v2_argv:
            contract_version = "v2"
        else:
            _fail(f"physical_case_runner_untrusted:{case_id}")
        cases.append(
            {
                "case_id": case_id,
                "argv": list(argv),
                "evidence_file": str(evidence_file),
                "contract_version": contract_version,
            }
        )

    raw_cleanup = document.get("cleanup_checks")
    if not isinstance(raw_cleanup, list):
        _fail("physical_cleanup_checks_invalid")
    cleanup_ids = tuple(
        item.get("check_id") if isinstance(item, Mapping) else None
        for item in raw_cleanup
    )
    if cleanup_ids != REQUIRED_CLEANUP_CHECKS:
        _fail("physical_cleanup_check_set_mismatch")
    cleanup = []
    for item in raw_cleanup:
        assert isinstance(item, Mapping)
        check_id = str(item["check_id"])
        argv = _argv(
            item.get("argv"),
            f"physical_cleanup_argv_invalid:{check_id}",
        )
        expected_argv = (
            sys.executable,
            str(TRUSTED_CASE_RUNNER),
            "--cleanup-check",
            check_id,
            "--operator-plan",
            str(document["operator_plan"]),
        )
        if argv != expected_argv:
            _fail(f"physical_cleanup_runner_untrusted:{check_id}")
        cleanup.append({"check_id": check_id, "argv": list(argv)})
    return {
        **dict(document),
        "evidence_root": str(evidence_root),
        "cases": cases,
        "cleanup_checks": cleanup,
    }


def preparation_plan() -> dict[str, Any]:
    return {
        "protocol": "mycelium.a6_physical_recovery_plan.v1",
        "executed": False,
        "passed": False,
        "fleet_contact_attempted": False,
        "qualification_claim": False,
        "expected_product_origin": EXPECTED_ORIGIN,
        "expected_product_port": 8787,
        "expected_placements": list(EXPECTED_PLACEMENTS),
        "admissible_placements": [list(item) for item in ADMISSIBLE_PLACEMENTS],
        "placements_authority": "legacy_fleet_or_role_binding",
        "requirements_spec": REQUIREMENTS_SPEC,
        "requirements_spec_digest": _gate_requirements()[1].load_pinned_spec("A6").digest,
        "required_cases": list(REQUIRED_CASES),
        "v2_admissible_cases": list(V2_CASES),
        "required_cleanup_checks": list(REQUIRED_CLEANUP_CHECKS),
        "evidence_root_template": str(
            Path.home() / ".mycelium" / "evidence" / "a6" / "<source-digest>"
        ),
        "commands_after_grant": {
            "serve": [
                "/opt/homebrew/bin/python3.14",
                "-m",
                "mycelium_demo",
                "serve",
                "--mode",
                "live",
                "--host",
                "127.0.0.1",
                "--port",
                "8787",
                "--operator-plan",
                "<fresh-owner-private-operator-plan>",
                "--seed-state-root",
                "<fresh-owner-private-seed-state-root>",
                "--deployment-dir",
                "<exact-staged-deployment>",
                "--a6-fault-control-port",
                "<a6-fault-control-port>",
            ],
            "physical_gate": [
                "/usr/bin/env",
                "MYCELIUM_A6_FLEET_GRANT=FLEET GRANTED TO A6",
                "/opt/homebrew/bin/python3.14",
                "scripts/run_a6_recovery_gate.py",
                "--execute",
                "--plan-file",
                "<fresh-owner-private-a6-execution-plan>",
            ],
            "browser_gate": [
                "/usr/bin/env",
                "MYCELIUM_A6_FLEET_GRANT=FLEET GRANTED TO A6",
                "MYCELIUM_A6_PRODUCT_ORIGIN=http://127.0.0.1:8787",
                "MYCELIUM_A6_BROWSER_EVIDENCE=<owner-private-browser-evidence-file>",
                "MYCELIUM_A6_POSITIVE_FAULT_URL=http://127.0.0.1:8787/<positive-fault-control>",
                "MYCELIUM_A6_NEGATIVE_FAULT_URL=http://127.0.0.1:8787/<negative-fault-control>",
                "npm",
                "run",
                "test:a6-live-browser",
            ],
        },
    }


def _require_private_root(path: Path) -> None:
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        _fail("physical_evidence_root_mode_invalid")
    if info.st_uid != os.getuid():
        _fail("physical_evidence_root_owner_invalid")


def _verify_case_record(
    path: Path, case_id: str, plan: Mapping[str, Any], contract_version: str = "v1"
) -> None:
    info = path.stat()
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        _fail(f"physical_case_record_permissions_invalid:{case_id}")
    record = json.loads(path.read_text())
    if (
        record.get("case_id") != case_id
        or record.get("executed") is not True
        or record.get("result") != "passed"
    ):
        _fail(f"physical_case_record_not_passing:{case_id}")
    expected_protocol = V2_RECORD_PROTOCOL if contract_version == "v2" else V1_RECORD_PROTOCOL
    if record.get("protocol") != expected_protocol or (
        contract_version == "v2" and record.get("contract_version") != "v2"
    ):
        _fail(f"physical_case_record_contract_mismatch:{case_id}")
    for field in ("source_digest", "spec_digest", "contract_digest"):
        if record.get(field) != plan[field]:
            _fail(f"physical_case_record_binding_mismatch:{case_id}:{field}")


def execute(plan: Mapping[str, Any]) -> dict[str, Any]:
    validated = validate_plan(plan)
    evidence_root = Path(validated["evidence_root"])
    _require_private_root(evidence_root)
    if not Path(validated["operator_plan"]).is_file():
        _fail("physical_operator_plan_missing")
    completed: list[str] = []
    cleanup_results: list[dict[str, Any]] = []
    failure: str | None = None
    try:
        for case in validated["cases"]:
            result = subprocess.run(
                case["argv"],
                check=False,
                env={
                    **os.environ,
                    "MYCELIUM_A6_CASE_EVIDENCE_FILE": case["evidence_file"],
                },
            )
            if result.returncode != 0:
                _fail(f"physical_case_command_failed:{case['case_id']}")
            _verify_case_record(
                Path(case["evidence_file"]),
                case["case_id"],
                validated,
                case["contract_version"],
            )
            completed.append(case["case_id"])
    except (OSError, ValueError, json.JSONDecodeError) as error:
        failure = str(error)
    finally:
        for check in validated["cleanup_checks"]:
            try:
                result = subprocess.run(check["argv"], check=False)
                cleanup_results.append(
                    {"check_id": check["check_id"], "passed": result.returncode == 0}
                )
            except OSError:
                cleanup_results.append({"check_id": check["check_id"], "passed": False})
    passed = (
        failure is None
        and tuple(completed) == REQUIRED_CASES
        and all(item["passed"] for item in cleanup_results)
    )
    return {
        "protocol": "mycelium.a6_physical_recovery_gate.v1",
        "executed": True,
        "passed": passed,
        "result": "passed" if passed else "failed",
        "source_digest": validated["source_digest"],
        "spec_digest": validated["spec_digest"],
        "contract_digest": validated["contract_digest"],
        "completed_cases": completed,
        "case_contracts": {
            case["case_id"]: case["contract_version"] for case in validated["cases"]
        },
        "cleanup_checks": cleanup_results,
        "failure_code": failure,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--execute", action="store_true")
    parser.add_argument("--plan-file", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.plan:
        print(json.dumps(preparation_plan(), indent=2, sort_keys=True))
        return 0
    # Deliberately gate before plan access, parsing, process launch, or network work.
    if os.environ.get("MYCELIUM_A6_FLEET_GRANT") != EXACT_FLEET_GRANT:
        print("explicit_fleet_grant_required", file=sys.stderr)
        return 2
    if args.plan_file is None:
        print("physical_plan_file_required", file=sys.stderr)
        return 2
    try:
        report = execute(json.loads(args.plan_file.read_text()))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
