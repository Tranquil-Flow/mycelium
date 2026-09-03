from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path

import pytest

from .materiality_harness import (
    evaluate_benchmark,
    load_protocol,
    protocol_digest,
    workload_manifest_digest,
)


SPEC = (
    Path(__file__).parents[2]
    / "docs/superpowers/specs/2026-08-18-mycelium-a5-multistage-replication.md"
)
PROTOCOL_FIELDS = {
    "protocol",
    "gate_state",
    "claim_boundary",
    "metric",
    "confidence",
    "schedule",
    "identical_binding_fields",
    "allowed_mode_specific_binding_fields",
    "workspace_projections",
    "physical_gate_matrix",
    "retained_outcomes",
    "invalidation_rules",
    "decisions",
    "qualification_claim",
    "promotion_authorized",
}
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
PHYSICAL_GATE_MATRIX = {
    "positive": [
        "qualified_replica_yields_complete_legal_tracks",
        "ordinary_browser_requests_use_distinct_complete_tracks",
        "per_placement_work_parity_kv_and_cleanup_proven",
        "paired_material_throughput_without_regression",
    ],
    "negative": [
        "replica_loss_rejects_affected_track_admission",
        "lost_placement_request_aborts_without_continuity_claim",
        "unaffected_admitted_request_completes",
        "invalid_or_neutral_candidate_preserves_incumbent",
    ],
    "ordinary_browser_gateway_required": True,
    "qualification_claim": False,
}
RETAINED_OUTCOMES = {
    "completed",
    "error",
    "timeout",
    "cancelled",
    "rejected_admission",
    "cleanup_failure",
}
IDENTICAL_BINDING_FIELDS = [
    "model_id",
    "model_revision",
    "representation_digest",
    "base_deployment_id",
    "route_generation",
    "workload_digest",
    "arrival_schedule_digest",
    "prompt_output_buckets_digest",
    "qos_mix_digest",
    "offered_concurrency",
    "token_limits_digest",
    "product_session_digest",
    "software_digest",
    "configuration_digest",
    "instrumentation_digest",
]
INVALIDATION_RULES = [
    "authority_stale",
    "binding_mismatch",
    "duplicate_or_missing_window",
    "fewer_than_minimum_requests",
    "incomplete_outcome_accounting",
    "member_or_runtime_incarnation_changed",
    "nonfinite_measurement",
    "posthoc_exclusion_or_threshold_change",
    "route_changed",
    "server_restarted",
    "software_configuration_or_instrumentation_changed",
    "thermal_or_power_envelope_drift",
    "window_order_or_pairing_changed",
    "workload_or_session_changed",
]


def _bindings(mode: str) -> dict:
    protocol = load_protocol()
    values = {
        name: (4 if name == "offered_concurrency" else f"bound-{name}")
        for name in protocol["identical_binding_fields"]
    }
    values["qualified_track_policy_digest"] = f"sha256:policy-{mode}"
    return values


def _outcomes(*, completed: int = 60, error: int = 0) -> dict[str, int]:
    return {
        "completed": completed,
        "error": error,
        "timeout": 0,
        "cancelled": 0,
        "rejected_admission": 0,
        "cleanup_failure": 0,
    }


def _run(improvements: list[float]) -> dict:
    protocol = load_protocol()
    assert len(improvements) == 6
    warmups = [
        {
            "mode": mode,
            "scored": False,
            "request_count": 60,
            "outcomes": _outcomes(),
            "bindings": _bindings(mode),
            "invalidations": [],
        }
        for mode in ("baseline", "candidate")
    ]
    windows = []
    for expected in protocol["schedule"]["measured_windows"]:
        mode = expected["mode"]
        rate = 100.0 if mode == "baseline" else 100.0 * (
            1.0 + improvements[expected["pair_id"]]
        )
        windows.append(
            {
                **expected,
                "request_count": 60,
                "completed_generated_tokens": round(rate * 1000),
                "wall_clock_seconds": 1000.0,
                "outcomes": _outcomes(),
                "bindings": _bindings(mode),
                "invalidations": [],
            }
        )
    return {
        "protocol": "mycelium.a5_benchmark_run_fixture.v1",
        "benchmark_protocol_digest": protocol_digest(protocol),
        "workload_manifest_digest": workload_manifest_digest(),
        "warmups": warmups,
        "windows": windows,
    }


