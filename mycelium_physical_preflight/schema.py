from __future__ import annotations

PROTOCOL = "mycelium.physical_qualification_operator_plan.v1"
EXECUTION_PROTOCOL = "mycelium.physical_qualification_execution_plan.v1"

ROOT_FIELDS = {
    "abort_conditions",
    "authorization_statement",
    "cleanup",
    "coordinator",
    "decode_parity",
    "evidence",
    "hosts",
    "identities",
    "negative_tests",
    "plan_id",
    "protocol",
    "rollback",
    "run_matrix",
    "source_files",
}
HOST_FIELDS = {
    "assignment_digest",
    "assignment_id",
    "endpoint_id",
    "evidence_copyback_destination",
    "expected_generation",
    "host_name",
    "role",
    "ssh_user",
    "staging_root",
    "token_file_path",
}
COORDINATOR_FIELDS = {"address", "host_name", "port"}
IDENTITY_FIELDS = {
    "assignment_bundle_digest",
    "deployment_epoch",
    "deployment_id",
    "execution_graph_digest",
    "model_id",
    "model_manifest_digest",
    "resolved_commit",
    "route_id",
    "route_plan_digest",
    "topology_generation",
}
RUN_MATRIX_FIELDS = {"cold", "warm"}
RUN_FIELDS = {"cache_precondition", "expected_network_bytes", "local_files_only"}
DECODE_FIELDS = {
    "activation_abs_tolerance",
    "decode_steps",
    "final_logits_abs_tolerance",
    "mode",
    "oracle",
    "per_step_evidence",
    "require_no_full_prefix",
    "require_single_token_decode",
    "token_match",
}
EVIDENCE_FIELDS = {
    "copyback_order",
    "preserve_distinct_cold_and_warm",
    "require_immutable_manifest",
    "verify_before_cleanup",
}
CLEANUP_FIELDS = {
    "preserve_verified_copyback",
    "remove_staging_roots",
    "remove_token_files",
    "require_coordinator_port_free",
    "stop_run_scoped_processes",
}
ROLLBACK_FIELDS = {
    "order",
    "preserve_remote_evidence_on_copyback_failure",
    "require_reauthorization_after_abort",
    "scope",
}

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
