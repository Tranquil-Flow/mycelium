#!/usr/bin/env python3
"""Run the A8 rehearsal seed behind a public origin (spec §3 wire-up).

The rehearsal seed is a staging artifact: loopback-bound, owner-private
state, fresh identity per run, and explicitly labelled REHEARSAL. It never
claims qualification and never shares state or ports with the m14 physical
seed (which listens on 8876). The public origin is supplied by the operator
(quick tunnel or Cloudflare tunnel) and pinned identically into the
boundary policy and the coordinator.

Usage:
  a8_run_rehearsal_seed.py --origin https://<public-origin> [--port 18876]
      [--state-root ~/.mycelium/a8-tls/rehearsal-seed] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
from itertools import count
import os
from pathlib import Path
import signal
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_invite import SqliteInviteRegistry  # noqa: E402
from mycelium_node.identity import (  # noqa: E402
    load_node_signer,
    load_or_create_node_signer,
)
from mycelium_seed import SeedCoordinator, SeedCoordinatorError  # noqa: E402
from mycelium_seed.http import SeedHTTPServer  # noqa: E402
from mycelium_internet.bootstrap import (  # noqa: E402
    PublicBootstrapPolicy,
    canonical_https_origin,
)

DEFAULT_LOOPBACK_PORT = 18876
DEFAULT_STATE_ROOT = Path.home() / ".mycelium" / "a8-tls" / "rehearsal-seed"
REHEARSAL_LABEL = "a8-rehearsal"
SWARM_ID = "a8-rehearsal-swarm"
SEED_NODE_ID = "a8-rehearsal-seed"
INCARNATION = "a8-rehearsal-1"
SEED_ENDPOINT_ID = "a8-rehearsal-seed-endpoint"


def _token_digest(token: str) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _max_emitted_message_counter(state_root: Path) -> int:
    """Highest numeric suffix among the seed's emitted message ids.

    ``seed_emitted_messages`` persists in the state database, so a restart
    that began again at 1 would reuse ids and fail joins with
    ``seed_message_id_reused``.
    """

    import sqlite3

    db = state_root / "state.sqlite3"
    if not db.exists():
        return 0
    maximum = 0
    try:
        connection = sqlite3.connect(db)
        try:
            rows = connection.execute(
                "SELECT message_id FROM seed_emitted_messages"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return 0
    for (message_id,) in rows:
        suffix = str(message_id).rsplit("-", 1)[-1]
        if suffix.isdigit():
            maximum = max(maximum, int(suffix))
    return maximum


def _durable_seed_id_source(state_root: Path):
    counter = count(_max_emitted_message_counter(state_root) + 1)
    return lambda: f"{SEED_NODE_ID}-{next(counter)}"


def build_rehearsal_seed(
    *,
    origin: str,
    state_root: Path,
    port: int,
) -> tuple[SeedHTTPServer, SeedCoordinator, PublicBootstrapPolicy]:
    canonical_origin = canonical_https_origin(origin)
    state_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(state_root, 0o700)
    identity_root = state_root / "identity"
    identity_root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(identity_root, 0o700)
    signer = load_or_create_node_signer(
        identity_root / "seed.key",
        endpoint_id=SEED_ENDPOINT_ID,
    )
    policy = PublicBootstrapPolicy(canonical_origin=canonical_origin)
    invite_registry = SqliteInviteRegistry(state_root / "state.sqlite3")
    coordinator = SeedCoordinator(
        swarm_id=SWARM_ID,
        seed_node_id=SEED_NODE_ID,
        seed_url=canonical_origin,
        signer=signer,
        invite_registry=invite_registry,
        incarnation=INCARNATION,
        id_source=_durable_seed_id_source(state_root),
    )
    server = SeedHTTPServer(
        coordinator,
        host="127.0.0.1",
        port=port,
        public_seed_url=canonical_origin,
        policy=policy,
    )
    return server, coordinator, policy


def load_rehearsal_signer(state_root: Path):
    identity_root = state_root / "identity"
    return load_node_signer(
        identity_root / "seed.key",
        endpoint_id=SEED_ENDPOINT_ID,
    )


def mint_rehearsal_invite(
    *,
    origin: str,
    state_root: Path,
    nonce: str,
) -> tuple[dict, Path]:
    canonical_origin = canonical_https_origin(origin)
    signer = load_rehearsal_signer(state_root)
    invite_registry = SqliteInviteRegistry(state_root / "state.sqlite3")
    coordinator = SeedCoordinator(
        swarm_id=SWARM_ID,
        seed_node_id=SEED_NODE_ID,
        seed_url=canonical_origin,
        signer=signer,
        invite_registry=invite_registry,
        incarnation=INCARNATION,
        id_source=_ids("a8-rehearsal-seed"),
    )
    bundle = coordinator.mint_invite(nonce=nonce, ttl_seconds=3600)
    bundle_path = state_root / f"invite-{nonce}.json"
    bundle_path.write_text(json.dumps(bundle, sort_keys=True), encoding="utf-8")
    bundle_path.chmod(0o600)
    return bundle, bundle_path


def _probe_unqualified_member(
    coordinator: SeedCoordinator,
    node_id: str,
) -> dict[str, object]:
    """Exercise every serve capability through seed assignment admission.

    Artifact disclosure, placement, activation, deployment selection, and
    prompt delivery all require a signed assignment offer. The method issues
    one real offer attempt for each capability label. An activation-ineligible
    member must fail at the shared production choke point before assignment
    persistence, making all downstream side effects unreachable.
    """

    matching = [m for m in coordinator.members() if m["node_id"] == node_id]
    if not matching:
        raise RuntimeError("seed_member_unknown")
    member = matching[0]
    attempts: dict[str, str] = {}
    accepted_ids: set[str] = set()
    for authority in ("artifact", "placement", "activation", "selection", "inference"):
        assignment_id = f"a8-unqualified-{authority}"
        try:
            coordinator.assignment_offer(
                node_id=node_id,
                deployment_id="a8-unqualified-probe",
                deployment_epoch=1,
                assignment_id=assignment_id,
                assignment_digest="sha256:" + "1" * 64,
                stage_pack_digest="sha256:" + "2" * 64,
                graph_digest="sha256:" + "3" * 64,
                load_generation=1,
                peer_node_ids=[],
                placement_provenance="owner_unqualified_probe",
            )
        except SeedCoordinatorError as exc:
            if exc.code != "seed_member_activation_ineligible":
                raise
            attempts[authority] = "rejected"
        else:
            attempts[authority] = "accepted"
            accepted_ids.add(assignment_id)

    return {
        "protocol": "mycelium.unqualified_member_authority_probe.v1",
        "member_id": node_id,
        "member_visible": True,
        "activation_eligible": bool(member["activation_eligible"]),
        "authority_attempts": attempts,
        "forbidden_side_effects": {
            "artifact_disclosed": "a8-unqualified-artifact" in accepted_ids,
            "placement_created": "a8-unqualified-placement" in accepted_ids,
            "deployment_selected": "a8-unqualified-selection" in accepted_ids,
        },
        "prompt_deliveries": int("a8-unqualified-inference" in accepted_ids),
    }


def _owner_console(coordinator, stop: threading.Event) -> None:
    """Read owner administration commands from stdin while the seed serves."""

    for line in sys.stdin:
        if stop.is_set():
            return
        parts = line.split()
        if not parts:
            continue
        command, args = parts[0], parts[1:]
        try:
            if command == "members":
                for member in coordinator.members():
                    print(
                        f"{REHEARSAL_LABEL}: member {member['node_id']} "
                        f"generation={member['generation']} "
                        f"lifecycle={member['lifecycle_state']} "
                        f"activation_eligible={member['activation_eligible']}"
                    )
            elif command == "revoke" and args:
                node_id = args[0]
                matching = [
                    m for m in coordinator.members() if m["node_id"] == node_id
                ]
                if not matching:
                    print(f"{REHEARSAL_LABEL}: no such member {node_id}")
                    continue
                member = matching[0]
                coordinator.advance_member_generation(
                    node_id=node_id,
                    expected_generation=int(member["generation"]),
                    lifecycle_state="STOPPED",
                )
                print(f"{REHEARSAL_LABEL}: revoked {node_id}", flush=True)
            elif command == "probe-unqualified" and args:
                node_id = args[0]
                report = _probe_unqualified_member(coordinator, node_id)
                print(
                    f"{REHEARSAL_LABEL}: probe-unqualified {node_id} "
                    + json.dumps(report, sort_keys=True, separators=(",", ":")),
                    flush=True,
                )
            else:
                print(
                    f"{REHEARSAL_LABEL}: commands are 'members', 'revoke <id>', "
                    "'probe-unqualified <id>'",
                    flush=True,
                )
        except Exception as exc:  # noqa: BLE001 - owner console must not die
            print(f"{REHEARSAL_LABEL}: command failed: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a8_run_rehearsal_seed")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_LOOPBACK_PORT)
    parser.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--mint-invite", metavar="NONCE")
    args = parser.parse_args(argv)
    if args.mint_invite:
        bundle, bundle_path = mint_rehearsal_invite(
            origin=args.origin,
            state_root=args.state_root,
            nonce=args.mint_invite,
        )
        print(f"{REHEARSAL_LABEL}: minted invite {args.mint_invite}")
        print(f"bundle: {bundle_path}")
        print(f"token_sha256: {_token_digest(bundle['token'])}")
        return 0
    if args.dry_run:
        print(
            f"dry-run: would serve {REHEARSAL_LABEL} on "
            f"127.0.0.1:{args.port} for {args.origin} "
            f"with state {args.state_root}"
        )
        return 0
    server, coordinator, policy = build_rehearsal_seed(
        origin=args.origin,
        state_root=args.state_root,
        port=args.port,
    )
    print(
        f"{REHEARSAL_LABEL}: seed={SEED_NODE_ID} serving "
        f"{server.base_url} (loopback) for public origin {policy.canonical_origin} "
        f"state={args.state_root}"
    )
    print(f"{REHEARSAL_LABEL}: ready")
    print(
        f"{REHEARSAL_LABEL}: owner console on stdin - "
        "'members' lists, 'revoke <node_id>' fences one member, "
        "'probe-unqualified <node_id>' exercises serve admission"
    )
    stop = threading.Event()

    def _stop(_signum: int, _frame: object) -> None:
        stop.set()
        server.close()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    server.start()
    # Owner-private administration. This is stdin on the seed host - it has no
    # network surface at all, and no admin route is added to the public
    # allowlist. Members live in this process's memory, so revocation has to
    # happen here rather than from a separate CLI against the same state file.
    threading.Thread(
        target=_owner_console,
        args=(coordinator, stop),
        daemon=True,
    ).start()
    try:
        while not stop.is_set():
            stop.wait(0.5)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.close()
    print(f"{REHEARSAL_LABEL}: stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
