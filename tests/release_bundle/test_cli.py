from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from mycelium_release_bundle import canonical_output, sha256_bytes, verify_bundle

from .conftest import MANIFEST_FILENAME, canonical_bytes, replace_artifact

SUMMARY_PATH = "qualification/synthetic-summary.json"


def _run_cli(root: Path, *extra: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mycelium_release_bundle",
            "verify",
            str(root),
            *extra,
        ],
        check=False,
        capture_output=True,
    )


def test_cli_emits_byte_identical_canonical_machine_output(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, _manifest = synthetic_bundle

    first = _run_cli(root)
    second = _run_cli(root)

    assert first.returncode == 0
    assert first.stderr == b""
    assert first.stdout == second.stdout
    assert first.stdout == canonical_output(verify_bundle(root))
    document = json.loads(first.stdout)
    assert document["ok"] is True
    assert document["route_ready"] is False
    assert document["release_ready"] is False
    assert document["physical_evidence_accepted"] is False


def test_cli_can_pin_exact_manifest_sha256(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, _manifest = synthetic_bundle
    digest = sha256_bytes((root / MANIFEST_FILENAME).read_bytes())

    accepted = _run_cli(root, "--expected-manifest-sha256", digest)
    rejected = _run_cli(root, "--expected-manifest-sha256", "sha256:" + "f" * 64)

    assert accepted.returncode == 0
    assert json.loads(accepted.stdout)["ok"] is True
    assert rejected.returncode == 1
    assert json.loads(rejected.stdout)["findings"] == [
        {"code": "expected_manifest_sha256_mismatch", "subject": "manifest"}
    ]
    assert rejected.stderr == b""


def test_cli_failure_output_redacts_bundle_path_and_secret_content(
    synthetic_bundle: tuple[Path, dict[str, Any]],
) -> None:
    root, manifest = synthetic_bundle
    secret = "cli-must-never-echo-this-password"
    summary = json.loads((root / SUMMARY_PATH).read_text(encoding="utf-8"))
    summary["password"] = secret
    replace_artifact(root, manifest, SUMMARY_PATH, canonical_bytes(summary))

    completed = _run_cli(root)

    assert completed.returncode == 1
    assert completed.stderr == b""
    assert secret.encode("utf-8") not in completed.stdout
    assert str(root).encode("utf-8") not in completed.stdout
    assert json.loads(completed.stdout)["findings"] == [
        {"code": "forbidden_bundle_content", "subject": "artifact:0002"}
    ]


def test_cli_unavailable_bundle_does_not_echo_sensitive_path(tmp_path: Path) -> None:
    unavailable = tmp_path / "credential-never-echo" / "missing"

    completed = _run_cli(unavailable)

    assert completed.returncode == 1
    assert completed.stderr == b""
    assert str(unavailable).encode("utf-8") not in completed.stdout
    result = json.loads(completed.stdout)
    assert result["ok"] is False
    assert result["route_ready"] is False
    assert result["release_ready"] is False
