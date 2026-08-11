from __future__ import annotations

import hashlib
from pathlib import Path
import shutil

import mycelium_reviewer_preflight as preflight


class _Client:
    def identity(self, *, now: float):
        return {"verified": True}


def test_reviewer_preflight_is_read_only_idempotent_and_privacy_reduced(
    tmp_path: Path, monkeypatch
) -> None:
    artifact = tmp_path / "stage.bin"
    artifact.write_bytes(b"assigned bytes")
    digest = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    monkeypatch.setattr(
        preflight,
        "verify_invite_bundle",
        lambda _bundle, now: {"payload": {"swarm_id": "private-swarm"}},
    )
    monkeypatch.setattr(
        preflight.SeedHTTPClient,
        "from_invite_bundle",
        lambda _bundle, now: _Client(),
    )
    monkeypatch.setattr(preflight, "_memory_bytes", lambda: 16 * 1024**3)
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(100 * 1024**3, 10, 90 * 1024**3),
    )
    monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda _name: object())
    requirements = {
        "files": [
            {
                "logical_name": "assigned-stage",
                "path": str(artifact),
                "size_bytes": artifact.stat().st_size,
                "sha256": digest,
            }
        ]
    }
    before = tuple(tmp_path.iterdir())
    first = preflight.reviewer_preflight(
        invite_bundle={"token": "never-output-this"},
        state_root=tmp_path / "absent-state",
        artifact_requirements=requirements,
        required_memory_bytes=8 * 1024**3,
        required_disk_bytes=8 * 1024**3,
        now=1_000,
    )
    second = preflight.reviewer_preflight(
        invite_bundle={"token": "never-output-this"},
        state_root=tmp_path / "absent-state",
        artifact_requirements=requirements,
        required_memory_bytes=8 * 1024**3,
        required_disk_bytes=8 * 1024**3,
        now=1_000,
    )
    assert first == second
    assert tuple(tmp_path.iterdir()) == before
    assert first["qualification"] == {
        "membership_ready": True,
        "activation_eligible": True,
        "route_qualified": False,
        "reason": "preflight_only_qualification_required",
    }
    assert first["artifacts"]["cache_reused"] is True
    assert first["state_mutated"] is False
    encoded = str(first)
    assert "never-output-this" not in encoded
    assert "private-swarm" not in encoded
    assert str(tmp_path) not in encoded


def test_preflight_reports_actionable_capacity_and_runtime_failures(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        preflight,
        "verify_invite_bundle",
        lambda _bundle, now: {"payload": {"swarm_id": "swarm"}},
    )

    class _Offline:
        def identity(self, *, now: float):
            raise ConnectionError

    monkeypatch.setattr(
        preflight.SeedHTTPClient,
        "from_invite_bundle",
        lambda _bundle, now: _Offline(),
    )
    monkeypatch.setattr(preflight, "_memory_bytes", lambda: 1024)
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: shutil._ntuple_diskusage(2048, 1024, 1024),
    )
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(preflight.importlib.util, "find_spec", lambda _name: None)
    result = preflight.reviewer_preflight(
        invite_bundle={"token": "private"},
        state_root=None,
        artifact_requirements=None,
        required_memory_bytes=2048,
        required_disk_bytes=2048,
        now=1_000,
    )
    assert result["qualification"]["activation_eligible"] is False
    assert result["failures"] == [
        "supported_apple_silicon_mac_required",
        "mlx_runtime_missing",
        "insufficient_memory",
        "insufficient_disk",
        "coordinator_unreachable_or_unverified",
    ]
