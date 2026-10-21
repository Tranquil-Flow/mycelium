# SPDX-License-Identifier: AGPL-3.0-only
"""Test-only throughput-materiality checker for the A10 benchmark protocol v2.

The owner ruling of 2026-09-27 (pre-registered amendment after the r33
underpowered result) applies to A10 exactly as to A5, so this module scores
A10 paired windows with the same shared rules as the A5 harness: a recorded
baseline-only calibration selects the offered rate, a baseline window that
completes under 90% of offered requests is invalid and excluded with its pair,
exactly 16 pairs are scheduled of which at least 12 must be valid, every valid
pair is scored with no selection or trimming (owner refinement of 2026-09-27,
made before any v2 data existed), and the +10% paired-bootstrap lower-bound
threshold is unchanged.  Only the throughput-materiality part of the A10
benchmark is decided here; exact output parity, TTFT/TPOT, reliability and
cleanup requirements remain separate A10 acceptance conditions.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

from tests.a5_acceptance.materiality_harness import (
    MATERIAL_THRESHOLD_FRACTION,
    _canonical,
    _valid_outcomes,
    amended_protocol_violations,
    decide_calibrated_pairs,
    validate_calibration,
)


PROTOCOL_PATH = Path(__file__).with_name("benchmark_protocol.v2.json")
PROTOCOL = "mycelium.a10_benchmark_acceptance.v2"
RUN_PROTOCOL = "mycelium.a10_benchmark_throughput_run.v2"
DECISION_PROTOCOL = "mycelium.a10_benchmark_throughput_decision.v2"
DECISION_SCOPE = "throughput_materiality_only"
RUN_FIELDS = {"protocol", "benchmark_protocol_digest", "calibration", "windows"}
WINDOW_FIELDS = {
    "index",
    "pair_id",
    "mode",
    "offered_arrival_rate_per_second",
    "offered_request_count",
    "request_count",
    "outcomes",
    "completed_generated_tokens",
    "wall_clock_seconds",
    "invalidations",
}
# Specification section: route churn, thermal/power drift, stale evidence,
# incompatible software or a missing measurement invalidates the run; a
# post-hoc threshold or exclusion change is never allowed.
INVALIDATION_RULES = {
    "route_changed",
    "thermal_or_power_envelope_drift",
    "authority_stale",
    "software_configuration_or_instrumentation_changed",
    "missing_measurement",
    "posthoc_exclusion_or_threshold_change",
}


def load_protocol() -> dict[str, Any]:
    value = json.loads(PROTOCOL_PATH.read_text("utf-8"))
    assert isinstance(value, dict)
    return value


def protocol_digest(protocol: Mapping[str, Any] | None = None) -> str:
    value = load_protocol() if protocol is None else dict(protocol)
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def evaluate_throughput(document: object) -> dict[str, Any]:
    """Decide A10 throughput materiality under the amended protocol."""

    protocol = load_protocol()

    def finish(decision: dict[str, Any], rate: float | None) -> dict[str, Any]:
        decision["scope"] = DECISION_SCOPE
        decision["calibrated_offered_arrival_rate_per_second"] = rate
        return decision

    def refuse(reasons: list[str], rate: float | None = None) -> dict[str, Any]:
        refused = decide_calibrated_pairs([], decision_protocol=DECISION_PROTOCOL)
        refused["reasons"] = sorted(set(reasons))
        return finish(refused, rate)

    reasons = amended_protocol_violations(protocol)
    if protocol.get("protocol") != PROTOCOL:
        reasons.append("benchmark_protocol_invalid")
    metric = protocol.get("metric")
    requirements = protocol.get("benchmark_requirements")
    if (
        not isinstance(metric, Mapping)
        or metric.get("material_threshold_fraction") != MATERIAL_THRESHOLD_FRACTION
        or not isinstance(requirements, Mapping)
        or requirements.get("throughput_improvement_percent") != 10
        or requirements.get("throughput_lower_bound_percent") != 10
        or requirements.get("paired_bootstrap_confidence_percent") != 95
    ):
        reasons.append("posthoc_exclusion_or_threshold_change")
    if not isinstance(document, Mapping) or set(document) != RUN_FIELDS:
        return refuse(reasons + ["benchmark_run_shape_invalid"])
    if document.get("protocol") != RUN_PROTOCOL:
        reasons.append("benchmark_run_protocol_invalid")
    if document.get("benchmark_protocol_digest") != protocol_digest(protocol):
        reasons.append("benchmark_protocol_digest_mismatch")

    retained = protocol["retained_outcomes"]
    selected_rate, calibration_reasons = validate_calibration(
        document.get("calibration"),
        rate_ladder=protocol["calibration"]["rate_ladder_per_second"],
        minimum_requests_per_trial=protocol["calibration"]["minimum_requests_per_trial"],
        retained_outcomes=retained,
    )
    reasons.extend(calibration_reasons)
    if selected_rate is None:
        return refuse(reasons)

    expected_windows = protocol["measured_windows"]
    minimum_completed = requirements["minimum_completed_requests_per_window"]
    windows = document.get("windows")
    shaped: list[Mapping[str, Any]] = []
    if not isinstance(windows, list) or len(windows) != len(expected_windows):
        reasons.append("duplicate_or_missing_window")
    else:
        for expected, window in zip(expected_windows, windows, strict=True):
            if not isinstance(window, Mapping) or set(window) != WINDOW_FIELDS:
                reasons.append("benchmark_window_shape_invalid")
                continue
            shaped.append(window)
            if any(window.get(name) != expected[name] for name in expected):
                reasons.append("window_order_or_pairing_changed")
            if window.get("offered_arrival_rate_per_second") != selected_rate:
                reasons.append("workload_or_session_changed")
            request_count = window.get("request_count")
            offered = window.get("offered_request_count")
            if (
                type(request_count) is not int
                or type(offered) is not int
                or offered < request_count
                or not _valid_outcomes(
                    window.get("outcomes"),
                    request_count=request_count,
                    retained_outcomes=retained,
                )
            ):
                reasons.append("incomplete_outcome_accounting")
            elif window["outcomes"]["completed"] < minimum_completed:
                reasons.append("fewer_than_minimum_completed_requests")
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
                reasons.append("missing_measurement")
            invalidations = window.get("invalidations")
            if not isinstance(invalidations, list) or any(
                type(item) is not str or item not in INVALIDATION_RULES
                for item in invalidations
            ):
                reasons.append("invalidation_rule_invalid")
            elif invalidations:
                reasons.extend(invalidations)
    if reasons:
        return refuse(reasons, selected_rate)

    by_pair: dict[int, dict[str, Mapping[str, Any]]] = {}
    for window in shaped:
        by_pair.setdefault(window["pair_id"], {})[window["mode"]] = window
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
    return finish(
        decide_calibrated_pairs(pairs, decision_protocol=DECISION_PROTOCOL),
        selected_rate,
    )


__all__ = ["evaluate_throughput", "load_protocol", "protocol_digest"]
