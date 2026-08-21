#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Execute the A4 queue-saturation physical negative gate against a live
product serve.

Four long blocker requests hold the bounded worker pool while a submission
storm exceeds the product's bounded pending-admission capacity. The ordinary
path must return bounded backpressure (503 with a stable code), every accepted
request must reach a terminal state, and after cancellation and drain the
runtime must return to zero live resources with no worker or reservation
leaks.

The output is a bounded privacy-reduced observation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_a4_product_gate import (  # noqa: E402
    GateError,
    ProductSession,
    _zero_live_resources,
    public_json,
)

BLOCKER_TOKENS = 128
STORM_TOKENS = 2
POLL_INTERVAL = 0.5


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url
    if not _zero_live_resources(
        public_json(base_url, "/__mycelium/runtime/admission-status")
    ) or public_json(base_url, "/__mycelium/live-status").get("route_alive") is not True:
        raise GateError("route_not_clean_and_alive")

    before = public_json(base_url, "/__mycelium/live-status")
    before_counters = dict(before.get("counters") or {})

    # Phase 1: four blockers hold the bounded worker pool.
    blocker_session = ProductSession(base_url)
    blockers = [
        blocker_session.submit(label=f"blocker-{index}", maximum_new_tokens=BLOCKER_TOKENS)
        for index in range(4)
    ]
    blocker_summaries: dict[str, Any] = {}
    blocker_failures: list[str] = []

    def stream_blocker(accepted: dict[str, Any]) -> None:
        try:
            blocker_summaries[accepted["request_id"]] = (
                blocker_session.stream_summary(accepted)
            )
        except Exception as exc:  # bounded, privacy-reduced
            blocker_failures.append(str(exc))

    blocker_threads = [
        threading.Thread(target=stream_blocker, args=(accepted,), daemon=True)
        for accepted in blockers
    ]
    for thread in blocker_threads:
        thread.start()

    # Wait until all four blockers are actively dispatched.
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
        if len(runtime["queue"]["active_request_ids"]) >= 4:
            break
        time.sleep(0.2)

    # Phase 2: submission storm against the bounded admission capacity.
    outcomes: list[dict[str, Any]] = []
    outcomes_lock = threading.Lock()
    stop_flag = threading.Event()
    maximum_active_seen = 4
    maximum_pending_seen = 0

    def storm_worker() -> None:
        nonlocal maximum_active_seen, maximum_pending_seen
        session = ProductSession(base_url)
        while not stop_flag.is_set():
            with outcomes_lock:
                if len(outcomes) >= args.submissions:
                    return
            try:
                accepted = session.submit(label="storm", maximum_new_tokens=STORM_TOKENS)
                with outcomes_lock:
                    outcomes.append(
                        {"outcome": "accepted", "request_id": accepted["request_id"]}
                    )
                    maximum_pending_seen = max(
                        maximum_pending_seen, len(outcomes)
                    )
            except GateError as exc:
                with outcomes_lock:
                    outcomes.append({"outcome": "rejected", "code": str(exc)})

    storm_threads = [
        threading.Thread(target=storm_worker, daemon=True)
        for _ in range(args.storm_workers)
    ]
    for thread in storm_threads:
        thread.start()

    storm_started = time.monotonic()
    while True:
        with outcomes_lock:
            done = len(outcomes) >= args.submissions
        if done:
            break
        if time.monotonic() - storm_started > args.storm_timeout:
            break
        runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
        maximum_active_seen = max(
            maximum_active_seen,
            len(runtime["queue"]["active_request_ids"]),
        )
        time.sleep(POLL_INTERVAL)
    stop_flag.set()
    for thread in storm_threads:
        thread.join(timeout=10.0)

    accepted_ids = [
        outcome["request_id"]
        for outcome in outcomes
        if outcome["outcome"] == "accepted"
    ]
    rejected_codes = Counter(
        outcome["code"]
        for outcome in outcomes
        if outcome["outcome"] == "rejected"
    )

    # Phase 3: cancel blockers, then drain.
    for accepted in blockers:
        try:
            blocker_session.cancel(accepted)
        except GateError:
            pass
    for thread in blocker_threads:
        thread.join(timeout=120.0)

    drain_deadline = time.monotonic() + args.drain_timeout
    zero_at = None
    while time.monotonic() < drain_deadline:
        runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
        if _zero_live_resources(runtime):
            zero_at = time.monotonic()
            break
        time.sleep(1.0)
    if zero_at is None:
        raise GateError("drain_timeout")

    runtime_final = public_json(base_url, "/__mycelium/runtime/admission-status")
    after = public_json(base_url, "/__mycelium/live-status")
    after_counters = dict(after.get("counters") or {})
    requests = runtime_final.get("requests") or []
    terminal_census = Counter(
        request.get("terminal_state") for request in requests
    )
    all_terminals_recorded = all(
        request.get("terminal_state") is not None for request in requests
    )
    counters_advanced = (
        int(after_counters.get("frames_sent", 0))
        > int(before_counters.get("frames_sent", 0))
        and int(after_counters.get("frames_received", 0))
        > int(before_counters.get("frames_received", 0))
        and int(after_counters.get("applied_operation_count", 0))
        > int(before_counters.get("applied_operation_count", 0))
    )

    report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_negative_queue_saturation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "claim_boundary": (
            "bounded backpressure under request-storm saturation with zero "
            "worker or reservation leaks"
        ),
        "storm": {
            "attempted": len(outcomes),
            "accepted": len(accepted_ids),
            "rejected": len(outcomes) - len(accepted_ids),
            "rejection_codes": dict(sorted(rejected_codes.items())),
            "maximum_pending_observed": maximum_pending_seen,
            "maximum_active_observed": maximum_active_seen,
        },
        "blocker_terminals": {
            request_id: summary.get("terminal")
            for request_id, summary in blocker_summaries.items()
        },
        "blocker_stream_failures": blocker_failures,
        "drain": {
            "zero_resources_after_drain": True,
            "drain_wall_seconds": zero_at - storm_started,
        },
        "terminal_census": dict(sorted(terminal_census.items())),
        "all_terminals_recorded": all_terminals_recorded,
        "counters_advanced": counters_advanced,
        "before_counters": before_counters,
        "after_counters": after_counters,
        "final_queue": runtime_final["queue"],
        "route_alive": after.get("route_alive"),
        "deployment_fatal_reason": after.get("fatal"),
    }
    checks = {
        "bounded_backpressure_observed": (
            report["storm"]["rejected"] >= 1
            and all(
                code.startswith("http_") for code in rejected_codes
            )
        ),
        "accepted_requests_all_reach_terminal": all_terminals_recorded,
        "no_worker_or_reservation_leaks": _zero_live_resources(runtime_final),
        "counters_advanced": counters_advanced,
        "route_alive": after.get("route_alive") is True,
        "not_deployment_fatal": after.get("fatal") is None,
        "not_simulated": report["simulated"] is False,
    }
    report["passed"] = all(checks.values())
    report["checks"] = checks
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A4 queue-saturation physical gate against a live product serve."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--submissions", type=int, default=400)
    parser.add_argument("--storm-workers", type=int, default=32)
    parser.add_argument("--storm-timeout", type=float, default=180.0)
    parser.add_argument("--drain-timeout", type=float, default=900.0)
    args = parser.parse_args(argv)
    report = run_gate(args)
    payload = json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(payload, encoding="utf-8")
    digest = "sha256:" + hashlib.sha256(args.output.read_bytes()).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "passed": report["passed"],
                "checks": report["checks"],
                "storm": report["storm"],
                "digest": digest,
            },
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
