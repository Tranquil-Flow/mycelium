# SPDX-License-Identifier: AGPL-3.0-only
"""Test-only machine checker for the frozen A5 materiality protocol."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import random
from typing import Any, Mapping


PROTOCOL_PATH = Path(__file__).with_name("benchmark_protocol.v1.json")
WORKLOAD_PATH = Path(__file__).with_name("workload_manifest.v1.json")
RUN_PROTOCOL = "mycelium.a5_benchmark_run_fixture.v1"
DECISION_PROTOCOL = "mycelium.a5_benchmark_decision.v1"
WORKLOAD_PROTOCOL = "mycelium.a5_workload_manifest.v1"
RUN_FIELDS = {"protocol", "benchmark_protocol_digest", "workload_manifest_digest", "warmups", "windows"}
WARMUP_FIELDS = {
    "mode",
    "scored",
    "request_count",
    "outcomes",
    "bindings",
    "invalidations",
}
WINDOW_FIELDS = {
    "index",
    "block",
    "pair_id",
    "mode",
    "request_count",
    "completed_generated_tokens",
    "wall_clock_seconds",
    "outcomes",
    "bindings",
    "invalidations",
}


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def load_protocol() -> dict[str, Any]:
    value = json.loads(PROTOCOL_PATH.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def load_workload_manifest() -> dict[str, Any]:
    """Load the frozen A5 workload corpus.

    Returns the canonical manifest dict. The digest of this manifest is one of
    the bindings a benchmark run fixture must carry (per spec §9 identical
    binding fields). If the manifest file is missing or malformed, the harness
    cannot validate any run fixture that references it.
    """
    if not WORKLOAD_PATH.is_file():
        raise FileNotFoundError(f"workload manifest not found: {WORKLOAD_PATH}")
    value = json.loads(WORKLOAD_PATH.read_text("utf-8"))
    assert isinstance(value, dict)
    assert value.get("protocol") == WORKLOAD_PROTOCOL, (
        f"workload manifest protocol mismatch: got {value.get('protocol')!r}, "
        f"expected {WORKLOAD_PROTOCOL!r}"
    )
    return value


def workload_manifest_digest(manifest: Mapping[str, Any] | None = None) -> str:
    """Canonical sha256 digest of the workload manifest, in `sha256:<hex>` form."""
    value = load_workload_manifest() if manifest is None else dict(manifest)
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def protocol_digest(protocol: Mapping[str, Any] | None = None) -> str:
    value = load_protocol() if protocol is None else dict(protocol)
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _decision(
    decision: str,
    reasons: list[str],
    *,
    point_estimate: float | None = None,
    lower_bound: float | None = None,
    paired_improvements: list[float] | None = None,
) -> dict[str, Any]:
    return {
        "protocol": DECISION_PROTOCOL,
        "gate_state": "design_only",
        "decision": decision,
        "reasons": sorted(set(reasons)),
        "point_estimate_fraction": point_estimate,
        "paired_95_percent_bootstrap_lower_bound_fraction": lower_bound,
        "paired_improvements": [] if paired_improvements is None else paired_improvements,
        "qualification_claim": False,
        "promotion_authorized": False,
    }


def _valid_outcomes(
    value: object,
    *,
    request_count: int,
    retained_outcomes: list[str],
) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == set(retained_outcomes)
        and all(type(value[name]) is int and value[name] >= 0 for name in retained_outcomes)
        and sum(value[name] for name in retained_outcomes) == request_count
    )


def _validate_bindings(
    records: list[Mapping[str, Any]],
    *,
    identical_fields: list[str],
) -> list[str]:
    reasons: list[str] = []
    expected_fields = set(identical_fields) | {"qualified_track_policy_digest"}
    first: dict[str, Any] | None = None
    policy_by_mode: dict[str, Any] = {}
    for record in records:
        bindings = record.get("bindings")
        if not isinstance(bindings, Mapping) or set(bindings) != expected_fields:
            reasons.append("binding_mismatch")
            continue
        identical = {name: bindings[name] for name in identical_fields}
        if first is None:
            first = identical
        elif identical != first:
            reasons.append("workload_or_session_changed")
        mode = record.get("mode")
        policy = bindings["qualified_track_policy_digest"]
        prior = policy_by_mode.setdefault(str(mode), policy)
        if policy != prior:
            reasons.append("binding_mismatch")
    return reasons


def _bootstrap_lower_bound(
    values: list[float],
    *,
    resamples: int,
    seed: int,
    lower_quantile: float,
) -> float:
    generator = random.Random(seed)
    count = len(values)
    estimates = []
    for _ in range(resamples):
        sample = [values[generator.randrange(count)] for _ in range(count)]
        estimates.append(sum(sample) / count)
    estimates.sort()
    rank = max(1, math.ceil(lower_quantile * len(estimates)))
    return estimates[rank - 1]


def _at_least(value: float, threshold: float) -> bool:
    return value > threshold or math.isclose(value, threshold, abs_tol=1e-12)


def evaluate_benchmark(document: object) -> dict[str, Any]:
    """Return an honest design-only decision; malformed evidence is inconclusive."""

    protocol = load_protocol()
    reasons: list[str] = []
    if not isinstance(document, Mapping) or set(document) != RUN_FIELDS:
        return _decision("inconclusive", ["benchmark_run_shape_invalid"])
    if document.get("protocol") != RUN_PROTOCOL:
        reasons.append("benchmark_run_protocol_invalid")
    if document.get("benchmark_protocol_digest") != protocol_digest(protocol):
        reasons.append("benchmark_protocol_digest_mismatch")

    warmups = document.get("warmups")
    windows = document.get("windows")
    expected_windows = protocol["schedule"]["measured_windows"]
    retained_outcomes = protocol["retained_outcomes"]
    invalidation_rules = set(protocol["invalidation_rules"])
    binding_records: list[Mapping[str, Any]] = []

    if not isinstance(warmups, list) or len(warmups) != 2:
        reasons.append("warmup_invalid")
    else:
        for index, warmup in enumerate(warmups):
            if not isinstance(warmup, Mapping) or set(warmup) != WARMUP_FIELDS:
                reasons.append("warmup_invalid")
                continue
            binding_records.append(warmup)
            if (
                warmup.get("mode") != protocol["schedule"]["warmup_modes"][index]
                or warmup.get("scored") is not False
                or type(warmup.get("request_count")) is not int
                or warmup["request_count"]
                < protocol["schedule"]["minimum_requests_per_warmup"]
                or not _valid_outcomes(
                    warmup.get("outcomes"),
                    request_count=warmup.get("request_count", -1),
                    retained_outcomes=retained_outcomes,
                )
            ):
                reasons.append("warmup_invalid")
            invalidations = warmup.get("invalidations")
            if not isinstance(invalidations, list) or any(
                type(item) is not str or item not in invalidation_rules
                for item in invalidations
            ):
                reasons.append("invalidation_rule_invalid")
            elif invalidations:
                reasons.extend(invalidations)

    valid_windows: list[Mapping[str, Any]] = []
    if not isinstance(windows, list) or len(windows) != len(expected_windows):
        reasons.append("duplicate_or_missing_window")
    else:
        for expected, window in zip(expected_windows, windows, strict=True):
            if not isinstance(window, Mapping) or set(window) != WINDOW_FIELDS:
                reasons.append("benchmark_window_shape_invalid")
                continue
            binding_records.append(window)
            valid_windows.append(window)
            if any(window.get(name) != expected[name] for name in expected):
                reasons.append("window_order_or_pairing_changed")
            request_count = window.get("request_count")
            if (
                type(request_count) is not int
                or request_count
                < protocol["schedule"]["minimum_requests_per_measured_window"]
            ):
                reasons.append("fewer_than_minimum_requests")
            elif not _valid_outcomes(
                window.get("outcomes"),
                request_count=request_count,
                retained_outcomes=retained_outcomes,
            ):
                reasons.append("incomplete_outcome_accounting")
            tokens = window.get("completed_generated_tokens")
            seconds = window.get("wall_clock_seconds")
            if (
                type(tokens) is not int
                or tokens < 0
                or type(seconds) not in {int, float}
                or isinstance(seconds, bool)
                or not math.isfinite(seconds)
                or seconds <= 0
            ):
                reasons.append("nonfinite_measurement")
            invalidations = window.get("invalidations")
            if not isinstance(invalidations, list) or any(
                type(item) is not str or item not in invalidation_rules
                for item in invalidations
            ):
                reasons.append("invalidation_rule_invalid")
            elif invalidations:
                reasons.extend(invalidations)

    reasons.extend(
        _validate_bindings(
            binding_records,
            identical_fields=protocol["identical_binding_fields"],
        )
    )
    if reasons:
        return _decision("inconclusive", reasons)

    by_pair: dict[int, dict[str, float]] = {}
    for window in valid_windows:
        rate = window["completed_generated_tokens"] / window["wall_clock_seconds"]
        by_pair.setdefault(window["pair_id"], {})[window["mode"]] = rate
    if set(by_pair) != set(range(protocol["confidence"]["sample_size_pairs"])) or any(
        set(pair) != {"baseline", "candidate"} or pair["baseline"] <= 0
        for pair in by_pair.values()
    ):
        return _decision("inconclusive", ["window_order_or_pairing_changed"])

    improvements = [
        by_pair[pair_id]["candidate"] / by_pair[pair_id]["baseline"] - 1.0
        for pair_id in sorted(by_pair)
    ]
    point_estimate = sum(improvements) / len(improvements)
    confidence = protocol["confidence"]
    lower_bound = _bootstrap_lower_bound(
        improvements,
        resamples=confidence["resamples"],
        seed=confidence["seed"],
        lower_quantile=confidence["lower_quantile"],
    )
    threshold = protocol["metric"]["material_threshold_fraction"]
    if not _at_least(point_estimate, threshold):
        return _decision(
            "not_material",
            ["point_estimate_below_material_threshold"],
            point_estimate=point_estimate,
            lower_bound=lower_bound,
            paired_improvements=improvements,
        )
    if not _at_least(lower_bound, threshold):
        return _decision(
            "inconclusive",
            ["paired_confidence_lower_bound_below_material_threshold"],
            point_estimate=point_estimate,
            lower_bound=lower_bound,
            paired_improvements=improvements,
        )
    return _decision(
        "material",
        [],
        point_estimate=point_estimate,
        lower_bound=lower_bound,
        paired_improvements=improvements,
    )


# ---------------------------------------------------------------------------
# Protocol v2: owner ruling 2026-09-27, pre-registered amendment after the r33
# underpowered result.  v1 above stays byte-for-byte the frozen evaluator that
# scored r33 (inconclusive); it is retained only to re-verify v1 evidence.
# Every v2 rule below is enforced in code: the JSON is checked against these
# pre-registered constants, so editing the protocol file cannot move a
# threshold or relax the validity rule without the evaluator refusing.
# ---------------------------------------------------------------------------

PROTOCOL_V2_PATH = Path(__file__).with_name("benchmark_protocol.v2.json")
PROTOCOL_V2 = "mycelium.a5_benchmark_acceptance.v2"
RUN_PROTOCOL_V2 = "mycelium.a5_benchmark_run_fixture.v2"
DECISION_PROTOCOL_V2 = "mycelium.a5_benchmark_decision.v2"
V1_PROTOCOL_DIGEST = (
    "sha256:5b6f3c83f5772d65cab66af9190948f0b2cce78ac0e6243328bae78077b38424"
)
AMENDMENT_RULING_DATE = "2026-09-27"
AMENDMENT_RULING = (
    "owner ruling: pre-registered amendment after r33 underpowered result"
)
# Owner refinement of the same ruling date, made before any v2 benchmark data
# existed, so the protocol stays version 2: schedule 16 pairs, require at
# least 12 valid pairs, and score every valid pair with no selection.
AMENDMENT_REFINEMENT = {
    "ruling_date": "2026-09-27",
    "ruling": (
        "owner ruling: schedule 16 paired windows, require at least 12 valid "
        "pairs, score all valid pairs with no selection, trimming or choice of "
        "a subset; the +10% paired-bootstrap lower-bound threshold and every "
        "bootstrap parameter are unchanged"
    ),
    "made_before_any_v2_benchmark_data": True,
    "protocol_version_unchanged": 2,
    "protocol_version_unchanged_reason": "no_v2_benchmark_result_exists",
}
PAIR_SELECTION = "all_valid_pairs_no_selection_or_trimming"
# Unchanged by the amendment (identical to v1).
MATERIAL_THRESHOLD_FRACTION = 0.1
BOOTSTRAP_CONFIDENCE_LEVEL = 0.95
BOOTSTRAP_LOWER_QUANTILE = 0.025
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 0xA5
# Introduced by the amendment.  Scheduled pairs are an exact count; the
# minimum is a floor on the valid pairs among them.
SCHEDULED_PAIRS = 16
MINIMUM_VALID_PAIRS = 12
MINIMUM_BASELINE_COMPLETION_FRACTION = 0.9
# The completion rule is evaluated in exact integer arithmetic:
# completed / offered >= 9 / 10.
_COMPLETION_NUMERATOR = 9
_COMPLETION_DENOMINATOR = 10
INVALID_WINDOW_REASON = "baseline_completion_below_minimum_fraction"
CALIBRATION_FAILED_REASON = "calibration_no_rate_meets_baseline_completion"
TOO_FEW_VALID_PAIRS_REASON = "fewer_than_minimum_valid_pairs"
SCHEDULE_NOT_REGISTERED_REASON = "scheduled_pairs_not_pre_registered"
WARMUP_FIELDS_V2 = WARMUP_FIELDS | {"offered_arrival_rate_per_second"}
WINDOW_FIELDS_V2 = WINDOW_FIELDS | {
    "offered_request_count",
    "offered_arrival_rate_per_second",
}
RUN_FIELDS_V2 = RUN_FIELDS | {"calibration"}
CALIBRATION_FIELDS = {
    "mode",
    "scored",
    "rate_ladder_per_second",
    "trials",
    "selected_offered_arrival_rate_per_second",
}
CALIBRATION_TRIAL_FIELDS = {
    "ordinal",
    "offered_arrival_rate_per_second",
    "request_count",
    "offered_request_count",
    "outcomes",
}


def load_protocol_v2() -> dict[str, Any]:
    value = json.loads(PROTOCOL_V2_PATH.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def protocol_v2_digest(protocol: Mapping[str, Any] | None = None) -> str:
    value = load_protocol_v2() if protocol is None else dict(protocol)
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def baseline_window_is_valid(completed: object, offered: object) -> bool:
    """True when a baseline arm completed at least 90% of offered requests."""

    return (
        type(completed) is int
        and type(offered) is int
        and offered > 0
        and 0 <= completed <= offered
        and completed * _COMPLETION_DENOMINATOR >= offered * _COMPLETION_NUMERATOR
    )


def amended_protocol_violations(protocol: Mapping[str, Any]) -> list[str]:
    """Reasons a protocol document departs from the pre-registered amendment.

    Shared by the A5 and A10 evaluators: every amended or unchanged threshold is
    pinned here in code, so a protocol file edited after registration is refused
    instead of being trusted.
    """

    reasons: list[str] = []
    amendment = protocol.get("amendment")
    if (
        not isinstance(amendment, Mapping)
        or amendment.get("ruling_date") != AMENDMENT_RULING_DATE
        or amendment.get("ruling") != AMENDMENT_RULING
        or amendment.get("pre_registered") is not True
        or amendment.get("refinements") != [AMENDMENT_REFINEMENT]
    ):
        reasons.append("amendment_ruling_missing")
    confidence = protocol.get("confidence")
    calibration = protocol.get("calibration")
    validity = protocol.get("window_validity")
    if (
        not isinstance(confidence, Mapping)
        or confidence.get("method") != "paired_nonparametric_bootstrap"
        or confidence.get("confidence_level") != BOOTSTRAP_CONFIDENCE_LEVEL
        or confidence.get("lower_quantile") != BOOTSTRAP_LOWER_QUANTILE
        or confidence.get("resamples") != BOOTSTRAP_RESAMPLES
        or confidence.get("seed") != BOOTSTRAP_SEED
        or confidence.get("statistic") != "arithmetic_mean"
        or confidence.get("quantile_method") != "nearest_rank_ceiling"
        or confidence.get("minimum_valid_pairs") != MINIMUM_VALID_PAIRS
        or type(confidence.get("scheduled_pairs")) is not int
        or confidence["scheduled_pairs"] != SCHEDULED_PAIRS
        or confidence.get("pair_selection") != PAIR_SELECTION
        or not isinstance(calibration, Mapping)
        or calibration.get("minimum_baseline_completion_fraction")
        != MINIMUM_BASELINE_COMPLETION_FRACTION
        or not isinstance(validity, Mapping)
        or validity.get("minimum_baseline_completion_fraction")
        != MINIMUM_BASELINE_COMPLETION_FRACTION
        or validity.get("invalid_window_excludes_its_pair") is not True
        or validity.get("fewer_valid_pairs_than_minimum_decision") != "inconclusive"
    ):
        reasons.append("posthoc_exclusion_or_threshold_change")
    schedule = protocol.get("schedule")
    windows = (
        schedule.get("measured_windows")
        if isinstance(schedule, Mapping)
        else protocol.get("measured_windows")
    )
    if not scheduled_windows_are_registered(windows):
        reasons.append(SCHEDULE_NOT_REGISTERED_REASON)
    return reasons


def scheduled_windows_are_registered(windows: object) -> bool:
    """True when a window schedule has exactly the registered 16 pairs.

    Every scheduled pair 0..15 has exactly one baseline and one candidate
    window, and window indexes run 0..31 in order.
    """

    if not isinstance(windows, list) or len(windows) != 2 * SCHEDULED_PAIRS:
        return False
    arms: dict[object, list[object]] = {}
    for index, window in enumerate(windows):
        if not isinstance(window, Mapping) or window.get("index") != index:
            return False
        arms.setdefault(window.get("pair_id"), []).append(window.get("mode"))
    return set(arms) == set(range(SCHEDULED_PAIRS)) and all(
        sorted(modes) == ["baseline", "candidate"] for modes in arms.values()
    )


def validate_calibration(
    calibration: object,
    *,
    rate_ladder: list[float],
    minimum_requests_per_trial: int,
    retained_outcomes: list[str],
) -> tuple[float | None, list[str]]:
    """Check the recorded calibration phase and return the selected rate.

    Trials run baseline-only in descending ladder order and stop at the first
    rate whose trial completes at least 90% of offered requests; that rate is
    the only admissible selection.  No qualifying rate selects ``None``.
    """

    if not isinstance(calibration, Mapping) or set(calibration) != CALIBRATION_FIELDS:
        return None, ["calibration_invalid"]
    if (
        calibration.get("mode") != "baseline"
        or calibration.get("scored") is not False
        or calibration.get("rate_ladder_per_second") != rate_ladder
    ):
        return None, ["calibration_invalid"]
    trials = calibration.get("trials")
    if not isinstance(trials, list) or not trials or len(trials) > len(rate_ladder):
        return None, ["calibration_invalid"]
    qualifying: float | None = None
    for ordinal, trial in enumerate(trials):
        if qualifying is not None:
            # A trial after the first qualifying rate is rate shopping.
            return None, ["calibration_invalid"]
        if (
            not isinstance(trial, Mapping)
            or set(trial) != CALIBRATION_TRIAL_FIELDS
            or trial.get("ordinal") != ordinal
            or trial.get("offered_arrival_rate_per_second") != rate_ladder[ordinal]
            or type(trial.get("request_count")) is not int
            or trial["request_count"] < minimum_requests_per_trial
            or type(trial.get("offered_request_count")) is not int
            or trial["offered_request_count"] < trial["request_count"]
            or not _valid_outcomes(
                trial.get("outcomes"),
                request_count=trial["request_count"],
                retained_outcomes=retained_outcomes,
            )
        ):
            return None, ["calibration_invalid"]
        if baseline_window_is_valid(
            trial["outcomes"]["completed"], trial["offered_request_count"]
        ):
            qualifying = rate_ladder[ordinal]
    selected = calibration.get("selected_offered_arrival_rate_per_second")
    if qualifying is None:
        if len(trials) != len(rate_ladder) or selected is not None:
            return None, ["calibration_invalid"]
        return None, [CALIBRATION_FAILED_REASON]
    if selected != qualifying:
        return None, ["calibration_invalid"]
    return qualifying, []


def decide_calibrated_pairs(
    pairs: list[Mapping[str, Any]],
    *,
    decision_protocol: str,
    minimum_valid_pairs: int = MINIMUM_VALID_PAIRS,
) -> dict[str, Any]:
    """Score paired windows under the amended validity rule.

    Each pair carries ``pair_id``, ``baseline_rate``, ``candidate_rate``,
    ``baseline_completed``, ``baseline_offered`` and ``baseline_index``.
    Exactly the 16 scheduled pairs (ids 0..15) must be supplied; any other
    schedule is refused as inconclusive.  A pair whose baseline arm completed
    under 90% of offered requests is excluded and recorded with its reason,
    never silently dropped; fewer than 12 valid pairs is inconclusive and can
    never be material.  Every valid pair is scored: no selection, trimming or
    choice of a subset.  The +10% point and paired bootstrap lower-bound
    thresholds are the v1 values, unchanged.
    """

    if minimum_valid_pairs < MINIMUM_VALID_PAIRS:
        raise ValueError("minimum_valid_pairs_below_pre_registered_floor")
    excluded: list[dict[str, Any]] = []
    valid: list[Mapping[str, Any]] = []
    for pair in sorted(pairs, key=lambda item: item["pair_id"]):
        if baseline_window_is_valid(pair["baseline_completed"], pair["baseline_offered"]):
            valid.append(pair)
        else:
            excluded.append(
                {
                    "pair_id": pair["pair_id"],
                    "baseline_index": pair["baseline_index"],
                    "reason": INVALID_WINDOW_REASON,
                    "baseline_completed": pair["baseline_completed"],
                    "baseline_offered": pair["baseline_offered"],
                }
            )

    def result(
        decision: str,
        reasons: list[str],
        *,
        point_estimate: float | None = None,
        lower_bound: float | None = None,
        improvements: list[float] | None = None,
    ) -> dict[str, Any]:
        return {
            "protocol": decision_protocol,
            "gate_state": "design_only",
            "decision": decision,
            "reasons": sorted(set(reasons)),
            "point_estimate_fraction": point_estimate,
            "paired_95_percent_bootstrap_lower_bound_fraction": lower_bound,
            "paired_improvements": [] if improvements is None else improvements,
            "scheduled_pairs": SCHEDULED_PAIRS,
            "valid_pair_count": len(valid),
            "valid_pair_ids": [pair["pair_id"] for pair in valid],
            "minimum_valid_pairs": minimum_valid_pairs,
            "pair_selection": PAIR_SELECTION,
            "excluded_windows": excluded,
            "material_threshold_fraction": MATERIAL_THRESHOLD_FRACTION,
            "qualification_claim": False,
            "promotion_authorized": False,
        }

    if [pair["pair_id"] for pair in sorted(pairs, key=lambda item: item["pair_id"])] != list(
        range(SCHEDULED_PAIRS)
    ):
        return result("inconclusive", [SCHEDULE_NOT_REGISTERED_REASON])
    # Every scheduled pair is accounted for: valid and scored, or excluded and
    # recorded with its reason.
    if len(valid) + len(excluded) != SCHEDULED_PAIRS:
        raise RuntimeError("scheduled_pair_accounting_broken")
    if len(valid) < minimum_valid_pairs:
        return result("inconclusive", [TOO_FEW_VALID_PAIRS_REASON])
    if any(
        type(pair["baseline_rate"]) is not float
        or type(pair["candidate_rate"]) is not float
        or not math.isfinite(pair["baseline_rate"])
        or not math.isfinite(pair["candidate_rate"])
        or pair["baseline_rate"] <= 0
        or pair["candidate_rate"] < 0
        for pair in valid
    ):
        return result("inconclusive", ["nonfinite_measurement"])
    improvements = [
        pair["candidate_rate"] / pair["baseline_rate"] - 1.0 for pair in valid
    ]
    point_estimate = sum(improvements) / len(improvements)
    lower_bound = _bootstrap_lower_bound(
        improvements,
        resamples=BOOTSTRAP_RESAMPLES,
        seed=BOOTSTRAP_SEED,
        lower_quantile=BOOTSTRAP_LOWER_QUANTILE,
    )
    if not _at_least(point_estimate, MATERIAL_THRESHOLD_FRACTION):
        return result(
            "not_material",
            ["point_estimate_below_material_threshold"],
            point_estimate=point_estimate,
            lower_bound=lower_bound,
            improvements=improvements,
        )
    if not _at_least(lower_bound, MATERIAL_THRESHOLD_FRACTION):
        return result(
            "inconclusive",
            ["paired_confidence_lower_bound_below_material_threshold"],
            point_estimate=point_estimate,
            lower_bound=lower_bound,
            improvements=improvements,
        )
    return result(
        "material",
        [],
        point_estimate=point_estimate,
        lower_bound=lower_bound,
        improvements=improvements,
    )


def evaluate_benchmark_v2(document: object) -> dict[str, Any]:
    """Protocol v2 decision; malformed evidence or too few valid pairs is inconclusive."""

    protocol = load_protocol_v2()

    def refuse(reasons: list[str], rate: float | None = None) -> dict[str, Any]:
        refused = decide_calibrated_pairs([], decision_protocol=DECISION_PROTOCOL_V2)
        refused["reasons"] = sorted(set(reasons))
        refused["calibrated_offered_arrival_rate_per_second"] = rate
        return refused

    reasons: list[str] = amended_protocol_violations(protocol)
    if protocol.get("protocol") != PROTOCOL_V2:
        reasons.append("benchmark_protocol_invalid")
    metric = protocol.get("metric")
    if (
        not isinstance(metric, Mapping)
        or metric.get("material_threshold_fraction") != MATERIAL_THRESHOLD_FRACTION
    ):
        reasons.append("posthoc_exclusion_or_threshold_change")
    if not isinstance(document, Mapping) or set(document) != RUN_FIELDS_V2:
        return refuse(reasons + ["benchmark_run_shape_invalid"])
    if document.get("protocol") != RUN_PROTOCOL_V2:
        reasons.append("benchmark_run_protocol_invalid")
    if document.get("benchmark_protocol_digest") != protocol_v2_digest(protocol):
        reasons.append("benchmark_protocol_digest_mismatch")

    schedule = protocol["schedule"]
    retained_outcomes = protocol["retained_outcomes"]
    invalidation_rules = set(protocol["invalidation_rules"])
    selected_rate, calibration_reasons = validate_calibration(
        document.get("calibration"),
        rate_ladder=protocol["calibration"]["rate_ladder_per_second"],
        minimum_requests_per_trial=protocol["calibration"]["minimum_requests_per_trial"],
        retained_outcomes=retained_outcomes,
    )
    reasons.extend(calibration_reasons)
    if selected_rate is None:
        return refuse(reasons)

    warmups = document.get("warmups")
    windows = document.get("windows")
    expected_windows = schedule["measured_windows"]
    binding_records: list[Mapping[str, Any]] = []

    if not isinstance(warmups, list) or len(warmups) != 2:
        reasons.append("warmup_invalid")
    else:
        for index, warmup in enumerate(warmups):
            if not isinstance(warmup, Mapping) or set(warmup) != WARMUP_FIELDS_V2:
                reasons.append("warmup_invalid")
                continue
            binding_records.append(warmup)
            if (
                warmup.get("mode") != schedule["warmup_modes"][index]
                or warmup.get("scored") is not False
                or warmup.get("offered_arrival_rate_per_second") != selected_rate
                or type(warmup.get("request_count")) is not int
                or warmup["request_count"] < schedule["minimum_requests_per_warmup"]
                or not _valid_outcomes(
                    warmup.get("outcomes"),
                    request_count=warmup.get("request_count", -1),
                    retained_outcomes=retained_outcomes,
                )
            ):
                reasons.append("warmup_invalid")
            invalidations = warmup.get("invalidations")
            if not isinstance(invalidations, list) or any(
                type(item) is not str or item not in invalidation_rules
                for item in invalidations
            ):
                reasons.append("invalidation_rule_invalid")
            elif invalidations:
                reasons.extend(invalidations)

    shaped_windows: list[Mapping[str, Any]] = []
    if not isinstance(windows, list) or len(windows) != len(expected_windows):
        reasons.append("duplicate_or_missing_window")
    else:
        for expected, window in zip(expected_windows, windows, strict=True):
            if not isinstance(window, Mapping) or set(window) != WINDOW_FIELDS_V2:
                reasons.append("benchmark_window_shape_invalid")
                continue
            binding_records.append(window)
            shaped_windows.append(window)
            if any(window.get(name) != expected[name] for name in expected):
                reasons.append("window_order_or_pairing_changed")
            if window.get("offered_arrival_rate_per_second") != selected_rate:
                reasons.append("workload_or_session_changed")
            request_count = window.get("request_count")
            offered = window.get("offered_request_count")
            if (
                type(request_count) is not int
                or request_count < schedule["minimum_requests_per_measured_window"]
            ):
                reasons.append("fewer_than_minimum_requests")
            elif not _valid_outcomes(
                window.get("outcomes"),
                request_count=request_count,
                retained_outcomes=retained_outcomes,
            ) or type(offered) is not int or offered < request_count:
                reasons.append("incomplete_outcome_accounting")
            tokens = window.get("completed_generated_tokens")
            seconds = window.get("wall_clock_seconds")
            if (
                type(tokens) is not int
                or tokens < 0
                or type(seconds) not in {int, float}
                or isinstance(seconds, bool)
                or not math.isfinite(seconds)
                or seconds <= 0
            ):
                reasons.append("nonfinite_measurement")
            invalidations = window.get("invalidations")
            if not isinstance(invalidations, list) or any(
                type(item) is not str or item not in invalidation_rules
                for item in invalidations
            ):
                reasons.append("invalidation_rule_invalid")
            elif invalidations:
                reasons.extend(invalidations)

    reasons.extend(
        _validate_bindings(
            binding_records,
            identical_fields=protocol["identical_binding_fields"],
        )
    )
    if reasons:
        return refuse(reasons, selected_rate)

    by_pair: dict[int, dict[str, Mapping[str, Any]]] = {}
    for window in shaped_windows:
        by_pair.setdefault(window["pair_id"], {})[window["mode"]] = window
    if set(by_pair) != set(range(protocol["confidence"]["scheduled_pairs"])) or any(
        set(pair) != {"baseline", "candidate"} for pair in by_pair.values()
    ):
        return refuse(["window_order_or_pairing_changed"], selected_rate)
    pairs = [
        {
            "pair_id": pair_id,
            "baseline_index": arms["baseline"]["index"],
            "baseline_completed": arms["baseline"]["outcomes"]["completed"],
            "baseline_offered": arms["baseline"]["offered_request_count"],
            "baseline_rate": float(
                arms["baseline"]["completed_generated_tokens"]
                / arms["baseline"]["wall_clock_seconds"]
            ),
            "candidate_rate": float(
                arms["candidate"]["completed_generated_tokens"]
                / arms["candidate"]["wall_clock_seconds"]
            ),
        }
        for pair_id, arms in sorted(by_pair.items())
    ]
    decision = decide_calibrated_pairs(pairs, decision_protocol=DECISION_PROTOCOL_V2)
    decision["calibrated_offered_arrival_rate_per_second"] = selected_rate
    return decision


__all__ = [
    "AMENDMENT_RULING",
    "AMENDMENT_REFINEMENT",
    "AMENDMENT_RULING_DATE",
    "MATERIAL_THRESHOLD_FRACTION",
    "MINIMUM_BASELINE_COMPLETION_FRACTION",
    "MINIMUM_VALID_PAIRS",
    "PAIR_SELECTION",
    "SCHEDULED_PAIRS",
    "amended_protocol_violations",
    "baseline_window_is_valid",
    "decide_calibrated_pairs",
    "evaluate_benchmark",
    "evaluate_benchmark_v2",
    "load_protocol",
    "load_protocol_v2",
    "protocol_digest",
    "protocol_v2_digest",
    "scheduled_windows_are_registered",
    "validate_calibration",
]
