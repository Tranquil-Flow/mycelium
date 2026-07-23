from __future__ import annotations
import ast
from builtins import BaseExceptionGroup
from dataclasses import asdict
import hashlib
import inspect
from itertools import count
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
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
import mycelium_qualification.signing as signing
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
    _command,
)
from tests.seed_coordinator import native_process_harness as harness

NOW = 7_000.0
NODE_IDS = ("node-local-a", "node-local-b")
WORKTREE_ROOT = Path(__file__).resolve().parents[2]
NODE_SCRIPT = WORKTREE_ROOT / "physical_inference_node.py"
SERVICE_EXECUTABLE = Path(sys.prefix, "Resources/Python.app/Contents/MacOS/Python")
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


def _redacted_evidence_json(
    payload: dict[str, Any],
    *,
    raw_host_id: str,
    sidecar_binary: Path,
    raw_traces: dict[str, list[str]],
) -> str:
    repository = WORKTREE_ROOT.resolve(strict=True)
    sidecar_path = sidecar_binary.resolve().relative_to(repository)
    evidence = {
        **payload,
        "host_id_sha256": _digest(raw_host_id),
        "sidecar_path": sidecar_path.as_posix(),
        "trace_deltas": {
            node_id: {
                "entries": len(trace),
                "remote_entries": sum(":remote" in entry for entry in trace),
            }
            for node_id, trace in raw_traces.items()
        },
    }
    rendered = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    forbidden = {
        raw_host_id,
        str(sidecar_binary.resolve()),
        str(repository),
        str(Path.home()),
    }
    assert all(not value or value not in rendered for value in forbidden)
    return rendered


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
        signer=signing.generate_ed25519_signer(endpoint_id=endpoint_addr["id"]),
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
        executable = SERVICE_EXECUTABLE
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
        harness.register_owned_group(
            owned_groups,
            node_id,
            self.process,
            executable,
            worktree=WORKTREE_ROOT,
        )

    def stop(self) -> None:
        if self.process.poll() is None:
            self.command("stop")
            self.process.wait(timeout=harness.PROCESS_WAIT_TIMEOUT)

    def request_stop(self) -> bool:
        if self.process.poll() is not None:
            return True
        stream = self.process.stdin
        if stream is None or stream.closed:
            raise BrokenPipeError("owned process stdin unavailable")
        command_id = f"{self.node_id}-{self.next_id}"
        self.next_id += 1
        encoded = (
            json.dumps(
                _command(
                    "stop",
                    command_id=command_id,
                    run_id=self.run_id,
                    deployment_id=self.deployment_id,
                ),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        descriptor = stream.fileno()
        was_blocking = os.get_blocking(descriptor)
        os.set_blocking(descriptor, False)
        try:
            written = os.write(descriptor, encoded)
        finally:
            os.set_blocking(descriptor, was_blocking)
        if written != len(encoded):
            raise BlockingIOError("incomplete owned stop request")
        return True


def _native_sidecar_pid(
    owned_groups: dict[str, harness.OwnedGroup],
    node_id: str,
    *,
    discovered_pids: set[int],
    expected_binary: Path = SIDECAR_BINARY,
) -> int:
    owner = owned_groups[node_id]
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
    harness.register_owned_member(
        owned_groups,
        node_id,
        child,
        expected_binary,
    )
    return child.pid


def _fake_owner(
    pid: int,
    *,
    process: Any | None = None,
    executable: Path = SERVICE_EXECUTABLE,
    worktree: Path = WORKTREE_ROOT,
    started: str | None = "Thu Jul 23 21:00:00 2026",
) -> harness.OwnedGroup:
    if process is None:
        process = Mock(pid=pid, stdin=None, stdout=None, stderr=None)
        process.poll.return_value = None
    leader = (
        None
        if started is None
        else harness.ProcessRecord(pid, os.getpid(), pid, started, str(executable))
    )
    members = (
        ()
        if leader is None
        else (
            harness.OwnedMember(
                leader,
                executable,
                harness.path_identity(executable),
                worktree,
                harness.path_identity(worktree),
            ),
        )
    )
    return harness.OwnedGroup(
        f"node-{pid}",
        process,
        pid,
        pid,
        pid,
        executable,
        worktree,
        harness.path_identity(executable),
        harness.path_identity(worktree),
        leader,
        members,
    )


def _exception_leaves(error: BaseException) -> list[BaseException]:
    if isinstance(error, BaseExceptionGroup):
        return [
            leaf for nested in error.exceptions for leaf in _exception_leaves(nested)
        ]
    return [error]


_ObservationTrust = tuple[bytes, str, dict[str, Any]]


def _pin_configured_observation(
    result: dict[str, Any],
    *,
    process: _IsolatedNodeClient,
    expected_host_id: str,
) -> tuple[dict[str, Any], _ObservationTrust]:
    assert set(result) == {"observation", "signature", "verification_key"}
    observation, key, signature = (
        result["observation"],
        result["verification_key"],
        result["signature"],
    )
    endpoint_id = observation["endpoint_id"]
    expected_identity = {
        "protocol": "mycelium.physical_node_observation.v1",
        "run_id": process.run_id,
        "deployment_id": process.deployment_id,
        "node_id": process.node_id,
        "host_id": expected_host_id,
        "process_id": process.process.pid,
        "endpoint_id": endpoint_id,
        "route_ready": False,
    }
    assert (
        set(observation)
        == {
            *expected_identity,
            "event",
            "monotonic_ns",
            "peer_generation",
            "state",
            "details",
        }
        and all(observation[key] == value for key, value in expected_identity.items())
        and observation["event"] == "configured"
        and type(observation["monotonic_ns"]) is int
        and observation["monotonic_ns"] > 0
        and isinstance(endpoint_id, str)
        and endpoint_id
        and observation["peer_generation"] == 0
        and observation["state"] == "CONFIGURED"
        and observation["details"]["endpoint_addr"]["id"] == endpoint_id
    )
    verifier = signing.build_ed25519_verifier([key])
    assert verifier(canonical_json_bytes(observation), signature)
    assert (
        signature["signer_endpoint_id"] == endpoint_id
        and signature["verification_key_digest"] == key["verification_key_digest"]
    )
    return observation, (
        canonical_json_bytes(key),
        key["verification_key_digest"],
        expected_identity,
    )


def _verified_observation(
    result: dict[str, Any],
    *,
    trust: _ObservationTrust,
    expected_event: str,
) -> dict[str, Any]:
    assert set(result) == {"observation", "signature", "verification_key"}
    key_record, trusted_digest, expected_identity = trust
    key_bytes = canonical_json_bytes(result["verification_key"])
    assert key_bytes == key_record
    observation, signature = result["observation"], result["signature"]
    assert set(observation) == {
        *expected_identity,
        "event",
        "monotonic_ns",
        "peer_generation",
        "state",
        "details",
    }
    verifier = signing.build_ed25519_verifier([json.loads(key_bytes)])
    assert verifier(canonical_json_bytes(observation), signature)
    assert (
        signature["verification_key_digest"] == trusted_digest
        and signature["signer_endpoint_id"] == expected_identity["endpoint_id"]
        and observation["event"] == expected_event
        and all(observation[key] == value for key, value in expected_identity.items())
        and type(observation["monotonic_ns"]) is int
        and observation["monotonic_ns"] > 0
        and observation["peer_generation"] == 1
        and observation["state"] == "RUNNING"
    )
    transport = observation["details"].get("transport")
    if transport is not None:
        assert transport["local_endpoint_id"] == expected_identity["endpoint_id"]
    return observation


def _assert_request_route_evidence(
    frame_deltas: dict[str, tuple[int, int]],
    traces: dict[str, list[str]],
    *,
    request_id: str,
    expected_types: dict[str, tuple[str, ...]],
) -> None:
    assert all(sent > 0 and received > 0 for sent, received in frame_deltas.values())
    for node_id, expected in expected_types.items():
        observed: list[str] = []
        for entry in traces[node_id]:
            assert len(entry.encode()) <= 512
            prefix, marker, payload = entry.partition(":remote:")
            if not marker:
                continue
            identity = json.loads(payload)
            assert set(identity) <= {
                "phase",
                "request_id",
                "request_id_sha256",
                "token_index",
            }
            if identity.get("request_id") == request_id:
                observed.append(prefix.split("->", 1)[0])
        assert tuple(observed) == expected


def _signed_observation(
    signer: Any,
    *,
    event: str = "snapshot",
    **changes: Any,
) -> dict[str, Any]:
    observation = {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": event,
        "monotonic_ns": 1,
        "run_id": "run-trusted",
        "deployment_id": "deployment-trusted",
        "node_id": "node-trusted",
        "host_id": "host-trusted",
        "process_id": 4_242,
        "endpoint_id": signer.endpoint_id,
        "peer_generation": 1,
        "state": "RUNNING",
        "route_ready": False,
        "details": {
            "transport": {"local_endpoint_id": signer.endpoint_id},
        },
        **changes,
    }
    return {
        "observation": observation,
        "signature": signer.sign(observation),
        "verification_key": signer.public_key_record(),
    }


def test_observation_rejects_untrusted_keys_and_identity_swaps() -> None:
    trusted = signing.generate_ed25519_signer(endpoint_id="endpoint-trusted")
    attacker = signing.generate_ed25519_signer(endpoint_id="endpoint-trusted")
    configured = _signed_observation(
        trusted,
        event="configured",
        peer_generation=0,
        state="CONFIGURED",
        details={"endpoint_addr": {"id": trusted.endpoint_id}},
    )
    channel = Mock(
        node_id="node-trusted",
        run_id="run-trusted",
        deployment_id="deployment-trusted",
        process=Mock(pid=4_242),
    )
    _, trust = _pin_configured_observation(
        configured, process=channel, expected_host_id="host-trusted"
    )
    forged = [
        _signed_observation(attacker, event="configured"),
        _signed_observation(trusted, node_id="node-swapped"),
        _signed_observation(trusted, process_id=9_999),
        _signed_observation(trusted, host_id="host-swapped"),
        _signed_observation(
            signing.generate_ed25519_signer(endpoint_id="endpoint-swapped")
        ),
    ]
    for result in forged:
        with pytest.raises(AssertionError):
            _verified_observation(
                result,
                trust=trust,
                expected_event=result["observation"]["event"],
            )


def test_request_route_evidence_rejects_unrelated_remote_noise() -> None:
    expected = {
        NODE_IDS[0]: ("ProgressivePrefillMessage", "HopHeader", "HopHeader"),
        NODE_IDS[1]: (
            "ManifestLocked",
            "TokenEvent",
            "TokenEvent",
            "TokenEvent",
        ),
    }
    unrelated = {
        node_id: [f"{frame}->peer:remote" for frame in frames]
        for node_id, frames in expected.items()
    }
    with pytest.raises(AssertionError):
        _assert_request_route_evidence(
            {node_id: (3, 3) for node_id in NODE_IDS},
            unrelated,
            request_id="request-current",
            expected_types=expected,
        )


def test_sidecar_observation_requires_exact_child_and_owned_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected, spoof = (tmp_path / name / SIDECAR_BINARY.name for name in ("ok", "bad"))
    for path in (expected, spoof):
        path.parent.mkdir()
        path.write_bytes(path.parent.name.encode())
    owner, child_pid = _fake_owner(4_242), 9_001
    owners = {owner.node_id: owner}
    records = iter(
        harness.ProcessRecord(child_pid, owner.pid, owner.pgid, "start", str(path))
        for path in (spoof, expected)
    )
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: {child_pid: next(records)},
    )
    real_getpgid = os.getpgid
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda pid: (
            owner.pgid if pid in {owner.pid, child_pid} else real_getpgid(pid)
        ),
    )
    monkeypatch.setattr(os, "getsid", lambda pid: owner.sid)
    monkeypatch.setattr(harness, "process_cwd", lambda pid: WORKTREE_ROOT)
    discovered: set[int] = set()
    with pytest.raises(AssertionError, match="unexpected executable"):
        _native_sidecar_pid(
            owners,
            owner.node_id,
            discovered_pids=discovered,
            expected_binary=expected,
        )
    assert (
        _native_sidecar_pid(
            owners,
            owner.node_id,
            discovered_pids=discovered,
            expected_binary=expected,
        )
        == child_pid
    )
    assert discovered == {child_pid}
    assert [member.record for member in owners[owner.node_id].members] == [
        owner.leader,
        harness.ProcessRecord(
            child_pid,
            owner.pid,
            owner.pgid,
            "start",
            str(expected),
        ),
    ]
    registered = owners[owner.node_id]
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: {
            member.record.pid: member.record for member in registered.members
        },
    )
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pgid, requested: signals.append((pgid, requested)),
    )
    errors, uncertain = harness.signal_owned_groups([registered], signal.SIGTERM)
    assert errors == [] and uncertain is False
    assert signals == [(registered.pgid, signal.SIGTERM)]


