#!/usr/bin/env python3
"""Run the A8 peer rehearsal through the live public origin (spec §4-§5).

This exercises the complete pin-first adapter flow over the REAL publicly
trusted HTTPS origin: preflight, join, heartbeat, resume, and the
privacy-clean bootstrap status. Everything printed is bounded and labelled
REHEARSAL - a same-LAN or origin-side run is never gate evidence and can
never be sealed (spec §12).

Usage:
  a8_run_rehearsal_peer.py --origin https://<public-origin> \
      --bundle-file <bundle.json> --token-file <token.txt> \
      [--node-root ~/.mycelium/a8-tls/rehearsal-peer]
"""

from __future__ import annotations

import argparse
import json
from itertools import count
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_invite import verify_invite_bundle  # noqa: E402
from mycelium_node.identity import load_or_create_node_signer  # noqa: E402
from mycelium_node.membership import NodeMembershipSession  # noqa: E402
from mycelium_seed.http import SeedHTTPClient  # noqa: E402
from mycelium_internet.bootstrap import (  # noqa: E402
    PublicBootstrapPolicy,
    canonical_https_origin,
)
from mycelium_internet.enrollment import EnrollmentError, PublicBootstrapClient  # noqa: E402

REHEARSAL_LABEL = "a8-rehearsal-peer"
PEER_INCARNATION = "a8-rehearsal-peer-1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a8_run_rehearsal_peer")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--bundle-file", required=True, type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument(
        "--node-root",
        type=Path,
        default=Path.home() / ".mycelium" / "a8-tls" / "rehearsal-peer",
    )
    args = parser.parse_args(argv)
    origin = canonical_https_origin(args.origin)
    args.node_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(args.node_root, 0o700)
    node_id = args.node_root.name
    endpoint_id = f"{node_id}-endpoint"
    counter = count(1)
    id_source = lambda: f"{node_id}-{next(counter)}"  # noqa: E731

    bundle = json.loads(args.bundle_file.read_text("utf-8"))
    invite_token = bundle["token"]
    now = time.time()
    verified = verify_invite_bundle(bundle, now=now)
    assert verified["payload"]["seed_url"] == origin, "bundle seed_url mismatch"

    client = SeedHTTPClient(
        seed_url=origin,
        swarm_id=verified["payload"]["swarm_id"],
        seed_key_digest=verified["seed_key_digest"],
        seed_key_records=list(verified["seed_key_records"]),
        timeout=15.0,
    )
    policy = PublicBootstrapPolicy(canonical_origin=origin)
    adapter = PublicBootstrapClient.from_seed_client(
        client,
        policy=policy,
        tls_state="publicly_trusted",
        bundle=bundle,
        invite_token=invite_token,
        clock=time.time,
        backoff_seconds=1.0,
    )

    print(f"{REHEARSAL_LABEL}: preflight over {origin}")
    preflight = adapter.preflight(now=time.time())
    print(
        f"{REHEARSAL_LABEL}: pin verified protocol={preflight['protocol']} "
        f"seed_node={preflight['seed_node_id']}"
    )

    node = NodeMembershipSession(
        node_id=node_id,
        swarm_id=verified["payload"]["swarm_id"],
        seed_node_id=preflight["seed_node_id"],
        signer=load_or_create_node_signer(
            args.node_root / "node.key",
            endpoint_id=endpoint_id,
        ),
        incarnation=PEER_INCARNATION,
        software_version="a8-rehearsal",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        id_source=id_source,
    )
    join_request = node.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=[f"https://{node_id}.invalid/control"],
    )
    print(f"{REHEARSAL_LABEL}: join attempt")
    acceptance = adapter.join(join_request, now=time.time())
    node.accept_join(acceptance, seed_key_digest=verified["seed_key_digest"])
    message = acceptance["message"]
    print(
        f"{REHEARSAL_LABEL}: joined generation={message['membership_generation']} "
        f"lease={message['lease_expires_at']}"
    )

    heartbeat = node.heartbeat(lifecycle_state="RUNNING", active_requests=0)
    assert heartbeat is not None
    renewal = adapter.heartbeat(heartbeat, now=time.time())
    renewal_message = renewal.get("message") or {}
    lease = renewal_message.get("lease_expires_at", "unknown")
    print(f"{REHEARSAL_LABEL}: heartbeat renewal ok lease={lease}")

    # Simulate a process restart: a fresh session bound to the same durable
    # key claims the previous generation and incarnation.
    restarted = NodeMembershipSession(
        node_id=node_id,
        swarm_id=verified["payload"]["swarm_id"],
        seed_node_id=preflight["seed_node_id"],
        signer=load_or_create_node_signer(
            args.node_root / "node.key",
            endpoint_id=endpoint_id,
        ),
        incarnation="a8-rehearsal-peer-2",
        software_version="a8-rehearsal",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        id_source=id_source,
    )
    resume_request = restarted.resume_request(
        previous_generation=message["membership_generation"],
        previous_incarnation=message["accepted_incarnation"],
        endpoint_addrs=[f"https://{node_id}.invalid/control"],
    )
    resume = adapter.resume(resume_request, now=time.time())
    print(
        f"{REHEARSAL_LABEL}: resume generation="
        f"{resume['message']['membership_generation']}"
    )

    status = adapter.bootstrap_status(now=time.time())
    print(f"{REHEARSAL_LABEL}: bootstrap status")
    print(json.dumps(status, sort_keys=True))
    print(f"{REHEARSAL_LABEL}: done (rehearsal, not evidence)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except EnrollmentError as exc:
        print(f"{REHEARSAL_LABEL}: rejected: {exc.code}", file=sys.stderr)
        raise SystemExit(2)
    except (ValueError, FileNotFoundError) as exc:
        print(f"{REHEARSAL_LABEL}: invalid input: {exc}", file=sys.stderr)
        raise SystemExit(2)
