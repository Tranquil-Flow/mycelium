"""Build exact, dynamic authority documents for frozen-route inference runs."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mycelium_qualification.evidence import canonical_json_bytes, sha256_document
from mycelium_qualification.sealer import REQUIRED_AUTHORITY_DOCUMENTS
from physical_inference_qualification import build_transfer_archive

from .errors import RunnerError

FROZEN_ROUTE_AUTHORITY_PROFILE = "physical_frozen_route_inference_v1"
FROZEN_ROUTE_CHALLENGE_KIND = "physical_frozen_route_challenge_v1"


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise RunnerError(code)


def _clone(value: Any) -> Any:
    try:
        return json.loads(canonical_json_bytes(value))
    except Exception as exc:
        raise RunnerError("authority_evidence_invalid") from exc


def _read_document(source_root: Path, relative_path: str) -> dict[str, Any]:
    candidate = source_root / relative_path
    try:
        resolved_root = source_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        raw = candidate.read_bytes()
    except OSError as exc:
        raise RunnerError("authority_source_document_missing") from exc
    _require(
        resolved.is_relative_to(resolved_root)
        and candidate.is_file()
        and not candidate.is_symlink()
        and 0 < len(raw) <= 1_048_576,
        "authority_source_document_invalid",
    )
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunnerError("authority_source_document_invalid") from exc
    _require(isinstance(value, dict), "authority_source_document_invalid")
    _require(raw == canonical_json_bytes(value), "authority_source_document_noncanonical")
    return value


def _signed_observation_index(
    evidence: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], dict[str, Any]]]:
    raw = evidence.get("signed_observations")
    _require(isinstance(raw, list) and raw, "signed_observations_missing")
    envelopes: list[dict[str, Any]] = []
    indexed: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in raw:
        _require(
            isinstance(candidate, Mapping)
            and set(candidate) == {"observation", "signature", "verification_key"},
            "signed_observation_invalid",
        )
        envelope = _clone(candidate)
        observation = envelope["observation"]
        _require(isinstance(observation, dict), "signed_observation_invalid")
        node_id = observation.get("node_id")
        event = observation.get("event")
        _require(
            isinstance(node_id, str)
            and bool(node_id)
            and isinstance(event, str)
            and bool(event),
            "signed_observation_invalid",
        )
        key = (node_id, event)
        _require(key not in indexed, "signed_observation_duplicate")
        indexed[key] = envelope
        envelopes.append(envelope)
    return envelopes, indexed


def build_frozen_route_authority_documents(
    *,
    controller_config: Mapping[str, Any],
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind one completed physical run to canonical authority documents.

    This profile qualifies only ordinary inference over one frozen placement. It
    explicitly does not claim cancellation or recovery qualification.
    """

    _require(
        controller_config.get("authority_profile") == FROZEN_ROUTE_AUTHORITY_PROFILE,
        "authority_profile_invalid",
    )
    run_plan = controller_config.get("run_plan")
    peers = controller_config.get("peers")
    transfer_manifest = controller_config.get("transfer_manifest")
    membership = controller_config.get("membership_snapshot")
    source_root_value = controller_config.get("source_root")
    _require(isinstance(run_plan, Mapping), "authority_run_plan_invalid")
    _require(isinstance(peers, list) and len(peers) >= 2, "authority_peers_invalid")
    _require(isinstance(transfer_manifest, Mapping), "authority_transfer_manifest_invalid")
    _require(isinstance(membership, Mapping), "authority_membership_invalid")
    _require(isinstance(source_root_value, str), "authority_source_root_invalid")
    assert isinstance(run_plan, Mapping)
    source_root = Path(source_root_value)

    run_id = run_plan.get("run_id")
    expected_tokens = run_plan.get("expected_token_ids")
    output_tokens = evidence.get("output_token_ids")
    operation = evidence.get("command")
    cancellation_scope = operation == "cancel"
    _require(
        isinstance(run_id, str)
        and bool(run_id)
        and evidence.get("run_id") == run_id
        and evidence.get("protocol") == "mycelium.physical_controller_result.v1"
        and operation in {"run", "cancel"}
        and evidence.get("mode") == "physical"
        and evidence.get("physical_execution") is True
        and evidence.get("route_ready") is False
        and evidence.get("release_ready") is False
        and isinstance(expected_tokens, list)
        and evidence.get("expected_token_ids") == expected_tokens
        and evidence.get("recovered_nodes") == []
        and evidence.get("restart_attempts") == {},
        "authority_run_evidence_invalid",
    )
    if cancellation_scope:
        _require(
            evidence.get("cancelled") is True
            and evidence.get("token_parity") is False
            and output_tokens == [],
            "authority_run_evidence_invalid",
        )
    else:
        _require(
            evidence.get("cancelled") is False
            and evidence.get("token_parity") is True
            and bool(expected_tokens)
            and output_tokens == expected_tokens,
            "authority_run_evidence_invalid",
        )

    node_ids = [peer.get("node_id") for peer in peers if isinstance(peer, Mapping)]
    _require(
        len(node_ids) == len(peers)
        and all(isinstance(node_id, str) and node_id for node_id in node_ids)
        and len(set(node_ids)) == len(node_ids),
        "authority_peers_invalid",
    )
    peer_by_node = {str(peer["node_id"]): peer for peer in peers}
    _require(len({peer.get("host_id") for peer in peers}) == len(peers), "physical_hosts_not_distinct")

    identities = evidence.get("identities")
    observations = evidence.get("observations")
    cleanup = evidence.get("cleanup")
    _require(isinstance(identities, Mapping), "authority_identity_evidence_invalid")
    _require(isinstance(observations, Mapping), "authority_observation_evidence_invalid")
    _require(isinstance(cleanup, list), "authority_cleanup_evidence_invalid")
    _require(set(identities) == set(node_ids), "authority_identity_evidence_invalid")
    _require(set(observations) == set(node_ids), "authority_observation_evidence_invalid")
    cleanup_by_node = {
        item.get("node_id"): item for item in cleanup if isinstance(item, Mapping)
    }
    _require(
        set(cleanup_by_node) == set(node_ids)
        and all(item.get("removed") is True for item in cleanup_by_node.values()),
        "authority_cleanup_evidence_invalid",
    )

    signed_observations, signed_index = _signed_observation_index(evidence)
    entry_node_id = run_plan.get("entry_node_id")
    _require(entry_node_id in set(node_ids), "authority_entry_node_invalid")
    required_events = {"configured", "started", "snapshot", "stopping"}
    for node_id in node_ids:
        required = set(required_events)
        if node_id == entry_node_id:
            required.add("inference_started")
            required.add("cancelled" if cancellation_scope else "inference_decoded")
        _require(
            all((node_id, event) in signed_index for event in required),
            "signed_observations_missing",
        )
        identity = identities[node_id]
        peer = peer_by_node[node_id]
        _require(
            isinstance(identity, Mapping)
            and identity.get("run_id") == run_id
            and identity.get("deployment_id") == run_plan.get("deployment_id")
            and identity.get("node_id") == node_id
            and identity.get("host_id") == peer.get("host_id")
            and isinstance(identity.get("process_id"), int)
            and not isinstance(identity.get("process_id"), bool)
            and identity.get("process_id") > 0,
            "authority_identity_evidence_invalid",
        )
        for event in required:
            observation = signed_index[(node_id, event)]["observation"]
            _require(
                observation.get("run_id") == run_id
                and observation.get("deployment_id") == run_plan.get("deployment_id")
                and observation.get("host_id") == peer.get("host_id")
                and observation.get("process_id") == identity.get("process_id")
                and observation.get("route_ready") is False,
                "signed_observation_binding_invalid",
            )
        snapshot = signed_index[(node_id, "snapshot")]["observation"].get("details")
        _require(isinstance(snapshot, Mapping), "physical_snapshot_invalid")
        transport = snapshot.get("transport")
        runtime = snapshot.get("runtime")
        release_counts = runtime.get("release_counts", {}) if isinstance(runtime, Mapping) else {}
        release_reason = "cancellation" if cancellation_scope else "normal_completion"
        _require(
            isinstance(transport, Mapping)
            and transport.get("local_node_id") == node_id
            and transport.get("peer_node_id") in set(node_ids) - {node_id}
            and isinstance(transport.get("remote_frames_sent"), int)
            and transport.get("remote_frames_sent") > 0
            and isinstance(transport.get("remote_frames_received"), int)
            and transport.get("remote_frames_received") > 0
            and transport.get("route_ready") is False
            and snapshot.get("transport_fatal_error") is None
            and isinstance(runtime, Mapping)
            and runtime.get("mode")
            in {"stage_local_kv", "complete_context_replay"}
            and runtime.get("active_state_count") == 0
            and isinstance(release_counts, Mapping)
            and release_counts.get(release_reason) == 1,
            "physical_snapshot_invalid",
        )
        if cancellation_scope:
            _require(
                snapshot.get("transport_pending_delivery_count") == 0
                and snapshot.get("transport_cancellation_cleanup_complete") is True,
                "physical_snapshot_invalid",
            )

    cancellation_record: dict[str, Any] | None = None
    if cancellation_scope:
        cancelled = signed_index[(str(entry_node_id), "cancelled")]["observation"]
        cancelled_details = cancelled.get("details", {})
        cancel_result = (
            cancelled_details.get("result", {})
            if isinstance(cancelled_details, Mapping)
            else {}
        )
        path_id = cancel_result.get("path_id")
        path_attempt = cancel_result.get("path_attempt")
        request = run_plan.get("request")
        request_id = request.get("request_id") if isinstance(request, Mapping) else None
        _require(
            isinstance(cancelled_details, Mapping)
            and cancelled_details.get("request_id") == request_id
            and isinstance(cancel_result, Mapping)
            and cancel_result.get("cancelled") is True
            and isinstance(path_id, str)
            and bool(path_id)
            and isinstance(path_attempt, int)
            and not isinstance(path_attempt, bool)
            and path_attempt >= 0
            and cancel_result.get("status_after") == "CANCELLED"
            and cancel_result.get("post_cancel_token_count") == 0,
            "authority_cancellation_evidence_invalid",
        )
        cancellation_record = {
            "request_id": cancelled_details["request_id"],
            "path_id": path_id,
            "path_attempt": path_attempt,
            "entry_terminal_state": "CANCELLED",
            "remote_terminal_state": "CANCELLED",
            "post_cancel_token_count": 0,
            "transport_cancellation_observed": True,
            "cleanup_complete": True,
        }
    else:
        decoded = signed_index[(str(entry_node_id), "inference_decoded")]["observation"]
        decoded_output = decoded.get("details", {}).get("output", {})
        _require(
            decoded.get("details", {}).get("status") == "COMPLETED"
            and decoded_output.get("token_ids") == expected_tokens,
            "authority_token_evidence_invalid",
        )

    model_manifest = _read_document(source_root, "control/model-manifest.json")
    execution_graph = _read_document(source_root, "control/execution-graph.json")
    assignments = [
        _read_document(source_root, f"control/{node_id}-assignment.json")
        for node_id in node_ids
    ]
    provisioning_reports = [
        _read_document(source_root, f"control/{node_id}-artifact-report.json")
        for node_id in node_ids
    ]
    load_proofs = [
        _read_document(source_root, f"control/{node_id}-load-proof.json")
        for node_id in node_ids
    ]

    archive = build_transfer_archive(source_root, dict(transfer_manifest))
    source_provenance = {
        "kind": "physical_frozen_route_source_provenance_v1",
        "archive_digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
        "archive_size_bytes": len(archive),
        "transfer_manifest": _clone(transfer_manifest),
    }
    control = {
        "kind": "physical_frozen_route_control_v1",
        "entry_node_id": entry_node_id,
        "assignments": assignments,
        "run_plan_digest": sha256_document(run_plan),
    }
    gossip = {
        "kind": "physical_frozen_route_membership_v1",
        "snapshot": _clone(membership),
    }
    load_signatures = {
        "kind": "physical_frozen_route_signed_observations_v1",
        "run_id": run_id,
        "observations": signed_observations,
    }
    now_unix_ms = int(float(controller_config.get("now")) * 1000)
    challenge_expected_tokens = [] if cancellation_scope else _clone(expected_tokens)
    challenge_output_tokens = [] if cancellation_scope else _clone(output_tokens)
    challenge = {
        "kind": FROZEN_ROUTE_CHALLENGE_KIND,
        "run_id": run_id,
        "evidence_class": "physical_qualification",
        "qualification_scope": "cancellation" if cancellation_scope else "inference",
        "generated_at_unix_ms": now_unix_ms,
        "valid_until_unix_ms": now_unix_ms + 60_000,
        "deployment_id": run_plan.get("deployment_id"),
        "deployment_epoch": execution_graph.get("deployment_epoch"),
        "topology_version": execution_graph.get("topology_version"),
        "placement_provenance": "frozen_fixture",
        "model_id": execution_graph.get("model_id"),
        "resolved_commit": execution_graph.get("resolved_commit"),
        "manifest_digest": execution_graph.get("manifest_digest"),
        "entry_node_id": entry_node_id,
        "request": _clone(run_plan.get("request")),
        "expected_token_ids": challenge_expected_tokens,
        "output_token_ids": challenge_output_tokens,
        "identities": _clone(identities),
        "signed_observations": signed_observations,
        "cleanup": _clone(cleanup),
    }
    if cancellation_scope:
        assert cancellation_record is not None
        challenge["cancellation"] = cancellation_record
    qualified_operations = ["cancellation"] if cancellation_scope else ["inference"]
    unqualified_operations = (
        ["inference", "recovery"] if cancellation_scope else ["cancellation", "recovery"]
    )
    claim_boundary = (
        "cross-host cancellation only; inference is not qualified by this record; recovery is not qualified"
        if cancellation_scope
        else "ordinary frozen-placement inference only; cancellation and recovery are not qualified"
    )
    scope = {
        "kind": "physical_frozen_route_scope_v1",
        "run_id": run_id,
        "qualified_operations": qualified_operations,
        "unqualified_operations": unqualified_operations,
        "claim_boundary": claim_boundary,
    }
    documents = {
        "qualification/source-provenance.json": source_provenance,
        "model/model-manifest.json": model_manifest,
        "control/control-plane-tranche.json": control,
        "control/gossip-signature.json": gossip,
        "runtime/provisioning-reports.json": provisioning_reports,
        "runtime/load-proofs.json": load_proofs,
        "runtime/load-proof-signatures.json": load_signatures,
        "router/execution-graph.json": execution_graph,
        "run/route-challenge.json": challenge,
        "run/negative-runs.json": scope,
    }
    _require(set(documents) == set(REQUIRED_AUTHORITY_DOCUMENTS), "authority_documents_invalid")
    return _clone(documents)


__all__ = [
    "FROZEN_ROUTE_AUTHORITY_PROFILE",
    "FROZEN_ROUTE_CHALLENGE_KIND",
    "build_frozen_route_authority_documents",
]
