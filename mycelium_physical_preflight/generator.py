from __future__ import annotations

import hashlib
from typing import Any

from .schema import EXECUTION_PROTOCOL


def _phase(phase: str, actions: list[dict[str, Any]], **requirements: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"actions": actions, "phase": phase}
    value.update(requirements)
    return value


def generate_execution_plan(operator_plan: dict[str, Any], operator_plan_bytes: bytes) -> dict[str, Any]:
    """Generate inert, declarative instructions. This function executes nothing."""
    hosts = operator_plan["hosts"]
    source_files = operator_plan["source_files"]
    identities = operator_plan["identities"]

    stages = [
        {
            "host_name": host["host_name"],
            "source_file": source_file,
            "staging_root": host["staging_root"],
        }
        for host in hosts
        for source_file in source_files
    ]
    path_checks = [
        {
            "evidence_copyback_destination": host["evidence_copyback_destination"],
            "host_name": host["host_name"],
            "staging_root": host["staging_root"],
            "token_file_path": host["token_file_path"],
        }
        for host in hosts
    ]
    credential_checks = [
        {
            "checks": ["regular_file", "owner_matches_ssh_user", "mode_0600", "no_symlink"],
            "host_name": host["host_name"],
            "token_file_path": host["token_file_path"],
        }
        for host in hosts
    ]
    cold_actions = [
        {
            "assignment_id": host["assignment_id"],
            "expected_network_bytes": "positive",
            "host_name": host["host_name"],
            "local_files_only": False,
        }
        for host in hosts
    ]
    warm_actions = [
        {
            "assignment_id": host["assignment_id"],
            "expected_network_bytes": "zero",
            "host_name": host["host_name"],
            "local_files_only": True,
        }
        for host in hosts
    ]
    decode_actions = [
        {
            "decode_index": index,
            "record_per_step_evidence": list(operator_plan["decode_parity"]["per_step_evidence"]),
            "release_ready": False,
            "route_ready": False,
        }
        for index in range(8)
    ]
    negative_actions = [
        {"expected_outcome": "fail_closed", "test_id": test_id}
        for test_id in operator_plan["negative_tests"]
    ]

    execution_phases = [
        _phase(
            "authorization_gate",
            [{"plan_id": operator_plan["plan_id"], "require_exact_statement_match": True}],
            abort_on_change=True,
        ),
        _phase(
            "host_local_path_revalidation",
            path_checks,
            require_absolute_nonoverlapping_non_source_tree_paths=True,
            require_no_symlink_components=True,
        ),
        _phase(
            "stage_explicit_files",
            stages,
            prohibit_directory_copy=True,
            prohibit_globs=True,
        ),
        _phase(
            "credential_file_preflight",
            credential_checks,
            prohibit_credential_bytes_in_arguments_or_evidence=True,
        ),
        _phase(
            "coordinator_start_and_pending_status",
            [
                {
                    "address": operator_plan["coordinator"]["address"],
                    "host_name": operator_plan["coordinator"]["host_name"],
                    "port": operator_plan["coordinator"]["port"],
                    "status": "pending",
                }
            ],
            require_route_ready_false=True,
        ),
        _phase("cold_runs", cold_actions, requirements=operator_plan["run_matrix"]["cold"]),
        _phase("warm_offline_runs", warm_actions, requirements=operator_plan["run_matrix"]["warm"]),
        _phase(
            "coordinator_restart_and_report_resubmission",
            [{"route_id": identities["route_id"], "status": "pending"}],
            require_identity_revalidation=True,
        ),
        _phase(
            "stage_local_kv_prefill",
            [{"host_name": host["host_name"], "mode": "stage_local_kv"} for host in hosts],
            prohibit_full_prefix_decode=True,
        ),
        _phase(
            "eight_step_decode_parity",
            decode_actions,
            requirements=operator_plan["decode_parity"],
        ),
        _phase("negative_tests", negative_actions, unexpected_acceptance_aborts=True),
        _phase(
            "evidence_copyback_and_verification",
            [
                {
                    "destination": host["evidence_copyback_destination"],
                    "host_name": host["host_name"],
                }
                for host in hosts
            ],
            requirements=operator_plan["evidence"],
        ),
        _phase(
            "run_scoped_cleanup",
            [{"host_name": host["host_name"], "staging_root": host["staging_root"]} for host in hosts],
            requirements=operator_plan["cleanup"],
        ),
        _phase(
            "abort_and_rollback",
            [],
            abort_conditions=operator_plan["abort_conditions"],
            requirements=operator_plan["rollback"],
        ),
    ]

    return {
        "authorization_statement": operator_plan["authorization_statement"],
        "coordinator": operator_plan["coordinator"],
        "execution_phases": execution_phases,
        "hosts": hosts,
        "identities": identities,
        "operator_plan_digest": "sha256:" + hashlib.sha256(operator_plan_bytes).hexdigest(),
        "operator_plan_protocol": operator_plan["protocol"],
        "physical_qualification_executed": False,
        "plan_id": operator_plan["plan_id"],
        "protocol": EXECUTION_PROTOCOL,
        "release_ready": False,
        "route_ready": False,
        "source_files": source_files,
    }
