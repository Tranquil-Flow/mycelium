from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
from typing import Any

import pytest

from mycelium_qualification.qualifier import QualificationError
from mycelium_qualification.sealer import (
    EVIDENCE_MANIFEST_NAME,
    MAX_SEALED_FILE_BYTES,
    REQUIRED_AUTHORITY_DOCUMENTS,
    EvidenceSealingError,
    qualify_sealed_evidence,
    seal_physical_evidence,
)
from mycelium_qualification.signing import (
    build_ed25519_verifier,
    generate_ed25519_signer,
)


def _real_signatures(case: Any):
    public_keys: list[dict[str, str]] = []
    gossip = case.documents["control/gossip-signature.json"]
    gossip_statement = gossip["statement"]
    gossip_endpoint = gossip_statement["peers"][0]["endpoint_id"]
    gossip_signer = generate_ed25519_signer(endpoint_id=gossip_endpoint)
    gossip["signature"] = gossip_signer.sign(gossip_statement)
    public_keys.append(gossip_signer.public_key_record())

    signatures = case.documents["runtime/load-proof-signatures.json"]["signatures"]
    for item in signatures:
        endpoint = item["statement"]["endpoint_id"]
        signer = generate_ed25519_signer(endpoint_id=endpoint)
        item["signature"] = signer.sign(item["statement"])
        public_keys.append(signer.public_key_record())
    return build_ed25519_verifier(public_keys)


def _seal_case(case: Any, root: Path):
    verifier = _real_signatures(case)
    sealed = seal_physical_evidence(
        output_dir=root,
        run_id=case.documents["run/route-challenge.json"]["run_id"],
        documents=case.documents,
        extra_files=case.extra_files,
    )
    return sealed, verifier


def test_sealer_writes_exact_canonical_read_only_tree_and_qualifies(
    qualification_case: Any, tmp_path: Path
) -> None:
    root = tmp_path / "sealed"
    sealed, verifier = _seal_case(qualification_case, root)

    assert sealed.root == root
    assert sealed.manifest_digest.startswith("sha256:")
    assert sealed.file_count == len(qualification_case.documents) + len(
        qualification_case.extra_files
    )
    assert {path for path in qualification_case.documents} == REQUIRED_AUTHORITY_DOCUMENTS
    assert (root / EVIDENCE_MANIFEST_NAME).is_file()
    assert stat.S_IMODE(root.stat().st_mode) & 0o222 == 0
    for path in qualification_case.documents:
        content = (root / path).read_bytes()
        assert not content.endswith(b"\n")
        assert stat.S_IMODE((root / path).stat().st_mode) & 0o222 == 0

    record = qualify_sealed_evidence(
        sealed,
        now_unix_ms=qualification_case.now_unix_ms,
        verify_gossip_signature=verifier,
        verify_load_proof_signature=verifier,
    )
    assert record.route_ready is True
    assert record.evidence_class == "physical_qualification"
    assert record.reason_codes == ()


def test_sealer_requires_exact_authority_documents_and_physical_identity(
    qualification_case: Any, tmp_path: Path
) -> None:
    missing = copy.deepcopy(qualification_case.documents)
    missing.pop("run/negative-runs.json")
    with pytest.raises(EvidenceSealingError, match="missing_authority_document"):
        seal_physical_evidence(
            output_dir=tmp_path / "missing",
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=missing,
            extra_files=qualification_case.extra_files,
        )
    assert not (tmp_path / "missing").exists()

    extra_document = copy.deepcopy(qualification_case.documents)
    extra_document["run/unapproved.json"] = {}
    with pytest.raises(EvidenceSealingError, match="unknown_authority_document"):
        seal_physical_evidence(
            output_dir=tmp_path / "extra",
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=extra_document,
            extra_files=qualification_case.extra_files,
        )

    challenge = copy.deepcopy(qualification_case.documents)
    challenge["run/route-challenge.json"]["evidence_class"] = "synthetic_test_fixture"
    with pytest.raises(EvidenceSealingError, match="nonphysical_evidence"):
        seal_physical_evidence(
            output_dir=tmp_path / "synthetic",
            run_id=challenge["run/route-challenge.json"]["run_id"],
            documents=challenge,
            extra_files=qualification_case.extra_files,
        )


def test_sealer_rejects_unsafe_paths_oversize_files_existing_targets_and_symlinks(
    qualification_case: Any, tmp_path: Path
) -> None:
    with pytest.raises(EvidenceSealingError, match="unsafe_evidence_path"):
        seal_physical_evidence(
            output_dir=tmp_path / "unsafe",
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=qualification_case.documents,
            extra_files={"../escape": b"x"},
        )

    with pytest.raises(EvidenceSealingError, match="evidence_file_too_large"):
        seal_physical_evidence(
            output_dir=tmp_path / "oversize",
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=qualification_case.documents,
            extra_files={"raw/oversize.bin": b"x" * (MAX_SEALED_FILE_BYTES + 1)},
        )

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(EvidenceSealingError, match="evidence_output_exists"):
        seal_physical_evidence(
            output_dir=existing,
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=qualification_case.documents,
            extra_files=qualification_case.extra_files,
        )

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(EvidenceSealingError, match="evidence_output_symlink"):
        seal_physical_evidence(
            output_dir=symlink,
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=qualification_case.documents,
            extra_files=qualification_case.extra_files,
        )


