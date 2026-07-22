from __future__ import annotations

from dataclasses import asdict
import hashlib
from itertools import count
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any
import uuid

import mlx.core as mx
import pytest

from mycelium_invite import SqliteInviteRegistry, verify_invite_bundle
from mycelium_node import NodeMembershipSession, load_or_create_node_signer
from mycelium_qualification.authority import QualificationAuthority
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_qualification.physical_deployment import (
    build_execution_graph,
    build_physical_device_states,
    prepare_physical_deployment,
)
from mycelium_qualification.qualifier import QualificationError
from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_seed import SeedCoordinator
from mycelium_seed.http import SeedHTTPClient, SeedHTTPError, SeedHTTPServer
from runtime_loader import execute_loaded_stage, load_assignment_stage
from tests.e2e_request_iroh.conftest import (
    native_iroh_sidecar_binary as local_control_sidecar_binary,  # noqa: F401
)
from tests.qualification.conftest import make_case, synthetic_signature_verifier
from tests.physical_qualification.test_node_service import (
    SIDECAR_BINARY,
    _NodeClient,
)


NOW = 7_000.0
NODE_IDS = ("node-local-a", "node-local-b")
MAC_RUNTIME_CAPABILITY = {
    "runtime_backend": "mlx",
    "transport": "iroh",
    "activation_protocol": "mycelium.router_wire.v1",
}


def _ids(prefix: str):
    values = count(1)
    return lambda: f"{prefix}-{next(values)}"


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _coordinator(
    database: Path,
    *,
    signer: Any,
    id_prefix: str,
) -> SeedCoordinator:
    return SeedCoordinator(
        swarm_id="swarm-local-e2e",
        seed_node_id="seed-node",
        seed_url=None,
        signer=signer,
        invite_registry=SqliteInviteRegistry(database),
        incarnation="seed-incarnation",
        clock=lambda: NOW,
        id_source=_ids(id_prefix),
    )


def _join_node(
    *,
    coordinator: SeedCoordinator,
    node_id: str,
    ordinal: int,
    endpoint_addr: dict[str, Any],
) -> tuple[NodeMembershipSession, SeedHTTPClient, dict[str, Any]]:
    bundle = coordinator.mint_invite(
        nonce=f"invite-local-e2e-{ordinal}",
        ttl_seconds=120,
    )
    verified = verify_invite_bundle(bundle, now=NOW)
    client = SeedHTTPClient.from_invite_bundle(bundle, now=NOW, timeout=2)
    session = NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-local-e2e",
        seed_node_id="seed-node",
        signer=generate_ed25519_signer(endpoint_id=endpoint_addr["id"]),
        incarnation=f"incarnation-{ordinal}",
        software_version="mycelium-local-e2e",
        peer_class="mac_mlx_iroh",
        runtime_capability=MAC_RUNTIME_CAPABILITY,
        clock=lambda: NOW,
        id_source=_ids(f"{node_id}-message"),
    )
    request = session.join_request(
        invite_nonce=verified["payload"]["nonce"],
        endpoint_addrs=[canonical_json_bytes(endpoint_addr).decode("utf-8")],
    )
    acceptance = client.join(invite_token=bundle["token"], join_envelope=request)
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
            backends=["mlx", "native-iroh"],
            precisions=["float16"],
        ),
        now=NOW,
    )
    client.send_member_message(
        session.heartbeat(lifecycle_state="NEW", active_requests=0),
        now=NOW,
    )
    return session, client, bundle


def _inference_request(request_id: str) -> dict[str, Any]:
    return {
        "request": {
            "request_id": request_id,
            "prompt_token_ids": [1, 2, 3],
            "max_new_tokens": 3,
            "expected_new_tokens": 3,
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 37,
            "generation_config_digest": "sha256:" + "e" * 64,
        }
    }


