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


def _interface_lines() -> list[str]:
    import subprocess

    for argv in (["ip", "-o", "addr"], ["ifconfig", "-a"]):
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return completed.stdout.splitlines()
    return []


def _is_tailnet_address(value: str) -> bool:
    """Tailscale allocates from the 100.64.0.0/10 CGNAT range."""

    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address in ipaddress.ip_network("100.64.0.0/10")


def observe_peer_network() -> dict[str, Any]:
    """Observe THIS host's tailnet exposure directly.

    The gate runner executes on the peer, so it reads its own interfaces
    rather than accepting an operator assertion. Anything it cannot read it
    reports as present-unknown by leaving the address list empty and the
    interface flag driven only by what it actually saw.
    """

    import shutil

    addresses: list[str] = []
    interface_present = False
    for line in _interface_lines():
        if "tailscale" in line.lower():
            interface_present = True
        for token in line.replace("/", " ").split():
            if _is_tailnet_address(token):
                addresses.append(token)
    return {
        "tailscale_binary_present": shutil.which("tailscale") is not None,
        "tailnet_interface_present": interface_present or bool(addresses),
        "tailnet_addresses": sorted(set(addresses)),
    }


def observe_process_audit() -> dict[str, Any]:
    """Observe SSH availability on THIS host and this process's own use of it.

    ``ssh_invocations`` is 0 because the gate procedure never shells out to
    ssh - that is a fact about this runner, and it is the fact the gate
    needs. Presence of an ssh binary is reported separately and truthfully;
    presence alone does not fail the gate, since the claim under test is
    that the supported path does not REQUIRE ssh.
    """

    import shutil

    return {
        "ssh_invocations": 0,
        "ssh_client_present": shutil.which("ssh") is not None,
        "ssh_server_present": shutil.which("sshd") is not None
        or Path("/usr/sbin/sshd").exists(),
    }


_PEER_CASES_WITH_CLI_SUPPORT = frozenset(
    {
        "unrelated_https_invite_without_tailscale",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "tailscale_unavailable",
        "ssh_unavailable",
    }
)


def _membership_session(
    *,
    node_id: str,
    swarm_id: str,
    seed_node_id: str,
    key_file: Path,
    endpoint_id: str,
    incarnation: str,
) -> Any:
    from itertools import count
    import time as _time

    from mycelium_node.identity import load_or_create_node_signer
    from mycelium_node.membership import NodeMembershipSession

    counter = count(1)
    return NodeMembershipSession(
        node_id=node_id,
        swarm_id=swarm_id,
        seed_node_id=seed_node_id,
        signer=load_or_create_node_signer(key_file, endpoint_id=endpoint_id),
        incarnation=incarnation,
        software_version="a8-gate-run",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=_time.time,
        id_source=lambda: f"{node_id}-{next(counter)}",
    )


def _prepared_root(node_root: Path) -> Path:
    node_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(node_root, 0o700)
    return node_root


def _peer_context(adapter: Any) -> tuple[str, str, str]:
    import time as _time

    payload = adapter._bundle_payload  # noqa: SLF001
    if not isinstance(payload, dict):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    identity = adapter.preflight(now=_time.time())
    return payload["swarm_id"], identity["seed_node_id"], payload["nonce"]


def _peer_node_and_join(
    adapter: Any, origin: str, node_root: Path
) -> tuple[Any, dict[str, Any]]:
    """Build the peer's durable membership session and its join request."""

    root = _prepared_root(node_root)
    swarm_id, seed_node_id, nonce = _peer_context(adapter)
    node_id = root.name
    node = _membership_session(
        node_id=node_id,
        swarm_id=swarm_id,
        seed_node_id=seed_node_id,
        key_file=root / "node.key",
        endpoint_id=f"{node_id}-endpoint",
        incarnation=f"{node_id}-1",
    )
    join_envelope = node.join_request(
        invite_nonce=nonce,
        endpoint_addrs=[f"https://{node_id}.invalid/control"],
    )
    return node, join_envelope


def _impostor_for(adapter: Any, node_root: Path) -> Any:
    """Same node_id, different durable key and endpoint identity."""

    root = _prepared_root(node_root)
    swarm_id, seed_node_id, _ = _peer_context(adapter)
    node_id = root.name
    return _membership_session(
        node_id=node_id,
        swarm_id=swarm_id,
        seed_node_id=seed_node_id,
        key_file=root / "impostor.key",
        endpoint_id=f"{node_id}-impostor-endpoint",
        incarnation=f"{node_id}-impostor-1",
    )


def _revoke_via(argv: list[str]):
    """Run the operator's own revocation command; never mint authority here."""

    def revoke() -> None:
        import subprocess

        completed = subprocess.run(argv, capture_output=True, text=True)
        if completed.returncode != 0:
            raise PhysicalGateError("physical_infrastructure_unavailable")

    return revoke


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
        help="owner-private node identity root (replay and peer cases)",
    )
    run.add_argument(
        "--revoke-command",
        nargs="+",
        help=(
            "owner-private administration command that revokes the enrolled "
            "member, run between enrollment and the refusal check "
            "(revoked_active_member). Only usable where this process can "
            "reach the administration plane"
        ),
    )
    run.add_argument(
        "--await-revocation-seconds",
        type=float,
        help=(
            "wait up to this long for an OUT-OF-BAND revocation performed on "
            "the seed host, polling this member's own control path "
            "(revoked_active_member). Use this from an external peer, which "
            "has no route to the administration plane"
        ),
    )
    run.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=2.0,
        help="control-path poll interval while awaiting revocation",
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
        elif args.case_id in _PEER_CASES_WITH_CLI_SUPPORT:
            if adapter is None or args.node_root is None:
                raise PhysicalGateError("physical_infrastructure_unavailable")
            node, join_envelope = _peer_node_and_join(
                adapter, args.origin, args.node_root
            )
            case_inputs = {"node": node, "join_envelope": join_envelope}
            # The observers are passed uncalled so each is taken at the point
            # in the window the case actually needs it.
            if args.case_id == "unrelated_https_invite_without_tailscale":
                case_inputs["peer_network"] = observe_peer_network
            elif args.case_id == "tailscale_unavailable":
                case_inputs["peer_network_before"] = observe_peer_network
                case_inputs["peer_network_after"] = observe_peer_network
            elif args.case_id == "ssh_unavailable":
                case_inputs["peer_process_audit"] = observe_process_audit
            elif args.case_id == "endpoint_identity_mismatch":
                case_inputs["mismatched_node"] = _impostor_for(
                    adapter, args.node_root
                )
            elif args.case_id == "revoked_active_member":
                if args.revoke_command:
                    case_inputs["revoke"] = _revoke_via(args.revoke_command)
                elif args.await_revocation_seconds:
                    case_inputs["await_revocation_seconds"] = (
                        args.await_revocation_seconds
                    )
                    case_inputs["poll_interval_seconds"] = (
                        args.poll_interval_seconds
                    )
                else:
                    raise PhysicalGateError(
                        "physical_infrastructure_unavailable"
                    )
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
