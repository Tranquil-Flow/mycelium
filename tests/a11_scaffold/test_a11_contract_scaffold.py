# SPDX-License-Identifier: AGPL-3.0-only
"""RED-first tests for the A11 target-authoritative speculative-decode contract scaffold.

STRONG-RED DISCIPLINE (read before trusting a failure in this file): each test
below targets behaviour that revision a7b412a cannot provide. Executed baseline
probes recorded in the A11 implementation contract show (a) no code defines the
``mycelium.speculative_decode_*`` protocols, (b) the legacy M20 runtime validator
accepts ``verified_count > proposed_count``, and (c) the legacy runtime shape
rejects every deadline and circuit-breaker field. Where a test contrasts the
legacy validator with the A11 validator, the legacy half is executed against the
real M20 module so the baseline gap is part of the test itself.

These tests are a scaffold gate only. Passing them proves closed contract
validation and policy arithmetic; it does not prove speculative decoding,
benefit, an enabled decision, a measured-disabled evaluation, or qualification.
"""

from __future__ import annotations

import copy

import pytest

import mycelium_m20_speculation as legacy_m20
import mycelium_speculative_decode as speculative_decode

D1 = "sha256:" + "1" * 64
D2 = "sha256:" + "2" * 64
D3 = "sha256:" + "3" * 64
D4 = "sha256:" + "4" * 64
D5 = "sha256:" + "5" * 64
D6 = "sha256:" + "6" * 64
D7 = "sha256:" + "7" * 64
D8 = "sha256:" + "8" * 64

RUNTIME_BINDING = {
    "deployment_id": "deployment-1",
    "deployment_epoch": 1,
    "graph_digest": D1,
    "membership_generation": 1,
    "qualification_digest": D2,
    "target_runtime_incarnation": D3,
    "draft_runtime_incarnation": D4,
}

LEGACY_BINDING = {
    "deployment_id": "deployment-1",
    "deployment_epoch": 1,
    "graph_digest": D1,
    "membership_generation": 1,
    "model_id": "target-only-model",
    "model_revision": "rev-1",
    "qualification_id": "qualification-1",
    "qualification_digest": D2,
}


def _base_plan() -> dict:
    return {
        "protocol": speculative_decode.PLAN_PROTOCOL,
        "decision": {
            "state": speculative_decode.DISABLED_DECISION,
            "reason": "no_authorized_local_draft",
            "material_gain_threshold": speculative_decode.BENCHMARK_IMPROVEMENT_FRACTION,
            "target_only_fallback": True,
        },
        "target": {
            "role": "target",
            "identity_digest": D1,
            "representation_digest": D2,
            "runtime_incarnation": D3,
        },
        "draft": {
            "role": "draft",
            "identity_digest": D4,
            "representation_digest": D5,
            "runtime_incarnation": D6,
        },
        "verifier": {
            "capability": "a10_target_verifier",
            "qualified_width": 4,
            "target_runtime_binding_digest": D7,
            "status": "unknown",
        },
        "compatibility": {
            "tokenizer_digest_match": None,
            "canonical_token_mapping_match": None,
            "special_token_semantics_match": None,
            "position_semantics_match": None,
            "attention_mask_match": None,
            "context_truncation_match": None,
            "generation_policy": "deterministic_greedy",
            "kv_alias": False,
            "kv_transfer": False,
        },
        "workload": {"workload_id": "workload-compat", "route_digest": D8, "session_generation": 1},
        "proposal_bounds": {"maximum_width": 4, "maximum_count": 32, "maximum_bytes": 512},
        "deadline_policy": {
            "maximum_deadline_ms": speculative_decode.DEADLINE_MAXIMUM_MS,
            "minimum_deadline_ms": speculative_decode.DEADLINE_MINIMUM_MS,
            "warm_p95_multiplier": speculative_decode.DEADLINE_WARM_P95_MULTIPLIER,
            "warm_target_only_p95_tpot_ms": None,
            "remaining_request_deadline_ms": 500.0,
            "source": "measured_warm",
        },
        "circuit_breaker_policy": {
            "consecutive_deadlines": speculative_decode.CIRCUIT_BREAKER_CONSECUTIVE_DEADLINES,
            "rolling_window_cycles": speculative_decode.CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES,
            "cooldown_seconds": speculative_decode.CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        },
        "resource_envelope": {
            "target_kv_bytes": 1024,
            "draft_kv_bytes": 1024,
            "verifier_workspace_bytes": 2048,
            "proposal_buffer_bytes": 512,
            "transport_buffer_bytes": 512,
            "runtime_batch_slots": 2,
            "independent_reservations": True,
        },
        "observation": {
            "sample_count": 0,
            "observed_wall_clock_reduction_fraction": None,
            "paired_lower_bound_fraction": None,
            "ttft_regression_fraction": None,
        },
        "target_only_fallback": True,
        "benchmark_threshold": {
            "improvement_fraction": speculative_decode.BENCHMARK_IMPROVEMENT_FRACTION,
            "paired_lower_bound_fraction": speculative_decode.BENCHMARK_PAIRED_LOWER_BOUND_FRACTION,
            "confidence": speculative_decode.BENCHMARK_CONFIDENCE,
            "maximum_ttft_regression_fraction": speculative_decode.BENCHMARK_MAXIMUM_TTFT_REGRESSION_FRACTION,
        },
        "privacy": speculative_decode.PLAN_PRIVACY_STATEMENT,
    }


