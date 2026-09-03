#!/usr/bin/env python3
"""Execute the A5 replica-loss negative gate against a live product serve.

Kills the node-3-r2 data-plane sidecar while one ordinary product request is
admitted on the replica track, then proves (spec §5, §10):

(a) an admitted request on the lost placement terminates explicitly WITHOUT
    migration (terminal is cancelled/failed, never completed-on-another-track);
(b) an unaffected admitted request completes;
(c) the A4 liveness quarantine marks the replica placement lost in live-status
    (replica_loss_placement_ids), so the affected track stops being selectable
    and new admission lands on the incumbent track only — the surviving track
    remains usable at reduced capacity;
(d) zero live resources remain afterwards.

The data-plane sidecar is killed through the ordinary SSH path used to
operate node-3-r2 (NOT the membership daemon). The output is a bounded
privacy-reduced observation (mycelium.a5_replica_loss_negative_observation.v1):
no prompt, decoded text, token IDs, cookies, CSRF values, hostnames,
addresses, paths, command lines, or exception strings.
"""

from __future__ import annotations

import argparse
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

from scripts.run_a5_product_gate import (  # noqa: E402
    GateError,
    ProductSession,
    _request_map,
    _zero_live_resources,
    atomic_json,
    public_json,
    wait_for,
)

DEFAULT_SSH_HOST = "mycelium-laptop"
QUARANTINE_TIMEOUT_SECONDS = 120.0


def _ssh(args: argparse.Namespace, remote: str, *, timeout: float = 30.0) -> str:
    command = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]
    if args.ssh_host != DEFAULT_SSH_HOST:
        command += ["-i", args.ssh_key]
    command += [args.ssh_host, remote]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GateError("node3_ssh_unavailable") from error
    if result.returncode != 0:
        raise GateError(f"node3_ssh_failed_{result.returncode}")
    return result.stdout.strip()


def _resolve_sidecar_pid(args: argparse.Namespace) -> int:
    socket_path = shlex.quote(str(args.sidecar_socket_path))
    output = _ssh(args, f"lsof -t {socket_path}")
    pids = [int(item) for item in output.split() if item.isdigit()]
    if len(pids) != 1:
        raise GateError("replica_sidecar_pid_ambiguous_or_missing")
    return pids[0]


def _kill_sidecar(args: argparse.Namespace, pid: int) -> None:
    _ssh(args, f"kill {pid}")


def _replica_placement_id(status: dict[str, Any]) -> str:
    qualifications = status.get("replica_track_qualification") or []
    placement_ids = sorted(
        {
            document["placement_id"]
            for document in qualifications
            if isinstance(document.get("placement_id"), str)
        }
    )
    if not placement_ids:
        raise GateError("replica_placement_identity_missing")
    return placement_ids[0]


def _loss_observed(status: dict[str, Any], replica_placement_id: str) -> bool:
    return replica_placement_id in (status.get("replica_loss_placement_ids") or [])