@pytest.mark.parametrize("requested", [signal.SIGTERM, signal.SIGKILL])
def test_group_signal_accepts_only_exact_immutable_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    requested: signal.Signals,
) -> None:
    exact, reused = _fake_owner(9_101), _fake_owner(9_104)
    exited_absent_process, exited_present_process = Mock(pid=9_105), Mock(pid=9_107)
    exited_absent_process.poll.return_value = 0
    exited_present_process.poll.return_value = 0
    exited_absent = _fake_owner(9_105, process=exited_absent_process)
    exited_present = _fake_owner(9_107, process=exited_present_process)
    descendant = harness.ProcessRecord(
        9_106, exited_absent.pid, exited_absent.pgid, "child", "sidecar"
    )
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    registered_executable = tmp_path / "registered-service"
    registered_executable.write_bytes(b"registered")
    replaced_path = _fake_owner(9_108, executable=registered_executable)
    registered_executable.rename(tmp_path / "original-service")
    registered_executable.write_bytes(b"replacement")
    cwd_drifted = _fake_owner(9_109)
    inventory = {
        exact.pid: exact.leader,
        replaced_path.pid: replaced_path.leader,
        cwd_drifted.pid: cwd_drifted.leader,
        reused.pid: harness.ProcessRecord(
            reused.pid, 2, reused.pgid, "changed start", str(reused.executable)
        ),
        exited_present.pid: exited_present.leader,
        descendant.pid: descendant,
    }
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: {pid: item for pid, item in inventory.items() if item is not None},
    )

    def group_id(pid: int) -> int:
        return exited_absent.pgid if pid == descendant.pid else pid

    monkeypatch.setattr(os, "getpgid", group_id)
    monkeypatch.setattr(os, "getsid", group_id)
    monkeypatch.setattr(
        harness,
        "process_cwd",
        lambda pid: foreign_root if pid == cwd_drifted.pid else WORKTREE_ROOT,
    )
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
            exited_absent,
            exited_present,
            replaced_path,
            cwd_drifted,
            exact,
        ],
        requested,
    )
    assert uncertain and len(errors) == 8
    assert signals == [(exact.pgid, requested)]
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: (_ for _ in ()).throw(RuntimeError("ps failed")),
    )
    signals.clear()
    errors, uncertain = harness.signal_owned_groups([exact], requested)
    assert uncertain and signals == []
    assert [str(error) for error in errors] == ["ps failed"]