def _plan(**mutations: object) -> dict:
    document = _base_plan()
    for path, value in mutations.items():
        target = document
        parts = path.split("__")
        for part in parts[:-1]:
            target = target[part]
        target[parts[-1]] = value
    return speculative_decode.seal(document)


def _enabled_plan() -> dict:
    document = _base_plan()
    document["verifier"]["status"] = "qualified"
    for key in list(document["compatibility"]):
        if key not in {"generation_policy", "kv_alias", "kv_transfer"}:
            document["compatibility"][key] = True
    document["deadline_policy"]["warm_target_only_p95_tpot_ms"] = 80.0
    document["observation"] = {
        "sample_count": 12,
        "observed_wall_clock_reduction_fraction": 0.14,
        "paired_lower_bound_fraction": 0.11,
        "ttft_regression_fraction": 0.02,
    }
    document["decision"] = {
        "state": speculative_decode.ENABLED_DECISION,
        "reason": "material_gain_and_parity_qualified",
        "material_gain_threshold": speculative_decode.BENCHMARK_IMPROVEMENT_FRACTION,
        "target_only_fallback": True,
    }
    return speculative_decode.seal(document)


def _runtime_request(**overrides: object) -> dict:
    request = {
        "request_id": "req-1",
        "mode": "speculative_admitted",
        "proposal_width": 4,
        "proposed_count": 4,
        "verified_count": 4,
        "accepted_count": 3,
        "rejected_count": 1,
        "target_corrected_count": 1,
        "rollback_count": 1,
        "committed_count": 4,
        "target_committed_output_tokens": 4,
        "deadline_outcome": "proposal_arrived",
        "draft_queue_ms": 1.0,
        "draft_compute_ms": 2.0,
        "transfer_ms": 0.5,
        "fallback_reason": "none",
        "cancelled": False,
        "circuit_breaker_state": "closed",
        "cleanup_complete": True,
        "terminal_state": "completed",
    }
    request.update(overrides)
    return request


def _runtime(**overrides: object) -> dict:
    document = {
        "protocol": speculative_decode.RUNTIME_PROTOCOL,
        "binding": copy.deepcopy(RUNTIME_BINDING),
        "mode": "speculative",
        "requests": [_runtime_request()],
    }
    document.update(overrides)
    return speculative_decode.seal(document)


def _window(mode: str, *, wall_clock_ms: float, ttft_p95_ms: float) -> dict:
    return {
        "mode": mode,
        "completed_requests": speculative_decode.BENCHMARK_MINIMUM_COMPLETED_REQUESTS,
        "target_committed_output_tokens": speculative_decode.BENCHMARK_MINIMUM_COMMITTED_TOKENS,
        "prompt_length_buckets": 2,
        "output_length_buckets": 2,
        "wall_clock_ms": wall_clock_ms,
        "ttft_p95_ms": ttft_p95_ms,
        "error_timeout_cancellation_fallback_count": 0,
    }


