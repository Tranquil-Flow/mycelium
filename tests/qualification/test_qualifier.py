from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from mycelium_physical_runner.frozen_evidence import (
    FROZEN_ROUTE_AUTHORITY_PROFILE,
    build_frozen_route_authority_documents,
)
from mycelium_qualification import QualificationAuthority
from mycelium_qualification.contracts import route_qualification_to_dict
from mycelium_qualification.evidence import (
    canonical_json_bytes,
    evidence_manifest_digest,
    sha256_bytes,
    sha256_document,
)
from mycelium_qualification.qualifier import QualificationError, qualify_route
from mycelium_qualification.sealer import (
    _read_sealed_evidence,
    qualify_sealed_evidence,
    seal_physical_evidence,
)
from mycelium_qualification.signing import generate_ed25519_signer


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


def _frozen_route_case(
    case: Any,
    *,
    runtime_mode: str = "stage_local_kv",
) -> Any:
    tranche = case.documents["control/control-plane-tranche.json"]
    assignments = tranche["assignments"]
    graph = case.documents["router/execution-graph.json"]
    run_id = case.documents["run/route-challenge.json"]["run_id"]
    expected_tokens = [4599, 3329, 2506, 5145]
    signed_observations: list[dict[str, Any]] = []
    identities: dict[str, dict[str, Any]] = {}
    observation_signers: dict[str, Any] = {}

    def signed(observation: dict[str, Any], endpoint_id: str) -> None:
        signer = observation_signers.setdefault(
            endpoint_id,
            generate_ed25519_signer(endpoint_id=endpoint_id),
        )
        signed_observations.append(
            {
                "observation": observation,
                "signature": signer.sign(observation),
                "verification_key": signer.public_key_record(),
            }
        )

    for index, (assignment, stage) in enumerate(
        zip(assignments, graph["stages"]), start=1
    ):
        node_id = assignment["node_id"]
        host_id = f"physical-host-{index}"
        process_id = 7_000 + index
        endpoint_id = f"physical-endpoint-{index}"
        peer_node_id = assignments[index % len(assignments)]["node_id"]
        identities[node_id] = {
            "run_id": run_id,
            "deployment_id": graph["deployment_id"],
            "node_id": node_id,
            "host_id": host_id,
            "process_id": process_id,
            "endpoint_id": endpoint_id,
        }
        common = {
            "protocol": "mycelium.node_observation.v1",
            "run_id": run_id,
            "deployment_id": graph["deployment_id"],
            "node_id": node_id,
            "host_id": host_id,
            "process_id": process_id,
            "endpoint_id": endpoint_id,
            "route_ready": False,
            "release_ready": False,
            "reason_codes": ["physical_qualification_pending"],
        }
        released_reservations = {
            f"reservation-{position}": {
                "reservation_id": f"reservation-{position}",
                "node_id": reserved_assignment["node_id"],
                "placement_id": graph["stages"][position - 1]["placements"][0][
                    "placement_id"
                ],
                "status": "RELEASED",
            }
            for position, reserved_assignment in enumerate(assignments, start=1)
        }
        for event, details in (
            (
                "configured",
                {
                    "assignment_id": assignment["assignment_id"],
                    "placement_id": stage["placements"][0]["placement_id"],
                    "manifest_digest": graph["manifest_digest"],
                },
            ),
            ("started", {}),
            (
                "snapshot",
                {
                    "transport": {
                        "local_node_id": node_id,
                        "peer_node_id": peer_node_id,
                        "remote_frames_sent": 8,
                        "remote_frames_received": 8,
                        "route_ready": False,
                    },
                    "transport_fatal_error": None,
                    "runtime": {
                        "mode": runtime_mode,
                        "active_state_count": 0,
                        "release_counts": {"normal_completion": 1},
                    },
                    "capacity": {
                        "node_reserved_kv_bytes": {
                            reserved_assignment["node_id"]: 0
                            for reserved_assignment in assignments
                        },
                        "reservations": released_reservations,
                    },
                },
            ),
            ("stopping", {}),
        ):
            signed(dict(common, event=event, details=details), endpoint_id)
        if index == 1:
            signed(
                dict(common, event="inference_started", details={"status": "RUNNING"}),
                endpoint_id,
            )
            signed(
                dict(
                    common,
                    event="inference_decoded",
                    details={
                        "status": "COMPLETED",
                        "output": {"token_ids": list(expected_tokens)},
                    },
                ),
                endpoint_id,
            )

    offers = []
    for assignment in assignments:
        statement = {
            "deployment_id": graph["deployment_id"],
            "recipient_node_id": assignment["node_id"],
            "assignment_id": assignment["assignment_id"],
            "assignment_digest": sha256_document(assignment),
            "graph_digest": sha256_document(graph),
            "expires_at_unix_ms": case.now_unix_ms + 30_000,
        }
        offers.append(
            {
                "message": statement,
                "signature": {
                    "algorithm": "ed25519",
                    "signed_statement_digest": sha256_bytes(
                        canonical_json_bytes(statement)
                    ),
                    "signature": "synthetic-test-signature-never-production",
                },
                "verification_key": {"endpoint_id": "physical-seed"},
            }
        )

    source_documents = {
        "control/model-manifest.json": case.documents["model/model-manifest.json"],
        "control/execution-graph.json": graph,
    }
    for assignment, report, proof in zip(
        assignments,
        case.documents["runtime/provisioning-reports.json"],
        case.documents["runtime/load-proofs.json"],
    ):
        node_id = assignment["node_id"]
        source_documents[f"control/{node_id}-assignment.json"] = assignment
        source_documents[f"control/{node_id}-artifact-report.json"] = report
        source_documents[f"control/{node_id}-load-proof.json"] = proof
    case.documents["qualification/source-provenance.json"] = {
        "kind": "physical_frozen_route_source_provenance_v1",
        "archive_digest": sha256_document({"archive": "physical"}),
        "archive_size_bytes": 1,
        "transfer_manifest": {
            "protocol": "mycelium.controller_transfer_manifest.v1",
            "files": [
                {
                    "path": path,
                    "size_bytes": len(canonical_json_bytes(document)),
                    "content_digest": sha256_document(document),
                }
                for path, document in sorted(source_documents.items())
            ],
        },
    }
    case.documents["control/control-plane-tranche.json"] = {
        "kind": "physical_frozen_route_control_v1",
        "entry_node_id": assignments[0]["node_id"],
        "assignments": assignments,
        "run_plan_digest": sha256_document({"run_id": run_id}),
    }
    case.documents["control/gossip-signature.json"] = {
        "kind": "physical_frozen_route_membership_v1",
        "snapshot": {
            "protocol": "mycelium.controller_membership_snapshot.v1",
            "deployment_id": graph["deployment_id"],
            "assignment_offers": offers,
        },
    }
    case.documents["runtime/load-proof-signatures.json"] = {
        "kind": "physical_frozen_route_signed_observations_v1",
        "run_id": run_id,
        "observations": signed_observations,
    }
    case.documents["run/route-challenge.json"] = {
        "kind": "physical_frozen_route_challenge_v1",
        "run_id": run_id,
        "evidence_class": "physical_qualification",
        "qualification_scope": "inference",
        "generated_at_unix_ms": case.now_unix_ms - 1_000,
        "valid_until_unix_ms": case.now_unix_ms + 30_000,
        "deployment_id": graph["deployment_id"],
        "deployment_epoch": graph["deployment_epoch"],
        "topology_version": graph["topology_version"],
        "placement_provenance": "frozen_fixture",
        "model_id": graph["model_id"],
        "resolved_commit": graph["resolved_commit"],
        "manifest_digest": graph["manifest_digest"],
        "entry_node_id": assignments[0]["node_id"],
        "request": {"prompt_token_ids": [1, 2, 3]},
        "expected_token_ids": expected_tokens,
        "output_token_ids": list(expected_tokens),
        "identities": identities,
        "signed_observations": signed_observations,
        "cleanup": [
            {"node_id": assignment["node_id"], "removed": True}
            for assignment in assignments
        ],
    }
    case.documents["run/negative-runs.json"] = {
        "kind": "physical_frozen_route_scope_v1",
        "run_id": run_id,
        "qualified_operations": ["inference"],
        "unqualified_operations": ["cancellation", "recovery"],
        "claim_boundary": "ordinary frozen-placement inference only; cancellation and recovery are not qualified",
    }
    return case


