from pathlib import Path

import pytest

from mycelium_m22_sbom import build_sbom, file_component, validate_sbom, version_component


def test_sbom_is_deterministic_path_free_and_digest_bound(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"model bytes")
    components = [
        file_component(kind="model-artifact", name="model/file", path=artifact),
        version_component(kind="runtime", name="python", version="3.14.4"),
    ]
    first = build_sbom(revision="abc123", components=components)
    second = build_sbom(revision="abc123", components=list(reversed(components)))
    assert first == second
    assert str(tmp_path) not in str(first)
    assert validate_sbom(first) == first


def test_sbom_rejects_duplicate_identity_and_digest_drift(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.bin"
    artifact.write_bytes(b"model bytes")
    item = file_component(kind="binary", name="sidecar", path=artifact)
    with pytest.raises(ValueError, match="duplicate"):
        build_sbom(revision="abc123", components=[item, item])
    document = build_sbom(revision="abc123", components=[item])
    document["network_download_performed"] = True
    with pytest.raises(ValueError, match="m22_sbom_invalid"):
        validate_sbom(document)