def test_group_signal_refuses_unregistered_same_group_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = _fake_owner(9_121)
    extra = harness.ProcessRecord(
        9_122,
        owner.pid,
        owner.pgid,
        "Thu Jul 23 21:00:01 2026",
        str(SIDECAR_BINARY),
    )
    monkeypatch.setattr(
        harness,
        "process_inventory",
        lambda: {owner.pid: owner.leader, extra.pid: extra},
    )
    monkeypatch.setattr(harness, "process_cwd", lambda pid: WORKTREE_ROOT)
    real_getpgid = os.getpgid
    monkeypatch.setattr(
        os,
        "getpgid",
        lambda pid: (
            owner.pgid if pid in {owner.pid, extra.pid} else real_getpgid(pid)
        ),
    )
    monkeypatch.setattr(os, "getsid", lambda pid: owner.sid)
    signals: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        os,
        "killpg",
        lambda pgid, requested: signals.append((pgid, requested)),
    )

    errors, uncertain = harness.signal_owned_groups([owner], signal.SIGTERM)

    assert uncertain
    assert signals == []
    assert len(errors) == 1
    assert "unregistered" in str(errors[0])


def test_owner_registration_requires_live_exact_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 9_151
    process = Mock(pid=pid, **{"poll.return_value": None})
    expected = SERVICE_EXECUTABLE
    spoof = tmp_path / expected.name
    spoof.write_bytes(b"spoof")
    row = harness.ProcessRecord(pid, os.getpid(), pid, "start", str(spoof))
    inventory = {pid: row}
    monkeypatch.setattr(harness, "process_inventory", lambda: inventory)
    real_getpgid = os.getpgid

    def getpgid(candidate: int) -> int:
        return pid if candidate == pid else real_getpgid(candidate)

    monkeypatch.setattr(os, "getpgid", getpgid)
    monkeypatch.setattr(os, "getsid", lambda candidate: pid)
    monkeypatch.setattr(harness, "process_cwd", lambda candidate: WORKTREE_ROOT)
    owners: dict[str, harness.OwnedGroup] = {}
    with pytest.raises(AssertionError, match="executable"):
        harness.register_owned_group(owners, "node", process, expected)
    assert owners["node"].leader is None
    inventory[pid] = row._replace(comm=str(expected))
    process.poll.return_value = 0
    with pytest.raises(AssertionError, match="live"):
        harness.register_owned_group(owners, "node", process, expected)
    process.poll.return_value = None
    owner = harness.register_owned_group(owners, "node", process, expected)
    assert owner.leader == inventory[pid] and owners == {"node": owner}


