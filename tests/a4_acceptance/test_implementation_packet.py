from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[2]
PACKET_PATH = Path(__file__).with_name("implementation_packet.v1.json")
SCENARIOS_PATH = Path(__file__).with_name("scenarios.v1.json")
HANDOVER_PATH = (
    ROOT / "docs/handover/2026-08-18-mycelium-a4-implementation-packet.md"
)
SPEC_PATH = (
    ROOT
    / "docs/superpowers/specs/2026-08-17-mycelium-a4-product-concurrency-liveness.md"
)
PROTOCOL = "mycelium.a4_implementation_packet.v1"
BASE_COMMIT = "4bde254f3f53bd77ce9a642397e8cfc297425334"
PACKET_SOURCE_COMMIT = "641eba4a78185fb23982bed1cdbe43b66b96983a"
A3_INTERMEDIATE_COMMIT = "95fe9b77d9fb6f43e96cdd35e7d5ee02e8dc136d"
A3_ATOMIC_COMMIT = "905786df41ffdad5718d3464733e2f5cb8727532"
TOP_LEVEL_FIELDS = {
    "protocol",
    "gate_state",
    "base_acceptance_commit",
    "claim_boundary",
    "implementation_base",
    "allowed_source_paths",
    "planned_new_source_paths",
    "conditional_source_paths",
    "physical_command_surfaces",
    "implementation_lane_prohibited_paths",
    "lock_ownership",
    "lock_order",
    "replacement_map",
    "asynchronous_command_architecture",
    "worker_pool",
    "reconnect_and_generation_reset",
    "cancellation_and_cleanup_ownership",
    "primary_integrator_prerequisites",
    "migration_order",
    "rollback_boundary",
    "primary_integrator_ui",
    "scenario_mappings",
    "regression_commands",
    "physical_browser_gates",
    "qualification_claim",
    "promotion_authorized",
}
ALLOWED_SOURCE_PATHS = {
    "mycelium_m16_runtime.py",
    "mycelium_live/health.py",
    "mycelium_live/registry.py",
    "mycelium_live/route.py",
    "mycelium_live/router_port.py",
    "mycelium_membership/contracts.py",
    "mycelium_node/process.py",
    "mycelium_request_gateway/asgi.py",
    "mycelium_request_gateway/backend.py",
    "mycelium_request_gateway/contracts.py",
    "mycelium_request_gateway/service.py",
    "mycelium_router/contracts.py",
    "mycelium_router/entry.py",
    "mycelium_router/live_ports.py",
    "mycelium_router/mlx_runtime.py",
    "mycelium_router/numpy_runtime.py",
    "mycelium_router/relay.py",
    "mycelium_router/transports/iroh.py",
    "mycelium_seed/state.py",
    "physical_inference_node.py",
}
PLANNED_NEW_SOURCE_PATHS = {
    "mycelium_live/command_controller.py",
    "mycelium_live/liveness.py",
}
CONDITIONAL_SOURCE_PATHS = {
    "mycelium_router/router.py": (
        "only_if_the_router_facade_exposes_attempt_aware_a4_apis"
    ),
    "mycelium_mobile/pixel_runtime.py": (
        "only_if_the_pixel_backend_remains_a4_eligible_after_bounded_"
        "cancellation_proof"
    ),
}
PHYSICAL_COMMAND_SURFACES = {
    "mycelium_node/process.py": "controller_process_command_transport",
    "physical_inference_node.py": "physical_node_command_service",
    "mycelium_router/transports/iroh.py": "physical_router_data_transport",
}
PROHIBITED_PATHS = {
    "contracts/contract-manifest.v1.json",
    "contracts/compatibility-fixtures/**",
    "docs/qualification/**",
    "mycelium_layer_planner/**",
    "mycelium_m18_replication.py",
    "mycelium_m19_recovery.py",
    "mycelium_m20_speculation.py",
    "mycelium_m23_kv.py",
    "mycelium_physical_runner/**",
    "mycelium_ui_gateway/**",
    "physical_inference_qualification.py",
    "release/**",
    "scripts/**",
    "tests/evidence/**",
    "ui/**",
}
LOCK_ORDER = [
    "authority",
    "deployment",
    "session",
    "request",
    "path",
    "placement",
    "transport",
    "detector",
]
LOCK_FIELDS = {
    "lock_id",
    "order_rank",
    "owner",
    "source_path",
    "primitive",
    "protects",
    "blocking_work_allowed",
}
EXPECTED_LOCKS = {
    "authority": (10, "deployment_registry_and_qualifier"),
    "session": (30, "request_gateway"),
    "request": (40, "router"),
    "cancellation": (40, "command_controller"),
    "stage": (60, "placement_runtime"),
    "liveness": (80, "liveness_detector"),
}
REPLACEMENT_FIELDS = {
    "replacement_id",
    "current_source_path",
    "current_symbol",
    "current_problem",
    "replacement_source_path",
    "replacement",
    "required_proof",
}
REPLACEMENT_IDS = {
    "m16_route_global_metadata_lock",
    "single_active_request_slot",
    "single_dispatch_thread",
    "generation_long_route_execution_lock",
    "qualification_session_command_lock",
    "physical_node_exchange_lock",
    "physical_node_inline_command_loop",
    "gateway_thread_per_request",
}
COMMAND_PROTOCOL_FIELDS = [
    "request_id",
    "request_attempt",
    "path_digest",
    "absolute_deadline",
    "cancellation_generation",
    "idempotency_digest",
    "terminal_compare_and_swap",
    "cleanup_result",
]
CANCELLATION_CORRELATION_FIELDS = [
    "request_id",
    "request_attempt",
    "path_digest",
    "absolute_deadline",
    "cancellation_generation",
]
WORKER_DEFAULTS = {
    "worker_count": (4, 1, 64),
    "queue_maximum_items": (256, 1, 4_096),
    "queue_maximum_bytes": (67_108_864, 1_048_576, 1_073_741_824),
    "per_placement_maximum_commands": (2, 1, 64),
    "browser_event_buffer_items": (64, 2, 65_536),
    "request_scoped_interruption_and_cleanup_ms": (2_000, 1, 2_000),
    "shutdown_join_ms": (4_000, 1, 4_000),
}
WORKER_OPERATIONS = {
    "enqueue",
    "claim",
    "dispatch_stage_command",
    "cancel",
    "complete",
    "shutdown",
}
RECONNECT_AND_RESET = {
    "owner": "primary_integrator",
    "disconnect_mid_request": "detach_only_subscription_request_and_backend_continue",
    "reconnect_authority": "same_authenticated_session_request_and_captured_generation",
    "resume_cursor": "last_client_applied_event_sequence",
    "replay_rule": "replay_strictly_after_cursor_with_same_event_identity",
    "single_subscriber_rule": "one_live_subscriber_per_request_session",
    "invalid_cursor_rule": (
        "future_expired_cross_session_or_cross_generation_cursor_fails_closed"
    ),
    "cancel_after_reconnect": (
        "targets_same_request_attempt_and_advances_cancellation_generation_once"
    ),
    "backend_generation_change": (
        "active_request_remains_pinned_new_admission_uses_new_generation"
    ),
    "publisher_generation_reset": (
        "emit_full_authoritative_snapshot_and_never_union_old_and_new_generations"
    ),
    "terminal_rule": "exactly_one_terminal_event_survives_reconnect_and_reset",
}
CLEANUP_OWNERS = {
    "browser_cancel_authentication": "request_gateway",
    "request_cancellation_generation": "command_controller",
    "request_path_cancellation": "router",
    "complete_path_reservations": "m16_admission_ledger",
    "stage_capacity_and_kv": "placement_runtime",
    "transport_stream_and_receipts": "transport",
    "terminal_publication": "request_gateway",
    "cleanup_record": "m16_admission_ledger",
}
CLEANUP_RULES = {
    "cleanup_requires_matching_request_attempt_generation_and_recorded_owner",
    "cleanup_is_idempotent_only_for_same_canonical_digest",
    "non_owner_cleanup_cannot_release_resources",
    "late_result_or_cleanup_cannot_mutate_newer_generation",
    "terminal_history_is_not_counted_as_live_resource",
    "unknown_exception_fails_owned_request_not_deployment",
    "failure_and_cleanup_are_scoped_by_request_path_and_attempt",
    "request_failure_is_not_automatically_promoted_to_deployment_fatal",
    "automatic_replay_and_recovery_are_disabled_and_owned_by_a6",
}
VERSIONED_CONTRACTS = [
    "mycelium.concurrent_request_runtime.v1",
    "mycelium.traffic_liveness.v1",
    "mycelium.scoped_runtime_incident.v1",
    "mycelium.interruptible_stage_command.v1",
    "mycelium.product_concurrency_liveness_qualification.v1",
]
REQUIRED_SCENARIOS = {
    "overlapping_requests",
    "route_global_lock_absence",
    "lock_order_deadlock_detection",
    "cancel_isolation",
    "participating_active_disconnect",
    "participating_idle_staleness",
    "nonparticipating_peer_exit",
    "one_missed_receipt",
    "stale_incarnation_receipt",
    "late_command_result",
    "queue_saturation",
    "worker_exit",
    "bounded_shutdown",
    "fatal_allowlist_rejection",
    "active_request_reconnect",
    "state_subset_ownership_boundaries",
    "second_session_privacy",
}
MAPPING_FIELDS = {
    "scenario_id",
    "production_paths",
    "focused_tests",
    "primary_integrator_paths",
}
EXPECTED_FOCUSED_TESTS = {
    "overlapping_requests": "tests/live/test_a4_concurrency.py::test_overlapping_requests_advance_independently",
    "route_global_lock_absence": "tests/live/test_a4_concurrency.py::test_every_blocking_boundary_allows_other_dispatch_and_cancel",
    "lock_order_deadlock_detection": "tests/live/test_a4_concurrency.py::test_lock_inversion_fails_before_physical_command",
    "cancel_isolation": "tests/request_gateway/test_a4_cancellation.py::test_cancel_one_request_does_not_mutate_another",
    "participating_active_disconnect": "tests/live/test_a4_liveness.py::test_active_disconnect_interrupts_owned_command_within_budget",
    "participating_idle_staleness": "tests/live/test_a4_liveness.py::test_idle_subject_quarantines_only_after_frozen_threshold",
    "nonparticipating_peer_exit": "tests/live/test_a4_liveness.py::test_nonparticipating_peer_exit_does_not_mutate_active_route",
    "one_missed_receipt": "tests/live/test_a4_liveness.py::test_one_missed_receipt_is_suspect_only",
    "stale_incarnation_receipt": "tests/live/test_a4_liveness.py::test_stale_incarnation_receipt_cannot_refresh_subject",
    "late_command_result": "tests/live/test_a4_commands.py::test_late_result_cannot_mutate_new_request_generation",
    "queue_saturation": "tests/live/test_a4_concurrency.py::test_queue_saturation_returns_bounded_backpressure_without_leak",
    "worker_exit": "tests/live/test_a4_concurrency.py::test_worker_exit_fails_only_owned_request_and_releases_resources",
    "bounded_shutdown": "tests/live/test_a4_concurrency.py::test_shutdown_interrupts_joins_and_returns_all_counters_to_zero",
    "fatal_allowlist_rejection": "tests/live/test_a4_liveness.py::test_unknown_worker_exception_cannot_latch_deployment_fatal",
    "active_request_reconnect": "tests/request_gateway/test_a4_reconnect.py::test_mid_request_reconnect_replays_without_duplicate_or_cancel",
    "state_subset_ownership_boundaries": "tests/live/test_a4_ownership.py::test_cross_owner_mutation_and_cleanup_fail_closed",
    "second_session_privacy": "tests/request_gateway/test_a4_reconnect.py::test_second_session_cannot_observe_or_resume_private_request",
}
REQUIRED_PRODUCTION_PATHS = {
    "route_global_lock_absence": {
        "mycelium_m16_runtime.py",
        "mycelium_live/router_port.py",
        "mycelium_live/route.py",
    },
    "cancel_isolation": {
        "mycelium_live/command_controller.py",
        "mycelium_request_gateway/service.py",
    },
    "participating_active_disconnect": {
        "mycelium_live/command_controller.py",
        "mycelium_live/liveness.py",
        "mycelium_router/transports/iroh.py",
        "physical_inference_node.py",
    },
    "active_request_reconnect": {
        "mycelium_request_gateway/asgi.py",
        "mycelium_request_gateway/contracts.py",
        "mycelium_request_gateway/service.py",
    },
    "state_subset_ownership_boundaries": {
        "mycelium_live/liveness.py",
        "mycelium_live/registry.py",
        "mycelium_live/router_port.py",
        "mycelium_request_gateway/service.py",
    },
}
INTEGRATOR_WORKSPACES = {
    "inference",
    "device_lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
}
REGRESSION_COMMANDS = {
    "packet": [
        "python3 -m pytest -q tests/a4_acceptance",
        "ruff check tests/a4_acceptance",
    ],
    "focused_implementation": [
        "python3 -m pytest -q tests/live/test_a4_concurrency.py tests/live/test_a4_commands.py tests/live/test_a4_liveness.py tests/live/test_a4_ownership.py tests/request_gateway/test_a4_cancellation.py tests/request_gateway/test_a4_reconnect.py",
        "python3 -m pytest -q tests/live/test_m16_runtime.py tests/live/test_router_port.py tests/live/test_fake_route.py tests/request_gateway tests/router tests/membership tests/ui_gateway",
    ],
    "full": [
        "python3 -m pytest -q",
        "cd ui/web && npm run check",
        "cd ui/web && npm run test:e2e",
    ],
}
PHYSICAL_BROWSER_GATES = {
    "physical_overlapping_requests": "physical_positive",
    "physical_active_disconnect": "physical_positive",
    "browser_reconnect_mid_request": "browser_positive",
    "physical_negative_scope_and_shutdown": "physical_negative",
}


