# SPDX-License-Identifier: AGPL-3.0-only
"""mycelium.replica_benchmark.v2: the closed record of a protocol v2 A5 decision.

v2 carries the pre-registered calibrated-load decision (16 scheduled pairs, at
least 12 valid pairs, every valid pair scored, excluded windows with reasons,
the calibration record, the unchanged +10% threshold).  The v1 contract keeps
its exact six-pair shape for historical r33 evidence and is not relaxed.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

import mycelium_replica_contracts as contracts
from mycelium_replica_contracts import (
    REPLICA_BENCHMARK_PROTOCOL,
    REPLICA_BENCHMARK_V2_PROTOCOL,
    compatibility_fixtures,
    validate_replica_benchmark,
    validate_replica_benchmark_v2,
)
from tests.a5_acceptance.materiality_harness import (
    INVALID_WINDOW_REASON,
    MATERIAL_THRESHOLD_FRACTION,
    MINIMUM_VALID_PAIRS,
    PAIR_SELECTION,
    PROTOCOL_V2,
    SCHEDULED_PAIRS,
    decide_calibrated_pairs,
    load_protocol_v2,
)

ROOT = Path(__file__).resolve().parents[2]
V1_FIXTURE = ROOT / "contracts" / "compatibility-fixtures" / "replica-benchmark-v1.json"
V2_FIXTURE = ROOT / "contracts" / "compatibility-fixtures" / "replica-benchmark-v2.json"
CODE = "invalid_replica_benchmark_v2"


def _payload() -> dict:
    return copy.deepcopy(compatibility_fixtures()["replica-benchmark-v2.json"])


def _rejects(payload: dict) -> None:
    with pytest.raises(ValueError, match=CODE):
        validate_replica_benchmark_v2(payload)


def _pairs(valid_count: int, improvement: float = 0.2) -> list[dict]:
    pairs = []
    for pair_id in range(SCHEDULED_PAIRS):
        valid = pair_id < valid_count
        pairs.append(
            {
                "pair_id": pair_id,
                "baseline_index": 2 * pair_id + pair_id % 2,
                "baseline_completed": 60 if valid else 40,
                "baseline_offered": 60,
                "baseline_rate": 10.0,
                "candidate_rate": 10.0 * (1.0 + improvement + 0.001 * pair_id),
            }
        )
    return pairs


def _from_decision(decision: dict) -> dict:
    """Carry a harness v2 decision into the contract, field for field."""
    payload = _payload()
    for field in (
        "decision",
        "reasons",
        "point_estimate_fraction",
        "paired_95_percent_bootstrap_lower_bound_fraction",
        "paired_improvements",
        "valid_pair_ids",
        "excluded_windows",
        "scheduled_pairs",
        "minimum_valid_pairs",
        "pair_selection",
        "material_threshold_fraction",
    ):
        payload[field] = copy.deepcopy(decision[field])
    return payload


# ---------- pinned to the registered protocol ----------


def test_contract_constants_match_the_registered_v2_protocol() -> None:
    protocol = load_protocol_v2()
    assert contracts.BENCHMARK_ACCEPTANCE_V2_PROTOCOL == PROTOCOL_V2 == protocol["protocol"]
    assert contracts.BENCHMARK_V2_SCHEDULED_PAIRS == SCHEDULED_PAIRS
    assert contracts.BENCHMARK_V2_SCHEDULED_PAIRS == protocol["confidence"]["scheduled_pairs"]
    assert contracts.BENCHMARK_V2_MINIMUM_VALID_PAIRS == MINIMUM_VALID_PAIRS
    assert (
        contracts.BENCHMARK_V2_MINIMUM_VALID_PAIRS
        == protocol["confidence"]["minimum_valid_pairs"]
    )
    assert contracts.BENCHMARK_V2_PAIR_SELECTION == PAIR_SELECTION
    assert contracts.BENCHMARK_V2_PAIR_SELECTION == protocol["confidence"]["pair_selection"]
    assert contracts.BENCHMARK_V2_MATERIAL_THRESHOLD_FRACTION == MATERIAL_THRESHOLD_FRACTION
    assert (
        contracts.BENCHMARK_V2_MATERIAL_THRESHOLD_FRACTION
        == protocol["metric"]["material_threshold_fraction"]
    )
    assert (
        list(contracts.BENCHMARK_V2_RATE_LADDER_PER_SECOND)
        == protocol["calibration"]["rate_ladder_per_second"]
    )
    assert (
        contracts.BENCHMARK_V2_MINIMUM_REQUESTS_PER_TRIAL
        == protocol["calibration"]["minimum_requests_per_trial"]
    )
    assert contracts.BENCHMARK_V2_INVALID_WINDOW_REASON == INVALID_WINDOW_REASON
    baseline_indexes = {
        window["pair_id"]: window["index"]
        for window in protocol["schedule"]["measured_windows"]
        if window["mode"] == "baseline"
    }
    assert baseline_indexes == {
        pair: contracts._v2_scheduled_baseline_index(pair) for pair in range(SCHEDULED_PAIRS)
    }


# ---------- v1 is untouched ----------


def test_v1_fixture_bytes_and_six_pair_rule_are_unchanged() -> None:
    v1 = json.loads(V1_FIXTURE.read_text("utf-8"))
    assert v1["protocol"] == REPLICA_BENCHMARK_PROTOCOL
    assert V1_FIXTURE.read_bytes() == (
        json.dumps(
            compatibility_fixtures()["replica-benchmark-v1.json"],
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    validate_replica_benchmark(v1)
    for count in (5, 7, 12, 16):
        widened = dict(v1, paired_improvements=[0.12] * count)
        with pytest.raises(ValueError, match="invalid_replica_benchmark"):
            validate_replica_benchmark(widened)


def test_v1_and_v2_do_not_accept_each_other() -> None:
    v1 = compatibility_fixtures()["replica-benchmark-v1.json"]
    v2 = _payload()
    _rejects(copy.deepcopy(v1))
    with pytest.raises(ValueError, match="invalid_replica_benchmark"):
        validate_replica_benchmark(v2)
    relabelled = dict(v1, protocol=REPLICA_BENCHMARK_V2_PROTOCOL)
    _rejects(relabelled)


# ---------- positive shapes ----------


def test_fixture_is_valid_and_matches_the_committed_file() -> None:
    payload = validate_replica_benchmark_v2(_payload())
    assert payload["protocol"] == REPLICA_BENCHMARK_V2_PROTOCOL
    assert json.loads(V2_FIXTURE.read_text("utf-8")) == payload


def test_all_scheduled_pairs_valid_and_material_is_accepted() -> None:
    decision = decide_calibrated_pairs(_pairs(16), decision_protocol="x")
    assert decision["decision"] == "material"
    validate_replica_benchmark_v2(_from_decision(decision))


def test_twelve_valid_pairs_with_four_excluded_is_accepted() -> None:
    decision = decide_calibrated_pairs(_pairs(12), decision_protocol="x")
    assert decision["decision"] == "material"
    assert len(decision["excluded_windows"]) == 4
    validate_replica_benchmark_v2(_from_decision(decision))


def test_too_few_valid_pairs_is_carried_as_inconclusive() -> None:
    decision = decide_calibrated_pairs(_pairs(11), decision_protocol="x")
    assert decision["decision"] == "inconclusive"
    validate_replica_benchmark_v2(_from_decision(decision))


def test_not_material_and_low_bound_inconclusive_decisions_are_accepted() -> None:
    not_material = decide_calibrated_pairs(_pairs(16, 0.01), decision_protocol="x")
    assert not_material["decision"] == "not_material"
    validate_replica_benchmark_v2(_from_decision(not_material))
    pairs = _pairs(16, 0.12)
    for pair in pairs[:4]:
        pair["candidate_rate"] = 5.0
    for pair in pairs[4:]:
        pair["candidate_rate"] = 14.0
    wide = decide_calibrated_pairs(pairs, decision_protocol="x")
    assert wide["decision"] == "inconclusive"
    assert wide["paired_improvements"]
    validate_replica_benchmark_v2(_from_decision(wide))


def test_failed_calibration_is_carried_as_unscored_inconclusive() -> None:
    payload = _payload()
    payload["calibration"]["trials"] = [
        {
            "ordinal": index,
            "offered_arrival_rate_per_second": rate,
            "request_count": 60,
            "offered_request_count": 60,
            "completed": 10,
        }
        for index, rate in enumerate(contracts.BENCHMARK_V2_RATE_LADDER_PER_SECOND)
    ]
    payload["calibration"]["selected_offered_arrival_rate_per_second"] = None
    payload.update(
        valid_pair_ids=[],
        excluded_windows=[],
        paired_improvements=[],
        point_estimate_fraction=None,
        paired_95_percent_bootstrap_lower_bound_fraction=None,
        decision="inconclusive",
        reasons=["calibration_no_rate_meets_baseline_completion"],
    )
    validate_replica_benchmark_v2(payload)


# ---------- negative controls ----------


def test_rejects_material_with_fewer_than_twelve_valid_pairs() -> None:
    decision = decide_calibrated_pairs(_pairs(16), decision_protocol="x")
    payload = _from_decision(decision)
    # Drop four scored pairs into exclusions but keep the material claim.
    payload["valid_pair_ids"] = list(range(11))
    payload["paired_improvements"] = payload["paired_improvements"][:11]
    payload["point_estimate_fraction"] = sum(payload["paired_improvements"]) / 11
    payload["excluded_windows"] = [
        {
            "pair_id": pair,
            "baseline_index": 2 * pair + pair % 2,
            "reason": INVALID_WINDOW_REASON,
            "baseline_completed": 40,
            "baseline_offered": 60,
        }
        for pair in range(11, 16)
    ]
    _rejects(payload)


def test_rejects_too_few_pairs_relabelled_material() -> None:
    payload = _from_decision(decide_calibrated_pairs(_pairs(11), decision_protocol="x"))
    payload["decision"] = "material"
    _rejects(payload)


def test_rejects_selected_subset_of_valid_pairs() -> None:
    """Scoring only some valid pairs is selection; every valid pair is scored."""
    payload = _from_decision(decide_calibrated_pairs(_pairs(16), decision_protocol="x"))
    payload["paired_improvements"] = payload["paired_improvements"][:14]
    payload["point_estimate_fraction"] = sum(payload["paired_improvements"]) / 14
    _rejects(payload)


def test_rejects_unaccounted_scheduled_pair() -> None:
    payload = _payload()
    payload["excluded_windows"] = payload["excluded_windows"][:1]
    _rejects(payload)


def test_rejects_pair_both_valid_and_excluded() -> None:
    payload = _payload()
    payload["valid_pair_ids"] = sorted(payload["valid_pair_ids"] + [3])
    _rejects(payload)


def test_rejects_excluded_window_that_met_the_completion_rule() -> None:
    payload = _payload()
    payload["excluded_windows"][0]["baseline_completed"] = 54  # 54/60 == 0.9
    _rejects(payload)


def test_rejects_excluded_window_without_the_registered_reason() -> None:
    payload = _payload()
    payload["excluded_windows"][0]["reason"] = "operator_discretion"
    _rejects(payload)


def test_rejects_excluded_window_at_an_unscheduled_index() -> None:
    payload = _payload()
    payload["excluded_windows"][0]["baseline_index"] += 1
    _rejects(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scheduled_pairs", 12),
        ("minimum_valid_pairs", 6),
        ("material_threshold_fraction", 0.05),
        ("pair_selection", "best_pairs"),
        ("benchmark_protocol", "mycelium.a5_benchmark_acceptance.v1"),
        ("qualification_claim", True),
        ("promotion_authorized", True),
        ("decision", "yes"),
    ],
)
def test_rejects_moved_threshold_or_claim(field: str, value: object) -> None:
    payload = _payload()
    payload[field] = value
    _rejects(payload)


def test_rejects_calibration_rate_shopping() -> None:
    payload = _payload()
    # A trial after the first qualifying rate.
    payload["calibration"]["trials"].append(
        {
            "ordinal": 3,
            "offered_arrival_rate_per_second": 0.5,
            "request_count": 60,
            "offered_request_count": 60,
            "completed": 60,
        }
    )
    _rejects(payload)


def test_rejects_calibration_selecting_a_non_qualifying_rate() -> None:
    payload = _payload()
    payload["calibration"]["selected_offered_arrival_rate_per_second"] = 2.0
    _rejects(payload)


def test_rejects_calibration_with_a_moved_ladder() -> None:
    payload = _payload()
    payload["calibration"]["rate_ladder_per_second"] = [8.0, 4.0, 2.0, 1.0]
    _rejects(payload)


def test_rejects_point_estimate_not_mean_of_scored_pairs() -> None:
    payload = _payload()
    payload["point_estimate_fraction"] += 0.01
    _rejects(payload)


def test_rejects_decision_inconsistent_with_bound() -> None:
    payload = _payload()
    payload["paired_95_percent_bootstrap_lower_bound_fraction"] = 0.05
    _rejects(payload)


def test_rejects_unscored_record_claiming_an_estimate() -> None:
    payload = _from_decision(decide_calibrated_pairs(_pairs(11), decision_protocol="x"))
    payload["point_estimate_fraction"] = 0.2
    _rejects(payload)


def test_rejects_unknown_field_and_private_nested_data() -> None:
    payload = _payload()
    payload["extra"] = 1
    _rejects(payload)
    payload = _payload()
    payload["calibration"]["trials"][0]["prompt"] = "nope"
    _rejects(payload)
