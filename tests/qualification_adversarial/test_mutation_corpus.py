"""Bounded adversarial conformance corpus for the frozen qualification authority.

The corpus starts from ``tests.qualification.conftest.make_case`` for every
mutation.  Mutators alter fixture fields or serialized bytes only; they do not
reimplement qualifier validation or derive expected values with qualifier logic.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from mycelium_qualification.evidence import (
    build_evidence_manifest,
    canonical_json_bytes,
    sha256_bytes,
)
from mycelium_qualification.qualifier import QualificationError, qualify_route
from tests.qualification.conftest import (
    TEST_RUN_ID,
    QualificationCase,
    make_case,
    synthetic_signature_verifier,
)

CaseMutation = Callable[[QualificationCase], None]
FilesMutation = Callable[[dict[str, bytes]], None]
ManifestMutation = Callable[[dict[str, Any]], None]

MAX_MUTATIONS = 100
MAX_SERIALIZED_BYTES_PER_MUTATION = 512 * 1024
MAX_SERIALIZED_BYTES_TOTAL = 32 * 1024 * 1024
EXPECTED_FAMILIES = frozenset(
    {
        "assignment-node-stage-placement",
        "deployment-epoch-topology",
        "endpoint-process-host",
        "execution-graph-path-tensor-kv",
        "gossip-signature-generation",
        "model-commit-manifest",
        "negative-synthetic-simulator",
        "provenance-locks-evidence-manifest",
        "reservation-identity-expiry",
        "schema-canonicalization-types",
        "stage-signature-load-proof",
        "timing-token-numeric-trace",
    }
)
WRONG_DIGEST = "sha256:" + "f" * 64
WRONG_DIGEST_2 = "sha256:" + "e" * 64


@dataclass(frozen=True, slots=True)
class MutationSpec:
    mutation_id: str
    family: str
    expected_code: str
    mutate_case: CaseMutation = lambda _case: None
    mutate_files: FilesMutation | None = None
    mutate_manifest: ManifestMutation | None = None
    minimum_gossip_verifications: int = 0
    minimum_load_verifications: int = 0


@dataclass(frozen=True, slots=True)
class MaterializedMutation:
    spec: MutationSpec
    files: dict[str, bytes]
    manifest: dict[str, Any]
    serialized_bytes: int


def _at(case: QualificationCase, document: str, *path: str | int) -> Any:
    value: Any = case.documents[document]
    for component in path:
        value = value[component]
    return value


def _set(document: str, *path: str | int, value: Any) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        parent = _at(case, document, *path[:-1])
        parent[path[-1]] = copy.deepcopy(value)

    return mutate


def _delete(document: str, *path: str | int) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        parent = _at(case, document, *path[:-1])
        del parent[path[-1]]

    return mutate


def _append(document: str, *path: str | int, value: Any) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        _at(case, document, *path).append(copy.deepcopy(value))

    return mutate


def _resign_gossip(case: QualificationCase) -> None:
    signed = case.documents["control/gossip-signature.json"]
    signed["signature"]["signed_statement_digest"] = sha256_bytes(
        canonical_json_bytes(signed["statement"])
    )


def _mutate_gossip_statement(*path: str | int, value: Any) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        statement = _at(case, "control/gossip-signature.json", "statement")
        parent = statement
        for component in path[:-1]:
            parent = parent[component]
        parent[path[-1]] = copy.deepcopy(value)
        _resign_gossip(case)

    return mutate


def _swap_stage_assignment_ids(case: QualificationCase) -> None:
    stages = _at(case, "run/route-challenge.json", "stage_evidence")
    stages[0]["assignment_id"], stages[1]["assignment_id"] = (
        stages[1]["assignment_id"],
        stages[0]["assignment_id"],
    )


def _duplicate_process_identity(case: QualificationCase) -> None:
    stages = _at(case, "run/route-challenge.json", "stage_evidence")
    stages[1]["process_id"] = stages[0]["process_id"]
    stages[1]["process_host_id"] = stages[0]["process_host_id"]


def _remove_last_tensor(list_name: str) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        _at(case, "run/route-challenge.json", "stage_evidence", 0, list_name).pop()

    return mutate


def _reverse(document: str, *path: str | int) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        _at(case, document, *path).reverse()

    return mutate


def _duplicate_negative_run(case: QualificationCase) -> None:
    runs = _at(case, "run/negative-runs.json", "runs")
    runs.append(copy.deepcopy(runs[0]))


def _mutate_extra_file(path: str) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        case.extra_files[path] += b"\nmutation"

    return mutate


def _mutate_load_statement(field: str, value: Any) -> CaseMutation:
    def mutate(case: QualificationCase) -> None:
        signed = _at(
            case,
            "runtime/load-proof-signatures.json",
            "signatures",
            0,
        )
        signed["statement"][field] = copy.deepcopy(value)
        signed["signature"]["signed_statement_digest"] = sha256_bytes(
            canonical_json_bytes(signed["statement"])
        )

    return mutate


def _raw_duplicate_challenge_key(files: dict[str, bytes]) -> None:
    path = "run/route-challenge.json"
    needle = b'"kind":"route_challenge_evidence_v1"'
    replacement = needle + b"," + needle
    assert files[path].count(needle) == 1
    files[path] = files[path].replace(needle, replacement, 1)


def _raw_nonfinite_numeric_value(files: dict[str, bytes]) -> None:
    path = "run/route-challenge.json"
    needle = b'"absolute_tolerance":0.0001'
    assert files[path].count(needle) >= 1
    files[path] = files[path].replace(needle, b'"absolute_tolerance":NaN', 1)


def _manifest_wrong_file_digest(manifest: dict[str, Any]) -> None:
    manifest["files"][0]["sha256"] = WRONG_DIGEST


def _manifest_duplicate_entry(manifest: dict[str, Any]) -> None:
    entry = copy.deepcopy(manifest["files"][0])
    manifest["files"].append(entry)
    manifest["file_count"] += 1
    manifest["total_size_bytes"] += entry["size_bytes"]


def _manifest_unknown_field(manifest: dict[str, Any]) -> None:
    manifest["unknown_field"] = False


def _manifest_bool_size(manifest: dict[str, Any]) -> None:
    manifest["files"][0]["size_bytes"] = True


def _provenance_duplicate_lock(case: QualificationCase) -> None:
    locks = _at(case, "qualification/source-provenance.json", "dependency_locks")
    locks.append(copy.deepcopy(locks[0]))


def _stage_opened_tensor_duplicate(case: QualificationCase) -> None:
    opened = _at(
        case,
        "run/route-challenge.json",
        "stage_evidence",
        0,
        "opened_tensor_keys",
    )
    opened[-1] = opened[0]


def _token_reference_mismatch(case: QualificationCase) -> None:
    _at(case, "run/route-challenge.json", "token_parity", "reference_token_ids")[0] += 1


def _token_decode_short(case: QualificationCase) -> None:
    parity = _at(case, "run/route-challenge.json", "token_parity")
    parity["distributed_token_ids"].pop()
    parity["reference_token_ids"].pop()
    parity["decode_steps"] -= 1


def _numeric_over_tolerance(case: QualificationCase) -> None:
    numeric = _at(case, "run/route-challenge.json", "numeric_parity")
    numeric["stage_reports"][0]["max_abs_diff"] = numeric["absolute_tolerance"] * 2


def _final_logits_cross_artifact_mismatch(case: QualificationCase) -> None:
    trace = _at(case, "run/route-challenge.json", "execution_trace")
    trace["final_logits"]["distributed_digest"] = WRONG_DIGEST


def _stale_challenge(case: QualificationCase) -> None:
    challenge = _at(case, "run/route-challenge.json")
    challenge["valid_until_unix_ms"] = case.now_unix_ms


def _stale_gossip(case: QualificationCase) -> None:
    challenge = _at(case, "run/route-challenge.json")
    statement = _at(case, "control/gossip-signature.json", "statement")
    statement["captured_at_unix_ms"] = (
        case.now_unix_ms - challenge["max_load_proof_age_ms"] - 1
    )
    _resign_gossip(case)


def _stale_load_proof(case: QualificationCase) -> None:
    challenge = _at(case, "run/route-challenge.json")
    generated = case.now_unix_ms - challenge["max_load_proof_age_ms"] - 1
    challenge["stage_evidence"][0]["load_proof_generated_at_unix_ms"] = generated


def _expired_reservation(case: QualificationCase) -> None:
    _at(case, "run/route-challenge.json", "path_manifest", "ordered_hops", 0)[
        "reservation_expires_at_unix_ms"
    ] = case.now_unix_ms


def _specs() -> tuple[MutationSpec, ...]:
    post_load: dict[str, Any] = {
        "minimum_gossip_verifications": 1,
        "minimum_load_verifications": 2,
    }
    post_gossip: dict[str, Any] = {"minimum_gossip_verifications": 1}
    specs = [
        MutationSpec(
            "deployment.challenge-id",
            "deployment-epoch-topology",
            "deployment_id_mismatch",
            _set("run/route-challenge.json", "deployment_id", value="wrong-deployment"),
            **post_load,
        ),
        MutationSpec(
            "deployment.challenge-epoch",
            "deployment-epoch-topology",
            "deployment_epoch_mismatch",
            _set("run/route-challenge.json", "deployment_epoch", value=99),
            **post_load,
        ),
        MutationSpec(
            "deployment.challenge-topology",
            "deployment-epoch-topology",
            "topology_version_mismatch",
            _set("run/route-challenge.json", "topology_version", value=99),
            **post_load,
        ),
        MutationSpec(
            "deployment.path-id",
            "deployment-epoch-topology",
            "path_manifest_mismatch",
            _set(
                "run/route-challenge.json",
                "path_manifest",
                "deployment_id",
                value="wrong-deployment",
            ),
            **post_load,
        ),
        MutationSpec(
            "deployment.gossip-epoch",
            "deployment-epoch-topology",
            "signed_gossip_snapshot_mismatch",
            _mutate_gossip_statement("deployment_epoch", value=99),
        ),
        MutationSpec(
            "model.challenge-model-id",
            "model-commit-manifest",
            "model_id_mismatch",
            _set("run/route-challenge.json", "model_id", value="wrong-model"),
            **post_load,
        ),
        MutationSpec(
            "model.challenge-commit",
            "model-commit-manifest",
            "model_revision_mismatch",
            _set("run/route-challenge.json", "resolved_commit", value="b" * 40),
            **post_load,
        ),
        MutationSpec(
            "model.challenge-manifest",
            "model-commit-manifest",
            "manifest_digest_mismatch",
            _set("run/route-challenge.json", "manifest_digest", value=WRONG_DIGEST),
            **post_load,
        ),
        MutationSpec(
            "model.gossip-model-id",
            "model-commit-manifest",
            "signed_gossip_snapshot_mismatch",
            _mutate_gossip_statement("model_id", value="wrong-model"),
        ),
        MutationSpec(
            "model.load-proof-commit",
            "model-commit-manifest",
            "signed_load_proof_mismatch",
            _mutate_load_statement("resolved_commit", "b" * 40),
            **post_gossip,
        ),
        MutationSpec(
            "model.manifest-document",
            "model-commit-manifest",
            "invalid_model_manifest",
            _set("model/model-manifest.json", "model_id", value="wrong-model"),
        ),
        MutationSpec(
            "assignment.control-plane-id",
            "assignment-node-stage-placement",
            "control_plane_chain_invalid",
            _set(
                "control/control-plane-tranche.json",
                "assignments",
                0,
                "assignment_id",
                value="wrong-assignment",
            ),
        ),
        MutationSpec(
            "assignment.stage-id-swap",
            "assignment-node-stage-placement",
            "assignment_id_mismatch",
            _swap_stage_assignment_ids,
            **post_load,
        ),
        MutationSpec(
            "assignment.stage-node",
            "assignment-node-stage-placement",
            "node_id_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "node_id",
                value="wrong-node",
            ),
            **post_load,
        ),
        MutationSpec(
            "assignment.stage-identity",
            "assignment-node-stage-placement",
            "stage_evidence_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "stage_id",
                value="wrong-stage",
            ),
            **post_load,
        ),
        MutationSpec(
            "assignment.stage-placement",
            "assignment-node-stage-placement",
            "path_manifest_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "placement_id",
                value="wrong-placement",
            ),
            **post_load,
        ),
        MutationSpec(
            "assignment.path-hop-stage",
            "assignment-node-stage-placement",
            "path_manifest_mismatch",
            _set(
                "run/route-challenge.json",
                "path_manifest",
                "ordered_hops",
                0,
                "stage_id",
                value="wrong-stage",
            ),
            **post_load,
        ),
        MutationSpec(
            "endpoint.stage-endpoint",
            "endpoint-process-host",
            "endpoint_id_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "endpoint_id",
                value="wrong-endpoint",
            ),
            **post_load,
        ),
        MutationSpec(
            "endpoint.authenticated-endpoint",
            "endpoint-process-host",
            "endpoint_id_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "authenticated_endpoint_id",
                value="wrong-endpoint",
            ),
            **post_load,
        ),
        MutationSpec(
            "endpoint.runtime-endpoint",
            "endpoint-process-host",
            "endpoint_id_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "runtime_endpoint",
                value="iroh://wrong-endpoint",
            ),
            **post_load,
        ),
        MutationSpec(
            "endpoint.gossip-duplicate",
            "endpoint-process-host",
            "endpoint_id_mismatch",
            _mutate_gossip_statement(
                "peers",
                1,
                "endpoint_id",
                value="synthetic-test-endpoint-1",
            ),
        ),
        MutationSpec(
            "endpoint.transport-source",
            "endpoint-process-host",
            "endpoint_id_mismatch",
            _set(
                "run/route-challenge.json",
                "transport",
                "source_endpoint_id",
                value="wrong-endpoint",
            ),
            **post_load,
        ),
        MutationSpec(
            "process.bool-process-id",
            "endpoint-process-host",
            "process_identity_invalid",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "process_id",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "process.duplicate-host-process",
            "endpoint-process-host",
            "process_identity_invalid",
            _duplicate_process_identity,
            **post_load,
        ),
        MutationSpec(
            "process.kv-process-id",
            "endpoint-process-host",
            "kv_ownership_mismatch",
            _set(
                "run/route-challenge.json",
                "kv_ownership",
                0,
                "process_id",
                value=9999,
            ),
            **post_load,
        ),
        MutationSpec(
            "stage.signature",
            "stage-signature-load-proof",
            "stage_signature_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "stage_signature",
                value=WRONG_DIGEST,
            ),
            **post_load,
        ),
        MutationSpec(
            "stage.load-proof-digest",
            "stage-signature-load-proof",
            "load_proof_digest_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "load_proof_digest",
                value=WRONG_DIGEST,
            ),
            **post_load,
        ),
        MutationSpec(
            "stage.probe-digest-shape",
            "stage-signature-load-proof",
            "stage_probe_result_invalid",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "stage_probe_result_digest",
                value="not-a-digest",
            ),
            **post_load,
        ),
        MutationSpec(
            "stage.compute-observed",
            "stage-signature-load-proof",
            "stage_compute_missing",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "stage_compute_observed",
                value=False,
            ),
            **post_load,
        ),
        MutationSpec(
            "load-proof.signed-digest",
            "stage-signature-load-proof",
            "signed_load_proof_mismatch",
            _mutate_load_statement("load_proof_digest", WRONG_DIGEST),
            **post_gossip,
        ),
        MutationSpec(
            "load-proof.signature-bytes",
            "stage-signature-load-proof",
            "load_proof_signature_invalid",
            _set(
                "runtime/load-proof-signatures.json",
                "signatures",
                0,
                "signature",
                "signature",
                value="invalid-signature",
            ),
            minimum_gossip_verifications=1,
            minimum_load_verifications=1,
        ),
        MutationSpec(
            "load-proof.route-ready",
            "stage-signature-load-proof",
            "load_proof_chain_invalid",
            _set("runtime/load-proofs.json", 0, "route_ready", value=True),
            **post_gossip,
        ),
        MutationSpec(
            "load-proof.graph-signature",
            "stage-signature-load-proof",
            "execution_graph_chain_invalid",
            _set(
                "router/execution-graph.json",
                "stages",
                0,
                "placements",
                0,
                "stage_signature",
                value=WRONG_DIGEST,
            ),
            **post_gossip,
        ),
        MutationSpec(
            "load-proof.provisioning-ready",
            "stage-signature-load-proof",
            "provisioning_report_invalid",
            _set("runtime/provisioning-reports.json", 0, "route_ready", value=True),
            **post_gossip,
        ),
        MutationSpec(
            "gossip.snapshot-generation",
            "gossip-signature-generation",
            "signed_gossip_snapshot_mismatch",
            _mutate_gossip_statement("snapshot_generation", value=999),
        ),
        MutationSpec(
            "gossip.bundle-digest",
            "gossip-signature-generation",
            "signed_gossip_snapshot_mismatch",
            _mutate_gossip_statement("evidence_bundle_digest", value=WRONG_DIGEST),
        ),
        MutationSpec(
            "gossip.stale-capture",
            "gossip-signature-generation",
            "stale_gossip_snapshot",
            _stale_gossip,
        ),
        MutationSpec(
            "gossip.dropped-peer",
            "gossip-signature-generation",
            "dropped_peer",
            _mutate_gossip_statement("peers", 1, "peer_state", value="dead"),
        ),
        MutationSpec(
            "gossip.signature-bytes",
            "gossip-signature-generation",
            "gossip_signature_invalid",
            _set(
                "control/gossip-signature.json",
                "signature",
                "signature",
                value="invalid-signature",
            ),
            minimum_gossip_verifications=1,
        ),
        MutationSpec(
            "gossip.signed-digest",
            "gossip-signature-generation",
            "gossip_signature_invalid",
            _set(
                "control/gossip-signature.json",
                "signature",
                "signed_statement_digest",
                value=WRONG_DIGEST,
            ),
        ),
        MutationSpec(
            "gossip.unknown-statement-field",
            "gossip-signature-generation",
            "invalid_gossip_signature",
            _set(
                "control/gossip-signature.json",
                "statement",
                "unknown_field",
                value=None,
            ),
        ),
        MutationSpec(
            "reservation.stage-id",
            "reservation-identity-expiry",
            "reservation_mismatch",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "reservation_id",
                value="wrong-reservation",
            ),
            **post_load,
        ),
        MutationSpec(
            "reservation.path-id",
            "reservation-identity-expiry",
            "reservation_mismatch",
            _set(
                "run/route-challenge.json",
                "path_manifest",
                "ordered_hops",
                0,
                "reservation_id",
                value="wrong-reservation",
            ),
            **post_load,
        ),
        MutationSpec(
            "reservation.epoch",
            "reservation-identity-expiry",
            "reservation_mismatch",
            _set(
                "run/route-challenge.json",
                "path_manifest",
                "ordered_hops",
                0,
                "reservation_epoch",
                value=99,
            ),
            **post_load,
        ),
        MutationSpec(
            "reservation.expiry",
            "reservation-identity-expiry",
            "expired_reservation",
            _expired_reservation,
            **post_load,
        ),
        MutationSpec(
            "graph.edge-id",
            "execution-graph-path-tensor-kv",
            "execution_graph_chain_invalid",
            _set(
                "router/execution-graph.json",
                "edges",
                0,
                "edge_id",
                value="wrong-edge",
            ),
            **post_gossip,
        ),
        MutationSpec(
            "path.hop-order",
            "execution-graph-path-tensor-kv",
            "path_manifest_mismatch",
            _reverse("run/route-challenge.json", "path_manifest", "ordered_hops"),
            **post_load,
        ),
        MutationSpec(
            "path.edge-id",
            "execution-graph-path-tensor-kv",
            "path_manifest_mismatch",
            _set(
                "run/route-challenge.json",
                "path_manifest",
                "forward_edge_ids",
                0,
                value="wrong-edge",
            ),
            **post_load,
        ),
        MutationSpec(
            "tensor.assigned-key-missing",
            "execution-graph-path-tensor-kv",
            "tensor_scope_mismatch",
            _remove_last_tensor("assigned_tensor_keys"),
            **post_load,
        ),
        MutationSpec(
            "tensor.opened-key-missing",
            "execution-graph-path-tensor-kv",
            "tensor_scope_mismatch",
            _remove_last_tensor("opened_tensor_keys"),
            **post_load,
        ),
        MutationSpec(
            "tensor.opened-key-duplicate",
            "execution-graph-path-tensor-kv",
            "tensor_scope_mismatch",
            _stage_opened_tensor_duplicate,
            **post_load,
        ),
        MutationSpec(
            "kv.layer-range",
            "execution-graph-path-tensor-kv",
            "kv_ownership_mismatch",
            _set(
                "run/route-challenge.json",
                "kv_ownership",
                0,
                "owned_layer_range",
                "end",
                value=999,
            ),
            **post_load,
        ),
        MutationSpec(
            "kv.remote-access",
            "execution-graph-path-tensor-kv",
            "kv_ownership_invalid",
            _set(
                "run/route-challenge.json",
                "kv_ownership",
                0,
                "remote_kv_access",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "timing.sequence-replay",
            "timing-token-numeric-trace",
            "sequence_replay",
            _set(
                "run/route-challenge.json",
                "transport",
                "observed_frame_sequences",
                5,
                value=5,
            ),
            **post_load,
        ),
        MutationSpec(
            "timing.edge-id",
            "timing-token-numeric-trace",
            "timing_evidence_invalid",
            _set(
                "run/route-challenge.json",
                "transport",
                "hop_timings",
                0,
                "edge_id",
                value="wrong-edge",
            ),
            **post_load,
        ),
        MutationSpec(
            "timing.elapsed",
            "timing-token-numeric-trace",
            "timing_evidence_invalid",
            _set(
                "run/route-challenge.json",
                "transport",
                "hop_timings",
                0,
                "receiver_elapsed_ns",
                value=10_001,
            ),
            **post_load,
        ),
        MutationSpec(
            "timing.synthetic-hop",
            "timing-token-numeric-trace",
            "synthetic_timing",
            _set(
                "run/route-challenge.json",
                "transport",
                "hop_timings",
                0,
                "synthetic",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "token.reference-mismatch",
            "timing-token-numeric-trace",
            "token_parity_mismatch",
            _token_reference_mismatch,
            **post_load,
        ),
        MutationSpec(
            "token.short-decode",
            "timing-token-numeric-trace",
            "insufficient_decode_tokens",
            _token_decode_short,
            **post_load,
        ),
        MutationSpec(
            "token.event-sequence-duplicate",
            "timing-token-numeric-trace",
            "sequence_replay",
            _set(
                "run/route-challenge.json",
                "token_parity",
                "event_sequences",
                3,
                value=3,
            ),
            **post_load,
        ),
        MutationSpec(
            "token.activation-count",
            "timing-token-numeric-trace",
            "activation_trace_mismatch",
            _delete(
                "run/route-challenge.json",
                "token_parity",
                "activation_digests",
                0,
            ),
            **post_load,
        ),
        MutationSpec(
            "numeric.passed",
            "timing-token-numeric-trace",
            "numeric_parity_failed",
            _set("run/route-challenge.json", "numeric_parity", "passed", value=False),
            **post_load,
        ),
        MutationSpec(
            "numeric.stage-signature",
            "timing-token-numeric-trace",
            "numeric_parity_failed",
            _set(
                "run/route-challenge.json",
                "numeric_parity",
                "stage_reports",
                0,
                "stage_signature",
                value=WRONG_DIGEST,
            ),
            **post_load,
        ),
        MutationSpec(
            "numeric.over-tolerance",
            "timing-token-numeric-trace",
            "numeric_parity_failed",
            _numeric_over_tolerance,
            **post_load,
        ),
        MutationSpec(
            "trace.prefill-sequence",
            "timing-token-numeric-trace",
            "execution_trace_invalid",
            _set(
                "run/route-challenge.json",
                "execution_trace",
                "prefill_event_sequence",
                value=99,
            ),
            **post_load,
        ),
        MutationSpec(
            "trace.decode-token",
            "timing-token-numeric-trace",
            "execution_trace_invalid",
            _set(
                "run/route-challenge.json",
                "execution_trace",
                "decode_events",
                0,
                "distributed_token_id",
                value=999,
            ),
            **post_load,
        ),
        MutationSpec(
            "trace.timestamp-order",
            "timing-token-numeric-trace",
            "execution_trace_invalid",
            _set(
                "run/route-challenge.json",
                "execution_trace",
                "decode_events",
                1,
                "received_at_monotonic_ns",
                value=1_000_000,
            ),
            **post_load,
        ),
        MutationSpec(
            "trace.decoded-text",
            "timing-token-numeric-trace",
            "decoded_text_parity_mismatch",
            _set(
                "run/route-challenge.json",
                "execution_trace",
                "decoded_text",
                "reference_digest",
                value=WRONG_DIGEST,
            ),
            **post_load,
        ),
        MutationSpec(
            "trace.final-logits-cross-binding",
            "timing-token-numeric-trace",
            "final_logits_parity_failed",
            _final_logits_cross_artifact_mismatch,
            **post_load,
        ),
        MutationSpec(
            "negative.required-run-missing",
            "negative-synthetic-simulator",
            "missing_negative_run_evidence",
            _delete("run/negative-runs.json", "runs", 0),
            **post_load,
        ),
        MutationSpec(
            "negative.duplicate-kind",
            "negative-synthetic-simulator",
            "missing_negative_run_evidence",
            _duplicate_negative_run,
            **post_load,
        ),
        MutationSpec(
            "negative.bool-as-int-ready",
            "negative-synthetic-simulator",
            "missing_negative_run_evidence",
            _set("run/negative-runs.json", "runs", 0, "route_ready", value=0),
            **post_load,
        ),
        MutationSpec(
            "negative.reordered-set-members",
            "negative-synthetic-simulator",
            "missing_negative_run_evidence",
            _reverse("run/negative-runs.json", "runs"),
            **post_load,
        ),
        MutationSpec(
            "negative.synthetic-evidence-class",
            "negative-synthetic-simulator",
            "evidence_class_invalid",
            _set(
                "run/route-challenge.json",
                "evidence_class",
                value="synthetic_test_fixture",
            ),
        ),
        MutationSpec(
            "negative.simulator-participation",
            "negative-synthetic-simulator",
            "simulator_participation",
            _set(
                "run/route-challenge.json",
                "transport",
                "simulator_participated",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "negative.fixture-port",
            "negative-synthetic-simulator",
            "fixture_participation",
            _set(
                "run/route-challenge.json",
                "transport",
                "fixture_port_participated",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "negative.synthetic-timing",
            "negative-synthetic-simulator",
            "synthetic_timing",
            _set(
                "run/route-challenge.json",
                "transport",
                "synthetic_timing",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "negative.full-model-fallback",
            "negative-synthetic-simulator",
            "full_model_fallback",
            _set(
                "run/route-challenge.json",
                "token_parity",
                "full_model_fallback",
                value=True,
            ),
            **post_load,
        ),
        MutationSpec(
            "provenance.source-manifest-bytes",
            "provenance-locks-evidence-manifest",
            "source_manifest_digest_mismatch",
            _mutate_extra_file("provenance/source-manifest.json"),
        ),
        MutationSpec(
            "provenance.environment-bytes",
            "provenance-locks-evidence-manifest",
            "environment_digest_mismatch",
            _mutate_extra_file("provenance/environment.json"),
        ),
        MutationSpec(
            "provenance.contract-manifest-bytes",
            "provenance-locks-evidence-manifest",
            "contract_manifest_digest_mismatch",
            _mutate_extra_file("provenance/contract-manifest.v1.json"),
        ),
        MutationSpec(
            "provenance.dependency-lock-bytes",
            "provenance-locks-evidence-manifest",
            "dependency_lock_digest_mismatch",
            _mutate_extra_file("provenance/native-iroh-Cargo.lock"),
        ),
        MutationSpec(
            "provenance.dependency-lock-order",
            "provenance-locks-evidence-manifest",
            "invalid_dependency_locks",
            _reverse("qualification/source-provenance.json", "dependency_locks"),
        ),
        MutationSpec(
            "provenance.dependency-lock-duplicate",
            "provenance-locks-evidence-manifest",
            "invalid_dependency_locks",
            _provenance_duplicate_lock,
        ),
        MutationSpec(
            "manifest.run-id",
            "provenance-locks-evidence-manifest",
            "evidence_manifest_run_id_mismatch",
            mutate_manifest=lambda manifest: manifest.__setitem__("run_id", "wrong-run"),
        ),
        MutationSpec(
            "manifest.file-digest",
            "provenance-locks-evidence-manifest",
            "evidence_file_digest_mismatch",
            mutate_manifest=_manifest_wrong_file_digest,
        ),
        MutationSpec(
            "manifest.entry-order",
            "provenance-locks-evidence-manifest",
            "evidence_manifest_not_canonical",
            mutate_manifest=lambda manifest: manifest["files"].reverse(),
        ),
        MutationSpec(
            "manifest.duplicate-entry",
            "provenance-locks-evidence-manifest",
            "duplicate_evidence_path",
            mutate_manifest=_manifest_duplicate_entry,
        ),
        MutationSpec(
            "manifest.bool-file-size",
            "provenance-locks-evidence-manifest",
            "invalid_evidence_file_size",
            mutate_manifest=_manifest_bool_size,
        ),
        MutationSpec(
            "manifest.unknown-field",
            "provenance-locks-evidence-manifest",
            "unknown_evidence_manifest_field",
            mutate_manifest=_manifest_unknown_field,
        ),
        MutationSpec(
            "schema.duplicate-json-field",
            "schema-canonicalization-types",
            "duplicate_json_key",
            mutate_files=_raw_duplicate_challenge_key,
        ),
        MutationSpec(
            "schema.nonfinite-number",
            "schema-canonicalization-types",
            "invalid_evidence_json",
            mutate_files=_raw_nonfinite_numeric_value,
        ),
        MutationSpec(
            "schema.punctuation-alias",
            "schema-canonicalization-types",
            "route_challenge_invalid",
            _set("run/route-challenge.json", "deployment-id", value="alias"),
        ),
        MutationSpec(
            "schema.case-alias",
            "schema-canonicalization-types",
            "route_challenge_invalid",
            _set("run/route-challenge.json", "Deployment_ID", value="alias"),
        ),
        MutationSpec(
            "schema.unknown-nested-field",
            "schema-canonicalization-types",
            "stage_evidence_invalid",
            _set(
                "run/route-challenge.json",
                "stage_evidence",
                0,
                "unknown_field",
                value=False,
            ),
            **post_load,
        ),
        MutationSpec(
            "schema.bool-as-int-epoch",
            "schema-canonicalization-types",
            "deployment_epoch_mismatch",
            _set("run/route-challenge.json", "deployment_epoch", value=True),
            **post_load,
        ),
        MutationSpec(
            "schema.stale-route-challenge",
            "schema-canonicalization-types",
            "stale_route_challenge",
            _stale_challenge,
        ),
        MutationSpec(
            "schema.stale-load-proof",
            "schema-canonicalization-types",
            "stale_load_proof",
            _stale_load_proof,
            **post_load,
        ),
    ]
    return tuple(sorted(specs, key=lambda spec: spec.mutation_id))


RED_MUTATION_IDS = frozenset(
    {
        "negative.reordered-set-members",
        "schema.bool-as-int-epoch",
    }
)
ALL_MUTATION_SPECS = _specs()
MUTATION_SPECS = tuple(
    spec for spec in ALL_MUTATION_SPECS if spec.mutation_id not in RED_MUTATION_IDS
)
RED_COUNTEREXAMPLES = tuple(
    spec for spec in ALL_MUTATION_SPECS if spec.mutation_id in RED_MUTATION_IDS
)


def _materialize(spec: MutationSpec) -> MaterializedMutation:
    case = make_case()
    spec.mutate_case(case)
    files, manifest = case.render()
    if spec.mutate_files is not None:
        spec.mutate_files(files)
        manifest = build_evidence_manifest(
            run_id=TEST_RUN_ID,
            evidence_class="physical_qualification",
            files=files,
        )
    if spec.mutate_manifest is not None:
        spec.mutate_manifest(manifest)
    serialized_bytes = sum(len(content) for content in files.values()) + len(
        canonical_json_bytes(manifest)
    )
    return MaterializedMutation(spec, files, manifest, serialized_bytes)


def _qualify(
    materialized: MaterializedMutation,
    *,
    gossip_verifier: Callable[[bytes, dict[str, Any]], bool],
    load_verifier: Callable[[bytes, dict[str, Any]], bool],
):
    return qualify_route(
        evidence_files=materialized.files,
        evidence_manifest=materialized.manifest,
        now_unix_ms=make_case().now_unix_ms,
        verify_gossip_signature=gossip_verifier,
        verify_load_proof_signature=load_verifier,
    )


def test_canonical_fixture_control_is_accepted_only_by_existing_authority() -> None:
    case = make_case()
    files, manifest = case.render()
    record = qualify_route(
        evidence_files=files,
        evidence_manifest=manifest,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=synthetic_signature_verifier,
        verify_load_proof_signature=synthetic_signature_verifier,
    )

    assert record.route_ready is True
    assert record.qualified_by == "mycelium_qualification.qualifier:RouteQualificationV1"


def test_mutation_corpus_is_deterministic_complete_and_bounded() -> None:
    assert 0 < len(ALL_MUTATION_SPECS) <= MAX_MUTATIONS
    assert [spec.mutation_id for spec in ALL_MUTATION_SPECS] == sorted(
        spec.mutation_id for spec in ALL_MUTATION_SPECS
    )
    assert len({spec.mutation_id for spec in ALL_MUTATION_SPECS}) == len(
        ALL_MUTATION_SPECS
    )
    assert {spec.family for spec in ALL_MUTATION_SPECS} == EXPECTED_FAMILIES
    assert {spec.mutation_id for spec in RED_COUNTEREXAMPLES} == RED_MUTATION_IDS

    total = 0
    for spec in ALL_MUTATION_SPECS:
        first = _materialize(spec)
        second = _materialize(spec)
        assert first.files == second.files, spec.mutation_id
        assert first.manifest == second.manifest, spec.mutation_id
        assert first.serialized_bytes == second.serialized_bytes, spec.mutation_id
        assert first.serialized_bytes <= MAX_SERIALIZED_BYTES_PER_MUTATION, spec.mutation_id
        total += first.serialized_bytes
    assert total <= MAX_SERIALIZED_BYTES_TOTAL


def _assert_mutation_fails_closed(spec: MutationSpec) -> None:
    materialized = _materialize(spec)
    calls = {"gossip": 0, "load": 0}

    def verify_gossip(statement: bytes, signature: dict[str, Any]) -> bool:
        calls["gossip"] += 1
        return synthetic_signature_verifier(statement, signature)

    def verify_load(statement: bytes, signature: dict[str, Any]) -> bool:
        calls["load"] += 1
        return synthetic_signature_verifier(statement, signature)

    try:
        qualification = _qualify(
            materialized,
            gossip_verifier=verify_gossip,
            load_verifier=verify_load,
        )
    except QualificationError as captured:
        assert captured.code == spec.expected_code
        assert calls["gossip"] >= spec.minimum_gossip_verifications
        assert calls["load"] >= spec.minimum_load_verifications
        return
    pytest.fail(
        f"{spec.mutation_id} reached qualification: "
        f"route_ready={qualification.route_ready!r}, qualified_by={qualification.qualified_by!r}"
    )


@pytest.mark.parametrize("spec", MUTATION_SPECS, ids=lambda spec: spec.mutation_id)
def test_every_mutation_fails_at_its_intended_gate_without_qualification(
    spec: MutationSpec,
) -> None:
    _assert_mutation_fails_closed(spec)


@pytest.mark.parametrize("spec", RED_COUNTEREXAMPLES, ids=lambda spec: spec.mutation_id)
def test_minimized_red_counterexample_requires_fail_closed_rejection(
    spec: MutationSpec,
) -> None:
    _assert_mutation_fails_closed(spec)


@pytest.mark.parametrize("callback", ["gossip", "load"])
def test_verifier_callback_exceptions_fail_closed_without_qualification(callback: str) -> None:
    control = MutationSpec(
        mutation_id=f"callback.{callback}-exception",
        family="gossip-signature-generation",
        expected_code="unused",
    )
    materialized = _materialize(control)

    def raises(_statement: bytes, _signature: dict[str, Any]) -> bool:
        raise RuntimeError("deterministic synthetic callback failure")

    qualification = None
    with pytest.raises(QualificationError) as captured:
        qualification = _qualify(
            materialized,
            gossip_verifier=raises if callback == "gossip" else synthetic_signature_verifier,
            load_verifier=raises if callback == "load" else synthetic_signature_verifier,
        )

    assert captured.value.code == (
        "gossip_signature_invalid" if callback == "gossip" else "load_proof_signature_invalid"
    )
    assert qualification is None


def test_bound_constants_are_finite_integers() -> None:
    for value in (
        MAX_MUTATIONS,
        MAX_SERIALIZED_BYTES_PER_MUTATION,
        MAX_SERIALIZED_BYTES_TOTAL,
    ):
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value > 0 and math.isfinite(value)