def _load(path: Path) -> dict:
    value = json.loads(path.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def _path_matches_pattern(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        prefix = pattern[:-3]
        return path == prefix or path.startswith(prefix + "/")
    return path == pattern


def test_packet_is_closed_and_permanently_design_only() -> None:
    packet = _load(PACKET_PATH)

    assert set(packet) == TOP_LEVEL_FIELDS
    assert packet["protocol"] == PROTOCOL
    assert packet["gate_state"] == "design_only"
    assert packet["base_acceptance_commit"] == BASE_COMMIT
    assert packet["claim_boundary"] == (
        "machine-checked implementation handover only; no production, contract, UI, "
        "service, device, evidence, or qualification change"
    )
    assert packet["qualification_claim"] is False
    assert packet["promotion_authorized"] is False


def test_implementation_base_requires_exact_atomic_a3_commit() -> None:
    base = _load(PACKET_PATH)["implementation_base"]

    assert set(base) == {
        "packet_source_commit",
        "required_base",
        "insufficient_bases",
        "rule",
    }
    assert base["packet_source_commit"] == PACKET_SOURCE_COMMIT
    assert base["required_base"] == A3_ATOMIC_COMMIT
    assert base["insufficient_bases"] == [
        PACKET_SOURCE_COMMIT,
        A3_INTERMEDIATE_COMMIT,
    ]
    assert base["rule"] == (
        "implementation_must_branch_from_or_rebase_onto_exact_atomic_a3_commit_"
        f"{A3_ATOMIC_COMMIT}_before_any_production_edit"
    )


def test_source_allowlist_planned_paths_and_prohibitions_are_exact() -> None:
    packet = _load(PACKET_PATH)
    allowed = packet["allowed_source_paths"]
    planned = packet["planned_new_source_paths"]
    conditional = packet["conditional_source_paths"]
    physical = packet["physical_command_surfaces"]
    prohibited = packet["implementation_lane_prohibited_paths"]

    assert set(allowed) == ALLOWED_SOURCE_PATHS
    assert len(allowed) == len(ALLOWED_SOURCE_PATHS)
    assert set(planned) == PLANNED_NEW_SOURCE_PATHS
    assert len(planned) == len(PLANNED_NEW_SOURCE_PATHS)
    assert all(set(item) == {"path", "condition"} for item in conditional)
    assert {item["path"]: item["condition"] for item in conditional} == (
        CONDITIONAL_SOURCE_PATHS
    )
    assert all((ROOT / item["path"]).is_file() for item in conditional)
    assert all(set(item) == {"path", "role"} for item in physical)
    assert {item["path"]: item["role"] for item in physical} == (
        PHYSICAL_COMMAND_SURFACES
    )
    assert set(PHYSICAL_COMMAND_SURFACES) <= ALLOWED_SOURCE_PATHS
    assert set(prohibited) == PROHIBITED_PATHS
    assert len(prohibited) == len(PROHIBITED_PATHS)
    assert all((ROOT / path).is_file() for path in allowed)
    assert all((ROOT / Path(path).parent).is_dir() for path in planned)
    assert not any(
        _path_matches_pattern(path, pattern)
        for path in [*allowed, *planned, *CONDITIONAL_SOURCE_PATHS]
        for pattern in prohibited
    )


def test_lock_ownership_order_and_nonblocking_boundary_are_frozen() -> None:
    packet = _load(PACKET_PATH)
    locks = packet["lock_ownership"]

    assert packet["lock_order"] == LOCK_ORDER
    assert {lock["lock_id"] for lock in locks} == set(EXPECTED_LOCKS)
    for lock in locks:
        assert set(lock) == LOCK_FIELDS
        assert (lock["order_rank"], lock["owner"]) == EXPECTED_LOCKS[
            lock["lock_id"]
        ]
        assert lock["source_path"] in ALLOWED_SOURCE_PATHS | PLANNED_NEW_SOURCE_PATHS
        assert lock["blocking_work_allowed"] is False
        assert isinstance(lock["protects"], list) and lock["protects"]
        assert len(lock["protects"]) == len(set(lock["protects"]))


def test_every_current_serialization_choke_point_has_a_bounded_replacement() -> None:
    packet = _load(PACKET_PATH)
    replacements = packet["replacement_map"]

    assert {item["replacement_id"] for item in replacements} == REPLACEMENT_IDS
    assert len(replacements) == len(REPLACEMENT_IDS)
    for replacement in replacements:
        assert set(replacement) == REPLACEMENT_FIELDS
        assert (ROOT / replacement["current_source_path"]).is_file()
        assert replacement["replacement_source_path"] in (
            ALLOWED_SOURCE_PATHS | PLANNED_NEW_SOURCE_PATHS
        )
        assert replacement["current_problem"]
        assert replacement["replacement"]
        assert replacement["required_proof"]
    qualification = next(
        item
        for item in replacements
        if item["replacement_id"] == "qualification_session_command_lock"
    )
    assert qualification["current_source_path"] == "physical_inference_qualification.py"
    assert qualification["current_source_path"] not in ALLOWED_SOURCE_PATHS
    assert qualification["replacement_source_path"] == (
        "mycelium_live/command_controller.py"
    )
    physical_transport = next(
        item
        for item in replacements
        if item["replacement_id"] == "physical_node_exchange_lock"
    )
    assert physical_transport["replacement_source_path"] == (
        "mycelium_node/process.py"
    )
    assert "short_frame_write_lock" in physical_transport["replacement"]
    node_service = next(
        item
        for item in replacements
        if item["replacement_id"] == "physical_node_inline_command_loop"
    )
    assert node_service["replacement_source_path"] == "physical_inference_node.py"
    assert "responsive_stdin" in node_service["replacement"]


def test_asynchronous_command_and_backend_eligibility_are_frozen() -> None:
    architecture = _load(PACKET_PATH)["asynchronous_command_architecture"]

    assert set(architecture) == {
        "transport_write_lock",
        "response_correlation",
        "lock_may_span_request_response",
        "node_command_handling",
        "runtime_work_units",
        "command_protocol_fields",
        "cancellation_correlation_fields",
        "interruption_and_cleanup_budget_ms",
        "backend_eligibility",
        "shared_node_termination_is_request_scoped_cancellation",
        "noncooperative_backend_policy",
        "failure_and_cleanup_scope",
        "deployment_global_fatal_promotion",
        "automatic_replay_recovery",
        "replay_recovery_owner",
    }
    assert architecture["transport_write_lock"] == "short_canonical_frame_write_only"
    assert architecture["response_correlation"] == "command_id_correlated_waiters"
    assert architecture["lock_may_span_request_response"] is False
    assert architecture["node_command_handling"] == (
        "responsive_stdin_plus_bounded_command_workers"
    )
    assert architecture["runtime_work_units"] == (
        "bounded_cooperative_prefill_and_decode_units"
    )
    assert architecture["command_protocol_fields"] == COMMAND_PROTOCOL_FIELDS
    assert (
        architecture["cancellation_correlation_fields"]
        == CANCELLATION_CORRELATION_FIELDS
    )
    assert architecture["interruption_and_cleanup_budget_ms"] == 2_000
    assert architecture["backend_eligibility"] == (
        "proof_required_or_backend_unavailable"
    )
    assert (
        architecture["shared_node_termination_is_request_scoped_cancellation"]
        is False
    )
    assert architecture["noncooperative_backend_policy"] == (
        "unavailable_without_gate_weakening"
    )
    assert architecture["failure_and_cleanup_scope"] == "request_path_attempt"
    assert architecture["deployment_global_fatal_promotion"] == (
        "explicit_allowlist_only"
    )
    assert architecture["automatic_replay_recovery"] is False
    assert architecture["replay_recovery_owner"] == "A6"


def test_worker_pool_and_queue_interfaces_are_closed_and_bounded() -> None:
    pool = _load(PACKET_PATH)["worker_pool"]

    assert set(pool) == {"configuration", "configuration_scope", "interfaces"}
    assert pool["configuration_scope"] == "future_request_generation_only"
    assert set(pool["configuration"]) == set(WORKER_DEFAULTS)
    for name, expected in WORKER_DEFAULTS.items():
        bound = pool["configuration"][name]
        assert set(bound) == {"default", "minimum", "maximum"}
        assert (bound["default"], bound["minimum"], bound["maximum"]) == expected
        assert bound["minimum"] <= bound["default"] <= bound["maximum"]
    interfaces = pool["interfaces"]
    assert {item["operation"] for item in interfaces} == WORKER_OPERATIONS
    assert len(interfaces) == len(WORKER_OPERATIONS)
    for interface in interfaces:
        assert set(interface) == {"operation", "input", "output"}
        assert interface["input"] and interface["output"]


def test_reconnect_reset_cancellation_and_cleanup_authority_are_exact() -> None:
    packet = _load(PACKET_PATH)

    assert packet["reconnect_and_generation_reset"] == RECONNECT_AND_RESET
    cleanup = packet["cancellation_and_cleanup_ownership"]
    assert set(cleanup) == {*CLEANUP_OWNERS, "rules"}
    assert {name: cleanup[name] for name in CLEANUP_OWNERS} == CLEANUP_OWNERS
    assert set(cleanup["rules"]) == CLEANUP_RULES
    assert len(cleanup["rules"]) == len(CLEANUP_RULES)


def test_primary_integrator_prelands_contract_and_generation_interfaces() -> None:
    prerequisites = _load(PACKET_PATH)["primary_integrator_prerequisites"]

    assert set(prerequisites) == {
        "owner",
        "must_precede_a4_implementation",
        "versioned_contracts",
        "compatibility_fixtures",
        "manifest_entries",
        "session_publisher_generation_interface",
        "exclusive_ownership",
    }
    assert prerequisites["owner"] == "primary_integrator"
    assert prerequisites["must_precede_a4_implementation"] is True
    assert prerequisites["versioned_contracts"] == VERSIONED_CONTRACTS
    assert prerequisites["compatibility_fixtures"] == (
        "preland_all_five_versioned_contract_fixtures"
    )
    assert prerequisites["manifest_entries"] == (
        "preland_all_five_versioned_contract_manifest_entries"
    )
    assert prerequisites["session_publisher_generation_interface"] == (
        "preland_versioned_same_session_and_publisher_generation_interface"
    )
    assert prerequisites["exclusive_ownership"] == [
        "request_gateway_service",
        "a4_physical_fault_browser_harness_and_evidence_root",
        "shared_contract_generation",
        "live_supervisor_composition",
        "same_session_reconnect",
        "publisher_generation_reset",
        "shared_ui",
    ]


def test_migration_is_ordered_and_rollback_has_one_activation_boundary() -> None:
    packet = _load(PACKET_PATH)
    migration = packet["migration_order"]

    assert [item["step"] for item in migration] == list(range(1, 12))
    assert all(set(item) == {"step", "name", "activation"} for item in migration)
    assert [item["activation"] for item in migration] == [False] * 10 + [True]
    assert migration[0]["name"] == (
        f"branch_from_or_rebase_onto_exact_atomic_a3_commit_{A3_ATOMIC_COMMIT}"
    )
    assert migration[1]["name"] == (
        "primary_integrator_prelands_five_contracts_fixtures_manifest_and_"
        "generation_interface"
    )
    rollback = packet["rollback_boundary"]
    assert set(rollback) == {
        "boundary",
        "pre_boundary_action",
        "post_boundary_action",
        "prohibited_partial_rollback",
    }
    assert rollback["boundary"] == (
        "before_owner_approved_a4_contract_generation_activation"
    )
    assert set(rollback["prohibited_partial_rollback"]) == {
        "restore_route_global_lock_while_worker_pool_is_live",
        "mix_old_and_new_request_or_cancellation_generations",
        "reuse_pre_rollback_physical_or_browser_evidence",
        "leave_new_ui_projection_on_old_backend_contracts",
    }


def test_every_a4_acceptance_scenario_maps_to_surfaces_and_a_focused_test() -> None:
    packet = _load(PACKET_PATH)
    acceptance = _load(SCENARIOS_PATH)
    acceptance_ids = {
        scenario["scenario_id"] for scenario in acceptance["scenarios"]
    }
    mappings = packet["scenario_mappings"]

    assert acceptance_ids == REQUIRED_SCENARIOS
    assert {item["scenario_id"] for item in mappings} == acceptance_ids
    assert len(mappings) == len(acceptance_ids)
    reserved = set(packet["primary_integrator_ui"]["reserved_paths"])
    source_scope = (
        ALLOWED_SOURCE_PATHS
        | PLANNED_NEW_SOURCE_PATHS
        | set(CONDITIONAL_SOURCE_PATHS)
    )
    for mapping in mappings:
        assert set(mapping) == MAPPING_FIELDS
        scenario_id = mapping["scenario_id"]
        assert mapping["production_paths"]
        assert set(mapping["production_paths"]) <= source_scope
        assert mapping["focused_tests"] == [EXPECTED_FOCUSED_TESTS[scenario_id]]
        assert set(mapping["primary_integrator_paths"]) <= reserved
        assert all(
            test.startswith("tests/") and ".py::test_" in test
            for test in mapping["focused_tests"]
        )
    by_id = {mapping["scenario_id"]: mapping for mapping in mappings}
    for scenario_id, required_paths in REQUIRED_PRODUCTION_PATHS.items():
        assert required_paths <= set(by_id[scenario_id]["production_paths"])


def test_shared_ui_paths_are_reserved_for_primary_integrator_only() -> None:
    packet = _load(PACKET_PATH)
    ui = packet["primary_integrator_ui"]

    assert set(ui) == {"owner", "reserved_paths", "workspace_bindings", "rule"}
    assert ui["owner"] == "primary_integrator"
    assert set(ui["workspace_bindings"]) == INTEGRATOR_WORKSPACES
    assert ui["reserved_paths"]
    assert len(ui["reserved_paths"]) == len(set(ui["reserved_paths"]))
    assert all((ROOT / path).is_file() for path in ui["reserved_paths"])
    assert all(
        any(_path_matches_pattern(path, pattern) for pattern in PROHIBITED_PATHS)
        for path in ui["reserved_paths"]
    )
    assert ui["rule"] == (
        "backend_lanes_publish_only_privacy_reduced_same_generation_snapshots_"
        "primary_integrator_owns_shared_ui_edits"
    )


def test_regression_commands_and_physical_browser_gates_are_frozen() -> None:
    packet = _load(PACKET_PATH)

    assert packet["regression_commands"] == REGRESSION_COMMANDS
    gates = packet["physical_browser_gates"]
    assert {gate["gate_id"]: gate["kind"] for gate in gates} == PHYSICAL_BROWSER_GATES
    assert len(gates) == len(PHYSICAL_BROWSER_GATES)
    for gate in gates:
        assert set(gate) == {"gate_id", "kind", "requirements"}
        assert isinstance(gate["requirements"], list) and gate["requirements"]
        assert len(gate["requirements"]) == len(set(gate["requirements"]))
        assert all(requirement == requirement.lower() for requirement in gate["requirements"])


def test_handover_freezes_claim_boundary_and_all_required_sections() -> None:
    text = " ".join(HANDOVER_PATH.read_text("utf-8").split())

    for phrase in (
        "**Status:** `design_only`",
        BASE_COMMIT,
        PACKET_SOURCE_COMMIT,
        A3_ATOMIC_COMMIT,
        "only production surfaces a future A4 implementation lane may change",
        "Cancellation uses the owning request scope and generation",
        "generation-long route-global lock",
        "fixed worker pool over the existing bounded admission queue",
        "A publisher reset emits one full authoritative snapshot",
        "The rollback boundary is before owner-approved activation",
        "It must remain a one-to-one mapping with `scenarios.v1.json`",
        "primary integrator alone owns the exact reserved paths",
        "short lock only to write one canonical frame",
        "Prefill and decode are divided into bounded cancellable work units",
        "Killing a shared node is never request-scoped cancellation",
        "A4 disables automatic replay and recovery",
        "all five versioned A4 contracts",
        "same-session reconnect, shared UI, and generation reset",
        "None of those gates is executed or satisfied by this packet",
        "`qualification_claim=false`",
        "`promotion_authorized=false`",
    ):
        assert phrase in text


def test_frozen_spec_records_owner_approved_bounded_step_cancellation() -> None:
    text = " ".join(SPEC_PATH.read_text("utf-8").split())

    for phrase in (
        "**Status:** `design_only`",
        "owner-approved cancellation model is cooperative bounded-step execution",
        "Prefill and decode are divided into bounded cancellable work units",
        "node command handling remains responsive while those units execute",
        "Cancellation is correlated by request, attempt, path digest, absolute deadline, and cancellation generation",
        "expected terminal compare-and-swap and bounded cleanup-result envelope",
        "short write lock for one canonical frame and command-ID-correlated waiters",
        "Interruption, request-owned cleanup, backend release, and terminal publication must together complete within one original absolute 2,000 ms end-to-end bound",
        "backend that cannot prove this bound is A4-ineligible",
        "Killing a shared node is never request-scoped cancellation",
        "remain scoped by request, path, and attempt",
        "A4 disables automatic replay or recovery",
        "replay/recovery remains owned by A6",
    ):
        assert phrase in text
