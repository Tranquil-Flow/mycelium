#!/usr/bin/env python3
"""Drive one host-local Iroh sidecar for a bounded M14 matrix exercise."""
# ruff: noqa: E402 -- direct execution bootstraps the repository import root.
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_iroh_sidecar import SidecarClient


def _secret(path: Path) -> bytes:
    value = path.read_bytes()
    if len(value) != 32:
        raise RuntimeError("m14_probe_bootstrap_secret_invalid")
    return value


def _client(args: argparse.Namespace) -> SidecarClient:
    client = SidecarClient(
        args.socket,
        _secret(args.bootstrap_secret_path),
        timeout=20.0,
    )
    client.connect()
    return client


def _configure(args: argparse.Namespace) -> dict[str, object]:
    peers = json.loads(args.peers.read_text("utf-8"))
    if not isinstance(peers, list):
        raise RuntimeError("m14_probe_peers_invalid")
    client = _client(args)
    try:
        client.configure_peers(peers, timeout=20.0)
        client.ping()
        return {"endpoint_id": client.endpoint_id, "configured_peers": len(peers)}
    finally:
        client.close()


def _receive(args: argparse.Namespace) -> dict[str, object]:
    client = _client(args)
    received: dict[str, int] = {}
    try:
        for _ in range(args.count):
            message_id, endpoint_id, _generation, _frame = client.recv_with_source(
                timeout=args.timeout
            )
            if endpoint_id is None:
                raise RuntimeError("m14_probe_source_missing")
            received[endpoint_id] = received.get(endpoint_id, 0) + 1
            client.ack(message_id)
        return {"endpoint_id": client.endpoint_id, "received": received}
    finally:
        client.close()


def _send(args: argparse.Namespace) -> dict[str, object]:
    frame = args.frame.read_bytes()
    client = _client(args)
    try:
        for _ in range(args.count):
            client.send_routed(
                args.peer_endpoint_id,
                frame,
                os.urandom(16),
                expected_generation=args.generation,
                source_generation=args.generation,
            )
        deadline = time.monotonic() + args.timeout
        selected = None
        while time.monotonic() < deadline:
            for observation in client.transport_observations():
                if (
                    observation.get("remote_endpoint_id") == args.peer_endpoint_id
                    and observation.get("frames_sent", 0) >= args.count
                ):
                    selected = observation
                    break
            if selected is not None:
                break
            time.sleep(0.05)
        if selected is None:
            raise RuntimeError("m14_probe_observation_timeout")
        return {"endpoint_id": client.endpoint_id, "observation": selected}
    finally:
        client.close()


def _observe(args: argparse.Namespace) -> dict[str, object]:
    client = _client(args)
    try:
        return {
            "endpoint_id": client.endpoint_id,
            "observations": client.transport_observations(),
        }
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--bootstrap-secret-path", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    configure = subparsers.add_parser("configure")
    configure.add_argument("--peers", type=Path, required=True)
    configure.set_defaults(run=_configure)
    receive = subparsers.add_parser("receive")
    receive.add_argument("--count", type=int, required=True)
    receive.add_argument("--timeout", type=float, default=60.0)
    receive.set_defaults(run=_receive)
    send = subparsers.add_parser("send")
    send.add_argument("--peer-endpoint-id", required=True)
    send.add_argument("--frame", type=Path, required=True)
    send.add_argument("--count", type=int, default=4)
    send.add_argument("--generation", type=int, default=1)
    send.add_argument("--timeout", type=float, default=30.0)
    send.set_defaults(run=_send)
    observe = subparsers.add_parser("observe")
    observe.set_defaults(run=_observe)
    args = parser.parse_args()
    if getattr(args, "count", 1) < 1:
        raise SystemExit("count must be positive")
    encoded = json.dumps(args.run(args), sort_keys=True) + "\n"
    if args.output is None:
        print(encoded, end="")
    else:
        args.output.write_text(encoded, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
