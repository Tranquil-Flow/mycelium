"""Hardware-independent harness for Task 3.7 physical negative-run records.

These tests exercise the sole qualification authority against deterministic
synthetic fixtures.  They validate the harness that a physical controller must
use, but they are not physical evidence and never authorize ``route_ready``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from mycelium_qualification.evidence import (
    canonical_json_bytes,
    evidence_manifest_digest,
    sha256_bytes,
)
from mycelium_qualification.qualifier import (
    REQUIRED_NEGATIVE_RUNS,
    QualificationError,
    _validate_negative_runs,
    qualify_route,
)
from mycelium_qualification.sealer import (
    EvidenceSealingError,
    qualify_sealed_evidence,
    seal_physical_evidence,
)
from mycelium_qualification.signing import (
    Ed25519EvidenceSigner,
    build_ed25519_verifier,
)
from tests.qualification.conftest import (
    TEST_RUN_ID,
    QualificationCase,
    make_case,
)

Mutation = Callable[[QualificationCase], None]


@dataclass(frozen=True, slots=True)
class NegativeProbe:
    kind: str
    expected_code: str
    mutate: Mutation


def _challenge(case: QualificationCase) -> dict[str, Any]:
    return case.documents["run/route-challenge.json"]


def _set_challenge(*path: str | int, value: Any) -> Mutation:
    def mutate(case: QualificationCase) -> None:
        parent: Any = _challenge(case)
        for component in path[:-1]:
            parent = parent[component]
        parent[path[-1]] = value

    return mutate


def _stale_proof(case: QualificationCase) -> None:
    challenge = _challenge(case)
    challenge["stage_evidence"][0]["load_proof_generated_at_unix_ms"] = (
        case.now_unix_ms - challenge["max_load_proof_age_ms"] - 1
    )


def _dropped_peer(case: QualificationCase) -> None:
    signed = case.documents["control/gossip-signature.json"]
    signed["statement"]["peers"][1]["peer_state"] = "dead"
    signed["signature"]["signed_statement_digest"] = sha256_bytes(
        canonical_json_bytes(signed["statement"])
    )


def _missing_tensor(case: QualificationCase) -> None:
    _challenge(case)["stage_evidence"][0]["assigned_tensor_keys"].pop()


def _expired_reservation(case: QualificationCase) -> None:
    _challenge(case)["path_manifest"]["ordered_hops"][0][
        "reservation_expires_at_unix_ms"
    ] = case.now_unix_ms


NEGATIVE_PROBES = (
    NegativeProbe("stale_proof", "stale_load_proof", _stale_proof),
    NegativeProbe(
        "wrong_revision",
        "model_revision_mismatch",
        _set_challenge("resolved_commit", value="b" * 40),
    ),
    NegativeProbe(
        "wrong_endpoint",
        "endpoint_id_mismatch",
        _set_challenge(
            "stage_evidence",
            0,
            "endpoint_id",
            value="wrong-endpoint",
        ),
    ),
    NegativeProbe("missing_tensor", "tensor_scope_mismatch", _missing_tensor),
    NegativeProbe(
        "expired_reservation",
        "expired_reservation",
        _expired_reservation,
    ),
    NegativeProbe(
        "sequence_replay",
        "sequence_replay",
        _set_challenge(
            "transport",
            "observed_frame_sequences",
            5,
            value=5,
        ),
    ),
    NegativeProbe("dropped_peer", "dropped_peer", _dropped_peer),
    NegativeProbe(
        "full_model_fallback",
        "full_model_fallback",
        _set_challenge(
            "token_parity",
            "full_model_fallback",
            value=True,
        ),
    ),
    NegativeProbe(
        "simulator_participation",
        "simulator_participation",
        _set_challenge(
            "transport",
            "simulator_participated",
            value=True,
        ),
    ),
    NegativeProbe(
        "synthetic_timing",
        "synthetic_timing",
        _set_challenge(
            "transport",
            "synthetic_timing",
            value=True,
        ),
    ),
)


def _deterministic_signer(label: str, endpoint_id: str) -> Ed25519EvidenceSigner:
    private_key = Ed25519PrivateKey.from_private_bytes(
        hashlib.sha256(f"{label}:{endpoint_id}".encode()).digest()
    )
    return Ed25519EvidenceSigner(
        endpoint_id=endpoint_id,
        _private_key=private_key,
        _public_key_bytes=private_key.public_key().public_bytes_raw(),
    )


def _install_real_signatures(case: QualificationCase) -> Callable[
    [bytes, dict[str, Any]], bool
]:
    public_keys: list[dict[str, str]] = []
    gossip = case.documents["control/gossip-signature.json"]
    gossip_endpoint = gossip["statement"]["peers"][0]["endpoint_id"]
    gossip_signer = _deterministic_signer("gossip", gossip_endpoint)
    gossip["signature"] = gossip_signer.sign(gossip["statement"])
    public_keys.append(gossip_signer.public_key_record())

    signatures = case.documents["runtime/load-proof-signatures.json"]["signatures"]
    for index, item in enumerate(signatures):
        endpoint = item["statement"]["endpoint_id"]
        signer = _deterministic_signer(f"load-{index}", endpoint)
        item["signature"] = signer.sign(item["statement"])
        public_keys.append(signer.public_key_record())
    return build_ed25519_verifier(public_keys)


def _run_probe(probe: NegativeProbe) -> dict[str, Any]:
    case = make_case()
    probe.mutate(case)
    verifier = _install_real_signatures(case)
    files, manifest = case.render()

    qualification = None
    with pytest.raises(QualificationError) as captured:
        qualification = qualify_route(
            evidence_files=files,
            evidence_manifest=manifest,
            now_unix_ms=case.now_unix_ms,
            verify_gossip_signature=verifier,
            verify_load_proof_signature=verifier,
        )

    assert qualification is None
    assert captured.value.code == probe.expected_code
    return {
        "kind": probe.kind,
        "route_ready": False,
        "reason_code": captured.value.code,
        "evidence_digest": evidence_manifest_digest(manifest),
    }


def _negative_run_document() -> dict[str, Any]:
    return {
        "kind": "negative_run_set_v1",
        "run_id": TEST_RUN_ID,
        "runs": [_run_probe(probe) for probe in NEGATIVE_PROBES],
    }


def test_required_negative_records_derive_from_real_qualifier_rejections() -> None:
    document = _negative_run_document()

    assert tuple(probe.kind for probe in NEGATIVE_PROBES) == REQUIRED_NEGATIVE_RUNS
    assert [run["kind"] for run in document["runs"]] == list(
        REQUIRED_NEGATIVE_RUNS
    )
    assert all(run["route_ready"] is False for run in document["runs"])
    assert all(run["reason_code"] for run in document["runs"])
    assert all(run["evidence_digest"].startswith("sha256:") for run in document["runs"])
    assert len({run["evidence_digest"] for run in document["runs"]}) == len(
        REQUIRED_NEGATIVE_RUNS
    )
    assert _validate_negative_runs(document, TEST_RUN_ID) == document


def test_negative_record_codes_and_digests_are_deterministic() -> None:
    first = _negative_run_document()
    second = _negative_run_document()

    assert canonical_json_bytes(first) == canonical_json_bytes(second)


@pytest.mark.parametrize("probe", NEGATIVE_PROBES, ids=lambda probe: probe.kind)
def test_no_forbidden_mutation_returns_a_qualification(probe: NegativeProbe) -> None:
    record = _run_probe(probe)

    assert record == {
        "kind": probe.kind,
        "route_ready": False,
        "reason_code": probe.expected_code,
        "evidence_digest": record["evidence_digest"],
    }


@pytest.mark.parametrize("probe", NEGATIVE_PROBES, ids=lambda probe: probe.kind)
def test_sealed_rejected_evidence_remains_rejected_and_preserved(
    probe: NegativeProbe,
    tmp_path: Path,
) -> None:
    case = make_case()
    probe.mutate(case)
    verifier = _install_real_signatures(case)
    _files, manifest = case.render()
    output = tmp_path / probe.kind
    sealed = seal_physical_evidence(
        output_dir=output,
        run_id=TEST_RUN_ID,
        documents=case.documents,
        extra_files=case.extra_files,
    )

    qualification = None
    with pytest.raises(QualificationError) as captured:
        qualification = qualify_sealed_evidence(
            sealed,
            now_unix_ms=case.now_unix_ms,
            verify_gossip_signature=verifier,
            verify_load_proof_signature=verifier,
        )

    assert qualification is None
    assert captured.value.code == probe.expected_code
    assert sealed.root == output
    assert sealed.root.is_dir()
    assert sealed.manifest_digest == evidence_manifest_digest(manifest)


def test_synthetic_fixture_class_cannot_enter_physical_sealer(tmp_path: Path) -> None:
    case = make_case()
    _challenge(case)["evidence_class"] = "synthetic_test_fixture"
    output = tmp_path / "synthetic"

    with pytest.raises(EvidenceSealingError) as captured:
        seal_physical_evidence(
            output_dir=output,
            run_id=TEST_RUN_ID,
            documents=case.documents,
            extra_files=case.extra_files,
        )

    assert captured.value.code == "nonphysical_evidence"
    assert not output.exists()
