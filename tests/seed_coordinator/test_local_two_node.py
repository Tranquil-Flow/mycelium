from __future__ import annotations

from builtins import BaseExceptionGroup, ExceptionGroup
from dataclasses import asdict
import hashlib
from itertools import count
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from unittest.mock import Mock
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
from tests.seed_coordinator import native_process_harness as harness


NOW = 7_000.0
NODE_IDS = ("node-local-a", "node-local-b")
WORKTREE_ROOT = Path(__file__).resolve().parents[2]
NODE_SCRIPT = WORKTREE_ROOT / "physical_inference_node.py"
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


class _IsolatedNodeClient(_NodeClient):
    def __init__(
        self,
        *,
        node_id: str,
        run_id: str,
        deployment_id: str,
        artifact_root: Path,
        socket_root: Path,
        owned_groups: dict[str, harness.OwnedGroup],
    ) -> None:
        self.node_id = node_id
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.next_id = 1
        executable = Path(sys.executable).resolve(strict=True)
        self.process = subprocess.Popen(
            [
                str(executable),
                str(NODE_SCRIPT),
                "--run-id",
                run_id,
                "--deployment-id",
                deployment_id,
                "--node-id",
                node_id,
                "--artifact-root",
                str(artifact_root),
                "--socket-root",
                str(socket_root),
                "--sidecar-binary",
                str(SIDECAR_BINARY),
                "--sidecar-local-only",
                "--command-timeout",
                "30",
            ],
            cwd=WORKTREE_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        pid = self.process.pid
        owner = harness.OwnedGroup(
            node_id,
            self.process,
            pid,
            pid,
            pid,
            executable,
            WORKTREE_ROOT,
            None,
        )
        owned_groups[node_id] = owner
        assert os.getpgid(pid) == pid and os.getsid(pid) == pid
        leader = harness.process_inventory().get(pid)
        assert leader is not None and leader.ppid == os.getpid() and leader.pgid == pid
        owned_groups[node_id] = owner._replace(leader=leader)

    def stop(self) -> None:
        if self.process.poll() is None:
            self.command("stop")
            self.process.wait(timeout=harness.PROCESS_WAIT_TIMEOUT)


def _native_sidecar_pid(
    owner: harness.OwnedGroup,
    *,
    discovered_pids: set[int],
    expected_binary: Path = SIDECAR_BINARY,
) -> int:
    children = [
        process
        for process in harness.process_inventory().values()
        if process.ppid == owner.pid
    ]
    discovered_pids.update(process.pid for process in children)
    assert len(children) == 1, (
        f"service process {owner.pid} has unexpected children "
        f"{sorted(process.pid for process in children)}"
    )
    child = children[0]
    assert type(child.pid) is int and child.pid > 1
    assert child.pgid == os.getpgid(child.pid) == owner.pgid
    assert os.getsid(child.pid) == owner.sid
    assert harness.same_canonical_file(child.comm, expected_binary), (
        f"service process {owner.pid} child {child.pid} has unexpected "
        f"executable {child.comm}; expected {expected_binary.resolve(strict=True)}"
    )
    return child.pid


def _fake_owner(
    pid: int,
    *,
    process: Any | None = None,
    worktree: Path = WORKTREE_ROOT,
    started: str | None = "Thu Jul 23 21:00:00 2026",
) -> harness.OwnedGroup:
    executable = Path(sys.executable).resolve(strict=True)
    if process is None:
        process = Mock(pid=pid, stdin=None, stdout=None, stderr=None)
        process.poll.return_value = None
    leader = (
        None
        if started is None
        else harness.ProcessRecord(pid, 2, pid, started, str(executable))
    )
    return harness.OwnedGroup(
        f"node-{pid}",
        process,  # type: ignore[arg-type]
        pid,
        pid,
        pid,
        executable,
        worktree,
        leader,
    )


def _exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [
            leaf for nested in error.exceptions for leaf in _exception_leaves(nested)
        ]
    return [error]


def test_sidecar_observation_requires_exact_child_and_owned_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected, spoof = (tmp_path / name / SIDECAR_BINARY.name for name in ("ok", "bad"))
    for path in (expected, spoof):
        path.parent.mkdir()
        path.write_bytes(path.parent.name.encode())
    owner, child_pid = _fake_owner(4_242, started=None), 9_001
    records = iter(
        harness.ProcessRecord(child_pid, owner.pid, owner.pgid, "start", str(path))
        for path in (spoof, expected)
    )
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: {child_pid: next(records)},
    )
    monkeypatch.setattr(os, "getpgid", lambda pid: owner.pgid)
    monkeypatch.setattr(os, "getsid", lambda pid: owner.sid)
    discovered: set[int] = set()
    with pytest.raises(AssertionError, match="unexpected executable"):
        _native_sidecar_pid(owner, discovered_pids=discovered, expected_binary=expected)
    assert (
        _native_sidecar_pid(owner, discovered_pids=discovered, expected_binary=expected)
        == child_pid
    )
    assert discovered == {child_pid}


