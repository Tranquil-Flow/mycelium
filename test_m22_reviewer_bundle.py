from pathlib import Path

import pytest

from mycelium_reviewer_bundle import build_reviewer_bundle, verify_reviewer_bundle
from scripts.build_astra_reviewer_bundle import _runtime_files


def test_reviewer_bundle_is_deterministic_credential_free_and_verified(
    tmp_path: Path,
) -> None:
    script = tmp_path / "preflight.py"
    script.write_text("print('preflight')\n")
    sidecar = tmp_path / "sidecar"
    sidecar.write_bytes(b"binary")
    sidecar.chmod(0o700)
    first = tmp_path / "first.tar"
    second = tmp_path / "second.tar"
    files = {"scripts/preflight.py": script, "bin/sidecar": sidecar}
    manifest = build_reviewer_bundle(
        version="astras-macbook-m22-1", files=files, output=first
    )
    build_reviewer_bundle(
        version="astras-macbook-m22-1",
        files=dict(reversed(tuple(files.items()))),
        output=second,
    )
    assert first.read_bytes() == second.read_bytes()
    assert manifest["contains_credentials"] is False
    assert manifest["contains_invitation"] is False
    assert manifest["tailscale_required"] is False
    assert verify_reviewer_bundle(first) == manifest


def test_reviewer_bundle_detects_payload_drift(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    bundle = tmp_path / "bundle.tar"
    build_reviewer_bundle(
        version="reviewer-v1", files={"source": source}, output=bundle
    )
    raw = bytearray(bundle.read_bytes())
    raw[1024] ^= 1
    bundle.write_bytes(raw)
    with pytest.raises((ValueError, OSError)):
        verify_reviewer_bundle(bundle)


def test_reviewer_runtime_includes_durable_service_packager(tmp_path: Path) -> None:
    sidecar = tmp_path / "sidecar"
    sidecar.write_bytes(b"binary")

    files = _runtime_files(sidecar)

    assert files["scripts/package_m22_service.py"].is_file()
    assert files["scripts/astra_reviewer_preflight.py"].is_file()
    assert files["bin/mycelium-iroh-sidecar"] == sidecar