def _benchmark(*, wall_scale: float = 1.0, decision: dict | None = None) -> dict:
    target_ttft = 100.0
    speculative_ttft = target_ttft * 1.02
    windows = [
        _window("target_only", wall_clock_ms=1000.0 * wall_scale, ttft_p95_ms=target_ttft),
        _window("speculative", wall_clock_ms=880.0 * wall_scale, ttft_p95_ms=speculative_ttft),
        _window("speculative", wall_clock_ms=870.0 * wall_scale, ttft_p95_ms=speculative_ttft),
        _window("target_only", wall_clock_ms=1010.0 * wall_scale, ttft_p95_ms=target_ttft),
        _window("target_only", wall_clock_ms=990.0 * wall_scale, ttft_p95_ms=target_ttft),
        _window("speculative", wall_clock_ms=890.0 * wall_scale, ttft_p95_ms=speculative_ttft),
    ]
    document = {
        "protocol": speculative_decode.BENCHMARK_PROTOCOL,
        "plan_digest": D1,
        "executed": True,
        "paired_order": list(speculative_decode.BENCHMARK_PAIRED_ORDER),
        "warmups": {
            "target_only": speculative_decode.BENCHMARK_WARMUPS_PER_MODE,
            "speculative": speculative_decode.BENCHMARK_WARMUPS_PER_MODE,
        },
        "windows": windows,
        "confidence": speculative_decode.BENCHMARK_CONFIDENCE,
        "valid": True,
        "invalidation_reasons": [],
        "decision": decision
        if decision is not None
        else {
            "state": speculative_decode.ENABLED_DECISION,
            "reason": "material_gain_and_parity_qualified",
            "observed_reduction_fraction": 0.12,
            "paired_lower_bound_fraction": 0.11,
            "ttft_regression_fraction": 0.02,
            "blocking_conditions": [],
        },
    }
    return speculative_decode.seal(document)


# ---------------------------------------------------------------------------
# Protocol vocabulary
# ---------------------------------------------------------------------------


def test_protocols_are_the_capability_named_a11_records() -> None:
    assert speculative_decode.PLAN_PROTOCOL == "mycelium.speculative_decode_plan.v1"
    assert speculative_decode.RUNTIME_PROTOCOL == "mycelium.speculative_decode_runtime.v1"
    assert speculative_decode.BENCHMARK_PROTOCOL == "mycelium.speculative_decode_benchmark.v1"
    assert speculative_decode.QUALIFICATION_PROTOCOL == "mycelium.speculative_decode_qualification.v1"


def test_fixtures_are_closed_self_bound_and_claim_nothing() -> None:
    fixtures = speculative_decode.compatibility_fixtures()

    assert set(fixtures) == {
        "speculative_decode_plan",
        "speculative_decode_runtime",
        "speculative_decode_benchmark",
        "speculative_decode_qualification",
    }
    plan = speculative_decode.validate_speculative_decode_plan(fixtures["speculative_decode_plan"])
    assert plan["decision"]["state"] == speculative_decode.DISABLED_DECISION
    assert plan["decision"]["reason"] == "no_authorized_local_draft"
    runtime = speculative_decode.validate_speculative_decode_runtime(fixtures["speculative_decode_runtime"])
    assert runtime["requests"][0]["proposed_count"] == 0
    benchmark = speculative_decode.validate_speculative_decode_benchmark(fixtures["speculative_decode_benchmark"])
    assert benchmark["executed"] is False
    assert benchmark["decision"]["observed_reduction_fraction"] is None
    qualification = speculative_decode.validate_speculative_decode_qualification(
        fixtures["speculative_decode_qualification"]
    )
    assert qualification["executed"] is False
    assert qualification["result"] == "not_executed"
    assert qualification["projection"] is None


# ---------------------------------------------------------------------------
# Plan contract
# ---------------------------------------------------------------------------


def test_plan_rejects_unknown_field() -> None:
    document = _plan()
    document["extra"] = "not-in-contract"
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(speculative_decode.seal(document))
    assert error.value.code == "speculative_decode_plan_invalid"


def test_plan_rejects_missing_field() -> None:
    document = _plan()
    del document["resource_envelope"]
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(document)
    assert error.value.code == "speculative_decode_plan_invalid"


