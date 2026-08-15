#!/usr/bin/env python3
"""Stage a physical multi-host route from a stored operator plan.

Runs the existing QualificationController `prepare` command, which transfers the
model shards and control documents to every peer's staging root. This exists so
staging is a durable, repeatable repository operation rather than an ad-hoc
command reconstructed from a handover document.

Usage:
    python3 scripts/stage_live_route.py --operator-plan /path/to/operator-plan.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

CONTROLLER = "physical_inference_qualification.py"


def _canonical_bytes(value: object) -> bytes:
    """Match the controller's canonical document encoding exactly.

    The controller rejects any document that does not round-trip to these exact
    bytes with `noncanonical_document`.
    """
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _peer_argument(peer: dict) -> str:
    """Encode a peer record in the controller's 7-field comma form."""
    return ",".join(
        (
            peer["node_id"],
            peer["ssh_target"],
            peer["host_id"],
            peer["boot_id"],
            peer["staging_root"],
            peer["process_transport"],
            peer["ssh_identity_file"] or "-",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument(
        "--work-dir",
        type=Path,
        help="where extracted controller documents are written "
        "(default: alongside the operator plan)",
    )
    parser.add_argument(
        "--python",
        default="/opt/homebrew/bin/python3.14",
        help="interpreter used to run the controller",
    )
    parser.add_argument(
        "--command",
        default="prepare",
        choices=("preflight", "prepare", "cleanup"),
        help="controller command to run (default: prepare)",
    )
    args = parser.parse_args(argv)

    plan = json.loads(args.operator_plan.read_text(encoding="utf-8"))
    controller = plan["controller"]

    work_dir = args.work_dir or args.operator_plan.parent / "controller-inputs"
    work_dir.mkdir(parents=True, exist_ok=True)

    transfer_manifest_path = work_dir / "transfer-manifest.json"
    membership_snapshot_path = work_dir / "membership-snapshot.json"
    transfer_manifest_path.write_bytes(
        _canonical_bytes(controller["transfer_manifest"])
    )
    membership_snapshot_path.write_bytes(
        _canonical_bytes(controller["membership_snapshot"])
    )

    # The stored membership offers carry valid_from/valid_until windows anchored
    # to the plan's own clock, so the plan's `now` is the only value that
    # validates. Regenerating offers with a live clock is separate work.
    command = [
        args.python,
        CONTROLLER,
        args.command,
        "--mode",
        controller["mode"],
        "--source-root",
        controller["source_root"],
        "--transfer-manifest",
        str(transfer_manifest_path),
        "--membership-snapshot",
        str(membership_snapshot_path),
        "--now",
        repr(controller["now"]),
        "--peers",
        *(_peer_argument(peer) for peer in controller["peers"]),
    ]
    node_transfer_manifests = controller.get("node_transfer_manifests")
    if node_transfer_manifests is not None:
        node_transfer_manifests_path = work_dir / "node-transfer-manifests.json"
        node_transfer_manifests_path.write_bytes(
            _canonical_bytes(node_transfer_manifests)
        )
        command.extend(
            ["--node-transfer-manifests", str(node_transfer_manifests_path)]
        )
    prepositioned_artifacts = controller.get("prepositioned_artifacts")
    if prepositioned_artifacts is not None:
        prepositioned_artifacts_path = work_dir / "prepositioned-artifacts.json"
        prepositioned_artifacts_path.write_bytes(
            _canonical_bytes(prepositioned_artifacts)
        )
        command.extend(
            ["--prepositioned-artifacts", str(prepositioned_artifacts_path)]
        )

    print(f"$ {' '.join(command)}", file=sys.stderr, flush=True)
    completed = subprocess.run(command, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
