# SPDX-License-Identifier: AGPL-3.0-only
"""Gap A2 closed: ``scripts/build_a13_windows.py`` builds the pilot Windows bundle.

Mirrors ``tests/a13_macos/test_build_source_truth.py``'s style: import the
build script's own functions and exercise them directly (never PyInstaller,
never a real MSI toolchain -- there isn't one on this macOS host, and the
owner's pre-fire packet says a zip/staging layout is fine).
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
import build_a13_windows  # noqa: E402

from mycelium_a13_windows.install_source import (  # noqa: E402
    AUTHENTICODE_UNSIGNED_SUBJECT,
    verify_windows_install_source,
)
from mycelium_a13_windows.package_manifest import verify_windows_package  # noqa: E402
from mycelium_a13_windows.platform_admission import ADMITTED_RELEASES  # noqa: E402


def _write_test_key(path: Path) -> bytes:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = Ed25519PrivateKey.generate().private_bytes_raw()
    path.write_bytes(raw)
    return raw


def test_the_build_root_must_look_like_the_windows_app_tree() -> None:
    assert build_a13_windows.require_app_tree_root(REPO) == {"root": str(REPO)}


def test_a_foreign_build_root_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="build_root_not_the_app_tree"):
        build_a13_windows.require_app_tree_root(tmp_path)


def test_runtime_source_closure_covers_the_windows_tranche_and_its_real_dependencies() -> None:
    closure = build_a13_windows.runtime_source_closure(REPO)
    assert "mycelium_a13_windows/service_lifecycle.py" in closure
    assert "mycelium_a13_windows/install_source.py" in closure
    assert "mycelium_a13_windows/package_manifest.py" in closure
    assert "mycelium_a13_windows/platform_admission.py" in closure
    assert "mycelium_a13_windows/onboarding.py" in closure
    assert "mycelium_service_package.py" in closure
    # Real transitive deps observed by reading the modules (FINDINGS.md §3.1):
    # mycelium_qualification.evidence/signing, mycelium_invite, and the reused
    # mycelium_a13_linux.progress ladder.
    assert "mycelium_qualification/signing.py" in closure
    assert "mycelium_qualification/evidence.py" in closure
    assert "mycelium_invite/token.py" in closure
    assert "mycelium_a13_linux/progress.py" in closure
    # Nothing outside the repo, no symlink escape, no execution: it is pure
    # static analysis, so a second call on the same tree is byte-identical.
    assert closure == build_a13_windows.runtime_source_closure(REPO)


def test_load_pinned_signer_never_returns_or_leaks_the_raw_key(tmp_path: Path) -> None:
    key_path = tmp_path / "pilot-signing-key.raw"
    raw = _write_test_key(key_path)
    signer = build_a13_windows.load_pinned_signer(key_path, endpoint_id="test-signer")
    assert signer.endpoint_id == "test-signer"
    record = signer.public_key_record()
    assert record["verification_key_digest"] == signer.verification_key_digest
    # The signer's own public surface never exposes the private bytes.
    assert not hasattr(signer, "private_key_bytes")
    rendered = repr(signer) + json.dumps(record)
    assert raw.hex() not in rendered
    import base64

    assert base64.b64encode(raw).decode("ascii") not in rendered


def test_load_pinned_signer_rejects_a_wrong_length_key_file(tmp_path: Path) -> None:
    key_path = tmp_path / "bad-key.raw"
    key_path.write_bytes(b"too-short")
    with pytest.raises(SystemExit, match="signing_key_file_must_hold_32_raw_private_bytes"):
        build_a13_windows.load_pinned_signer(key_path, endpoint_id="test-signer")


def test_render_windows_service_member_produces_a_real_digest(tmp_path: Path) -> None:
    member = build_a13_windows.render_windows_service_member(tmp_path / "work")
    assert member["windows_member_rendered"] is True
    assert member["windows_member_sha256"].startswith("sha256:")
    # Per-user logon task, not SCM (see build_a13_windows.render_windows_service_member).
    assert member["windows_install_mode"] == "windows_service_per_user"


def test_build_windows_bundle_end_to_end_produces_a_verifiable_signed_bundle(
    tmp_path: Path,
) -> None:
    key_path = tmp_path / "pilot-signing-key.raw"
    raw = _write_test_key(key_path)
    output_root = tmp_path / "out"
    work_root = tmp_path / "work"

    report = build_a13_windows.build_windows_bundle(
        output_root=output_root,
        work_root=work_root,
        signing_key_path=key_path,
        version="0.0.0-test",
        root=REPO,
    )

    zip_path = output_root / report["artifact_name"]
    assert zip_path.is_file()
    zip_bytes = zip_path.read_bytes()
    assert report["artifact_digest"] == "sha256:" + hashlib.sha256(zip_bytes).hexdigest()
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert "mycelium_a13_windows/service_lifecycle.py" in names
        # Deterministic: fixed per-entry timestamp, not the build host's clock.
        info = archive.getinfo("mycelium_a13_windows/service_lifecycle.py")
        assert info.date_time == (1980, 1, 1, 0, 0, 0)

    install_source = json.loads((output_root / "install-source.json").read_text())
    verified_source = verify_windows_install_source(
        install_source,
        expected_publisher="mycelium-release",
        expected_authenticode_subject=AUTHENTICODE_UNSIGNED_SUBJECT,
        expected_architecture="x86_64",
        expected_windows_version=ADMITTED_RELEASES["windows"]["minimum_version"],
        expected_windows_build=ADMITTED_RELEASES["windows"]["minimum_build"],
        installed_version="0.1.0",
        rollback_floor_pin="0.1.0",
        now=2_000_000_100.0,
    )
    assert verified_source["verified"] is True
    assert verified_source["route_ready"] is False
    assert verified_source["authenticode_subject"] == AUTHENTICODE_UNSIGNED_SUBJECT

    manifest = json.loads((output_root / "installation-package-manifest.json").read_text())
    verified_manifest = verify_windows_package(
        manifest,
        expected_publisher="mycelium-release",
        expected_authenticode_subject=AUTHENTICODE_UNSIGNED_SUBJECT,
        expected_architecture="x86_64",
        expected_windows_version=ADMITTED_RELEASES["windows"]["minimum_version"],
        expected_windows_build=ADMITTED_RELEASES["windows"]["minimum_build"],
        installed_version="0.1.0",
        rollback_floor_pin="0.1.0",
        publisher_key_digest_pin=report["manifest_publisher_key_digest"],
        now=2_000_000_100.0,
    )
    assert verified_manifest["verified"] is True
    assert verified_manifest["route_ready"] is False
    assert verified_manifest["service_descriptor"]["sha256"] == report["windows_service_member"]["sha256"]

    readme = (output_root / "README-PILOT.txt").read_text()
    assert "Authenticode-UNSIGNED" in readme
    assert "owner_private_media" in readme

    identity = json.loads((output_root / "artifact-identity.json").read_text())
    assert identity["channel"] == "owner_private_media"
    for path in output_root.iterdir():
        if path.is_file():
            text = path.read_bytes()
            assert raw not in text
            assert raw.hex().encode("ascii") not in text

    with pytest.raises(SystemExit, match="fresh_output_root_required"):
        build_a13_windows.build_windows_bundle(
            output_root=output_root,
            work_root=tmp_path / "work2",
            signing_key_path=key_path,
            version="0.0.0-test",
            root=REPO,
        )


def test_a_tampered_manifest_is_refused_not_silently_accepted(tmp_path: Path) -> None:
    """Mutation-shaped negative control: this is the exact positive test, tampered."""
    key_path = tmp_path / "pilot-signing-key.raw"
    _write_test_key(key_path)
    report = build_a13_windows.build_windows_bundle(
        output_root=tmp_path / "out",
        work_root=tmp_path / "work",
        signing_key_path=key_path,
        version="0.0.0-test",
        root=REPO,
    )
    manifest_path = tmp_path / "out" / "installation-package-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["package_digest"] = "sha256:" + "0" * 64
    with pytest.raises(Exception):
        verify_windows_package(
            manifest,
            expected_publisher="mycelium-release",
            expected_authenticode_subject=AUTHENTICODE_UNSIGNED_SUBJECT,
            expected_architecture="x86_64",
            expected_windows_version=ADMITTED_RELEASES["windows"]["minimum_version"],
            expected_windows_build=ADMITTED_RELEASES["windows"]["minimum_build"],
            installed_version="0.1.0",
            rollback_floor_pin="0.1.0",
            publisher_key_digest_pin=report["manifest_publisher_key_digest"],
            now=2_000_000_100.0,
        )
