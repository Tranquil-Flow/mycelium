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


__all__ = ["evaluate_benchmark", "load_protocol", "protocol_digest"]
