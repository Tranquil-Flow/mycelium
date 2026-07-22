from __future__ import annotations

from contextlib import ExitStack
from itertools import count
from pathlib import Path
import sys

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import (
    NodeMembershipSession,
    PhysicalNodeProcess,
    build_physical_node_command,
    load_or_create_node_signer,
)
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPServer
from tests.e2e_request_iroh.conftest import (
    native_iroh_sidecar_binary as local_control_sidecar_binary,
)


NOW = 4_000.0


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def test_seed_and_two_real_node_processes_join_over_tcp(
    tmp_path: Path,
    local_control_sidecar_binary: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    coordinator = SeedCoordinator(
        swarm_id="swarm-local-two-node",
        seed_node_id="seed-node",
        seed_url=None,
        signer=generate_ed25519_signer(endpoint_id="seed-endpoint"),
        invite_registry=SqliteInviteRegistry(tmp_path / "seed" / "invites.sqlite3"),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=_ids("seed-message"),
    )

    with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as server, ExitStack() as stack:
        for ordinal, node_id in enumerate(("node-local-a", "node-local-b"), start=1):
            run_id = f"run-local-{ordinal}"
            deployment_id = f"deployment-local-{ordinal}"
            artifact_root = tmp_path / node_id / "artifacts"
            socket_root = tmp_path / node_id / "sockets"
            artifact_root.mkdir(parents=True)
            socket_root.mkdir()
            command = build_physical_node_command(
                python_executable=Path(sys.executable),
                service_script=root / "physical_inference_node.py",
                run_id=run_id,
                deployment_id=deployment_id,
                node_id=node_id,
                artifact_root=artifact_root,
                socket_root=socket_root,
                sidecar_binary=local_control_sidecar_binary,
                sidecar_local_only=True,
            )
            process = stack.enter_context(
                PhysicalNodeProcess(
                    command=command,
                    node_id=node_id,
                    run_id=run_id,
                    deployment_id=deployment_id,
                )
            )
            hello = process.command("hello")
            assert hello["node_id"] == node_id
            assert hello["state"] == "NEW"
            assert hello["route_ready"] is False

            bundle = coordinator.mint_invite(
                nonce=f"invite-local-{ordinal}",
                ttl_seconds=120,
            )
            verified = verify_invite_bundle(bundle, now=NOW)
            client = SeedHTTPClient.from_invite_bundle(bundle, now=NOW, timeout=2)
            session = NodeMembershipSession(
                node_id=node_id,
                swarm_id="swarm-local-two-node",
                seed_node_id="seed-node",
                signer=load_or_create_node_signer(
                    tmp_path / node_id / "identity" / "node.key"
                ),
                incarnation=f"incarnation-{ordinal}",
                software_version="mycelium-local-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
                clock=lambda: NOW,
                id_source=_ids(f"{node_id}-message"),
            )
            join_request = session.join_request(
                invite_nonce=verified["payload"]["nonce"],
                endpoint_addrs=[f"http://127.0.0.1/{node_id}"],
            )
            acceptance = client.join(
                invite_token=bundle["token"],
                join_envelope=join_request,
            )
            session.accept_join(
                acceptance,
                seed_key_digest=verified["seed_key_digest"],
            )
            client.send_member_message(
                session.capability_report(
                    platform="macOS-15",
                    architecture="arm64",
                    memory_bytes=8 * 1024**3,
                    available_storage_bytes=40 * 1024**3,
                    backends=["native-iroh"],
                    precisions=["float16"],
                ),
                now=NOW,
            )
            client.send_member_message(
                session.heartbeat(lifecycle_state="NEW", active_requests=0),
                now=NOW,
            )

        first = coordinator.member("node-local-a")
        second = coordinator.member("node-local-b")
        assert first["generation"] == second["generation"] == 1
        assert first["verification_key_digest"] != second["verification_key_digest"]
        assert first["last_heartbeat_sequence"] == 1
        assert second["last_heartbeat_sequence"] == 1
