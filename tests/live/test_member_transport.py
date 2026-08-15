from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mycelium_live.member_transport import (
    MemberArtifactTransport,
    RUNTIME_MANIFEST_PROTOCOL,
    TRANSPORT_PLAN_PROTOCOL,
    _REMOTE_INSTALL_OBJECT_SCRIPT,
    _REMOTE_REGISTER_MANIFEST_SCRIPT,
    _REMOTE_STAGE_SCRIPT,
    _member_execution_status,
)
from mycelium_live.preparation import ModelPreparationError
from mycelium_node.identity import load_or_create_node_signer
from mycelium_swarm_artifacts import canonical_digest


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _plan(tmp_path: Path) -> Path:
    identity = tmp_path / "provisioner.key"
    load_or_create_node_signer(identity, endpoint_id="artifact-provisioner")
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("test-only-ca\n")
    availability = tmp_path / "availability.json"
    availability.write_bytes(_canonical({"private": "source-projection"}))
    manifest_inbox = tmp_path / "manifest-inbox"
    manifest_inbox.mkdir(mode=0o700)
    object_store = tmp_path / "objects"
    object_store.mkdir(mode=0o700)
    runtime_manifest = tmp_path / "runtime.json"
    runtime_manifest.write_bytes(
        _canonical(
            {
                "protocol": RUNTIME_MANIFEST_PROTOCOL,
                "files": [
                    {
                        "path": "mycelium_live/member_artifact_provisioner.py",
                        "size_bytes": 10,
                        "content_digest": "sha256:" + "a" * 64,
                    }
                ],
            }
        )
    )
    plan = {
        "protocol": TRANSPORT_PLAN_PROTOCOL,
        "provisioner_generation": 1,
        "provisioner_identity_key_file": str(identity),
        "tls_ca_file": str(ca_file),
        "predicted_improvement_ratio": 0.5,
        "serving_reserve_satisfied": True,
        "sources": [
            {
                "member_id": "source-a",
                "membership_generation": 4,
                "endpoint": "https://source-a.example:9443",
                "verification_key": {
                    "algorithm": "ed25519",
                    "encoding": "base64",
                    "verification_key": "test",
                    "verification_key_digest": "sha256:" + "b" * 64,
                },
                "control": {"transport": "local"},
                "python_executable": "/usr/bin/python3",
                "object_store_root": str(object_store),
                "manifest_inbox_directory": str(manifest_inbox),
                "availability_bundle_file": str(availability),
            }
        ],
        "recipients": {
            "node-1": {
                "artifact_store_root": "/private/mycelium/artifacts",
                "job_root": "/private/mycelium/jobs",
                "recipient_identity_key_file": "/private/mycelium/node.key",
                "python_executable": "/usr/bin/python3",
                "python_path_root": "/private/mycelium/runtime",
                "runtime_manifest_file": str(runtime_manifest),
            }
        },
    }
    path = tmp_path / "transport-plan.json"
    path.write_bytes(_canonical(plan))
    path.chmod(0o600)
    return path


def test_transport_plan_is_private_closed_and_https_only(tmp_path: Path) -> None:
    path = _plan(tmp_path)

    transport = MemberArtifactTransport(path, clock_unix_ms=lambda: 1_000)

    assert transport._plan["protocol"] == TRANSPORT_PLAN_PROTOCOL
    changed = json.loads(path.read_text())
    changed["sources"][0]["endpoint"] = "http://source-a.example:9443"
    path.write_bytes(_canonical(changed))
    path.chmod(0o600)
    with pytest.raises(
        ModelPreparationError,
        match="member_artifact_source_plan_invalid",
    ):
        MemberArtifactTransport(path)


def test_source_ssh_control_is_closed_port_bound_and_argv_only(tmp_path: Path) -> None:
    path = _plan(tmp_path)
    plan = json.loads(path.read_text())
    plan["sources"][0]["control"] = {
        "transport": "ssh",
        "target": "source@example.test",
        "port": 8022,
        "identity_file": plan["provisioner_identity_key_file"],
    }
    path.write_bytes(_canonical(plan))
    path.chmod(0o600)

    transport = MemberArtifactTransport(path)
    source = transport._plan["sources"][0]
    command = transport._source_argv(source, ("/usr/bin/python3", "-c", "pass"))

    assert command[:10] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=15",
        "-p",
    ]
    assert command[10:14] == [
        "8022",
        "-i",
        plan["provisioner_identity_key_file"],
        "--",
    ]
    assert command[14:] == [
        "source@example.test",
        "/usr/bin/python3 -c pass",
    ]

    plan["sources"][0]["control"]["port"] = 0
    path.write_bytes(_canonical(plan))
    path.chmod(0o600)
    with pytest.raises(
        ModelPreparationError, match="member_artifact_source_control_invalid"
    ):
        MemberArtifactTransport(path)


def test_transport_plan_rejects_unbound_runtime_manifest(tmp_path: Path) -> None:
    path = _plan(tmp_path)
    plan = json.loads(path.read_text())
    runtime_path = Path(plan["recipients"]["node-1"]["runtime_manifest_file"])
    runtime = json.loads(runtime_path.read_text())
    runtime["files"][0]["content_digest"] = "sha256:" + "z" * 64
    runtime_path.write_bytes(_canonical(runtime))

    with pytest.raises(
        ModelPreparationError,
        match="member_artifact_runtime_manifest_invalid",
    ):
        MemberArtifactTransport(path)