def test_cleanup_preserves_body_and_cleanup_faults_without_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger: list[str] = []
    processes = {node: Mock(pid=8_200 + index) for index, node in enumerate(NODE_IDS)}
    clients = {node: Mock(process=processes[node]) for node in NODE_IDS}
    owners = {
        node: _fake_owner(process.pid, process=process, started=None)._replace(
            node_id=node
        )
        for node, process in processes.items()
    }
    for process in processes.values():
        process.poll.return_value = 0
        process.wait.side_effect = lambda timeout: ledger.append("wait")
        process.stdin = process.stdout = process.stderr = Mock(closed=False)

    def inventory(groups: Any, known_pids: Any):
        ledger.append("inventory")
        return {}, []

    cleanup_fault = GeneratorExit("signal fault")

    def signal_groups(groups: Any, requested: signal.Signals):
        ledger.append(f"signal:{requested.name}")
        return ([cleanup_fault], False) if requested == signal.SIGTERM else ([], False)

    monkeypatch.setattr(harness, "owned_inventory", inventory)
    monkeypatch.setattr(harness, "signal_owned_groups", signal_groups)
    monkeypatch.setattr(
        harness,
        "groups_still_present",
        lambda groups, timeout: ledger.append("poll") or [],
    )
    monkeypatch.setattr(harness, "PROCESS_WAIT_TIMEOUT", 0.01)
    root = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    body_fault = ValueError("body")
    try:
        with pytest.raises(BaseExceptionGroup) as grouped:
            with harness.node_process_cleanup(
                clients,
                owned_groups=owners,
                known_pids=set(),
                socket_root=root,
            ):
                raise body_fault
        cleanup_removed_root = not root.exists()
    finally:
        if root.exists():
            shutil.rmtree(root)
    leaves = _exception_leaves(grouped.value)
    for injected in (body_fault, cleanup_fault):
        assert sum(error is injected for error in leaves) == 1
    assert [ledger.count(item) for item in ("inventory", "poll", "wait")] == [2, 2, 4]
    assert ledger.count("signal:SIGTERM") == ledger.count("signal:SIGKILL") == 1
    assert cleanup_removed_root
    assert all(client.stop.call_count == 0 for client in clients.values())
    assert not any(
        thread.name.startswith("native-iroh-cleanup-")
        for thread in threading.enumerate()
    )