def _make_frozen_cancellation_case(case: Any) -> Any:
    case = _frozen_route_case(case)
    challenge = case.documents["run/route-challenge.json"]
    signed_observations = case.documents["runtime/load-proof-signatures.json"][
        "observations"
    ]
    entry_node_id = challenge["entry_node_id"]
    entry_started = next(
        envelope
        for envelope in signed_observations
        if envelope["observation"]["node_id"] == entry_node_id
        and envelope["observation"]["event"] == "inference_started"
    )
    entry_template = dict(entry_started["observation"])
    cancellation_details = {
        "request_id": "frozen-cancel-request",
        "result": {
            "cancelled": True,
            "path_id": "frozen-cancel-path",
            "path_attempt": 0,
            "status_before": "DECODING",
            "status_after": "CANCELLED",
            "post_cancel_token_count": 0,
        },
    }
    signer = generate_ed25519_signer(endpoint_id=entry_template["endpoint_id"])
    cancelled_observation = dict(
        entry_template,
        event="cancelled",
        details=cancellation_details,
    )
    signed_observations[:] = [
        envelope
        for envelope in signed_observations
        if envelope["observation"].get("event") != "inference_decoded"
    ]
    signed_observations.append(
        {
            "observation": cancelled_observation,
            "signature": signer.sign(cancelled_observation),
            "verification_key": signer.public_key_record(),
        }
    )
    for envelope in signed_observations:
        observation = envelope["observation"]
        if observation.get("event") != "snapshot":
            continue
        runtime = observation["details"]["runtime"]
        runtime["release_counts"] = {"cancellation": 1}
        observation["details"]["transport_cancellation_cleanup_complete"] = True
        observation["details"]["transport_pending_delivery_count"] = 0
        signer = generate_ed25519_signer(endpoint_id=observation["endpoint_id"])
        envelope["signature"] = signer.sign(observation)
        envelope["verification_key"] = signer.public_key_record()
    challenge.update(
        {
            "qualification_scope": "cancellation",
            "request": {
                "request_id": "frozen-cancel-request",
                "prompt_token_ids": [1, 2, 3],
            },
            "expected_token_ids": [],
            "output_token_ids": [],
            "signed_observations": signed_observations,
            "cancellation": {
                "request_id": "frozen-cancel-request",
                "path_id": "frozen-cancel-path",
                "path_attempt": 0,
                "entry_terminal_state": "CANCELLED",
                "remote_terminal_state": "CANCELLED",
                "post_cancel_token_count": 0,
                "transport_cancellation_observed": True,
                "cleanup_complete": True,
            },
        }
    )
    case.documents["run/negative-runs.json"] = {
        "kind": "physical_frozen_route_scope_v1",
        "run_id": challenge["run_id"],
        "qualified_operations": ["cancellation"],
        "unqualified_operations": ["inference", "recovery"],
        "claim_boundary": "cross-host cancellation only; inference is not qualified by this record; recovery is not qualified",
    }
    return case