def test_manifest_freezes_closed_design_only_materiality_protocol() -> None:
    protocol = load_protocol()
    assert set(protocol) == PROTOCOL_FIELDS
    assert protocol["protocol"] == "mycelium.a5_benchmark_acceptance.v1"
    assert protocol["gate_state"] == "design_only"
    assert protocol["qualification_claim"] is False
    assert protocol["promotion_authorized"] is False
    assert protocol["metric"]["material_threshold_fraction"] == 0.10
    assert protocol["confidence"] == {
        "method": "paired_nonparametric_bootstrap",
        "confidence_level": 0.95,
        "lower_quantile": 0.025,
        "resamples": 10_000,
        "sample_size_pairs": 6,
        "seed": 0xA5,
        "statistic": "arithmetic_mean",
        "quantile_method": "nearest_rank_ceiling",
    }
    schedule = protocol["schedule"]
    assert schedule["warmup_modes"] == ["baseline", "candidate"]
    assert schedule["warmup_scored"] is False
    assert schedule["minimum_requests_per_warmup"] == 60
    assert schedule["abba_repetitions"] == 3
    assert schedule["minimum_requests_per_measured_window"] == 60
    assert [item["mode"] for item in schedule["measured_windows"]] == [
        "baseline",
        "candidate",
        "candidate",
        "baseline",
    ] * 3
    assert [item["pair_id"] for item in schedule["measured_windows"]] == [
        0,
        0,
        1,
        1,
        2,
        2,
        3,
        3,
        4,
        4,
        5,
        5,
    ]
    assert set(protocol["retained_outcomes"]) == RETAINED_OUTCOMES
    assert protocol["identical_binding_fields"] == IDENTICAL_BINDING_FIELDS
    assert protocol["allowed_mode_specific_binding_fields"] == [
        "mode",
        "qualified_track_policy_digest",
    ]
    assert protocol["invalidation_rules"] == INVALIDATION_RULES
    assert protocol["decisions"] == ["inconclusive", "material", "not_material"]


def test_spec_and_manifest_freeze_the_same_claim_boundary() -> None:
    text = SPEC.read_text("utf-8")
    for phrase in (
        "`ABBA` repeated three times",
        "at least 60 terminal requests",
        "paired 95% bootstrap lower confidence bound",
        "at least **10%**",
        "`inconclusive`",
        "`qualification_claim=false`",
        "`promotion_authorized=false`",
        "`design_only`",
    ):
        assert phrase in text


def test_all_workspace_and_physical_positive_negative_matrices_are_closed() -> None:
    protocol = load_protocol()
    projections = protocol["workspace_projections"]
    assert set(projections) == WORKSPACES
    for requirements in projections.values():
        assert isinstance(requirements, list) and requirements
        assert len(requirements) == len(set(requirements))

    assert protocol["physical_gate_matrix"] == PHYSICAL_GATE_MATRIX
    assert set(PHYSICAL_GATE_MATRIX["positive"]).isdisjoint(
        PHYSICAL_GATE_MATRIX["negative"]
    )


def test_material_requires_point_estimate_and_paired_lower_bound_at_ten_percent() -> None:
    result = evaluate_benchmark(_run([0.12] * 6))

    assert result["decision"] == "material"
    assert result["point_estimate_fraction"] == pytest.approx(0.12)
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] == pytest.approx(
        0.12
    )
    assert result["reasons"] == []
    assert result["qualification_claim"] is False
    assert result["promotion_authorized"] is False


