from __future__ import annotations

import json
from typing import Any

from .compiler import CapacityProfile, compile_capacity_profile
from .contracts import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    canonical_json_bytes,
)


MAX_PROFILE_DOCUMENT_BYTES = 256 * 1024

_TOP_LEVEL_FIELDS = frozenset(
    {
        "protocol",
        "key",
        "policy",
        "points",
        "max_safe_concurrency",
        "interactive_concurrency_limit",
        "batch_concurrency_limit",
        "safety_boundary",
        "interactive_boundary",
        "evidence_scope",
        "qualification_evaluated",
        "route_ready",
        "release_ready",
        "profile_digest",
    }
)
_KEY_FIELDS = frozenset(
    {
        "model_digest",
        "source_evidence_digest",
        "quantization",
        "backend",
        "runtime_build",
        "hardware_class",
        "power_mode",
        "context_bucket",
        "kv_mode",
    }
)
_POLICY_FIELDS = frozenset(
    {"ttft_p95_slo_ms", "tpot_p95_slo_ms", "min_samples"}
)
_POINT_FIELDS = frozenset(
    {
        "concurrency",
        "sample_count",
        "p95_ttft_ms",
        "p95_tpot_ms",
        "aggregate_output_tps",
        "peak_memory_bytes",
        "memory_budget_bytes",
        "oom",
        "thermal_throttled",
        "safe",
        "interactive_slo_met",
    }
)
_BOUNDARY_FIELDS = (
    frozenset({"kind", "concurrency"}),
    frozenset({"kind", "concurrency", "reasons"}),
)


def _reject_nonfinite(_: str) -> None:
    raise ValueError("capacity profile JSON numbers must be finite")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("capacity profile JSON contains duplicate keys")
        result[key] = value
    return result


def _require_exact_object(
    name: str,
    value: object,
    fields: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict or frozenset(value) != fields:  # type: ignore[arg-type]
        raise ValueError(f"capacity profile {name} must use its exact schema")
    return value  # type: ignore[return-value]


def _validate_closed_schema(document: dict[str, Any]) -> None:
    _require_exact_object("document", document, _TOP_LEVEL_FIELDS)
    _require_exact_object("key", document["key"], _KEY_FIELDS)
    _require_exact_object("policy", document["policy"], _POLICY_FIELDS)

    points = document["points"]
    if type(points) is not list:
        raise ValueError("capacity profile points must use their exact schema")
    for point in points:
        _require_exact_object("point", point, _POINT_FIELDS)

    for name in ("safety_boundary", "interactive_boundary"):
        boundary = document[name]
        if type(boundary) is not dict or frozenset(boundary) not in _BOUNDARY_FIELDS:
            raise ValueError(f"capacity profile {name} must use its exact schema")
        if "reasons" in boundary:
            reasons = boundary["reasons"]
            if type(reasons) is not list or any(type(reason) is not str for reason in reasons):
                raise ValueError(f"capacity profile {name} must use its exact schema")


def _rebuild_profile(document: dict[str, Any]) -> CapacityProfile:
    try:
        key = CapacityProfileKey(**document["key"])
        policy = CapacityProfilePolicy(**document["policy"])
        observations = tuple(
            CapacityObservation(
                concurrency=point["concurrency"],
                sample_count=point["sample_count"],
                p95_ttft_ms=point["p95_ttft_ms"],
                p95_tpot_ms=point["p95_tpot_ms"],
                aggregate_output_tps=point["aggregate_output_tps"],
                peak_memory_bytes=point["peak_memory_bytes"],
                memory_budget_bytes=point["memory_budget_bytes"],
                oom=point["oom"],
                thermal_throttled=point["thermal_throttled"],
            )
            for point in document["points"]
        )
        return compile_capacity_profile(key, observations, policy)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(
            "capacity profile document does not match its derived profile"
        ) from exc


def parse_capacity_profile_bytes(payload: bytes) -> CapacityProfile:
    """Parse exact canonical compiler output and re-derive every claimed field."""

    if type(payload) is not bytes:
        raise ValueError("capacity profile payload must be bytes")
    if not payload or len(payload) > MAX_PROFILE_DOCUMENT_BYTES:
        raise ValueError("capacity profile payload must be a non-empty bounded byte string")

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("capacity profile payload must be UTF-8") from exc

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_nonfinite,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("capacity profile payload must be valid JSON") from exc
    except RecursionError as exc:
        raise ValueError("capacity profile JSON nesting must be bounded") from exc

    if type(value) is not dict:
        raise ValueError("capacity profile document must be an object")
    document: dict[str, Any] = value

    try:
        encoded = canonical_json_bytes(document)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("capacity profile payload must be canonical JSON") from exc
    if encoded != payload:
        raise ValueError("capacity profile payload must be canonical JSON")

    _validate_closed_schema(document)
    profile = _rebuild_profile(document)
    if profile.canonical_json_bytes() != payload:
        raise ValueError("capacity profile document does not match its derived profile")
    return profile
