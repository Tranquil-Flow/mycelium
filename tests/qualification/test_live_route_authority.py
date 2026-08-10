from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest

from mycelium_qualification import (
    LiveRouteQualificationError,
    generate_ed25519_signer,
    issue_live_route_qualification,
)
from mycelium_router.serialization import execution_graph_from_dict, execution_graph_to_dict


ROOT = Path(__file__).resolve().parents[2]
PROMPT = (15496, 11, 703, 389, 345, 30)
OUTPUT = (4599, 3329, 2506, 5145)


def _graph():
    spec = importlib.util.spec_from_file_location(
        "live_authority_graph_fixture",
        ROOT / "tests" / "qualification" / "conftest.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    files, _manifest = module.make_case().render()
    return execution_graph_from_dict(json.loads(files["router/execution-graph.json"]))


def _signed(signer, observation):
    return {
        "observation": observation,
        "signature": signer.sign(observation),
        "verification_key": signer.public_key_record(),
    }


def _attestation():
    graph = _graph()
    node_ids = tuple(
        placement.node_id
        for stage in graph.stages
        for placement in stage.placements
    )
    endpoints = {node_id: f"endpoint-{index}" for index, node_id in enumerate(node_ids)}
    signers = {
        node_id: generate_ed25519_signer(endpoint_id=endpoints[node_id])
        for node_id in node_ids
    }
    observations = []
    process_ids = {
        node_id: 1000 + index for index, node_id in enumerate(node_ids)
    }

    def observation(node_id, event, details, sequence):
        return {
            "protocol": "mycelium.physical_node_observation.v1",
            "event": event,
            "monotonic_ns": sequence,
            "run_id": "live-run-1",
            "deployment_id": graph.deployment_id,
            "node_id": node_id,
            "host_id": f"host-{node_id}",
            "process_id": process_ids[node_id],
            "endpoint_id": endpoints[node_id],
            "peer_generation": 1,
            "state": "RUNNING",
            "route_ready": False,
            "details": details,
        }

    placements = {
        placement.node_id: placement
        for stage in graph.stages
        for placement in stage.placements
    }
    sequence = 1
    for node_id in node_ids:
        placement = placements[node_id]
        configured = observation(
            node_id,
            "configured",
            {
                "assignment_id": placement.assignment_id,
                "placement_id": placement.placement_id,
                "manifest_digest": graph.manifest_digest,
                "endpoint_addr": {"id": endpoints[node_id]},
                "runtime_mode": "stage_local_kv",
                "stage_pack_digest": "sha256:" + "a" * 64,
                "stage_pack_verification_digest": "sha256:" + "b" * 64,
            },
            sequence,
        )
        observations.append(_signed(signers[node_id], configured))
        sequence += 1
    for index, node_id in enumerate(node_ids):
        peer_id = node_ids[(index + 1) % len(node_ids)]
        started = observation(
            node_id,
            "started",
            {
                "peer": {
                    "node_id": peer_id,
                    "endpoint_id": endpoints[peer_id],
                    "endpoint_addr": {"id": endpoints[peer_id]},
                    "generation": 1,
                }
            },
            sequence,
        )
        observations.append(_signed(signers[node_id], started))
        sequence += 1
    entry = node_ids[0]
    for event, output, status in (
        ("inference_started", OUTPUT[:1], "DECODING"),
        ("inference_decoded", OUTPUT, "COMPLETED"),
    ):
        inference = observation(
            entry,
            event,
            {
                "request_id": "startup-challenge",
                "status": status,
                "output": {
                    "token_ids": list(output),
                    "token_indexes": list(range(len(output))),
                },
                **({"dispatched": 3} if event == "inference_decoded" else {}),
            },
            sequence,
        )
        observations.append(_signed(signers[entry], inference))
        sequence += 1
    for index, node_id in enumerate(node_ids):
        peer_id = node_ids[(index + 1) % len(node_ids)]
        snapshot = observation(
            node_id,
            "snapshot",
            {
                "runtime": {
                    "active_state_count": 0,
                    "applied_operation_count": 4,
                    "mode": "stage_local_kv",
                },
                "transport": {
                    "local_node_id": node_id,
                    "peer_node_id": peer_id,
                    "local_endpoint_id": endpoints[node_id],
                    "peer_endpoint_id": endpoints[peer_id],
                    "remote_frames_sent": 4,
                    "remote_frames_received": 4,
                    "route_ready": False,
                },
                "transport_fatal_error": None,
            },
            sequence,
        )
        observations.append(_signed(signers[node_id], snapshot))
        sequence += 1
    return {
        "protocol": "mycelium.live_route_attestation.v1",
        "captured_at_unix_ms": 1_000_000,
        "run_id": "live-run-1",
        "entry_node_id": entry,
        "request_id": "startup-challenge",
        "prompt_token_ids": list(PROMPT),
        "max_new_tokens": 4,
        "execution_graph": execution_graph_to_dict(graph),
        "output_token_ids": list(OUTPUT),
        "signed_observations": observations,
        "counters": {
            "frames_sent": 8,
            "frames_received": 8,
            "applied_operation_count": 8,
            "fatal": None,
        },
    }


def test_live_authority_issues_graph_bound_ready_record():
    attestation = _attestation()
    qualification = issue_live_route_qualification(
        attestation,
        expected_prompt_token_ids=PROMPT,
        expected_output_token_ids=OUTPUT,
    )
    graph = execution_graph_from_dict(attestation["execution_graph"])

    assert qualification.route_ready is True
    assert qualification.evidence_class == "physical_qualification"
    assert qualification.deployment_id == graph.deployment_id
    assert qualification.manifest_digest == graph.manifest_digest
    assert len(qualification.stage_bindings) == 2


@pytest.mark.parametrize("mutation", ("tokens", "signature", "fatal", "frames"))
def test_live_authority_rejects_tampered_or_nonphysical_attestation(mutation):
    attestation = _attestation()
    if mutation == "tokens":
        attestation["output_token_ids"][-1] += 1
    elif mutation == "signature":
        attestation["signed_observations"][0]["signature"]["signature"] = "invalid"
    elif mutation == "fatal":
        attestation["counters"]["fatal"] = "transport_failed"
    else:
        attestation["counters"]["frames_sent"] = 0

    with pytest.raises(LiveRouteQualificationError):
        issue_live_route_qualification(
            attestation,
            expected_prompt_token_ids=PROMPT,
            expected_output_token_ids=OUTPUT,
        )
