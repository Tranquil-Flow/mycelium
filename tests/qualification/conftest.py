"""In-memory synthetic_test_fixture inputs for RouteQualificationV1 schema tests.

These documents model the *shape* of physical evidence only.  They are assembled
at test runtime, use a test-only run ID and signature, and are never accepted or
written as physical qualification evidence outside this test process.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from mycelium_qualification.evidence import (
    build_evidence_manifest,
    canonical_json_bytes,
    sha256_bytes,
)
from mycelium_router.layer_builder import build_execution_graph, layer_load_proof_digest
from mycelium_router.serialization import execution_graph_to_dict
from scripts.generate_contract_fixtures import (
    control_plane_documents,
    gossip_documents,
    layer_load_proofs,
    model_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
NOW_UNIX_MS = 1_800_000_000_000
TEST_RUN_ID = "synthetic_test_fixture/hypothetical-physical-shape"
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


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pin(path: str, content: bytes) -> dict[str, Any]:
    return {"path": path, "size_bytes": len(content), "sha256": sha256_bytes(content)}


def _provisioning_reports(assignments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for assignment in assignments:
        expected_bytes = sum(item["size_bytes"] for item in assignment["files"])
        reports.append(
            {
                "protocol": "mycelium.artifact_verification_report.v1",
                "deployment_id": assignment["deployment_id"],
                "deployment_epoch": assignment["deployment_epoch"],
                "assignment_id": assignment["assignment_id"],
                "node_id": assignment["node_id"],
                "manifest_digest": assignment["manifest_digest"],
                "resolved_commit": assignment["resolved_commit"],
                "range": assignment["range"],
                "artifact_cache_root": assignment["artifact_cache_root"],
                "verified_files": assignment["files"],
                "verified_tensor_prefixes": assignment["expected_tensor_prefixes"],
                "verified_tensor_count": len(set(assignment["expected_tensor_keys"])),
                "expected_bytes": expected_bytes,
                "network_download_bytes": 0,
                "cache_hit_bytes": expected_bytes,
                "ready_for_load": True,
                "route_ready": False,
                "claim_boundary": "synthetic_test_fixture artifact identity only",
                "timestamp": "2026-07-18T00:00:00+00:00",
            }
        )
    return reports


@dataclass
class QualificationCase:
    documents: dict[str, Any]
    extra_files: dict[str, bytes]
    now_unix_ms: int = NOW_UNIX_MS

    def clone(self) -> "QualificationCase":
        return QualificationCase(copy.deepcopy(self.documents), dict(self.extra_files), self.now_unix_ms)

    def render(self) -> tuple[dict[str, bytes], dict[str, Any]]:
        files = {
            path: canonical_json_bytes(document)
            for path, document in self.documents.items()
        }
        files.update(self.extra_files)
        manifest = build_evidence_manifest(
            run_id=TEST_RUN_ID,
            evidence_class="physical_qualification",
            files=files,
        )
        return files, manifest


def synthetic_signature_verifier(statement_bytes: bytes, signature: dict[str, Any]) -> bool:
    return (
        signature.get("algorithm") == "ed25519"
        and signature.get("signature") == "synthetic-test-signature-never-production"
        and signature.get("signed_statement_digest") == sha256_bytes(statement_bytes)
    )


def make_case() -> QualificationCase:
    manifest = model_manifest()
    _, _, gossip_bundle = gossip_documents()
    _, _, tranche = control_plane_documents(gossip_bundle)
    proofs = layer_load_proofs(tranche)
    endpoint_ids = {
        assignment["assignment_id"]: f"synthetic-test-endpoint-{index}"
        for index, assignment in enumerate(tranche["assignments"], start=1)
    }
    runtime_endpoints = {
        assignment_id: f"iroh://{endpoint_id}"
        for assignment_id, endpoint_id in endpoint_ids.items()
    }
    graph = execution_graph_to_dict(
        build_execution_graph(
            tranche,
            proofs,
            manifest=manifest,
            runtime_endpoints=runtime_endpoints,
            topology_version=42,
            token_envelope_bytes=1024,
        )
    )
    reports = _provisioning_reports(tranche["assignments"])

    stage_evidence: list[dict[str, Any]] = []
    path_hops: list[dict[str, Any]] = []
    signed_peers: list[dict[str, Any]] = []
    load_proof_signatures: list[dict[str, Any]] = []
    kv_ownership: list[dict[str, Any]] = []
    for index, (stage, assignment, proof) in enumerate(
        zip(graph["stages"], tranche["assignments"], proofs), start=1
    ):
        placement = stage["placements"][0]
        endpoint_id = endpoint_ids[assignment["assignment_id"]]
        reservation_id = f"synthetic-test-reservation-{index}"
        process_id = 4_100 + index
        process_host_id = f"synthetic-test-host-{index}"
        signed_peers.append(
            {
                "node_id": assignment["node_id"],
                "endpoint_id": endpoint_id,
                "peer_state": "alive",
            }
        )
        load_statement = {
            "kind": "signed_load_proof_v1",
            "run_id": TEST_RUN_ID,
            "assignment_id": assignment["assignment_id"],
            "node_id": assignment["node_id"],
            "endpoint_id": endpoint_id,
            "process_id": process_id,
            "process_host_id": process_host_id,
            "deployment_id": assignment["deployment_id"],
            "deployment_epoch": assignment["deployment_epoch"],
            "model_id": assignment["model_id"],
            "resolved_commit": assignment["resolved_commit"],
            "manifest_digest": assignment["manifest_digest"],
            "load_proof_digest": layer_load_proof_digest(proof),
            "load_proof_generated_at_unix_ms": NOW_UNIX_MS - 2_000,
        }
        load_proof_signatures.append(
            {
                "statement": load_statement,
                "signature": {
                    "algorithm": "ed25519",
                    "signer_endpoint_id": endpoint_id,
                    "verification_key_digest": _digest_text(
                        f"synthetic-test-load-proof-key-{index}"
                    ),
                    "signed_statement_digest": sha256_bytes(
                        canonical_json_bytes(load_statement)
                    ),
                    "signature": "synthetic-test-signature-never-production",
                },
            }
        )
        path_hops.append(
            {
                "hop_index": index - 1,
                "stage_id": stage["stage_id"],
                "placement_id": placement["placement_id"],
                "reservation_id": reservation_id,
                "reservation_epoch": assignment["deployment_epoch"],
                "reservation_expires_at_unix_ms": NOW_UNIX_MS + 60_000,
            }
        )
        stage_evidence.append(
            {
                "stage_id": stage["stage_id"],
                "placement_id": placement["placement_id"],
                "assignment_id": assignment["assignment_id"],
                "node_id": assignment["node_id"],
                "stage_signature": placement["stage_signature"],
                "load_proof_digest": layer_load_proof_digest(proof),
                "load_proof_generated_at_unix_ms": NOW_UNIX_MS - 2_000,
                "load_generation": proof["load_generation"],
                "probe_digest": proof["probe_digest"],
                "stage_probe_result_digest": _digest_text(
                    f"synthetic-stage-probe-result-{index}"
                ),
                "endpoint_id": endpoint_id,
                "authenticated_endpoint_id": endpoint_id,
                "runtime_endpoint": placement["runtime_endpoint"],
                "process_id": process_id,
                "process_host_id": process_host_id,
                "assigned_tensor_keys": list(assignment["expected_tensor_keys"]),
                "opened_tensor_keys": list(proof["loaded_tensor_keys"]),
                "reservation_id": reservation_id,
                "stage_compute_observed": True,
            }
        )
        kv_ownership.append(
            {
                "stage_id": stage["stage_id"],
                "node_id": assignment["node_id"],
                "process_id": process_id,
                "process_host_id": process_host_id,
                "owned_layer_range": dict(assignment["range"]),
                "local_kv_observed": True,
                "remote_kv_access": False,
                "peak_kv_bytes": 4_096 * index,
                "trace_digest": _digest_text(f"synthetic-kv-trace-{index}"),
            }
        )

    statement = {
        "kind": "signed_gossip_snapshot_v1",
        "run_id": TEST_RUN_ID,
        "captured_at_unix_ms": NOW_UNIX_MS - 3_000,
        "evidence_bundle_digest": gossip_bundle["evidence_bundle_digest"],
        "snapshot_generation": gossip_bundle["snapshot_generation"],
        "deployment_id": gossip_bundle["deployment"]["deployment_id"],
        "deployment_epoch": gossip_bundle["deployment"]["deployment_epoch"],
        "model_id": gossip_bundle["model"]["model_id"],
        "resolved_commit": gossip_bundle["model"]["resolved_commit"],
        "manifest_digest": gossip_bundle["model"]["manifest_digest"],
        "peers": signed_peers,
    }
    gossip_signature = {
        "kind": "detached_gossip_signature_v1",
        "statement": statement,
        "signature": {
            "algorithm": "ed25519",
            "signer_endpoint_id": signed_peers[0]["endpoint_id"],
            "verification_key_digest": _digest_text("synthetic-test-verification-key"),
            "signed_statement_digest": sha256_bytes(canonical_json_bytes(statement)),
            "signature": "synthetic-test-signature-never-production",
        },
    }

    path_manifest = {
        "kind": "qualified_path_manifest_v1",
        "path_id": "synthetic-test-path-1",
        "path_attempt": 2,
        "request_id": "synthetic-test-request-1",
        "deployment_id": graph["deployment_id"],
        "deployment_epoch": graph["deployment_epoch"],
        "topology_version": graph["topology_version"],
        "model_id": graph["model_id"],
        "resolved_commit": graph["resolved_commit"],
        "manifest_digest": graph["manifest_digest"],
        "ordered_hops": path_hops,
        "forward_edge_ids": [edge["edge_id"] for edge in graph["edges"]],
        "loopback_edge_id": graph["loopback_edges"][0]["edge_id"],
    }
    decode_tokens = [101, 102, 103, 104, 105, 106, 107, 108]
    token_parity = {
        "prompt_token_ids": [11, 12, 13],
        "distributed_token_ids": decode_tokens,
        "reference_token_ids": list(decode_tokens),
        "decode_steps": len(decode_tokens),
        "event_sequences": list(range(1, len(decode_tokens) + 2)),
        "activation_digests": [
            _digest_text(f"synthetic-activation-{stage_index}-{step}")
            for stage_index in range(len(stage_evidence))
            for step in range(len(decode_tokens) + 1)
        ],
        "full_model_fallback": False,
    }
    decoded_text_digest = _digest_text("synthetic-distributed-decoded-text")
    final_logits_distributed_digest = _digest_text("synthetic-distributed-final-logits")
    final_logits_reference_digest = _digest_text("synthetic-reference-final-logits")
    execution_trace = {
        "prefill_observed": True,
        "prefill_event_sequence": 1,
        "decode_events": [
            {
                "sequence": step + 2,
                "distributed_token_id": token_id,
                "reference_token_id": token_id,
                "token_envelope_digest": _digest_text(
                    f"synthetic-token-envelope-{step}"
                ),
                "received_at_monotonic_ns": 1_000_000 + step * 10_000,
            }
            for step, token_id in enumerate(decode_tokens)
        ],
        "decoded_text": {
            "distributed_digest": decoded_text_digest,
            "reference_digest": decoded_text_digest,
            "match": True,
        },
        "final_logits": {
            "distributed_digest": final_logits_distributed_digest,
            "reference_digest": final_logits_reference_digest,
            "max_abs_diff": 0.00001,
            "absolute_tolerance": 0.0001,
            "passed": True,
        },
    }
    numeric_parity = {
        "passed": True,
        "absolute_tolerance": 0.0001,
        "stage_reports": [
            {
                "stage_id": item["stage_id"],
                "stage_signature": item["stage_signature"],
                "max_abs_diff": 0.00001,
                "distributed_digest": _digest_text(f"distributed-{item['stage_id']}"),
                "reference_digest": _digest_text(f"reference-{item['stage_id']}"),
            }
            for item in stage_evidence
        ],
        "final_logits_report": dict(execution_trace["final_logits"]),
    }
    transport = {
        "adapter": "mycelium_iroh",
        "protocol": "mycelium.router_wire.v1",
        "physical_transport_observed": True,
        "mutual_authentication_observed": True,
        "simulator_participated": False,
        "fixture_port_participated": False,
        "synthetic_timing": False,
        "timing_source": "receiver_monotonic_clock",
        "peer_dropped": False,
        "source_endpoint_id": stage_evidence[0]["endpoint_id"],
        "destination_endpoint_id": stage_evidence[-1]["endpoint_id"],
        "observed_frame_sequences": list(range(1, 21)),
        "hop_timings": [
            {
                "edge_id": edge["edge_id"],
                "source_endpoint_id": stage_evidence[index]["endpoint_id"],
                "destination_endpoint_id": stage_evidence[
                    (index + 1) % len(stage_evidence)
                ]["endpoint_id"],
                "receiver_started_at_monotonic_ns": 2_000_000 + index * 100_000,
                "receiver_completed_at_monotonic_ns": 2_010_000 + index * 100_000,
                "receiver_elapsed_ns": 10_000,
                "observed_frame_count": 10,
                "synthetic": False,
            }
            for index, edge in enumerate(graph["edges"] + graph["loopback_edges"])
        ],
    }
    lifecycle_evidence = {
        "kind": "route_lifecycle_evidence_v1",
        "run_id": TEST_RUN_ID,
        "cancellation": {
            "request_id": "synthetic-test-cancel-request-1",
            "path_id": "synthetic-test-cancel-path-1",
            "path_attempt": 1,
            "path_cancellation_observed": True,
            "transport_cancellation_observed": True,
            "entry_terminal_state": "cancelled",
            "remote_terminal_state": "cancelled",
            "post_cancel_token_count": 0,
            "local_kv_released": True,
            "remote_kv_released": True,
            "reservations_released": True,
            "capacity_released": True,
            "pending_deliveries": 0,
            "trace_digest": _digest_text("synthetic-cancellation-trace"),
        },
        "recovery": {
            "request_id": path_manifest["request_id"],
            "failed_stage_id": stage_evidence[-1]["stage_id"],
            "old_placement_id": "synthetic-test-old-placement-2",
            "replacement_placement_id": stage_evidence[-1]["placement_id"],
            "old_process_id": 4_002,
            "new_process_id": stage_evidence[-1]["process_id"],
            "process_host_id": stage_evidence[-1]["process_host_id"],
            "old_endpoint_id": "synthetic-test-old-endpoint-2",
            "new_endpoint_id": stage_evidence[-1]["endpoint_id"],
            "old_peer_generation": 1,
            "new_peer_generation": 2,
            "old_topology_version": graph["topology_version"] - 1,
            "new_topology_version": graph["topology_version"],
            "old_path_attempt": 1,
            "new_path_attempt": path_manifest["path_attempt"],
            "failure_observed": True,
            "remote_disconnect_observed": True,
            "peer_drop_observed": True,
            "old_process_exited": True,
            "replacement_process_started": True,
            "stale_generation_rejected": True,
            "stale_frame_rejected": True,
            "recovery_phase": "RECOVERY_PREFILL",
            "recovery_prefill_observed": True,
            "generated_token_ids_before_failure": decode_tokens[:3],
            "generated_token_ids_after_recovery": decode_tokens[3:],
            "final_token_ids": list(decode_tokens),
            "reference_token_ids": list(decode_tokens),
            "event_sequences": list(range(2, len(decode_tokens) + 2)),
            "full_model_fallback": False,
            "local_kv_released": True,
            "remote_kv_released": True,
            "reservations_released": True,
            "capacity_released": True,
            "pending_deliveries": 0,
            "trace_digest": _digest_text("synthetic-recovery-trace"),
        },
    }
    challenge = {
        "kind": "route_challenge_evidence_v1",
        "run_id": TEST_RUN_ID,
        "evidence_class": "physical_qualification",
        "generated_at_unix_ms": NOW_UNIX_MS - 1_000,
        "valid_until_unix_ms": NOW_UNIX_MS + 30_000,
        "max_load_proof_age_ms": 60_000,
        "deployment_id": graph["deployment_id"],
        "deployment_epoch": graph["deployment_epoch"],
        "topology_version": graph["topology_version"],
        "model_id": graph["model_id"],
        "resolved_commit": graph["resolved_commit"],
        "manifest_digest": graph["manifest_digest"],
        "path_manifest": path_manifest,
        "stage_evidence": stage_evidence,
        "transport": transport,
        "token_parity": token_parity,
        "numeric_parity": numeric_parity,
        "execution_trace": execution_trace,
        "kv_ownership": kv_ownership,
        "lifecycle_evidence": lifecycle_evidence,
    }
    negative_runs = {
        "kind": "negative_run_set_v1",
        "run_id": TEST_RUN_ID,
        "runs": [
            {
                "kind": kind,
                "route_ready": False,
                "reason_code": f"synthetic_test_fixture_{kind}_rejected",
                "evidence_digest": _digest_text(f"synthetic-negative-{kind}"),
            }
            for kind in REQUIRED_NEGATIVE_RUNS
        ],
    }

    source_manifest_bytes = canonical_json_bytes(
        {
            "kind": "synthetic_test_fixture_source_manifest",
            "base_branch": "automation/mycelium-overnight",
            "source_commit": "1d458f5474294f11ae3ff12cf333fe28a799931f",
        }
    )
    environment_bytes = canonical_json_bytes(
        {
            "kind": "synthetic_test_fixture_environment",
            "python": "3.14",
            "platform": "test-only",
        }
    )
    contract_manifest_bytes = (ROOT / "contracts/contract-manifest.v1.json").read_bytes()
    cargo_lock_bytes = (ROOT / "native/iroh_transport/Cargo.lock").read_bytes()
    ui_lock_bytes = (ROOT / "ui/web/package-lock.json").read_bytes()
    extra_files = {
        "provenance/source-manifest.json": source_manifest_bytes,
        "provenance/environment.json": environment_bytes,
        "provenance/contract-manifest.v1.json": contract_manifest_bytes,
        "provenance/native-iroh-Cargo.lock": cargo_lock_bytes,
        "provenance/observatory-package-lock.json": ui_lock_bytes,
    }
    source_provenance = {
        "kind": "qualification_source_provenance_v1",
        "source_manifest": _pin("provenance/source-manifest.json", source_manifest_bytes),
        "environment": _pin("provenance/environment.json", environment_bytes),
        "contract_manifest": _pin(
            "provenance/contract-manifest.v1.json", contract_manifest_bytes
        ),
        "dependency_locks": [
            _pin("provenance/native-iroh-Cargo.lock", cargo_lock_bytes),
            _pin("provenance/observatory-package-lock.json", ui_lock_bytes),
        ],
    }

    documents = {
        "qualification/source-provenance.json": source_provenance,
        "model/model-manifest.json": manifest,
        "control/control-plane-tranche.json": tranche,
        "control/gossip-signature.json": gossip_signature,
        "runtime/provisioning-reports.json": reports,
        "runtime/load-proofs.json": proofs,
        "runtime/load-proof-signatures.json": {
            "kind": "signed_load_proof_set_v1",
            "run_id": TEST_RUN_ID,
            "signatures": load_proof_signatures,
        },
        "router/execution-graph.json": graph,
        "run/route-challenge.json": challenge,
        "run/negative-runs.json": negative_runs,
    }
    return QualificationCase(documents=documents, extra_files=extra_files)


@pytest.fixture
def qualification_case() -> QualificationCase:
    return make_case()