def test_seed_two_memberships_assign_and_run_native_iroh_inference(
    tmp_path: Path,
    local_control_sidecar_binary: Path,  # noqa: F811
) -> None:
    assert local_control_sidecar_binary == SIDECAR_BINARY
    assert local_control_sidecar_binary.is_file()
    deployment = prepare_physical_deployment(
        tmp_path / "deployment",
        node_ids=NODE_IDS,
    )
    loaded = [
        load_assignment_stage(assignment, report, load_generation=7)
        for assignment, report in zip(
            deployment.assignments,
            deployment.artifact_reports,
            strict=True,
        )
    ]
    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    graph_document = json.loads(json.dumps(asdict(graph)))
    state_document = json.loads(
        json.dumps(
            {
                node_id: asdict(state)
                for node_id, state in build_physical_device_states(graph).items()
            }
        )
    )
    (tmp_path / "model-manifest.json").write_bytes(
        canonical_json_bytes(deployment.manifest)
    )
    for node_id, assignment, pack in zip(
        NODE_IDS,
        deployment.assignments,
        deployment.stage_packs,
        strict=True,
    ):
        (tmp_path / f"{node_id}-assignment.json").write_bytes(
            canonical_json_bytes(assignment)
        )
        (tmp_path / f"{node_id}-stage-pack.json").write_bytes(
            canonical_json_bytes(pack)
        )

    database = tmp_path / "seed" / "state.sqlite3"
    seed_signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(database, signer=seed_signer, id_prefix="seed-message")
    sessions: dict[str, NodeMembershipSession] = {}
    clients: dict[str, SeedHTTPClient] = {}
    invite_bundles: dict[str, dict[str, Any]] = {}
    node_processes: dict[str, _NodeClient] = {}
    configured: dict[str, dict[str, Any]] = {}
    accepted_offers: dict[str, dict[str, Any]] = {}
    socket_root = Path(tempfile.mkdtemp(prefix="myc-seed-e2e-", dir="/tmp"))
    run_id = str(uuid.uuid4())
    seed_port: int | None = None

    try:
        for node_id, suffix in zip(NODE_IDS, ("a", "b"), strict=True):
            node_processes[node_id] = _NodeClient(
                node_id=node_id,
                run_id=run_id,
                deployment_id=graph.deployment_id,
                artifact_root=tmp_path,
                socket_root=socket_root / suffix,
            )
        for node_id, process in node_processes.items():
            result = process.command(
                "configure",
                {
                    "assignment_file": f"{node_id}-assignment.json",
                    "manifest_file": "model-manifest.json",
                    "stage_pack_file": f"{node_id}-stage-pack.json",
                    "graph": graph_document,
                    "device_states": state_document,
                    "load_generation": 7,
                },
            )
            assert result["observation"]["event"] == "configured"
            configured[node_id] = result["observation"]["details"]

        with SeedHTTPServer(coordinator, host="127.0.0.1", port=0) as seed_server:
            seed_port = int(seed_server.base_url.rsplit(":", 1)[1])
            for ordinal, node_id in enumerate(NODE_IDS, start=1):
                session, client, bundle = _join_node(
                    coordinator=coordinator,
                    node_id=node_id,
                    ordinal=ordinal,
                    endpoint_addr=configured[node_id]["endpoint_addr"],
                )
                sessions[node_id] = session
                clients[node_id] = client
                invite_bundles[node_id] = bundle
                member = coordinator.member(node_id)
                assert member["endpoint_id"] == configured[node_id]["endpoint_addr"][
                    "id"
                ]
                assert member["endpoint_addrs"] == [
                    canonical_json_bytes(configured[node_id]["endpoint_addr"]).decode(
                        "utf-8"
                    )
                ]

            graph_digest = _digest(graph_document)
            placement_by_node = {
                placement.node_id: placement
                for stage in graph.stages
                for placement in stage.placements
            }
            for node_id, assignment, pack in zip(
                NODE_IDS,
                deployment.assignments,
                deployment.stage_packs,
                strict=True,
            ):
                offer = coordinator.assignment_offer(
                    node_id=node_id,
                    deployment_id=graph.deployment_id,
                    deployment_epoch=graph.deployment_epoch,
                    assignment_id=assignment["assignment_id"],
                    assignment_digest=_digest(assignment),
                    stage_pack_digest=pack["stage_pack_digest"],
                    graph_digest=graph_digest,
                    load_generation=7,
                    peer_node_ids=[peer for peer in NODE_IDS if peer != node_id],
                    placement_provenance="frozen_fixture",
                )
                accepted_offer = sessions[node_id].accept_assignment_offer(offer)
                accepted_offers[node_id] = accepted_offer
                assert accepted_offer["assignment_digest"] == _digest(assignment)
                assert accepted_offer["placement_provenance"] == "frozen_fixture"
                assert [
                    peer["node_id"]
                    for peer in accepted_offer["peer_endpoint_records"]
                ] == [peer for peer in NODE_IDS if peer != node_id]
                assert accepted_offer["stage_pack_digest"] == pack["stage_pack_digest"]
                assert accepted_offer["graph_digest"] == graph_digest

            for node_id, peer_node_id in zip(
                NODE_IDS,
                reversed(NODE_IDS),
                strict=True,
            ):
                peer_records = accepted_offers[node_id]["peer_endpoint_records"]
                assert len(peer_records) == 1
                peer_record = peer_records[0]
                peer_member = coordinator.member(peer_node_id)
                assert peer_record["node_id"] == peer_node_id
                assert peer_record["endpoint_id"] == peer_member["endpoint_id"]
                assert len(peer_member["endpoint_addrs"]) == 1
                peer_endpoint_addr = json.loads(peer_member["endpoint_addrs"][0])
                assert peer_endpoint_addr["id"] == peer_record["endpoint_id"]
                started = node_processes[node_id].command(
                    "start",
                    {
                        "peer": {
                            "node_id": peer_node_id,
                            "endpoint_id": peer_record["endpoint_id"],
                            "endpoint_addr": peer_endpoint_addr,
                            "generation": peer_record["membership_generation"],
                        }
                    },
                )
                assert started["observation"]["event"] == "started"

            first = node_processes[NODE_IDS[0]]

            for node_id in NODE_IDS:
                details = configured[node_id]
                result = sessions[node_id].assignment_result(
                    assignment_id=details["assignment_id"],
                    accepted=True,
                    result_code="loaded",
                    load_proof_digest=placement_by_node[node_id].load_proof_digest,
                    runtime_endpoint="iroh://" + details["endpoint_addr"]["id"],
                )
                clients[node_id].send_member_message(result, now=NOW)
                clients[node_id].send_member_message(
                    sessions[node_id].heartbeat(
                        lifecycle_state="RUNNING",
                        active_requests=0,
                    ),
                    now=NOW,
                )
                status = coordinator.assignment_status(details["assignment_id"])
                assert status["accepted"] is True
                assert status["load_proof_digest"] == placement_by_node[node_id].load_proof_digest
                assert status["runtime_endpoint"] == "iroh://" + details["endpoint_addr"]["id"]
                assert coordinator.member(node_id)["last_heartbeat_sequence"] == 2

            request_id = str(uuid.uuid4())
            started = first.command("infer_start", _inference_request(request_id))[
                "observation"
            ]["details"]
            assert started["status"] == "DECODING"
            decoded = first.command(
                "infer_decode",
                {"request_id": request_id, "count": 2},
            )["observation"]["details"]
            assert decoded["status"] == "COMPLETED"
            assert decoded["output"]["token_indexes"] == [0, 1, 2]

            reference = load_assignment_stage(
                deployment.reference_assignment,
                deployment.reference_report,
                load_generation=7,
            )
            context = [1, 2, 3]
            expected_tokens: list[int] = []
            for _ in range(3):
                logits = execute_loaded_stage(
                    reference,
                    token_ids=mx.array((tuple(context),), dtype=mx.uint32),
                )
                mx.eval(logits)
                token = int(mx.argmax(logits[0, -1, :]).item())
                expected_tokens.append(token)
                context.append(token)
            assert decoded["output"]["token_ids"] == expected_tokens

            node_observations: dict[str, dict[str, Any]] = {}
            for node_id in NODE_IDS:
                observation = node_processes[node_id].command("snapshot")[
                    "observation"
                ]
                node_observations[node_id] = observation
                snapshot = observation["details"]
                assert snapshot["runtime"]["active_state_count"] == 0
                assert snapshot["transport_fatal_error"] is None
                clients[node_id].send_member_message(
                    sessions[node_id].drain_acknowledgement(
                        drain_id="drain-local-e2e",
                        last_request_id=request_id,
                        completed_at=NOW,
                    ),
                    now=NOW,
                )

            assert len({item["process_id"] for item in node_observations.values()}) == 2
            assert {
                item["process_id"] for item in node_observations.values()
            } == {process.process.pid for process in node_processes.values()}
            host_ids = {item["host_id"] for item in node_observations.values()}
            assert len(host_ids) == 1

            same_host_case = make_case()
            shared_host_id = next(iter(host_ids))
            stages = same_host_case.documents["run/route-challenge.json"][
                "stage_evidence"
            ]
            signed_load_proofs = same_host_case.documents[
                "runtime/load-proof-signatures.json"
            ]["signatures"]
            for stage, signed in zip(stages, signed_load_proofs, strict=True):
                stage["process_host_id"] = shared_host_id
                statement = signed["statement"]
                statement["process_host_id"] = shared_host_id
                signed["signature"]["signed_statement_digest"] = sha256_bytes(
                    canonical_json_bytes(statement)
                )
            evidence_files, evidence_manifest = same_host_case.render()
            qualification_authority = QualificationAuthority(
                clock_unix_ms=lambda: same_host_case.now_unix_ms
            )
            with pytest.raises(QualificationError) as unqualified:
                qualification_authority.qualify_and_publish(
                    evidence_files=evidence_files,
                    evidence_manifest=evidence_manifest,
                    verify_gossip_signature=synthetic_signature_verifier,
                    verify_load_proof_signature=synthetic_signature_verifier,
                )
            assert unqualified.value.code == "process_identity_invalid"
            assert qualification_authority.current() is None
    finally:
        for process in node_processes.values():
            process.stop()
        shutil.rmtree(socket_root, ignore_errors=True)

    assert node_processes
    assert all(process.process.returncode == 0 for process in node_processes.values())

    restarted = _coordinator(
        database,
        signer=seed_signer,
        id_prefix="restarted-seed-message",
    )
    for node_id, assignment in zip(NODE_IDS, deployment.assignments, strict=True):
        assert restarted.member(node_id)["last_heartbeat_sequence"] == 2
        status = restarted.assignment_status(assignment["assignment_id"])
        assert status["accepted"] is True
        assert status["result_code"] == "loaded"

    assert seed_port is not None
    with SeedHTTPServer(restarted, host="127.0.0.1", port=seed_port):
        replay_session = NodeMembershipSession(
            node_id="replay-node",
            swarm_id="swarm-local-e2e",
            seed_node_id="seed-node",
            signer=load_or_create_node_signer(
                tmp_path / "replay-node" / "identity" / "node.key"
            ),
            incarnation="replay-incarnation",
            software_version="mycelium-local-e2e",
            peer_class="mac_mlx_iroh",
            runtime_capability=MAC_RUNTIME_CAPABILITY,
            clock=lambda: NOW,
            id_source=_ids("replay-message"),
        )
        replay_request = replay_session.join_request(
            invite_nonce="invite-local-e2e-1",
            endpoint_addrs=["http://127.0.0.1/replay-node"],
        )
        replay_client = SeedHTTPClient.from_invite_bundle(
            invite_bundles[NODE_IDS[0]],
            now=NOW,
            timeout=2,
        )
        with pytest.raises(SeedHTTPError) as replayed:
            replay_client.join(
                invite_token=invite_bundles[NODE_IDS[0]]["token"],
                join_envelope=replay_request,
            )
        assert replayed.value.code == "seed_join_retry_mismatch"
