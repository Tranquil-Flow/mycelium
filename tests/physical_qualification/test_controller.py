from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
from typing import Any

import pytest

from mycelium_membership.contracts import (
    ASSIGNMENT_OFFER_PROTOCOL,
    sign_membership_message,
)
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_qualification import (
    COMMANDS,
    CommandCapture,
    ControllerError,
    PeerIdentity,
    QualificationController,
    SubprocessRunner,
    _REMOTE_CLEANUP_SCRIPT,
    _REMOTE_STAGE_SCRIPT,
    build_transfer_archive,
    main,
)

NOW = 10_000.0
EPOCH = 9
DIGEST = "sha256:" + "a" * 64


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def run(self, argv: tuple[str, ...], *, timeout_seconds: float) -> object:
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


def _peers(count: int, *, same_host: bool = False) -> tuple[PeerIdentity, ...]:
    values = []
    for index in range(count):
        values.append(
            PeerIdentity(
                node_id=f"node-{index}",
                ssh_target=f"operator@peer-{index}.example",
                host_id="host-shared" if same_host else f"host-{index}",
                boot_id=f"boot-{index}",
                staging_root=f"/tmp/mycelium-controller/run-a/node-{index}",
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
    peers = _peers(count, same_host=same_host)
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


@pytest.mark.parametrize(
    "path",
    ["../escape.py", ".git/config", ".env", ".cache/model.bin", "models/weights.bin", "private-key.pem"],
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
    peers = _peers(3)
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
    peers = _peers(3)
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


def test_physical_mode_requires_distinct_host_and_boot_then_stays_blocked(
    tmp_path: Path,
) -> None:
    same_host, runner = _controller(
        tmp_path / "same", mode="physical", same_host=True
    )
    with pytest.raises(ControllerError, match="physical_host_identity_not_distinct"):
        same_host.execute("run")
    assert runner.calls == []

    distinct, runner = _controller(tmp_path / "distinct", mode="physical")
    with pytest.raises(ControllerError, match="physical_execution_not_implemented"):
        distinct.execute("run")
    assert runner.calls == []


def test_physical_prepare_streams_verified_archive_and_requires_bound_acknowledgements(
    tmp_path: Path,
) -> None:
    peers = _peers(2)
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


def test_physical_prepare_cleans_attempted_peers_when_staging_fails(
    tmp_path: Path,
) -> None:
    peers = _peers(2)
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
