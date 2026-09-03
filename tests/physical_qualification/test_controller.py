from __future__ import annotations

import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tarfile
from typing import Any

import pytest

import physical_inference_qualification as controller_module
from mycelium_membership.contracts import (
    ASSIGNMENT_OFFER_PROTOCOL,
    sign_membership_message,
)
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import (
    build_ed25519_verifier,
    generate_ed25519_signer,
)
from physical_inference_qualification import (
    COMMANDS,
    CommandCapture,
    ControllerError,
    PeerIdentity,
    QualificationController,
    SubprocessRunner,
    _REMOTE_CLEANUP_SCRIPT,
    _REMOTE_STAGE_SCRIPT,
    _NODE_PROCESS_ENV_PREFIX,
    _peer_argument,
    _peer_process_argv,
    _parser,
    build_transfer_archive,
    main,
)

NOW = 10_000.0
EPOCH = 9
DIGEST = "sha256:" + "a" * 64
_SECURE_IDENTITY_ROOT: Path | None = None


@pytest.fixture(autouse=True)
def _secure_identity_root(tmp_path: Path):
    global _SECURE_IDENTITY_ROOT
    base = Path.home() / ".cache" / "mycelium-physical-tests"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    base.chmod(0o700)
    root = base / f"{os.getpid()}-{tmp_path.parent.name}-{tmp_path.name}"
    root.mkdir(mode=0o700)
    _SECURE_IDENTITY_ROOT = root
    try:
        yield
    finally:
        _SECURE_IDENTITY_ROOT = None
        shutil.rmtree(root, ignore_errors=True)


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_bytes: bytes | None = None,
    ) -> CommandCapture:
        del stdin_bytes
        self.calls.append((argv, timeout_seconds))
        raise AssertionError("dry/fake/local controller must not launch commands")


class StagingRunner:
    def __init__(self, responses: list[CommandCapture]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], float, bytes | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: float,
        stdin_bytes: bytes | None = None,
    ) -> CommandCapture:
        self.calls.append((argv, timeout_seconds, stdin_bytes))
        response = self.responses.pop(0)
        return CommandCapture(
            argv=argv,
            returncode=response.returncode,
            stdout=response.stdout,
            stderr=response.stderr,
        )


