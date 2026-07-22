"""Sole RouteQualificationV1 authority and fail-closed physical-evidence gates."""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from typing import Any

import model_manifest as mm
from mycelium_router.layer_builder import (
    LayerBuildError,
    build_execution_graph,
    layer_load_proof_digest,
)
from mycelium_router.serialization import execution_graph_from_dict, execution_graph_to_dict
from mycelium_router.validation import ContractError
from planner_assignment import validate_control_plane_tranche
from weight_provisioning import artifact_report_errors

from .contracts import (
    QUALIFIER_AUTHORITY,
    ROUTE_QUALIFICATION_PROTOCOL,
    RouteQualificationV1,
    StageQualificationBinding,
    route_qualification_to_dict,
)
from .evidence import (
    EvidenceValidationError,
    canonical_json_bytes,
    canonical_json_loads,
    is_sha256_ref,
    sha256_bytes,
    sha256_document,
    validate_evidence_manifest,
)

REQUIRED_NEGATIVE_RUNS = (
    "stale_proof",
    "wrong_revision",
    "wrong_endpoint",
    "missing_tensor",
    "expired_reservation",
    "sequence_replay",
    "dropped_peer",
    "full_model_fallback",
    "simulator_participation",
    "synthetic_timing",
)
_REQUIRED_DOCUMENTS = {
    "qualification/source-provenance.json": "source_provenance",
    "model/model-manifest.json": "model_manifest",
    "control/control-plane-tranche.json": "control_plane_tranche",
    "control/gossip-signature.json": "gossip_signature",
    "runtime/provisioning-reports.json": "provisioning_reports",
    "runtime/load-proofs.json": "load_proofs",
    "runtime/load-proof-signatures.json": "load_proof_signatures",
    "router/execution-graph.json": "execution_graph",
    "run/route-challenge.json": "route_challenge",
    "run/negative-runs.json": "negative_runs",
}
_PIN_FIELDS = frozenset({"path", "size_bytes", "sha256"})


