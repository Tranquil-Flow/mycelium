from __future__ import annotations

from typing import Any, Callable

import pytest

from mycelium_qualification.contracts import route_qualification_to_dict
from mycelium_qualification.evidence import (
    canonical_json_bytes,
    evidence_manifest_digest,
    sha256_bytes,
    sha256_document,
)
from mycelium_qualification.qualifier import QualificationError, qualify_route


Mutation = Callable[[Any], None]


EXPECTED_STAGE_FIELDS = {
    "stage_id",
    "placement_id",
    "assignment_id",
    "node_id",
    "stage_signature",
    "load_proof_digest",
    "stage_probe_result_digest",
    "endpoint_id",
    "process_id",
    "process_host_id",
    "tensor_scope_digest",
    "reservation_id",
}


def _verify_synthetic_signature(statement: bytes, signature: dict[str, Any]) -> bool:
    return (
        signature.get("algorithm") == "ed25519"
        and signature.get("signature") == "synthetic-test-signature-never-production"
        and signature.get("signed_statement_digest") == sha256_bytes(statement)
    )


def _qualify(case: Any):
    files, manifest = case.render()
    return qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=_verify_synthetic_signature,
        verify_load_proof_signature=_verify_synthetic_signature,
    )


def _assert_rejected(case: Any, expected_code: str) -> None:
    with pytest.raises(QualificationError) as captured:
        _qualify(case)
    assert captured.value.code == expected_code


def _resign_gossip_statement(case: Any) -> None:
    signed = case.documents["control/gossip-signature.json"]
    signed["signature"]["signed_statement_digest"] = sha256_bytes(
        canonical_json_bytes(signed["statement"])
    )


def _resign_load_statement(case: Any, index: int) -> None:
    signed = case.documents["runtime/load-proof-signatures.json"]["signatures"][index]
    signed["signature"]["signed_statement_digest"] = sha256_bytes(
        canonical_json_bytes(signed["statement"])
    )


def test_hypothetical_physical_shape_qualifies_in_memory_only(qualification_case: Any) -> None:
    files, manifest = qualification_case.render()
    record = _qualify(qualification_case)
    document = route_qualification_to_dict(record)
    challenge = qualification_case.documents["run/route-challenge.json"]

    assert document["route_ready"] is True
    assert document["reason_codes"] == []
    assert document["evidence_class"] == "physical_qualification"
    assert document["qualified_by"] == "mycelium_qualification.qualifier:RouteQualificationV1"
    assert document["evidence_manifest_digest"] == evidence_manifest_digest(manifest)
    assert document["source_provenance_digest"] == sha256_document(
        qualification_case.documents["qualification/source-provenance.json"]
    )
    assert document["execution_graph_digest"] == sha256_document(
        qualification_case.documents["router/execution-graph.json"]
    )
    assert document["path_manifest_digest"] == sha256_document(
        challenge["path_manifest"]
    )
    assert document["load_proof_signatures_digest"] == sha256_document(
        qualification_case.documents["runtime/load-proof-signatures.json"]
    )
    assert document["environment_digest"] == sha256_bytes(
        qualification_case.extra_files["provenance/environment.json"]
    )
    assert document["execution_trace_digest"] == sha256_document(
        challenge["execution_trace"]
    )
    assert document["kv_ownership_digest"] == sha256_document(challenge["kv_ownership"])
    assert document["timing_evidence_digest"] == sha256_document(
        challenge["transport"]["hop_timings"]
    )
    assert len(document["dependency_lock_digests"]) == 2
    assert len(document["stage_bindings"]) == 2
    assert all(
        binding["stage_signature"].startswith("sha256:")
        for binding in document["stage_bindings"]
    )
    assert all(
        binding["load_proof_digest"].startswith("sha256:")
        for binding in document["stage_bindings"]
    )
    assert all(set(binding) == EXPECTED_STAGE_FIELDS for binding in document["stage_bindings"])
    assert all(
        binding["stage_probe_result_digest"].startswith("sha256:")
        for binding in document["stage_bindings"]
    )
    assert set(files) == {entry["path"] for entry in manifest["files"]}


def test_qualification_snapshots_manifest_before_verifier_callbacks(
    qualification_case: Any,
) -> None:
    files, manifest = qualification_case.render()
    expected_digest = evidence_manifest_digest(manifest)

    def mutating_verifier(statement: bytes, signature: dict[str, Any]) -> bool:
        manifest["evidence_class"] = "synthetic_test_fixture"
        return _verify_synthetic_signature(statement, signature)

    record = qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=qualification_case.now_unix_ms,
        verify_gossip_signature=mutating_verifier,
        verify_load_proof_signature=_verify_synthetic_signature,
    )

    assert record.route_ready is True
    assert record.evidence_manifest_digest == expected_digest


