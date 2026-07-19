from __future__ import annotations

import base64

import pytest

from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_qualification.signing import (
    SIGNATURE_FIELDS,
    EvidenceSigningError,
    build_ed25519_verifier,
    generate_ed25519_signer,
)


def _statement() -> dict[str, object]:
    return {
        "kind": "physical_node_observation_v1",
        "run_id": "run-physical-1",
        "deployment_id": "deployment-1",
        "node_id": "node-a",
        "host_id": "host-a",
        "process_id": 1234,
        "endpoint_id": "endpoint-a",
        "statement_digest": "sha256:" + "1" * 64,
    }


def test_ed25519_signer_emits_qualifier_compatible_signature_and_public_key() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    statement = _statement()

    signature = signer.sign(statement)
    public = signer.public_key_record()
    verifier = build_ed25519_verifier([public])

    assert set(signature) == SIGNATURE_FIELDS
    assert signature["algorithm"] == "ed25519"
    assert signature["signer_endpoint_id"] == "endpoint-a"
    assert signature["signed_statement_digest"] == sha256_bytes(
        canonical_json_bytes(statement)
    )
    assert signature["verification_key_digest"] == public["verification_key_digest"]
    assert set(public) == {
        "algorithm",
        "encoding",
        "verification_key",
        "verification_key_digest",
    }
    assert public["algorithm"] == "ed25519"
    assert public["encoding"] == "base64"
    assert len(base64.b64decode(public["verification_key"], validate=True)) == 32
    assert all("private" not in field for field in public)
    assert verifier(canonical_json_bytes(statement), signature) is True


def test_ed25519_verifier_rejects_tamper_wrong_key_and_metadata_changes() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    other = generate_ed25519_signer(endpoint_id="endpoint-b")
    statement = _statement()
    statement_bytes = canonical_json_bytes(statement)
    signature = signer.sign(statement)

    verifier = build_ed25519_verifier([signer.public_key_record()])
    other_verifier = build_ed25519_verifier([other.public_key_record()])

    tampered = dict(statement, process_id=1235)
    assert verifier(canonical_json_bytes(tampered), signature) is False
    assert other_verifier(statement_bytes, signature) is False
    assert verifier(statement_bytes, dict(signature, algorithm="none")) is False
    assert verifier(
        statement_bytes,
        dict(signature, signer_endpoint_id="endpoint-b"),
    ) is False
    assert verifier(
        statement_bytes,
        dict(signature, signed_statement_digest="sha256:" + "0" * 64),
    ) is False


def test_ed25519_verifier_rejects_unknown_and_malformed_keys_and_signatures() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    statement = _statement()
    statement_bytes = canonical_json_bytes(statement)
    signature = signer.sign(statement)
    public = signer.public_key_record()

    verifier = build_ed25519_verifier([public])
    unknown = dict(signature, verification_key_digest="sha256:" + "f" * 64)
    malformed = dict(signature, signature="not-base64!")
    extra = dict(signature, surprise=True)

    assert verifier(statement_bytes, unknown) is False
    assert verifier(statement_bytes, malformed) is False
    assert verifier(statement_bytes, extra) is False
    with pytest.raises(EvidenceSigningError, match="invalid_verification_key_record"):
        build_ed25519_verifier([dict(public, surprise=True)])
    with pytest.raises(EvidenceSigningError, match="verification_key_digest_mismatch"):
        build_ed25519_verifier(
            [dict(public, verification_key_digest="sha256:" + "0" * 64)]
        )


def test_signing_rejects_invalid_endpoint_and_noncanonical_statement() -> None:
    with pytest.raises(EvidenceSigningError, match="invalid_signer_endpoint_id"):
        generate_ed25519_signer(endpoint_id="")

    signer = generate_ed25519_signer(endpoint_id="endpoint-a")
    with pytest.raises(EvidenceSigningError, match="invalid_signed_statement"):
        signer.sign({"not_json": object()})