def run_gate(base_url: str, args: argparse.Namespace) -> dict[str, Any]:
    session = ProductSession(base_url)
    before = public_json(base_url, "/__mycelium/live-status")
    if before.get("route_alive") is not True:
        raise GateError("route_not_alive")
    if not before.get("replica_track_qualification"):
        raise GateError("replica_track_not_qualified")
    if before.get("replica_loss_placement_ids"):
        raise GateError("replica_loss_present_before_gate")
    replica_placement_id = _replica_placement_id(before)

    before_runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    if not _zero_live_resources(before_runtime):
        raise GateError("route_not_clean_and_alive")

    # Two concurrent requests: rotation [incumbent, replica] puts request
    # "two" on the replica track; request "one" is the unaffected incumbent.
    accepted = (
        session.submit(label="one", maximum_new_tokens=args.maximum_new_tokens),
        session.submit(label="two", maximum_new_tokens=args.maximum_new_tokens),
    )
    request_ids = {item["request_id"] for item in accepted}
    summaries: dict[int, dict[str, Any]] = {}

    def stream(index: int) -> None:
        summaries[index] = session.stream_summary(accepted[index])

    threads = tuple(
        threading.Thread(target=stream, args=(index,), daemon=True)
        for index in range(2)
    )
    for thread in threads:
        thread.start()

    overlap = wait_for(
        lambda: (
            status
            if request_ids
            <= set(
                (status := public_json(
                    base_url, "/__mycelium/runtime/admission-status"
                ))["queue"]["active_request_ids"]
            )
            else None
        ),
        timeout=30.0,
    )
    placements_by_request = {
        item["request_id"]: tuple(
            _request_map(overlap)[item["request_id"]]["placement_ids"]
        )
        for item in accepted
    }
    replica_request = next(
        item
        for item in accepted
        if replica_placement_id in placements_by_request[item["request_id"]]
    )
    incumbent_request = next(
        item for item in accepted if item["request_id"] != replica_request["request_id"]
    )

    # Kill the node-3-r2 data sidecar while the replica request is in flight.
    sidecar_pid = _resolve_sidecar_pid(args)
    kill_started = time.monotonic()
    _kill_sidecar(args, sidecar_pid)

    for thread in threads:
        thread.join(timeout=240.0)
        if thread.is_alive():
            raise GateError("stream_worker_join_timeout")

    replica_summary = summaries[
        0 if accepted[0]["request_id"] == replica_request["request_id"] else 1
    ]
    incumbent_summary = summaries[
        0 if accepted[0]["request_id"] == incumbent_request["request_id"] else 1
    ]
    if replica_summary["terminal"] not in {"cancelled", "failed"}:
        raise GateError("lost_placement_request_did_not_terminate_explicitly")
    if incumbent_summary["terminal"] != "completed":
        raise GateError("unaffected_request_terminal_invalid")

    # Wait for the A4 liveness quarantine -> A5 loss marking (3 misses +
    # stale bound), then confirm the affected track is no longer selectable.
    wait_for(
        lambda: (
            status
            if _loss_observed(
                status := public_json(base_url, "/__mycelium/live-status"),
                replica_placement_id,
            )
            else None
        ),
        timeout=QUARANTINE_TIMEOUT_SECONDS,
    )

    # New admission lands on the incumbent only: surviving track usable.
    follow_up = session.submit(
        label="three", maximum_new_tokens=args.maximum_new_tokens
    )
    follow_summary = session.stream_summary(follow_up)
    if follow_summary["terminal"] != "completed":
        raise GateError("surviving_track_terminal_invalid")
    follow_placements = wait_for(
        lambda: (
            record
            if (
                record := _request_map(
                    public_json(base_url, "/__mycelium/runtime/admission-status")
                ).get(follow_up["request_id"])
            )
            is not None
            and record["terminal_state"] is not None
            else None
        ),
        timeout=30.0,
    )
    if replica_placement_id in follow_placements["placement_ids"]:
        raise GateError("new_admission_used_lost_placement")

    after_runtime = wait_for(
        lambda: (
            status
            if _zero_live_resources(
                status := public_json(
                    base_url, "/__mycelium/runtime/admission-status"
                )
            )
            else None
        ),
        timeout=30.0,
    )
    after = public_json(base_url, "/__mycelium/live-status")

    return {
        "protocol": "mycelium.a5_replica_loss_negative_observation.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "deployment_id": after_runtime["deployment_id"],
        "topology_generation": after_runtime["topology_version"],
        "replica_placement_id": replica_placement_id,
        "request_ids": sorted(request_ids),
        "tracks_at_overlap": {
            request_id: list(placements)
            for request_id, placements in placements_by_request.items()
        },
        "sidecar_pid": sidecar_pid,
        "kill_to_terminal_ms": (
            replica_summary["terminal_at_monotonic_s"] - kill_started
        )
        * 1_000.0,
        "streams": {
            item["request_id"]: {
                key: value
                for key, value in summaries[index].items()
                if key != "terminal_at_monotonic_s"
            }
            for index, item in enumerate(accepted)
        },
        "follow_up": {
            "request_id": follow_up["request_id"],
            "terminal": follow_summary["terminal"],
            "placement_ids": list(follow_placements["placement_ids"]),
        },
        "loss_observed": _loss_observed(after, replica_placement_id),
        "final_loss_set": after.get("replica_loss_placement_ids"),
        "final_queue": after_runtime["queue"],
        "final_placement_reservations": {
            item["placement_id"]: item["active_reservations"]
            for item in after_runtime["placements"]
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--maximum-new-tokens", type=int, default=64)
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--sidecar-socket-path", required=True)
    parser.add_argument(
        "--ssh-key",
        default="/Users/evinova-self/.ssh/id_ed25519_m4pro_to_laptop",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.maximum_new_tokens <= 128:
        raise SystemExit("--maximum-new-tokens must be in [2, 128]")
    report = run_gate(args.base_url, args)
    atomic_json(args.output.resolve(), report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
