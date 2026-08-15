#!/usr/bin/env python3
"""Create a new owner-only operator plan bound to current durable seed members."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_node.identity import load_node_signer  # noqa: E402
from mycelium_node.process import private_directory_lease  # noqa: E402
from mycelium_qualification.evidence import canonical_json_bytes  # noqa: E402
from mycelium_seed.plan_binding import (  # noqa: E402
    PlanBindingError,
    bind_operator_plan_document,
)
from mycelium_seed.state import SqliteSeedState  # noqa: E402
from scripts.build_qwen_live_route import refresh_peer_identities  # noqa: E402


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
IdentityRefresher = Callable[[list[dict[str, Any]], str], list[dict[str, Any]]]


def _rotate_run_session(
    plan: dict[str, Any],
    *,
    run_id: str,
    identity_refresher: IdentityRefresher = refresh_peer_identities,
) -> dict[str, Any]:
    if _RUN_ID.fullmatch(run_id) is None:
        raise PlanBindingError("operator_plan_run_id_invalid")
    controller = plan.get("controller")
    run_plan = controller.get("run_plan") if isinstance(controller, dict) else None
    peers = controller.get("peers") if isinstance(controller, dict) else None
    if (
        not isinstance(run_plan, dict)
        or not isinstance(peers, list)
        or not peers
        or not all(isinstance(peer, dict) for peer in peers)
        or not isinstance(plan.get("run_id"), str)
        or run_plan.get("run_id") != plan.get("run_id")
    ):
        raise PlanBindingError("operator_plan_run_session_invalid")
    rotated = copy.deepcopy(plan)
    rotated["run_id"] = run_id
    rotated["controller"]["run_plan"]["run_id"] = run_id
    refreshed = identity_refresher(copy.deepcopy(peers), run_id)
    if (
        not isinstance(refreshed, list)
        or len(refreshed) != len(peers)
        or any(not isinstance(peer, dict) for peer in refreshed)
        or [peer.get("node_id") for peer in refreshed]
        != [peer.get("node_id") for peer in peers]
    ):
        raise PlanBindingError("operator_plan_run_identity_invalid")
    rotated["controller"]["peers"] = refreshed
    return rotated


def _load_seed(root: Path):
    lease = private_directory_lease(root, create=False)
    try:
        lease.revalidate()
        signer = load_node_signer(lease.path / "identity" / "seed.key")
        database = lease.path / "state.sqlite3"
        metadata = database.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PlanBindingError("operator_plan_seed_state_invalid")
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise PlanBindingError("operator_plan_seed_state_invalid")
            rows = connection.execute("SELECT key,value FROM seed_metadata").fetchall()
            member_rows = connection.execute("SELECT * FROM seed_members ORDER BY node_id").fetchall()
        finally:
            connection.close()
        binding = {row["key"]: row["value"] for row in rows}
        if binding.get("seed_key_digest") != signer.verification_key_digest:
            raise PlanBindingError("operator_plan_seed_state_invalid")
        members = [SqliteSeedState._decode_member_row(row) for row in member_rows]
        lease.revalidate()
        return signer, binding["swarm_id"], binding["seed_node_id"], members
    finally:
        lease.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operator-plan", type=Path, required=True)
    parser.add_argument("--seed-state-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--rotate-run-id",
        help=(
            "fresh physical session identity; preserves model, representation, "
            "deployment, graph, and assignments"
        ),
    )
    args = parser.parse_args(argv)
    try:
        signer, swarm_id, seed_node_id, members = _load_seed(args.seed_state_root)
        plan = json.loads(args.operator_plan.read_text(encoding="utf-8"))
        bound = bind_operator_plan_document(
            plan,
            signer=signer,
            swarm_id=swarm_id,
            seed_node_id=seed_node_id,
            members=members,
            now=time.time(),
        )
        if args.rotate_run_id is not None:
            bound = _rotate_run_session(bound, run_id=args.rotate_run_id)
        body = canonical_json_bytes(bound) + b"\n"
        output = args.output.expanduser().resolve()
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({"status": "bound", "seed_key_digest": signer.verification_key_digest}, sort_keys=True))
        return 0
    except (OSError, KeyError, ValueError, PlanBindingError) as exc:
        print(getattr(exc, "code", "operator_plan_binding_failed"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
