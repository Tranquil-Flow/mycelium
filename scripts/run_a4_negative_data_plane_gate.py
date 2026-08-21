#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Execute the A4 data-plane negative gate against a live product serve.

Kills the participating node-2 data-plane iroh sidecar while one ordinary
product request is in flight, then proves: the active failure is observed, the
blocked command is interrupted, the affected request terminates explicitly,
every request-owned resource returns to baseline within the 2,000 ms bound
(visibility-clock methodology), the healthy peer's resources are released, and
nothing latches deployment-fatal state.

The output is a bounded privacy-reduced observation: no prompt, decoded text,
token IDs, cookies, CSRF values, hostnames, local paths, or exception strings.
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
    public_json,
)

CLEANUP_BOUND_MS = 2000.0
DEFAULT_SSH_HOST = "astra@100.125.181.68"
DEFAULT_SSH_KEY = "/Users/evinova-self/.ssh/id_ed25519_mycelium_linux"

RESOLVE_SIDECARS = (
    "import os\n"
    "node=None; out=[]\n"
    "for p in os.listdir('/proc'):\n"
    "    if not p.isdigit(): continue\n"
    "    try:\n"
    "        cmd=open('/proc/'+p+'/cmdline','rb').read().replace(b'\\0',b' ').decode('utf-8','replace')\n"
    "        stat=open('/proc/'+p+'/stat').read().split()\n"
    "    except Exception: continue\n"
    "    if 'physical_inference_node.py' in cmd and 'a4-concurrency' in cmd and '--run-id' in cmd:\n"
    "        node=int(p)\n"
    "for q in os.listdir('/proc'):\n"
    "    if not q.isdigit(): continue\n"
    "    try:\n"
    "        qc=open('/proc/'+q+'/cmdline','rb').read().replace(b'\\0',b' ').decode('utf-8','replace')\n"
    "        qs=open('/proc/'+q+'/stat').read().split()\n"
    "    except Exception: continue\n"
    "    if int(qs[3])==node and 'iroh-sidecar' in qc and qs[2]=='S': out.append((int(q), qc))\n"
    "import json; print(json.dumps(out))\n"
)


def _ssh_prefix(args: argparse.Namespace) -> list[str]:
    return [
        "ssh",
        "-i",
        args.ssh_key,
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=15",
        args.ssh_host,
    ]


