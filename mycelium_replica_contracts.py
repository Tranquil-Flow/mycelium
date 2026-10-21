# SPDX-License-Identifier: AGPL-3.0-only
"""Closed, privacy-reduced A5 replica-path contracts.

Defines the capability-named, closed, bounded, canonically digest-bound
records the A5 gate emits on the production path. See
``docs/superpowers/notes/2026-08-20-mycelium-a5-replica-contract-shapes.md``
for the authoritative field definitions (mirror of spec §7 + §8).

This module is the single source of truth for the wire format. Validators
raise ``ValueError(<code>)`` on any violation. ``compatibility_fixtures()``
returns valid fixture documents for ``scripts/generate_contract_fixtures.py``.

Status: implemented — validators are enforced at the qualifier/install and
product projection boundaries.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any


REPLICA_PLAN_PROTOCOL = "mycelium.replica_plan.v1"
REPLICA_QUALIFICATION_PROTOCOL = "mycelium.replica_qualification.v1"
REPLICA_RUNTIME_PROTOCOL = "mycelium.replica_runtime.v1"
REPLICA_BENCHMARK_PROTOCOL = "mycelium.replica_benchmark.v1"
REPLICA_BENCHMARK_V2_PROTOCOL = "mycelium.replica_benchmark.v2"

# Pre-registered A5 benchmark acceptance protocol v2 (calibrated offered load,
# 16 scheduled pairs, at least 12 valid pairs, every valid pair scored).  These
# mirror tests/a5_acceptance/benchmark_protocol.v2.json; a test pins them to
# the registered JSON so neither side can drift alone.  The v1 contract above
# is untouched and keeps its exact six-pair shape for historical evidence.
BENCHMARK_ACCEPTANCE_V2_PROTOCOL = "mycelium.a5_benchmark_acceptance.v2"
BENCHMARK_V2_SCHEDULED_PAIRS = 16
BENCHMARK_V2_MINIMUM_VALID_PAIRS = 12
BENCHMARK_V2_MATERIAL_THRESHOLD_FRACTION = 0.1
BENCHMARK_V2_PAIR_SELECTION = "all_valid_pairs_no_selection_or_trimming"
BENCHMARK_V2_RATE_LADDER_PER_SECOND = (4.0, 2.0, 1.0, 0.5, 0.25, 0.125, 0.0625)
BENCHMARK_V2_MINIMUM_REQUESTS_PER_TRIAL = 60
BENCHMARK_V2_INVALID_WINDOW_REASON = "baseline_completion_below_minimum_fraction"

_REJECTION_REASONS = frozenset({
    "artifact_verification_failed",
    "load_proof_failed",
    "startup_challenge_failed",
    "parity_mismatch",
    "memory_budget_exceeded",
    "cleanup_budget_exceeded",
    "directed_link_unqualified",
    "stale_authority",
    "mixed_generation",
    "illegal_track",
    "zero_final_flow",
    "unknown_failure_domain",
    "neutral_marginal_gain",
    "owner_authority_missing",
    "replica_loss",
})

_FATAL_REASONS = frozenset({
    "immutable_authority_contradiction",
    "deployment_resource_ledger_corruption",
    "active_authority_compromise",
    "all_qualified_tracks_lost",
})

_REPLICA_REJECTION_REASONS = frozenset({
    "replica_loss",
    "saturation",
    "no_qualified_track",
    "stale_authority",
    "owner_authority_missing",
})

_BENCHMARK_DECISIONS = frozenset({"material", "not_material", "inconclusive"})

_BATCH_MODES = frozenset({"none", "replica_only", "tracked"})

_TRACK_POLICIES = frozenset({
    "round_robin",
    "weighted_round_robin",
    "congestion_aware",
    "spec_default",
})

_MAX_COLLECTION_ITEMS = 256
_MAX_DOCUMENT_NODES = 4096
_MAX_NESTING_DEPTH = 8
_PRIVATE_FIELD_NAMES = frozenset({
    "activation", "activations", "address", "addresses", "command",
    "credential", "credentials", "exception", "kv", "kv_block",
    "kv_blocks", "logit", "logits", "output", "outputs", "path",
    "paths", "private_key", "prompt", "prompts", "raw_address",
    "secret", "secrets", "text", "token_id", "token_ids",
})


def _bounded_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value.encode("utf-8")) <= 256


def _integer(value: object, *, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= (1 << 63) - 1


def _float(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and value not in (float("inf"), float("-inf"))


def _digest(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _exact(document: Mapping[str, Any], fields: set[str], code: str) -> dict[str, Any]:
    if not isinstance(document, Mapping) or set(document) != fields:
        raise ValueError(code)
    return dict(document)


def _bounded_privacy_reduced_json(value: object, code: str) -> None:
    """Reject unbounded or privacy-bearing content anywhere below a shape.

    Closed top-level keys are insufficient when a nested record can smuggle a
    prompt, credential, private path, or an arbitrarily large list.  This
    traversal supplies the common recursive bound required by all four A5
    contracts; shape-specific validators still enforce their exact records.
    """

    remaining = _MAX_DOCUMENT_NODES

    def visit(item: object, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > _MAX_NESTING_DEPTH:
            raise ValueError(code)
        if item is None or isinstance(item, bool):
            return
        if type(item) is int:
            if not _integer(item, minimum=-(1 << 63)):
                raise ValueError(code)
            return
        if isinstance(item, float):
            if not _float(item):
                raise ValueError(code)
            return
        if isinstance(item, str):
            if len(item.encode("utf-8")) > 256:
                raise ValueError(code)
            return
        if isinstance(item, Mapping):
            if len(item) > _MAX_COLLECTION_ITEMS:
                raise ValueError(code)
            for key, nested in item.items():
                if not _bounded_text(key) or key.casefold() in _PRIVATE_FIELD_NAMES:
                    raise ValueError(code)
                visit(nested, depth + 1)
            return
        if isinstance(item, (list, tuple)):
            if len(item) > _MAX_COLLECTION_ITEMS:
                raise ValueError(code)
            for nested in item:
                visit(nested, depth + 1)
            return
        raise ValueError(code)

    visit(value, 0)


def _sorted_unique(values: list[str]) -> list[str]:
    return sorted(set(values))


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def replica_qualification_digest(document: Mapping[str, Any]) -> str:
    """Return the qualifier-owned self-binding identity for one document."""

    candidate = dict(document)
    placeholder = "sha256:" + "0" * 64
    candidate["qualification_id"] = placeholder
    candidate["qualification_digest"] = placeholder
    return "sha256:" + hashlib.sha256(_canonical(candidate)).hexdigest()


def validate_replica_plan(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed replica plan document (mycelium.replica_plan.v1).

    Replicates spec §7 shape 1: planner output, route_ready=false always.
    """
    fields = {
        "protocol", "plan_id", "plan_digest", "deployment_id", "deployment_epoch",
        "base_qualification_digest", "issued_at_unix_ms", "model_id",
        "model_revision", "representation_digest", "route_generation",
        "route_ready", "replica_groups", "legal_tracks", "directed_edges",
        "traffic_fractions", "predicted_marginal_gain_fraction", "uncertainty",
        "failure_domain_facts", "zero_flow_removals", "rejections",
        "workload_digest",
    }
    data = _exact(document, fields, "invalid_replica_plan")
    _bounded_privacy_reduced_json(data, "invalid_replica_plan")
    if (
        data["protocol"] != REPLICA_PLAN_PROTOCOL
        or data["route_ready"] is not False
        or not _bounded_text(data["plan_id"])
        or not _digest(data["plan_digest"])
        or not _bounded_text(data["deployment_id"])
        or not _integer(data["deployment_epoch"])
        or not _digest(data["base_qualification_digest"])
        or not _integer(data["issued_at_unix_ms"])
        or not _bounded_text(data["model_id"])
        or not _bounded_text(data["model_revision"])
        or not _digest(data["representation_digest"])
        or not _integer(data["route_generation"])
        or not isinstance(data["replica_groups"], list)
        or len(data["replica_groups"]) < 2
        or not isinstance(data["legal_tracks"], list)
        or len(data["legal_tracks"]) < 1
        or not isinstance(data["directed_edges"], list)
        or not isinstance(data["traffic_fractions"], list)
        or not _float(data["predicted_marginal_gain_fraction"])
        or not isinstance(data["uncertainty"], Mapping)
        or not isinstance(data["failure_domain_facts"], list)
        or not all(_bounded_text(z) for z in data["zero_flow_removals"])
        or not isinstance(data["rejections"], list)
        or not _digest(data["workload_digest"])
    ):
        raise ValueError("invalid_replica_plan")

    # Every replica group and legal track is an exact, bounded nested record.
    for group in data["replica_groups"]:
        if not (
            isinstance(group, Mapping)
            and set(group) == {"group_id", "layer_range"}
            and _bounded_text(group.get("group_id", ""))
            and isinstance(group.get("layer_range"), list)
            and len(group["layer_range"]) == 2
            and all(_integer(layer) for layer in group["layer_range"])
            and group["layer_range"][0] <= group["layer_range"][1]
        ):
            raise ValueError("invalid_replica_plan")
    for track in data["legal_tracks"]:
        if not (
            isinstance(track, Mapping)
            and set(track) == {"track_id", "placement_sequence"}
            and _bounded_text(track.get("track_id", ""))
            and isinstance(track.get("placement_sequence"), list)
            and 2 <= len(track["placement_sequence"]) <= 64
            and all(_bounded_text(item) for item in track["placement_sequence"])
            and len(set(track["placement_sequence"])) == len(track["placement_sequence"])
        ):
            raise ValueError("invalid_replica_plan")
    if not (
        set(data["uncertainty"]) == {"lower", "upper"}
        and _float(data["uncertainty"]["lower"])
        and _float(data["uncertainty"]["upper"])
        and data["uncertainty"]["lower"] <= data["uncertainty"]["upper"]
    ):
        raise ValueError("invalid_replica_plan")

    # Traffic fractions must sum to 1 within tolerance 0.001
    total = 0.0
    for fraction in data["traffic_fractions"]:
        if not (
            isinstance(fraction, Mapping)
            and set(fraction) == {"track_id", "fraction"}
            and _float(fraction.get("fraction"))
            and 0.0 <= float(fraction["fraction"]) <= 1.0
            and _bounded_text(fraction.get("track_id", ""))
        ):
            raise ValueError("invalid_replica_plan")
        total += float(fraction["fraction"])
    if abs(total - 1.0) > 0.001:
        raise ValueError("invalid_replica_plan")

    return data