def test_cleanup_returns_without_starting_or_leaving_stop_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = NODE_IDS[0]
    process = Mock(pid=8_250, stdin=None, stdout=None, stderr=None)
    process.poll.return_value = 0
    process.wait.return_value = 0
    owner = _fake_owner(process.pid, process=process, started=None)._replace(
        node_id=node_id
    )
    release = threading.Event()
    client = Mock(process=process)
    client.stop.side_effect = release.wait
    monkeypatch.setattr(harness, "owned_inventory", lambda *a, **k: ({}, []))
    monkeypatch.setattr(
        harness,
        "signal_owned_groups",
        lambda *a, **k: ([], False),
    )
    monkeypatch.setattr(harness, "groups_still_present", lambda *a, **k: [])
    monkeypatch.setattr(harness, "PROCESS_WAIT_TIMEOUT", 0.01)
    root = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    before = set(threading.enumerate())
    caught: BaseException | None = None
    try:
        try:
            harness.cleanup_node_processes(
                {node_id: client},
                owned_groups={node_id: owner},
                known_pids=set(),
                root_identity=harness.capture_temp_root(root),
            )
        except BaseException as error:
            caught = error
        helpers = [
            thread
            for thread in set(threading.enumerate()) - before
            if thread.name.startswith("native-iroh-cleanup-")
        ]
        assert helpers == []
        assert caught is None
        client.stop.assert_not_called()
    finally:
        release.set()
        for thread in set(threading.enumerate()) - before:
            thread.join(0.2)
        if root.exists():
            shutil.rmtree(root)


def test_temp_root_removal_requires_original_safe_identity(tmp_path: Path) -> None:
    valid = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    harness.remove_temp_root(harness.capture_temp_root(valid))
    assert not valid.exists()
    wrong_parent = tmp_path / f"{harness.TEMP_ROOT_PREFIX}wrong-parent"
    wrong_parent.mkdir()
    with pytest.raises(AssertionError):
        harness.remove_temp_root(harness.capture_temp_root(wrong_parent))
    assert wrong_parent.is_dir()
    target = tmp_path / "symlink-target"
    target.mkdir()
    (target / "keep").write_text("safe")
    link = Path(tempfile.gettempdir()) / f"{harness.TEMP_ROOT_PREFIX}{uuid.uuid4()}"
    link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(AssertionError):
            harness.remove_temp_root(harness.capture_temp_root(link))
        assert (target / "keep").read_text() == "safe"
    finally:
        link.unlink(missing_ok=True)
    replaced = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    backup = replaced.with_name(replaced.name + "-original")
    guard = harness.capture_temp_root(replaced)
    try:
        with pytest.raises(AssertionError):
            replaced.rename(backup)
            replaced.mkdir()
            (replaced / "keep").write_text("replacement")
            harness.remove_temp_root(guard)
        assert (replaced / "keep").read_text() == "replacement"
        assert backup.is_dir()
    finally:
        if replaced.exists():
            shutil.rmtree(replaced)
        if backup.exists():
            shutil.rmtree(backup)
    raced = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    raced_backup = raced.with_name(raced.name + "-original")
    raced_guard = harness.capture_temp_root(raced)
    real_rename, real_rmtree = os.rename, shutil.rmtree
    injected = False

    def inject_replacement() -> None:
        nonlocal injected
        if injected:
            return
        injected = True
        real_rename(raced, raced_backup)
        raced.mkdir()
        (raced / "keep").write_text("replacement")

    def racing(action: Any) -> Any:
        def run(*args: Any, **kwargs: Any) -> None:
            inject_replacement()
            action(*args, **kwargs)

        return run

    try:
        with pytest.MonkeyPatch.context() as race:
            race.setattr(harness.os, "rename", racing(real_rename))
            race.setattr(shutil, "rmtree", racing(real_rmtree))
            with pytest.raises(AssertionError, match="replaced|quarantined"):
                harness.remove_temp_root(raced_guard)
        survivors = [
            candidate / "keep"
            for candidate in raced.parent.glob(raced.name + "*")
            if candidate.is_dir()
        ]
        assert any(
            path.read_text() == "replacement" for path in survivors if path.exists()
        )
    finally:
        for candidate in raced.parent.glob(raced.name + "*"):
            if candidate.is_dir():
                real_rmtree(candidate)