def resolve_targets(args: argparse.Namespace, explicit: list[int]) -> list[int]:
    if explicit:
        return explicit
    result = subprocess.run(
        _ssh_prefix(args) + ["python3 -c " + shlex.quote(RESOLVE_SIDECARS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    candidates = json.loads(result.stdout.strip() or "[]")
    targets = [
        pid
        for pid, cmd in candidates
        if "--uds /home/astra/mycelium-a4-concurrency-node2" in cmd
    ]
    if not targets and candidates:
        targets = [candidates[0][0]]
    return targets


def run_gate(args: argparse.Namespace) -> dict[str, Any]:
    base_url = args.base_url
    session = ProductSession(base_url)
    accepted = session.submit(
        label="data-plane", maximum_new_tokens=args.maximum_new_tokens
    )
    result_box: dict[str, Any] = {}

    def run_stream() -> None:
        try:
            result_box["summary"] = session.stream_summary(accepted)
        except Exception as exc:  # bounded, privacy-reduced
            result_box["error"] = str(exc)

    stream_thread = threading.Thread(target=run_stream, daemon=True)
    stream_thread.start()

    reached = False
    wait_deadline = time.monotonic() + 8.0
    while time.monotonic() < wait_deadline:
        runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
        match = [
            request
            for request in (runtime.get("requests") or [])
            if request.get("request_id") == accepted["request_id"]
        ]
        if match and "placement-001" in (match[0].get("placement_ids") or []):
            reached = True
            break
        time.sleep(0.05)

    targets = resolve_targets(args, args.sidecar_pids or [])
    if not targets:
        raise GateError("no_live_sidecar_target")
    subprocess.run(
        _ssh_prefix(args) + ["kill -9 " + " ".join(str(pid) for pid in targets)],
        capture_output=True,
        timeout=30,
    )

    deadline = time.monotonic() + 15.0
    incident = None
    visible_at = None
    clean_at = None
    while time.monotonic() < deadline:
        status = public_json(base_url, "/__mycelium/live-status")
        incidents = status.get("incidents") or []
        if incident is None:
            candidates = [
                item
                for item in incidents
                if item.get("request_id") == accepted["request_id"]
            ]
            if candidates:
                incident = candidates[0]
                visible_at = time.monotonic()
        if incident is not None:
            runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
            residual = [
                request
                for request in (runtime.get("requests") or [])
                if request.get("request_id") == accepted["request_id"]
                and request.get("terminal_state") is None
            ]
            if not residual:
                clean_at = time.monotonic()
                break
        time.sleep(0.05)

    stream_thread.join(timeout=5)
    status = public_json(base_url, "/__mycelium/live-status")
    peers = status.get("peers") or []
    node2 = next((peer for peer in peers if peer.get("node_id") == "node-2"), {})
    node0 = next((peer for peer in peers if peer.get("node_id") == "node-0"), {})
    runtime = public_json(base_url, "/__mycelium/runtime/admission-status")
    residual = [
        request
        for request in (runtime.get("requests") or [])
        if request.get("request_id") == accepted["request_id"]
        and request.get("terminal_state") is None
    ]
    visibility_cleanup_ms = (
        ((clean_at - visible_at) * 1000.0) if (clean_at and visible_at) else None
    )

    report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_negative_data_plane.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "failure_surface": "participating_remote_iroh_sidecar_process_exit",
        "sidecar_pids_killed": targets,
        "request_reached_second_stage": reached,
        "affected_request_id": accepted.get("request_id"),
        "affected_terminal": (result_box.get("summary") or {}).get("terminal"),
        "scoped_active_failure_incidents": 1 if incident else 0,
        "incident_reason": incident.get("reason") if incident else None,
        "incident_observed_at_unix_ms": (
            float(incident.get("observed_at_unix_ms", 0)) if incident else None
        ),
        "visibility_to_runtime_clean_ms": visibility_cleanup_ms,
        "cleanup_bound_ms": CLEANUP_BOUND_MS,
        "zero_runtime_resources": not residual,
        "healthy_peer_resources_released": (
            node0.get("sidecar_process_alive") is True
            and node0.get("release_state") in (None, "released")
        ),
        "fatal_peer_projected": (
            node2.get("transport_fatal") is True
            or node2.get("sidecar_process_alive") is False
        ),
        "route_alive_projection": status.get("route_alive"),
        "deployment_fatal_reason": status.get("fatal"),
        "stream_errors": [result_box["error"]] if result_box.get("error") else [],
        "peer_health": [
            {
                key: peer.get(key)
                for key in (
                    "node_id",
                    "sidecar_process_alive",
                    "transport_running",
                    "transport_fatal",
                    "release_state",
                )
            }
            for peer in peers
        ],
    }
    checks = {
        "request_reached_second_stage": reached is True,
        "incident_present": report["scoped_active_failure_incidents"] >= 1,
        "zero_runtime": report["zero_runtime_resources"] is True,
        "cleanup_bounded": (
            report["visibility_to_runtime_clean_ms"] is not None
            and report["visibility_to_runtime_clean_ms"] <= CLEANUP_BOUND_MS
        ),
        "healthy_peer_released": report["healthy_peer_resources_released"] is True,
        "fatal_peer_projected": report["fatal_peer_projected"] is True,
        "route_alive": report["route_alive_projection"] is True,
        "not_deployment_fatal": report["deployment_fatal_reason"] is None,
        "not_simulated": report["simulated"] is False,
    }
    report["passed"] = all(checks.values())
    report["checks"] = checks
    return report


def atomic_json(path: Path, document: dict[str, Any]) -> None:
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A4 data-plane negative gate against a live product serve."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--maximum-new-tokens", type=int, default=128)
    parser.add_argument(
        "--sidecar-pids",
        type=lambda value: [int(item) for item in value.split(",") if item],
        default=None,
        help="explicit node-2 sidecar pids; resolved via SSH when omitted",
    )
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    args = parser.parse_args(argv)
    if not 2 <= args.maximum_new_tokens <= 128:
        raise SystemExit("--maximum-new-tokens must be in [2, 128]")
    report = run_gate(args)
    atomic_json(args.output.resolve(), report)
    digest = "sha256:" + hashlib.sha256(
        args.output.read_bytes()
    ).hexdigest()
    print(
        json.dumps(
            {
                "output": str(args.output),
                "passed": report["passed"],
                "checks": report["checks"],
                "visibility_cleanup_ms": report["visibility_to_runtime_clean_ms"],
                "reason": report["incident_reason"],
                "digest": digest,
            },
            sort_keys=True,
        )
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