def validate_replica_qualification(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed replica qualification (mycelium.replica_qualification.v1).

    Spec §7 shape 2. Only the qualifier emits these. route_ready may be True.
    """
    fields = {
        "protocol", "qualification_id", "qualification_digest", "deployment_id",
        "deployment_epoch", "replica_group_id", "placement_id", "placement_ids",
        "track_id", "traffic_fraction", "qualifier_generation", "issued_at_unix_ms", "expires_at_unix_ms",
        "evidence_bundle_digest", "load_proof_digest",
        "assignment_digest", "artifact_verification_digest", "parity_verified",
        "startup_challenge_passed", "memory_within_bounds",
        "cleanup_within_bounds", "directed_link_qualified",
        "workload_envelope_digest", "rejected_reasons", "route_ready",
    }
    data = _exact(document, fields, "invalid_replica_qualification")
    _bounded_privacy_reduced_json(data, "invalid_replica_qualification")
    if (
        data["protocol"] != REPLICA_QUALIFICATION_PROTOCOL
        or not _digest(data["qualification_id"])
        or not _digest(data["qualification_digest"])
        or not _bounded_text(data["deployment_id"])
        or not _integer(data["deployment_epoch"])
        or not _bounded_text(data["replica_group_id"])
        or not _bounded_text(data["placement_id"])
        or not isinstance(data["placement_ids"], list)
        or not 2 <= len(data["placement_ids"]) <= 64
        or not all(_bounded_text(item) for item in data["placement_ids"])
        or len(set(data["placement_ids"])) != len(data["placement_ids"])
        or data["placement_id"] not in data["placement_ids"]
        or not _bounded_text(data["track_id"])
        or not _float(data["traffic_fraction"])
        or not 0.0 < float(data["traffic_fraction"]) <= 1.0
        or not _integer(data["qualifier_generation"])
        or not _integer(data["issued_at_unix_ms"])
        or not _integer(data["expires_at_unix_ms"], minimum=1)
        or data["expires_at_unix_ms"] < data["issued_at_unix_ms"]
        or not _digest(data["evidence_bundle_digest"])
        or not _digest(data["load_proof_digest"])
        or not _digest(data["assignment_digest"])
        or not _digest(data["artifact_verification_digest"])
        or not isinstance(data["parity_verified"], bool)
        or not isinstance(data["startup_challenge_passed"], bool)
        or not isinstance(data["memory_within_bounds"], bool)
        or not isinstance(data["cleanup_within_bounds"], bool)
        or not isinstance(data["directed_link_qualified"], bool)
        or not _digest(data["workload_envelope_digest"])
        or not isinstance(data["rejected_reasons"], list)
        or not all(_bounded_text(r) for r in data["rejected_reasons"])
        or not isinstance(data["route_ready"], bool)
    ):
        raise ValueError("invalid_replica_qualification")
    reasons = data["rejected_reasons"]
    expected_reasons: set[str] = set()
    if data["qualifier_generation"] <= 0 or data["expires_at_unix_ms"] <= data["issued_at_unix_ms"]:
        expected_reasons.add("stale_authority")
    if data["parity_verified"] is not True:
        expected_reasons.add("parity_mismatch")
    if data["startup_challenge_passed"] is not True:
        expected_reasons.add("startup_challenge_failed")
    if data["memory_within_bounds"] is not True:
        expected_reasons.add("memory_budget_exceeded")
    if data["cleanup_within_bounds"] is not True:
        expected_reasons.add("cleanup_budget_exceeded")
    if data["directed_link_qualified"] is not True:
        expected_reasons.add("directed_link_unqualified")
    if (
        reasons != sorted(set(reasons))
        or any(reason not in _REJECTION_REASONS for reason in reasons)
        or not expected_reasons <= set(reasons)
        or data["route_ready"] is not (not reasons)
    ):
        raise ValueError("invalid_replica_qualification")
    expected_digest = replica_qualification_digest(data)
    if (
        data["qualification_id"] != expected_digest
        or data["qualification_digest"] != expected_digest
    ):
        raise ValueError("invalid_replica_qualification")
    return data


def validate_replica_runtime(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed replica runtime projection (mycelium.replica_runtime.v1).

    Spec §7 shape 3. Privacy: admitted_requests must NOT contain prompts,
    output text, token IDs, logits, KV contents, or credentials. We check the
    keys of each admitted_request are bounded identifiers.
    """
    fields = {
        "protocol", "runtime_digest", "deployment_id", "deployment_epoch",
        "qualification_digest", "route_generation", "snapshot_generation",
        "observed_at_unix_ms", "tracks", "placements", "batch_mode",
        "admitted_requests", "rejected_admissions", "replica_loss_actions",
    }
    data = _exact(document, fields, "invalid_replica_runtime")
    _bounded_privacy_reduced_json(data, "invalid_replica_runtime")
    if (
        data["protocol"] != REPLICA_RUNTIME_PROTOCOL
        or not _digest(data["runtime_digest"])
        or not _bounded_text(data["deployment_id"])
        or not _integer(data["deployment_epoch"])
        or not _digest(data["qualification_digest"])
        or not _integer(data["route_generation"])
        or not _integer(data["snapshot_generation"])
        or not _integer(data["observed_at_unix_ms"])
        or not isinstance(data["tracks"], list)
        or not isinstance(data["placements"], list)
        or data["batch_mode"] not in _BATCH_MODES
        or not isinstance(data["admitted_requests"], list)
        or not isinstance(data["rejected_admissions"], list)
        or not isinstance(data["replica_loss_actions"], list)
    ):
        raise ValueError("invalid_replica_runtime")

    # The request projection is deliberately closed: it carries only the
    # public track binding, never inference material or arbitrary metadata.
    for record in data["admitted_requests"]:
        if not (
            isinstance(record, Mapping)
            and set(record) == {
                "request_id", "track_id", "placement_sequence", "edge_set"
            }
            and _bounded_text(record.get("request_id", ""))
            and _bounded_text(record.get("track_id", ""))
            and isinstance(record.get("placement_sequence"), list)
            and 2 <= len(record["placement_sequence"]) <= 64
            and all(_bounded_text(item) for item in record["placement_sequence"])
            and isinstance(record.get("edge_set"), list)
            and 1 <= len(record["edge_set"]) <= 128
            and all(_bounded_text(item) for item in record["edge_set"])
        ):
            raise ValueError("invalid_replica_runtime")

    return data


def validate_replica_benchmark(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed replica benchmark result (mycelium.replica_benchmark.v1).

    Spec §7 shape 4. Always reports qualification_claim=false and
    promotion_authorized=false (spec §11).
    """
    fields = {
        "protocol", "benchmark_run_id", "benchmark_protocol_digest",
        "workload_manifest_digest", "deployment_id", "deployment_epoch",
        "route_generation", "started_at_unix_ms", "finished_at_unix_ms",
        "primary_only_samples", "replicated_samples", "paired_improvements",
        "point_estimate_fraction",
        "paired_95_percent_bootstrap_lower_bound_fraction",
        "prediction_error_fraction", "decision", "reasons",
        "qualification_claim", "promotion_authorized", "provenance",
    }
    data = _exact(document, fields, "invalid_replica_benchmark")
    _bounded_privacy_reduced_json(data, "invalid_replica_benchmark")
    if (
        data["protocol"] != REPLICA_BENCHMARK_PROTOCOL
        or not _bounded_text(data["benchmark_run_id"])
        or not _digest(data["benchmark_protocol_digest"])
        or not _digest(data["workload_manifest_digest"])
        or not _bounded_text(data["deployment_id"])
        or not _integer(data["deployment_epoch"])
        or not _integer(data["route_generation"])
        or not _integer(data["started_at_unix_ms"])
        or not _integer(data["finished_at_unix_ms"])
        or data["finished_at_unix_ms"] < data["started_at_unix_ms"]
        or not isinstance(data["primary_only_samples"], list)
        or not isinstance(data["replicated_samples"], list)
        or not isinstance(data["paired_improvements"], list)
        or len(data["paired_improvements"]) != 6
        or not all(_float(v) for v in data["paired_improvements"])
        or not _float(data["point_estimate_fraction"])
        or not _float(data["paired_95_percent_bootstrap_lower_bound_fraction"])
        or not _float(data["prediction_error_fraction"])
        or data["decision"] not in _BENCHMARK_DECISIONS
        or not isinstance(data["reasons"], list)
        or not all(_bounded_text(r) for r in data["reasons"])
        or data["qualification_claim"] is not False
        or data["promotion_authorized"] is not False
        or not isinstance(data["provenance"], Mapping)
    ):
        raise ValueError("invalid_replica_benchmark")
    if not (
        set(data["provenance"]) == {
            "software_digest", "configuration_digest", "instrumentation_digest"
        }
        and all(_digest(value) for value in data["provenance"].values())
    ):
        raise ValueError("invalid_replica_benchmark")
    return data


def _v2_baseline_completion_met(completed: int, offered: int) -> bool:
    # Exact integer form of completed / offered >= 0.9.
    return completed * 10 >= offered * 9


def _v2_calibration_valid(calibration: object) -> bool:
    """The recorded baseline-only calibration obeys the registered ladder rule.

    Trials run in descending ladder order and stop at the first rate whose
    baseline completes at least 90% of offered requests; that rate is the only
    admissible selection.  No qualifying rate means every ladder rate was tried
    and nothing is selected.
    """

    if not isinstance(calibration, Mapping) or set(calibration) != {
        "mode", "scored", "rate_ladder_per_second", "trials",
        "selected_offered_arrival_rate_per_second",
    }:
        return False
    ladder = list(BENCHMARK_V2_RATE_LADDER_PER_SECOND)
    trials = calibration["trials"]
    if (
        calibration["mode"] != "baseline"
        or calibration["scored"] is not False
        or calibration["rate_ladder_per_second"] != ladder
        or not isinstance(trials, list)
        or not 1 <= len(trials) <= len(ladder)
    ):
        return False
    qualifying = None
    for ordinal, trial in enumerate(trials):
        if qualifying is not None:
            return False
        if (
            not isinstance(trial, Mapping)
            or set(trial) != {
                "ordinal", "offered_arrival_rate_per_second", "request_count",
                "offered_request_count", "completed",
            }
            or trial["ordinal"] != ordinal
            or type(trial["ordinal"]) is not int
            or trial["offered_arrival_rate_per_second"] != ladder[ordinal]
            or not _integer(trial["request_count"])
            or trial["request_count"] < BENCHMARK_V2_MINIMUM_REQUESTS_PER_TRIAL
            or not _integer(trial["offered_request_count"])
            or trial["offered_request_count"] < trial["request_count"]
            or not _integer(trial["completed"])
            or trial["completed"] > trial["request_count"]
        ):
            return False
        if _v2_baseline_completion_met(trial["completed"], trial["offered_request_count"]):
            qualifying = ladder[ordinal]
    selected = calibration["selected_offered_arrival_rate_per_second"]
    if qualifying is None:
        return len(trials) == len(ladder) and selected is None
    return type(selected) is float and selected == qualifying


def _v2_scheduled_baseline_index(pair_id: int) -> int:
    # ABBA schedule: even pairs run baseline first, odd pairs baseline second.
    return 2 * pair_id + (pair_id % 2)


def validate_replica_benchmark_v2(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a closed replica benchmark result under benchmark protocol v2.

    Carries the pre-registered v2 decision: 16 scheduled pairs, the valid pair
    ids that were scored (all of them, no selection), every excluded window
    with its reason, the baseline calibration record, and the unchanged +10%
    threshold.  Fewer than 12 valid pairs can never be material.  Always
    reports qualification_claim=false and promotion_authorized=false.  The v1
    shape (exactly six paired improvements) is a separate contract and is not
    relaxed by this one.
    """
    code = "invalid_replica_benchmark_v2"
    fields = {
        "protocol", "benchmark_run_id", "benchmark_protocol",
        "benchmark_protocol_digest", "workload_manifest_digest",
        "deployment_id", "route_generation", "started_at_unix_ms",
        "finished_at_unix_ms", "calibration", "scheduled_pairs",
        "minimum_valid_pairs", "pair_selection", "material_threshold_fraction",
        "valid_pair_ids", "excluded_windows", "paired_improvements",
        "point_estimate_fraction",
        "paired_95_percent_bootstrap_lower_bound_fraction",
        "prediction_error_fraction", "decision", "reasons",
        "qualification_claim", "promotion_authorized", "provenance",
    }
    data = _exact(document, fields, code)
    _bounded_privacy_reduced_json(data, code)
    if (
        data["protocol"] != REPLICA_BENCHMARK_V2_PROTOCOL
        or data["benchmark_protocol"] != BENCHMARK_ACCEPTANCE_V2_PROTOCOL
        or not _bounded_text(data["benchmark_run_id"])
        or not _digest(data["benchmark_protocol_digest"])
        or not _digest(data["workload_manifest_digest"])
        or not _bounded_text(data["deployment_id"])
        or not _integer(data["route_generation"])
        or not _integer(data["started_at_unix_ms"])
        or not _integer(data["finished_at_unix_ms"])
        or data["finished_at_unix_ms"] < data["started_at_unix_ms"]
        or type(data["scheduled_pairs"]) is not int
        or data["scheduled_pairs"] != BENCHMARK_V2_SCHEDULED_PAIRS
        or type(data["minimum_valid_pairs"]) is not int
        or data["minimum_valid_pairs"] != BENCHMARK_V2_MINIMUM_VALID_PAIRS
        or data["pair_selection"] != BENCHMARK_V2_PAIR_SELECTION
        or type(data["material_threshold_fraction"]) is not float
        or data["material_threshold_fraction"] != BENCHMARK_V2_MATERIAL_THRESHOLD_FRACTION
        or data["decision"] not in _BENCHMARK_DECISIONS
        or not isinstance(data["reasons"], list)
        or not all(_bounded_text(r) for r in data["reasons"])
        or data["qualification_claim"] is not False
        or data["promotion_authorized"] is not False
        or not isinstance(data["provenance"], Mapping)
        or not _v2_calibration_valid(data["calibration"])
    ):
        raise ValueError(code)
    if not (
        set(data["provenance"]) == {
            "software_digest", "configuration_digest", "instrumentation_digest"
        }
        and all(_digest(value) for value in data["provenance"].values())
    ):
        raise ValueError(code)

    scheduled = set(range(BENCHMARK_V2_SCHEDULED_PAIRS))
    valid_ids = data["valid_pair_ids"]
    excluded = data["excluded_windows"]
    if (
        not isinstance(valid_ids, list)
        or not all(type(v) is int and v in scheduled for v in valid_ids)
        or valid_ids != sorted(set(valid_ids))
        or not isinstance(excluded, list)
    ):
        raise ValueError(code)
    excluded_ids: list[int] = []
    for window in excluded:
        if (
            not isinstance(window, Mapping)
            or set(window) != {
                "pair_id", "baseline_index", "reason", "baseline_completed",
                "baseline_offered",
            }
            or type(window["pair_id"]) is not int
            or window["pair_id"] not in scheduled
            or type(window["baseline_index"]) is not int
            or window["baseline_index"] != _v2_scheduled_baseline_index(window["pair_id"])
            or window["reason"] != BENCHMARK_V2_INVALID_WINDOW_REASON
            or not _integer(window["baseline_completed"])
            or not _integer(window["baseline_offered"], minimum=1)
            or window["baseline_completed"] > window["baseline_offered"]
            # An excluded window must actually fail the registered rule.
            or _v2_baseline_completion_met(
                window["baseline_completed"], window["baseline_offered"]
            )
        ):
            raise ValueError(code)
        excluded_ids.append(window["pair_id"])
    if excluded_ids != sorted(set(excluded_ids)) or set(excluded_ids) & set(valid_ids):
        raise ValueError(code)
    accounted = set(valid_ids) | set(excluded_ids)
    # Either nothing was scored (refused before pairing) or every scheduled
    # pair is accounted for: valid and scored, or excluded with its reason.
    if accounted and accounted != scheduled:
        raise ValueError(code)
    if data["calibration"]["selected_offered_arrival_rate_per_second"] is None and accounted:
        raise ValueError(code)

    improvements = data["paired_improvements"]
    point = data["point_estimate_fraction"]
    lower = data["paired_95_percent_bootstrap_lower_bound_fraction"]
    prediction_error = data["prediction_error_fraction"]
    if not isinstance(improvements, list) or not all(
        type(v) is float and _float(v) for v in improvements
    ):
        raise ValueError(code)
    if prediction_error is not None and not (
        type(prediction_error) is float and _float(prediction_error)
    ):
        raise ValueError(code)
    threshold = BENCHMARK_V2_MATERIAL_THRESHOLD_FRACTION

    def at_least(value: float) -> bool:
        return value > threshold or abs(value - threshold) <= 1e-12

    if not improvements:
        # Unscored: never material, and no estimate may be claimed.
        if (
            data["decision"] != "inconclusive"
            or point is not None
            or lower is not None
            or prediction_error is not None
            # Enough valid pairs but no scores is only admissible when the
            # measurements themselves were unusable.
            or (
                len(valid_ids) >= BENCHMARK_V2_MINIMUM_VALID_PAIRS
                and "nonfinite_measurement" not in data["reasons"]
            )
        ):
            raise ValueError(code)
        return data
    # Scored: every valid pair, and only valid pairs, carries an improvement.
    if (
        len(improvements) != len(valid_ids)
        or len(valid_ids) < BENCHMARK_V2_MINIMUM_VALID_PAIRS
        or type(point) is not float
        or not _float(point)
        or type(lower) is not float
        or not _float(lower)
        or abs(point - sum(improvements) / len(improvements)) > 1e-9
    ):
        raise ValueError(code)
    if not at_least(point):
        expected = "not_material"
    elif not at_least(lower):
        expected = "inconclusive"
    else:
        expected = "material"
    if data["decision"] != expected:
        raise ValueError(code)
    return data


def _compatibility_qualification_fixture(
    *, digest_b: str, digest_c: str, digest_d: str, digest_e: str
) -> dict[str, Any]:
    rejected_reasons: list[str] = []
    document: dict[str, Any] = {
        "protocol": REPLICA_QUALIFICATION_PROTOCOL,
        "qualification_id": "sha256:" + "0" * 64,
        "qualification_digest": "sha256:" + "0" * 64,
        "deployment_id": "deployment-fixture",
        "deployment_epoch": 1,
        "replica_group_id": "group-fixture-0",
        "placement_id": "placement-fixture-replica",
        "placement_ids": [
            "placement-fixture-replica",
            "placement-fixture-stage-1",
        ],
        "track_id": "track-fixture",
        "traffic_fraction": 0.5,
        "qualifier_generation": 1,
        "issued_at_unix_ms": 10_000,
        "expires_at_unix_ms": 600_000,
        "evidence_bundle_digest": digest_b,
        "load_proof_digest": digest_c,
        "assignment_digest": digest_d,
        "artifact_verification_digest": digest_d,
        "parity_verified": True,
        "startup_challenge_passed": True,
        "memory_within_bounds": True,
        "cleanup_within_bounds": True,
        "directed_link_qualified": True,
        "workload_envelope_digest": digest_e,
        "rejected_reasons": rejected_reasons,
        "route_ready": not rejected_reasons,
    }
    identity = replica_qualification_digest(document)
    document["qualification_id"] = identity
    document["qualification_digest"] = identity
    return validate_replica_qualification(document)


def compatibility_fixtures() -> dict[str, dict[str, Any]]:
    """Return minimal valid fixture documents for the four replica shapes.

    Consumed by ``scripts/generate_contract_fixtures.py``. Each fixture uses
    placeholder sha256 digests so the canonical digest is reproducible and
    privacy-clean (no real bindings).
    """
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    digest_c = "sha256:" + "c" * 64
    digest_d = "sha256:" + "d" * 64
    digest_e = "sha256:" + "e" * 64
    return {
        "replica-plan-v1.json": validate_replica_plan(
            {
                "protocol": REPLICA_PLAN_PROTOCOL,
                "plan_id": "plan-fixture",
                "plan_digest": digest_a,
                "deployment_id": "deployment-fixture",
                "deployment_epoch": 1,
                "base_qualification_digest": digest_b,
                "issued_at_unix_ms": 10_000,
                "model_id": "model-fixture",
                "model_revision": "revision-fixture",
                "representation_digest": digest_c,
                "route_generation": 7,
                "route_ready": False,
                "replica_groups": [
                    {"group_id": "group-fixture-0", "layer_range": [0, 22]},
                    {"group_id": "group-fixture-1", "layer_range": [23, 24]},
                ],
                "legal_tracks": [
                    {"track_id": "track-fixture", "placement_sequence": ["p0", "p1"]},
                ],
                "directed_edges": [],
                "traffic_fractions": [
                    {"track_id": "track-fixture", "fraction": 1.0},
                ],
                "predicted_marginal_gain_fraction": 0.12,
                "uncertainty": {"lower": 0.0, "upper": 0.3},
                "failure_domain_facts": [],
                "zero_flow_removals": [],
                "rejections": [],
                "workload_digest": digest_d,
            }
        ),
        "replica-qualification-v1.json": _compatibility_qualification_fixture(
            digest_b=digest_b,
            digest_c=digest_c,
            digest_d=digest_d,
            digest_e=digest_e,
        ),
        "replica-runtime-v1.json": validate_replica_runtime(
            {
                "protocol": REPLICA_RUNTIME_PROTOCOL,
                "runtime_digest": digest_a,
                "deployment_id": "deployment-fixture",
                "deployment_epoch": 1,
                "qualification_digest": digest_b,
                "route_generation": 7,
                "snapshot_generation": 1,
                "observed_at_unix_ms": 10_000,
                "tracks": [],
                "placements": [],
                "batch_mode": "tracked",
                "admitted_requests": [
                    {
                        "request_id": "request-fixture",
                        "track_id": "track-fixture",
                        "placement_sequence": ["p0", "p1"],
                        "edge_set": ["e0", "e1"],
                    },
                ],
                "rejected_admissions": [],
                "replica_loss_actions": [],
            }
        ),
        "replica-benchmark-v1.json": validate_replica_benchmark(
            {
                "protocol": REPLICA_BENCHMARK_PROTOCOL,
                "benchmark_run_id": "run-fixture",
                "benchmark_protocol_digest": digest_a,
                "workload_manifest_digest": digest_b,
                "deployment_id": "deployment-fixture",
                "deployment_epoch": 1,
                "route_generation": 7,
                "started_at_unix_ms": 10_000,
                "finished_at_unix_ms": 1_000_000,
                "primary_only_samples": [],
                "replicated_samples": [],
                "paired_improvements": [0.12, 0.14, 0.11, 0.15, 0.13, 0.16],
                "point_estimate_fraction": 0.135,
                "paired_95_percent_bootstrap_lower_bound_fraction": 0.105,
                "prediction_error_fraction": -0.015,
                "decision": "material",
                "reasons": ["throughput_window_within_tolerance"],
                "qualification_claim": False,
                "promotion_authorized": False,
                "provenance": {
                    "software_digest": digest_c,
                    "configuration_digest": digest_d,
                    "instrumentation_digest": digest_e,
                },
            }
        ),
        "replica-benchmark-v2.json": validate_replica_benchmark_v2(
            _compatibility_benchmark_v2_fixture(
                digest_a=digest_a,
                digest_b=digest_b,
                digest_c=digest_c,
                digest_d=digest_d,
                digest_e=digest_e,
            )
        ),
    }


def _compatibility_benchmark_v2_fixture(
    *, digest_a: str, digest_b: str, digest_c: str, digest_d: str, digest_e: str
) -> dict[str, Any]:
    # Calibration: 4 and 2 req/s overload the baseline, 1 req/s qualifies.
    # Pairs 3 and 9 fail the baseline completion rule and are excluded; the
    # other 14 valid pairs are all scored.
    excluded_pairs = (3, 9)
    valid_pair_ids = [
        pair for pair in range(BENCHMARK_V2_SCHEDULED_PAIRS) if pair not in excluded_pairs
    ]
    improvements = [0.125 + 0.0078125 * (index % 4) for index in range(len(valid_pair_ids))]
    return {
        "protocol": REPLICA_BENCHMARK_V2_PROTOCOL,
        "benchmark_run_id": "run-fixture-v2",
        "benchmark_protocol": BENCHMARK_ACCEPTANCE_V2_PROTOCOL,
        "benchmark_protocol_digest": digest_a,
        "workload_manifest_digest": digest_b,
        "deployment_id": "deployment-fixture",
        "route_generation": 7,
        "started_at_unix_ms": 10_000,
        "finished_at_unix_ms": 2_000_000,
        "calibration": {
            "mode": "baseline",
            "scored": False,
            "rate_ladder_per_second": list(BENCHMARK_V2_RATE_LADDER_PER_SECOND),
            "trials": [
                {
                    "ordinal": 0,
                    "offered_arrival_rate_per_second": 4.0,
                    "request_count": 60,
                    "offered_request_count": 240,
                    "completed": 12,
                },
                {
                    "ordinal": 1,
                    "offered_arrival_rate_per_second": 2.0,
                    "request_count": 60,
                    "offered_request_count": 120,
                    "completed": 40,
                },
                {
                    "ordinal": 2,
                    "offered_arrival_rate_per_second": 1.0,
                    "request_count": 60,
                    "offered_request_count": 60,
                    "completed": 58,
                },
            ],
            "selected_offered_arrival_rate_per_second": 1.0,
        },
        "scheduled_pairs": BENCHMARK_V2_SCHEDULED_PAIRS,
        "minimum_valid_pairs": BENCHMARK_V2_MINIMUM_VALID_PAIRS,
        "pair_selection": BENCHMARK_V2_PAIR_SELECTION,
        "material_threshold_fraction": BENCHMARK_V2_MATERIAL_THRESHOLD_FRACTION,
        "valid_pair_ids": valid_pair_ids,
        "excluded_windows": [
            {
                "pair_id": pair,
                "baseline_index": _v2_scheduled_baseline_index(pair),
                "reason": BENCHMARK_V2_INVALID_WINDOW_REASON,
                "baseline_completed": 50,
                "baseline_offered": 60,
            }
            for pair in excluded_pairs
        ],
        "paired_improvements": improvements,
        "point_estimate_fraction": sum(improvements) / len(improvements),
        "paired_95_percent_bootstrap_lower_bound_fraction": 0.125,
        "prediction_error_fraction": None,
        "decision": "material",
        "reasons": [],
        "qualification_claim": False,
        "promotion_authorized": False,
        "provenance": {
            "software_digest": digest_c,
            "configuration_digest": digest_d,
            "instrumentation_digest": digest_e,
        },
    }


# Public exports
__all__ = [
    "REPLICA_PLAN_PROTOCOL",
    "REPLICA_QUALIFICATION_PROTOCOL",
    "REPLICA_RUNTIME_PROTOCOL",
    "REPLICA_BENCHMARK_PROTOCOL",
    "REPLICA_BENCHMARK_V2_PROTOCOL",
    "BENCHMARK_ACCEPTANCE_V2_PROTOCOL",
    "validate_replica_plan",
    "validate_replica_qualification",
    "validate_replica_runtime",
    "validate_replica_benchmark",
    "validate_replica_benchmark_v2",
    "compatibility_fixtures",
]