def test_plan_rejects_equal_target_and_draft_identity() -> None:
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(_plan(draft__identity_digest=D1))
    assert error.value.code == "duplicate_model_identity"


def test_plan_rejects_planner_prediction_as_deadline_source() -> None:
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(
            _plan(deadline_policy__source="planner_prediction")
        )
    assert error.value.code == "planner_prediction_not_authoritative"


def test_plan_rejects_unsupported_generation_policy() -> None:
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(
            _plan(**{"compatibility__generation_policy": "unseeded_sampling"})
        )
    assert error.value.code == "unsupported_generation_policy"

    document = _base_plan()
    document["compatibility"]["generation_policy"] = "unseeded_sampling"
    document["decision"]["reason"] = "incompatible_token_semantics"
    evaluated = speculative_decode.validate_speculative_decode_plan(speculative_decode.seal(document))
    assert evaluated["decision"]["state"] == speculative_decode.DISABLED_DECISION


def test_plan_rejects_a_capability_reason_carrying_a_fabricated_measurement() -> None:
    document = _base_plan()
    document["observation"] = {
        "sample_count": 1,
        "observed_wall_clock_reduction_fraction": 0.0,
        "paired_lower_bound_fraction": None,
        "ttft_regression_fraction": None,
    }
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(speculative_decode.seal(document))
    assert error.value.code == "capability_reason_may_not_carry_measurement"


def test_plan_performance_reason_requires_a_real_sample() -> None:
    document = _base_plan()
    document["decision"]["reason"] = "material_gain_not_observed"
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(speculative_decode.seal(document))
    assert error.value.code == "measurement_required_for_performance_reason"

    document = _base_plan()
    document["decision"]["reason"] = "material_gain_not_observed"
    document["observation"] = {
        "sample_count": 12,
        "observed_wall_clock_reduction_fraction": 0.04,
        "paired_lower_bound_fraction": 0.02,
        "ttft_regression_fraction": 0.01,
    }
    evaluated = speculative_decode.validate_speculative_decode_plan(speculative_decode.seal(document))
    assert evaluated["decision"]["state"] == speculative_decode.DISABLED_DECISION


def test_plan_rejects_enabled_decision_below_the_frozen_threshold() -> None:
    document = _enabled_plan()
    document["observation"]["observed_wall_clock_reduction_fraction"] = 0.05
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_plan(speculative_decode.seal(document))
    assert error.value.code == "enabled_decision_below_material_threshold"


def test_plan_accepts_enabled_decision_at_the_frozen_threshold() -> None:
    evaluated = speculative_decode.validate_speculative_decode_plan(_enabled_plan())

    assert evaluated["decision"]["state"] == speculative_decode.ENABLED_DECISION
    assert evaluated["decision"]["target_only_fallback"] is True
    assert evaluated["benchmark_threshold"]["improvement_fraction"] == 0.10


# ---------------------------------------------------------------------------
# Runtime contract
# ---------------------------------------------------------------------------


def test_runtime_rejects_verified_exceeding_proposed() -> None:
    document = _runtime(requests=[_runtime_request(proposed_count=1, verified_count=5, accepted_count=5)])
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(document)
    assert error.value.code == "impossible_runtime_counts"


def test_runtime_rejects_accepted_plus_rejected_exceeding_verified() -> None:
    document = _runtime(
        requests=[_runtime_request(accepted_count=4, rejected_count=4, target_corrected_count=4)]
    )
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(document)
    assert error.value.code == "impossible_runtime_counts"


def test_runtime_requires_every_committed_position_target_authoritative() -> None:
    document = _runtime(requests=[_runtime_request(committed_count=5)])
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(document)
    assert error.value.code == "committed_positions_not_target_authoritative"


def test_runtime_rejects_target_loss_reported_as_draft_fallback() -> None:
    document = _runtime(requests=[_runtime_request(fallback_reason="target_loss")])
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(document)
    assert error.value.code == "target_loss_is_not_draft_fallback"


