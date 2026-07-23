from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from typing import Any

import pytest

from mycelium_membership.contracts import (
    ASSIGNMENT_OFFER_PROTOCOL,
    sign_membership_message,
)
from mycelium_qualification.signing import generate_ed25519_signer
from physical_inference_qualification import (
    COMMANDS,
    ControllerError,
    PeerIdentity,
    QualificationController,
    SubprocessRunner,
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