def test_verifier_callbacks_cannot_mutate_bound_signature_evidence(
    qualification_case: Any,
) -> None:
    expected_gossip_digest = sha256_document(
        qualification_case.documents["control/gossip-signature.json"]
    )
    expected_load_digest = sha256_document(
        qualification_case.documents["runtime/load-proof-signatures.json"]
    )
    files, manifest = qualification_case.render()

    def mutating_verifier(statement: bytes, signature: dict[str, Any]) -> bool:
        verified = _verify_synthetic_signature(statement, signature)
        signature["signature"] = "callback-mutated-signature"
        return verified

    record = qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=qualification_case.now_unix_ms,
        verify_gossip_signature=mutating_verifier,
        verify_load_proof_signature=mutating_verifier,
    )

    assert record.gossip_signature_digest == expected_gossip_digest
    assert record.load_proof_signatures_digest == expected_load_digest


def test_evidence_manifest_binding_rejects_mutated_or_missing_files(qualification_case: Any) -> None:
    files, manifest = qualification_case.render()
    content = files["run/route-challenge.json"]
    files["run/route-challenge.json"] = b"X" + content[1:]
    with pytest.raises(QualificationError) as changed:
        qualify_route(
            evidence_files=files,
            evidence_manifest=manifest,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=lambda _statement, _signature: True,
            verify_load_proof_signature=lambda _statement, _signature: True,
        )
    assert changed.value.code == "evidence_file_digest_mismatch"

    files, manifest = qualification_case.render()
    files.pop("runtime/load-proofs.json")
    with pytest.raises(QualificationError) as missing:
        qualify_route(
            evidence_files=files,
            evidence_manifest=manifest,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=lambda _statement, _signature: True,
            verify_load_proof_signature=lambda _statement, _signature: True,
        )
    assert missing.value.code == "evidence_manifest_file_set_mismatch"


def test_missing_required_evidence_document_is_rejected(qualification_case: Any) -> None:
    qualification_case.documents.pop("runtime/load-proofs.json")
    _assert_rejected(qualification_case, "missing_evidence_file")


def test_source_manifest_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/source-manifest.json"] += b"mutation"
    _assert_rejected(qualification_case, "source_manifest_digest_mismatch")


def test_contract_manifest_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/contract-manifest.v1.json"] += b"mutation"
    _assert_rejected(qualification_case, "contract_manifest_digest_mismatch")


def test_environment_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/environment.json"] += b"mutation"
    _assert_rejected(qualification_case, "environment_digest_mismatch")


def test_every_dependency_lock_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/native-iroh-Cargo.lock"] += b"mutation"
    _assert_rejected(qualification_case, "dependency_lock_digest_mismatch")


def test_signed_load_proof_set_is_complete_bound_and_verified(qualification_case: Any) -> None:
    pristine = qualification_case.clone()
    signed_set = qualification_case.documents["runtime/load-proof-signatures.json"]
    signed_set["signatures"].pop()
    _assert_rejected(qualification_case, "signed_load_proof_set_mismatch")

    case = pristine.clone()
    signed = case.documents["runtime/load-proof-signatures.json"]["signatures"][0]
    signed["statement"]["load_proof_digest"] = "sha256:" + "f" * 64
    signed["signature"]["signed_statement_digest"] = sha256_bytes(
        canonical_json_bytes(signed["statement"])
    )
    _assert_rejected(case, "signed_load_proof_mismatch")

    case = pristine
    case.documents["runtime/load-proof-signatures.json"]["signatures"][0]["signature"][
        "signature"
    ] = "invalid"
    _assert_rejected(case, "load_proof_signature_invalid")


def test_signed_gossip_snapshot_must_verify(qualification_case: Any) -> None:
    qualification_case.documents["control/gossip-signature.json"]["signature"][
        "signature"
    ] = "invalid"
    _assert_rejected(qualification_case, "gossip_signature_invalid")


