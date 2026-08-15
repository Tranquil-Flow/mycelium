from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from mycelium_member_runtime import (
    MemberRuntimeError,
    RUNTIME_MANIFEST_PROTOCOL,
    RUNTIME_PATHS,
    build_member_runtime,
)


ROOT = Path(__file__).resolve().parents[2]


def test_member_runtime_is_closed_hashed_and_importable(tmp_path: Path) -> None:
    output = tmp_path / "runtime"
    output.mkdir(mode=0o700)
    manifest_path = output / "runtime-manifest.json"

    manifest = build_member_runtime(
        repo_root=ROOT,
        output_root=output,
        manifest_path=manifest_path,
    )

    assert manifest["protocol"] == RUNTIME_MANIFEST_PROTOCOL
    assert [item["path"] for item in manifest["files"]] == list(RUNTIME_PATHS)
    assert json.loads(manifest_path.read_text("utf-8")) == manifest
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    for item in manifest["files"]:
        payload = (output / item["path"]).read_bytes()
        assert item["size_bytes"] == len(payload)
        assert item["content_digest"] == "sha256:" + hashlib.sha256(payload).hexdigest()
        assert stat.S_IMODE((output / item["path"]).stat().st_mode) == 0o400
    assert b"from .authority" not in (
        output / "mycelium_qualification/__init__.py"
    ).read_bytes()
    assert b"from .membership" not in (
        output / "mycelium_node/__init__.py"
    ).read_bytes()

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mycelium_live.member_artifact_provisioner as member; print(member.__name__)",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(output), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout == "mycelium_live.member_artifact_provisioner\n"
    assert probe.stderr == ""


def test_member_runtime_rejects_manifest_outside_runtime_root(tmp_path: Path) -> None:
    output = tmp_path / "runtime"
    output.mkdir(mode=0o700)
    with pytest.raises(MemberRuntimeError, match="member_runtime_manifest_path_unsafe"):
        build_member_runtime(
            repo_root=ROOT,
            output_root=output,
            manifest_path=tmp_path / "runtime-manifest.json",
        )
