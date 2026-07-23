from __future__ import annotations

from builtins import BaseExceptionGroup, ExceptionGroup
from contextlib import contextmanager
from dataclasses import asdict
import hashlib
from itertools import count
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Iterator
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


def _native_sidecar_pid(
    service_pid: int,
    *,
    discovered_pids: set[int],
    expected_binary: Path = SIDECAR_BINARY,
) -> int:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,comm="],
        check=True,
        capture_output=True,
        text=True,
    )
    child_processes: dict[int, str] = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3 and int(fields[1]) == service_pid:
            child_processes[int(fields[0])] = fields[2]
    discovered_pids.update(child_processes)
    assert len(child_processes) == 1, (
        f"service process {service_pid} has unexpected children "
        f"{sorted(child_processes)}"
    )
    sidecar_pid, executable = next(iter(child_processes.items()))
    actual_binary = Path(executable).resolve(strict=True)
    exact_binary = expected_binary.resolve(strict=True)
    assert actual_binary == exact_binary and os.path.samefile(
        actual_binary, exact_binary
    ), (
        f"service process {service_pid} child {sidecar_pid} has unexpected "
        f"executable {actual_binary}; expected {exact_binary}"
    )
    return sidecar_pid


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pids_still_running(pids: set[int], *, timeout: float) -> set[int]:
    deadline = time.monotonic() + timeout
    remaining = {pid for pid in pids if _pid_exists(pid)}
    while remaining and time.monotonic() < deadline:
        time.sleep(0.02)
        remaining = {pid for pid in remaining if _pid_exists(pid)}
    return remaining


def _scoped_process_pids(
    service_pids: set[int],
    *,
    socket_root: Path,
    expected_sidecar: Path = SIDECAR_BINARY,
) -> set[int]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,args="],
        check=True,
        capture_output=True,
        text=True,
    )
    processes: dict[int, tuple[int, str]] = {}
    for line in result.stdout.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            processes[int(fields[0])] = (int(fields[1]), fields[2])
        except ValueError:
            continue

    scoped = set(service_pids)
    while True:
        descendants = {
            pid for pid, (parent_pid, _) in processes.items() if parent_pid in scoped
        }
        expanded = scoped | descendants
        if expanded == scoped:
            break
        scoped = expanded

    resolved_socket_root = socket_root.resolve(strict=False)
    resolved_sidecar = expected_sidecar.resolve(strict=True)
    for pid, (_, command) in processes.items():
        try:
            arguments = shlex.split(command)
        except ValueError:
            continue
        if not arguments or "--uds" not in arguments:
            continue
        try:
            executable = Path(arguments[0]).resolve(strict=True)
            socket_path = Path(arguments[arguments.index("--uds") + 1]).resolve(
                strict=False
            )
            socket_path.relative_to(resolved_socket_root)
        except (FileNotFoundError, IndexError, ValueError):
            continue
        if executable == resolved_sidecar and os.path.samefile(
            executable, resolved_sidecar
        ):
            scoped.add(pid)
    return scoped


def _signal_processes(
    pids: set[int], process_signal: signal.Signals
) -> list[Exception]:
    errors: list[Exception] = []
    for pid in sorted(pids):
        if pid == os.getpid():
            errors.append(AssertionError("cleanup refused to signal its own process"))
            continue
        try:
            os.kill(pid, process_signal)
        except ProcessLookupError:
            continue
        except OSError as error:
            errors.append(error)
    return errors


def _cleanup_node_processes(
    node_processes: dict[str, _NodeClient],
    *,
    discovered_child_pids: set[int],
    socket_root: Path,
) -> None:
    cleanup_errors: list[BaseException] = []
    service_pids = {
        process.process.pid
        for process in node_processes.values()
        if process.process.pid is not None
    }
    tracked_pids = service_pids | set(discovered_child_pids)
    try:
        tracked_pids.update(_scoped_process_pids(service_pids, socket_root=socket_root))
    except BaseException as error:
        cleanup_errors.append(error)

    for process in node_processes.values():
        try:
            process.stop()
        except BaseException as error:
            cleanup_errors.append(error)

    try:
        shutil.rmtree(socket_root)
    except FileNotFoundError:
        pass
    except BaseException as error:
        cleanup_errors.append(error)

    try:
        tracked_pids.update(_scoped_process_pids(service_pids, socket_root=socket_root))
    except BaseException as error:
        cleanup_errors.append(error)

    remaining = _pids_still_running(tracked_pids, timeout=2.0)
    if remaining:
        cleanup_errors.extend(_signal_processes(remaining, signal.SIGTERM))
        for process in node_processes.values():
            if process.process.pid in remaining:
                try:
                    process.process.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, ChildProcessError):
                    pass
        remaining = _pids_still_running(remaining, timeout=2.0)
    if remaining:
        cleanup_errors.extend(_signal_processes(remaining, signal.SIGKILL))
        for process in node_processes.values():
            if process.process.pid in remaining:
                try:
                    process.process.wait(timeout=2.0)
                except (subprocess.TimeoutExpired, ChildProcessError) as error:
                    cleanup_errors.append(error)
        remaining = _pids_still_running(remaining, timeout=2.0)

    for process in node_processes.values():
        for stream in (
            getattr(process.process, "stdin", None),
            getattr(process.process, "stdout", None),
            getattr(process.process, "stderr", None),
        ):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except BaseException as error:
                    cleanup_errors.append(error)

    try:
        final_scope = _scoped_process_pids(service_pids, socket_root=socket_root)
    except BaseException as error:
        cleanup_errors.append(error)
        final_scope = set()
    leaked_pids = _pids_still_running(
        tracked_pids | final_scope | remaining,
        timeout=1.0,
    )
    if leaked_pids:
        cleanup_errors.append(
            AssertionError(f"scoped service/sidecar processes leaked: {leaked_pids}")
        )
    if cleanup_errors:
        raise BaseExceptionGroup("native-Iroh process cleanup failed", cleanup_errors)


