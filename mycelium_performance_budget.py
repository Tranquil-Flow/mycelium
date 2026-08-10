# SPDX-License-Identifier: AGPL-3.0-or-later
"""Closed M12 performance-budget contract shared by canary and workload gates."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping


PROTOCOL = "mycelium.performance_budget.v1"
PROTOCOL_V2 = "mycelium.performance_budget.v2"
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:~-]{0,127}\Z")


class PerformanceBudgetError(ValueError):
    """Stable contract failure without embedding rejected input."""

    def __init__(self, code: str = "performance_budget_invalid") -> None:
        self.code = code
        super().__init__(code)


def _identifier(value: object) -> bool:
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def _positive_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _positive_integer(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= MAX_SAFE_INTEGER
    )


def _nonnegative_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _latency_budget(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and set(value) == {"maximum_p50", "maximum_p95"}
        and _positive_number(value["maximum_p50"])
        and _positive_number(value["maximum_p95"])
        and float(value["maximum_p50"]) <= float(value["maximum_p95"])
    )


def _bounded_integer_map(value: object) -> bool:
    return (
        isinstance(value, Mapping)
        and 1 <= len(value) <= 256
        and all(_identifier(key) and _positive_integer(item) for key, item in value.items())
    )


def validate_performance_budget(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and detach one sequential M12 performance acceptance budget."""

    expected = {
        "protocol",
        "budget_id",
        "workload_label",
        "minimum_sample_size",
        "ttft_ms",
        "tpot_ms",
        "minimum_output_tokens_per_second",
        "maximum_peak_rss_bytes_by_member",
        "maximum_frames_per_request_by_stage",
        "execution_scope",
        "queueing_budget_state",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise PerformanceBudgetError()
    if (
        document["protocol"] != PROTOCOL
        or not _identifier(document["budget_id"])
        or not _identifier(document["workload_label"])
        or not _positive_integer(document["minimum_sample_size"])
        or not _latency_budget(document["ttft_ms"])
        or not _latency_budget(document["tpot_ms"])
        or not _positive_number(document["minimum_output_tokens_per_second"])
        or not _bounded_integer_map(document["maximum_peak_rss_bytes_by_member"])
        or not _bounded_integer_map(document["maximum_frames_per_request_by_stage"])
        or document["execution_scope"] != "sequential_observed"
        or document["queueing_budget_state"] != "deferred_to_m16"
    ):
        raise PerformanceBudgetError()
    try:
        return json.loads(
            json.dumps(
                dict(document),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise PerformanceBudgetError() from exc


def validate_performance_budget_v2(document: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one M15 workload-calibration budget without weakening v1."""

    expected = {
        "protocol",
        "budget_id",
        "profile_id",
        "minimum_sample_size",
        "ttft_ms_maximum",
        "tpot_ms_maximum",
        "minimum_output_tokens_per_second",
        "maximum_frames_per_request",
        "maximum_relative_model_error",
        "execution_scope",
        "peak_memory_budget_state",
        "energy_thermal_budget_state",
        "reconnect_budget_state",
        "queueing_budget_state",
        "admission_latency_budget_state",
        "concurrency_budget_state",
        "batch_shape_budget_state",
    }
    if not isinstance(document, Mapping) or set(document) != expected:
        raise PerformanceBudgetError()
    error_bounds = document["maximum_relative_model_error"]
    if (
        document["protocol"] != PROTOCOL_V2
        or not _identifier(document["budget_id"])
        or not _identifier(document["profile_id"])
        or not _positive_integer(document["minimum_sample_size"])
        or not _positive_number(document["ttft_ms_maximum"])
        or not _positive_number(document["tpot_ms_maximum"])
        or not _positive_number(document["minimum_output_tokens_per_second"])
        or not _positive_integer(document["maximum_frames_per_request"])
        or not isinstance(error_bounds, Mapping)
        or set(error_bounds) != {"ttft", "tpot", "throughput"}
        or not all(_nonnegative_number(value) for value in error_bounds.values())
        or document["execution_scope"] != "sequential_observed"
        or document["peak_memory_budget_state"] != "approved_exclusion"
        or document["energy_thermal_budget_state"] != "approved_exclusion"
        or document["reconnect_budget_state"] != "approved_exclusion"
        or document["queueing_budget_state"] != "deferred_to_m16"
        or document["admission_latency_budget_state"] != "deferred_to_m16"
        or document["concurrency_budget_state"] != "deferred_to_m16"
        or document["batch_shape_budget_state"] != "deferred_to_m16"
    ):
        raise PerformanceBudgetError()
    try:
        return json.loads(
            json.dumps(
                dict(document),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise PerformanceBudgetError() from exc


__all__ = [
    "PROTOCOL",
    "PROTOCOL_V2",
    "PerformanceBudgetError",
    "validate_performance_budget",
    "validate_performance_budget_v2",
]
