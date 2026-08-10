from __future__ import annotations

import copy

import pytest

from mycelium_layer_planner.allocation import allocate_layers
from mycelium_layer_planner.contracts import (
    ModelIdentity,
    NodeCapability,
    PlanningPolicy,
    WorkloadScenario,
)
from mycelium_layer_planner.workload import (
    empirical_interactive_chat,
    sustained_batch,
)
from mycelium_layer_planner.workload_intelligence import (
    attach_m15_observations,
    build_m15_plan_comparison,
    validate_m15_plan_comparison,
)


def _model() -> ModelIdentity:
    return ModelIdentity(
        model_id="org/model",
        revision="a" * 40,
        weight_digest="sha256:" + "1" * 64,
        architecture="Decoder",
        num_layers=8,
        hidden_size=256,
        dtype_bytes=2,
        kv_heads=4,
        head_dim=32,
        weight_bytes=800_000_000,
    )


def _nodes() -> tuple[NodeCapability, NodeCapability]:
    return (
        NodeCapability(
            "prefill-node",
            prefill_ms_per_layer_token=0.001,
            decode_ms_per_layer_token=1.0,
            fast_memory_bytes=4_000_000_000,
            total_memory_bytes=4_000_000_000,
            memory_bandwidth_Bps=1_000_000_000.0,
            spill_bandwidth_Bps=1_000_000_000.0,
        ),
        NodeCapability(
            "decode-node",
            prefill_ms_per_layer_token=1.0,
            decode_ms_per_layer_token=0.001,
            fast_memory_bytes=4_000_000_000,
            total_memory_bytes=4_000_000_000,
            memory_bandwidth_Bps=1_000_000_000.0,
            spill_bandwidth_Bps=1_000_000_000.0,
        ),
    )


def _snapshot() -> dict:
    nodes = _nodes()
    return {
        "protocol": "mycelium.layer_planner_snapshot.v1",
        "snapshot_generation": 15,
        "swarm_id": "swarm-m15",
        "placement_provenance": "planner_v2",
        "decode_mode": "stage_local_kv",
        "evidence_bundle_digest": "sha256:" + "2" * 64,
        "deployment": {"deployment_id": "deployment-m15", "deployment_epoch": 15},
        "model": _model().to_dict(),
        "nodes": [vars(node) for node in nodes],
        "links": [
            {
                "src": "prefill-node",
                "dst": "decode-node",
                "rtt_ms": 2.0,
                "jitter_ms": 0.1,
                "bandwidth_Bps": 100_000_000.0,
            },
            {
                "src": "decode-node",
                "dst": "prefill-node",
                "rtt_ms": 2.0,
                "jitter_ms": 0.1,
                "bandwidth_Bps": 100_000_000.0,
            },
        ],
        "policy": {
            "memory_reserve_fraction": 0.0,
            "replica_budget": 0,
            "ttft_slo_ms": 1_000_000.0,
            "tpot_slo_ms": 1_000_000.0,
        },
        "admitted_node_ids": ["prefill-node", "decode-node"],
        "node_runtime": {
            "prefill-node": {"backend": "mlx", "decode_mode": "stage_local_kv"},
            "decode-node": {"backend": "mlx", "decode_mode": "stage_local_kv"},
        },
    }


def test_phase_objective_changes_contiguous_dp_allocation() -> None:
    workload = WorkloadScenario(
        "interactive",
        prompt_tokens=512,
        output_tokens=64,
        concurrency=1,
    )
    prefill = allocate_layers(
        _nodes(), _model(), workload, PlanningPolicy(memory_reserve_fraction=0, objective="prefill_ttft")
    )
    decode = allocate_layers(
        _nodes(), _model(), workload, PlanningPolicy(memory_reserve_fraction=0, objective="decode_tpot")
    )

    assert [stage.layer_range.count for stage in prefill.stages] == [7, 1]
    assert [stage.layer_range.count for stage in decode.stages] == [1, 7]


def test_m15_comparison_is_deterministic_phase_separated_and_pareto_explicit() -> None:
    profiles = (
        empirical_interactive_chat(concurrency_points=(1, 2)),
        sustained_batch(concurrency_points=(1, 4)),
    )
    first = build_m15_plan_comparison(_snapshot(), profiles)
    second = build_m15_plan_comparison(_snapshot(), profiles)

    assert first == second
    assert first["protocol"] == "mycelium.m15_plan_comparison.v1"
    assert first["route_ready"] is False
    assert [item["profile_id"] for item in first["profiles"]] == [
        "interactive_chat_v1",
        "sustained_batch_v1",
    ]
    assert all(item["content_removed"] is True for item in first["profiles"])
    assert len(first["comparisons"]) == 2
    for comparison in first["comparisons"]:
        assert comparison["selection_mode"] == "minimax_normalized_regret"
        assert comparison["selected_candidate_id"] in comparison["pareto_candidate_ids"]
        assert {candidate["policy_id"] for candidate in comparison["candidates"]} == {
            "balanced",
            "decode_tpot",
            "prefill_ttft",
        }
        for candidate in comparison["candidates"]:
            assert candidate["scenarios"]
            assert all(
                set(metric)
                == {
                    "scenario_id",
                    "ttft_ms",
                    "prefill_compute_ms",
                    "prefill_transfer_ms",
                    "tpot_ms",
                    "decode_compute_ms",
                    "decode_transfer_ms",
                    "output_goodput_tps",
                    "expected_response_ms",
                    "required_memory_bytes",
                    "confidence",
                }
                for metric in candidate["scenarios"]
            )

    assert validate_m15_plan_comparison(copy.deepcopy(first)) == first


