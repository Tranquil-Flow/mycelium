# SPDX-License-Identifier: AGPL-3.0-only
"""The shared A13 packaging core: both platform wrappers are thin over one engine."""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from mycelium_a13_linux import install_source as linux_source, package_manifest as linux_manifest
from mycelium_a13_packaging import build, install_source as core_source, manifest as core_manifest
from mycelium_a13_windows import install_source as windows_source, package_manifest as windows_manifest


def test_platform_wrappers_share_one_error_type_and_binding_set():
    assert linux_manifest.PackageManifestError is windows_manifest.PackageManifestError is core_manifest.PackageManifestError
    assert linux_manifest.BINDINGS == windows_manifest.BINDINGS == core_manifest.BINDINGS
    assert linux_source.InstallSourceError is windows_source.InstallSourceError is core_source.InstallSourceError
    assert linux_source.EXPERT_DEPENDENCIES == windows_source.EXPERT_DEPENDENCIES
    assert windows_manifest.PROFILE.descriptor_name_field == "service_name"
    assert linux_manifest.PROFILE.descriptor_name_field == "unit_name"
    assert "authenticode_subject" in windows_manifest.PROFILE.publisher_fields
    assert "authenticode_subject" not in linux_manifest.PROFILE.publisher_fields


def test_non_string_source_revision_is_a_coded_refusal_on_both_platforms():
    for validate in (linux_manifest.validate_linux_package_manifest,
                     windows_manifest.validate_windows_package_manifest):
        document = {name: None for name in core_manifest.DOCUMENT_FIELDS}
        document["protocol"] = core_manifest.PROTOCOL
        document["source_revision"] = 12345
        with pytest.raises(core_manifest.PackageManifestError) as refused:
            validate(document)
        assert refused.value.code == "package_source_revision_invalid"


def test_deterministic_tar_gz_and_payload_digest(tmp_path: Path):
    tree = tmp_path / "app"
    (tree / "_internal").mkdir(parents=True)
    (tree / "mycelium").write_bytes(b"\x7fELF")
    (tree / "mycelium").chmod(0o755)
    (tree / "_internal" / "lib.so").write_bytes(b"lib")
    first = build.deterministic_tar_gz(tree, tmp_path / "a.tar.gz", prefix="mycelium-0.1.0")
    second = build.deterministic_tar_gz(tree, tmp_path / "b.tar.gz", prefix="mycelium-0.1.0")
    assert first == second
    with tarfile.open(fileobj=io.BytesIO(gzip.decompress(first))) as archive:
        members = {m.name: m for m in archive.getmembers()}
    assert members["mycelium-0.1.0/mycelium"].mode == 0o755
    assert members["mycelium-0.1.0/_internal/lib.so"].mode == 0o644
    assert all(m.mtime == 0 and m.uid == 0 and m.uname == "" for m in members.values())
    digest = build.payload_digest(tree)
    (tree / "_internal" / "lib.so").write_bytes(b"lib!")
    assert build.payload_digest(tree) != digest
    (tree / "link").symlink_to(tree / "mycelium")
    with pytest.raises(build.PayloadError):
        build.payload_digest(tree)