@pytest.mark.parametrize("verifier_result", [False, None, 1, "true"])
def test_gossip_verifier_requires_literal_true(
    qualification_case: Any, verifier_result: Any
) -> None:
    files, manifest = qualification_case.render()
    with pytest.raises(QualificationError) as captured:
        qualify_route(
            evidence_files=files,
            evidence_manifest=manifest,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=lambda _statement, _signature: verifier_result,
            verify_load_proof_signature=_verify_synthetic_signature,
        )
    assert captured.value.code == "gossip_signature_invalid"


def test_gossip_verifier_exception_fails_closed(qualification_case: Any) -> None:
    files, manifest = qualification_case.render()

    def broken_verifier(_statement: bytes, _signature: dict[str, Any]) -> bool:
        raise RuntimeError("synthetic verifier failure")

    with pytest.raises(QualificationError) as captured:
        qualify_route(
            evidence_files=files,
            evidence_manifest=manifest,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=broken_verifier,
            verify_load_proof_signature=_verify_synthetic_signature,
        )
    assert captured.value.code == "gossip_signature_invalid"


def test_signed_gossip_snapshot_identity_and_peer_liveness_are_bound(qualification_case: Any) -> None:
    pristine = qualification_case.clone()
    signed = qualification_case.documents["control/gossip-signature.json"]
    signed["statement"]["evidence_bundle_digest"] = "sha256:" + "f" * 64
    _resign_gossip_statement(qualification_case)
    _assert_rejected(qualification_case, "signed_gossip_snapshot_mismatch")

    case = pristine
    signed = case.documents["control/gossip-signature.json"]
    signed["statement"]["peers"][1]["peer_state"] = "dead"
    _resign_gossip_statement(case)
    _assert_rejected(case, "dropped_peer")


def test_planner_assignment_chain_must_be_coherent(qualification_case: Any) -> None:
    qualification_case.documents["control/control-plane-tranche.json"]["assignments"][0][
        "deployment_epoch"
    ] += 1
    _assert_rejected(qualification_case, "control_plane_chain_invalid")


def test_provisioning_reports_are_exactly_assignment_bound(qualification_case: Any) -> None:
    qualification_case.documents["runtime/provisioning-reports.json"][0]["route_ready"] = True
    _assert_rejected(qualification_case, "provisioning_report_invalid")


def test_load_proof_chain_remains_unqualified_and_exact(qualification_case: Any) -> None:
    qualification_case.documents["runtime/load-proofs.json"][0]["route_ready"] = True
    _assert_rejected(qualification_case, "load_proof_chain_invalid")


def test_execution_graph_must_equal_the_bound_builder_output(qualification_case: Any) -> None:
    graph = qualification_case.documents["router/execution-graph.json"]
    graph["stages"][0]["placements"][0]["stage_signature"] = "sha256:" + "e" * 64
    _assert_rejected(qualification_case, "execution_graph_chain_invalid")


@pytest.mark.parametrize(
    ("field", "mutation", "code"),
    [
        ("deployment_id", "wrong-deployment", "deployment_id_mismatch"),
        ("deployment_epoch", 99, "deployment_epoch_mismatch"),
        ("topology_version", 99, "topology_version_mismatch"),
        ("resolved_commit", "b" * 40, "model_revision_mismatch"),
        ("manifest_digest", "sha256:" + "b" * 64, "manifest_digest_mismatch"),
    ],
)
def test_deployment_topology_and_model_identity_are_exact(
    qualification_case: Any, field: str, mutation: Any, code: str
) -> None:
    qualification_case.documents["run/route-challenge.json"][field] = mutation
    _assert_rejected(qualification_case, code)


def test_stage_signature_and_load_proof_digest_are_exact(qualification_case: Any) -> None:
    pristine = qualification_case.clone()
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0]["stage_signature"] = "sha256:" + "b" * 64
    _assert_rejected(qualification_case, "stage_signature_mismatch")

    case = pristine
    challenge = case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0]["load_proof_digest"] = "sha256:" + "b" * 64
    _assert_rejected(case, "load_proof_digest_mismatch")


def test_per_stage_probe_result_digest_is_required(qualification_case: Any) -> None:
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0]["stage_probe_result_digest"] = "not-a-digest"
    _assert_rejected(qualification_case, "stage_probe_result_invalid")


def test_path_order_and_graph_binding_are_exact(qualification_case: Any) -> None:
    path = qualification_case.documents["run/route-challenge.json"]["path_manifest"]
    path["ordered_hops"].reverse()
    _assert_rejected(qualification_case, "path_manifest_mismatch")


