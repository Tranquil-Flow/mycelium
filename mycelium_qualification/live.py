"""Qualification authority for a currently alive physical route session."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from mycelium_router.serialization import execution_graph_from_dict, execution_graph_to_dict

from .contracts import (
    QUALIFIER_AUTHORITY,
    ROUTE_QUALIFICATION_PROTOCOL,
    RouteQualificationV1,
    StageQualificationBinding,
    route_qualification_to_dict,
)
from .evidence import canonical_json_bytes, is_sha256_ref, sha256_document
from .signing import EvidenceSigningError, build_ed25519_verifier
from .qualifier import QUALIFIED_ROUTE_READY


_ATTESTATION_FIELDS = {
    "protocol",
    "captured_at_unix_ms",
    "run_id",
    "entry_node_id",
    "request_id",
    "prompt_token_ids",
    "max_new_tokens",
    "execution_graph",
    "output_token_ids",
    "signed_observations",
    "counters",
}
_OBSERVATION_FIELDS = {
    "protocol",
    "event",
    "monotonic_ns",
    "run_id",
    "deployment_id",
    "node_id",
    "host_id",
    "process_id",
    "endpoint_id",
    "peer_generation",
    "state",
    "route_ready",
    "details",
}


class LiveRouteQualificationError(ValueError):
    """A live-route attestation failed one current physical-readiness gate."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _reject(code: str) -> NoReturn:
    raise LiveRouteQualificationError(code)