@pytest.mark.parametrize("requested", [signal.SIGTERM, signal.SIGKILL])
def test_group_signal_accepts_only_exact_immutable_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: signal.Signals,
) -> None:
    exact, reused = _fake_owner(9_101), _fake_owner(9_104)
    exited_process = Mock(pid=9_105)
    exited_process.poll.return_value = 0
    exited = _fake_owner(9_105, process=exited_process)
    descendant = harness.ProcessRecord(
        9_106, exited.pid, exited.pgid, "child", "sidecar"
    )
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    inventory = {
        exact.pid: exact.leader,
        reused.pid: harness.ProcessRecord(
            reused.pid, 2, reused.pgid, "changed start", str(reused.executable)
        ),
        descendant.pid: descendant,
    }
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: {pid: item for pid, item in inventory.items() if item is not None},
    )

    def group_id(pid: int) -> int:
        return exited.pgid if pid == descendant.pid else pid

    monkeypatch.setattr(os, "getpgid", group_id)
    monkeypatch.setattr(os, "getsid", group_id)
    monkeypatch.setattr(
        os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("raw PID"))
    )
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append((pgid, sig)))
    errors, uncertain = harness.signal_owned_groups(
        [
            _fake_owner(os.getpgrp()),
            _fake_owner(1),
            _fake_owner(9_103, worktree=foreign_root),
            reused,
            exited,
            exact,
        ],
        requested,
    )
    assert not uncertain and len(errors) == 4
    assert signals == [(exited.pgid, requested), (exact.pgid, requested)]


def test_ps_failure_falls_back_only_to_recorded_live_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _fake_owner(9_201, started=None)
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: (_ for _ in ()).throw(RuntimeError("ps failed")),
    )
    real_getpgid = os.getpgid
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda pid: owner.pgid if pid == owner.pid else real_getpgid(pid),
    )
    monkeypatch.setattr(os, "getsid", lambda pid: owner.sid)
    signals: list[int] = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: signals.append(pgid))
    errors, uncertain = harness.signal_owned_groups([owner], signal.SIGTERM)
    assert uncertain and signals == [owner.pgid]
    assert [str(error) for error in errors] == ["ps failed"]


def test_cleanup_reraises_lone_body_with_original_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = RuntimeError("lone body")
    monkeypatch.setattr(harness, "cleanup_node_processes", lambda *a, **k: None)

    def invoke() -> None:
        with harness.node_process_cleanup(
            {}, owned_groups={}, known_pids=set(), socket_root=tmp_path
        ):
            raise failure

    with pytest.raises(RuntimeError) as raised:
        invoke()
    names, traceback = [], raised.value.__traceback__
    while traceback is not None:
        names.append(traceback.tb_frame.f_code.co_name)
        traceback = traceback.tb_next
    assert raised.value is failure and "invoke" in names