def test_reservation_identity_and_expiry_fail_closed(qualification_case: Any) -> None:
    pristine = qualification_case.clone()
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0]["reservation_id"] = "wrong-reservation"
    _assert_rejected(qualification_case, "reservation_mismatch")

    case = pristine
    challenge = case.documents["run/route-challenge.json"]
    challenge["path_manifest"]["ordered_hops"][0][
        "reservation_expires_at_unix_ms"
    ] = case.now_unix_ms
    _assert_rejected(case, "expired_reservation")


def test_endpoint_process_and_tensor_scope_fail_closed(qualification_case: Any) -> None:
    pristine = qualification_case.clone()
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0]["authenticated_endpoint_id"] = "wrong-endpoint"
    _assert_rejected(qualification_case, "endpoint_id_mismatch")

    case = pristine.clone()
    stages = case.documents["run/route-challenge.json"]["stage_evidence"]
    stages[1]["process_host_id"] = stages[0]["process_host_id"]
    signed = case.documents["runtime/load-proof-signatures.json"]["signatures"][1]
    signed["statement"]["process_host_id"] = stages[1]["process_host_id"]
    _resign_load_statement(case, 1)
    _assert_rejected(case, "process_identity_invalid")

    case = pristine
    opened = case.documents["run/route-challenge.json"]["stage_evidence"][0][
        "opened_tensor_keys"
    ]
    opened.pop()
    _assert_rejected(case, "tensor_scope_mismatch")


@pytest.mark.parametrize(
    ("field", "forged_value"),
    [
        ("process_host_id", "forged-physical-host"),
        ("process_id", 99_999),
    ],
)
def test_process_identity_is_bound_to_signed_load_proof(
    qualification_case: Any,
    field: str,
    forged_value: Any,
) -> None:
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0][field] = forged_value
    challenge["kv_ownership"][0][field] = forged_value

    _assert_rejected(qualification_case, "signed_load_proof_mismatch")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("adapter", "fixture_adapter", "transport_adapter_invalid"),
        ("protocol", "fixture.protocol", "transport_protocol_invalid"),
        ("physical_transport_observed", False, "physical_transport_missing"),
        ("mutual_authentication_observed", False, "transport_authentication_missing"),
        ("simulator_participated", True, "simulator_participation"),
        ("fixture_port_participated", True, "fixture_participation"),
        ("synthetic_timing", True, "synthetic_timing"),
        ("peer_dropped", True, "dropped_peer"),
    ],
)
def test_transport_must_be_authenticated_physical_and_nonsynthetic(
    qualification_case: Any, field: str, value: Any, code: str
) -> None:
    qualification_case.documents["run/route-challenge.json"]["transport"][field] = value
    _assert_rejected(qualification_case, code)


def test_each_physical_hop_has_receiver_observed_nonsynthetic_timing(
    qualification_case: Any,
) -> None:
    pristine = qualification_case.clone()
    timings = qualification_case.documents["run/route-challenge.json"]["transport"][
        "hop_timings"
    ]
    timings.pop()
    _assert_rejected(qualification_case, "timing_evidence_invalid")

    case = pristine.clone()
    timing = case.documents["run/route-challenge.json"]["transport"]["hop_timings"][0]
    timing["synthetic"] = True
    _assert_rejected(case, "synthetic_timing")

    case = pristine
    timing = case.documents["run/route-challenge.json"]["transport"]["hop_timings"][0]
    timing["receiver_elapsed_ns"] += 1
    _assert_rejected(case, "timing_evidence_invalid")


def test_sequence_replay_is_rejected(qualification_case: Any) -> None:
    sequences = qualification_case.documents["run/route-challenge.json"]["transport"][
        "observed_frame_sequences"
    ]
    sequences[5] = sequences[4]
    _assert_rejected(qualification_case, "sequence_replay")


def test_token_parity_requires_eight_exact_decode_steps_and_no_fallback(
    qualification_case: Any,
) -> None:
    pristine = qualification_case.clone()
    parity = qualification_case.documents["run/route-challenge.json"]["token_parity"]
    parity["distributed_token_ids"].pop()
    parity["reference_token_ids"].pop()
    parity["decode_steps"] -= 1
    _assert_rejected(qualification_case, "insufficient_decode_tokens")

    case = pristine.clone()
    parity = case.documents["run/route-challenge.json"]["token_parity"]
    parity["reference_token_ids"][0] += 1
    _assert_rejected(case, "token_parity_mismatch")

    case = pristine
    parity = case.documents["run/route-challenge.json"]["token_parity"]
    parity["full_model_fallback"] = True
    _assert_rejected(case, "full_model_fallback")