def _mapping(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _reject(code)
    return dict(value)


def _tokens(value: Any, code: str) -> tuple[int, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or not all(
            isinstance(token, int) and not isinstance(token, bool) and token >= 0
            for token in value
        )
    ):
        _reject(code)
    return tuple(value)


def _positive_integer(value: Any, code: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        _reject(code)
    return value


def _signed_observations(
    attestation: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    records = attestation.get("signed_observations")
    if not isinstance(records, list) or not records:
        _reject("live_observations_missing")
    verified: list[dict[str, Any]] = []
    keys_by_node: dict[str, dict[str, Any]] = {}
    identity_by_node: dict[str, tuple[str, int, str]] = {}
    for value in records:
        envelope = _mapping(value, "live_observation_invalid")
        if set(envelope) != {"observation", "signature", "verification_key"}:
            _reject("live_observation_invalid")
        observation = _mapping(
            envelope["observation"], "live_observation_invalid"
        )
        signature = _mapping(envelope["signature"], "live_signature_invalid")
        verification_key = _mapping(
            envelope["verification_key"], "live_signature_invalid"
        )
        if (
            set(observation) != _OBSERVATION_FIELDS
            or observation.get("protocol")
            != "mycelium.physical_node_observation.v1"
            or observation.get("run_id") != attestation.get("run_id")
            or observation.get("deployment_id")
            != attestation.get("execution_graph", {}).get("deployment_id")
            or observation.get("route_ready") is not False
            or not isinstance(observation.get("details"), Mapping)
        ):
            _reject("live_observation_invalid")
        node_id = observation.get("node_id")
        host_id = observation.get("host_id")
        process_id = observation.get("process_id")
        endpoint_id = observation.get("endpoint_id")
        if (
            not isinstance(node_id, str)
            or not node_id
            or not isinstance(host_id, str)
            or not host_id
            or not isinstance(process_id, int)
            or isinstance(process_id, bool)
            or process_id <= 0
            or not isinstance(endpoint_id, str)
            or not endpoint_id
            or signature.get("signer_endpoint_id") != endpoint_id
        ):
            _reject("live_identity_invalid")
        try:
            valid = build_ed25519_verifier([verification_key])(
                canonical_json_bytes(observation), signature
            )
        except (EvidenceSigningError, TypeError, ValueError):
            valid = False
        if valid is not True:
            _reject("live_signature_invalid")
        previous_key = keys_by_node.setdefault(node_id, verification_key)
        previous_identity = identity_by_node.setdefault(
            node_id, (host_id, process_id, endpoint_id)
        )
        if previous_key != verification_key or previous_identity != (
            host_id,
            process_id,
            endpoint_id,
        ):
            _reject("live_identity_changed")
        verified.append(envelope)
    identities = tuple(identity_by_node.values())
    if (
        len(identities) < 2
        or len({item[0] for item in identities}) != len(identities)
        or len({item[1] for item in identities}) != len(identities)
        or len({item[2] for item in identities}) != len(identities)
    ):
        _reject("live_physical_identity_not_distinct")
    return tuple(verified)


def _select(
    records: Sequence[Mapping[str, Any]],
    *,
    node_id: str,
    event: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    matches = []
    for record in records:
        observation = record["observation"]
        if observation["node_id"] != node_id or observation["event"] != event:
            continue
        if request_id is not None and observation["details"].get(
            "request_id"
        ) != request_id:
            continue
        matches.append(observation)
    if len(matches) != 1:
        _reject("live_observation_set_invalid")
    return dict(matches[0])


def _select_many(
    records: Sequence[Mapping[str, Any]],
    *,
    node_id: str,
    event: str,
    request_id: str,
) -> tuple[dict[str, Any], ...]:
    matches = tuple(
        dict(record["observation"])
        for record in records
        if record["observation"]["node_id"] == node_id
        and record["observation"]["event"] == event
        and record["observation"]["details"].get("request_id") == request_id
    )
    if not matches:
        _reject("live_observation_set_invalid")
    return matches


def issue_live_route_qualification(
    attestation: Mapping[str, Any],
    *,
    expected_prompt_token_ids: Sequence[int],
    expected_output_token_ids: Sequence[int],
) -> RouteQualificationV1:
    """Issue readiness from one signed startup challenge on an open route.

    This is intentionally not an archival qualification. The caller must pair the
    returned record with a liveness-aware source that drops it when either process
    exits or transport reports a fatal error.
    """

    live = _mapping(attestation, "live_attestation_invalid")
    if (
        set(live) != _ATTESTATION_FIELDS
        or live.get("protocol") != "mycelium.live_route_attestation.v1"
        or not isinstance(live.get("run_id"), str)
        or not live["run_id"]
        or not isinstance(live.get("request_id"), str)
        or not live["request_id"]
    ):
        _reject("live_attestation_invalid")
    captured_at = _positive_integer(
        live.get("captured_at_unix_ms"), "live_capture_time_invalid"
    )
    prompt_tokens = _tokens(live.get("prompt_token_ids"), "live_prompt_invalid")
    output_tokens = _tokens(live.get("output_token_ids"), "live_output_invalid")
    expected_prompt = _tokens(expected_prompt_token_ids, "live_prompt_invalid")
    expected_output = _tokens(expected_output_token_ids, "live_output_invalid")
    if prompt_tokens != expected_prompt or output_tokens != expected_output:
        _reject("live_startup_challenge_mismatch")
    if live.get("max_new_tokens") != len(output_tokens):
        _reject("live_output_invalid")

    graph_document = _mapping(live.get("execution_graph"), "live_graph_invalid")
    try:
        graph = execution_graph_from_dict(graph_document)
        canonical_graph = execution_graph_to_dict(graph)
    except (TypeError, ValueError):
        _reject("live_graph_invalid")
    entry_node_id = live.get("entry_node_id")
    placements = {
        placement.node_id: (stage, placement)
        for stage in graph.stages
        for placement in stage.placements
        if placement.lifecycle_state == "ACTIVE"
    }
    if (
        not isinstance(entry_node_id, str)
        or entry_node_id not in placements
        or len(placements) < 2
    ):
        _reject("live_graph_invalid")

    signed = _signed_observations(live)
    observed_nodes = {item["observation"]["node_id"] for item in signed}
    if observed_nodes != set(placements):
        _reject("live_observation_set_invalid")

    configured: dict[str, dict[str, Any]] = {}
    snapshots: dict[str, dict[str, Any]] = {}
    stage_bindings: list[StageQualificationBinding] = []
    transport_records: list[dict[str, Any]] = []
    runtime_records: list[dict[str, Any]] = []
    for node_id, (stage, placement) in placements.items():
        configured_observation = _select(
            signed, node_id=node_id, event="configured"
        )
        started_observation = _select(signed, node_id=node_id, event="started")
        snapshot_observation = _select(signed, node_id=node_id, event="snapshot")
        details = _mapping(
            configured_observation["details"], "live_configuration_invalid"
        )
        endpoint_address = _mapping(
            details.get("endpoint_addr"), "live_configuration_invalid"
        )
        if (
            details.get("assignment_id") != placement.assignment_id
            or details.get("placement_id") != placement.placement_id
            or details.get("manifest_digest") != graph.manifest_digest
            or endpoint_address.get("id")
            != configured_observation.get("endpoint_id")
            or not is_sha256_ref(details.get("stage_pack_digest"))
            or not is_sha256_ref(details.get("stage_pack_verification_digest"))
        ):
            _reject("live_configuration_invalid")
        started_peer = _mapping(
            started_observation["details"].get("peer"), "live_peer_binding_invalid"
        )
        if (
            started_peer.get("node_id") not in placements
            or started_peer.get("node_id") == node_id
            or started_peer.get("endpoint_id")
            != next(
                item["observation"]["endpoint_id"]
                for item in signed
                if item["observation"]["node_id"] == started_peer.get("node_id")
            )
        ):
            _reject("live_peer_binding_invalid")
        snapshot_details = _mapping(
            snapshot_observation["details"], "live_snapshot_invalid"
        )
        transport = _mapping(
            snapshot_details.get("transport"), "live_snapshot_invalid"
        )
        runtime = _mapping(snapshot_details.get("runtime"), "live_snapshot_invalid")
        if (
            snapshot_details.get("transport_fatal_error") is not None
            or transport.get("local_node_id") != node_id
            or transport.get("peer_node_id") not in set(placements) - {node_id}
            or transport.get("remote_frames_sent", 0) <= 0
            or transport.get("remote_frames_received", 0) <= 0
            or transport.get("route_ready") is not False
            or runtime.get("active_state_count") != 0
            or runtime.get("mode") != details.get("runtime_mode")
            or runtime.get("applied_operation_count", 0) <= 0
        ):
            _reject("live_snapshot_invalid")
        configured[node_id] = configured_observation
        snapshots[node_id] = snapshot_observation
        transport_records.append(transport)
        runtime_records.append(runtime)
        reservation_id = f"live:{live['run_id']}:{placement.assignment_id}"
        stage_bindings.append(
            StageQualificationBinding(
                stage_id=stage.stage_id,
                placement_id=placement.placement_id,
                assignment_id=placement.assignment_id,
                node_id=node_id,
                stage_signature=placement.stage_signature,
                load_proof_digest=placement.load_proof_digest,
                stage_probe_result_digest=sha256_document(configured_observation),
                endpoint_id=configured_observation["endpoint_id"],
                process_id=configured_observation["process_id"],
                process_host_id=configured_observation["host_id"],
                tensor_scope_digest=sha256_document(
                    {
                        "assignment_id": placement.assignment_id,
                        "runtime_mode": details.get("runtime_mode"),
                    }
                ),
                reservation_id=reservation_id,
            )
        )

    inference_started = _select(
        signed,
        node_id=entry_node_id,
        event="inference_started",
        request_id=live["request_id"],
    )
    inference_decoded = _select_many(
        signed,
        node_id=entry_node_id,
        event="inference_decoded",
        request_id=live["request_id"],
    )
    decoded_prefixes: list[tuple[int, ...]] = []
    decoded_statuses: list[object] = []
    for observation in inference_decoded:
        decoded_details = _mapping(
            observation["details"], "live_inference_invalid"
        )
        decoded_output = _mapping(
            decoded_details.get("output"), "live_inference_invalid"
        )
        decoded_prefixes.append(tuple(decoded_output.get("token_ids", ())))
        decoded_statuses.append(decoded_details.get("status"))
    if (
        inference_started["details"].get("status") != "DECODING"
        or decoded_statuses[-1] != "COMPLETED"
        or any(status != "DECODING" for status in decoded_statuses[:-1])
        or decoded_prefixes[-1] != expected_output
        or any(
            len(current) <= len(previous)
            or current[: len(previous)] != previous
            or current != expected_output[: len(current)]
            for previous, current in zip(
                decoded_prefixes, decoded_prefixes[1:], strict=False
            )
        )
    ):
        _reject("live_inference_invalid")

    counters = _mapping(live.get("counters"), "live_counters_invalid")
    if set(counters) != {
        "frames_sent",
        "frames_received",
        "applied_operation_count",
        "fatal",
    } or counters.get("fatal") is not None:
        _reject("live_counters_invalid")
    expected_frames_sent = sum(item["remote_frames_sent"] for item in transport_records)
    expected_frames_received = sum(
        item["remote_frames_received"] for item in transport_records
    )
    expected_operations = sum(
        item["applied_operation_count"] for item in runtime_records
    )
    if (
        counters.get("frames_sent") != expected_frames_sent
        or counters.get("frames_received") != expected_frames_received
        or counters.get("applied_operation_count") != expected_operations
        or expected_frames_sent <= 0
        or expected_frames_received <= 0
        or expected_operations <= 0
    ):
        _reject("live_counters_invalid")

    stage_bindings_tuple = tuple(
        sorted(stage_bindings, key=lambda item: item.stage_id)
    )
    path_manifest = {
        "entry_node_id": entry_node_id,
        "request_id": live["request_id"],
        "prompt_token_ids": list(prompt_tokens),
        "ordered_stage_ids": [stage.stage_id for stage in graph.stages],
    }
    endpoint_set = [
        {"node_id": item.node_id, "endpoint_id": item.endpoint_id}
        for item in stage_bindings_tuple
    ]
    process_set = [
        {
            "node_id": item.node_id,
            "process_host_id": item.process_host_id,
            "process_id": item.process_id,
        }
        for item in stage_bindings_tuple
    ]
    attestation_digest = sha256_document(live)
    qualification_material = {
        "protocol": ROUTE_QUALIFICATION_PROTOCOL,
        "issued_at_unix_ms": captured_at,
        "run_id": live["run_id"],
        "attestation_digest": attestation_digest,
        "execution_graph_digest": sha256_document(canonical_graph),
        "path_manifest_digest": sha256_document(path_manifest),
    }
    record_values = {
        "protocol": ROUTE_QUALIFICATION_PROTOCOL,
        "qualification_id": sha256_document(qualification_material),
        "issued_at_unix_ms": captured_at,
        "evidence_class": "physical_qualification",
        "route_ready": QUALIFIED_ROUTE_READY,
        "reason_codes": (),
        "deployment_id": graph.deployment_id,
        "deployment_epoch": graph.deployment_epoch,
        "topology_version": graph.topology_version,
        "placement_provenance": "frozen_fixture",
        "model_id": graph.model_id,
        "resolved_commit": graph.resolved_commit,
        "manifest_digest": graph.manifest_digest,
        "gossip_snapshot_digest": sha256_document(endpoint_set),
        "gossip_signature_digest": sha256_document(
            [item["signature"] for item in signed]
        ),
        "planner_snapshot_digest": sha256_document(
            {"stages": [stage.stage_id for stage in graph.stages]}
        ),
        "route_plan_digest": sha256_document(canonical_graph),
        "assignments_digest": sha256_document(
            [item.assignment_id for item in stage_bindings_tuple]
        ),
        "provisioning_reports_digest": sha256_document(
            [configured[item.node_id]["details"] for item in stage_bindings_tuple]
        ),
        "load_proofs_digest": sha256_document(
            [item.load_proof_digest for item in stage_bindings_tuple]
        ),
        "load_proof_signatures_digest": sha256_document(
            [item["verification_key"] for item in signed]
        ),
        "execution_graph_digest": sha256_document(canonical_graph),
        "path_manifest_digest": sha256_document(path_manifest),
        "reservations_digest": sha256_document(
            [item.reservation_id for item in stage_bindings_tuple]
        ),
        "stage_bindings": stage_bindings_tuple,
        "endpoint_set_digest": sha256_document(endpoint_set),
        "process_set_digest": sha256_document(process_set),
        "tensor_scope_digest": sha256_document(
            [item.tensor_scope_digest for item in stage_bindings_tuple]
        ),
        "transport_digest": sha256_document(transport_records),
        "timing_evidence_digest": sha256_document(
            [item["observation"]["monotonic_ns"] for item in signed]
        ),
        "token_parity_digest": sha256_document(
            {"prompt_token_ids": list(prompt_tokens), "output_token_ids": list(output_tokens)}
        ),
        "numeric_parity_digest": sha256_document(
            {"evaluated": False, "scope": "startup_token_parity_only"}
        ),
        "execution_trace_digest": sha256_document(list(signed)),
        "kv_ownership_digest": sha256_document(runtime_records),
        "lifecycle_evidence_digest": sha256_document(
            [inference_started, *inference_decoded]
        ),
        "negative_runs_digest": sha256_document(
            {"qualified_operations": ["inference"], "unqualified_operations": ["cancellation", "recovery"]}
        ),
        "source_provenance_digest": sha256_document(
            {"run_id": live["run_id"], "kind": "live_signed_node_observations"}
        ),
        "source_manifest_digest": graph.manifest_digest,
        "environment_digest": sha256_document(process_set),
        "contract_manifest_digest": sha256_document(
            {"attestation_protocol": live["protocol"], "qualification_protocol": ROUTE_QUALIFICATION_PROTOCOL}
        ),
        "dependency_lock_digests": (graph.manifest_digest,),
        "evidence_manifest_digest": attestation_digest,
        "qualified_by": QUALIFIER_AUTHORITY,
        "claim_boundary": (
            "current live inference only: exact graph, distinct signed physical node/process/endpoint "
            "identity, startup token parity, positive bidirectional transport frames, zero active "
            "runtime state, and no transport fatal passed; readiness remains valid only while the "
            "publishing RouteHealthSource observes both node processes alive"
        ),
    }
    record = object.__new__(RouteQualificationV1)
    for name, value in record_values.items():
        object.__setattr__(record, name, value)
    route_qualification_to_dict(record)
    return record


__all__ = ["LiveRouteQualificationError", "issue_live_route_qualification"]
