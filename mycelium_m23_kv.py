"""Closed validator for the sealed M23 heterogeneous KV A/B gate."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any


PROTOCOL = "mycelium.m23_heterogeneous_kv_gate.v1"
_FIELDS = frozenset(
    {
        "protocol",
        "generated_at_unix_ms",
        "replay_capture_digest",
        "kv_capture_digest",
        "gates",
        "implemented",
        "performance_qualified",
        "promotion_state",
        "measurements",
        "claim_boundary",
        "evidence_digest",
    }
)
_GATES = frozenset(
    {
        "same_route_model_stages_hosts",
        "same_prompt_and_budget",
        "exact_output_parity",
        "one_token_decode_every_stage",
        "all_stages_advanced_physical_counters",
        "kv_active_then_terminally_released",
        "no_fatal_or_cleanup_failure",
        "measured_tpot_improvement",
    }
)
_MEASUREMENTS = frozenset(
    {
        "replay_tpot_ms",
        "kv_tpot_ms",
        "tpot_delta_ms",
        "tpot_improvement_ratio",
        "replay_activation_output_bytes",
        "kv_activation_output_bytes",
        "activation_byte_delta",
        "replay_total_ms",
        "kv_total_ms",
    }
)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _closed(value: Any, fields: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("m23_kv_evidence_invalid")
    return copy.deepcopy(dict(value))


def _sha(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise ValueError("m23_kv_evidence_invalid")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ValueError("m23_kv_evidence_invalid") from exc
    return value


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("m23_kv_evidence_invalid")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("m23_kv_evidence_invalid")
    return result


def validate_m23_kv_evidence(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate closed evidence and recompute every promotion decision."""

    try:
        result = _closed(document, _FIELDS)
        if result["protocol"] != PROTOCOL:
            raise ValueError("m23_kv_evidence_invalid")
        if type(result["generated_at_unix_ms"]) is not int or result[
            "generated_at_unix_ms"
        ] <= 0:
            raise ValueError("m23_kv_evidence_invalid")
        _sha(result["replay_capture_digest"])
        _sha(result["kv_capture_digest"])
        gates = _closed(result["gates"], _GATES)
        if any(type(value) is not bool for value in gates.values()):
            raise ValueError("m23_kv_evidence_invalid")
        measurements = _closed(result["measurements"], _MEASUREMENTS)
        for value in measurements.values():
            _number(value)
        for key in (
            "replay_tpot_ms",
            "kv_tpot_ms",
            "replay_activation_output_bytes",
            "kv_activation_output_bytes",
            "replay_total_ms",
            "kv_total_ms",
        ):
            if _number(measurements[key]) < 0:
                raise ValueError("m23_kv_evidence_invalid")
        if not math.isclose(
            _number(measurements["tpot_delta_ms"]),
            _number(measurements["kv_tpot_ms"])
            - _number(measurements["replay_tpot_ms"]),
            rel_tol=1e-12,
            abs_tol=1e-9,
        ):
            raise ValueError("m23_kv_evidence_invalid")
        if measurements["activation_byte_delta"] != (
            measurements["kv_activation_output_bytes"]
            - measurements["replay_activation_output_bytes"]
        ):
            raise ValueError("m23_kv_evidence_invalid")
        implemented = all(
            value
            for key, value in gates.items()
            if key != "measured_tpot_improvement"
        )
        performance = implemented and gates["measured_tpot_improvement"]
        promotion = (
            "qualified"
            if performance
            else "implemented_not_performance_qualified"
            if implemented
            else "withheld"
        )
        if (
            result["implemented"] is not implemented
            or result["performance_qualified"] is not performance
            or result["promotion_state"] != promotion
        ):
            raise ValueError("m23_kv_evidence_invalid")
        if not isinstance(result["claim_boundary"], str) or not (
            1 <= len(result["claim_boundary"]) <= 512
        ):
            raise ValueError("m23_kv_evidence_invalid")
        supplied = _sha(result["evidence_digest"])
        unsigned = copy.deepcopy(result)
        del unsigned["evidence_digest"]
        if supplied != _digest(unsigned):
            raise ValueError("m23_kv_evidence_invalid")
        return result
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "m23_kv_evidence_invalid":
            raise
        raise ValueError("m23_kv_evidence_invalid") from exc


__all__ = ["PROTOCOL", "validate_m23_kv_evidence"]
