#!/usr/bin/env python3
"""Run one A8 physical gate or emit an inert preflight envelope (spec §11-§12).

Exit codes: 0 success; 2 bounded failure (PhysicalGateError/PeerRequired);
1 unexpected failure. Nothing here fabricates a result: cases that need
infrastructure or a peer fail closed and never write evidence.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mycelium_internet.physical import (  # noqa: E402 - path bootstrap above
    A8_PHYSICAL_CASES,
    PhysicalGateError,
    build_adapter_from_bundle,
    execute_case,
    preflight_document,
    seal_qualification,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="a8_run_physical_gate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("--spec-digest", required=True)
    preflight.add_argument("--source-digest", required=True)
    preflight.add_argument("--now-unix-ms", type=int)

    run = subparsers.add_parser("run")
    run.add_argument("case_id", choices=sorted(A8_PHYSICAL_CASES))
    run.add_argument("--origin", required=True)
    run.add_argument("--evidence-root", type=Path)
    run.add_argument("--seal", action="store_true")
    run.add_argument("--spec-digest", default="sha256:" + "0" * 64)
    run.add_argument("--source-digest", default="sha256:" + "0" * 64)
    run.add_argument(
        "--bundle-file",
        type=Path,
        help="owner-delivered invite bundle for the seed-side adapter cases",
    )
    run.add_argument("--token-file", type=Path)
    run.add_argument(
        "--node-root",
        type=Path,
        help="owner-private node identity root (replay case)",
    )

    cases = subparsers.add_parser("cases")
    cases.add_argument("--json", action="store_true", dest="as_json")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "cases":
        listing = sorted(A8_PHYSICAL_CASES)
        if args.as_json:
            print(json.dumps(listing))
        else:
            print("\n".join(listing))
        return 0
    if args.command == "preflight":
        now_unix_ms = args.now_unix_ms
        if now_unix_ms is None:
            from time import time

            now_unix_ms = int(time() * 1000)
        document = preflight_document(
            now_unix_ms=now_unix_ms,
            spec_digest=args.spec_digest,
            source_digest=args.source_digest,
        )
        print(json.dumps(document, sort_keys=True))
        return 0
    try:
        adapter = None
        case_inputs: dict[str, Any] = {}
        if args.bundle_file is not None:
            if not args.bundle_file.is_file():
                raise PhysicalGateError("physical_infrastructure_unavailable")
            bundle = json.loads(args.bundle_file.read_text("utf-8"))
            token = (
                args.token_file.read_text("utf-8").strip()
                if args.token_file is not None
                else str(bundle["token"])
            )
            adapter = build_adapter_from_bundle(
                origin=args.origin,
                bundle=bundle,
                invite_token=token,
            )
        if args.case_id == "invalid_or_replayed_invitation":
            if (
                adapter is None
                or args.node_root is None
                or args.bundle_file is None
            ):
                raise PhysicalGateError("physical_infrastructure_unavailable")
            node_root: Path = args.node_root
            node_root.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.chmod(node_root, 0o700)
            from itertools import count

            from mycelium_node.identity import load_or_create_node_signer
            from mycelium_node.membership import NodeMembershipSession

            verified_payload = adapter._bundle_payload  # noqa: SLF001
            assert isinstance(verified_payload, dict)
            swarm_id = verified_payload["swarm_id"]
            seed_node_id = "a8-rehearsal-seed"
            counter = count(1)
            id_source = lambda: f"a8-gate-node-{next(counter)}"  # noqa: E731

            def _node() -> NodeMembershipSession:
                return NodeMembershipSession(
                    node_id="a8-gate-node",
                    swarm_id=swarm_id,
                    seed_node_id=seed_node_id,
                    signer=load_or_create_node_signer(
                        node_root / "node.key",
                        endpoint_id="a8-gate-node-endpoint",
                    ),
                    incarnation="a8-gate-node-1",
                    software_version="a8-gate-run",
                    peer_class="mac_mlx_iroh",
                    runtime_capability={
                        "runtime_backend": "mlx",
                        "transport": "iroh",
                        "activation_protocol": "mycelium.router_wire.v1",
                    },
                    clock=lambda: __import__("time").time(),
                    id_source=id_source,
                )

            first_node = _node()
            first_envelope = first_node.join_request(
                invite_nonce=verified_payload["nonce"],
                endpoint_addrs=["https://a8-gate-node-a.invalid/control"],
            )
            second_node = _node()
            second_envelope = second_node.join_request(
                invite_nonce=verified_payload["nonce"],
                endpoint_addrs=["https://a8-gate-node-b.invalid/control"],
            )
            case_inputs = {
                "first_join_envelope": first_envelope,
                "second_join_envelope": second_envelope,
                "second_adapter": build_adapter_from_bundle(
                    origin=args.origin,
                    bundle=bundle,
                    invite_token=token,
                ),
            }
        document = execute_case(
            args.case_id,
            origin=args.origin,
            evidence_root=args.evidence_root,
            adapter=adapter,
            case_inputs=case_inputs,
            spec_digest=args.spec_digest,
            source_digest=args.source_digest,
        )
    except PhysicalGateError as exc:
        print(f"gate rejected: {exc.code}", file=sys.stderr)
        return 2
    print(json.dumps(document, sort_keys=True))
    if args.seal:
        if args.evidence_root is None:
            print("gate rejected: evidence_root_unsafe", file=sys.stderr)
            return 2
        try:
            record = seal_qualification(
                document,
                evidence_root=args.evidence_root,
            )
        except PhysicalGateError as exc:
            print(f"gate rejected: {exc.code}", file=sys.stderr)
            return 2
        print(f"sealed: {record}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
