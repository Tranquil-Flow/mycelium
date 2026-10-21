# SPDX-License-Identifier: AGPL-3.0-only
"""A10 benchmark protocol v2: the owner-approved amendment applied identically.

Owner ruling 2026-09-27 (pre-registered amendment after r33 underpowered
result): the A5 calibrated-offered-load rule, the baseline 90% completion
validity rule, 16 scheduled pairs with a floor of 12 valid pairs, scoring of
every valid pair with no selection (owner refinement of the same date, made
before any v2 data existed) and the unchanged +10% paired bootstrap
lower-bound threshold apply to A10 exactly as to A5.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from mycelium_router import a10_runtime_batch as rrb
from tests.a5_acceptance.materiality_harness import (
    CALIBRATION_FAILED_REASON,
    INVALID_WINDOW_REASON,
    SCHEDULE_NOT_REGISTERED_REASON,
    TOO_FEW_VALID_PAIRS_REASON,
    amended_protocol_violations,
    decide_calibrated_pairs,
    load_protocol_v2 as load_a5_protocol_v2,
)

from . import benchmark_amendment as amendment
from .benchmark_amendment import evaluate_throughput, load_protocol, protocol_digest


ROOT = Path(__file__).parents[2]
INVENTORY = Path(__file__).with_name("scenarios.v1.json")
RATE = 2.0
LADDER = [4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625]
DIGEST = "sha256:" + "a" * 64


def _outcomes(completed: int, total: int) -> dict[str, int]:
    return {
        "completed": completed,
        "failed": 0,
        "cancelled": total - completed,
        "overflowed": 0,
        "slow_consumer": 0,
    }


def _calibration(completions: list[int]) -> dict:
    trials = [
        {
            "ordinal": ordinal,
            "offered_arrival_rate_per_second": LADDER[ordinal],
            "request_count": 66,
            "offered_request_count": 66,
            "outcomes": _outcomes(completed, 66),
        }
        for ordinal, completed in enumerate(completions)
    ]
    selected = next(
        (
            trial["offered_arrival_rate_per_second"]
            for trial in trials
            if trial["outcomes"]["completed"] * 10 >= 66 * 9
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


def _run(
    improvements: list[float],
    *,
    baseline_offered: dict[int, int] | None = None,
    candidate_completed: dict[int, int] | None = None,
) -> dict:
    protocol = load_protocol()
    assert len(improvements) == 16
    baseline_offered = baseline_offered or {}
    candidate_completed = candidate_completed or {}
    windows = []
    for expected in protocol["measured_windows"]:
        pair, mode = expected["pair_id"], expected["mode"]
        rate = 100.0 if mode == "baseline" else 100.0 * (1.0 + improvements[pair])
        offered = baseline_offered.get(pair, 66) if mode == "baseline" else 66
        completed = 60 if mode == "baseline" else candidate_completed.get(pair, 60)
        windows.append(
            {
                **expected,
                "offered_arrival_rate_per_second": RATE,
                "offered_request_count": offered,
                "request_count": 66,
                "outcomes": _outcomes(completed, 66),
                "completed_generated_tokens": round(rate * 1000),
                "wall_clock_seconds": 1000.0,
                "invalidations": [],
            }
        )
        if offered < 66:
            raise AssertionError("fixture offered count must cover terminals")
    return {
        "protocol": "mycelium.a10_benchmark_throughput_run.v2",
        "benchmark_protocol_digest": protocol_digest(protocol),
        "calibration": _calibration([20, 62]),
        "windows": windows,
    }


def test_protocol_records_the_ruling_and_supersedes_only_the_pair_count() -> None:
    protocol = load_protocol()
    inventory = json.loads(INVENTORY.read_text("utf-8"))
    assert protocol["protocol"] == "mycelium.a10_benchmark_acceptance.v2"
    assert protocol["protocol_version"] == 2
    ruling = protocol["amendment"]
    assert ruling["ruling_date"] == "2026-09-27"
    assert ruling["ruling"] == (
        "owner ruling: pre-registered amendment after r33 underpowered result"
    )
    assert ruling["pre_registered"] is True
    assert ruling["applies_identically_to"] == ["A5", "A10"]
    superseded = ruling["supersedes"]
    assert superseded["inventory_digest"] == rrb.ACCEPTANCE_INVENTORY_DIGEST
    v1 = inventory["benchmark_requirements"]
    assert superseded["superseded_fields"] == {
        "paired_window_order": v1["paired_window_order"],
        "measured_windows": v1["measured_windows"],
    }
    v2 = protocol["benchmark_requirements"]
    assert set(v2) == set(v1)
    for field in set(v1) - {"paired_window_order", "measured_windows"}:
        assert v2[field] == v1[field], field
    assert v2["throughput_improvement_percent"] == 10
    assert v2["throughput_lower_bound_percent"] == 10
    assert v2["paired_bootstrap_confidence_percent"] == 95
    assert amended_protocol_violations(protocol) == []


def test_amendment_is_identical_to_the_a5_amendment() -> None:
    a10 = load_protocol()
    a5 = load_a5_protocol_v2()
    for field in ("metric", "confidence", "calibration", "window_validity"):
        assert a10[field] == a5[field], field
    for field in (
        "ruling_date",
        "ruling",
        "pre_registered",
        "refinements",
        "applies_identically_to",
    ):
        assert a10["amendment"][field] == a5["amendment"][field], field


def test_schedule_is_sixteen_alternating_pairs() -> None:
    protocol = load_protocol()
    order = protocol["benchmark_requirements"]["paired_window_order"]
    assert order == ["baseline_candidate", "candidate_baseline"] * 8
    windows = protocol["measured_windows"]
    assert protocol["benchmark_requirements"]["measured_windows"] == len(windows) == 32
    assert [item["index"] for item in windows] == list(range(32))
    for pair_id, pair_order in enumerate(order):
        modes = [item["mode"] for item in windows if item["pair_id"] == pair_id]
        assert "_".join(modes) == pair_order


def test_runtime_constants_mirror_the_v2_protocol() -> None:
    protocol = load_protocol()
    assert (ROOT / rrb.BENCHMARK_PROTOCOL).resolve() == amendment.PROTOCOL_PATH.resolve()
    assert rrb.SCHEDULED_BENCHMARK_PAIRS == protocol["confidence"]["scheduled_pairs"] == 16
    assert (
        rrb.MINIMUM_VALID_BENCHMARK_PAIRS
        == protocol["confidence"]["minimum_valid_pairs"]
        == 12
    )


@pytest.mark.parametrize(
    ("baseline", "candidate", "valid", "code"),
    [
        (16, 16, 12, None),
        (16, 16, 13, None),
        (16, 16, 16, None),
        # scheduled windows are an exact count, not a floor
        (12, 12, 12, "invalid_benchmark_window_count"),
        (15, 15, 12, "invalid_benchmark_window_count"),
        (17, 17, 12, "invalid_benchmark_window_count"),
        (3, 3, 12, "invalid_benchmark_window_count"),
        (16, 15, 12, "invalid_benchmark_window_count"),
        # valid pairs have a floor of twelve and cannot exceed the schedule
        (16, 16, 11, "invalid_benchmark_valid_pair_count"),
        (16, 16, 17, "invalid_benchmark_valid_pair_count"),
        (16, 16, True, "invalid_benchmark_valid_pair_count"),
    ],
)
def test_qualification_request_schedules_sixteen_pairs_with_a_floor_of_twelve_valid(
    baseline: int, candidate: int, valid: int, code: str | None
) -> None:
    values = {
        "owner_private_root": "private:root",
        "benchmark_manifest_digest": DIGEST,
        "acceptance_inventory_digest": DIGEST,
        "baseline_windows": baseline,
        "candidate_windows": candidate,
        "valid_pairs": valid,
    }
    if code is None:
        request = rrb.QualificationRequest(**values)
        assert request.baseline_windows == request.candidate_windows == 16
        assert request.valid_pairs == valid
        return
    with pytest.raises(rrb.A10ContractError) as raised:
        rrb.QualificationRequest(**values)
    assert raised.value.code == code


def test_sixteen_valid_pairs_at_twelve_percent_are_material() -> None:
    result = evaluate_throughput(_run([0.12] * 16))
    assert result["decision"] == "material"
    assert result["scope"] == "throughput_materiality_only"
    assert result["scheduled_pairs"] == 16
    assert result["valid_pair_count"] == 16
    assert result["calibrated_offered_arrival_rate_per_second"] == RATE
    assert result["qualification_claim"] is False
    assert result["promotion_authorized"] is False


def test_threshold_is_unchanged() -> None:
    assert evaluate_throughput(_run([0.10] * 16))["decision"] == "material"
    assert evaluate_throughput(_run([0.099] * 16))["decision"] == "not_material"
    spread = evaluate_throughput(_run([0.0] * 8 + [0.2] * 8))
    assert spread["decision"] == "inconclusive"
    assert spread["reasons"] == [
        "paired_confidence_lower_bound_below_material_threshold"
    ]


def test_decision_math_matches_the_shared_a5_core() -> None:
    gains = [
        0.3, 0.1, 0.2, 0.05, 0.4, 0.15, 0.12, 0.22,
        0.18, 0.11, 0.25, 0.14, 0.08, 0.31, 0.19, 0.16,
    ]
    a10 = evaluate_throughput(_run(gains))
    core = decide_calibrated_pairs(
        [
            {
                "pair_id": pair_id,
                "baseline_index": 0,
                "baseline_completed": 60,
                "baseline_offered": 66,
                "baseline_rate": 100.0,
                "candidate_rate": round(100.0 * (1.0 + gain) * 1000) / 1000.0,
            }
            for pair_id, gain in enumerate(gains)
        ],
        decision_protocol="core",
    )
    assert a10["decision"] == core["decision"]
    assert a10["paired_improvements"] == pytest.approx(core["paired_improvements"])
    assert a10["paired_95_percent_bootstrap_lower_bound_fraction"] == pytest.approx(
        core["paired_95_percent_bootstrap_lower_bound_fraction"]
    )


def test_invalid_baseline_window_is_excluded_recorded_and_the_rest_scored() -> None:
    # 60 completed of 67 offered is 89.6%: under the 90% baseline rule.
    run = _run([0.5] * 16, baseline_offered={4: 67})

    result = evaluate_throughput(run)

    assert result["decision"] == "material"
    assert result["valid_pair_count"] == 15
    assert 4 not in result["valid_pair_ids"]
    assert result["excluded_windows"] == [
        {
            "pair_id": 4,
            "baseline_index": 8,
            "reason": INVALID_WINDOW_REASON,
            "baseline_completed": 60,
            "baseline_offered": 67,
        }
    ]


def test_v1_completed_request_minimum_still_applies_to_every_window() -> None:
    result = evaluate_throughput(_run([0.5] * 16, candidate_completed={2: 59}))
    assert result["decision"] == "inconclusive"
    assert "fewer_than_minimum_completed_requests" in result["reasons"]


def test_no_qualifying_calibration_rate_is_inconclusive() -> None:
    run = _run([0.5] * 16)
    run["calibration"] = _calibration([10, 20, 30, 40, 50, 55, 59])
    run["windows"] = []
    result = evaluate_throughput(run)
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [CALIBRATION_FAILED_REASON]


def test_rate_shopping_after_the_first_qualifying_rate_is_refused() -> None:
    run = _run([0.5] * 16)
    extra = deepcopy(run["calibration"]["trials"][-1])
    extra.update(ordinal=2, offered_arrival_rate_per_second=1.0)
    run["calibration"]["trials"].append(extra)
    result = evaluate_throughput(run)
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == ["calibration_invalid"]


def test_scored_windows_must_run_at_the_calibrated_rate() -> None:
    run = _run([0.5] * 16)
    run["windows"][7]["offered_arrival_rate_per_second"] = 4.0
    result = evaluate_throughput(run)
    assert result["decision"] == "inconclusive"
    assert "workload_or_session_changed" in result["reasons"]


def test_r33_shaped_gains_never_pass() -> None:
    r33 = [
        0.9576492856350087,
        -0.4573205253694699,
        1.3581363642815854,
        -0.7693039948106314,
        0.12351752496970225,
        -0.21003389813837736,
    ]
    result = evaluate_throughput(_run((r33 * 3)[:16]))
    assert result["point_estimate_fraction"] > 0.1
    assert result["decision"] == "inconclusive"
    assert result["paired_95_percent_bootstrap_lower_bound_fraction"] < 0.1


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("metric", "material_threshold_fraction", 0.05),
        ("benchmark_requirements", "throughput_lower_bound_percent", 5),
        ("confidence", "minimum_valid_pairs", 3),
        ("confidence", "scheduled_pairs", 12),
        ("confidence", "pair_selection", "best_12_of_16_valid_pairs"),
        ("window_validity", "minimum_baseline_completion_fraction", 0.5),
    ],
)
def test_editing_a_registered_threshold_is_refused(
    monkeypatch, tmp_path: Path, section: str, field: str, value: object
) -> None:
    tampered = load_protocol()
    tampered[section][field] = value
    path = tmp_path / "benchmark_protocol.v2.json"
    path.write_text(json.dumps(tampered), "utf-8")
    monkeypatch.setattr(amendment, "PROTOCOL_PATH", path)
    run = _run([0.5] * 16)
    run["benchmark_protocol_digest"] = protocol_digest(tampered)

    result = evaluate_throughput(run)

    assert result["decision"] == "inconclusive"
    assert "posthoc_exclusion_or_threshold_change" in result["reasons"]


def test_five_invalid_baseline_windows_leave_too_few_pairs() -> None:
    invalid = {0, 3, 8, 11, 15}
    result = evaluate_throughput(_run([0.5] * 16, baseline_offered={p: 67 for p in invalid}))
    assert result["decision"] == "inconclusive"
    assert result["reasons"] == [TOO_FEW_VALID_PAIRS_REASON]
    assert result["valid_pair_count"] == 11
    assert [item["pair_id"] for item in result["excluded_windows"]] == sorted(invalid)
    assert result["paired_improvements"] == []


@pytest.mark.parametrize("valid_count", [12, 13, 14, 15, 16])
def test_twelve_to_sixteen_valid_pairs_are_scored_on_every_valid_pair(
    valid_count: int,
) -> None:
    gains = [0.2 + 0.01 * pair_id for pair_id in range(16)]
    invalid = set(range(1, 1 + 3 * (16 - valid_count), 3))
    result = evaluate_throughput(_run(gains, baseline_offered={p: 67 for p in invalid}))
    valid_ids = [pair for pair in range(16) if pair not in invalid]
    assert result["decision"] == "material"
    assert result["valid_pair_count"] == valid_count
    assert result["valid_pair_ids"] == valid_ids
    assert result["valid_pair_count"] + len(result["excluded_windows"]) == 16
    assert [item["pair_id"] for item in result["excluded_windows"]] == sorted(invalid)
    assert result["point_estimate_fraction"] == pytest.approx(
        sum(gains[pair] for pair in valid_ids) / valid_count, abs=1e-6
    )


def test_every_valid_pair_is_scored_never_a_favourable_subset() -> None:
    # Twelve pairs at +20% and two at -60% are valid; the best twelve would be
    # material, every valid pair scored is not.
    gains = [0.2] * 12 + [-0.6, -0.6, 0.9, 0.9]
    result = evaluate_throughput(_run(gains, baseline_offered={14: 67, 15: 67}))
    assert result["valid_pair_count"] == 14
    assert result["paired_improvements"] == pytest.approx([0.2] * 12 + [-0.6, -0.6])
    assert result["decision"] == "not_material"


def test_a_protocol_file_scheduling_other_than_sixteen_pairs_is_refused(
    monkeypatch, tmp_path: Path
) -> None:
    run = _run([0.5] * 16)
    tampered = load_protocol()
    del tampered["measured_windows"][24:]
    path = tmp_path / "benchmark_protocol.v2.json"
    path.write_text(json.dumps(tampered), "utf-8")
    monkeypatch.setattr(amendment, "PROTOCOL_PATH", path)
    run["benchmark_protocol_digest"] = protocol_digest(tampered)

    result = evaluate_throughput(run)

    assert result["decision"] == "inconclusive"
    assert SCHEDULE_NOT_REGISTERED_REASON in result["reasons"]
