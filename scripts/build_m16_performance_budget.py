#!/usr/bin/env python3
"""Build a closed M16 performance budget from physical gate evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_performance_budget import validate_performance_budget_v3  # noqa: E402


def load_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        document = json.load(stream)
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain an object")
    return document


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def evidence_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def m15_interactive_ttft(document: dict[str, Any]) -> float:
    observations = document.get("observations")
    if not isinstance(observations, list):
        raise ValueError("M15 observations missing")
    for observation in observations:
        if (
            isinstance(observation, dict)
            and observation.get("profile_id") == "interactive_chat_v1"
            and isinstance(observation.get("observed"), dict)
        ):
            value = observation["observed"].get("ttft_ms")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                return float(value)
    raise ValueError("M15 interactive TTFT observation missing")


def dimension(
    name: str,
    *,
    state: str,
    bound: float,
    observed: float,
    unit: str,
    digest: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "dimension": name,
        "state": state,
        "bound": bound,
        "observed": observed,
        "unit": unit,
        "evidence_digest": digest,
        "reason": reason,
    }


def build_budget(gate_path: Path, comparison_path: Path) -> dict[str, Any]:
    gate = load_object(gate_path)
    comparison = load_object(comparison_path)
    if (
        gate.get("protocol") != "mycelium.m16_physical_gate.v1"
        or gate.get("claim") != "concurrent_physical_observed"
    ):
        raise ValueError("invalid M16 physical gate evidence")
    requests = gate.get("terminal_requests")
    if not isinstance(requests, dict) or set(requests) != {
        "active_batch",
        "queued_batch",
        "queued_interactive",
    }:
        raise ValueError("M16 terminal request evidence incomplete")
    digest = evidence_digest(gate_path)
    admission = max(float(item["admission_latency_ms"]) for item in requests.values())
    waits = [
        float(item["queue_wait_ms"])
        for item in requests.values()
        if item["queue_wait_ms"] is not None
    ]
    maximum_wait = max(waits)
    completed = sum(item["phase"] == "completed" for item in requests.values())
    active_batch_lifecycle = float(requests["active_batch"]["lifecycle_ms"])
    baseline_ttft = m15_interactive_ttft(comparison)
    interactive_wait = float(requests["queued_interactive"]["queue_wait_ms"])
    regression = (baseline_ttft + interactive_wait) / baseline_ttft
    maximum_queue_depth = float(gate["concurrent_queue"]["depth"])
    cancellation_release = float(gate["cancellation"]["release_latency_ms"])

    dimensions = [
        dimension(
            "admission_latency_p95_ms",
            state="met" if admission <= 100.0 else "failed",
            bound=100.0,
            observed=admission,
            unit="milliseconds",
            digest=digest,
            reason="maximum_of_three_observed_admission_latencies",
        ),
        dimension(
            "queue_wait_p95_ms",
            state="met" if maximum_wait <= 60_000.0 else "failed",
            bound=60_000.0,
            observed=maximum_wait,
            unit="milliseconds",
            digest=digest,
            reason="maximum_of_observed_completed_request_queue_waits",
        ),
        dimension(
            "maximum_queue_depth",
            state="met" if maximum_queue_depth <= 32.0 else "failed",
            bound=32.0,
            observed=maximum_queue_depth,
            unit="requests",
            digest=digest,
            reason="concurrent_physical_queue_remained_within_bound",
        ),
        dimension(
            "completed_requests",
            state="met" if completed >= 2 else "failed",
            bound=2.0,
            observed=float(completed),
            unit="requests",
            digest=digest,
            reason="minimum_two_physical_requests_completed_and_one_cancelled",
        ),
        dimension(
            "interactive_latency_regression_ratio",
            state="met" if regression <= 6.0 else "failed",
            bound=6.0,
            observed=regression,
            unit="ratio",
            digest=digest,
            reason="queued_interactive_wait_plus_m15_ttft_over_m15_ttft",
        ),
        dimension(
            "batch_starvation_interval_ms",
            state="met" if active_batch_lifecycle <= 60_000.0 else "failed",
            bound=60_000.0,
            observed=active_batch_lifecycle,
            unit="milliseconds",
            digest=digest,
            reason="physical_batch_request_completed_with_competing_workload_present",
        ),
        dimension(
            "cancellation_release_latency_ms",
            state="met" if cancellation_release <= 1_000.0 else "failed",
            bound=1_000.0,
            observed=cancellation_release,
            unit="milliseconds",
            digest=digest,
            reason="queued_batch_cancelled_and_all_path_reservations_released",
        ),
        dimension(
            "runtime_batch_size",
            state="approved_exclusion",
            bound=4.0,
            observed=1.0,
            unit="requests",
            digest=digest,
            reason="sequential_dispatch_observed_no_microbatch_or_overlap_claim",
        ),
    ]
    states = {item["state"] for item in dimensions}
    overall = (
        "failed"
        if "failed" in states
        else "met_with_approved_exclusions"
        if "approved_exclusion" in states
        else "met"
    )
    return validate_performance_budget_v3(
        {
            "protocol": "mycelium.performance_budget.v3",
            "budget_id": "m16-concurrent-physical-v1",
            "profile_id": "mixed_interactive_batch_v1",
            "evidence_scope": "concurrent_physical_observed",
            "observed_request_count": len(requests),
            "dimensions": dimensions,
            "overall_state": overall,
        }
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate", type=Path, required=True)
    parser.add_argument("--m15-comparison", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    budget = build_budget(args.gate.resolve(), args.m15_comparison.resolve())
    atomic_json(args.output.resolve(), budget)
    print(json.dumps(budget, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