def test_numeric_parity_is_stage_bound_and_within_tolerance(qualification_case: Any) -> None:
    numeric = qualification_case.documents["run/route-challenge.json"]["numeric_parity"]
    numeric["stage_reports"][0]["max_abs_diff"] = numeric["absolute_tolerance"] * 2
    _assert_rejected(qualification_case, "numeric_parity_failed")


def test_prefill_decode_trace_and_final_output_parity_are_strictly_bound(
    qualification_case: Any,
) -> None:
    pristine = qualification_case.clone()
    trace = qualification_case.documents["run/route-challenge.json"]["execution_trace"]
    trace["prefill_observed"] = False
    _assert_rejected(qualification_case, "execution_trace_invalid")

    case = pristine.clone()
    trace = case.documents["run/route-challenge.json"]["execution_trace"]
    trace["decode_events"][0]["distributed_token_id"] += 1
    _assert_rejected(case, "execution_trace_invalid")

    case = pristine.clone()
    trace = case.documents["run/route-challenge.json"]["execution_trace"]
    trace["decoded_text"]["reference_digest"] = "sha256:" + "f" * 64
    _assert_rejected(case, "decoded_text_parity_mismatch")

    case = pristine
    trace = case.documents["run/route-challenge.json"]["execution_trace"]
    trace["final_logits"]["max_abs_diff"] = trace["final_logits"]["absolute_tolerance"] * 2
    case.documents["run/route-challenge.json"]["numeric_parity"][
        "final_logits_report"
    ] = dict(trace["final_logits"])
    _assert_rejected(case, "final_logits_parity_failed")


def test_local_kv_ownership_is_stage_and_process_bound(qualification_case: Any) -> None:
    pristine = qualification_case.clone()
    ownership = qualification_case.documents["run/route-challenge.json"]["kv_ownership"]
    ownership[0]["local_kv_observed"] = False
    _assert_rejected(qualification_case, "kv_ownership_invalid")

    case = pristine.clone()
    ownership = case.documents["run/route-challenge.json"]["kv_ownership"]
    ownership[0]["process_id"] += 1
    _assert_rejected(case, "kv_ownership_mismatch")

    case = pristine
    ownership = case.documents["run/route-challenge.json"]["kv_ownership"]
    ownership[0]["remote_kv_access"] = True
    _assert_rejected(case, "kv_ownership_invalid")


def test_stale_load_proof_is_rejected(qualification_case: Any) -> None:
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["stage_evidence"][0]["load_proof_generated_at_unix_ms"] = (
        qualification_case.now_unix_ms - challenge["max_load_proof_age_ms"] - 1
    )
    _assert_rejected(qualification_case, "stale_load_proof")


def test_missing_negative_run_evidence_is_rejected(qualification_case: Any) -> None:
    runs = qualification_case.documents["run/negative-runs.json"]["runs"]
    runs.pop()
    _assert_rejected(qualification_case, "missing_negative_run_evidence")


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda case: case.documents["run/route-challenge.json"].__setitem__(
                "unknown_field", True
            ),
            "route_challenge_invalid",
        ),
        (
            lambda case: case.documents["run/route-challenge.json"]["stage_evidence"][
                0
            ].__setitem__("unknown_field", True),
            "stage_evidence_invalid",
        ),
        (
            lambda case: case.documents["run/route-challenge.json"][
                "token_parity"
            ].__setitem__("unknown_field", True),
            "token_parity_invalid",
        ),
        (
            lambda case: case.documents["run/route-challenge.json"][
                "numeric_parity"
            ].__setitem__("unknown_field", True),
            "numeric_parity_invalid",
        ),
        (
            lambda case: case.documents["run/route-challenge.json"]["numeric_parity"][
                "stage_reports"
            ][0].__setitem__("unknown_field", True),
            "numeric_parity_invalid",
        ),
    ],
)
def test_versioned_evidence_documents_reject_unknown_fields(
    qualification_case: Any,
    mutation: Mutation,
    code: str,
) -> None:
    mutation(qualification_case)
    _assert_rejected(qualification_case, code)


@pytest.mark.parametrize("evidence_class", ["synthetic_test_fixture", "unknown_class"])
def test_synthetic_or_fixture_evidence_class_cannot_qualify(
    qualification_case: Any, evidence_class: str
) -> None:
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["evidence_class"] = evidence_class
    _assert_rejected(qualification_case, "evidence_class_invalid")