def test_cleanup_accumulates_every_phase_failure_and_bounds_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger: list[str] = []

    def fail(phase: str) -> None:
        ledger.append(phase)
        raise RuntimeError(phase)

    processes = {node: Mock(pid=8_100 + index) for index, node in enumerate(NODE_IDS)}
    clients = {node: Mock(node_id=node, process=processes[node]) for node in NODE_IDS}
    clients[NODE_IDS[0]].stop.side_effect = threading.Event().wait
    clients[NODE_IDS[1]].stop.side_effect = RuntimeError("stop")
    for process in processes.values():
        process.wait.side_effect = RuntimeError("wait")
        for name in ("stdin", "stdout", "stderr"):
            stream = Mock(closed=False)
            stream.close.side_effect = RuntimeError("close")
            setattr(process, name, stream)
    owners = {
        node: _fake_owner(process.pid, process=process, started=None)
        for node, process in processes.items()
    }
    root = tmp_path / "myc-seed-e2e-faults"
    root.mkdir()
    for name in ("STOP_TIMEOUT", "STOP_REJOIN_TIMEOUT", "PROCESS_WAIT_TIMEOUT"):
        monkeypatch.setattr(harness, name, 0.01)
    monkeypatch.setattr(
        harness,
        "owned_inventory",
        lambda groups, known_pids: fail("inventory"),
    )
    monkeypatch.setattr(
        harness,
        "groups_still_present",
        lambda groups, timeout: fail("poll"),
    )
    monkeypatch.setattr(
        harness,
        "signal_owned_groups",
        lambda groups, sig: ([RuntimeError(f"signal:{sig.name}")], True),
    )
    monkeypatch.setattr(harness.shutil, "rmtree", lambda path: fail("remove"))
    started = time.monotonic()
    with pytest.raises(ExceptionGroup) as grouped:
        with harness.node_process_cleanup(
            clients,  # type: ignore[arg-type]
            owned_groups=owners,
            known_pids=set(),
            socket_root=root,
        ):
            raise ValueError("body")
    assert time.monotonic() - started < 0.5
    leaves = _exception_leaves(grouped.value)
    notes = {note for error in leaves for note in getattr(error, "__notes__", ())}
    phases = (
        "initial-inventory",
        f"stop[{NODE_IDS[0]}]",
        "signal[TERM]",
        "wait[TERM]",
        "poll[TERM]",
        "signal[KILL]",
        "wait[KILL]",
        "poll[KILL]",
        "close[",
        "root-removal",
        "final-inventory",
        "stop-thread",
        "inventory-proof",
    )
    assert isinstance(leaves[0], ValueError)
    assert all(any(phase in note for note in notes) for phase in phases)
    assert ledger.count("inventory") == ledger.count("poll") == 2
    assert clients[NODE_IDS[1]].stop.called and root.exists()


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
    node_processes: dict[str, _IsolatedNodeClient] = {}
    owned_groups: dict[str, harness.OwnedGroup] = {}
    configured: dict[str, dict[str, Any]] = {}
    accepted_offers: dict[str, dict[str, Any]] = {}
    sidecar_pids: dict[str, int] = {}
    discovered_child_pids: set[int] = set()
    socket_root = Path(tempfile.mkdtemp(prefix="myc-seed-e2e-", dir="/tmp"))
    run_id = str(uuid.uuid4())
    seed_port: int | None = None

    with harness.node_process_cleanup(
        node_processes,
        owned_groups=owned_groups,
        known_pids=discovered_child_pids,
        socket_root=socket_root,
    ):
        for node_id, suffix in zip(NODE_IDS, ("a", "b"), strict=True):
            node_processes[node_id] = _IsolatedNodeClient(
                node_id=node_id,
                run_id=run_id,
                deployment_id=graph.deployment_id,
                artifact_root=tmp_path,
                socket_root=socket_root / suffix,
                owned_groups=owned_groups,
            )
        for node_id, process in node_processes.items():
            response = process.raw_command(
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
            sidecar_pids[node_id] = _native_sidecar_pid(
                owned_groups[node_id],
                discovered_pids=discovered_child_pids,
            )
            assert response["ok"] is True
            assert response["route_ready"] is False
            result = response["result"]
            assert result["observation"]["event"] == "configured"
            configured[node_id] = result["observation"]["details"]
        endpoint_ids = {
            details["endpoint_addr"]["id"] for details in configured.values()
        }
        assert all(
            isinstance(endpoint_id, str)
            and endpoint_id
            and endpoint_id == endpoint_id.strip()
            for endpoint_id in endpoint_ids
        )
        assert len(endpoint_ids) == len(NODE_IDS)

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
                assert (
                    member["endpoint_id"] == configured[node_id]["endpoint_addr"]["id"]
                )
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
                    peer["node_id"] for peer in accepted_offer["peer_endpoint_records"]
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
                assignment_index = NODE_IDS.index(node_id)
                assert (
                    details["stage_pack_digest"]
                    == deployment.stage_packs[assignment_index]["stage_pack_digest"]
                )
                assert (
                    details["stage_pack_verification_digest"]
                    == (
                        deployment.stage_pack_verifications[assignment_index][
                            "stage_pack_verification_digest"
                        ]
                    )
                )
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
                assert (
                    status["load_proof_digest"]
                    == placement_by_node[node_id].load_proof_digest
                )
                assert (
                    status["runtime_endpoint"]
                    == "iroh://" + details["endpoint_addr"]["id"]
                )
                assert coordinator.member(node_id)["last_heartbeat_sequence"] == 2

            before_request: dict[str, dict[str, Any]] = {}
            for node_id in NODE_IDS:
                response = node_processes[node_id].raw_command("snapshot")
                assert response["ok"] is True
                assert response["route_ready"] is False
                before_request[node_id] = response["result"]["observation"]["details"]

            request_id = str(uuid.uuid4())
            started = first.command("infer_start", _inference_request(request_id))[
                "observation"
            ]["details"]
            assert started["request_id"] == request_id
            assert started["status"] == "DECODING"
            decoded = first.command(
                "infer_decode",
                {"request_id": request_id, "count": 2},
            )["observation"]["details"]
            assert decoded["request_id"] == request_id
            assert decoded["status"] == "COMPLETED"
            assert decoded["output"]["token_indexes"] == [0, 1, 2]
            assert decoded["output"]["token_ids"] == [6, 6, 6]

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
            # Deterministic golden plus partition/routing parity. The reference
            # assignment shares this repository's loader and executor, so it is
            # not presented as an independent runtime oracle.
            assert expected_tokens == [6, 6, 6]
            assert decoded["output"]["token_ids"] == expected_tokens

            node_observations: dict[str, dict[str, Any]] = {}
            for node_id in NODE_IDS:
                response = node_processes[node_id].raw_command("snapshot")
                assert response["ok"] is True
                assert response["route_ready"] is False
                observation = response["result"]["observation"]
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

            request_frame_deltas: dict[str, tuple[int, int]] = {}
            request_trace_deltas: dict[str, list[str]] = {}
            for node_id in NODE_IDS:
                before = before_request[node_id]
                after = node_observations[node_id]["details"]
                sent_delta = (
                    after["transport"]["remote_frames_sent"]
                    - before["transport"]["remote_frames_sent"]
                )
                received_delta = (
                    after["transport"]["remote_frames_received"]
                    - before["transport"]["remote_frames_received"]
                )
                assert sent_delta >= 0
                assert received_delta >= 0
                request_frame_deltas[node_id] = (sent_delta, received_delta)
                before_trace = before["transport_outbound_trace"]
                after_trace = after["transport_outbound_trace"]
                assert after_trace[: len(before_trace)] == before_trace
                request_trace_deltas[node_id] = after_trace[len(before_trace) :]

            assert all(
                sent > 0 and received > 0
                for sent, received in request_frame_deltas.values()
            )
            assert all(
                any(":remote" in entry for entry in trace)
                for trace in request_trace_deltas.values()
            )
            assert len({item["process_id"] for item in node_observations.values()}) == 2
            assert {item["process_id"] for item in node_observations.values()} == {
                process.process.pid for process in node_processes.values()
            }
            assert len(set(sidecar_pids.values())) == len(NODE_IDS)
            service_pid_by_node = {
                node_id: process.process.pid
                for node_id, process in node_processes.items()
            }
            assert all(
                type(service_pid) is int and service_pid > 1
                for service_pid in service_pid_by_node.values()
            )
            service_pids = set(service_pid_by_node.values())
            assert len(service_pids) == len(NODE_IDS)
            control_pid = os.getpid()
            assert len(
                {
                    control_pid,
                    *service_pids,
                    *sidecar_pids.values(),
                }
            ) == 1 + 2 * len(NODE_IDS)
            host_ids = {item["host_id"] for item in node_observations.values()}
            assert len(host_ids) == 1
            shared_host_id = next(iter(host_ids))
            assert (
                isinstance(shared_host_id, str)
                and shared_host_id
                and shared_host_id == shared_host_id.strip()
            )

            same_host_case = make_case()
            challenge = same_host_case.documents["run/route-challenge.json"]
            stages = challenge["stage_evidence"]
            signed_load_proofs = same_host_case.documents[
                "runtime/load-proof-signatures.json"
            ]["signatures"]
            graph_stages = same_host_case.documents["router/execution-graph.json"][
                "stages"
            ]
            gossip = same_host_case.documents["control/gossip-signature.json"]
            gossip_peers = gossip["statement"]["peers"]
            observed_qualifier_inputs: list[tuple[int, str, str]] = []
            for node_id, stage, signed, graph_stage, gossip_peer in zip(
                NODE_IDS,
                stages,
                signed_load_proofs,
                graph_stages,
                gossip_peers,
                strict=True,
            ):
                observation = node_observations[node_id]
                process_id = observation["process_id"]
                endpoint_id = observation["endpoint_id"]
                assert endpoint_id == configured[node_id]["endpoint_addr"]["id"]
                observed_qualifier_inputs.append(
                    (process_id, observation["host_id"], endpoint_id)
                )
                runtime_endpoint = f"iroh://{endpoint_id}"
                graph_stage["placements"][0]["runtime_endpoint"] = runtime_endpoint
                stage.update(
                    {
                        "process_id": process_id,
                        "process_host_id": shared_host_id,
                        "endpoint_id": endpoint_id,
                        "authenticated_endpoint_id": endpoint_id,
                        "runtime_endpoint": runtime_endpoint,
                    }
                )
                statement = signed["statement"]
                statement.update(
                    {
                        "process_id": process_id,
                        "process_host_id": shared_host_id,
                        "endpoint_id": endpoint_id,
                    }
                )
                signed["signature"]["signer_endpoint_id"] = endpoint_id
                signed["signature"]["signed_statement_digest"] = sha256_bytes(
                    canonical_json_bytes(statement)
                )
                gossip_peer["endpoint_id"] = endpoint_id
            gossip["signature"]["signer_endpoint_id"] = gossip_peers[0]["endpoint_id"]
            gossip["signature"]["signed_statement_digest"] = sha256_bytes(
                canonical_json_bytes(gossip["statement"])
            )
            expected_qualifier_inputs = [
                (
                    node_observations[node_id]["process_id"],
                    shared_host_id,
                    configured[node_id]["endpoint_addr"]["id"],
                )
                for node_id in NODE_IDS
            ]
            assert observed_qualifier_inputs == expected_qualifier_inputs
            assert [
                (
                    signed["statement"]["process_host_id"],
                    signed["statement"]["process_id"],
                )
                for signed in signed_load_proofs
            ] == [(host, pid) for pid, host, _ in expected_qualifier_inputs]
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
            # Service responses independently remain local route_ready=false
            # evidence; the rejecting authority publishes no qualification.
            assert qualification_authority.current() is None

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

    print(
        json.dumps(
            {
                "authority_published": qualification_authority.current() is not None,
                "boundaries": {
                    "network": "localhost-only",
                    "qualification_signatures": "synthetic",
                    "route_ready": False,
                    "sidecar_mode": "local-only",
                },
                "frame_deltas": {
                    node_id: {"received": received, "sent": sent}
                    for node_id, (sent, received) in request_frame_deltas.items()
                },
                "host_id": shared_host_id,
                "nodes": {
                    node_id: {
                        "endpoint_id": configured[node_id]["endpoint_addr"]["id"],
                        "service_pid": node_processes[node_id].process.pid,
                        "sidecar_pid": sidecar_pids[node_id],
                    }
                    for node_id in NODE_IDS
                },
                "protocol": "mycelium.seed_native_iroh_e2e_evidence.v1",
                "qualifier_error": unqualified.value.code,
                "redacted": True,
                "replay_error": replayed.value.code,
                "request_id": request_id,
                "sidecar_path": str(local_control_sidecar_binary.resolve(strict=True)),
                "token_ids": {
                    "distributed": decoded["output"]["token_ids"],
                    "reference": expected_tokens,
                },
                "trace_deltas": request_trace_deltas,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