def test_remote_execution_propagates_only_canonical_bounded_failure() -> None:
    failure = {
        "protocol": "mycelium.member_artifact_acquisition_failure.v1",
        "reason_code": "insufficient_disk",
    }
    with pytest.raises(ModelPreparationError, match="insufficient_disk"):
        _member_execution_status(
            subprocess.CompletedProcess([], 2, _canonical(failure), b"")
        )

    malformed = json.dumps(failure).encode() + b"\n"
    with pytest.raises(
        ModelPreparationError, match="member_artifact_job_execution_failed"
    ):
        _member_execution_status(subprocess.CompletedProcess([], 2, malformed, b""))

    with pytest.raises(
        ModelPreparationError, match="member_artifact_job_execution_failed"
    ):
        _member_execution_status(
            subprocess.CompletedProcess([], 2, _canonical(failure), b"private detail")
        )


def test_remote_job_stage_is_digest_bound_and_private(tmp_path: Path) -> None:
    files = {
        "grant.json": b"{}\n",
        "job.json": b"{}\n",
        "manifest.json": b"{}\n",
    }
    archive = MemberArtifactTransport._archive(files)
    digest = "sha256:" + hashlib.sha256(archive).hexdigest()
    remote = tmp_path / "remote" / "mycelium-job" / "one"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _REMOTE_STAGE_SCRIPT,
            str(remote),
            digest,
            str(len(archive)),
        ],
        input=archive,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert json.loads(completed.stdout)["archive_digest"] == digest
    assert (remote / "grant.json").stat().st_mode & 0o777 == 0o600
    assert (remote / "manifest.json").stat().st_mode & 0o777 == 0o400


def test_remote_manifest_registration_is_digest_bound_and_private(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "manifest-inbox"
    inbox.mkdir(mode=0o700)
    unsigned = {"protocol": "test.manifest.v1"}
    digest = canonical_digest(unsigned)
    manifest = {**unsigned, "manifest_digest": digest}
    encoded = _canonical(manifest)
    name = digest.removeprefix("sha256:") + ".json"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _REMOTE_REGISTER_MANIFEST_SCRIPT,
            str(inbox),
            name,
            digest,
            str(len(encoded)),
        ],
        input=encoded,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == b""
    assert json.loads(completed.stdout)["content_digest"] == digest
    registered = inbox / name
    assert registered.read_bytes() == encoded
    assert registered.stat().st_mode & 0o777 == 0o400

    rejected = subprocess.run(
        [
            sys.executable,
            "-c",
            _REMOTE_REGISTER_MANIFEST_SCRIPT,
            str(inbox),
            name,
            digest,
            str(len(encoded)),
        ],
        input=_canonical({**manifest, "protocol": "substituted"}),
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert rejected.stderr == b"member_manifest_registration_rejected\n"
    assert registered.read_bytes() == encoded


def test_remote_source_object_install_is_digest_bound_and_idempotent(
    tmp_path: Path,
) -> None:
    objects = tmp_path / "objects"
    objects.mkdir(mode=0o700)
    payload = b"assignment-local-chunk"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    command = [
        sys.executable,
        "-c",
        _REMOTE_INSTALL_OBJECT_SCRIPT,
        str(objects),
        digest,
        str(len(payload)),
    ]

    installed = subprocess.run(
        command,
        input=payload,
        capture_output=True,
        check=False,
    )
    reused = subprocess.run(
        command,
        input=payload,
        capture_output=True,
        check=False,
    )

    assert installed.returncode == reused.returncode == 0
    assert installed.stdout == reused.stdout == (digest + "\n").encode()
    assert installed.stderr == reused.stderr == b""
    stored = objects / digest.removeprefix("sha256:")
    assert stored.read_bytes() == payload
    assert stored.stat().st_mode & 0o777 == 0o400

    rejected = subprocess.run(
        [*command[:-1], str(len(payload) + 1)],
        input=payload,
        capture_output=True,
        check=False,
    )
    assert rejected.returncode == 2
    assert stored.read_bytes() == payload


def test_source_seeding_replicates_each_chunk_to_every_authorized_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = object.__new__(MemberArtifactTransport)
    transport._plan = {
        "sources": [
            {"member_id": "source-a", "control": {"transport": "local"}},
            {"member_id": "source-b", "control": {"transport": "local"}},
        ]
    }
    stage_source = tmp_path / "stage"
    objects = stage_source / "objects"
    objects.mkdir(parents=True)
    payloads = (b"chunk-a", b"chunk-b")
    chunks = []
    for index, payload in enumerate(payloads):
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        (objects / digest.removeprefix("sha256:")).write_bytes(payload)
        chunks.append(
            {"index": index, "content_digest": digest, "size_bytes": len(payload)}
        )
    installed: list[tuple[str, str]] = []

    def capture_install(**kwargs) -> None:
        installed.append((kwargs["source"]["member_id"], kwargs["digest"]))

    monkeypatch.setattr(transport, "_install_source_object", capture_install)

    transport._seed_source_objects(
        manifest={"chunks": chunks},
        stage_source=stage_source,
    )

    assert installed == [
        ("source-a", chunks[0]["content_digest"]),
        ("source-b", chunks[0]["content_digest"]),
        ("source-a", chunks[1]["content_digest"]),
        ("source-b", chunks[1]["content_digest"]),
    ]