def test_manifest_is_written_last_after_evidence_file_syncs(
    qualification_case: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mycelium_qualification import sealer as sealer_module

    calls: list[str] = []
    original = sealer_module._write_new_file

    def observing_write(path: Path, content: bytes) -> None:
        original(path, content)
        calls.append(path.relative_to(tmp_path / "sealed").as_posix())

    monkeypatch.setattr(sealer_module, "_write_new_file", observing_write)
    _seal_case(qualification_case, tmp_path / "sealed")

    assert calls[-1] == EVIDENCE_MANIFEST_NAME
    assert set(calls[:-1]) == set(qualification_case.documents) | set(
        qualification_case.extra_files
    )


def test_tampered_unlisted_or_symlinked_sealed_bytes_fail_before_qualification(
    qualification_case: Any, tmp_path: Path
) -> None:
    sealed, verifier = _seal_case(qualification_case, tmp_path / "tampered")
    challenged = sealed.root / "run/route-challenge.json"
    os.chmod(challenged, 0o644)
    challenged.write_bytes(b"{}")
    with pytest.raises(EvidenceSealingError, match="evidence_file_(size|digest)_mismatch"):
        qualify_sealed_evidence(
            sealed,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=verifier,
            verify_load_proof_signature=verifier,
        )

    other = qualification_case.clone()
    sealed, verifier = _seal_case(other, tmp_path / "unlisted")
    os.chmod(sealed.root, 0o755)
    (sealed.root / "unlisted.json").write_bytes(b"{}")
    with pytest.raises(EvidenceSealingError, match="sealed_file_set_mismatch"):
        qualify_sealed_evidence(
            sealed,
            now_unix_ms=other.now_unix_ms,
            verify_gossip_signature=verifier,
            verify_load_proof_signature=verifier,
        )

    third = qualification_case.clone()
    sealed, verifier = _seal_case(third, tmp_path / "symlinked")
    victim = sealed.root / "run/negative-runs.json"
    os.chmod(sealed.root, 0o755)
    os.chmod(victim.parent, 0o755)
    victim.unlink()
    victim.symlink_to(sealed.root / "run/route-challenge.json")
    with pytest.raises(EvidenceSealingError, match="sealed_tree_symlink"):
        qualify_sealed_evidence(
            sealed,
            now_unix_ms=third.now_unix_ms,
            verify_gossip_signature=verifier,
            verify_load_proof_signature=verifier,
        )


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("simulator_participated", "simulator_participation"),
        ("fixture_port_participated", "fixture_participation"),
        ("synthetic_timing", "synthetic_timing"),
    ],
)
def test_sealed_fixture_simulator_and_synthetic_timing_claims_never_promote(
    qualification_case: Any,
    tmp_path: Path,
    field: str,
    code: str,
) -> None:
    qualification_case.documents["run/route-challenge.json"]["transport"][field] = True
    sealed, verifier = _seal_case(qualification_case, tmp_path / field)

    with pytest.raises(QualificationError, match=code):
        qualify_sealed_evidence(
            sealed,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=verifier,
            verify_load_proof_signature=verifier,
        )


def test_sealer_rejects_noncanonical_json_shapes_before_creating_output(
    qualification_case: Any, tmp_path: Path
) -> None:
    documents = copy.deepcopy(qualification_case.documents)
    nested: object = None
    for _ in range(60):
        nested = [nested]
    documents["run/route-challenge.json"]["unexpected_nested_value"] = nested

    with pytest.raises(EvidenceSealingError, match="noncanonical_json"):
        seal_physical_evidence(
            output_dir=tmp_path / "deep",
            run_id=documents["run/route-challenge.json"]["run_id"],
            documents=documents,
            extra_files=qualification_case.extra_files,
        )
    assert not (tmp_path / "deep").exists()

    with pytest.raises(EvidenceSealingError, match="noncanonical_evidence_json"):
        seal_physical_evidence(
            output_dir=tmp_path / "raw-observation",
            run_id=qualification_case.documents["run/route-challenge.json"]["run_id"],
            documents=qualification_case.documents,
            extra_files={
                **qualification_case.extra_files,
                "observations/node.json": b'{"z": 1, "a": 2}',
            },
        )
    assert not (tmp_path / "raw-observation").exists()