@contextmanager
def _node_process_cleanup(
    node_processes: dict[str, _NodeClient],
    *,
    discovered_child_pids: set[int],
    socket_root: Path,
) -> Iterator[None]:
    body_error: BaseException | None = None
    body_traceback = None
    try:
        yield
    except BaseException as error:
        body_error = error
        body_traceback = error.__traceback__

    cleanup_error: BaseException | None = None
    try:
        _cleanup_node_processes(
            node_processes,
            discovered_child_pids=discovered_child_pids,
            socket_root=socket_root,
        )
    except BaseException as error:
        cleanup_error = error

    if body_error is not None and cleanup_error is not None:
        raise BaseExceptionGroup(
            "test body and native-Iroh cleanup both failed",
            [body_error, cleanup_error],
        )
    if body_error is not None:
        raise body_error.with_traceback(body_traceback)
    if cleanup_error is not None:
        raise cleanup_error


def test_native_sidecar_pid_rejects_same_basename_and_tracks_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_binary = tmp_path / "built" / SIDECAR_BINARY.name
    spoof_binary = tmp_path / "untrusted" / SIDECAR_BINARY.name
    expected_binary.parent.mkdir()
    spoof_binary.parent.mkdir()
    expected_binary.write_bytes(b"expected")
    spoof_binary.write_bytes(b"spoof")
    discovered_pids: set[int] = set()
    service_pid = 4_242
    child_pid = 9_001
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=f"{child_pid} {service_pid} {spoof_binary}\n",
        ),
    )

    with pytest.raises(AssertionError, match="unexpected executable"):
        _native_sidecar_pid(
            service_pid,
            discovered_pids=discovered_pids,
            expected_binary=expected_binary,
        )
    assert discovered_pids == {child_pid}


def test_cleanup_preserves_body_and_stop_failures_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BodyFailure(RuntimeError):
        pass

    class StopFailure(RuntimeError):
        pass

    class FakeProcess:
        def __init__(self, pid: int) -> None:
            self.pid = pid
            self.returncode: int | None = None

        def wait(self, timeout: float) -> int:
            del timeout
            self.returncode = 0
            return 0

    class FakeClient:
        def __init__(self, node_id: str, pid: int) -> None:
            self.node_id = node_id
            self.process = FakeProcess(pid)

        def stop(self) -> None:
            stop_calls.append(self.node_id)
            if self.node_id == NODE_IDS[0]:
                raise StopFailure("injected first-stop failure")
            self.process.returncode = 0

    stop_calls: list[str] = []
    leak_checks: list[set[int]] = []
    node_processes = {
        node_id: FakeClient(node_id, 8_100 + index)
        for index, node_id in enumerate(NODE_IDS)
    }
    socket_root = tmp_path / "socket-root"
    socket_root.mkdir()
    (socket_root / "i.sock").write_text("socket-placeholder")
    expected_pids = {client.process.pid for client in node_processes.values()} | {8_200}

    monkeypatch.setattr(
        __name__ + "._scoped_process_pids",
        lambda service_pids, *, socket_root: set(service_pids) | {8_200},
    )

    def no_leaks(pids: set[int], *, timeout: float) -> set[int]:
        del timeout
        leak_checks.append(set(pids))
        return set()

    monkeypatch.setattr(__name__ + "._pids_still_running", no_leaks)

    with pytest.raises(ExceptionGroup) as grouped:
        with _node_process_cleanup(
            node_processes,  # type: ignore[arg-type]
            discovered_child_pids={8_200},
            socket_root=socket_root,
        ):
            raise BodyFailure("injected body failure")

    flattened = list(grouped.value.exceptions)
    assert isinstance(flattened[0], BodyFailure)
    assert any(
        isinstance(error, StopFailure)
        for group in flattened[1:]
        for error in getattr(group, "exceptions", (group,))
    )
    assert stop_calls == list(NODE_IDS)
    assert leak_checks
    assert expected_pids <= set().union(*leak_checks)
    assert not socket_root.exists()


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
    sidecar_pids: dict[str, int] = {}
    discovered_child_pids: set[int] = set()
    socket_root = Path(tempfile.mkdtemp(prefix="myc-seed-e2e-", dir="/tmp"))
    run_id = str(uuid.uuid4())
    seed_port: int | None = None

    with _node_process_cleanup(
        node_processes,
        discovered_child_pids=discovered_child_pids,
        socket_root=socket_root,
    ):
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
            sidecar_pids[node_id] = _native_sidecar_pid(
                process.process.pid,
                discovered_pids=discovered_child_pids,
            )
        assert len(
            {details["endpoint_addr"]["id"] for details in configured.values()}
        ) == len(NODE_IDS)

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
            service_pids = {process.process.pid for process in node_processes.values()}
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

            same_host_case = make_case()
            shared_host_id = next(iter(host_ids))
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
            assert observed_qualifier_inputs == [
                (
                    node_observations[node_id]["process_id"],
                    shared_host_id,
                    configured[node_id]["endpoint_addr"]["id"],
                )
                for node_id in NODE_IDS
            ]
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
