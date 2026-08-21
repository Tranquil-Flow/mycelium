#!/usr/bin/env python3
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
"""Execute the A4 idle-staleness physical gate against a live product serve.

During an idle qualified interval the participating node-2 physical
inference node is frozen with SIGSTOP (control-channel silence — the channel
the serve's idle-keepalive monitor probes). The detector must follow the
frozen keepalive -> suspect -> quarantine thresholds without fabricating
active-traffic failure: one missed receipt yields suspect only, quarantine
requires three consecutive misses and at least 15,000 ms stale, new affected
admissions fail closed, and the route never latches deployment-fatal state.
The node is resumed with SIGCONT afterwards, and the serve's recovery to
`recovered` is observed and recorded.

One stall maneuver emits two observations: the one-missed-receipt negative
(suspect-only snapshot) and the idle-staleness positive (full progression
plus fail-closed admission).

The outputs are bounded privacy-reduced observations.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import sys
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
)

DEFAULT_SSH_HOST = "astra@100.125.181.68"
DEFAULT_SSH_KEY = "/Users/evinova-self/.ssh/id_ed25519_mycelium_linux"
KEEPALIVE_MS = 5_000
SUSPECT_MISSES = 2
QUARANTINE_MISSES = 3
QUARANTINE_STALE_MS = 15_000

RESOLVE_STALL_NODES = (
    "import os, json\n"
    "out=[]\n"
    "for p in os.listdir('/proc'):\n"
    "    if not p.isdigit() or p==str(os.getpid()): continue\n"
    "    try: cmd=open('/proc/'+p+'/cmdline','rb').read().replace(bytes([0]),b' ').decode('utf-8','replace')\n"
    "    except Exception: continue\n"
    "    toks=cmd.split()\n"
    "    if toks and toks[0].endswith('python3') and 'physical_inference_node.py' in cmd and '--run-id' in cmd:\n"
    "        out.append((int(p), cmd))\n"
    "print(json.dumps(out))\n"
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


def _node2_subjects(subjects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        subject
        for subject in subjects
        if "node-2" in subject.get("subject_id", "")
    ]


def _resolve_targets(args: argparse.Namespace, explicit: list[int]) -> list[int]:
    if explicit:
        return explicit
    result = subprocess.run(
        _ssh_prefix(args) + ["python3 -c " + shlex.quote(RESOLVE_STALL_NODES)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    candidates = json.loads(result.stdout.strip() or "[]")
    targets = [
        pid
        for pid, cmd in candidates
        if "/home/astra/mycelium-a4-concurrency-node2" in cmd
    ]
    if not targets and candidates:
        targets = [candidates[0][0]]
    return targets


def _liveness_snapshot(base_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    status = public_json(base_url, "/__mycelium/live-status")
    liveness = status.get("liveness") or {}
    return status, liveness


def _observe_stall_progression(
    base_url: str,
    observation_window: float,
) -> tuple[
    float | None,
    dict[str, Any] | None,
    float | None,
    dict[str, Any] | None,
    bool,
    Any,
    set[str],
]:
    suspect_seen_at: float | None = None
    suspect_snapshot: dict[str, Any] | None = None
    quarantined_seen_at: float | None = None
    quarantine_snapshot: dict[str, Any] | None = None
    route_alive_throughout = True
    fatal_throughout: Any = None
    incident_sources: set[str] = set()

    deadline = time.monotonic() + observation_window
    while time.monotonic() < deadline:
        status, liveness = _liveness_snapshot(base_url)
        if status.get("route_alive") is not True:
            route_alive_throughout = False
        if status.get("fatal") is not None:
            fatal_throughout = status.get("fatal")
        for incident in liveness.get("incidents") or []:
            incident_sources.add(incident.get("source"))
        subjects = _node2_subjects(liveness.get("subjects") or [])
        for subject in subjects:
            if suspect_seen_at is None and subject.get("state") == "suspect":
                suspect_seen_at = time.monotonic()
                suspect_snapshot = {
                    key: subject.get(key)
                    for key in (
                        "subject_id",
                        "kind",
                        "state",
                        "consecutive_misses",
                        "last_fresh_ms",
                        "last_source",
                        "membership_generation",
                    )
                }
            if (
                quarantined_seen_at is None
                and subject.get("state") == "quarantined"
            ):
                quarantined_seen_at = time.monotonic()
                quarantine_snapshot = {
                    key: subject.get(key)
                    for key in (
                        "subject_id",
                        "kind",
                        "state",
                        "consecutive_misses",
                        "last_fresh_ms",
                        "last_source",
                        "membership_generation",
                    )
                }
                quarantine_snapshot["liveness_generated_at_monotonic_ms"] = (
                    liveness.get("generated_at_monotonic_ms")
                )
        if quarantined_seen_at is not None:
            break
        time.sleep(0.25)
    return (
        suspect_seen_at,
        suspect_snapshot,
        quarantined_seen_at,
        quarantine_snapshot,
        route_alive_throughout,
        fatal_throughout,
        incident_sources,
    )


def _observe_recovery(
    base_url: str,
    *,
    resumed_at: float,
    deadline: float,
) -> dict[str, Any]:
    """Poll until the stalled peer leaves quarantine or the deadline passes."""

    recovered_state: str | None = None
    recovered_at: float | None = None
    while time.monotonic() < deadline:
        _, liveness = _liveness_snapshot(base_url)
        subjects = _node2_subjects(liveness.get("subjects") or [])
        active = [
            subject
            for subject in subjects
            if subject.get("state") in {"suspect", "quarantined"}
        ]
        if not active and subjects:
            recovered_state = subjects[0].get("state")
            recovered_at = time.monotonic()
            break
        time.sleep(0.5)
    return {
        "recovered": recovered_state is not None,
        "subject_state": recovered_state,
        "recovery_ms": (
            (recovered_at - resumed_at) * 1_000.0
            if recovered_at is not None
            else None
        ),
    }


def run_gate(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    base_url = args.base_url
    status, liveness = _liveness_snapshot(base_url)
    subjects = _node2_subjects(liveness.get("subjects") or [])
    if (
        status.get("route_alive") is not True
        or not _zero_live_resources(
            public_json(base_url, "/__mycelium/runtime/admission-status")
        )
        or any(subject.get("consecutive_misses", 0) != 0 for subject in subjects)
    ):
        raise GateError("route_not_idle_clean_and_fresh")

    targets = _resolve_targets(args, args.node_pids or [])
    if not targets:
        raise GateError("no_live_node_target")
    stalled_at = time.monotonic()
    subprocess.run(
        _ssh_prefix(args) + ["kill -STOP " + " ".join(str(pid) for pid in targets)],
        capture_output=True,
        timeout=30,
    )
    try:
        suspect_seen_at, suspect_snapshot, quarantined_seen_at, quarantine_snapshot, route_alive_throughout, fatal_throughout, incident_sources = _observe_stall_progression(
            base_url, args.observation_window
        )
        if suspect_seen_at is None or suspect_snapshot is None:
            raise GateError("suspect_state_not_observed")
        if quarantined_seen_at is None or quarantine_snapshot is None:
            raise GateError("quarantine_state_not_observed")

        status, _ = _liveness_snapshot(base_url)
        # New affected admission must fail closed after quarantine.
        admission_rejected = False
        admission_terminal = None
        admission_outcome = None
        session = ProductSession(base_url)
        try:
            accepted = session.submit(label="idle-probe", maximum_new_tokens=4)
            admission_terminal = session.stream_summary(accepted).get("terminal")
            admission_outcome = f"accepted_terminal:{admission_terminal}"
        except GateError as exc:
            admission_rejected = True
            admission_outcome = str(exc)
    finally:
        resumed_at = time.monotonic()
        subprocess.run(
            _ssh_prefix(args) + ["kill -CONT " + " ".join(str(pid) for pid in targets)],
            capture_output=True,
            timeout=30,
        )
    recovery = _observe_recovery(
        base_url,
        resumed_at=resumed_at,
        deadline=resumed_at + 60.0,
    )

    suspect_misses = suspect_snapshot.get("consecutive_misses", 0)
    quarantine_misses = quarantine_snapshot.get("consecutive_misses", 0)
    stale_at_quarantine_ms: int | None = None
    generated_at = quarantine_snapshot.get("liveness_generated_at_monotonic_ms")
    fresh_at = quarantine_snapshot.get("last_fresh_ms")
    if isinstance(generated_at, int) and isinstance(fresh_at, int):
        stale_at_quarantine_ms = generated_at - fresh_at

    one_miss_report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_negative_one_missed_receipt.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "claim_boundary": "one missed receipt yields suspect only",
        "failure_surface": "participating_node_control_channel_frozen_during_idle",
        "node_pids_stalled": targets,
        "stall_signal": "SIGSTOP",
        "recovery_signal": "SIGCONT",
        "suspect_observed_ms_after_kill": (suspect_seen_at - stalled_at) * 1000.0,
        "suspect_snapshot": suspect_snapshot,
        "suspect_only_checks": {
            "state_was_suspect": suspect_snapshot.get("state") == "suspect",
            "suspect_misses_at_most_threshold": suspect_misses
            <= SUSPECT_MISSES,
            "not_yet_quarantined": True,
            "route_alive": route_alive_throughout,
            "no_deployment_fatal": fatal_throughout is None,
            "no_active_failure_fabrication": (
                "active_transport_failure" not in incident_sources
            ),
        },
        "keepalive_interval_ms": KEEPALIVE_MS,
        "suspect_threshold_misses": SUSPECT_MISSES,
    }
    one_miss_checks = dict(one_miss_report["suspect_only_checks"])
    one_miss_checks["simulated"] = one_miss_report["simulated"] is False
    one_miss_report["passed"] = all(one_miss_checks.values())
    one_miss_report["checks"] = one_miss_checks

    idle_report: dict[str, Any] = {
        "protocol": "mycelium.a4_product_positive_idle_staleness.v1",
        "qualification_claim": False,
        "promotion_authorized": False,
        "passed": False,
        "simulated": False,
        "claim_boundary": "idle keepalive loss follows suspect then quarantine thresholds",
        "failure_surface": "participating_node_control_channel_frozen_during_idle",
        "node_pids_stalled": targets,
        "stall_signal": "SIGSTOP",
        "recovery_signal": "SIGCONT",
        "progression": {
            "stall_to_suspect_ms": (suspect_seen_at - stalled_at) * 1000.0,
            "suspect_to_quarantine_ms": (
                quarantined_seen_at - suspect_seen_at
            )
            * 1000.0,
            "suspect_snapshot": suspect_snapshot,
            "quarantine_snapshot": quarantine_snapshot,
            "stale_at_quarantine_ms": stale_at_quarantine_ms,
        },
        "admission_after_quarantine": {
            "rejected": admission_rejected,
            "terminal": admission_terminal,
            "outcome": admission_outcome,
        },
        "recovery": recovery,
        "route_alive_throughout": route_alive_throughout,
        "deployment_fatal_reason": fatal_throughout,
        "observed_incident_sources": sorted(incident_sources),
    }
    idle_checks = {
        "suspect_before_quarantine": True,
        "quarantine_after_three_misses": quarantine_misses
        >= QUARANTINE_MISSES,
        "quarantine_after_stale_window": (
            stale_at_quarantine_ms is not None
            and stale_at_quarantine_ms >= QUARANTINE_STALE_MS
        ),
        "affected_admission_fails_closed": (
            admission_rejected
            or (admission_terminal is not None and admission_terminal != "completed")
        ),
        "no_active_failure_fabrication": (
            "active_transport_failure" not in incident_sources
        ),
        "route_alive": route_alive_throughout,
        "not_deployment_fatal": fatal_throughout is None,
        "not_simulated": idle_report["simulated"] is False,
    }
    idle_report["passed"] = all(idle_checks.values())
    idle_report["checks"] = idle_checks
    return one_miss_report, idle_report


def atomic_json(path: Path, document: dict[str, Any]) -> str:
    payload = json.dumps(document, indent=2, sort_keys=True, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="A4 idle-staleness physical gate against a live product serve."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8791")
    parser.add_argument("--output-one-miss", type=Path, required=True)
    parser.add_argument("--output-idle", type=Path, required=True)
    parser.add_argument("--observation-window", type=float, default=180.0)
    parser.add_argument(
        "--node-pids",
        type=lambda value: [int(item) for item in value.split(",") if item],
        default=None,
        help="explicit node-2 physical_inference_node pids; resolved via SSH when omitted",
    )
    parser.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    args = parser.parse_args(argv)
    one_miss, idle = run_gate(args)
    one_miss_digest = atomic_json(args.output_one_miss.resolve(), one_miss)
    idle_digest = atomic_json(args.output_idle.resolve(), idle)
    print(
        json.dumps(
            {
                "one_miss_output": str(args.output_one_miss),
                "one_miss_passed": one_miss["passed"],
                "one_miss_digest": one_miss_digest,
                "idle_output": str(args.output_idle),
                "idle_passed": idle["passed"],
                "idle_checks": idle["checks"],
                "idle_digest": idle_digest,
            },
            sort_keys=True,
        )
    )
    return 0 if (one_miss["passed"] and idle["passed"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