def test_runtime_target_only_mode_carries_no_proposals() -> None:
    request = _runtime_request(
        mode="target_only",
        proposal_width=0,
        proposed_count=0,
        verified_count=0,
        accepted_count=0,
        rejected_count=0,
        target_corrected_count=0,
        rollback_count=0,
        committed_count=0,
        target_committed_output_tokens=0,
        deadline_outcome="none",
        draft_queue_ms=None,
        draft_compute_ms=None,
        transfer_ms=None,
    )
    evaluated = speculative_decode.validate_speculative_decode_runtime(
        _runtime(mode="target_only", requests=[request])
    )
    assert evaluated["mode"] == "target_only"

    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(
            _runtime(mode="target_only", requests=[_runtime_request()])
        )
    assert error.value.code == "target_only_mode_carries_proposals"


def test_runtime_requires_cleanup_and_terminal_state_consistency() -> None:
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(
            _runtime(requests=[_runtime_request(cleanup_complete=False)])
        )
    assert error.value.code == "speculative_decode_runtime_invalid"

    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(
            _runtime(requests=[_runtime_request(cancelled=True, terminal_state="completed")])
        )
    assert error.value.code == "speculative_decode_runtime_invalid"


def test_baseline_witness_legacy_validator_accepts_counts_a11_rejects() -> None:
    """The a7b412a gap, executed against the real legacy module, then closed by A11."""

    legacy_request = {
        "request_id": "req-1",
        "proposal_width": 4,
        "proposed_count": 1,
        "target_verified_count": 5,
        "accepted_count": 5,
        "rejected_count": 0,
        "rollback_count": 0,
        "fallback_state": "none",
        "terminal_state": "completed",
        "cleanup_complete": True,
    }
    legacy_document = legacy_m20.build_speculative_runtime(
        binding=LEGACY_BINDING, mode="speculative", requests=[legacy_request]
    )
    assert legacy_document["requests"][0]["target_verified_count"] == 5  # baseline cannot see the violation

    a11_document = _runtime(requests=[_runtime_request(proposed_count=1, verified_count=5, accepted_count=5)])
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_runtime(a11_document)
    assert error.value.code == "impossible_runtime_counts"


# ---------------------------------------------------------------------------
# Adaptive deadline and circuit breaker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "warm_p95,remaining,expected_admitted,expected_reason",
    [
        (80.0, 500.0, 60.0, "within_bounds"),
        (4.0, 500.0, 5.0, "floor_applied"),
        (200.0, 500.0, 100.0, "ceiling_applied"),
        (80.0, 20.0, 20.0, "request_deadline_clamp"),
    ],
)
def test_deadline_formula_follows_the_frozen_clamp(
    warm_p95: float, remaining: float, expected_admitted: float, expected_reason: str
) -> None:
    admitted = speculative_decode.admit_proposal_deadline(
        warm_target_only_p95_tpot_ms=warm_p95, remaining_request_deadline_ms=remaining
    )

    assert admitted["admissible"] is True
    assert admitted["admitted_deadline_ms"] == pytest.approx(expected_admitted)
    assert admitted["clamp_reason"] == expected_reason


def test_deadline_unknown_warm_baseline_never_becomes_a_default() -> None:
    admitted = speculative_decode.admit_proposal_deadline(
        warm_target_only_p95_tpot_ms=None, remaining_request_deadline_ms=500.0
    )

    assert admitted["admissible"] is False
    assert admitted["admitted_deadline_ms"] is None
    assert admitted["clamp_reason"] == speculative_decode.DEADLINE_UNKNOWN_REASON


def test_circuit_breaker_opens_after_three_consecutive_deadlines() -> None:
    cycles = [
        {"deadline_missed": False, "speculative_cycle_ms": 9.0, "equivalent_target_only_ms": 10.0},
        {"deadline_missed": True, "speculative_cycle_ms": None, "equivalent_target_only_ms": None},
        {"deadline_missed": True, "speculative_cycle_ms": None, "equivalent_target_only_ms": None},
        {"deadline_missed": True, "speculative_cycle_ms": None, "equivalent_target_only_ms": None},
    ]
    decision = speculative_decode.circuit_breaker_decision(cycles=cycles)

    assert decision["state"] == "open_target_only"
    assert decision["reason"] == "consecutive_proposal_deadlines"
    assert decision["cooldown_seconds"] == speculative_decode.CIRCUIT_BREAKER_COOLDOWN_SECONDS


