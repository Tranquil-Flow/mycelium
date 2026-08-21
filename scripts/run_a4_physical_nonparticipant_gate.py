#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Execute the A4 non-participant physical gate against a live product serve.

The non-participating current swarm member (default node-3; pass
--node3-node-id for the enrolled member id) whose physical sidecar lives in
the workspace named by NODE3_MARKER on a distinct host that is not on the
selected route is killed while the selected route serves an ordinary product
request. That request and a subsequent admission on the unaffected route must
complete with positive physical counters, and nothing may latch
deployment-fatal state.

The output is a bounded privacy-reduced observation. The node-3 sidecar is
restarted by the operator after the gate to leave the fleet as found.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
import threading
import time
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
    wait_for,
)

DEFAULT_NODE3_HOST = "evinova@100.126.111.123"
DEFAULT_NODE3_KEY = "/Users/evinova-self/.ssh/id_ed25519_m4pro_to_laptop"
NODE3_MARKER = "mycelium-a4-concurrency-node3-v6"


def _node3_prefix(args: argparse.Namespace) -> list[str]:
    return [
        "ssh",
        "-i",
        args.node3_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        args.node3_host,
    ]


def _node3_pids(args: argparse.Namespace) -> list[int]:
    result = subprocess.run(
        _node3_prefix(args)
        + [
            "pgrep -f "
            + shlex.quote(NODE3_MARKER)
            + " || true"
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [int(pid) for pid in result.stdout.split() if pid.strip().isdigit()]


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url
    if not _zero_live_resources(
        public_json(base_url, "/__mycelium/runtime/admission-status")
    ) or public_json(base_url, "/__mycelium/live-status").get("route_alive") is not True:
        raise GateError("route_not_clean_and_alive")

    before = public_json(base_url, "/__mycelium/live-status")
    before_counters = dict(before.get("counters") or {})
    pids_before = _node3_pids(args)
    if not pids_before:
        raise GateError("node3_processes_missing")

    session = ProductSession(base_url)
    accepted_a = session.submit(
        label="nonparticipant-a", maximum_new_tokens=args.maximum_new_tokens
    )
    summaries: dict[str, Any] = {}

    def stream_a() -> None:
        try:
            summaries["a"] = session.stream_summary(accepted_a)
        except Exception as exc:  # bounded, privacy-reduced
            summaries["a_error"] = str(exc)

    thread_a = threading.Thread(target=stream_a, daemon=True)
    thread_a.start()

    wait_for(
        lambda: (
            True
            if accepted_a["request_id"]
            in public_json(base_url, "/__mycelium/runtime/admission-status")[
                "queue"
            ]["active_request_ids"]
            else None
        ),
        timeout=30.0,
    )
    kill_started = time.monotonic()
    killed_pids = _node3_pids(args)
    subprocess.run(
        _node3_prefix(args)
        + ["kill -9 " + " ".join(str(pid) for pid in killed_pids) + " 2>/dev/null || true"],
        capture_output=True,
        timeout=30,
    )
    kill_completed = time.monotonic()

    thread_a.join(timeout=240.0)
    if thread_a.is_alive():
        raise GateError("stream_worker_join_timeout")
    summary_a = summaries.get("a") or {}
    if summary_a.get("terminal") != "completed":
        raise GateError("request_a_terminal_invalid")

    # Subsequent admission on the unaffected route.
    accepted_b = session.submit(
        label="nonparticipant-b", maximum_new_tokens=args.maximum_new_tokens
    )
    summary_b = session.stream_summary(accepted_b)
    if summary_b.get("terminal") != "completed":
        raise GateError("request_b_terminal_invalid")

    after = public_json(base_url, "/__mycelium/live-status")
    after_counters = dict(after.get("counters") or {})
    runtime_after = public_json(base_url, "/__mycelium/runtime/admission-status")
    new_incidents = [
        incident
        for incident in (after.get("incidents") or [])
        if incident.get("request_id")
        in {accepted_a["request_id"], accepted_b["request_id"]}
    ]

    counters_advanced = (
        int(after_counters.get("frames_sent", 0))
        > int(before_counters.get("frames_sent", 0))
        and int(after_counters.get("frames_received", 0))
        > int(before_counters.get("frames_received", 0))
        and int(after_counters.get("applied_operation_count", 0))
        > int(before_counters.get("applied_operation_count", 0))
    )

    report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_positive_nonparticipant_peer_exit.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "claim_boundary": (
            "non-participating swarm member exit leaves the selected route usable"
        ),
        "nonparticipating_node_id": args.node3_node_id,
        "node3_processes_before": pids_before,
        "node3_processes_killed": killed_pids,
        "kill_duration_ms": (kill_completed - kill_started) * 1000.0,
        "request_a": {
            "request_id": accepted_a["request_id"],
            "terminal": summary_a.get("terminal"),
            "publisher_generation": summary_a.get("publisher_generation"),
            "event_counts": summary_a.get("event_counts"),
        },
        "request_b": {
            "request_id": accepted_b["request_id"],
            "terminal": summary_b.get("terminal"),
            "publisher_generation": summary_b.get("publisher_generation"),
            "event_counts": summary_b.get("event_counts"),
        },
        "counters_advanced": counters_advanced,
        "before_counters": before_counters,
        "after_counters": after_counters,
        "request_incidents_after_exit": [
            {
                key: incident.get(key)
                for key in ("request_id", "reason", "state")
            }
            for incident in new_incidents
        ],
        "route_alive": after.get("route_alive"),
        "deployment_fatal_reason": after.get("fatal"),
        "zero_live_resources_after": _zero_live_resources(runtime_after),
        "node3_restarted_by_operator": False,
    }
    checks = {
        "node3_killed": bool(killed_pids),
        "request_during_exit_completed": summary_a.get("terminal") == "completed",
        "subsequent_admission_completed": summary_b.get("terminal") == "completed",
        "positive_physical_counters": counters_advanced,
        "route_alive": after.get("route_alive") is True,
        "not_deployment_fatal": after.get("fatal") is None,
        "no_leaked_resources": _zero_live_resources(runtime_after),
        "not_simulated": report["simulated"] is False,
    }
    report["passed"] = all(checks.values())
    report["checks"] = checks
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A4 non-participant physical gate against a live product serve."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-new-tokens", type=int, default=32)
    parser.add_argument("--node3-host", default=DEFAULT_NODE3_HOST)
    parser.add_argument("--node3-key", default=DEFAULT_NODE3_KEY)
    parser.add_argument(
        "--node3-node-id",
        default="node-3",
        help=(
            "seed member id of the non-participating member whose sidecar is "
            "killed; carried verbatim into the report so the evidence names "
            "the member that actually exited"
        ),
    )
    args = parser.parse_args(argv)
    if not 2 <= args.maximum_new_tokens <= 128:
        raise SystemExit("--maximum-new-tokens must be in [2, 128]")
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
                "digest": digest,
            },
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
