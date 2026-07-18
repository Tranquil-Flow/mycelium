from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
SHA_E = "sha256:" + "e" * 64

NEGATIVE_TESTS = [
    "assignment_identity_mismatch",
    "authorization_statement_mismatch",
    "dropped_peer",
    "endpoint_generation_mismatch",
    "expired_reservation",
    "full_model_fallback",
    "missing_tensor",
    "route_identity_mismatch",
    "sequence_replay",
    "simulator_participation",
    "staging_root_symlink",
    "stale_proof",
    "synthetic_timing",
    "token_file_inline_value",
    "wrong_endpoint",
    "wrong_revision",
]

ABORT_CONDITIONS = [
    "authorization_changed",
    "coordinator_bind_failure",
    "credential_indirection_failure",
    "evidence_copyback_failure",
    "identity_mismatch",
    "negative_test_unexpected_acceptance",
    "parity_mismatch",
    "path_revalidation_failure",
    "peer_generation_mismatch",
    "source_revision_mismatch",
]

PER_STEP_EVIDENCE = [
    "active_cache_snapshots",
    "child_tensor_ownership",
    "distributed_and_reference_tokens",
    "input_token_and_position",
    "max_numeric_error_and_tolerance",
    "stage_digests_and_cache_lengths",
]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _authorization_statement(plan: dict[str, Any]) -> str:
    coordinator, peer = plan["hosts"]
    address = plan["coordinator"]
    return (
        "I explicitly authorize a later Mycelium physical qualification between "
        f"{coordinator['host_name']} as SSH user {coordinator['ssh_user']} and "
        f"{peer['host_name']} as SSH user {peer['ssh_user']}; stage only the declared "
        f"source_files under {coordinator['staging_root']} and {peer['staging_root']}; "
        f"use only token-file indirection at {coordinator['token_file_path']} and "
        f"{peer['token_file_path']}; bind the coordinator to "
        f"{address['address']}:{address['port']}; copy evidence to "
        f"{coordinator['evidence_copyback_destination']} and "
        f"{peer['evidence_copyback_destination']}; then apply the declared cleanup, "
        "rollback, and abort conditions. This statement authorizes only a later "
        "operator run; this validator performs no physical qualification."
    )


def make_plan() -> dict[str, Any]:
    plan: dict[str, Any] = {
        "protocol": "mycelium.physical_qualification_operator_plan.v1",
        "plan_id": "two-mac-route-qualification-001",
        "authorization_statement": "",
        "hosts": [
            {
                "role": "coordinator",
                "host_name": "m4pro",
                "ssh_user": "operator_a",
                "staging_root": "/Users/operator_a/mycelium-physical-qualification/two-mac-route-qualification-001/m4pro",
                "token_file_path": "/Users/operator_a/mycelium-physical-qualification/two-mac-route-qualification-001/m4pro/.credentials/coordinator.token",
                "endpoint_id": "endpoint-m4pro-001",
                "expected_generation": 7,
                "assignment_id": "assignment-m4pro-001",
                "assignment_digest": SHA_A,
                "evidence_copyback_destination": "/Users/operator_a/mycelium-physical-qualification-evidence/two-mac-route-qualification-001/m4pro",
            },
            {
                "role": "peer",
                "host_name": "evis-macbook-pro-1",
                "ssh_user": "operator_b",
                "staging_root": "/Users/operator_b/mycelium-physical-qualification/two-mac-route-qualification-001/evis-macbook-pro-1",
                "token_file_path": "/Users/operator_b/mycelium-physical-qualification/two-mac-route-qualification-001/evis-macbook-pro-1/.credentials/coordinator.token",
                "endpoint_id": "endpoint-laptop-001",
                "expected_generation": 11,
                "assignment_id": "assignment-laptop-001",
                "assignment_digest": SHA_B,
                "evidence_copyback_destination": "/Users/operator_a/mycelium-physical-qualification-evidence/two-mac-route-qualification-001/evis-macbook-pro-1",
            },
        ],
        "coordinator": {
            "host_name": "m4pro",
            "address": "100.84.252.4",
            "port": 43127,
        },
        "identities": {
            "deployment_id": "deployment-physical-001",
            "deployment_epoch": 3,
            "topology_generation": 19,
            "model_id": "openai-community/gpt2",
            "resolved_commit": "1" * 40,
            "model_manifest_digest": SHA_C,
            "route_id": "route-physical-001",
            "route_plan_digest": SHA_D,
            "execution_graph_digest": SHA_E,
            "assignment_bundle_digest": "sha256:" + "f" * 64,
        },
        "source_files": [
            "mycelium_router/transports/iroh.py",
            "runtime_loader.py",
            "two_process_inference_qualification.py",
        ],
        "run_matrix": {
            "cold": {
                "cache_precondition": "absent",
                "local_files_only": False,
                "expected_network_bytes": "positive",
            },
            "warm": {
                "cache_precondition": "same_pinned_assignment",
                "local_files_only": True,
                "expected_network_bytes": "zero",
            },
        },
        "decode_parity": {
            "decode_steps": 8,
            "mode": "stage_local_kv",
            "oracle": "independently_loaded_monolithic",
            "token_match": "exact",
            "activation_abs_tolerance": 0.00001,
            "final_logits_abs_tolerance": 0.00001,
            "require_single_token_decode": True,
            "require_no_full_prefix": True,
            "per_step_evidence": list(PER_STEP_EVIDENCE),
        },
        "negative_tests": list(NEGATIVE_TESTS),
        "evidence": {
            "copyback_order": ["m4pro", "evis-macbook-pro-1"],
            "preserve_distinct_cold_and_warm": True,
            "require_immutable_manifest": True,
            "verify_before_cleanup": True,
        },
        "cleanup": {
            "remove_staging_roots": True,
            "remove_token_files": True,
            "stop_run_scoped_processes": True,
            "require_coordinator_port_free": True,
            "preserve_verified_copyback": True,
        },
        "abort_conditions": list(ABORT_CONDITIONS),
        "rollback": {
            "scope": "run_scoped_only",
            "order": "stop_then_copy_partial_evidence_then_cleanup_if_copyback_verified",
            "preserve_remote_evidence_on_copyback_failure": True,
            "require_reauthorization_after_abort": True,
        },
    }
    plan["authorization_statement"] = _authorization_statement(plan)
    return plan


@pytest.fixture
def plan() -> dict[str, Any]:
    return copy.deepcopy(make_plan())


@pytest.fixture
def encoded_plan(plan: dict[str, Any]) -> bytes:
    return canonical_bytes(plan)


def refresh_authorization(plan: dict[str, Any]) -> None:
    plan["authorization_statement"] = _authorization_statement(plan)