class FakeNodeSession:
    def __init__(
        self,
        *,
        argv: tuple[str, ...],
        node_id: str,
        run_id: str,
        deployment_id: str,
        timeout_seconds: float,
    ) -> None:
        del timeout_seconds
        self.argv = argv
        self.node_id = node_id
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.endpoint_id = f"iroh-{node_id}"
        self.signer = generate_ed25519_signer(endpoint_id=self.endpoint_id)
        self.host_id = f"host-{int(node_id.rsplit('-', 1)[1])}"
        self.process_id = 10_000 + int(node_id.rsplit('-', 1)[1])
        self.peer_generation = 0
        self.state = "NEW"
        self.commands: list[str] = []
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.fail_snapshot = False
        self.stale_rotate = False
        self.cancel_seen = False
        self.snapshot_count = 0

    def _signed(self, event: str, details: dict[str, Any]) -> dict[str, Any]:
        observation = {
            "protocol": "mycelium.physical_node_observation.v1",
            "event": event,
            "monotonic_ns": len(self.commands),
            "run_id": self.run_id,
            "deployment_id": self.deployment_id,
            "node_id": self.node_id,
            "host_id": self.host_id,
            "process_id": self.process_id,
            "endpoint_id": self.endpoint_id,
            "peer_generation": self.peer_generation,
            "state": self.state,
            "route_ready": False,
            "details": details,
        }
        return {
            "observation": observation,
            "signature": self.signer.sign(observation),
            "verification_key": self.signer.public_key_record(),
        }

    def send(
        self,
        *,
        command_id: str,
        command: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.closed:
            raise ControllerError("node_process_exited")
        self.commands.append(command)
        self.sent.append((command, dict(payload)))
        if command == "snapshot" and self.fail_snapshot:
            raise ControllerError("node_process_exited")
        if command == "hello":
            result: dict[str, Any] = {
                "protocol": "mycelium.physical_node_control.v1",
                "run_id": self.run_id,
                "deployment_id": self.deployment_id,
                "node_id": self.node_id,
                "host_id": self.host_id,
                "process_id": self.process_id,
                "endpoint_id": None,
                "peer_generation": 0,
                "state": "NEW",
                "route_ready": False,
            }
        elif command == "configure":
            self.state = "CONFIGURED"
            result = self._signed(
                "configured",
                {
                    "assignment_id": f"assignment-{self.node_id}",
                    "placement_id": f"placement-{self.node_id}",
                    "manifest_digest": "sha256:" + "a" * 64,
                    "endpoint_addr": {"id": self.endpoint_id, "addrs": []},
                    "runtime_mode": "real",
                },
            )
        elif command == "start":
            self.peer_generation = payload["peer"]["generation"]
            self.state = "RUNNING"
            result = self._signed("started", {"peer": payload["peer"]})
        elif command == "rotate":
            generation = payload["peer"]["generation"]
            self.peer_generation = generation - 1 if self.stale_rotate else generation
            result = self._signed("peer_rotated", {"peer": payload["peer"]})
        elif command == "infer_start":
            result = self._signed(
                "inference_started",
                {
                    "request_id": payload["request"]["request_id"],
                    "status": "DECODING",
                    "output": {"token_indexes": [0], "token_ids": [11]},
                },
            )
        elif command == "infer_decode":
            result = self._signed(
                "inference_decoded",
                {
                    "request_id": payload["request_id"],
                    "dispatched": payload["count"],
                    "status": "COMPLETED",
                    "output": {"token_indexes": [0, 1], "token_ids": [11, 12]},
                },
            )
        elif command == "cancel":
            self.cancel_seen = True
            result = self._signed(
                "cancelled",
                {
                    "request_id": payload["request_id"],
                    "result": {
                        "cancelled": True,
                        "path_id": "path-fake",
                        "path_attempt": 0,
                        "status_before": "DECODING",
                        "status_after": "CANCELLED",
                        "pre_cancel_token_count": 1,
                        "post_cancel_token_count": 0,
                    },
                },
            )
        elif command == "snapshot":
            self.snapshot_count += 1
            cleanup_complete = not self.cancel_seen or self.snapshot_count > 1
            result = self._signed(
                "snapshot",
                {
                    "runtime": {"active_state_count": 0},
                    "capacity": {},
                    "transport": {},
                    "transport_pending_delivery_count": 0 if cleanup_complete else 1,
                    "transport_cancellation_cleanup_complete": cleanup_complete,
                },
            )
        elif command == "stop":
            self.state = "STOPPING"
            final_observation = self._signed("stopping", {})
            self.state = "STOPPED"
            result = {"state": "STOPPED", "final_observation": final_observation}
        else:
            raise AssertionError(command)
        return {
            "protocol": "mycelium.physical_node_control.v1",
            "command_id": command_id,
            "node_id": self.node_id,
            "ok": True,
            "route_ready": False,
            "result": result,
        }

    def close(self) -> None:
        self.closed = True


class FakeEvidenceSealer:
    def __init__(
        self,
        root: Path,
        attempts: dict[str, list[FakeNodeSession]],
        *,
        expected_command: str = "run",
    ) -> None:
        self.root = root
        self.attempts = attempts
        self.expected_command = expected_command
        self.calls = 0
        self.manifest_bytes = b""
        self.last_evidence: dict[str, Any] | None = None

    def __call__(
        self,
        *,
        run_id: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls += 1
        self.last_evidence = evidence
        assert evidence["command"] == self.expected_command
        assert all(
            session.closed
            for node_attempts in self.attempts.values()
            for session in node_attempts
        )
        self.root.mkdir(mode=0o700)
        manifest = {
            "protocol": "mycelium.evidence_manifest.v1",
            "run_id": run_id,
            "evidence_class": "physical_qualification",
            "file_count": 0,
            "total_size_bytes": 0,
            "files": [],
        }
        self.manifest_bytes = canonical_json_bytes(manifest)
        manifest_path = self.root / "evidence-manifest.json"
        manifest_path.write_bytes(self.manifest_bytes)
        manifest_path.chmod(0o400)
        self.root.chmod(0o500)
        return {
            "run_id": run_id,
            "manifest_path": str(manifest_path),
            "manifest_digest": "sha256:"
            + hashlib.sha256(self.manifest_bytes).hexdigest(),
        }


def _peers(
    count: int,
    identity_root: Path,
    *,
    same_host: bool = False,
) -> tuple[PeerIdentity, ...]:
    values = []
    for index in range(count):
        values.append(
            PeerIdentity(
                node_id=f"node-{index}",
                ssh_target=f"operator@peer-{index}.example",
                host_id="host-shared" if same_host else f"host-{index}",
                boot_id=f"boot-{index}",
                staging_root=f"/tmp/mycelium-controller/run-a/node-{index}",
                process_transport="ssh",
                ssh_identity_file=str(
                    _private_identity_file(identity_root, f"identity-{index}")
                ),
            )
        )
    return tuple(values)


def _snapshot(peers: tuple[PeerIdentity, ...]) -> dict[str, Any]:
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    endpoint_by_node = {peer.node_id: f"iroh-{peer.node_id}" for peer in peers}
    offers = []
    for index, peer in enumerate(peers):
        records = [
            {
                "node_id": other.node_id,
                "endpoint_id": endpoint_by_node[other.node_id],
                "deployment_epoch": EPOCH,
                "membership_generation": other_index + 1,
                "valid_from": NOW,
                "valid_until": NOW + 120.0,
            }
            for other_index, other in enumerate(peers)
            if other.node_id != peer.node_id
        ]
        records.sort(key=lambda item: item["node_id"])
        message = {
            "protocol": ASSIGNMENT_OFFER_PROTOCOL,
            "message_id": f"offer-{index}",
            "swarm_id": "swarm-demo",
            "sender_node_id": "seed-node",
            "sender_endpoint_id": "seed-endpoint",
            "recipient_node_id": peer.node_id,
            "incarnation": "seed-incarnation",
            "generation": 1,
            "issued_at": NOW,
            "expires_at": NOW + 120.0,
            "deployment_id": "deployment-demo",
            "deployment_epoch": EPOCH,
            "assignment_id": f"assignment-{index}",
            "assignment_digest": DIGEST,
            "stage_pack_digest": "sha256:" + "b" * 64,
            "graph_digest": "sha256:" + "c" * 64,
            "load_generation": 1,
            "placement_provenance": "frozen_fixture",
            "peer_endpoint_records": records,
        }
        offers.append(sign_membership_message(signer=signer, message=message))
    return {
        "protocol": "mycelium.controller_membership_snapshot.v1",
        "seed_key_digest": signer.public_key_record()["verification_key_digest"],
        "swarm_id": "swarm-demo",
        "deployment_id": "deployment-demo",
        "deployment_epoch": EPOCH,
        "assignment_offers": offers,
    }


def _physical_run_plan(
    peers: tuple[PeerIdentity, ...],
    *,
    identity_root: str = "/var/lib/mycelium/identities",
) -> dict[str, Any]:
    return {
        "protocol": "mycelium.controller_run_plan.v1",
        "run_id": "run-1",
        "deployment_id": "deployment-demo",
        "entry_node_id": peers[0].node_id,
        "nodes": [
            {
                "node_id": peer.node_id,
                "python_executable": f"/opt/mycelium/python-{index}/bin/python3",
                "socket_root": f"/tmp/mycelium-run/socket-{index}",
                "sidecar_binary": "/opt/mycelium/bin/mycelium-iroh-sidecar",
                "endpoint_secret_file": f"{identity_root}/{peer.node_id}.key",
                "configure": {"node_payload": peer.node_id},
            }
            for index, peer in enumerate(peers)
        ],
        "request": {
            "request_id": "request-1",
            "prompt_token_ids": [1, 2, 3],
            "max_new_tokens": 2,
            "expected_new_tokens": 2,
            "qos_class": "interactive",
            "admitted_at": 0.0,
            "target_ttft_ms": 1_000.0,
            "target_tpot_ms": 1_000.0,
            "target_tokens_per_second": 1.0,
            "sampling_seed": 17,
            "generation_config_digest": "sha256:" + "b" * 64,
        },
        "decode_count": 1,
        "expected_token_ids": [11, 12],
    }


def _stage_ack_bytes(
    peer: PeerIdentity,
    archive_digest: str,
    archive_size: int,
) -> bytes:
    return (
        json.dumps(
            {
                "protocol": "mycelium.controller_remote_stage_ack.v1",
                "node_id": peer.node_id,
                "staging_root": peer.staging_root,
                "archive_digest": archive_digest,
                "archive_size_bytes": archive_size,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _physical_cleanup_captures(
    peers: tuple[PeerIdentity, ...],
) -> list[CommandCapture]:
    return [
        CommandCapture(
            argv=(),
            returncode=0,
            stdout=(
                json.dumps(
                    {
                        "protocol": "mycelium.controller_remote_cleanup_ack.v1",
                        "node_id": peer.node_id,
                        "staging_root": peer.staging_root,
                        "removed": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            stderr=b"",
        )
        for peer in peers
    ]


def _transfers(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source_root = tmp_path / "source"
    source_root.mkdir(parents=True)
    files = {
        "physical_inference_node.py": b"print('node')\n",
        "runtime_contracts.py": b"RUNTIME = 'v1'\n",
    }
    records = []
    for relative, content in files.items():
        path = source_root / relative
        path.write_bytes(content)
        records.append(
            {
                "path": relative,
                "size_bytes": len(content),
                "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
        )
    return source_root, {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": records,
    }


def _controller(
    tmp_path: Path,
    *,
    count: int = 3,
    mode: str = "dry-run",
    same_host: bool = False,
    snapshot: dict[str, Any] | None = None,
    transfer_manifest: dict[str, Any] | None = None,
    runner: RecordingRunner | None = None,
) -> tuple[QualificationController, RecordingRunner]:
    peers = _peers(count, tmp_path, same_host=same_host)
    source_root, transfers = _transfers(tmp_path)
    recorder = runner or RecordingRunner()
    return (
        QualificationController(
            mode=mode,
            peers=peers,
            source_root=source_root,
            transfer_manifest=transfer_manifest or transfers,
            membership_snapshot=snapshot or _snapshot(peers),
            now=NOW + 1.0,
            runner=recorder,
        ),
        recorder,
    )


@pytest.mark.parametrize("peer_count", [1, 3, 5])
@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_every_dry_run_command_is_n_way_inert_and_route_false(
    tmp_path: Path,
    peer_count: int,
    command: str,
) -> None:
    controller, runner = _controller(tmp_path, count=peer_count)

    result = controller.execute(command)

    assert runner.calls == []
    assert result["protocol"] == "mycelium.physical_controller_result.v1"
    assert result["command"] == command
    assert result["peer_count"] == peer_count
    assert len(result["peers"]) == peer_count
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["physical_execution"] is False
    assert all(action["argv"] is None for action in result["actions"])


def test_transfer_manifest_is_explicit_digest_bound_and_tamper_detected(
    tmp_path: Path,
) -> None:
    controller, runner = _controller(tmp_path)
    source = controller.source_root / "runtime_contracts.py"
    source.write_text("tampered\n", encoding="utf-8")

    with pytest.raises(ControllerError, match="transfer_size_mismatch"):
        controller.execute("prepare")
    assert runner.calls == []


def test_transfer_archive_contains_only_declared_files_and_is_deterministic(
    tmp_path: Path,
) -> None:
    source_root, transfers = _transfers(tmp_path)
    (source_root / "unlisted-secret.txt").write_text("must not cross boundary\n")

    first = build_transfer_archive(source_root, transfers)
    second = build_transfer_archive(source_root, transfers)

    assert first == second
    with tarfile.open(fileobj=io.BytesIO(first), mode="r:") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "physical_inference_node.py",
            "runtime_contracts.py",
        ]
        assert all(member.isfile() for member in members)
        assert all(member.mode == 0o600 for member in members)
        assert all(member.uid == member.gid == member.mtime == 0 for member in members)
        assert all(member.uname == member.gname == "" for member in members)
        first_stream = archive.extractfile(members[0])
        second_stream = archive.extractfile(members[1])
        assert first_stream is not None and first_stream.read() == b"print('node')\n"
        assert second_stream is not None and second_stream.read() == b"RUNTIME = 'v1'\n"


def test_transfer_manifest_allows_digest_bound_token_python_module(
    tmp_path: Path,
) -> None:
    source_root, transfers = _transfers(tmp_path)
    source = source_root / "mycelium_invite" / "token.py"
    source.parent.mkdir()
    payload = b"PROTOCOL = 'mycelium.invite.v1'\n"
    source.write_bytes(payload)
    transfers["files"].append(
        {
            "path": "mycelium_invite/token.py",
            "size_bytes": len(payload),
            "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
        }
    )
    transfers["files"].sort(key=lambda record: record["path"])

    archive = build_transfer_archive(source_root, transfers)

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as opened:
        assert "mycelium_invite/token.py" in opened.getnames()


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        ".git/config",
        ".env",
        ".cache/model.bin",
        "models/weights.bin",
        "private-key.pem",
        "token.py",
        "secrets/token.py",
        "pkg/credentials.py",
        "pkg/password.py",
        "pkg/token.py",
    ],
)
def test_transfer_manifest_rejects_traversal_credentials_and_model_caches(
    tmp_path: Path,
    path: str,
) -> None:
    source_root, transfers = _transfers(tmp_path)
    transfers["files"].append(
        {"path": path, "size_bytes": 1, "content_digest": DIGEST}
    )
    transfers["files"].sort(key=lambda record: record["path"])
    peers = _peers(3, tmp_path)
    controller = QualificationController(
        mode="dry-run",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=RecordingRunner(),
    )

    with pytest.raises(ControllerError, match="unsafe_transfer_path"):
        controller.execute("prepare")


def test_seed_signature_and_current_epoch_are_mandatory(tmp_path: Path) -> None:
    peers = _peers(3, tmp_path)
    tampered = _snapshot(peers)
    tampered["assignment_offers"][0]["message"]["deployment_epoch"] = EPOCH - 1
    controller, runner = _controller(tmp_path, snapshot=tampered)

    with pytest.raises(ControllerError, match="membership_offer_invalid"):
        controller.execute("run")
    assert runner.calls == []

    stale = _snapshot(peers)
    stale["deployment_epoch"] = EPOCH + 1
    controller, _ = _controller(tmp_path / "stale", snapshot=stale)
    with pytest.raises(ControllerError, match="membership_epoch_mismatch"):
        controller.execute("run")


def test_fake_and_local_modes_never_claim_readiness(tmp_path: Path) -> None:
    for mode in ("fake", "local"):
        controller, runner = _controller(tmp_path / mode, mode=mode, same_host=True)
        result = controller.execute("run")
        assert result["route_ready"] is False
        assert result["physical_execution"] is False
        assert runner.calls == []


def test_physical_mode_requires_distinct_host_and_boot_then_requires_run_plan(
    tmp_path: Path,
) -> None:
    same_host, runner = _controller(
        tmp_path / "same", mode="physical", same_host=True
    )
    with pytest.raises(ControllerError, match="physical_host_identity_not_distinct"):
        same_host.execute("run")
    assert runner.calls == []

    distinct, runner = _controller(tmp_path / "distinct", mode="physical")
    with pytest.raises(ControllerError, match="controller_run_plan_invalid"):
        distinct.execute("run")
    assert runner.calls == []


def test_rejected_node_observation_preserves_remote_error_code(tmp_path: Path) -> None:
    controller, _runner = _controller(tmp_path, mode="physical")
    peer = controller.peers[0]

    with pytest.raises(ControllerError, match="node_command_rejected") as caught:
        controller._verified_observation(
            {
                "ok": False,
                "error": {"code": "prefill_completion_timeout"},
            },
            event="inference_started",
            peer=peer,
            process_id=1,
            run_id="run-a",
            deployment_id="deployment-a",
            endpoint_id="endpoint-a",
        )

    assert caught.value.code == "node_command_rejected"
    assert caught.value.remote_code == "prefill_completion_timeout"


def test_failed_physical_run_attaches_rejecting_node_stderr_tail(tmp_path: Path) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))

    class RejectingSession(FakeNodeSession):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.stderr = (
                b"Traceback (most recent call last):\n"
                b'  File "physical_inference_node.py", line 12, in configure\n'
                b"ValueError: stage pack digest mismatch\n"
            )

        def send(
            self,
            *,
            command_id: str,
            command: str,
            payload: dict[str, Any],
        ) -> dict[str, Any]:
            if command == "configure":
                return {
                    "protocol": "mycelium.physical_node_control.v1",
                    "command_id": command_id,
                    "node_id": self.node_id,
                    "ok": False,
                    "route_ready": False,
                    "error": {"code": "node_command_failed"},
                }
            return super().send(command_id=command_id, command=command, payload=payload)

    sessions: dict[str, FakeNodeSession] = {}

    def session_factory(**kwargs: Any) -> RejectingSession:
        session = RejectingSession(**kwargs)
        sessions[session.node_id] = session
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    with pytest.raises(ControllerError, match="node_command_rejected") as caught:
        controller.execute("run")

    assert caught.value.diagnostic is not None
    assert "node node-0 stderr" in caught.value.diagnostic
    assert "stage pack digest mismatch" in caught.value.diagnostic


def test_session_stderr_tail_is_bounded_and_skips_empty_sessions() -> None:
    class _Empty:
        pass

    class _Loud:
        def __init__(self, tail: bytes) -> None:
            self.stderr = tail

    sessions = {
        "node-0": _Empty(),
        "node-2": _Loud(b"real reason: evidence mismatch\n"),
    }
    tail = controller_module._session_stderr_tail(sessions)
    assert tail == "node node-2 stderr (tail):\nreal reason: evidence mismatch"
    assert controller_module._session_stderr_tail({"node-0": _Empty()}) is None

    noisy = "z" * (controller_module._NODE_STDERR_TAIL_CHARS + 500)
    bounded = controller_module._session_stderr_tail({"node-0": _Loud(noisy.encode())})
    assert bounded is not None
    assert len(bounded) <= controller_module._NODE_DIAGNOSTIC_MAX_CHARS


def test_physical_prepare_timeout_scales_with_archive_size_and_stays_bounded() -> None:
    assert controller_module._stage_timeout_seconds(0) == 120.0
    assert controller_module._stage_timeout_seconds(30 * 1024 * 1024) == 120.0
    assert controller_module._stage_timeout_seconds(90 * 1024 * 1024) == 240.0
    assert 735.0 < controller_module._stage_timeout_seconds(354_068_480) < 736.0
    assert 20_800.0 < controller_module._stage_timeout_seconds(
        10_878_450_728 + 13_178_880
    ) < 20_900.0
    assert controller_module._stage_timeout_seconds(10**12) == 21_600.0


def test_physical_prepare_streams_verified_archive_and_requires_bound_acknowledgements(
    tmp_path: Path,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    responses = [
        CommandCapture(
            argv=(),
            returncode=0,
            stdout=(
                json.dumps(
                    {
                        "protocol": "mycelium.controller_remote_stage_ack.v1",
                        "node_id": peer.node_id,
                        "staging_root": peer.staging_root,
                        "archive_digest": archive_digest,
                        "archive_size_bytes": len(archive),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("ascii"),
            stderr=b"",
        )
        for peer in peers
    ]
    runner = StagingRunner(responses)
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    result = controller.execute("prepare")

    assert len(runner.calls) == len(peers)
    for peer, (argv, timeout_seconds, stdin_bytes) in zip(
        peers, runner.calls, strict=True
    ):
        assert argv[0] == "ssh"
        assert peer.ssh_target in argv
        assert shlex.split(argv[-1])[0] == "python3"
        assert peer.staging_root in argv[-1]
        assert archive_digest in argv[-1]
        assert timeout_seconds == 120.0
        assert stdin_bytes == archive
    assert result["physical_execution"] is True
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert [action["status"] for action in result["actions"]] == [
        "staged",
        "staged",
    ]
    assert all(action["archive_digest"] == archive_digest for action in result["actions"])


def test_physical_prepare_uses_node_specific_subset_archives(tmp_path: Path) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    records = {record["path"]: record for record in transfers["files"]}
    node_manifests = {
        "protocol": "mycelium.controller_node_transfer_manifests.v1",
        "manifests": {
            peers[0].node_id: {
                "protocol": "mycelium.controller_transfer_manifest.v1",
                "files": [records["physical_inference_node.py"]],
            },
            peers[1].node_id: transfers,
        },
    }
    archives = [
        build_transfer_archive(source_root, node_manifests["manifests"][peer.node_id])
        for peer in peers
    ]
    digests = ["sha256:" + hashlib.sha256(archive).hexdigest() for archive in archives]
    runner = StagingRunner(
        [
            CommandCapture(
                argv=(),
                returncode=0,
                stdout=_stage_ack_bytes(peer, digest, len(archive)),
                stderr=b"",
            )
            for peer, digest, archive in zip(peers, digests, archives, strict=True)
        ]
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        node_transfer_manifests=node_manifests,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    result = controller.execute("prepare")

    assert archives[0] != archives[1]
    assert [call[2] for call in runner.calls] == archives
    assert [action["archive_digest"] for action in result["actions"]] == digests


def test_physical_prepare_binds_member_prepositioned_artifacts(tmp_path: Path) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    records = {record["path"]: record for record in transfers["files"]}
    node_manifests = {
        "protocol": "mycelium.controller_node_transfer_manifests.v1",
        "manifests": {
            peers[0].node_id: {
                "protocol": "mycelium.controller_transfer_manifest.v1",
                "files": [records["physical_inference_node.py"]],
            },
            peers[1].node_id: transfers,
        },
    }
    promoted = tmp_path / "member-state" / "promoted" / "runtime_contracts.py"
    promoted.parent.mkdir(parents=True)
    promoted.write_bytes((source_root / "runtime_contracts.py").read_bytes())
    prepositioned = {
        "protocol": "mycelium.controller_prepositioned_artifacts.v1",
        "members": {
            peers[0].node_id: [
                {
                    "destination_path": "runtime_contracts.py",
                    "source_path": str(promoted),
                    "size_bytes": records["runtime_contracts.py"]["size_bytes"],
                    "content_digest": records["runtime_contracts.py"][
                        "content_digest"
                    ],
                }
            ],
            peers[1].node_id: [],
        },
    }
    archives = [
        build_transfer_archive(source_root, node_manifests["manifests"][peer.node_id])
        for peer in peers
    ]
    member_documents = [
        controller_module._canonical_bytes(
            {
                "protocol": "mycelium.controller_prepositioned_member_artifacts.v1",
                "files": prepositioned["members"][peer.node_id],
            }
        )
        for peer in peers
    ]
    archive_digests = [
        "sha256:" + hashlib.sha256(archive).hexdigest() for archive in archives
    ]
    preposition_digests = [
        "sha256:" + hashlib.sha256(document).hexdigest()
        for document in member_documents
    ]
    runner = StagingRunner(
        [
            CommandCapture(
                argv=(),
                returncode=0,
                stdout=(
                    json.dumps(
                        {
                            "protocol": "mycelium.controller_remote_stage_ack.v1",
                            "node_id": peer.node_id,
                            "staging_root": peer.staging_root,
                            "archive_digest": archive_digest,
                            "archive_size_bytes": len(archive),
                            "preposition_digest": preposition_digest,
                            "preposition_size_bytes": len(member_document),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode(),
                stderr=b"",
            )
            for peer, archive, member_document, archive_digest, preposition_digest in zip(
                peers,
                archives,
                member_documents,
                archive_digests,
                preposition_digests,
                strict=True,
            )
        ]
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        node_transfer_manifests=node_manifests,
        prepositioned_artifacts=prepositioned,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    result = controller.execute("prepare")

    assert [call[2] for call in runner.calls] == [
        archive + member_document
        for archive, member_document in zip(archives, member_documents, strict=True)
    ]
    assert [call[1] for call in runner.calls] == [
        controller_module._stage_timeout_seconds(
            len(archive)
            + len(member_document)
            + sum(
                record["size_bytes"]
                for record in prepositioned["members"][peer.node_id]
            )
        )
        for peer, archive, member_document in zip(
            peers, archives, member_documents, strict=True
        )
    ]
    assert [action["preposition_digest"] for action in result["actions"]] == (
        preposition_digests
    )


def test_prepositioned_artifact_cannot_duplicate_coordinator_transfer(
    tmp_path: Path,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    record = transfers["files"][1]
    prepositioned = {
        "protocol": "mycelium.controller_prepositioned_artifacts.v1",
        "members": {
            peers[0].node_id: [
                {
                    "destination_path": record["path"],
                    "source_path": str(source_root / record["path"]),
                    "size_bytes": record["size_bytes"],
                    "content_digest": record["content_digest"],
                }
            ],
            peers[1].node_id: [],
        },
    }
    controller = QualificationController(
        mode="dry-run",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        node_transfer_manifests={
            "protocol": "mycelium.controller_node_transfer_manifests.v1",
            "manifests": {peer.node_id: transfers for peer in peers},
        },
        prepositioned_artifacts=prepositioned,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
    )

    with pytest.raises(
        ControllerError,
        match="prepositioned_artifact_also_transferred",
    ):
        controller.execute("prepare")


def test_node_transfer_manifests_must_cover_the_base_manifest(tmp_path: Path) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    node_script = transfers["files"][0]
    node_manifests = {
        "protocol": "mycelium.controller_node_transfer_manifests.v1",
        "manifests": {
            peer.node_id: {
                "protocol": "mycelium.controller_transfer_manifest.v1",
                "files": [node_script],
            }
            for peer in peers
        },
    }
    controller = QualificationController(
        mode="dry-run",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        node_transfer_manifests=node_manifests,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
    )

    with pytest.raises(ControllerError, match="node_transfer_manifests_incomplete"):
        controller.execute("prepare")


def test_physical_prepare_uses_declared_local_and_ssh_process_transports(
    tmp_path: Path,
) -> None:
    identity_file = _private_identity_file(tmp_path)
    peers = (
        PeerIdentity(
            node_id="node-0",
            ssh_target="operator@coordinator.example",
            host_id="host-0",
            boot_id="boot-0",
            staging_root="/tmp/mycelium-controller/run-a/node-0",
            process_transport="local",
        ),
        PeerIdentity(
            node_id="node-1",
            ssh_target="operator@peer.example",
            host_id="host-1",
            boot_id="boot-1",
            staging_root="/tmp/mycelium-controller/run-a/node-1",
            process_transport="ssh",
            ssh_identity_file=str(identity_file),
        ),
    )
    source_root, transfers = _transfers(tmp_path)
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    runner = StagingRunner(
        [
            CommandCapture(
                argv=(),
                returncode=0,
                stdout=_stage_ack_bytes(peer, archive_digest, len(archive)),
                stderr=b"",
            )
            for peer in peers
        ]
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    result = controller.execute("prepare")

    local_argv = runner.calls[0][0]
    remote_argv = runner.calls[1][0]
    assert local_argv[:2] == ("python3", "-c")
    assert "ssh" not in local_argv
    assert remote_argv[0] == "ssh"
    assert "BatchMode=yes" in remote_argv
    assert "IdentitiesOnly=yes" in remote_argv
    assert "StrictHostKeyChecking=yes" in remote_argv
    assert peers[1].ssh_target in remote_argv
    assert result["route_ready"] is False


def test_physical_prepare_cleans_attempted_peers_when_staging_fails(
    tmp_path: Path,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    first_ack = {
        "protocol": "mycelium.controller_remote_stage_ack.v1",
        "node_id": peers[0].node_id,
        "staging_root": peers[0].staging_root,
        "archive_digest": archive_digest,
        "archive_size_bytes": len(archive),
    }
    cleanup_acks = [
        {
            "protocol": "mycelium.controller_remote_cleanup_ack.v1",
            "node_id": peer.node_id,
            "staging_root": peer.staging_root,
            "removed": removed,
        }
        for peer, removed in zip(peers, (True, False), strict=True)
    ]
    runner = StagingRunner(
        [
            CommandCapture(
                argv=(),
                returncode=0,
                stdout=(json.dumps(first_ack, sort_keys=True, separators=(",", ":")) + "\n").encode(),
                stderr=b"",
            ),
            CommandCapture(argv=(), returncode=2, stdout=b"", stderr=b"remote_stage_rejected\n"),
            *[
                CommandCapture(
                    argv=(),
                    returncode=0,
                    stdout=(json.dumps(ack, sort_keys=True, separators=(",", ":")) + "\n").encode(),
                    stderr=b"",
                )
                for ack in cleanup_acks
            ],
        ]
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    with pytest.raises(ControllerError, match="remote_stage_failed"):
        controller.execute("prepare")

    assert len(runner.calls) == 4
    for peer, (cleanup_argv, cleanup_timeout, cleanup_stdin) in zip(
        peers, runner.calls[2:], strict=True
    ):
        assert cleanup_argv[0] == "ssh"
        assert peer.ssh_target in cleanup_argv
        assert peer.staging_root in cleanup_argv[-1]
        assert cleanup_timeout == 30.0
        assert cleanup_stdin is None


def test_physical_run_orchestrates_signed_nodes_and_cleans_staging(
    tmp_path: Path,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    cleanup_responses = [
        CommandCapture(
            argv=(),
            returncode=0,
            stdout=(
                json.dumps(
                    {
                        "protocol": "mycelium.controller_remote_cleanup_ack.v1",
                        "node_id": peer.node_id,
                        "staging_root": peer.staging_root,
                        "removed": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            stderr=b"",
        )
        for peer in peers
    ]
    runner = StagingRunner(cleanup_responses)
    sessions: dict[str, FakeNodeSession] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        sessions[session.node_id] = session
        return session

    run_plan = _physical_run_plan(peers)
    run_plan["decode_mode"] = "stage_local_kv"
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=run_plan,
        session_factory=session_factory,
    )

    result = controller.execute("run")

    assert result["physical_execution"] is True
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["token_parity"] is True
    assert result["output_token_ids"] == [11, 12]
    signed_observations = result["signed_observations"]
    assert len(signed_observations) == 10
    assert {
        (item["observation"]["node_id"], item["observation"]["event"])
        for item in signed_observations
    } == {
        ("node-0", "configured"),
        ("node-0", "started"),
        ("node-0", "inference_started"),
        ("node-0", "inference_decoded"),
        ("node-0", "snapshot"),
        ("node-0", "stopping"),
        ("node-1", "configured"),
        ("node-1", "started"),
        ("node-1", "snapshot"),
        ("node-1", "stopping"),
    }
    for item in signed_observations:
        assert set(item) == {"observation", "signature", "verification_key"}
        verifier = build_ed25519_verifier([item["verification_key"]])
        assert verifier(
            canonical_json_bytes(item["observation"]),
            item["signature"],
        ) is True
    assert set(sessions) == {peer.node_id for peer in peers}
    for peer in peers:
        remote_argv = shlex.split(sessions[peer.node_id].argv[-1])
        node_index = peers.index(peer)
        assert tuple(remote_argv[:4]) == _NODE_PROCESS_ENV_PREFIX
        assert remote_argv[4] == f"/opt/mycelium/python-{node_index}/bin/python3"
        assert remote_argv[5] == "-B"
        key_flag = remote_argv.index("--endpoint-secret-file")
        assert remote_argv[key_flag + 1] == (
            f"/var/lib/mycelium/identities/{peer.node_id}.key"
        )
        mode_flag = remote_argv.index("--decode-mode")
        assert remote_argv[mode_flag + 1] == "stage_local_kv"
    assert sessions[peers[0].node_id].commands == [
        "hello",
        "configure",
        "start",
        "infer_start",
        "infer_decode",
        "snapshot",
        "stop",
    ]
    assert sessions[peers[1].node_id].commands == [
        "hello",
        "configure",
        "start",
        "snapshot",
        "stop",
    ]
    assert all(session.closed for session in sessions.values())
    assert len(runner.calls) == len(peers)
    assert all(call[2] is None for call in runner.calls)
    assert all(archive_digest in call[0][-1] for call in runner.calls)


def test_physical_run_uses_declared_local_and_ssh_process_transports(
    tmp_path: Path,
) -> None:
    identity_file = _private_identity_file(tmp_path)
    peers = (
        PeerIdentity(
            node_id="node-0",
            ssh_target="operator@coordinator.example",
            host_id="host-0",
            boot_id="boot-0",
            staging_root="/tmp/mycelium-controller/run-a/node-0",
            process_transport="local",
        ),
        PeerIdentity(
            node_id="node-1",
            ssh_target="operator@peer.example",
            host_id="host-1",
            boot_id="boot-1",
            staging_root="/tmp/mycelium-controller/run-a/node-1",
            process_transport="ssh",
            ssh_identity_file=str(identity_file),
        ),
    )
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    sessions: dict[str, FakeNodeSession] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        sessions[session.node_id] = session
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    result = controller.execute("run")

    local_argv = sessions["node-0"].argv
    remote_argv = sessions["node-1"].argv
    assert local_argv[:4] == _NODE_PROCESS_ENV_PREFIX
    assert local_argv[4] == "/opt/mycelium/python-0/bin/python3"
    assert "ssh" not in local_argv
    assert remote_argv[0] == "ssh"
    assert "BatchMode=yes" in remote_argv
    assert "IdentitiesOnly=yes" in remote_argv
    assert "StrictHostKeyChecking=yes" in remote_argv
    assert peers[1].ssh_target in remote_argv
    assert result["route_ready"] is False


def _private_identity_file(tmp_path: Path, name: str = "peer.identity") -> Path:
    del tmp_path
    assert _SECURE_IDENTITY_ROOT is not None
    identity_file = _SECURE_IDENTITY_ROOT / name
    if not identity_file.exists():
        identity_file.write_bytes(b"non-credential test identity\n")
    identity_file.chmod(0o600)
    return identity_file


def test_ssh_process_transport_round_trips_exact_remote_argv(tmp_path: Path) -> None:
    identity_file = _private_identity_file(tmp_path)
    peer = PeerIdentity(
        node_id="node-1",
        ssh_target="operator@peer.example",
        host_id="host-1",
        boot_id="boot-1",
        staging_root="/tmp/mycelium-controller/run-a/node-1",
        process_transport="ssh",
        ssh_identity_file=str(identity_file),
    )
    command = (
        "/opt/mycelium runtime/bin/python3",
        "/tmp/mycelium route/node;literal.py",
        "--label",
        "$(touch /tmp/mycelium-must-not-run)",
    )

    argv = _peer_process_argv(peer, command)

    assert argv[0] == "ssh"
    assert argv[11:13] == ("-i", peer.ssh_identity_file)
    assert shlex.split(argv[-1]) == list(command)


def test_node_process_environment_reserves_native_control_capacity() -> None:
    assert _NODE_PROCESS_ENV_PREFIX == (
        "env",
        "OPENBLAS_NUM_THREADS=1",
        "OMP_NUM_THREADS=1",
        "MKL_NUM_THREADS=1",
    )


@pytest.mark.parametrize("kind", ["missing", "directory", "hardlink", "permissive"])
def test_ssh_peer_rejects_unsafe_identity_file(tmp_path: Path, kind: str) -> None:
    identity_file = tmp_path / "peer.identity"
    if kind == "directory":
        identity_file.mkdir()
    elif kind != "missing":
        identity_file.write_bytes(b"non-credential test identity\n")
        identity_file.chmod(0o600)
        if kind == "hardlink":
            (tmp_path / "second.identity").hardlink_to(identity_file)
        elif kind == "permissive":
            identity_file.chmod(0o640)

    with pytest.raises(ControllerError, match="peer_ssh_identity_file_invalid"):
        PeerIdentity(
            node_id="node-1",
            ssh_target="operator@peer.example",
            host_id="host-1",
            boot_id="boot-1",
            staging_root="/tmp/mycelium-controller/run-a/node-1",
            process_transport="ssh",
            ssh_identity_file=str(identity_file),
        )


def test_ssh_peer_rejects_identity_under_writable_ancestor(tmp_path: Path) -> None:
    identity_file = _private_identity_file(tmp_path)
    identity_file.parent.chmod(0o777)

    with pytest.raises(ControllerError, match="peer_ssh_identity_file_invalid"):
        PeerIdentity(
            node_id="node-1",
            ssh_target="operator@peer.example",
            host_id="host-1",
            boot_id="boot-1",
            staging_root="/tmp/mycelium-controller/run-a/node-1",
            process_transport="ssh",
            ssh_identity_file=str(identity_file),
        )


def test_ssh_identity_replacement_is_rejected_before_process_use(
    tmp_path: Path,
) -> None:
    identity_file = _private_identity_file(tmp_path)
    peer = PeerIdentity(
        node_id="node-1",
        ssh_target="operator@peer.example",
        host_id="host-1",
        boot_id="boot-1",
        staging_root="/tmp/mycelium-controller/run-a/node-1",
        process_transport="ssh",
        ssh_identity_file=str(identity_file),
    )
    replacement = _private_identity_file(tmp_path, "replacement.identity")
    replacement.replace(identity_file)

    with pytest.raises(ControllerError, match="peer_ssh_identity_file_changed"):
        _peer_process_argv(peer, ("true",))


def test_ssh_identity_in_place_mutation_is_rejected_before_process_use(
    tmp_path: Path,
) -> None:
    identity_file = _private_identity_file(tmp_path)
    peer = PeerIdentity(
        node_id="node-1",
        ssh_target="operator@peer.example",
        host_id="host-1",
        boot_id="boot-1",
        staging_root="/tmp/mycelium-controller/run-a/node-1",
        process_transport="ssh",
        ssh_identity_file=str(identity_file),
    )
    identity_file.write_bytes(b"changed non-credential test identity bytes\n")
    identity_file.chmod(0o600)

    with pytest.raises(ControllerError, match="peer_ssh_identity_file_changed"):
        _peer_process_argv(peer, ("true",))


def test_local_process_transport_returns_exact_command_argv() -> None:
    peer = PeerIdentity(
        node_id="node-0",
        ssh_target="operator@coordinator.example",
        host_id="host-0",
        boot_id="boot-0",
        staging_root="/tmp/mycelium-controller/run-a/node-0",
        process_transport="local",
    )
    command = ("/opt/mycelium runtime/bin/python3", "literal;argument")

    assert _peer_process_argv(peer, command) == command


@pytest.mark.parametrize("peer_count", [3, 5])
def test_physical_run_orchestrates_every_declared_peer_in_n_way_cycle(
    tmp_path: Path,
    peer_count: int,
) -> None:
    peers = _peers(peer_count, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    cleanup_responses = [
        CommandCapture(
            argv=(),
            returncode=0,
            stdout=(
                json.dumps(
                    {
                        "protocol": "mycelium.controller_remote_cleanup_ack.v1",
                        "node_id": peer.node_id,
                        "staging_root": peer.staging_root,
                        "removed": True,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
            stderr=b"",
        )
        for peer in peers
    ]
    runner = StagingRunner(cleanup_responses)
    sessions: dict[str, FakeNodeSession] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        sessions[session.node_id] = session
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    result = controller.execute("run")

    ordered_node_ids = sorted(peer.node_id for peer in peers)
    expected_successors = {
        node_id: ordered_node_ids[(index + 1) % peer_count]
        for index, node_id in enumerate(ordered_node_ids)
    }
    actual_successors = {
        node_id: next(
            payload["peer"]["node_id"]
            for command, payload in session.sent
            if command == "start"
        )
        for node_id, session in sessions.items()
    }
    actual_peer_sets = {
        node_id: {
            payload["peer"]["node_id"],
            *(item["node_id"] for item in payload["peers"]),
        }
        for node_id, session in sessions.items()
        for command, payload in session.sent
        if command == "start"
    }
    assert result["peer_count"] == peer_count
    assert result["route_ready"] is False
    assert set(sessions) == set(ordered_node_ids)
    assert all("start" in session.commands for session in sessions.values())
    assert actual_successors == expected_successors
    assert set(actual_successors.values()) == set(ordered_node_ids)
    assert actual_peer_sets == {
        node_id: set(ordered_node_ids) - {node_id}
        for node_id in ordered_node_ids
    }
    assert all(session.closed for session in sessions.values())
    assert len(runner.calls) == peer_count


def test_physical_cancel_reaches_entry_node_then_stops_and_cleans_all_peers(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    sessions: dict[str, FakeNodeSession] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        sessions[session.node_id] = session
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    result = controller.execute("cancel")

    entry_session = sessions[peers[0].node_id]
    assert result["command"] == "cancel"
    assert result["cancelled"] is True
    assert result["route_ready"] is False
    assert "infer_start" in entry_session.commands
    assert "cancel" in entry_session.commands
    assert "infer_decode" not in entry_session.commands
    cancelled = result["observations"][peers[0].node_id]["cancelled"]["details"]
    assert cancelled["result"]["status_after"] == "CANCELLED"
    assert cancelled["result"]["post_cancel_token_count"] == 0
    for peer in peers:
        snapshot = result["observations"][peer.node_id]["snapshot"]["details"]
        assert snapshot["transport_pending_delivery_count"] == 0
        assert snapshot["transport_cancellation_cleanup_complete"] is True
    assert all(session.closed for session in sessions.values())
    assert len(runner.calls) == len(peers)


def test_physical_cleanup_attempts_every_declared_peer_and_stays_not_ready(
    tmp_path: Path,
) -> None:
    peers = _peers(5, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    result = controller.execute("cleanup")

    assert result["command"] == "cleanup"
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert len(result["actions"]) == len(peers)
    assert len(runner.calls) == len(peers)
    for peer, call in zip(peers, runner.calls, strict=True):
        argv = call[0]
        assert argv[:15] == (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=15",
            "-o",
            "IPQoS=ef",
            "-i",
            peer.ssh_identity_file,
            "--",
            peer.ssh_target,
        )


def test_physical_cleanup_uses_declared_local_and_ssh_process_transports(
    tmp_path: Path,
) -> None:
    identity_file = _private_identity_file(tmp_path)
    peers = (
        PeerIdentity(
            node_id="node-0",
            ssh_target="operator@coordinator.example",
            host_id="host-0",
            boot_id="boot-0",
            staging_root="/tmp/mycelium-controller/run-a/node-0",
            process_transport="local",
        ),
        PeerIdentity(
            node_id="node-1",
            ssh_target="operator@peer.example",
            host_id="host-1",
            boot_id="boot-1",
            staging_root="/tmp/mycelium-controller/run-a/node-1",
            process_transport="ssh",
            ssh_identity_file=str(identity_file),
        ),
    )
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
    )

    result = controller.execute("cleanup")

    local_argv = runner.calls[0][0]
    remote_argv = runner.calls[1][0]
    assert local_argv[:2] == ("python3", "-c")
    assert "ssh" not in local_argv
    assert remote_argv[0] == "ssh"
    assert shlex.split(remote_argv[-1])[:2] == ["python3", "-c"]
    assert "BatchMode=yes" in remote_argv
    assert "IdentitiesOnly=yes" in remote_argv
    assert "StrictHostKeyChecking=yes" in remote_argv
    assert peers[1].ssh_target in remote_argv
    assert result["route_ready"] is False


def test_physical_recover_plan_fault_interlock_terminates_remote_before_snapshot(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}
    failed_node_id = peers[1].node_id
    run_plan = _physical_run_plan(peers)
    run_plan["recovery_fault"] = {
        "kind": "physical_recovery_fault_interlock_v1",
        "node_id": failed_node_id,
        "trigger": "before_snapshot",
        "mechanism": "controller_close_stdin_process_exit",
        "claim_boundary": (
            "bounded controller interlock terminates a real node process before snapshot; "
            "transport success or latency is not synthesized"
        ),
    }

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        attempts.setdefault(session.node_id, []).append(session)
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=run_plan,
        session_factory=session_factory,
    )

    result = controller.execute("recover")

    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["recovered_nodes"] == [failed_node_id]
    assert result["restart_attempts"] == {failed_node_id: 1}
    assert result["recovery_fault"] == {
        **run_plan["recovery_fault"],
        "observed": True,
    }
    first_attempt, replacement = attempts[failed_node_id]
    assert first_attempt.closed is True
    assert "snapshot" not in first_attempt.commands
    assert replacement.commands[:3] == ["hello", "configure", "start"]
    assert "snapshot" in replacement.commands
    predecessor = attempts[peers[0].node_id][0]
    assert any(command == "rotate" for command, _payload in predecessor.sent)
    assert all(
        session.closed
        for node_attempts in attempts.values()
        for session in node_attempts
    )


def test_physical_recover_restarts_dead_remote_once_and_rotates_predecessor(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    snapshot = _snapshot(peers)
    attempts: dict[str, list[FakeNodeSession]] = {}
    failed_node_id = peers[1].node_id

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        node_attempts = attempts.setdefault(session.node_id, [])
        if session.node_id == failed_node_id and not node_attempts:
            session.fail_snapshot = True
        node_attempts.append(session)
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=snapshot,
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    result = controller.execute("recover")

    assert result["route_ready"] is False
    assert result["recovered_nodes"] == [failed_node_id]
    assert result["restart_attempts"] == {failed_node_id: 1}
    assert len(attempts[failed_node_id]) == 2
    assert all(
        shlex.split(session.argv[-1])[5] == "-B"
        for node_attempts in attempts.values()
        for session in node_attempts
    )
    predecessor = attempts[peers[0].node_id][0]
    rotate_payload = next(
        payload for command, payload in predecessor.sent if command == "rotate"
    )
    assert rotate_payload["peer"]["node_id"] == failed_node_id
    expected_generation = next(
        index + 1
        for index, peer in enumerate(peers)
        if peer.node_id == failed_node_id
    ) + 1
    assert rotate_payload["peer"]["generation"] == expected_generation
    assert all(
        session.closed
        for node_attempts in attempts.values()
        for session in node_attempts
    )
    assert len(runner.calls) == len(peers)


def test_physical_recover_restarts_local_peer_without_ssh(
    tmp_path: Path,
) -> None:
    base_peers = _peers(3, tmp_path)
    peers = tuple(
        PeerIdentity(
            node_id=peer.node_id,
            ssh_target=peer.ssh_target,
            host_id=peer.host_id,
            boot_id=peer.boot_id,
            staging_root=peer.staging_root,
            process_transport="local" if index == 0 else "ssh",
            ssh_identity_file=None if index == 0 else peer.ssh_identity_file,
        )
        for index, peer in enumerate(base_peers)
    )
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}
    failed_node_id = peers[0].node_id

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        node_attempts = attempts.setdefault(session.node_id, [])
        if session.node_id == failed_node_id and not node_attempts:
            session.fail_snapshot = True
        node_attempts.append(session)
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    result = controller.execute("recover")

    local_attempts = attempts[failed_node_id]
    assert result["recovered_nodes"] == [failed_node_id]
    assert len(local_attempts) == 2
    assert all(session.argv[:4] == _NODE_PROCESS_ENV_PREFIX for session in local_attempts)
    assert all(session.argv[4].endswith("/python3") for session in local_attempts)
    assert all(session.argv[5] == "-B" for session in local_attempts)
    assert all("ssh" not in session.argv for session in local_attempts)
    assert all(
        attempts[peer.node_id][0].argv[0] == "ssh"
        for peer in peers[1:]
    )
    assert result["route_ready"] is False


def test_physical_recover_bounds_restart_and_cleans_every_session(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}
    failed_node_id = peers[1].node_id

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        if session.node_id == failed_node_id:
            session.fail_snapshot = True
        attempts.setdefault(session.node_id, []).append(session)
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    with pytest.raises(ControllerError, match="physical_recovery_exhausted"):
        controller.execute("recover")

    assert len(attempts[failed_node_id]) == 2
    assert all(
        session.closed
        for node_attempts in attempts.values()
        for session in node_attempts
    )
    assert len(runner.calls) == len(peers)


def test_physical_recover_rejects_stale_generation_and_cleans(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}
    failed_node_id = peers[1].node_id

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        node_attempts = attempts.setdefault(session.node_id, [])
        if session.node_id == failed_node_id and not node_attempts:
            session.fail_snapshot = True
        if session.node_id == peers[0].node_id:
            session.stale_rotate = True
        node_attempts.append(session)
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    with pytest.raises(ControllerError, match="recovery_generation_stale"):
        controller.execute("recover")

    assert len(attempts[failed_node_id]) == 2
    assert all(
        session.closed
        for node_attempts in attempts.values()
        for session in node_attempts
    )
    assert len(runner.calls) == len(peers)


def test_physical_seal_without_adapters_rejects_before_orchestration(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        attempts.setdefault(session.node_id, []).append(session)
        return session

    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
    )

    with pytest.raises(
        ControllerError,
        match="controller_evidence_adapter_missing",
    ):
        controller.execute("seal")

    assert attempts == {}
    assert runner.calls == []


def test_controller_has_no_injected_qualifier_authority() -> None:
    assert "qualifier_adapter" not in inspect.signature(QualificationController).parameters


def test_physical_seal_stops_writers_locks_manifest_without_qualifying(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path / "transfer")
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        attempts.setdefault(session.node_id, []).append(session)
        return session

    sealer = FakeEvidenceSealer(tmp_path / "sealed", attempts)
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
        seal_adapter=sealer,
    )

    result = controller.execute("seal")

    manifest_path = sealer.root / "evidence-manifest.json"
    assert result["command"] == "seal"
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["qualifier_invocations"] == 0
    assert "qualification" not in result
    assert sealer.calls == 1
    assert manifest_path.read_bytes() == sealer.manifest_bytes
    assert manifest_path.stat().st_mode & 0o222 == 0
    assert sealer.root.stat().st_mode & 0o222 == 0
    assert all(
        session.closed
        for node_attempts in attempts.values()
        for session in node_attempts
    )


def test_physical_seal_evidence_stops_writers_without_invoking_qualifier(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path / "transfer")
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        attempts.setdefault(session.node_id, []).append(session)
        return session

    sealer = FakeEvidenceSealer(tmp_path / "sealed", attempts)
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers),
        session_factory=session_factory,
        seal_adapter=sealer,
    )

    result = controller.seal_evidence()

    assert result["command"] == "seal"
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["qualifier_invocations"] == 0
    assert "qualification" not in result
    assert sealer.calls == 1
    assert all(
        session.closed
        for node_attempts in attempts.values()
        for session in node_attempts
    )


def test_physical_seal_evidence_uses_run_plan_cancellation_scope(
    tmp_path: Path,
) -> None:
    peers = _peers(3, tmp_path)
    source_root, transfers = _transfers(tmp_path / "transfer")
    runner = StagingRunner(_physical_cleanup_captures(peers))
    attempts: dict[str, list[FakeNodeSession]] = {}

    def session_factory(**kwargs: Any) -> FakeNodeSession:
        session = FakeNodeSession(**kwargs)
        attempts.setdefault(session.node_id, []).append(session)
        return session

    run_plan = _physical_run_plan(peers)
    run_plan["qualification_operation"] = "cancel"
    sealer = FakeEvidenceSealer(
        tmp_path / "sealed",
        attempts,
        expected_command="cancel",
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=run_plan,
        session_factory=session_factory,
        seal_adapter=sealer,
    )

    result = controller.seal_evidence()

    assert result["command"] == "seal"
    assert result["cancelled"] is True
    assert result["output_token_ids"] == []
    assert sealer.last_evidence is not None
    assert sealer.last_evidence["command"] == "cancel"
    assert sealer.last_evidence["observations"]["node-0"]["cancelled"]["details"][
        "result"
    ]["status_after"] == "CANCELLED"


def test_physical_run_rejects_endpoint_secret_outside_identity_root(
    tmp_path: Path,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = RecordingRunner()
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=_physical_run_plan(peers, identity_root="/etc/ssh"),
    )

    with pytest.raises(
        ControllerError,
        match="run_plan_endpoint_secret_file_invalid",
    ):
        controller.execute("run")

    assert runner.calls == []


def test_physical_run_accepts_private_singular_identity_directory(
    tmp_path: Path,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner(_physical_cleanup_captures(peers))
    run_plan = _physical_run_plan(
        peers,
        identity_root="/srv/mycelium-member/state/identity",
    )
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=run_plan,
        session_factory=FakeNodeSession,
    )

    result = controller.execute("run")

    assert result["route_ready"] is False


@pytest.mark.parametrize(
    "python_executable",
    ["python3", "/opt/mycelium/../python3", "/opt/mycelium/python3\n"],
)
def test_physical_run_rejects_unsafe_python_executable(
    tmp_path: Path,
    python_executable: str,
) -> None:
    peers = _peers(2, tmp_path)
    source_root, transfers = _transfers(tmp_path)
    runner = StagingRunner([])
    run_plan = _physical_run_plan(peers)
    run_plan["nodes"][0]["python_executable"] = python_executable
    controller = QualificationController(
        mode="physical",
        peers=peers,
        source_root=source_root,
        transfer_manifest=transfers,
        membership_snapshot=_snapshot(peers),
        now=NOW + 1.0,
        runner=runner,
        run_plan=run_plan,
    )

    with pytest.raises(
        ControllerError,
        match="run_plan_python_executable_invalid",
    ):
        controller.execute("run")

    assert runner.calls == []


def test_node_observation_rejects_signer_rotation_after_configure(
    tmp_path: Path,
) -> None:
    controller, _ = _controller(tmp_path, count=2)
    peer = controller.peers[0]
    session = FakeNodeSession(
        argv=("ssh",),
        node_id=peer.node_id,
        run_id="run-1",
        deployment_id="deployment-demo",
        timeout_seconds=1.0,
    )
    configured = session.send(
        command_id="configure-1",
        command="configure",
        payload={},
    )
    trusted_key = dict(configured["result"]["verification_key"])
    session.signer = generate_ed25519_signer(endpoint_id=session.endpoint_id)
    started = session.send(
        command_id="start-1",
        command="start",
        payload={"peer": {"generation": 1}},
    )

    with pytest.raises(ControllerError, match="node_observation_invalid"):
        controller._verified_observation(
            started,
            event="started",
            peer=peer,
            process_id=session.process_id,
            run_id="run-1",
            deployment_id="deployment-demo",
            endpoint_id=session.endpoint_id,
            expected_verification_key=trusted_key,
        )


def test_remote_stage_program_verifies_extracts_and_acknowledges_archive(
    tmp_path: Path,
) -> None:
    source_root, transfers = _transfers(tmp_path / "source")
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    staging_root = tmp_path / "remote" / "mycelium-run" / "node-1"

    capture = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_STAGE_SCRIPT,
            str(staging_root),
            "node-1",
            archive_digest,
            str(len(archive)),
        ),
        timeout_seconds=10.0,
        stdin_bytes=archive,
    )

    assert capture.returncode == 0
    assert capture.stderr == b""
    assert json.loads(capture.stdout) == {
        "protocol": "mycelium.controller_remote_stage_ack.v1",
        "node_id": "node-1",
        "staging_root": str(staging_root),
        "archive_digest": archive_digest,
        "archive_size_bytes": len(archive),
    }
    for record in transfers["files"]:
        staged = staging_root / record["path"]
        assert staged.read_bytes() == (source_root / record["path"]).read_bytes()
        assert staged.stat().st_mode & 0o777 == 0o600
    assert not (staging_root / ".incoming.tar").exists()
    assert not (staging_root / ".mycelium-stage-in-progress.json").exists()

    cleanup_argv = (
        sys.executable,
        "-c",
        _REMOTE_CLEANUP_SCRIPT,
        str(staging_root),
        "node-1",
        archive_digest,
    )
    first_cleanup = SubprocessRunner().run(
        cleanup_argv,
        timeout_seconds=10.0,
    )
    assert first_cleanup.returncode == 0
    assert json.loads(first_cleanup.stdout)["removed"] is True
    assert not staging_root.exists()
    second_cleanup = SubprocessRunner().run(
        cleanup_argv,
        timeout_seconds=10.0,
    )
    assert second_cleanup.returncode == 0
    assert json.loads(second_cleanup.stdout)["removed"] is False


def test_remote_cleanup_authenticates_and_removes_interrupted_stage(
    tmp_path: Path,
) -> None:
    staging_root = tmp_path / "remote" / "mycelium-run" / "node-1"
    staging_root.mkdir(parents=True)
    archive_digest = "sha256:" + "a" * 64
    marker = {
        "archive_digest": archive_digest,
        "node_id": "node-1",
    }
    journal = staging_root / ".mycelium-stage-in-progress.json"
    journal.write_text(
        json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    journal.chmod(0o600)
    (staging_root / "partial.bin").write_bytes(b"partial")

    cleanup = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_CLEANUP_SCRIPT,
            str(staging_root),
            "node-1",
            archive_digest,
        ),
        timeout_seconds=10.0,
    )

    assert cleanup.returncode == 0
    assert cleanup.stderr == b""
    assert json.loads(cleanup.stdout)["removed"] is True
    assert not staging_root.exists()


def test_remote_stage_replaces_authenticated_completed_candidate_stage(
    tmp_path: Path,
) -> None:
    source_root, transfers = _transfers(tmp_path / "source")
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    staging_root = tmp_path / "remote" / "mycelium-candidate" / "node-1"
    staging_root.mkdir(parents=True)
    old_marker = staging_root / ".mycelium-stage.json"
    old_marker.write_text(
        json.dumps(
            {
                "archive_digest": "sha256:" + "a" * 64,
                "node_id": "node-1",
                "preposition_digest": "sha256:" + "b" * 64,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    old_marker.chmod(0o600)
    (staging_root / "old-runtime.py").write_text("old", encoding="utf-8")

    capture = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_STAGE_SCRIPT,
            str(staging_root),
            "node-1",
            archive_digest,
            str(len(archive)),
        ),
        timeout_seconds=10.0,
        stdin_bytes=archive,
    )

    assert capture.returncode == 0
    assert capture.stderr == b""
    assert json.loads(capture.stdout)["archive_digest"] == archive_digest
    assert not (staging_root / "old-runtime.py").exists()
    assert json.loads((staging_root / ".mycelium-stage.json").read_text()) == {
        "archive_digest": archive_digest,
        "node_id": "node-1",
    }


def test_remote_stage_rejects_completed_stage_for_another_node(
    tmp_path: Path,
) -> None:
    source_root, transfers = _transfers(tmp_path / "source")
    archive = build_transfer_archive(source_root, transfers)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    staging_root = tmp_path / "remote" / "mycelium-candidate" / "node-1"
    staging_root.mkdir(parents=True)
    marker = staging_root / ".mycelium-stage.json"
    marker.write_text(
        json.dumps(
            {
                "archive_digest": "sha256:" + "a" * 64,
                "node_id": "node-2",
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    marker.chmod(0o600)

    capture = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_STAGE_SCRIPT,
            str(staging_root),
            "node-1",
            archive_digest,
            str(len(archive)),
        ),
        timeout_seconds=10.0,
        stdin_bytes=archive,
    )

    assert capture.returncode == 2
    assert capture.stderr == b"remote_stage_rejected\n"
    assert staging_root.exists()
    assert marker.exists()


def test_remote_cleanup_rejects_unbound_interrupted_stage(tmp_path: Path) -> None:
    staging_root = tmp_path / "remote" / "mycelium-run" / "node-1"
    staging_root.mkdir(parents=True)
    journal = staging_root / ".mycelium-stage-in-progress.json"
    journal.write_text(
        json.dumps(
            {"archive_digest": "sha256:" + "b" * 64, "node_id": "node-1"},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    journal.chmod(0o600)

    cleanup = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_CLEANUP_SCRIPT,
            str(staging_root),
            "node-1",
            "sha256:" + "a" * 64,
        ),
        timeout_seconds=10.0,
    )

    assert cleanup.returncode == 2
    assert cleanup.stdout == b""
    assert cleanup.stderr == b"remote_cleanup_rejected\n"
    assert staging_root.exists()


def test_remote_stage_program_verifies_member_promoted_artifact(
    tmp_path: Path,
) -> None:
    source_root, transfers = _transfers(tmp_path / "source")
    records = {record["path"]: record for record in transfers["files"]}
    archive_manifest = {
        "protocol": "mycelium.controller_transfer_manifest.v1",
        "files": [records["physical_inference_node.py"]],
    }
    archive = build_transfer_archive(source_root, archive_manifest)
    archive_digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    promoted = tmp_path / "member" / "promoted" / "runtime_contracts.py"
    promoted.parent.mkdir(parents=True)
    promoted.write_bytes((source_root / "runtime_contracts.py").read_bytes())
    preposition_document = controller_module._canonical_bytes(
        {
            "protocol": "mycelium.controller_prepositioned_member_artifacts.v1",
            "files": [
                {
                    "destination_path": "runtime_contracts.py",
                    "source_path": str(promoted),
                    "size_bytes": records["runtime_contracts.py"]["size_bytes"],
                    "content_digest": records["runtime_contracts.py"][
                        "content_digest"
                    ],
                }
            ],
        }
    )
    preposition_digest = "sha256:" + hashlib.sha256(
        preposition_document
    ).hexdigest()
    staging_root = tmp_path / "remote" / "mycelium-run" / "node-1"

    capture = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_STAGE_SCRIPT,
            str(staging_root),
            "node-1",
            archive_digest,
            str(len(archive)),
            preposition_digest,
            str(len(preposition_document)),
        ),
        timeout_seconds=10.0,
        stdin_bytes=archive + preposition_document,
    )

    assert capture.returncode == 0
    assert capture.stderr == b""
    assert json.loads(capture.stdout) == {
        "protocol": "mycelium.controller_remote_stage_ack.v1",
        "node_id": "node-1",
        "staging_root": str(staging_root),
        "archive_digest": archive_digest,
        "archive_size_bytes": len(archive),
        "preposition_digest": preposition_digest,
        "preposition_size_bytes": len(preposition_document),
    }
    assert (staging_root / "runtime_contracts.py").read_bytes() == (
        source_root / "runtime_contracts.py"
    ).read_bytes()
    cleanup = SubprocessRunner().run(
        (
            sys.executable,
            "-c",
            _REMOTE_CLEANUP_SCRIPT,
            str(staging_root),
            "node-1",
            archive_digest,
            preposition_digest,
        ),
        timeout_seconds=10.0,
    )
    assert cleanup.returncode == 0
    assert json.loads(cleanup.stdout)["removed"] is True


def test_remote_stage_program_streams_archive_and_members_with_bounded_memory() -> None:
    assert "read(expected_size+1)" not in _REMOTE_STAGE_SCRIPT
    assert "read(min(1_048_576,expected_size-received))" in _REMOTE_STAGE_SCRIPT
    assert 'os.open(archive_path,flags,0o600)' in _REMOTE_STAGE_SCRIPT
    assert "content=source.read()" not in _REMOTE_STAGE_SCRIPT
    assert "source.read(min(1_048_576,remaining))" in _REMOTE_STAGE_SCRIPT


def test_cli_accepts_explicit_run_plan_path() -> None:
    args = _parser().parse_args(
        ["run", "--run-plan", "/tmp/mycelium-controller/run-plan.json"]
    )

    assert args.command == "run"
    assert args.run_plan == "/tmp/mycelium-controller/run-plan.json"


def test_cli_peer_argument_requires_explicit_process_transport_and_identity(
    tmp_path: Path,
) -> None:
    identity_file = _private_identity_file(tmp_path)
    local_peer = _peer_argument(
        "node-0,operator@peer.example,host-0,boot-0,"
        "/tmp/mycelium-controller/run-a/node-0,local,-"
    )
    remote_peer = _peer_argument(
        "node-1,operator@peer.example,host-1,boot-1,"
        f"/tmp/mycelium-controller/run-a/node-1,ssh,{identity_file}"
    )

    assert local_peer.process_transport == "local"
    assert local_peer.ssh_identity_file is None
    assert remote_peer.process_transport == "ssh"
    assert remote_peer.ssh_identity_file == str(identity_file)
    with pytest.raises(ControllerError, match="invalid_arguments"):
        _peer_argument(
            "node-0,operator@peer.example,host-0,boot-0,"
            "/tmp/mycelium-controller/run-a/node-0"
        )
    with pytest.raises(ControllerError, match="invalid_arguments"):
        _peer_argument(
            "node-0,operator@peer.example,host-0,boot-0,"
            "/tmp/mycelium-controller/run-a/node-0,local,/tmp/identity"
        )
    with pytest.raises(ControllerError, match="invalid_arguments"):
        _peer_argument(
            "node-1,operator@peer.example,host-1,boot-1,"
            "/tmp/mycelium-controller/run-a/node-1,ssh,-"
        )


def test_cli_bare_dry_run_preflight_is_inert_and_route_false(
    capsys: Any,
) -> None:
    status = main(["preflight", "--dry-run"])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    result = json.loads(captured.out)
    assert result["command"] == "preflight"
    assert result["mode"] == "dry-run"
    assert result["peer_count"] == 0
    assert result["actions"] == []
    assert result["route_ready"] is False
    assert result["release_ready"] is False
    assert result["physical_execution"] is False


def test_cli_rejects_operator_supplied_endpoint_identity(capsys: Any) -> None:
    status = main(
        [
            "run",
            "--dry-run",
            "--peers",
            "node-0,user@host,host-0,boot-0,/tmp/mycelium/run/node-0",
            "--expected-endpoint-id",
            "forged",
        ]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert "expected-endpoint-id" not in captured.out
    error = json.loads(captured.err)
    assert error["error"]["code"] == "invalid_arguments"


def test_subprocess_runner_is_argv_only_and_separates_output_streams(
    tmp_path: Path,
) -> None:
    runner = SubprocessRunner()
    sentinel = tmp_path / "must-not-exist"
    injected = f"; touch {sentinel}"
    capture = runner.run(
        (
            sys.executable,
            "-c",
            "import sys;sys.stdout.buffer.write(b'out');sys.stderr.buffer.write(b'err')",
            injected,
        ),
        timeout_seconds=10.0,
    )

    assert capture.returncode == 0
    assert capture.stdout == b"out"
    assert capture.stderr == b"err"
    assert capture.argv[-1] == injected
    assert not sentinel.exists()


def test_subprocess_runner_allows_bounded_large_archive_timeout() -> None:
    runner = SubprocessRunner()

    capture = runner.run(
        (sys.executable, "-c", "pass"),
        timeout_seconds=735.5,
    )

    assert capture.returncode == 0
    with pytest.raises(ControllerError, match="runner_arguments_invalid"):
        runner.run(
            (sys.executable, "-c", "pass"),
            timeout_seconds=21_600.1,
        )