def test_frozen_physical_inference_profile_qualifies_without_post_mvp_claims(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(qualification_case)

    record = _qualify(case)
    document = route_qualification_to_dict(record)

    assert document["route_ready"] is True
    assert document["reason_codes"] == []
    assert document["evidence_class"] == "physical_qualification"
    assert document["placement_provenance"] == "frozen_fixture"
    assert "ordinary frozen-placement inference only" in document["claim_boundary"]
    assert "cancellation and recovery are not qualified" in document["claim_boundary"]
    assert len(document["stage_bindings"]) == 2


def test_complete_context_replay_physical_profile_remains_qualifiable(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(
        qualification_case,
        runtime_mode="complete_context_replay",
    )

    record = _qualify(case)

    assert record.route_ready is True
    assert record.reason_codes == ()


def test_frozen_physical_cancellation_profile_qualifies_cross_host_cancel(
    qualification_case: Any,
) -> None:
    case = _make_frozen_cancellation_case(qualification_case)

    record = _qualify(case)
    document = route_qualification_to_dict(record)

    assert document["route_ready"] is True
    assert document["reason_codes"] == []
    assert document["evidence_class"] == "physical_qualification"
    assert "cross-host cancellation only" in document["claim_boundary"]
    assert "recovery is not qualified" in document["claim_boundary"]


def _authority_builder_inputs(
    case: Any,
    tmp_path: Path,
    *,
    command: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    challenge = case.documents["run/route-challenge.json"]
    control = case.documents["control/control-plane-tranche.json"]
    source_root = tmp_path / f"source-{command}"
    source_root.mkdir()
    source_index = {
        entry["path"]: entry
        for entry in case.documents["qualification/source-provenance.json"][
            "transfer_manifest"
        ]["files"]
    }
    production_graph = json.loads(
        canonical_json_bytes(case.documents["router/execution-graph.json"])
    )
    for stage in production_graph["stages"]:
        stage["layer_range"] = stage.pop("range")
    production_graph_bytes = canonical_json_bytes(production_graph)
    source_index["control/execution-graph.json"]["size_bytes"] = len(
        production_graph_bytes
    )
    source_index["control/execution-graph.json"]["content_digest"] = sha256_bytes(
        production_graph_bytes
    )
    source_documents = {
        "control/model-manifest.json": case.documents["model/model-manifest.json"],
        "control/execution-graph.json": production_graph,
    }
    reports = {
        report["assignment_id"]: report
        for report in case.documents["runtime/provisioning-reports.json"]
    }
    proofs = {
        proof["assignment_id"]: proof
        for proof in case.documents["runtime/load-proofs.json"]
    }
    for assignment in control["assignments"]:
        node_id = assignment["node_id"]
        assignment_id = assignment["assignment_id"]
        source_documents[f"control/{node_id}-assignment.json"] = assignment
        source_documents[f"control/{node_id}-artifact-report.json"] = reports[
            assignment_id
        ]
        source_documents[f"control/{node_id}-load-proof.json"] = proofs[assignment_id]
    for path, document in source_documents.items():
        destination = source_root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical_json_bytes(document))
        assert source_index[path]["size_bytes"] == destination.stat().st_size

    production_graph_digest = sha256_document(production_graph)
    membership_snapshot = case.documents["control/gossip-signature.json"]["snapshot"]
    for offer in membership_snapshot["assignment_offers"]:
        offer["message"]["graph_digest"] = production_graph_digest
        offer["signature"]["signed_statement_digest"] = sha256_bytes(
            canonical_json_bytes(offer["message"])
        )

    peers = [
        {
            "node_id": node_id,
            "host_id": identity["host_id"],
        }
        for node_id, identity in challenge["identities"].items()
    ]
    controller_config = {
        "authority_profile": FROZEN_ROUTE_AUTHORITY_PROFILE,
        "now": case.now_unix_ms / 1000,
        "source_root": str(source_root),
        "peers": peers,
        "transfer_manifest": case.documents["qualification/source-provenance.json"][
            "transfer_manifest"
        ],
        "membership_snapshot": case.documents["control/gossip-signature.json"][
            "snapshot"
        ],
        "run_plan": {
            "run_id": challenge["run_id"],
            "deployment_id": challenge["deployment_id"],
            "entry_node_id": challenge["entry_node_id"],
            "request": challenge["request"],
            "expected_token_ids": challenge["expected_token_ids"],
        },
    }
    cancellation = command == "cancel"
    evidence = {
        "protocol": "mycelium.physical_controller_result.v1",
        "command": command,
        "mode": "physical",
        "run_id": challenge["run_id"],
        "physical_execution": True,
        "route_ready": False,
        "release_ready": False,
        "cancelled": cancellation,
        "token_parity": not cancellation,
        "expected_token_ids": challenge["expected_token_ids"],
        "output_token_ids": [] if cancellation else challenge["output_token_ids"],
        "identities": challenge["identities"],
        "observations": {node_id: {} for node_id in challenge["identities"]},
        "signed_observations": challenge["signed_observations"],
        "cleanup": challenge["cleanup"],
        "recovered_nodes": [],
        "restart_attempts": {},
    }
    return controller_config, evidence


def test_frozen_authority_document_builder_binds_dynamic_controller_result(
    qualification_case: Any,
    tmp_path: Path,
) -> None:
    case = _frozen_route_case(qualification_case)
    challenge = case.documents["run/route-challenge.json"]
    control = case.documents["control/control-plane-tranche.json"]
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_index = {
        entry["path"]: entry
        for entry in case.documents["qualification/source-provenance.json"][
            "transfer_manifest"
        ]["files"]
    }
    production_graph = json.loads(
        canonical_json_bytes(case.documents["router/execution-graph.json"])
    )
    for stage in production_graph["stages"]:
        stage["layer_range"] = stage.pop("range")
    production_graph_bytes = canonical_json_bytes(production_graph)
    source_index["control/execution-graph.json"]["size_bytes"] = len(
        production_graph_bytes
    )
    source_index["control/execution-graph.json"]["content_digest"] = sha256_bytes(
        production_graph_bytes
    )
    source_documents = {
        "control/model-manifest.json": case.documents["model/model-manifest.json"],
        "control/execution-graph.json": production_graph,
    }
    reports = {
        report["assignment_id"]: report
        for report in case.documents["runtime/provisioning-reports.json"]
    }
    proofs = {
        proof["assignment_id"]: proof
        for proof in case.documents["runtime/load-proofs.json"]
    }
    for assignment in control["assignments"]:
        node_id = assignment["node_id"]
        assignment_id = assignment["assignment_id"]
        source_documents[f"control/{node_id}-assignment.json"] = assignment
        source_documents[f"control/{node_id}-artifact-report.json"] = reports[
            assignment_id
        ]
        source_documents[f"control/{node_id}-load-proof.json"] = proofs[assignment_id]
    for path, document in source_documents.items():
        destination = source_root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(canonical_json_bytes(document))
        assert source_index[path]["size_bytes"] == destination.stat().st_size

    production_graph_digest = sha256_document(production_graph)
    membership_snapshot = case.documents["control/gossip-signature.json"]["snapshot"]
    for offer in membership_snapshot["assignment_offers"]:
        offer["message"]["graph_digest"] = production_graph_digest
        offer["signature"]["signed_statement_digest"] = sha256_bytes(
            canonical_json_bytes(offer["message"])
        )

    peers = [
        {
            "node_id": node_id,
            "host_id": identity["host_id"],
        }
        for node_id, identity in challenge["identities"].items()
    ]
    controller_config = {
        "authority_profile": FROZEN_ROUTE_AUTHORITY_PROFILE,
        "now": case.now_unix_ms / 1000,
        "source_root": str(source_root),
        "peers": peers,
        "transfer_manifest": case.documents["qualification/source-provenance.json"][
            "transfer_manifest"
        ],
        "membership_snapshot": case.documents["control/gossip-signature.json"][
            "snapshot"
        ],
        "run_plan": {
            "run_id": challenge["run_id"],
            "deployment_id": challenge["deployment_id"],
            "entry_node_id": challenge["entry_node_id"],
            "request": challenge["request"],
            "expected_token_ids": challenge["expected_token_ids"],
        },
    }
    evidence = {
        "protocol": "mycelium.physical_controller_result.v1",
        "command": "run",
        "mode": "physical",
        "run_id": challenge["run_id"],
        "physical_execution": True,
        "route_ready": False,
        "release_ready": False,
        "cancelled": False,
        "token_parity": True,
        "expected_token_ids": challenge["expected_token_ids"],
        "output_token_ids": challenge["output_token_ids"],
        "identities": challenge["identities"],
        "observations": {node_id: {} for node_id in challenge["identities"]},
        "signed_observations": challenge["signed_observations"],
        "cleanup": challenge["cleanup"],
        "recovered_nodes": [],
        "restart_attempts": {},
    }

    documents = build_frozen_route_authority_documents(
        controller_config=controller_config,
        evidence=evidence,
    )

    assert documents["run/route-challenge.json"]["output_token_ids"] == [
        4599,
        3329,
        2506,
        5145,
    ]
    assert documents["run/negative-runs.json"]["qualified_operations"] == ["inference"]
    assert documents["run/negative-runs.json"]["unqualified_operations"] == [
        "cancellation",
        "recovery",
    ]
    sealed = seal_physical_evidence(
        output_dir=tmp_path / "sealed",
        run_id=challenge["run_id"],
        documents=documents,
    )

    record = qualify_sealed_evidence(
        sealed,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=_verify_synthetic_signature,
        verify_load_proof_signature=_verify_synthetic_signature,
    )

    assert record.route_ready is True
    assert record.evidence_manifest_digest == sealed.manifest_digest
    files, manifest = _read_sealed_evidence(sealed)
    authority_record = QualificationAuthority(
        clock_unix_ms=lambda: case.now_unix_ms
    ).qualify_and_publish(
        evidence_files=files,
        evidence_manifest=manifest,
        verify_gossip_signature=_verify_synthetic_signature,
        verify_load_proof_signature=_verify_synthetic_signature,
    )
    assert authority_record.route_ready is True
    assert authority_record.evidence_manifest_digest == sealed.manifest_digest


def test_frozen_authority_document_builder_uses_configured_control_files(
    qualification_case: Any,
    tmp_path: Path,
) -> None:
    case = _frozen_route_case(qualification_case)
    controller_config, evidence = _authority_builder_inputs(
        case, tmp_path, command="run"
    )
    source_root = Path(controller_config["source_root"])
    nodes = []
    for index, peer in enumerate(controller_config["peers"]):
        node_id = peer["node_id"]
        source_prefix = source_root / "control" / node_id
        configured_prefix = source_root / "control" / f"fixture-{index}"
        for suffix in ("assignment", "artifact-report", "load-proof"):
            configured_prefix.with_name(f"fixture-{index}-{suffix}.json").write_bytes(
                source_prefix.with_name(f"{node_id}-{suffix}.json").read_bytes()
            )
        nodes.append(
            {
                "node_id": node_id,
                "configure": {
                    "assignment_file": f"control/fixture-{index}-assignment.json"
                },
            }
        )
    controller_config["run_plan"]["nodes"] = nodes

    documents = build_frozen_route_authority_documents(
        controller_config=controller_config,
        evidence=evidence,
    )

    assert [
        item["node_id"]
        for item in documents["control/control-plane-tranche.json"]["assignments"]
    ] == [peer["node_id"] for peer in controller_config["peers"]]


def test_frozen_authority_document_builder_binds_dynamic_cancellation_result(
    qualification_case: Any,
    tmp_path: Path,
) -> None:
    case = _make_frozen_cancellation_case(qualification_case)
    controller_config, evidence = _authority_builder_inputs(
        case,
        tmp_path,
        command="cancel",
    )

    documents = build_frozen_route_authority_documents(
        controller_config=controller_config,
        evidence=evidence,
    )

    challenge = documents["run/route-challenge.json"]
    assert challenge["qualification_scope"] == "cancellation"
    assert challenge["expected_token_ids"] == []
    assert challenge["output_token_ids"] == []
    assert challenge["cancellation"]["entry_terminal_state"] == "CANCELLED"
    assert challenge["cancellation"]["post_cancel_token_count"] == 0
    assert documents["run/negative-runs.json"]["qualified_operations"] == [
        "cancellation"
    ]
    assert documents["run/negative-runs.json"]["unqualified_operations"] == [
        "inference",
        "recovery",
    ]

    sealed = seal_physical_evidence(
        output_dir=tmp_path / "sealed-cancel",
        run_id=challenge["run_id"],
        documents=documents,
    )
    record = qualify_sealed_evidence(
        sealed,
        now_unix_ms=case.now_unix_ms,
        verify_gossip_signature=_verify_synthetic_signature,
        verify_load_proof_signature=_verify_synthetic_signature,
    )
    document = route_qualification_to_dict(record)
    assert document["route_ready"] is True
    assert "cross-host cancellation only" in document["claim_boundary"]
    assert "recovery is not qualified" in document["claim_boundary"]


def test_frozen_physical_inference_profile_rejects_unbound_stage_assignment(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(qualification_case)
    signed = case.documents["runtime/load-proof-signatures.json"]["observations"][0]
    signed["observation"]["details"]["assignment_id"] = "wrong-assignment"
    signer = generate_ed25519_signer(endpoint_id=signed["observation"]["endpoint_id"])
    signed["signature"] = signer.sign(signed["observation"])
    signed["verification_key"] = signer.public_key_record()

    _assert_rejected(case, "signed_observation_binding_invalid")


def test_frozen_physical_inference_profile_rejects_same_host_processes(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(qualification_case)
    identities = case.documents["run/route-challenge.json"]["identities"]
    node_ids = list(identities)
    identities[node_ids[1]]["host_id"] = identities[node_ids[0]]["host_id"]

    _assert_rejected(case, "physical_identity_invalid")


def test_frozen_physical_inference_profile_rejects_token_mismatch(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(qualification_case)
    case.documents["run/route-challenge.json"]["output_token_ids"][-1] += 1

    _assert_rejected(case, "token_parity_invalid")


def test_frozen_physical_inference_profile_rejects_post_mvp_scope_claim(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(qualification_case)
    case.documents["run/negative-runs.json"]["qualified_operations"].append("recovery")

    _assert_rejected(case, "qualification_scope_invalid")


def test_frozen_physical_inference_profile_rejects_source_pin_mismatch(
    qualification_case: Any,
) -> None:
    case = _frozen_route_case(qualification_case)
    case.documents["qualification/source-provenance.json"]["transfer_manifest"][
        "files"
    ][0]["content_digest"] = "sha256:" + "0" * 64

    _assert_rejected(case, "source_provenance_digest_mismatch")


def test_hypothetical_physical_shape_qualifies_in_memory_only(
    qualification_case: Any,
) -> None:
    qualification_case.documents["run/route-challenge.json"]["placement_provenance"] = (
        "frozen_fixture"
    )
    files, manifest = qualification_case.render()
    record = _qualify(qualification_case)
    document = route_qualification_to_dict(record)
    challenge = qualification_case.documents["run/route-challenge.json"]

    assert document["route_ready"] is True
    assert document["reason_codes"] == []
    assert document["evidence_class"] == "physical_qualification"
    assert document["placement_provenance"] == "frozen_fixture"
    assert (
        document["qualified_by"]
        == "mycelium_qualification.qualifier:RouteQualificationV1"
    )
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
    assert document["lifecycle_evidence_digest"] == sha256_document(
        challenge["lifecycle_evidence"]
    )
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
    assert all(
        set(binding) == EXPECTED_STAGE_FIELDS for binding in document["stage_bindings"]
    )
    assert all(
        binding["stage_probe_result_digest"].startswith("sha256:")
        for binding in document["stage_bindings"]
    )
    assert set(files) == {entry["path"] for entry in manifest["files"]}


def test_qualification_rejects_missing_placement_provenance(
    qualification_case: Any,
) -> None:
    qualification_case.documents["run/route-challenge.json"].pop("placement_provenance")
    _assert_rejected(qualification_case, "placement_provenance_missing")


@pytest.mark.parametrize(
    "value",
    ["", "heuristic", None, ["frozen_fixture"]],
)
def test_qualification_rejects_invalid_placement_provenance(
    qualification_case: Any,
    value: object,
) -> None:
    qualification_case.documents["run/route-challenge.json"]["placement_provenance"] = (
        value
    )
    _assert_rejected(qualification_case, "placement_provenance_invalid")


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


def test_evidence_manifest_binding_rejects_mutated_or_missing_files(
    qualification_case: Any,
) -> None:
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


def test_post_seal_placement_provenance_mutation_breaks_manifest_binding(
    qualification_case: Any,
) -> None:
    files, manifest = qualification_case.render()
    challenge = qualification_case.documents["run/route-challenge.json"]
    challenge["placement_provenance"] = "frozen_fixturz"
    files["run/route-challenge.json"] = canonical_json_bytes(challenge)

    with pytest.raises(QualificationError) as captured:
        qualify_route(
            evidence_files=files,
            evidence_manifest=manifest,
            now_unix_ms=qualification_case.now_unix_ms,
            verify_gossip_signature=_verify_synthetic_signature,
            verify_load_proof_signature=_verify_synthetic_signature,
        )

    assert captured.value.code == "evidence_file_digest_mismatch"


def test_missing_required_evidence_document_is_rejected(
    qualification_case: Any,
) -> None:
    qualification_case.documents.pop("runtime/load-proofs.json")
    _assert_rejected(qualification_case, "missing_evidence_file")


def test_source_manifest_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/source-manifest.json"] += b"mutation"
    _assert_rejected(qualification_case, "source_manifest_digest_mismatch")


def test_contract_manifest_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/contract-manifest.v1.json"] += (
        b"mutation"
    )
    _assert_rejected(qualification_case, "contract_manifest_digest_mismatch")


def test_environment_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/environment.json"] += b"mutation"
    _assert_rejected(qualification_case, "environment_digest_mismatch")


def test_every_dependency_lock_digest_is_bound(qualification_case: Any) -> None:
    qualification_case.extra_files["provenance/native-iroh-Cargo.lock"] += b"mutation"
    _assert_rejected(qualification_case, "dependency_lock_digest_mismatch")


def test_signed_load_proof_set_is_complete_bound_and_verified(
    qualification_case: Any,
) -> None:
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


def test_signed_gossip_snapshot_identity_and_peer_liveness_are_bound(
    qualification_case: Any,
) -> None:
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
    qualification_case.documents["control/control-plane-tranche.json"]["assignments"][
        0
    ]["deployment_epoch"] += 1
    _assert_rejected(qualification_case, "control_plane_chain_invalid")


def test_provisioning_reports_are_exactly_assignment_bound(
    qualification_case: Any,
) -> None:
    qualification_case.documents["runtime/provisioning-reports.json"][0][
        "route_ready"
    ] = True
    _assert_rejected(qualification_case, "provisioning_report_invalid")


def test_load_proof_chain_remains_unqualified_and_exact(
    qualification_case: Any,
) -> None:
    qualification_case.documents["runtime/load-proofs.json"][0]["route_ready"] = True
    _assert_rejected(qualification_case, "load_proof_chain_invalid")


def test_execution_graph_must_equal_the_bound_builder_output(
    qualification_case: Any,
) -> None:
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


def test_stage_signature_and_load_proof_digest_are_exact(
    qualification_case: Any,
) -> None:
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
    challenge["path_manifest"]["ordered_hops"][0]["reservation_expires_at_unix_ms"] = (
        case.now_unix_ms
    )
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


def test_numeric_parity_is_stage_bound_and_within_tolerance(
    qualification_case: Any,
) -> None:
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
    trace["final_logits"]["max_abs_diff"] = (
        trace["final_logits"]["absolute_tolerance"] * 2
    )
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


def test_cancellation_lifecycle_requires_distributed_observation_and_cleanup(
    qualification_case: Any,
) -> None:
    pristine = qualification_case.clone()
    cancellation = qualification_case.documents["run/route-challenge.json"][
        "lifecycle_evidence"
    ]["cancellation"]
    cancellation["transport_cancellation_observed"] = False
    _assert_rejected(qualification_case, "cancellation_not_observed")

    case = pristine.clone()
    cancellation = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "cancellation"
    ]
    cancellation["post_cancel_token_count"] = 1
    _assert_rejected(case, "post_cancel_token_emitted")

    case = pristine.clone()
    cancellation = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "cancellation"
    ]
    cancellation["remote_kv_released"] = False
    _assert_rejected(case, "cancellation_cleanup_incomplete")

    case = pristine.clone()
    cancellation = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "cancellation"
    ]
    cancellation["capacity_released"] = False
    _assert_rejected(case, "cancellation_cleanup_incomplete")

    case = pristine
    cancellation = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "cancellation"
    ]
    cancellation["pending_deliveries"] = 1
    _assert_rejected(case, "cancellation_cleanup_incomplete")


def test_recovery_lifecycle_requires_real_identity_and_generation_rotation(
    qualification_case: Any,
) -> None:
    pristine = qualification_case.clone()
    recovery = qualification_case.documents["run/route-challenge.json"][
        "lifecycle_evidence"
    ]["recovery"]
    recovery["old_process_id"] = recovery["new_process_id"]
    _assert_rejected(qualification_case, "recovery_identity_not_rotated")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["old_endpoint_id"] = recovery["new_endpoint_id"]
    _assert_rejected(case, "recovery_identity_not_rotated")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["new_peer_generation"] = recovery["old_peer_generation"]
    _assert_rejected(case, "recovery_generation_not_rotated")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["new_topology_version"] = recovery["old_topology_version"]
    _assert_rejected(case, "recovery_topology_invalid")

    case = pristine
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["new_path_attempt"] = recovery["old_path_attempt"]
    _assert_rejected(case, "recovery_path_attempt_invalid")


def test_recovery_lifecycle_requires_replay_continuity_stale_rejection_and_cleanup(
    qualification_case: Any,
) -> None:
    pristine = qualification_case.clone()
    recovery = qualification_case.documents["run/route-challenge.json"][
        "lifecycle_evidence"
    ]["recovery"]
    recovery["recovery_phase"] = "PREFILL"
    _assert_rejected(qualification_case, "recovery_prefill_missing")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["stale_generation_rejected"] = False
    _assert_rejected(case, "stale_generation_accepted")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["stale_frame_rejected"] = False
    _assert_rejected(case, "stale_generation_accepted")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["remote_disconnect_observed"] = False
    _assert_rejected(case, "recovery_not_observed")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["peer_drop_observed"] = False
    _assert_rejected(case, "recovery_not_observed")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["generated_token_ids_after_recovery"].pop()
    _assert_rejected(case, "recovery_token_continuity_invalid")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["event_sequences"][3] = recovery["event_sequences"][2]
    _assert_rejected(case, "sequence_replay")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["event_sequences"].pop()
    _assert_rejected(case, "sequence_replay")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["full_model_fallback"] = True
    _assert_rejected(case, "full_model_fallback")

    case = pristine.clone()
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["capacity_released"] = False
    _assert_rejected(case, "recovery_cleanup_incomplete")

    case = pristine
    recovery = case.documents["run/route-challenge.json"]["lifecycle_evidence"][
        "recovery"
    ]
    recovery["pending_deliveries"] = 1
    _assert_rejected(case, "recovery_cleanup_incomplete")


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
        (
            lambda case: case.documents["run/route-challenge.json"][
                "lifecycle_evidence"
            ].__setitem__("unknown_field", True),
            "lifecycle_evidence_invalid",
        ),
        (
            lambda case: case.documents["run/route-challenge.json"][
                "lifecycle_evidence"
            ]["recovery"].__setitem__("unknown_field", True),
            "recovery_evidence_invalid",
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