def test_circuit_breaker_revokes_admission_on_parity_or_compatibility_violation() -> None:
    decision = speculative_decode.circuit_breaker_decision(parity_violation=True)

    assert decision["state"] == "revoked_requalification_required"
    assert decision["reason"] == "compatibility_or_parity_violation"
    assert decision["cooldown_seconds"] is None


def test_circuit_breaker_opens_on_a_rolling_window_without_improvement() -> None:
    flat = [
        {"deadline_missed": False, "speculative_cycle_ms": 10.0, "equivalent_target_only_ms": 10.0}
        for _ in range(speculative_decode.CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES)
    ]
    decision = speculative_decode.circuit_breaker_decision(cycles=flat)

    assert decision["state"] == "open_target_only"
    assert decision["reason"] == "rolling_performance_window_not_improved"


def test_circuit_breaker_stays_closed_on_genuine_improvement() -> None:
    improving = [
        {"deadline_missed": False, "speculative_cycle_ms": 8.0, "equivalent_target_only_ms": 10.0}
        for _ in range(speculative_decode.CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES)
    ]
    decision = speculative_decode.circuit_breaker_decision(cycles=improving)

    assert decision["state"] == "closed"
    assert decision["reason"] == "within_policy"
    assert decision["cooldown_seconds"] is None


# ---------------------------------------------------------------------------
# Benchmark and qualification contracts
# ---------------------------------------------------------------------------


def test_benchmark_requires_the_frozen_pair_order() -> None:
    document = _benchmark()
    document["windows"] = list(reversed(document["windows"]))
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_benchmark(speculative_decode.seal(document))
    assert error.value.code == "benchmark_window_order_invalid"


def test_benchmark_requires_minimum_window_size_and_buckets() -> None:
    document = _benchmark()
    document["windows"][0]["completed_requests"] = 11
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_benchmark(speculative_decode.seal(document))
    assert error.value.code == "benchmark_window_undersized"

    document = _benchmark()
    document["windows"][0]["target_committed_output_tokens"] = 383
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_benchmark(speculative_decode.seal(document))
    assert error.value.code == "benchmark_window_undersized"

    document = _benchmark()
    document["windows"][0]["prompt_length_buckets"] = 1
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_benchmark(speculative_decode.seal(document))
    assert error.value.code == "benchmark_window_undersized"


def test_benchmark_decision_must_match_the_measured_windows() -> None:
    document = _benchmark()
    document["decision"]["state"] = speculative_decode.DISABLED_DECISION
    document["decision"]["reason"] = "material_gain_not_observed"
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_benchmark(speculative_decode.seal(document))
    assert error.value.code == "benchmark_decision_inconsistent"


def test_benchmark_disabled_decision_is_a_truthful_unexecuted_evaluation() -> None:
    document = _benchmark()
    document["executed"] = False
    document["windows"] = []
    document["valid"] = False
    document["invalidation_reasons"] = ["missing_samples"]
    document["decision"] = {
        "state": speculative_decode.DISABLED_DECISION,
        "reason": "confidence_bound_not_met",
        "observed_reduction_fraction": None,
        "paired_lower_bound_fraction": None,
        "ttft_regression_fraction": None,
        "blocking_conditions": ["benchmark_pair_invalid"],
    }
    evaluated = speculative_decode.validate_speculative_decode_benchmark(speculative_decode.seal(document))

    assert evaluated["decision"]["state"] == speculative_decode.DISABLED_DECISION
    assert evaluated["decision"]["observed_reduction_fraction"] is None


def test_qualification_scaffold_cannot_claim_executed_evidence() -> None:
    document = speculative_decode.compatibility_fixtures()["speculative_decode_qualification"]
    document["executed"] = True
    with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
        speculative_decode.validate_speculative_decode_qualification(speculative_decode.seal(document))
    assert error.value.code == "qualification_execution_requires_physical_pipeline"


def test_public_projection_rejects_private_material() -> None:
    good = speculative_decode.validate_public_projection({"accepted_count": 3, "state": "measured_disabled"})
    assert good["accepted_count"] == 3

    for key in ("prompt_text", "output_text", "raw_token_ids", "logits", "kv_bytes", "sampler_state"):
        with pytest.raises(speculative_decode.SpeculativeDecodeContractError) as error:
            speculative_decode.validate_public_projection({key: "x"})
        assert error.value.code == "public_projection_carries_private_fields"
