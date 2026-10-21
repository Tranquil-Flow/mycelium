# SPDX-License-Identifier: AGPL-3.0-only
"""Protocol v2: the pre-registered calibrated-offered-load amendment.

Owner ruling 2026-09-27 after the r33 underpowered result: a recorded
baseline-only calibration selects the offered rate before any scored window; a
baseline window completing under 90% of offered requests is invalid, excluded
with its pair and recorded; exactly 16 pairs are scheduled, at least 12 of
them must be valid, else the decision is inconclusive, and every valid pair is
scored with no selection or trimming (owner refinement of the same date, made
before any v2 data existed); the +10% paired-bootstrap lower-bound threshold
and every other v1 threshold are unchanged.  The r33 result stays on record under
v1 as inconclusive and is never re-scored as a pass.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import random

import pytest

import scripts.run_a5_benchmark_gate as benchmark_gate

from . import materiality_harness as harness
from .materiality_harness import (
    CALIBRATION_FAILED_REASON,
    INVALID_WINDOW_REASON,
    SCHEDULE_NOT_REGISTERED_REASON,
    TOO_FEW_VALID_PAIRS_REASON,
    V1_PROTOCOL_DIGEST,
    baseline_window_is_valid,
    decide_calibrated_pairs,
    evaluate_benchmark,
    evaluate_benchmark_v2,
    load_protocol,
    load_protocol_v2,
    protocol_digest,
    protocol_v2_digest,
    validate_calibration,
    workload_manifest_digest,
)
from .test_product_benchmark_gate import _SlowSuccessfulSession


RATE = 1.0
LADDER = [4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625]
# r33 (a5-multistage-20260822/final-v190-r33/a5-benchmark.json, v1 digest
# 5b6f3c83...): per measured window (index order) the completed count,
# cancelled count, rejected_admission count, timeout count, completed tokens
# and measured wall-clock seconds, copied verbatim.
R33_WINDOWS = [
    (5, 52, 3, 0, 67, 393.3066113329987),
    (8, 49, 3, 0, 128, 383.8234665409982),
    (9, 48, 3, 0, 192, 386.44576016599603),
    (15, 42, 3, 0, 341, 372.46467758399376),
    (6, 49, 4, 1, 103, 378.33283649999066),
    (8, 49, 3, 0, 252, 392.52600758300105),
    (4, 53, 3, 0, 49, 381.1773251660052),
    (8, 49, 3, 0, 210, 376.86894079099875),
    (10, 46, 4, 0, 165, 368.7689203339978),
    (7, 50, 3, 0, 192, 381.93701116601005),
    (7, 49, 4, 0, 134, 381.1609532080038),
    (8, 49, 3, 0, 169, 379.75086025001656),
]
R33_POINT = 0.16710745942796965
R33_LOWER_BOUND = -0.4196995766436599


def _outcomes(
    *,
    completed: int = 60,
    cancelled: int = 0,
    rejected_admission: int = 0,
    timeout: int = 0,
) -> dict[str, int]:
    return {
        "completed": completed,
        "error": 0,
        "timeout": timeout,
        "cancelled": cancelled,
        "rejected_admission": rejected_admission,
        "cleanup_failure": 0,
    }


def _bindings(mode: str, protocol: dict) -> dict:
    values = {
        name: (4 if name == "offered_concurrency" else f"bound-{name}")
        for name in protocol["identical_binding_fields"]
    }
    values["qualified_track_policy_digest"] = f"sha256:policy-{mode}"
    return values


def _calibration(completions: list[int], *, offered: int = 60) -> dict:
    trials = [
        {
            "ordinal": ordinal,
            "offered_arrival_rate_per_second": LADDER[ordinal],
            "request_count": 60,
            "offered_request_count": offered,
            "outcomes": _outcomes(completed=completed, cancelled=60 - completed),
        }
        for ordinal, completed in enumerate(completions)
    ]
    selected = next(
        (
            trial["offered_arrival_rate_per_second"]
            for trial in trials
            if trial["outcomes"]["completed"] * 10 >= offered * 9
        ),
        None,
    )
    return {
        "mode": "baseline",
        "scored": False,
        "rate_ladder_per_second": list(LADDER),
        "trials": trials,
        "selected_offered_arrival_rate_per_second": selected,
    }


def _run_v2(
    improvements: list[float],
    *,
    baseline_completed: dict[int, int] | None = None,
    candidate_completed: dict[int, int] | None = None,
) -> dict:
    """A v2 run fixture calibrated at 1.0 req/s (third ladder rung)."""

    protocol = load_protocol_v2()
    assert len(improvements) == 16
    baseline_completed = baseline_completed or {}
    candidate_completed = candidate_completed or {}
    warmups = [
        {
            "mode": mode,
            "scored": False,
            "request_count": 60,
            "offered_arrival_rate_per_second": RATE,
            "outcomes": _outcomes(),
            "bindings": _bindings(mode, protocol),
            "invalidations": [],
        }
        for mode in ("baseline", "candidate")
    ]
    windows = []
    for expected in protocol["schedule"]["measured_windows"]:
        mode = expected["mode"]
        pair = expected["pair_id"]
        rate = 100.0 if mode == "baseline" else 100.0 * (1.0 + improvements[pair])
        completed = (baseline_completed if mode == "baseline" else candidate_completed).get(
            pair, 60
        )
        windows.append(
            {
                **expected,
                "request_count": 60,
                "offered_request_count": 60,
                "offered_arrival_rate_per_second": RATE,
                "completed_generated_tokens": round(rate * 1000),
                "wall_clock_seconds": 1000.0,
                "outcomes": _outcomes(completed=completed, cancelled=60 - completed),
                "bindings": _bindings(mode, protocol),
                "invalidations": [],
            }
        )
    return {
        "protocol": "mycelium.a5_benchmark_run_fixture.v2",
        "benchmark_protocol_digest": protocol_v2_digest(protocol),
        "workload_manifest_digest": workload_manifest_digest(),
        "calibration": _calibration([12, 40, 58]),
        "warmups": warmups,
        "windows": windows,
    }


def _pairs(improvements: list[float], *, invalid: set[int] = frozenset()) -> list[dict]:
    return [
        {
            "pair_id": pair_id,
            "baseline_index": pair_id * 2,
            "baseline_completed": 50 if pair_id in invalid else 60,
            "baseline_offered": 60,
            "baseline_rate": 100.0,
            "candidate_rate": 100.0 * (1.0 + gain),
        }
        for pair_id, gain in enumerate(improvements)
    ]


# ---------------------------------------------------------------------------
# The record: v1 is frozen, r33 stays inconclusive, v2 records the ruling
# ---------------------------------------------------------------------------


def test_v1_protocol_is_unchanged_and_still_binds_the_r33_digest() -> None:
    assert protocol_digest(load_protocol()) == V1_PROTOCOL_DIGEST


def test_v2_records_the_owner_ruling_and_supersedes_v1_without_rescoring_r33() -> None:
    protocol = load_protocol_v2()
    assert protocol["protocol"] == "mycelium.a5_benchmark_acceptance.v2"
    assert protocol["protocol_version"] == 2
    amendment = protocol["amendment"]
    assert amendment["ruling_date"] == "2026-09-27"
    assert amendment["ruling"] == (
        "owner ruling: pre-registered amendment after r33 underpowered result"
    )
    assert amendment["pre_registered"] is True
    assert amendment["applies_identically_to"] == ["A5", "A10"]
    assert amendment["supersedes"]["benchmark_protocol_digest"] == V1_PROTOCOL_DIGEST
    on_record = amendment["result_on_record"]
    assert on_record["decision"] == "inconclusive"
    assert on_record["rescored"] is False
    assert on_record["benchmark_protocol_digest"] == V1_PROTOCOL_DIGEST
    assert on_record["point_estimate_fraction"] == R33_POINT
    assert on_record["paired_95_percent_bootstrap_lower_bound_fraction"] == (
        R33_LOWER_BOUND
    )
    assert harness.amended_protocol_violations(protocol) == []
    assert protocol_v2_digest(protocol) != V1_PROTOCOL_DIGEST


def test_v2_keeps_every_v1_threshold_and_binding_unchanged() -> None:
    v1 = load_protocol()
    v2 = load_protocol_v2()
    assert v2["metric"] == v1["metric"]
    assert v2["metric"]["material_threshold_fraction"] == 0.1
    v1_confidence = dict(v1["confidence"])
    assert v1_confidence.pop("sample_size_pairs") == 6
    v2_confidence = dict(v2["confidence"])
    assert v2_confidence.pop("minimum_valid_pairs") == 12
    assert v2_confidence.pop("scheduled_pairs") == 16
    assert v2_confidence.pop("sample") == "valid_pairs_only"
    assert v2_confidence.pop("pair_selection") == harness.PAIR_SELECTION
    assert v2_confidence == v1_confidence
    for field in (
        "identical_binding_fields",
        "allowed_mode_specific_binding_fields",
        "workspace_projections",
        "physical_gate_matrix",
        "retained_outcomes",
        "invalidation_rules",
        "decisions",
        "claim_boundary",
        "gate_state",
        "qualification_claim",
        "promotion_authorized",
    ):
        assert v2[field] == v1[field], field
    for field in (
        "warmup_modes",
        "warmup_scored",
        "minimum_requests_per_warmup",
        "minimum_requests_per_measured_window",
    ):
        assert v2["schedule"][field] == v1["schedule"][field], field
    assert harness.MATERIAL_THRESHOLD_FRACTION == v1["metric"]["material_threshold_fraction"]
    assert harness.BOOTSTRAP_RESAMPLES == v1["confidence"]["resamples"]
    assert harness.BOOTSTRAP_SEED == v1["confidence"]["seed"]
    assert harness.BOOTSTRAP_LOWER_QUANTILE == v1["confidence"]["lower_quantile"]


def test_v2_schedule_is_abba_eight_times_with_sixteen_complete_pairs() -> None:
    protocol = load_protocol_v2()
    schedule = protocol["schedule"]
    windows = schedule["measured_windows"]
    assert schedule["abba_repetitions"] == 8
    assert protocol["confidence"]["scheduled_pairs"] == harness.SCHEDULED_PAIRS == 16
    assert protocol["confidence"]["minimum_valid_pairs"] == 12
    assert [item["index"] for item in windows] == list(range(32))
    assert [item["mode"] for item in windows] == [
        "baseline",
        "candidate",
        "candidate",
        "baseline",
    ] * 8
    assert [item["block"] for item in windows] == [b for b in range(8) for _ in range(4)]
    assert harness.scheduled_windows_are_registered(windows)
    for pair_id in range(16):
        modes = sorted(item["mode"] for item in windows if item["pair_id"] == pair_id)
        assert modes == ["baseline", "candidate"]
    calibration = protocol["calibration"]
    assert calibration["mode"] == "baseline"
    assert calibration["scored"] is False
    assert calibration["precedes_warmups_and_scored_windows"] is True
    assert calibration["rate_ladder_per_second"] == LADDER
    assert calibration["rate_ladder_per_second"] == sorted(LADDER, reverse=True)
    assert calibration["minimum_baseline_completion_fraction"] == 0.9
    assert protocol["window_validity"]["minimum_baseline_completion_fraction"] == 0.9
    assert protocol["window_validity"]["applies_to_mode"] == "baseline"


# ---------------------------------------------------------------------------
# Decisions under the amendment
# ---------------------------------------------------------------------------


def test_sixteen_valid_pairs_at_twelve_percent_are_material() -> None:
    result = evaluate_benchmark_v2(_run_v2([0.12] * 16))
    assert result["decision"] == "material"
    assert result["reasons"] == []
    assert result["scheduled_pairs"] == 16
    assert result["valid_pair_count"] == 16
    assert result["excluded_windows"] == []
    assert result["calibrated_offered_arrival_rate_per_second"] == RATE
    assert result["point_estimate_fraction"] == pytest.approx(0.12)
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] == pytest.approx(0.12)
    assert result["qualification_claim"] is False
    assert result["promotion_authorized"] is False


def test_threshold_is_unchanged_exact_ten_percent_passes_and_below_does_not() -> None:
    assert evaluate_benchmark_v2(_run_v2([0.10] * 16))["decision"] == "material"
    below = evaluate_benchmark_v2(_run_v2([0.099] * 16))
    assert below["decision"] == "not_material"
    assert below["reasons"] == ["point_estimate_below_material_threshold"]
    spread = evaluate_benchmark_v2(_run_v2([0.0] * 8 + [0.2] * 8))
    assert spread["point_estimate_fraction"] == pytest.approx(0.10)
    assert spread["paired_95_percent_bootstrap_lower_bound_fraction"] < 0.10
    assert spread["decision"] == "inconclusive"
    assert spread["reasons"] == [
        "paired_confidence_lower_bound_below_material_threshold"
    ]


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("metric", "material_threshold_fraction", 0.05),
        ("confidence", "minimum_valid_pairs", 6),
        ("confidence", "scheduled_pairs", 12),
        ("confidence", "scheduled_pairs", 20),
        ("confidence", "pair_selection", "best_12_of_16_valid_pairs"),
        ("confidence", "lower_quantile", 0.05),
        ("confidence", "resamples", 100),
        ("calibration", "minimum_baseline_completion_fraction", 0.8),
        ("window_validity", "minimum_baseline_completion_fraction", 0.5),
        ("window_validity", "fewer_valid_pairs_than_minimum_decision", "material"),
    ],
)
def test_editing_a_registered_threshold_in_the_protocol_file_is_refused(
    monkeypatch, tmp_path: Path, section: str, field: str, value: object
) -> None:
    tampered = load_protocol_v2()
    tampered[section][field] = value
    path = tmp_path / "benchmark_protocol.v2.json"
    path.write_text(json.dumps(tampered), "utf-8")
    monkeypatch.setattr(harness, "PROTOCOL_V2_PATH", path)
    run = _run_v2([0.5] * 16)
    run["benchmark_protocol_digest"] = protocol_v2_digest(tampered)

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert "posthoc_exclusion_or_threshold_change" in result["reasons"]


# ---------------------------------------------------------------------------
# Validity rule: baseline arm must complete >= 90% of offered requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("completed", "offered", "valid"),
    [(54, 60, True), (53, 60, False), (60, 60, True), (0, 60, False), (9, 10, True),
     (89, 99, False), (0, 0, False), (61, 60, False)],
)
def test_baseline_completion_boundary_is_exactly_ninety_percent(
    completed: int, offered: int, valid: bool
) -> None:
    assert baseline_window_is_valid(completed, offered) is valid


def test_invalid_baseline_window_is_excluded_recorded_and_the_rest_scored() -> None:
    run = _run_v2([0.5] * 16, baseline_completed={3: 53})

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "material"
    assert result["valid_pair_count"] == 15
    assert 3 not in result["valid_pair_ids"]
    assert len(result["paired_improvements"]) == 15
    assert result["excluded_windows"] == [
        {
            "pair_id": 3,
            "baseline_index": 7,
            "reason": INVALID_WINDOW_REASON,
            "baseline_completed": 53,
            "baseline_offered": 60,
        }
    ]


def test_five_invalid_baseline_windows_leave_too_few_pairs() -> None:
    invalid = {1: 53, 4: 40, 9: 0, 12: 53, 15: 50}
    run = _run_v2([0.5] * 16, baseline_completed=invalid)

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [TOO_FEW_VALID_PAIRS_REASON]
    assert result["valid_pair_count"] == 11
    assert [item["pair_id"] for item in result["excluded_windows"]] == sorted(invalid)
    assert all(
        item["reason"] == INVALID_WINDOW_REASON for item in result["excluded_windows"]
    )
    assert result["point_estimate_fraction"] is None
    assert result["paired_improvements"] == []


def test_arrivals_shed_at_the_concurrency_cap_count_as_offered() -> None:
    run = _run_v2([0.5] * 16, baseline_completed={0: 54})
    assert evaluate_benchmark_v2(deepcopy(run))["decision"] == "material"
    run["windows"][0]["offered_request_count"] = 61  # 54/61 < 90%

    result = evaluate_benchmark_v2(run)

    assert result["valid_pair_count"] == 15
    assert 0 not in result["valid_pair_ids"]
    assert result["excluded_windows"][0]["pair_id"] == 0
    assert result["excluded_windows"][0]["baseline_offered"] == 61


def test_offered_count_below_terminal_count_is_refused() -> None:
    run = _run_v2([0.5] * 16)
    run["windows"][0]["offered_request_count"] = 59

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert "incomplete_outcome_accounting" in result["reasons"]


def test_low_candidate_completion_does_not_invalidate_a_pair() -> None:
    # The validity rule guards the baseline arm only; candidate cancellations
    # stay in the candidate's own throughput and cannot be excluded away.
    result = evaluate_benchmark_v2(
        _run_v2([0.5] * 16, candidate_completed={pair: 10 for pair in range(16)})
    )
    assert result["decision"] == "material"
    assert result["valid_pair_count"] == 16


# ---------------------------------------------------------------------------
# 16 scheduled pairs, a floor of 12 valid, every valid pair scored
# ---------------------------------------------------------------------------


def test_eleven_valid_pairs_are_inconclusive_even_with_a_large_gain() -> None:
    result = decide_calibrated_pairs(
        _pairs([0.5] * 16, invalid={11, 12, 13, 14, 15}), decision_protocol="test"
    )
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [TOO_FEW_VALID_PAIRS_REASON]
    assert result["valid_pair_count"] == 11
    assert len(result["excluded_windows"]) == 5


@pytest.mark.parametrize("valid_count", [12, 13, 14, 15, 16])
def test_twelve_to_sixteen_valid_pairs_are_scored_on_every_valid_pair(
    valid_count: int,
) -> None:
    # Distinct gains so any dropped, duplicated or substituted pair changes
    # the point estimate.
    gains = [0.2 + 0.01 * pair_id for pair_id in range(16)]
    invalid = set(range(0, 2 * (16 - valid_count), 2))
    result = evaluate_benchmark_v2(
        _run_v2(gains, baseline_completed={pair: 53 for pair in invalid})
    )
    valid_ids = [pair for pair in range(16) if pair not in invalid]
    assert result["decision"] == "material"
    assert result["scheduled_pairs"] == 16
    assert result["valid_pair_count"] == valid_count
    assert result["valid_pair_ids"] == valid_ids
    assert result["paired_improvements"] == pytest.approx([gains[pair] for pair in valid_ids])
    assert result["point_estimate_fraction"] == pytest.approx(
        sum(gains[pair] for pair in valid_ids) / valid_count
    )
    assert [item["pair_id"] for item in result["excluded_windows"]] == sorted(invalid)


@pytest.mark.parametrize("invalid_count", range(0, 17))
def test_every_scheduled_pair_is_scored_or_recorded_as_excluded(invalid_count: int) -> None:
    invalid = set(range(16 - invalid_count, 16))
    result = decide_calibrated_pairs(
        _pairs([0.5] * 16, invalid=invalid), decision_protocol="test"
    )
    excluded = [item["pair_id"] for item in result["excluded_windows"]]
    assert result["valid_pair_count"] + len(excluded) == 16
    assert sorted(result["valid_pair_ids"] + excluded) == list(range(16))
    assert excluded == sorted(invalid)
    assert all(
        item["reason"] == INVALID_WINDOW_REASON for item in result["excluded_windows"]
    )
    expected = "material" if 16 - invalid_count >= 12 else "inconclusive"
    assert result["decision"] == expected


def test_evaluator_scores_all_valid_pairs_never_a_favourable_subset() -> None:
    # Fourteen valid pairs: twelve at +20% and two at -60%.  The best twelve
    # would be material; every valid pair scored is not.
    gains = [0.2] * 12 + [-0.6, -0.6, 0.9, 0.9]
    pairs = _pairs(gains, invalid={14, 15})
    result = decide_calibrated_pairs(pairs, decision_protocol="test")
    all_valid = [0.2] * 12 + [-0.6, -0.6]
    assert result["valid_pair_count"] == 14
    assert result["paired_improvements"] == pytest.approx(all_valid)
    assert result["point_estimate_fraction"] == pytest.approx(sum(all_valid) / 14)
    assert result["decision"] == "not_material"
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] == (
        harness._bootstrap_lower_bound(
            result["paired_improvements"],
            resamples=harness.BOOTSTRAP_RESAMPLES,
            seed=harness.BOOTSTRAP_SEED,
            lower_quantile=harness.BOOTSTRAP_LOWER_QUANTILE,
        )
    )
    best_twelve = decide_calibrated_pairs(
        _pairs([0.2] * 12 + [0.5] * 4), decision_protocol="test"
    )
    assert best_twelve["decision"] == "material"
    # Input order cannot change which pairs are scored.
    assert decide_calibrated_pairs(pairs[::-1], decision_protocol="test") == result


@pytest.mark.parametrize(
    "pairs",
    [
        _pairs([0.5] * 12),
        _pairs([0.5] * 6),
        _pairs([0.5] * 17),
        _pairs([0.5] * 16)[1:] + [{**_pairs([0.5])[0], "pair_id": 16}],
        _pairs([0.5] * 15) + [_pairs([0.5])[0]],
    ],
    ids=["12", "6", "17", "ids_1_to_16", "duplicate_id"],
)
def test_a_schedule_other_than_sixteen_pairs_is_refused(pairs: list[dict]) -> None:
    result = decide_calibrated_pairs(pairs, decision_protocol="test")
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [SCHEDULE_NOT_REGISTERED_REASON]
    assert result["paired_improvements"] == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda schedule: schedule["measured_windows"].__delitem__(slice(24, 32)),
        lambda schedule: schedule["measured_windows"].extend(
            [
                {"index": 32, "block": 8, "pair_id": 16, "mode": "baseline"},
                {"index": 33, "block": 8, "pair_id": 16, "mode": "candidate"},
            ]
        ),
        lambda schedule: schedule["measured_windows"][31].update(mode="candidate"),
    ],
    ids=["twelve_pairs", "seventeen_pairs", "unpaired_window"],
)
def test_a_protocol_file_scheduling_other_than_sixteen_pairs_is_refused(
    monkeypatch, tmp_path: Path, mutate
) -> None:
    run = _run_v2([0.5] * 16)
    tampered = load_protocol_v2()
    mutate(tampered["schedule"])
    path = tmp_path / "benchmark_protocol.v2.json"
    path.write_text(json.dumps(tampered), "utf-8")
    monkeypatch.setattr(harness, "PROTOCOL_V2_PATH", path)
    run["benchmark_protocol_digest"] = protocol_v2_digest(tampered)

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert SCHEDULE_NOT_REGISTERED_REASON in result["reasons"]


def test_the_refinement_ruling_is_recorded_and_required() -> None:
    protocol = load_protocol_v2()
    refinements = protocol["amendment"]["refinements"]
    assert refinements == [harness.AMENDMENT_REFINEMENT]
    assert refinements[0]["ruling_date"] == "2026-09-27"
    assert refinements[0]["made_before_any_v2_benchmark_data"] is True
    assert refinements[0]["protocol_version_unchanged"] == protocol["protocol_version"] == 2
    assert "16 paired windows" in refinements[0]["ruling"]
    assert "at least 12 valid" in refinements[0]["ruling"]
    stripped = deepcopy(protocol)
    del stripped["amendment"]["refinements"]
    assert "amendment_ruling_missing" in harness.amended_protocol_violations(stripped)


def test_six_pairs_the_old_floor_can_never_be_material() -> None:
    result = decide_calibrated_pairs(
        _pairs([0.5] * 16, invalid=set(range(6, 16))), decision_protocol="test"
    )
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [TOO_FEW_VALID_PAIRS_REASON]


def test_floor_cannot_be_lowered_by_a_caller() -> None:
    with pytest.raises(ValueError, match="minimum_valid_pairs_below_pre_registered_floor"):
        decide_calibrated_pairs(
            _pairs([0.5] * 6), decision_protocol="test", minimum_valid_pairs=6
        )


def test_missing_window_is_inconclusive_not_a_smaller_sample() -> None:
    run = _run_v2([0.5] * 16)
    run["windows"].pop()
    result = evaluate_benchmark_v2(run)
    assert result["decision"] == "inconclusive"
    assert "duplicate_or_missing_window" in result["reasons"]


# ---------------------------------------------------------------------------
# Calibration phase
# ---------------------------------------------------------------------------


def test_calibration_selects_the_first_qualifying_descending_rate() -> None:
    protocol = load_protocol_v2()
    rate, reasons = validate_calibration(
        _calibration([12, 40, 58]),
        rate_ladder=LADDER,
        minimum_requests_per_trial=60,
        retained_outcomes=protocol["retained_outcomes"],
    )
    assert (rate, reasons) == (1.0, [])


@pytest.mark.parametrize(
    "mutate",
    [
        # rate shopping: a trial after the first qualifying rate
        lambda c: c["trials"].append(
            {**deepcopy(c["trials"][-1]), "ordinal": 3, "offered_arrival_rate_per_second": 0.5}
        ),
        # selecting a rate other than the first qualifying one
        lambda c: c.update(selected_offered_arrival_rate_per_second=2.0),
        # skipping a ladder rung
        lambda c: c["trials"][1].update(offered_arrival_rate_per_second=1.0),
        # a scored calibration
        lambda c: c.update(scored=True),
        # a calibration trial in candidate mode
        lambda c: c.update(mode="candidate"),
        # a different, unregistered ladder
        lambda c: c.update(rate_ladder_per_second=[8.0, *LADDER[1:]]),
        # an undersized trial
        lambda c: c["trials"][0].update(
            request_count=59, outcomes=_outcomes(completed=12, cancelled=47)
        ),
    ],
)
def test_calibration_that_departs_from_the_registered_procedure_is_refused(mutate) -> None:
    run = _run_v2([0.5] * 16)
    mutate(run["calibration"])

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert result["reasons"] == ["calibration_invalid"]


def test_no_qualifying_rate_is_inconclusive_and_scores_nothing() -> None:
    run = _run_v2([0.5] * 16)
    run["calibration"] = _calibration([5, 10, 20, 30, 40, 50, 53])
    run["warmups"] = []
    run["windows"] = []

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [CALIBRATION_FAILED_REASON]
    assert result["calibrated_offered_arrival_rate_per_second"] is None


def test_scored_windows_must_run_at_the_calibrated_rate() -> None:
    run = _run_v2([0.5] * 16)
    run["windows"][5]["offered_arrival_rate_per_second"] = 4.0
    result = evaluate_benchmark_v2(run)
    assert result["decision"] == "inconclusive"
    assert "workload_or_session_changed" in result["reasons"]


# ---------------------------------------------------------------------------
# r33-shaped input never passes
# ---------------------------------------------------------------------------


def _r33_v1_run() -> dict:
    protocol = load_protocol()
    warmups = [
        {
            "mode": mode,
            "scored": False,
            "request_count": 60,
            "outcomes": _outcomes(completed=completed, cancelled=cancelled, rejected_admission=rejected),
            "bindings": _bindings(mode, protocol),
            "invalidations": [],
        }
        for mode, completed, cancelled, rejected in (
            ("baseline", 12, 44, 4),
            ("candidate", 5, 52, 3),
        )
    ]
    windows = [
        {
            **expected,
            "request_count": 60,
            "completed_generated_tokens": tokens,
            "wall_clock_seconds": seconds,
            "outcomes": _outcomes(
                completed=completed,
                cancelled=cancelled,
                rejected_admission=rejected,
                timeout=timeout,
            ),
            "bindings": _bindings(expected["mode"], protocol),
            "invalidations": [],
        }
        for expected, (completed, cancelled, rejected, timeout, tokens, seconds) in zip(
            protocol["schedule"]["measured_windows"], R33_WINDOWS, strict=True
        )
    ]
    return {
        "protocol": "mycelium.a5_benchmark_run_fixture.v1",
        "benchmark_protocol_digest": protocol_digest(protocol),
        "workload_manifest_digest": workload_manifest_digest(),
        "warmups": warmups,
        "windows": windows,
    }


def test_r33_rescored_under_v1_reproduces_the_recorded_inconclusive() -> None:
    result = evaluate_benchmark(_r33_v1_run())
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [
        "paired_confidence_lower_bound_below_material_threshold"
    ]
    assert result["point_estimate_fraction"] == pytest.approx(R33_POINT, abs=1e-12)
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] == pytest.approx(
        R33_LOWER_BOUND, abs=1e-12
    )


def test_r33_v1_evidence_is_not_admissible_as_v2_evidence() -> None:
    result = evaluate_benchmark_v2(_r33_v1_run())
    assert result["decision"] == "inconclusive"
    assert "benchmark_run_shape_invalid" in result["reasons"]


def test_r33_shaped_windows_under_v2_are_all_invalid_and_inconclusive() -> None:
    v1_run = _r33_v1_run()
    run = _run_v2([0.0] * 16)
    # The r33 host at the manifest rate would not calibrate: 12/60 completed.
    run["calibration"] = _calibration([12, 12, 12, 12, 12, 12, 54])
    run["calibration"]["trials"][-1]["outcomes"] = _outcomes(completed=54, cancelled=6)
    run["calibration"]["selected_offered_arrival_rate_per_second"] = 0.0625
    for record in run["warmups"]:
        record["offered_arrival_rate_per_second"] = 0.0625
    # Tile the six r33 pairs onto the 32-window schedule.
    for window in run["windows"]:
        source = v1_run["windows"][window["index"] % 12]
        assert source["mode"] == window["mode"]
        for field in ("completed_generated_tokens", "wall_clock_seconds", "outcomes"):
            window[field] = deepcopy(source[field])
        window["offered_arrival_rate_per_second"] = 0.0625

    result = evaluate_benchmark_v2(run)

    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [TOO_FEW_VALID_PAIRS_REASON]
    assert result["valid_pair_count"] == 0
    assert len(result["excluded_windows"]) == 16


def test_r33_gains_would_stay_inconclusive_even_if_every_window_were_valid() -> None:
    gains = [
        0.9576492856350087,
        -0.4573205253694699,
        1.3581363642815854,
        -0.7693039948106314,
        0.12351752496970225,
        -0.21003389813837736,
    ]
    result = decide_calibrated_pairs(
        [
            {
                "pair_id": pair_id,
                "baseline_index": pair_id * 2,
                "baseline_completed": 60,
                "baseline_offered": 60,
                "baseline_rate": 1.0,
                "candidate_rate": 1.0 + gain,
            }
            for pair_id, gain in enumerate((gains * 3)[:16])
        ],
        decision_protocol="test",
    )
    assert result["decision"] == "inconclusive"
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] < 0.1


# ---------------------------------------------------------------------------
# Gate: calibration runs first, stops at the first qualifying rate, binds the
# calibrated rate everywhere, and a failed calibration scores nothing
# ---------------------------------------------------------------------------


def _drive_gate(monkeypatch, calibration_completed: dict[float, int]) -> tuple[dict, list]:
    calls: list[tuple[str, float]] = []
    installed = {"mode": "baseline"}
    digest = "sha256:" + "c" * 64

    monkeypatch.setattr(benchmark_gate, "_load_replica_documents", lambda paths: [{}])
    monkeypatch.setattr(
        benchmark_gate,
        "public_json",
        lambda base_url, path: {
            "protocol": "mycelium.live_route_status.v1",
            "route_alive": True,
            "replica_track_qualification": True,
            "deployment_id": "deployment-gate-test",
        },
    )
    monkeypatch.setattr(benchmark_gate, "_zero_live_resources", lambda status: True)
    monkeypatch.setattr(benchmark_gate, "_live_route_binding", lambda status: digest)
    monkeypatch.setattr(benchmark_gate, "wait_for", lambda predicate, *, timeout: predicate())

    def install(base_url: str, documents: list) -> None:
        installed["mode"] = "candidate" if documents else "baseline"

    monkeypatch.setattr(benchmark_gate, "install_replica_qualification", install)
    phase = {"calibrating": True}

    def window(base_url: str, rng: random.Random, arrival_rate: float) -> dict:
        mode = installed["mode"]
        calls.append((mode, arrival_rate))
        if phase["calibrating"] and arrival_rate in calibration_completed:
            completed = calibration_completed.pop(arrival_rate)
            if completed >= 54:
                phase["calibrating"] = False
        else:
            phase["calibrating"] = False
            completed = 60
        tokens = 100_000 if mode == "baseline" else 150_000
        return {
            "outcomes": _outcomes(completed=completed, cancelled=60 - completed),
            "completed_generated_tokens": tokens,
            "wall_clock_seconds": 1000.0,
            "request_count": 60,
            "offered_request_count": 60,
        }

    monkeypatch.setattr(benchmark_gate, "_run_isolated_window", window)
    report = benchmark_gate.run_benchmark(
        "http://route.invalid",
        [Path("unused")],
        source_manifest_digest="sha256:" + "d" * 64,
    )
    return report, calls


def test_gate_calibrates_before_scoring_and_binds_the_selected_rate(monkeypatch) -> None:
    report, calls = _drive_gate(monkeypatch, {4.0: 12, 2.0: 55})

    fixture = report["run_fixture"]
    assert fixture["protocol"] == "mycelium.a5_benchmark_run_fixture.v2"
    assert fixture["benchmark_protocol_digest"] == protocol_v2_digest()
    calibration = fixture["calibration"]
    assert [trial["offered_arrival_rate_per_second"] for trial in calibration["trials"]] == [
        4.0,
        2.0,
    ]
    assert calibration["selected_offered_arrival_rate_per_second"] == 2.0
    # two baseline calibration trials, then 2 warmups + 32 windows at 2.0
    assert calls[:2] == [("baseline", 4.0), ("baseline", 2.0)]
    assert len(calls) == 2 + 2 + 32
    assert {rate for _, rate in calls[2:]} == {2.0}
    assert report["observed"]["arrival_rate_per_second"] == 2.0
    assert report["decision"]["decision"] == "material"
    assert report["decision"]["valid_pair_count"] == 16
    assert report["decision"]["calibrated_offered_arrival_rate_per_second"] == 2.0
    assert evaluate_benchmark_v2(fixture) == report["decision"]


def test_gate_with_no_qualifying_rate_runs_no_scored_window(monkeypatch) -> None:
    report, calls = _drive_gate(
        monkeypatch, {rate: 30 for rate in LADDER}
    )

    assert calls == [("baseline", rate) for rate in LADDER]
    fixture = report["run_fixture"]
    assert fixture["warmups"] == []
    assert fixture["windows"] == []
    assert fixture["calibration"]["selected_offered_arrival_rate_per_second"] is None
    assert report["decision"]["decision"] == "inconclusive"
    assert report["decision"]["reasons"] == [CALIBRATION_FAILED_REASON]


def test_window_driver_counts_arrivals_shed_at_the_cap_as_offered(monkeypatch) -> None:
    monkeypatch.setattr(benchmark_gate, "MIN_WINDOW_REQUESTS", 5)
    monkeypatch.setattr(benchmark_gate, "WARMUP_SECONDS", 0)
    monkeypatch.setattr(benchmark_gate, "OFFERED_CONCURRENCY", 1)
    monkeypatch.setattr(
        benchmark_gate._WindowDriver, "_next_arrival_delay", lambda self: 0.001
    )

    result = benchmark_gate._WindowDriver(
        _SlowSuccessfulSession(), random.Random(1)
    ).run(duration_after_measure=2.0)

    assert result["request_count"] >= 5
    assert result["offered_request_count"] > result["request_count"]


def test_gate_refuses_a_protocol_not_scheduling_sixteen_pairs_before_any_window(
    monkeypatch,
) -> None:
    tampered = load_protocol_v2()
    tampered["confidence"]["scheduled_pairs"] = 12
    del tampered["schedule"]["measured_windows"][24:]
    monkeypatch.setattr(benchmark_gate, "PROTOCOL", tampered)
    touched: list[str] = []
    monkeypatch.setattr(
        benchmark_gate, "_load_replica_documents", lambda paths: touched.append("load")
    )
    monkeypatch.setattr(
        benchmark_gate, "public_json", lambda base_url, path: touched.append(path)
    )

    with pytest.raises(benchmark_gate.GateError, match="benchmark_protocol_not_pre_registered"):
        benchmark_gate.run_benchmark(
            "http://route.invalid",
            [Path("unused")],
            source_manifest_digest="sha256:" + "d" * 64,
        )
    assert touched == []