@pytest.mark.parametrize(
    ("kind", "nested"),
    [
        ("file", False),
        ("directory", False),
        ("symlink", False),
        ("file", True),
    ],
)
def test_temp_root_removal_never_unlinks_raced_child_replacement(
    kind: str,
    nested: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    target_parent = root / "nested" if nested else root
    target_parent.mkdir(exist_ok=True)
    target_name = f"victim-{kind}"
    target = target_parent / target_name
    if kind == "file":
        target.write_text("original")
    elif kind == "directory":
        target.mkdir()
    else:
        target.symlink_to("original-target")
    guard = harness.capture_temp_root(root)
    parent_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    target_parent_fd = os.open(target_parent, parent_flags)
    target_parent_inode = (
        os.fstat(target_parent_fd).st_dev,
        os.fstat(target_parent_fd).st_ino,
    )
    replacement_fds: list[int] = []
    injected = False
    real_rename, real_unlink, real_rmdir = os.rename, os.unlink, os.rmdir

    def is_target(name: str, directory_fd: int | None) -> bool:
        if name != target_name or directory_fd is None:
            return False
        metadata = os.fstat(directory_fd)
        return (metadata.st_dev, metadata.st_ino) == target_parent_inode

    def inject(directory_fd: int) -> None:
        nonlocal injected
        if injected:
            return
        injected = True
        backup = f"{target_name}.original"
        real_rename(
            target_name,
            backup,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        if kind == "file":
            replacement_fds.append(
                os.open(
                    target_name,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                    0o600,
                    dir_fd=directory_fd,
                )
            )
        elif kind == "directory":
            os.mkdir(target_name, dir_fd=directory_fd)
            replacement_fds.append(
                os.open(target_name, parent_flags, dir_fd=directory_fd)
            )
        else:
            os.symlink("replacement-target", target_name, dir_fd=directory_fd)
            if hasattr(os, "O_SYMLINK"):
                symlink_flags = os.O_RDONLY | os.O_SYMLINK
            else:
                symlink_flags = (
                    os.O_PATH
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                )
            replacement_fds.append(
                os.open(target_name, symlink_flags, dir_fd=directory_fd)
            )

    def racing_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if is_target(source, src_dir_fd):
            inject(src_dir_fd)
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    def racing_unlink(
        name: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if is_target(name, dir_fd):
            inject(dir_fd)
        real_unlink(name, dir_fd=dir_fd)

    def racing_rmdir(
        name: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        if is_target(name, dir_fd):
            inject(dir_fd)
        real_rmdir(name, dir_fd=dir_fd)

    try:
        monkeypatch.setattr(harness.os, "rename", racing_rename)
        monkeypatch.setattr(harness.os, "unlink", racing_unlink)
        monkeypatch.setattr(harness.os, "rmdir", racing_rmdir)
        with pytest.raises((AssertionError, OSError), match="replaced|quarantined|empty"):
            harness.remove_temp_root(guard)
        assert injected and len(replacement_fds) == 1
        replacement_stat = os.fstat(replacement_fds[0])
        restored_stat = os.stat(
            target_name,
            dir_fd=target_parent_fd,
            follow_symlinks=False,
        )
        assert replacement_stat.st_nlink > 0
        assert (restored_stat.st_dev, restored_stat.st_ino) == (
            replacement_stat.st_dev,
            replacement_stat.st_ino,
        )
    finally:
        monkeypatch.undo()
        os.close(target_parent_fd)
        for replacement_fd in replacement_fds:
            os.close(replacement_fd)
        for candidate in root.parent.glob(root.name + "*"):
            if candidate.is_symlink() or candidate.is_file():
                candidate.unlink()
            elif candidate.is_dir():
                shutil.rmtree(candidate)


def test_temp_root_removal_restores_raced_root_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
    guard = harness.capture_temp_root(root)
    backup = root.with_name(root.name + ".original")
    replacement_fd: int | None = None
    injected = False
    real_rename = os.rename

    def racing_rename(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        nonlocal injected, replacement_fd
        if source == root.name and not injected:
            injected = True
            real_rename(
                source,
                backup.name,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            os.mkdir(source, dir_fd=src_dir_fd)
            replacement_fd = os.open(
                source,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                dir_fd=src_dir_fd,
            )
        real_rename(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    try:
        monkeypatch.setattr(harness.os, "rename", racing_rename)
        with pytest.raises(AssertionError, match="replaced|quarantined"):
            harness.remove_temp_root(guard)
        assert injected and replacement_fd is not None
        assert os.fstat(replacement_fd).st_nlink > 0
        assert root.is_dir()
    finally:
        monkeypatch.undo()
        if replacement_fd is not None:
            os.close(replacement_fd)
        for candidate in root.parent.glob(root.name + "*"):
            if candidate.is_dir():
                shutil.rmtree(candidate)


def test_optimized_mode_keeps_cleanup_and_trace_guards_active(tmp_path: Path) -> None:
    wrong_parent = tmp_path / f"{harness.TEMP_ROOT_PREFIX}optimized"
    wrong_parent.mkdir()
    script = """
import json
from pathlib import Path
import sys
from mycelium_router.transports.iroh import _bounded_trace_identity
from tests.seed_coordinator.native_process_harness import (
    capture_temp_root,
    remove_temp_root,
)

root = Path(sys.argv[1])
try:
    remove_temp_root(capture_temp_root(root))
except BaseException as error:
    cleanup_error = type(error).__name__
else:
    cleanup_error = None
sensitive_request = "request-sensitive-" + "r" * 2048
sensitive_phase = "phase-sensitive-" + "p" * 2048
sensitive_token = int("7" * 800)
identity = _bounded_trace_identity(
    type(
        "TraceMessage",
        (),
        {
            "request_id": sensitive_request,
            "phase": sensitive_phase,
            "token_index": sensitive_token,
        },
    )()
)
entry = f"TokenEvent->peer:remote:{identity}"
print(
    json.dumps(
        {
            "cleanup_error": cleanup_error,
            "identity": identity,
            "identity_bytes": len(identity.encode()),
            "entry_bytes": len(entry.encode()),
        }
    )
)
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", script, str(wrong_parent)],
        cwd=WORKTREE_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    evidence = json.loads(result.stdout)

    assert wrong_parent.is_dir()
    assert evidence["cleanup_error"] == "AssertionError"
    assert evidence["identity_bytes"] <= 512
    assert evidence["entry_bytes"] <= 512
    assert all(
        value not in evidence["identity"]
        for value in (
            "request-sensitive-",
            "phase-sensitive-",
            "7" * 800,
        )
    )


def test_redacted_evidence_shape_excludes_raw_values() -> None:
    raw_host = "raw-hostname-must-not-escape"
    raw_trace = "unsafe-trace-" + "x" * 4_096
    sidecar = WORKTREE_ROOT / "native" / "target" / "sidecar"
    rendered = _redacted_evidence_json(
        {"redacted": True},
        raw_host_id=raw_host,
        sidecar_binary=sidecar,
        raw_traces={"node": [raw_trace, "frame:remote"]},
    )
    evidence = json.loads(rendered)
    assert evidence["host_id_sha256"].startswith("sha256:")
    assert evidence["sidecar_path"] == "native/target/sidecar"
    assert evidence["trace_deltas"] == {"node": {"entries": 2, "remote_entries": 1}}
    assert all(
        value not in rendered for value in (raw_host, raw_trace, str(Path.home()))
    )
    assert rendered == json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    function_ast = ast.parse(inspect.getsource(_redacted_evidence_json))
    constants = {
        node.value for node in ast.walk(function_ast) if isinstance(node, ast.Constant)
    }
    assert "host_id" not in constants
    assert {"host_id_sha256", "sidecar_path", "trace_deltas"} <= constants


def test_seed_two_memberships_assign_and_run_native_iroh_inference(
    tmp_path: Path,
    local_control_sidecar_binary: Path,  # noqa: F811
) -> None:
    assert local_control_sidecar_binary == SIDECAR_BINARY and SIDECAR_BINARY.is_file()
    deployment = prepare_physical_deployment(tmp_path / "deployment", node_ids=NODE_IDS)
    assignment_reports = zip(
        deployment.assignments, deployment.artifact_reports, strict=True
    )
    loaded = [
        load_assignment_stage(assignment, report, load_generation=7)
        for assignment, report in assignment_reports
    ]
    graph = build_execution_graph(
        deployment.assignments,
        [stage.proof for stage in loaded],
        link_scheme="iroh",
        runtime_scheme="iroh",
    )
    graph_document = json.loads(json.dumps(asdict(graph)))
    state_document = {
        node_id: asdict(state)
        for node_id, state in build_physical_device_states(graph).items()
    }
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
    seed_signer = signing.generate_ed25519_signer(endpoint_id="seed-endpoint")
    coordinator = _coordinator(database, signer=seed_signer, id_prefix="seed-message")
    sessions: dict[str, NodeMembershipSession] = {}
    clients: dict[str, SeedHTTPClient] = {}
    invite_bundles: dict[str, dict[str, Any]] = {}
    node_processes: dict[str, _IsolatedNodeClient] = {}
    owned_groups: dict[str, harness.OwnedGroup] = {}
    configured: dict[str, dict[str, Any]] = {}
    observation_trusts: dict[str, _ObservationTrust] = {}
    accepted_offers: dict[str, dict[str, Any]] = {}
    sidecar_pids: dict[str, int] = {}
    discovered_child_pids: set[int] = set()
    socket_root = Path(tempfile.mkdtemp(prefix=harness.TEMP_ROOT_PREFIX))
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
            assert response["ok"] is True and response["route_ready"] is False
            observation, observation_trusts[node_id] = _pin_configured_observation(
                response["result"],
                process=process,
                expected_host_id=os.uname().nodename,
            )
            configured[node_id] = observation["details"]
            sidecar_pids[node_id] = _native_sidecar_pid(
                owned_groups,
                node_id,
                discovered_pids=discovered_child_pids,
            )
        service_pid_by_node = {
            node_id: process.process.pid for node_id, process in node_processes.items()
        }
        native_pid_union = [*service_pid_by_node.values(), *sidecar_pids.values()]
        assert len(native_pid_union) == 2 * len(NODE_IDS)
        assert all(type(pid) is int and pid > 1 for pid in native_pid_union)
        assert len(set(native_pid_union)) == len(native_pid_union)
        assert os.getpid() not in native_pid_union
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
                assert (
                    peer_record["node_id"] == peer_node_id
                    and peer_record["endpoint_id"] == peer_member["endpoint_id"]
                    and len(peer_member["endpoint_addrs"]) == 1
                )
                peer_endpoint_addr = json.loads(peer_member["endpoint_addrs"][0])
                assert peer_endpoint_addr["id"] == peer_record["endpoint_id"]
                started_result = node_processes[node_id].command(
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
                started = _verified_observation(
                    started_result,
                    trust=observation_trusts[node_id],
                    expected_event="started",
                )
                assert (
                    started["details"]["peer"]["endpoint_id"]
                    == peer_record["endpoint_id"]
                )
            first = node_processes[NODE_IDS[0]]
            for node_id in NODE_IDS:
                details = configured[node_id]
                assignment_index = NODE_IDS.index(node_id)
                expected_pack = deployment.stage_packs[assignment_index]
                expected_verification = deployment.stage_pack_verifications[
                    assignment_index
                ]
                assert (
                    details["stage_pack_digest"] == expected_pack["stage_pack_digest"]
                )
                assert (
                    details["stage_pack_verification_digest"]
                    == expected_verification["stage_pack_verification_digest"]
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
            before_observations: dict[str, dict[str, Any]] = {}
            for node_id in NODE_IDS:
                response = node_processes[node_id].raw_command("snapshot")
                assert response["ok"] is True and response["route_ready"] is False
                observation = _verified_observation(
                    response["result"],
                    trust=observation_trusts[node_id],
                    expected_event="snapshot",
                )
                before_observations[node_id] = observation
                before_request[node_id] = observation["details"]
            request_id = str(uuid.uuid4())
            started = _verified_observation(
                first.command("infer_start", _inference_request(request_id)),
                trust=observation_trusts[NODE_IDS[0]],
                expected_event="inference_started",
            )["details"]
            assert (started["request_id"], started["status"]) == (
                request_id,
                "DECODING",
            )
            decoded = _verified_observation(
                first.command(
                    "infer_decode",
                    {"request_id": request_id, "count": 2},
                ),
                trust=observation_trusts[NODE_IDS[0]],
                expected_event="inference_decoded",
            )["details"]
            assert (decoded["request_id"], decoded["status"]) == (
                request_id,
                "COMPLETED",
            )
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
            assert expected_tokens == [6, 6, 6]
            assert decoded["output"]["token_ids"] == expected_tokens
            node_observations: dict[str, dict[str, Any]] = {}
            for node_id in NODE_IDS:
                response = node_processes[node_id].raw_command("snapshot")
                assert response["ok"] is True and response["route_ready"] is False
                observation = _verified_observation(
                    response["result"],
                    trust=observation_trusts[node_id],
                    expected_event="snapshot",
                )
                node_observations[node_id] = observation
                before = before_observations[node_id]
                assert observation["node_id"] == before["node_id"] == node_id
                assert (
                    observation["process_id"]
                    == before["process_id"]
                    == service_pid_by_node[node_id]
                )
                assert observation["host_id"] == before["host_id"]
                assert (
                    observation["endpoint_id"]
                    == before["endpoint_id"]
                    == configured[node_id]["endpoint_addr"]["id"]
                )
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
                sent_delta, received_delta = (
                    after["transport"][key] - before["transport"][key]
                    for key in ("remote_frames_sent", "remote_frames_received")
                )
                request_frame_deltas[node_id] = (sent_delta, received_delta)
                before_trace = before["transport_outbound_trace"]
                after_trace = after["transport_outbound_trace"]
                assert after_trace[: len(before_trace)] == before_trace
                request_trace_deltas[node_id] = after_trace[len(before_trace) :]
            _assert_request_route_evidence(
                request_frame_deltas,
                request_trace_deltas,
                request_id=request_id,
                expected_types={
                    NODE_IDS[0]: (
                        "ProgressivePrefillMessage",
                        "HopHeader",
                        "HopHeader",
                    ),
                    NODE_IDS[1]: (
                        "ManifestLocked",
                        "TokenEvent",
                        "TokenEvent",
                        "TokenEvent",
                    ),
                },
            )
            host_ids = {item["host_id"] for item in node_observations.values()}
            assert len(host_ids) == 1
            shared_host_id = next(iter(host_ids))
            assert isinstance(shared_host_id, str) and shared_host_id
            assert shared_host_id == shared_host_id.strip()
            same_host_case = make_case()
            documents = same_host_case.documents
            stages = documents["run/route-challenge.json"]["stage_evidence"]
            signed_load_proofs = documents["runtime/load-proof-signatures.json"][
                "signatures"
            ]
            graph_stages = documents["router/execution-graph.json"]["stages"]
            gossip = documents["control/gossip-signature.json"]
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
            assert qualification_authority.current() is None
    assert all(process.process.returncode == 0 for process in node_processes.values())
    restarted = _coordinator(
        database,
        signer=seed_signer,
        id_prefix="restarted-seed-message",
    )
    for node_id, assignment in zip(NODE_IDS, deployment.assignments, strict=True):
        assert restarted.member(node_id)["last_heartbeat_sequence"] == 2
        status = restarted.assignment_status(assignment["assignment_id"])
        assert status["accepted"] is True and status["result_code"] == "loaded"
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
        _redacted_evidence_json(
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
                "token_ids": {
                    "distributed": decoded["output"]["token_ids"],
                    "reference": expected_tokens,
                },
            },
            raw_host_id=shared_host_id,
            sidecar_binary=local_control_sidecar_binary,
            raw_traces=request_trace_deltas,
        )
    )