def test_m15_projection_is_closed_and_rejects_private_content() -> None:
    projection = build_m15_plan_comparison(
        _snapshot(),
        (empirical_interactive_chat(concurrency_points=(1,)),),
    )
    unknown = {**projection, "surprise": True}
    with pytest.raises(ValueError, match="shape"):
        validate_m15_plan_comparison(unknown)

    private = copy.deepcopy(projection)
    private["profiles"][0]["prompt"] = "private text"
    with pytest.raises(ValueError, match="private"):
        validate_m15_plan_comparison(private)


def test_planning_policy_rejects_unknown_phase_objective() -> None:
    with pytest.raises(ValueError, match="objective"):
        PlanningPolicy(objective="magic")


def _budget(profile_id: str) -> dict:
    return {
        "protocol": "mycelium.performance_budget.v2",
        "budget_id": f"m15-{profile_id}",
        "profile_id": profile_id,
        "minimum_sample_size": 1,
        "ttft_ms_maximum": 5_000.0,
        "tpot_ms_maximum": 2_000.0,
        "minimum_output_tokens_per_second": 0.5,
        "maximum_frames_per_request": 64,
        "maximum_relative_model_error": {"ttft": 100.0, "tpot": 100.0, "throughput": 1.0},
        "execution_scope": "sequential_observed",
        "peak_memory_budget_state": "approved_exclusion",
        "energy_thermal_budget_state": "approved_exclusion",
        "reconnect_budget_state": "approved_exclusion",
        "queueing_budget_state": "deferred_to_m16",
        "admission_latency_budget_state": "deferred_to_m16",
        "concurrency_budget_state": "deferred_to_m16",
        "batch_shape_budget_state": "deferred_to_m16",
    }


def _observation(profile_id: str, request_id: str, context_tokens: int) -> dict:
    return {
        "profile_id": profile_id,
        "request_id": request_id,
        "context_tokens": context_tokens,
        "output_tokens": 8,
        "runtime_backends": ["mlx", "numpy"],
        "topology_version": 15,
        "placement": [
            {"node_id": "prefill-node", "start": 0, "end": 7},
            {"node_id": "decode-node", "start": 7, "end": 8},
        ],
        "counters_before": {"frames_sent": 10, "frames_received": 10, "applied_operation_count": 8},
        "counters_after": {"frames_sent": 30, "frames_received": 30, "applied_operation_count": 26},
        "observed": {"ttft_ms": 1_000.0, "tpot_ms": 500.0, "output_goodput_tps": 2.0},
    }


def test_m15_observations_bind_exact_request_shape_errors_and_budgets() -> None:
    profiles = (empirical_interactive_chat(concurrency_points=(1,)), sustained_batch(concurrency_points=(1,)))
    projected = build_m15_plan_comparison(_snapshot(), profiles)
    calibrated = attach_m15_observations(
        projected,
        _snapshot(),
        [_budget(profile.name) for profile in profiles],
        [
            _observation("interactive_chat_v1", "request-interactive", 72),
            _observation("sustained_batch_v1", "request-batch", 73),
        ],
    )

    assert calibrated["calibration_state"] == "observed"
    assert len(calibrated["observations"]) == 2
    assert all(item["overall_state"] == "met" for item in calibrated["observations"])
    assert all(item["prediction"]["scenario_id"] == "exact_observed_shape" for item in calibrated["observations"])
    assert all(item["budget_results"]["queueing"] == "deferred_to_m16" for item in calibrated["observations"])
    assert validate_m15_plan_comparison(copy.deepcopy(calibrated)) == calibrated


def test_m15_observation_binding_rejects_duplicate_profiles_and_counter_regression() -> None:
    profiles = (empirical_interactive_chat(concurrency_points=(1,)), sustained_batch(concurrency_points=(1,)))
    projected = build_m15_plan_comparison(_snapshot(), profiles)
    duplicate = _observation("interactive_chat_v1", "request-one", 72)
    with pytest.raises(ValueError, match="exactly one observation"):
        attach_m15_observations(
            projected,
            _snapshot(),
            [_budget(profile.name) for profile in profiles],
            [duplicate, {**duplicate, "request_id": "request-two"}],
        )

    regressed = _observation("interactive_chat_v1", "request-one", 72)
    regressed["counters_after"]["frames_sent"] = 9
    with pytest.raises(ValueError, match="counter"):
        attach_m15_observations(
            build_m15_plan_comparison(_snapshot(), (profiles[0],)),
            _snapshot(),
            [_budget(profiles[0].name)],
            [regressed],
        )