class QualificationError(ValueError):
    """Fail-closed route-qualification error carrying a stable gate code."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")


def _require(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise QualificationError(code, detail)


def _as_mapping(value: Any, code: str) -> dict[str, Any]:
    _require(isinstance(value, Mapping), code)
    return dict(value)


def _as_list(value: Any, code: str) -> list[Any]:
    _require(
        isinstance(value, list),
        code,
    )
    return value


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and value == value.strip() and bool(value)


def _strict_sequences(values: Any, *, minimum_count: int = 1) -> bool:
    if not isinstance(values, list) or len(values) < minimum_count:
        return False
    if not all(_integer(value, minimum=1) for value in values):
        return False
    return all(right == left + 1 for left, right in zip(values, values[1:]))


def _pin_digest(
    pin: Any,
    *,
    files: Mapping[str, bytes],
    code: str,
) -> str:
    _require(isinstance(pin, Mapping) and set(pin) == _PIN_FIELDS, code)
    path = pin["path"]
    _require(_nonempty_string(path) and path in files, code, str(path))
    content = files[path]
    _require(
        _integer(pin["size_bytes"]) and pin["size_bytes"] == len(content),
        code,
        path,
    )
    digest = sha256_bytes(content)
    _require(is_sha256_ref(pin["sha256"]) and pin["sha256"] == digest, code, path)
    return digest


def _load_documents(files: Mapping[str, bytes]) -> dict[str, Any]:
    documents: dict[str, Any] = {}
    for path, name in _REQUIRED_DOCUMENTS.items():
        _require(path in files, "missing_evidence_file", path)
        try:
            documents[name] = canonical_json_loads(files[path], path=path)
        except EvidenceValidationError as exc:
            raise QualificationError(exc.code, exc.detail) from exc
    return documents


def _validate_source_provenance(
    source: Any, files: Mapping[str, bytes]
) -> tuple[str, str, str, tuple[str, ...]]:
    document = _as_mapping(source, "invalid_source_provenance")
    _require(
        set(document)
        == {
            "kind",
            "source_manifest",
            "environment",
            "contract_manifest",
            "dependency_locks",
        },
        "invalid_source_provenance",
    )
    _require(document["kind"] == "qualification_source_provenance_v1", "invalid_source_provenance")
    source_digest = _pin_digest(
        document["source_manifest"], files=files, code="source_manifest_digest_mismatch"
    )
    environment_digest = _pin_digest(
        document["environment"], files=files, code="environment_digest_mismatch"
    )
    contract_digest = _pin_digest(
        document["contract_manifest"], files=files, code="contract_manifest_digest_mismatch"
    )
    dependencies = _as_list(document["dependency_locks"], "invalid_dependency_locks")
    _require(bool(dependencies), "invalid_dependency_locks")
    paths = [pin.get("path") if isinstance(pin, Mapping) else None for pin in dependencies]
    _require(
        all(_nonempty_string(path) for path in paths)
        and paths == sorted(paths)
        and len(paths) == len(set(paths)),
        "invalid_dependency_locks",
    )
    dependency_digests = tuple(
        _pin_digest(pin, files=files, code="dependency_lock_digest_mismatch")
        for pin in dependencies
    )
    return source_digest, environment_digest, contract_digest, dependency_digests


def _validate_gossip_signature(
    signed_document: Any,
    *,
    tranche: dict[str, Any],
    manifest: dict[str, Any],
    challenge: dict[str, Any],
    now_unix_ms: int,
    verify: Callable[[bytes, dict[str, Any]], bool],
) -> tuple[dict[str, str], str]:
    signed = _as_mapping(signed_document, "invalid_gossip_signature")
    _require(set(signed) == {"kind", "statement", "signature"}, "invalid_gossip_signature")
    _require(signed["kind"] == "detached_gossip_signature_v1", "invalid_gossip_signature")
    statement = _as_mapping(signed["statement"], "invalid_gossip_signature")
    signature = _as_mapping(signed["signature"], "invalid_gossip_signature")
    expected_statement_fields = {
        "kind",
        "run_id",
        "captured_at_unix_ms",
        "evidence_bundle_digest",
        "snapshot_generation",
        "deployment_id",
        "deployment_epoch",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "peers",
    }
    _require(set(statement) == expected_statement_fields, "invalid_gossip_signature")
    _require(
        set(signature)
        == {
            "algorithm",
            "signer_endpoint_id",
            "verification_key_digest",
            "signed_statement_digest",
            "signature",
        },
        "invalid_gossip_signature",
    )
    _require(statement["kind"] == "signed_gossip_snapshot_v1", "invalid_gossip_signature")
    bundle = _as_mapping(tranche.get("evidence_bundle"), "signed_gossip_snapshot_mismatch")
    deployment = _as_mapping(bundle.get("deployment"), "signed_gossip_snapshot_mismatch")
    model = _as_mapping(bundle.get("model"), "signed_gossip_snapshot_mismatch")
    expected = {
        "run_id": challenge.get("run_id"),
        "evidence_bundle_digest": bundle.get("evidence_bundle_digest"),
        "snapshot_generation": bundle.get("snapshot_generation"),
        "deployment_id": deployment.get("deployment_id"),
        "deployment_epoch": deployment.get("deployment_epoch"),
        "model_id": manifest.get("model_id"),
        "resolved_commit": manifest.get("resolved_commit"),
        "manifest_digest": mm.manifest_digest_ref(manifest),
    }
    for field, value in expected.items():
        _require(statement.get(field) == value, "signed_gossip_snapshot_mismatch", field)
    _require(model.get("model_id") == expected["model_id"], "signed_gossip_snapshot_mismatch")
    _require(
        model.get("resolved_commit") == expected["resolved_commit"]
        and model.get("manifest_digest") == expected["manifest_digest"],
        "signed_gossip_snapshot_mismatch",
    )
    captured = statement["captured_at_unix_ms"]
    max_age = challenge.get("max_load_proof_age_ms")
    _require(
        _integer(captured)
        and _integer(max_age, minimum=1)
        and captured <= now_unix_ms
        and now_unix_ms - captured <= max_age,
        "stale_gossip_snapshot",
    )

    peers = _as_list(statement["peers"], "invalid_gossip_peers")
    peer_endpoints: dict[str, str] = {}
    for peer in peers:
        _require(
            isinstance(peer, Mapping)
            and set(peer) == {"node_id", "endpoint_id", "peer_state"},
            "invalid_gossip_peers",
        )
        node_id = peer["node_id"]
        endpoint_id = peer["endpoint_id"]
        _require(
            _nonempty_string(node_id)
            and _nonempty_string(endpoint_id)
            and node_id not in peer_endpoints,
            "invalid_gossip_peers",
        )
        _require(peer["peer_state"] == "alive", "dropped_peer", node_id)
        peer_endpoints[node_id] = endpoint_id
    assignment_nodes = {item["node_id"] for item in tranche["assignments"]}
    _require(set(peer_endpoints) == assignment_nodes, "signed_gossip_snapshot_mismatch", "peers")
    _require(len(set(peer_endpoints.values())) == len(peer_endpoints), "endpoint_id_mismatch")
    _require(
        signature["algorithm"] == "ed25519"
        and is_sha256_ref(signature["verification_key_digest"])
        and _nonempty_string(signature["signature"])
        and signature["signer_endpoint_id"] in set(peer_endpoints.values()),
        "invalid_gossip_signature",
    )
    statement_bytes = canonical_json_bytes(statement)
    _require(
        signature["signed_statement_digest"] == sha256_bytes(statement_bytes),
        "gossip_signature_invalid",
    )
    try:
        verified = verify(statement_bytes, signature)
    except Exception as exc:  # verifier failures must not escape as acceptance
        raise QualificationError("gossip_signature_invalid", str(exc)) from exc
    _require(verified is True, "gossip_signature_invalid")
    return peer_endpoints, bundle["evidence_bundle_digest"]


def _validate_load_proof_signatures(
    signed_set_value: Any,
    *,
    run_id: str,
    assignments: list[dict[str, Any]],
    proofs: list[dict[str, Any]],
    peer_endpoints: dict[str, str],
    challenge: dict[str, Any],
    now_unix_ms: int,
    verify: Callable[[bytes, dict[str, Any]], bool],
) -> dict[str, Any]:
    signed_set = _as_mapping(signed_set_value, "signed_load_proof_set_mismatch")
    _require(
        set(signed_set) == {"kind", "run_id", "signatures"}
        and signed_set["kind"] == "signed_load_proof_set_v1"
        and signed_set["run_id"] == run_id,
        "signed_load_proof_set_mismatch",
    )
    signatures = _as_list(signed_set["signatures"], "signed_load_proof_set_mismatch")
    assignments_by_id = {item["assignment_id"]: item for item in assignments}
    proofs_by_id = {item.get("assignment_id"): item for item in proofs}
    stage_evidence = _as_list(challenge.get("stage_evidence"), "signed_load_proof_set_mismatch")
    evidence_by_assignment = {
        item.get("assignment_id"): item for item in stage_evidence if isinstance(item, dict)
    }
    _require(
        len(assignments_by_id) == len(assignments)
        and set(proofs_by_id) == set(assignments_by_id)
        and set(evidence_by_assignment) == set(assignments_by_id)
        and len(signatures) == len(assignments),
        "signed_load_proof_set_mismatch",
    )
    seen: set[str] = set()
    statement_fields = {
        "kind",
        "run_id",
        "assignment_id",
        "node_id",
        "endpoint_id",
        "process_id",
        "process_host_id",
        "deployment_id",
        "deployment_epoch",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "load_proof_digest",
        "load_proof_generated_at_unix_ms",
    }
    signature_fields = {
        "algorithm",
        "signer_endpoint_id",
        "verification_key_digest",
        "signed_statement_digest",
        "signature",
    }
    for signed_value in signatures:
        signed = _as_mapping(signed_value, "signed_load_proof_mismatch")
        _require(set(signed) == {"statement", "signature"}, "signed_load_proof_mismatch")
        statement = _as_mapping(signed["statement"], "signed_load_proof_mismatch")
        signature = _as_mapping(signed["signature"], "signed_load_proof_mismatch")
        _require(
            set(statement) == statement_fields and set(signature) == signature_fields,
            "signed_load_proof_mismatch",
        )
        assignment_id = statement.get("assignment_id")
        _require(
            _nonempty_string(assignment_id)
            and assignment_id in assignments_by_id
            and assignment_id not in seen,
            "signed_load_proof_set_mismatch",
        )
        seen.add(assignment_id)
        assignment = assignments_by_id[assignment_id]
        proof = proofs_by_id[assignment_id]
        evidence = evidence_by_assignment[assignment_id]
        endpoint_id = peer_endpoints.get(assignment["node_id"])
        expected = {
            "kind": "signed_load_proof_v1",
            "run_id": run_id,
            "assignment_id": assignment_id,
            "node_id": assignment["node_id"],
            "endpoint_id": endpoint_id,
            "process_id": evidence.get("process_id"),
            "process_host_id": evidence.get("process_host_id"),
            "deployment_id": assignment["deployment_id"],
            "deployment_epoch": assignment["deployment_epoch"],
            "model_id": assignment["model_id"],
            "resolved_commit": assignment["resolved_commit"],
            "manifest_digest": assignment["manifest_digest"],
            "load_proof_digest": layer_load_proof_digest(proof),
        }
        _require(
            {key: value for key, value in statement.items() if key != "load_proof_generated_at_unix_ms"}
            == expected,
            "signed_load_proof_mismatch",
        )
        generated = statement["load_proof_generated_at_unix_ms"]
        _require(
            _integer(generated)
            and generated <= now_unix_ms
            and now_unix_ms - generated <= challenge["max_load_proof_age_ms"],
            "stale_load_proof",
        )
        statement_bytes = canonical_json_bytes(statement)
        _require(
            signature["algorithm"] == "ed25519"
            and signature["signer_endpoint_id"] == endpoint_id
            and is_sha256_ref(signature["verification_key_digest"])
            and _nonempty_string(signature["signature"])
            and signature["signed_statement_digest"] == sha256_bytes(statement_bytes),
            "load_proof_signature_invalid",
        )
        try:
            verified = verify(statement_bytes, signature)
        except Exception as exc:
            raise QualificationError("load_proof_signature_invalid", str(exc)) from exc
        _require(verified is True, "load_proof_signature_invalid")
    _require(seen == set(assignments_by_id), "signed_load_proof_set_mismatch")
    return signed_set


def _validate_provisioning(
    reports_value: Any, assignments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    reports_raw = _as_list(reports_value, "provisioning_report_invalid")
    _require(all(isinstance(report, dict) for report in reports_raw), "provisioning_report_invalid")
    reports: list[dict[str, Any]] = reports_raw
    by_assignment: dict[str, dict[str, Any]] = {}
    for report in reports:
        assignment_id = report.get("assignment_id")
        _require(
            _nonempty_string(assignment_id) and assignment_id not in by_assignment,
            "provisioning_report_invalid",
        )
        by_assignment[assignment_id] = report
    _require(
        set(by_assignment) == {assignment["assignment_id"] for assignment in assignments},
        "provisioning_report_invalid",
    )
    for assignment in assignments:
        report = by_assignment[assignment["assignment_id"]]
        _require(not artifact_report_errors(assignment, report), "provisioning_report_invalid")
    return reports


def _validate_graph_and_load_proofs(
    *,
    tranche: dict[str, Any],
    proofs_value: Any,
    graph_value: Any,
    manifest: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    proofs_raw = _as_list(proofs_value, "load_proof_chain_invalid")
    _require(all(isinstance(proof, dict) for proof in proofs_raw), "load_proof_chain_invalid")
    proofs: list[dict[str, Any]] = proofs_raw
    graph_document = _as_mapping(graph_value, "execution_graph_chain_invalid")
    try:
        graph = execution_graph_from_dict(graph_document)
        normalized_graph = execution_graph_to_dict(graph)
    except (ContractError, KeyError, TypeError, ValueError) as exc:
        raise QualificationError("execution_graph_chain_invalid", str(exc)) from exc
    runtime_endpoints: dict[str, str] = {}
    for stage in normalized_graph["stages"]:
        _require(len(stage["placements"]) == 1, "execution_graph_chain_invalid")
        placement = stage["placements"][0]
        assignment_id = placement["assignment_id"]
        _require(assignment_id not in runtime_endpoints, "execution_graph_chain_invalid")
        runtime_endpoints[assignment_id] = placement["runtime_endpoint"]
    try:
        expected = build_execution_graph(
            tranche,
            proofs,
            manifest=manifest,
            runtime_endpoints=runtime_endpoints,
            topology_version=normalized_graph["topology_version"],
            token_envelope_bytes=normalized_graph["token_envelope_bytes"],
        )
    except LayerBuildError as exc:
        raise QualificationError("load_proof_chain_invalid", exc.code) from exc
    expected_document = execution_graph_to_dict(expected)
    _require(
        canonical_json_bytes(expected_document) == canonical_json_bytes(normalized_graph),
        "execution_graph_chain_invalid",
    )
    return proofs, normalized_graph


def _validate_identity(
    *, challenge: dict[str, Any], graph: dict[str, Any], manifest: dict[str, Any]
) -> None:
    expected = (
        ("deployment_id", graph["deployment_id"], "deployment_id_mismatch"),
        ("deployment_epoch", graph["deployment_epoch"], "deployment_epoch_mismatch"),
        ("topology_version", graph["topology_version"], "topology_version_mismatch"),
        ("model_id", manifest["model_id"], "model_id_mismatch"),
        ("resolved_commit", manifest["resolved_commit"], "model_revision_mismatch"),
        ("manifest_digest", mm.manifest_digest_ref(manifest), "manifest_digest_mismatch"),
    )
    for field, value, code in expected:
        observed = challenge.get(field)
        _require(type(observed) is type(value) and observed == value, code)


def _validate_path_and_stages(
    *,
    challenge: dict[str, Any],
    graph: dict[str, Any],
    assignments: list[dict[str, Any]],
    proofs: list[dict[str, Any]],
    peer_endpoints: dict[str, str],
    now_unix_ms: int,
) -> tuple[tuple[StageQualificationBinding, ...], list[dict[str, Any]]]:
    path = _as_mapping(challenge.get("path_manifest"), "path_manifest_mismatch")
    expected_path_fields = {
        "kind",
        "path_id",
        "path_attempt",
        "request_id",
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
        "manifest_digest",
        "ordered_hops",
        "forward_edge_ids",
        "loopback_edge_id",
    }
    _require(set(path) == expected_path_fields, "path_manifest_mismatch")
    _require(path["kind"] == "qualified_path_manifest_v1", "path_manifest_mismatch")
    for field in (
        "deployment_id",
        "deployment_epoch",
        "topology_version",
        "model_id",
        "resolved_commit",
        "manifest_digest",
    ):
        _require(path[field] == graph[field], "path_manifest_mismatch", field)
    _require(_nonempty_string(path["path_id"]), "path_manifest_mismatch")
    _require(_integer(path["path_attempt"], minimum=1), "path_manifest_mismatch")
    _require(_nonempty_string(path["request_id"]), "path_manifest_mismatch")
    _require(
        path["forward_edge_ids"] == [edge["edge_id"] for edge in graph["edges"]]
        and path["loopback_edge_id"] == graph["loopback_edges"][0]["edge_id"],
        "path_manifest_mismatch",
    )
    hops = _as_list(path["ordered_hops"], "path_manifest_mismatch")
    stages = graph["stages"]
    _require(len(hops) == len(stages), "path_manifest_mismatch")

    assignments_by_id = {item["assignment_id"]: item for item in assignments}
    proofs_by_id = {item.get("assignment_id"): item for item in proofs}
    _require(
        len(assignments_by_id) == len(assignments)
        and set(proofs_by_id) == set(assignments_by_id),
        "load_proof_chain_invalid",
    )
    stage_evidence_raw = _as_list(challenge.get("stage_evidence"), "stage_evidence_mismatch")
    _require(len(stage_evidence_raw) == len(stages), "stage_evidence_mismatch")
    evidence_by_stage: dict[str, dict[str, Any]] = {}
    stage_evidence_fields = {
        "stage_id", "placement_id", "assignment_id", "node_id",
        "stage_signature", "load_proof_digest", "load_proof_generated_at_unix_ms",
        "load_generation", "probe_digest", "stage_probe_result_digest",
        "endpoint_id", "authenticated_endpoint_id", "runtime_endpoint",
        "process_id", "process_host_id", "assigned_tensor_keys",
        "opened_tensor_keys", "reservation_id", "stage_compute_observed",
    }
    for item in stage_evidence_raw:
        _require(
            isinstance(item, dict) and set(item) == stage_evidence_fields,
            "stage_evidence_invalid",
        )
        stage_id = item.get("stage_id")
        _require(_nonempty_string(stage_id) and stage_id not in evidence_by_stage, "stage_evidence_mismatch")
        evidence_by_stage[stage_id] = item

    bindings: list[StageQualificationBinding] = []
    process_pairs: set[tuple[str, int]] = set()
    process_hosts: set[str] = set()
    for index, (stage, hop) in enumerate(zip(stages, hops)):
        _require(
            isinstance(hop, Mapping)
            and set(hop)
            == {
                "hop_index",
                "stage_id",
                "placement_id",
                "reservation_id",
                "reservation_epoch",
                "reservation_expires_at_unix_ms",
            },
            "path_manifest_mismatch",
        )
        placement = stage["placements"][0]
        _require(
            hop["hop_index"] == index
            and hop["stage_id"] == stage["stage_id"]
            and hop["placement_id"] == placement["placement_id"],
            "path_manifest_mismatch",
        )
        _require(
            _nonempty_string(hop["reservation_id"])
            and hop["reservation_epoch"] == graph["deployment_epoch"],
            "reservation_mismatch",
        )
        _require(
            _integer(hop["reservation_expires_at_unix_ms"])
            and hop["reservation_expires_at_unix_ms"] > now_unix_ms,
            "expired_reservation",
        )

        evidence = evidence_by_stage.get(stage["stage_id"])
        _require(evidence is not None, "stage_evidence_mismatch")
        assignment = assignments_by_id[placement["assignment_id"]]
        proof = proofs_by_id[placement["assignment_id"]]
        for field, expected, code in (
            ("placement_id", placement["placement_id"], "path_manifest_mismatch"),
            ("assignment_id", placement["assignment_id"], "assignment_id_mismatch"),
            ("node_id", placement["node_id"], "node_id_mismatch"),
            ("stage_signature", placement["stage_signature"], "stage_signature_mismatch"),
            ("load_proof_digest", placement["load_proof_digest"], "load_proof_digest_mismatch"),
            ("load_generation", proof["load_generation"], "load_generation_mismatch"),
            ("probe_digest", proof["probe_digest"], "probe_digest_mismatch"),
            ("runtime_endpoint", placement["runtime_endpoint"], "endpoint_id_mismatch"),
            ("reservation_id", hop["reservation_id"], "reservation_mismatch"),
        ):
            _require(evidence.get(field) == expected, code)
        endpoint_id = peer_endpoints.get(placement["node_id"])
        _require(
            evidence.get("endpoint_id") == endpoint_id
            and evidence.get("authenticated_endpoint_id") == endpoint_id,
            "endpoint_id_mismatch",
        )
        generated = evidence.get("load_proof_generated_at_unix_ms")
        max_age = challenge["max_load_proof_age_ms"]
        _require(
            _integer(generated)
            and generated <= challenge["generated_at_unix_ms"]
            and now_unix_ms - generated <= max_age,
            "stale_load_proof",
        )
        _require(evidence.get("stage_compute_observed") is True, "stage_compute_missing")
        _require(
            is_sha256_ref(evidence.get("stage_probe_result_digest")),
            "stage_probe_result_invalid",
        )
        assigned_keys = evidence.get("assigned_tensor_keys")
        opened_keys = evidence.get("opened_tensor_keys")
        _require(
            assigned_keys == assignment["expected_tensor_keys"]
            and opened_keys == proof["loaded_tensor_keys"]
            and assigned_keys == opened_keys
            and bool(assigned_keys),
            "tensor_scope_mismatch",
        )
        process_id = evidence.get("process_id")
        process_host_id = evidence.get("process_host_id")
        _require(
            _integer(process_id, minimum=1) and _nonempty_string(process_host_id),
            "process_identity_invalid",
        )
        process_pair = (process_host_id, process_id)
        _require(process_pair not in process_pairs, "process_identity_invalid")
        process_pairs.add(process_pair)
        process_hosts.add(process_host_id)
        tensor_digest = sha256_document(
            {
                "assignment_id": assignment["assignment_id"],
                "assigned_tensor_keys": assigned_keys,
                "opened_tensor_keys": opened_keys,
            }
        )
        bindings.append(
            StageQualificationBinding(
                stage_id=stage["stage_id"],
                placement_id=placement["placement_id"],
                assignment_id=placement["assignment_id"],
                node_id=placement["node_id"],
                stage_signature=placement["stage_signature"],
                load_proof_digest=placement["load_proof_digest"],
                stage_probe_result_digest=evidence["stage_probe_result_digest"],
                endpoint_id=endpoint_id,
                process_id=process_id,
                process_host_id=process_host_id,
                tensor_scope_digest=tensor_digest,
                reservation_id=hop["reservation_id"],
            )
        )
    _require(
        len(bindings) >= 2 and len(process_hosts) == len(bindings),
        "process_identity_invalid",
    )
    return tuple(bindings), [dict(hop) for hop in hops]


def _validate_transport(
    transport_value: Any,
    bindings: tuple[StageQualificationBinding, ...],
    graph: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    transport = _as_mapping(transport_value, "transport_invalid")
    expected_fields = {
        "adapter", "protocol", "physical_transport_observed",
        "mutual_authentication_observed", "simulator_participated",
        "fixture_port_participated", "synthetic_timing", "timing_source",
        "peer_dropped", "source_endpoint_id", "destination_endpoint_id",
        "observed_frame_sequences", "hop_timings",
    }
    _require(set(transport) == expected_fields, "transport_invalid")
    _require(transport["adapter"] == "mycelium_iroh", "transport_adapter_invalid")
    _require(transport["protocol"] == "mycelium.router_wire.v1", "transport_protocol_invalid")
    _require(transport["physical_transport_observed"] is True, "physical_transport_missing")
    _require(transport["mutual_authentication_observed"] is True, "transport_authentication_missing")
    _require(transport["simulator_participated"] is False, "simulator_participation")
    _require(transport["fixture_port_participated"] is False, "fixture_participation")
    _require(transport["synthetic_timing"] is False, "synthetic_timing")
    _require(transport["timing_source"] == "receiver_monotonic_clock", "synthetic_timing")
    _require(transport["peer_dropped"] is False, "dropped_peer")
    _require(
        transport["source_endpoint_id"] == bindings[0].endpoint_id
        and transport["destination_endpoint_id"] == bindings[-1].endpoint_id,
        "endpoint_id_mismatch",
    )
    _require(
        _strict_sequences(transport["observed_frame_sequences"], minimum_count=2),
        "sequence_replay",
    )

    timings_raw = _as_list(transport["hop_timings"], "timing_evidence_invalid")
    edges = graph["edges"] + graph["loopback_edges"]
    _require(len(timings_raw) == len(edges), "timing_evidence_invalid")
    timings: list[dict[str, Any]] = []
    timing_fields = {
        "edge_id", "source_endpoint_id", "destination_endpoint_id",
        "receiver_started_at_monotonic_ns", "receiver_completed_at_monotonic_ns",
        "receiver_elapsed_ns", "observed_frame_count", "synthetic",
    }
    for index, (value, edge) in enumerate(zip(timings_raw, edges)):

        timing = _as_mapping(value, "timing_evidence_invalid")
        _require(set(timing) == timing_fields, "timing_evidence_invalid")
        _require(timing["synthetic"] is False, "synthetic_timing")
        started = timing["receiver_started_at_monotonic_ns"]
        completed = timing["receiver_completed_at_monotonic_ns"]
        elapsed = timing["receiver_elapsed_ns"]
        _require(
            timing["edge_id"] == edge["edge_id"]
            and timing["source_endpoint_id"] == bindings[index].endpoint_id
            and timing["destination_endpoint_id"]
            == bindings[(index + 1) % len(bindings)].endpoint_id
            and _integer(started) and _integer(completed)
            and _integer(elapsed, minimum=1) and completed > started
            and elapsed == completed - started
            and _integer(timing["observed_frame_count"], minimum=1),
            "timing_evidence_invalid",
        )
        timings.append(timing)
    return transport, timings




def _validate_token_parity(value: Any, stage_count: int) -> dict[str, Any]:
    parity = _as_mapping(value, "token_parity_invalid")
    _require(
        set(parity)
        == {
            "prompt_token_ids", "distributed_token_ids", "reference_token_ids",
            "decode_steps", "event_sequences", "activation_digests",
            "full_model_fallback",
        },
        "token_parity_invalid",
    )
    distributed = parity.get("distributed_token_ids")
    reference = parity.get("reference_token_ids")
    decode_steps = parity.get("decode_steps")
    _require(
        isinstance(distributed, list)
        and isinstance(reference, list)
        and _integer(decode_steps)
        and decode_steps == len(distributed) == len(reference),
        "token_parity_invalid",
    )
    _require(decode_steps >= 8, "insufficient_decode_tokens")
    _require(distributed == reference, "token_parity_mismatch")
    _require(
        all(_integer(token) for token in distributed)
        and isinstance(parity.get("prompt_token_ids"), list)
        and bool(parity["prompt_token_ids"])
        and all(_integer(token) for token in parity["prompt_token_ids"]),
        "token_parity_invalid",
    )
    _require(parity.get("full_model_fallback") is False, "full_model_fallback")
    _require(
        _strict_sequences(parity.get("event_sequences"), minimum_count=decode_steps + 1)
        and len(parity["event_sequences"]) == decode_steps + 1,
        "sequence_replay",
    )
    activations = parity.get("activation_digests")
    _require(
        isinstance(activations, list)
        and len(activations) == stage_count * (decode_steps + 1)
        and all(is_sha256_ref(item) for item in activations),
        "activation_trace_mismatch",
    )
    return parity


def _validate_numeric_parity(
    value: Any, bindings: tuple[StageQualificationBinding, ...]
) -> dict[str, Any]:
    parity = _as_mapping(value, "numeric_parity_failed")
    _require(
        set(parity)
        == {
            "passed", "absolute_tolerance", "stage_reports",
            "final_logits_report",
        },
        "numeric_parity_invalid",
    )
    tolerance = parity.get("absolute_tolerance")
    _require(
        parity.get("passed") is True
        and isinstance(tolerance, (int, float))
        and not isinstance(tolerance, bool)
        and math.isfinite(float(tolerance))
        and float(tolerance) > 0,
        "numeric_parity_failed",
    )
    reports = _as_list(parity.get("stage_reports"), "numeric_parity_failed")
    by_stage: dict[str, dict[str, Any]] = {}
    stage_report_fields = {
        "stage_id", "stage_signature", "max_abs_diff",
        "distributed_digest", "reference_digest",
    }
    for report in reports:
        _require(
            isinstance(report, dict) and set(report) == stage_report_fields,
            "numeric_parity_invalid",
        )
        stage_id = report.get("stage_id")
        _require(_nonempty_string(stage_id) and stage_id not in by_stage, "numeric_parity_failed")
        by_stage[stage_id] = report
    _require(set(by_stage) == {binding.stage_id for binding in bindings}, "numeric_parity_failed")
    for binding in bindings:
        report = by_stage[binding.stage_id]
        difference = report.get("max_abs_diff")
        _require(
            report.get("stage_signature") == binding.stage_signature
            and isinstance(difference, (int, float))
            and not isinstance(difference, bool)
            and math.isfinite(float(difference))
            and 0 <= float(difference) <= float(tolerance)
            and is_sha256_ref(report.get("distributed_digest"))
            and is_sha256_ref(report.get("reference_digest")),
            "numeric_parity_failed",
        )
    return parity


def _validate_execution_trace(
    value: Any,
    *,
    token_parity: dict[str, Any],
    numeric_parity: dict[str, Any],
) -> dict[str, Any]:
    trace = _as_mapping(value, "execution_trace_invalid")
    _require(
        set(trace) == {"prefill_observed", "prefill_event_sequence", "decode_events", "decoded_text", "final_logits"},
        "execution_trace_invalid",
    )
    event_sequences = token_parity["event_sequences"]
    distributed_tokens = token_parity["distributed_token_ids"]
    reference_tokens = token_parity["reference_token_ids"]
    _require(
        trace["prefill_observed"] is True
        and trace["prefill_event_sequence"] == event_sequences[0],
        "execution_trace_invalid",
    )
    events = _as_list(trace["decode_events"], "execution_trace_invalid")
    _require(len(events) == len(distributed_tokens), "execution_trace_invalid")
    expected_event_fields = {
        "sequence",
        "distributed_token_id",
        "reference_token_id",
        "token_envelope_digest",
        "received_at_monotonic_ns",
    }
    prior_timestamp: int | None = None
    for index, event_value in enumerate(events):
        event = _as_mapping(event_value, "execution_trace_invalid")
        timestamp = event.get("received_at_monotonic_ns")
        _require(
            set(event) == expected_event_fields
            and event["sequence"] == event_sequences[index + 1]
            and event["distributed_token_id"] == distributed_tokens[index]
            and event["reference_token_id"] == reference_tokens[index]
            and event["distributed_token_id"] == event["reference_token_id"]
            and is_sha256_ref(event["token_envelope_digest"])
            and _integer(timestamp)
            and (prior_timestamp is None or timestamp > prior_timestamp),
            "execution_trace_invalid",
        )
        prior_timestamp = timestamp
    decoded = _as_mapping(trace["decoded_text"], "decoded_text_parity_mismatch")
    _require(
        set(decoded) == {"distributed_digest", "reference_digest", "match"}
        and decoded["match"] is True
        and is_sha256_ref(decoded["distributed_digest"])
        and decoded["distributed_digest"] == decoded["reference_digest"],
        "decoded_text_parity_mismatch",
    )
    logits = _as_mapping(trace["final_logits"], "final_logits_parity_failed")
    _require(
        set(logits)
        == {"distributed_digest", "reference_digest", "max_abs_diff", "absolute_tolerance", "passed"},
        "final_logits_parity_failed",
    )
    difference = logits["max_abs_diff"]
    tolerance = logits["absolute_tolerance"]
    _require(
        logits["passed"] is True
        and is_sha256_ref(logits["distributed_digest"])
        and is_sha256_ref(logits["reference_digest"])
        and isinstance(difference, (int, float))
        and not isinstance(difference, bool)
        and math.isfinite(float(difference))
        and float(difference) >= 0
        and isinstance(tolerance, (int, float))
        and not isinstance(tolerance, bool)
        and math.isfinite(float(tolerance))
        and float(tolerance) > 0
        and float(difference) <= float(tolerance),
        "final_logits_parity_failed",
    )
    _require(
        numeric_parity.get("final_logits_report") == logits,
        "final_logits_parity_failed",
    )
    return trace


def _validate_kv_ownership(
    value: Any,
    *,
    bindings: tuple[StageQualificationBinding, ...],
    assignments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ownership_raw = _as_list(value, "kv_ownership_invalid")
    _require(len(ownership_raw) == len(bindings), "kv_ownership_invalid")
    assignment_by_id = {item["assignment_id"]: item for item in assignments}
    ownership_by_stage: dict[str, dict[str, Any]] = {}
    expected_fields = {
        "stage_id",
        "node_id",
        "process_id",
        "process_host_id",
        "owned_layer_range",
        "local_kv_observed",
        "remote_kv_access",
        "peak_kv_bytes",
        "trace_digest",
    }
    for item_value in ownership_raw:
        item = _as_mapping(item_value, "kv_ownership_invalid")
        stage_id = item.get("stage_id")
        _require(
            set(item) == expected_fields
            and _nonempty_string(stage_id)
            and stage_id not in ownership_by_stage,
            "kv_ownership_invalid",
        )
        ownership_by_stage[stage_id] = item
    _require(
        set(ownership_by_stage) == {binding.stage_id for binding in bindings},
        "kv_ownership_invalid",
    )
    for binding in bindings:
        item = ownership_by_stage[binding.stage_id]
        assignment = assignment_by_id[binding.assignment_id]
        _require(
            item["node_id"] == binding.node_id
            and item["process_id"] == binding.process_id
            and item["process_host_id"] == binding.process_host_id
            and item["owned_layer_range"] == assignment["range"],
            "kv_ownership_mismatch",
        )
        _require(
            item["local_kv_observed"] is True
            and item["remote_kv_access"] is False
            and _integer(item["peak_kv_bytes"], minimum=1)
            and is_sha256_ref(item["trace_digest"]),
            "kv_ownership_invalid",
        )
    return [ownership_by_stage[binding.stage_id] for binding in bindings]


def _validate_lifecycle_evidence(
    value: Any,
    *,
    run_id: str,
    path_manifest: dict[str, Any],
    graph: dict[str, Any],
    bindings: tuple[StageQualificationBinding, ...],
    token_parity: dict[str, Any],
) -> dict[str, Any]:
    document = _as_mapping(value, "lifecycle_evidence_invalid")
    _require(
        set(document) == {"kind", "run_id", "cancellation", "recovery"}
        and document["kind"] == "route_lifecycle_evidence_v1"
        and document["run_id"] == run_id,
        "lifecycle_evidence_invalid",
    )

    cancellation = _as_mapping(document["cancellation"], "cancellation_evidence_invalid")
    cancellation_fields = {
        "request_id",
        "path_id",
        "path_attempt",
        "path_cancellation_observed",
        "transport_cancellation_observed",
        "entry_terminal_state",
        "remote_terminal_state",
        "post_cancel_token_count",
        "local_kv_released",
        "remote_kv_released",
        "reservations_released",
        "capacity_released",
        "pending_deliveries",
        "trace_digest",
    }
    _require(set(cancellation) == cancellation_fields, "cancellation_evidence_invalid")
    _require(
        _nonempty_string(cancellation["request_id"])
        and cancellation["request_id"] != path_manifest["request_id"]
        and _nonempty_string(cancellation["path_id"])
        and cancellation["path_id"] != path_manifest["path_id"]
        and _integer(cancellation["path_attempt"], minimum=1)
        and is_sha256_ref(cancellation["trace_digest"]),
        "cancellation_evidence_invalid",
    )
    _require(
        cancellation["path_cancellation_observed"] is True
        and cancellation["transport_cancellation_observed"] is True
        and cancellation["entry_terminal_state"] == "cancelled"
        and cancellation["remote_terminal_state"] == "cancelled",
        "cancellation_not_observed",
    )
    _require(
        _integer(cancellation["post_cancel_token_count"])
        and cancellation["post_cancel_token_count"] == 0,
        "post_cancel_token_emitted",
    )
    _require(
        cancellation["local_kv_released"] is True
        and cancellation["remote_kv_released"] is True
        and cancellation["reservations_released"] is True
        and cancellation["capacity_released"] is True
        and _integer(cancellation["pending_deliveries"])
        and cancellation["pending_deliveries"] == 0,
        "cancellation_cleanup_incomplete",
    )

    recovery = _as_mapping(document["recovery"], "recovery_evidence_invalid")
    recovery_fields = {
        "request_id",
        "failed_stage_id",
        "old_placement_id",
        "replacement_placement_id",
        "old_process_id",
        "new_process_id",
        "process_host_id",
        "old_endpoint_id",
        "new_endpoint_id",
        "old_peer_generation",
        "new_peer_generation",
        "old_topology_version",
        "new_topology_version",
        "old_path_attempt",
        "new_path_attempt",
        "failure_observed",
        "remote_disconnect_observed",
        "peer_drop_observed",
        "old_process_exited",
        "replacement_process_started",
        "stale_generation_rejected",
        "stale_frame_rejected",
        "recovery_phase",
        "recovery_prefill_observed",
        "generated_token_ids_before_failure",
        "generated_token_ids_after_recovery",
        "final_token_ids",
        "reference_token_ids",
        "event_sequences",
        "full_model_fallback",
        "local_kv_released",
        "remote_kv_released",
        "reservations_released",
        "capacity_released",
        "pending_deliveries",
        "trace_digest",
    }
    _require(set(recovery) == recovery_fields, "recovery_evidence_invalid")
    failed_stage_id = recovery["failed_stage_id"]
    bindings_by_stage = {binding.stage_id: binding for binding in bindings}
    _require(
        _nonempty_string(failed_stage_id) and failed_stage_id in bindings_by_stage,
        "recovery_stage_mismatch",
    )
    binding = bindings_by_stage[failed_stage_id]
    _require(
        recovery["request_id"] == path_manifest["request_id"]
        and recovery["replacement_placement_id"] == binding.placement_id
        and recovery["new_process_id"] == binding.process_id
        and recovery["process_host_id"] == binding.process_host_id
        and recovery["new_endpoint_id"] == binding.endpoint_id,
        "recovery_stage_mismatch",
    )
    _require(
        _nonempty_string(recovery["old_placement_id"])
        and recovery["old_placement_id"] != recovery["replacement_placement_id"]
        and _integer(recovery["old_process_id"], minimum=1)
        and recovery["old_process_id"] != recovery["new_process_id"]
        and _nonempty_string(recovery["old_endpoint_id"])
        and recovery["old_endpoint_id"] != recovery["new_endpoint_id"],
        "recovery_identity_not_rotated",
    )
    _require(
        _integer(recovery["old_peer_generation"])
        and _integer(recovery["new_peer_generation"], minimum=1)
        and recovery["new_peer_generation"] > recovery["old_peer_generation"],
        "recovery_generation_not_rotated",
    )
    _require(
        _integer(recovery["old_topology_version"])
        and _integer(recovery["new_topology_version"], minimum=1)
        and recovery["old_topology_version"] < recovery["new_topology_version"]
        and recovery["new_topology_version"] == graph["topology_version"],
        "recovery_topology_invalid",
    )
    _require(
        _integer(recovery["old_path_attempt"], minimum=1)
        and _integer(recovery["new_path_attempt"], minimum=2)
        and recovery["old_path_attempt"] < recovery["new_path_attempt"]
        and recovery["new_path_attempt"] == path_manifest["path_attempt"],
        "recovery_path_attempt_invalid",
    )
    _require(
        recovery["failure_observed"] is True
        and recovery["remote_disconnect_observed"] is True
        and recovery["peer_drop_observed"] is True
        and recovery["old_process_exited"] is True
        and recovery["replacement_process_started"] is True,
        "recovery_not_observed",
    )
    _require(
        recovery["stale_generation_rejected"] is True
        and recovery["stale_frame_rejected"] is True,
        "stale_generation_accepted",
    )
    _require(
        recovery["recovery_phase"] == "RECOVERY_PREFILL"
        and recovery["recovery_prefill_observed"] is True,
        "recovery_prefill_missing",
    )
    before = recovery["generated_token_ids_before_failure"]
    after = recovery["generated_token_ids_after_recovery"]
    final = recovery["final_token_ids"]
    reference = recovery["reference_token_ids"]
    _require(
        isinstance(before, list)
        and isinstance(after, list)
        and isinstance(final, list)
        and isinstance(reference, list)
        and bool(before)
        and bool(after)
        and all(_integer(token) for token in before + after + final + reference)
        and before + after == final
        and final == reference == token_parity["distributed_token_ids"]
        and final == token_parity["reference_token_ids"],
        "recovery_token_continuity_invalid",
    )
    _require(
        _strict_sequences(recovery["event_sequences"], minimum_count=len(final))
        and recovery["event_sequences"] == token_parity["event_sequences"][1:],
        "sequence_replay",
    )
    _require(recovery["full_model_fallback"] is False, "full_model_fallback")
    _require(
        recovery["local_kv_released"] is True
        and recovery["remote_kv_released"] is True
        and recovery["reservations_released"] is True
        and recovery["capacity_released"] is True
        and _integer(recovery["pending_deliveries"])
        and recovery["pending_deliveries"] == 0,
        "recovery_cleanup_incomplete",
    )
    _require(is_sha256_ref(recovery["trace_digest"]), "recovery_evidence_invalid")
    return document


def _validate_negative_runs(value: Any, run_id: str) -> dict[str, Any]:
    document = _as_mapping(value, "missing_negative_run_evidence")
    _require(
        set(document) == {"kind", "run_id", "runs"}
        and document["kind"] == "negative_run_set_v1"
        and document["run_id"] == run_id,
        "missing_negative_run_evidence",
    )
    runs = _as_list(document["runs"], "missing_negative_run_evidence")
    indexed: dict[str, dict[str, Any]] = {}
    for run in runs:
        _require(
            isinstance(run, dict)
            and set(run) == {"kind", "route_ready", "reason_code", "evidence_digest"},
            "missing_negative_run_evidence",
        )
        kind = run["kind"]
        _require(_nonempty_string(kind) and kind not in indexed, "missing_negative_run_evidence")
        _require(
            run["route_ready"] is False
            and _nonempty_string(run["reason_code"])
            and is_sha256_ref(run["evidence_digest"]),
            "missing_negative_run_evidence",
        )
        indexed[kind] = run
    _require(tuple(indexed) == REQUIRED_NEGATIVE_RUNS, "missing_negative_run_evidence")
    return document


def qualify_route(
    *,
    evidence_files: Mapping[str, bytes],
    evidence_manifest: Mapping[str, Any],
    now_unix_ms: int,
    verify_gossip_signature: Callable[[bytes, dict[str, Any]], bool],
    verify_load_proof_signature: Callable[[bytes, dict[str, Any]], bool],
) -> RouteQualificationV1:
    """Issue route_ready=true only after every RouteQualificationV1 gate passes."""
    _require(_integer(now_unix_ms), "invalid_qualification_time")
    _require(callable(verify_gossip_signature), "missing_gossip_signature_verifier")
    _require(
        callable(verify_load_proof_signature),
        "missing_load_proof_signature_verifier",
    )
    try:
        if not isinstance(evidence_manifest, Mapping):
            raise EvidenceValidationError("invalid_evidence_manifest")
        manifest_snapshot = canonical_json_loads(
            canonical_json_bytes(dict(evidence_manifest)),
            path="<evidence-manifest>",
        )
        if not isinstance(manifest_snapshot, dict):
            raise EvidenceValidationError("invalid_evidence_manifest")
        if not isinstance(evidence_files, Mapping):
            raise EvidenceValidationError("invalid_evidence_files")
        files_snapshot: dict[str, bytes] = {}
        for path, content in evidence_files.items():
            if not isinstance(path, str) or type(content) is not bytes:
                raise EvidenceValidationError("invalid_evidence_files")
            files_snapshot[path] = bytes(content)
        manifest_binding = validate_evidence_manifest(manifest_snapshot, files_snapshot)
    except EvidenceValidationError as exc:
        raise QualificationError(exc.code, exc.detail) from exc
    evidence_manifest = manifest_snapshot
    evidence_files = files_snapshot
    documents = _load_documents(evidence_files)
    challenge = _as_mapping(documents["route_challenge"], "route_challenge_invalid")
    _require(
        "placement_provenance" in challenge,
        "placement_provenance_missing",
    )
    _require(
        set(challenge)
        == {
            "kind", "run_id", "evidence_class", "generated_at_unix_ms",
            "valid_until_unix_ms", "max_load_proof_age_ms", "deployment_id",
            "deployment_epoch", "topology_version", "placement_provenance", "model_id",
            "resolved_commit", "manifest_digest", "path_manifest", "stage_evidence", "transport",
            "token_parity", "numeric_parity", "execution_trace", "kv_ownership",
            "lifecycle_evidence",
        },
        "route_challenge_invalid",
    )
    _require(
        challenge.get("kind") == "route_challenge_evidence_v1",
        "route_challenge_invalid",
    )
    placement_provenance = challenge.get("placement_provenance")
    _require(
        isinstance(placement_provenance, str)
        and placement_provenance
        in {"frozen_fixture", "planner_v2"},
        "placement_provenance_invalid",
    )
    run_id_value = challenge.get("run_id")
    _require(_nonempty_string(run_id_value), "route_challenge_invalid")
    run_id = str(run_id_value)
    _require(
        evidence_manifest.get("run_id") == run_id,
        "evidence_manifest_run_id_mismatch",
    )
    _require(
        evidence_manifest.get("evidence_class") == "physical_qualification"
        and challenge.get("evidence_class") == "physical_qualification",
        "evidence_class_invalid",
    )
    generated = challenge.get("generated_at_unix_ms")
    valid_until = challenge.get("valid_until_unix_ms")
    max_age = challenge.get("max_load_proof_age_ms")
    _require(
        _integer(generated)
        and _integer(valid_until)
        and _integer(max_age, minimum=1)
        and generated <= now_unix_ms < valid_until,
        "stale_route_challenge",
    )

    (
        source_manifest_digest,
        environment_digest,
        contract_manifest_digest,
        dependency_lock_digests,
    ) = _validate_source_provenance(documents["source_provenance"], evidence_files)
    model_manifest = _as_mapping(documents["model_manifest"], "invalid_model_manifest")
    _require(mm.verify_manifest_digest(model_manifest), "invalid_model_manifest")
    tranche = _as_mapping(documents["control_plane_tranche"], "control_plane_chain_invalid")
    try:
        validate_control_plane_tranche(tranche, manifest=model_manifest)
    except (KeyError, TypeError, ValueError) as exc:
        raise QualificationError("control_plane_chain_invalid", str(exc)) from exc
    assignments = tranche["assignments"]
    peer_endpoints, gossip_snapshot_digest = _validate_gossip_signature(
        documents["gossip_signature"],
        tranche=tranche,
        manifest=model_manifest,
        challenge=challenge,
        now_unix_ms=now_unix_ms,
        verify=verify_gossip_signature,
    )
    reports = _validate_provisioning(documents["provisioning_reports"], assignments)
    proofs, graph = _validate_graph_and_load_proofs(
        tranche=tranche,
        proofs_value=documents["load_proofs"],
        graph_value=documents["execution_graph"],
        manifest=model_manifest,
    )
    signed_load_proofs = _validate_load_proof_signatures(
        documents["load_proof_signatures"],
        run_id=run_id,
        assignments=assignments,
        proofs=proofs,
        peer_endpoints=peer_endpoints,
        challenge=challenge,
        now_unix_ms=now_unix_ms,
        verify=verify_load_proof_signature,
    )
    _validate_identity(challenge=challenge, graph=graph, manifest=model_manifest)
    bindings, reservations = _validate_path_and_stages(
        challenge=challenge,
        graph=graph,
        assignments=assignments,
        proofs=proofs,
        peer_endpoints=peer_endpoints,
        now_unix_ms=now_unix_ms,
    )
    transport, hop_timings = _validate_transport(
        challenge.get("transport"), bindings, graph
    )
    token_parity = _validate_token_parity(challenge.get("token_parity"), len(bindings))
    numeric_parity = _validate_numeric_parity(challenge.get("numeric_parity"), bindings)
    execution_trace = _validate_execution_trace(
        challenge.get("execution_trace"),
        token_parity=token_parity,
        numeric_parity=numeric_parity,
    )
    kv_ownership = _validate_kv_ownership(
        challenge.get("kv_ownership"), bindings=bindings, assignments=assignments
    )
    lifecycle_evidence = _validate_lifecycle_evidence(
        challenge.get("lifecycle_evidence"),
        run_id=run_id,
        path_manifest=challenge["path_manifest"],
        graph=graph,
        bindings=bindings,
        token_parity=token_parity,
    )
    negative_runs = _validate_negative_runs(documents["negative_runs"], run_id)

    path_manifest = challenge["path_manifest"]
    source_provenance = documents["source_provenance"]
    gossip_signature = documents["gossip_signature"]
    qualification_material = {
        "protocol": ROUTE_QUALIFICATION_PROTOCOL,
        "issued_at_unix_ms": now_unix_ms,
        "run_id": run_id,
        "evidence_manifest_digest": manifest_binding,
        "execution_graph_digest": sha256_document(graph),
        "path_manifest_digest": sha256_document(path_manifest),
        "lifecycle_evidence_digest": sha256_document(lifecycle_evidence),
        "negative_runs_digest": sha256_document(negative_runs),
    }
    record_values = dict(
        protocol=ROUTE_QUALIFICATION_PROTOCOL,
        qualification_id=sha256_document(qualification_material),
        issued_at_unix_ms=now_unix_ms,
        evidence_class="physical_qualification",
        route_ready=True,
        reason_codes=(),
        deployment_id=graph["deployment_id"],
        deployment_epoch=graph["deployment_epoch"],
        topology_version=graph["topology_version"],
        placement_provenance=challenge["placement_provenance"],
        model_id=graph["model_id"],
        resolved_commit=graph["resolved_commit"],
        manifest_digest=graph["manifest_digest"],
        gossip_snapshot_digest=gossip_snapshot_digest,
        gossip_signature_digest=sha256_document(gossip_signature),
        planner_snapshot_digest=sha256_document(tranche["planner_snapshot"]),
        route_plan_digest=sha256_document(tranche["route_plan"]),
        assignments_digest=sha256_document(assignments),
        provisioning_reports_digest=sha256_document(reports),
        load_proofs_digest=sha256_document(proofs),
        load_proof_signatures_digest=sha256_document(signed_load_proofs),
        execution_graph_digest=sha256_document(graph),
        path_manifest_digest=sha256_document(path_manifest),
        reservations_digest=sha256_document(reservations),
        stage_bindings=bindings,
        endpoint_set_digest=sha256_document(
            [
                {"node_id": binding.node_id, "endpoint_id": binding.endpoint_id}
                for binding in bindings
            ]
        ),
        process_set_digest=sha256_document(
            [
                {
                    "node_id": binding.node_id,
                    "process_host_id": binding.process_host_id,
                    "process_id": binding.process_id,
                }
                for binding in bindings
            ]
        ),
        tensor_scope_digest=sha256_document(
            [
                {
                    "assignment_id": binding.assignment_id,
                    "tensor_scope_digest": binding.tensor_scope_digest,
                }
                for binding in bindings
            ]
        ),
        transport_digest=sha256_document(transport),
        timing_evidence_digest=sha256_document(hop_timings),
        token_parity_digest=sha256_document(token_parity),
        numeric_parity_digest=sha256_document(numeric_parity),
        execution_trace_digest=sha256_document(execution_trace),
        kv_ownership_digest=sha256_document(kv_ownership),
        lifecycle_evidence_digest=sha256_document(lifecycle_evidence),
        negative_runs_digest=sha256_document(negative_runs),
        source_provenance_digest=sha256_document(source_provenance),
        source_manifest_digest=source_manifest_digest,
        environment_digest=environment_digest,
        contract_manifest_digest=contract_manifest_digest,
        dependency_lock_digests=dependency_lock_digests,
        evidence_manifest_digest=manifest_binding,
        qualified_by=QUALIFIER_AUTHORITY,
        claim_boundary=(
            "all RouteQualificationV1 identity, signed load proof, physical transport, timing, "
            "tensor scope, execution trace, token/output/numeric parity, local KV ownership, "
            "cancellation/recovery lifecycle, negative-run, provenance, and immutable "
            "evidence-manifest gates passed"
        ),
    )
    record = object.__new__(RouteQualificationV1)
    for name, value in record_values.items():
        object.__setattr__(record, name, value)
    route_qualification_to_dict(record)
    return record