def test_exact_ten_percent_point_and_lower_bound_are_material() -> None:
    result = evaluate_benchmark(_run([0.10] * 6))

    assert result["point_estimate_fraction"] == pytest.approx(0.10)
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] == pytest.approx(
        0.10
    )
    assert result["decision"] == "material"


def test_valid_subthreshold_result_is_honestly_not_material() -> None:
    result = evaluate_benchmark(_run([0.08] * 6))

    assert result["decision"] == "not_material"
    assert result["reasons"] == ["point_estimate_below_material_threshold"]


def test_point_gain_without_paired_confidence_is_honestly_inconclusive() -> None:
    result = evaluate_benchmark(_run([0.0, 0.0, 0.0, 0.2, 0.2, 0.2]))

    assert result["point_estimate_fraction"] == pytest.approx(0.10)
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] < 0.10
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [
        "paired_confidence_lower_bound_below_material_threshold"
    ]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda run: run["windows"].pop(), "duplicate_or_missing_window"),
        (
            lambda run: run["windows"][0].update(request_count=59, outcomes=_outcomes(completed=59)),
            "fewer_than_minimum_requests",
        ),
        (
            lambda run: run["windows"][0].update(mode="candidate"),
            "window_order_or_pairing_changed",
        ),
        (
            lambda run: run["warmups"][0].update(scored=True),
            "warmup_invalid",
        ),
        (
            lambda run: run["warmups"][0].update(
                request_count=59,
                outcomes=_outcomes(completed=59),
            ),
            "warmup_invalid",
        ),
        (
            lambda run: run["windows"][0]["bindings"].update(
                product_session_digest="changed-session"
            ),
            "workload_or_session_changed",
        ),
        (
            lambda run: run["windows"][0].update(wall_clock_seconds=math.inf),
            "nonfinite_measurement",
        ),
    ],
)
def test_missing_ordered_unbound_or_nonfinite_inputs_are_inconclusive(
    mutation,
    reason: str,
) -> None:
    run = _run([0.12] * 6)
    mutation(run)

    result = evaluate_benchmark(run)

    assert result["decision"] == "inconclusive"
    assert reason in result["reasons"]
    assert result["qualification_claim"] is False
    assert result["promotion_authorized"] is False


def test_every_frozen_invalidation_rule_yields_inconclusive() -> None:
    for invalidation in load_protocol()["invalidation_rules"]:
        run = _run([0.12] * 6)
        run["windows"][0]["invalidations"] = [invalidation]

        result = evaluate_benchmark(run)

        assert result["decision"] == "inconclusive"
        assert invalidation in result["reasons"]


@pytest.mark.parametrize("invalidations", [["unknown_rule"], [{}]])
def test_malformed_invalidation_entry_fails_closed(invalidations: list[object]) -> None:
    run = _run([0.12] * 6)
    run["windows"][0]["invalidations"] = invalidations

    result = evaluate_benchmark(run)

    assert result["decision"] == "inconclusive"
    assert result["reasons"] == ["invalidation_rule_invalid"]


def test_noncompletion_outcomes_remain_in_accounting_without_exclusion() -> None:
    run = _run([0.12] * 6)
    for window in run["windows"]:
        window["outcomes"] = _outcomes(completed=55, error=5)

    result = evaluate_benchmark(run)

    assert result["decision"] == "material"
    assert all(sum(window["outcomes"].values()) == 60 for window in run["windows"])


def test_protocol_digest_or_unknown_run_fields_fail_closed_as_inconclusive() -> None:
    wrong_digest = _run([0.12] * 6)
    wrong_digest["benchmark_protocol_digest"] = "sha256:" + "0" * 64
    assert evaluate_benchmark(wrong_digest)["reasons"] == [
        "benchmark_protocol_digest_mismatch"
    ]

    widened = deepcopy(_run([0.12] * 6))
    widened["route_ready"] = True
    assert evaluate_benchmark(widened)["reasons"] == [
        "benchmark_run_shape_invalid"
    ]
