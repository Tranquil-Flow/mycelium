from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from mycelium_live.route import _refresh_membership_snapshot
from mycelium_node import load_or_create_node_signer
from mycelium_membership.contracts import (
    ASSIGNMENT_OFFER_PROTOCOL,
    verify_membership_message,
)
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity
from scripts import build_qwen_live_route as builder


def _template() -> dict:
    peers = [
        {
            "node_id": "node-0",
            "ssh_target": "operator@local",
            "host_id": "host-0",
            "boot_id": "boot-0",
            "staging_root": "/opt/mycelium/node-0",
            "process_transport": "local",
            "ssh_identity_file": None,
        },
        {
            "node_id": "node-1",
            "ssh_target": "operator@remote",
            "host_id": "host-1",
            "boot_id": "boot-1",
            "staging_root": "/opt/mycelium/node-1",
            "process_transport": "ssh",
            "ssh_identity_file": "/keys/remote",
        },
    ]
    runtime_nodes = [
        {
            "node_id": f"node-{index}",
            "python_executable": "/usr/bin/python3",
            "sidecar_binary": f"/opt/mycelium/node-{index}/sidecar",
            "endpoint_secret_file": f"/opt/mycelium/identities/node-{index}.key",
        }
        for index in range(2)
    ]
    endpoint_records = [
        {"node_id": "node-0", "endpoint_id": "endpoint-0"},
        {"node_id": "node-1", "endpoint_id": "endpoint-1"},
    ]
    return {
        "controller": {
            "peers": peers,
            "run_plan": {"nodes": runtime_nodes},
            "membership_snapshot": {
                "assignment_offers": [
                    {"message": {"peer_endpoint_records": endpoint_records}}
                ]
            },
        }
    }


def _topology_node(index: int, *, backend: str) -> dict:
    return {
        "node_id": f"node-{index}",
        "ssh_target": "operator@local" if index == 0 else f"operator@remote-{index}",
        "staging_root": f"/opt/mycelium/node-{index}",
        "process_transport": "local" if index == 0 else "ssh",
        "ssh_identity_file": None if index == 0 else f"/keys/remote-{index}",
        "python_executable": "/usr/bin/python3",
        "sidecar_binary": f"/opt/mycelium/node-{index}/sidecar",
        "endpoint_secret_file": f"/opt/mycelium/identities/node-{index}.key",
        "endpoint_id": f"endpoint-{index}",
        "runtime_backend": backend,
    }


def _write_topology(
    path: Path,
    nodes: list[dict],
    *,
    placement_order_authority: str | None = None,
) -> Path:
    document = {"protocol": builder._TOPOLOGY_PROTOCOL, "nodes": nodes}
    if placement_order_authority is not None:
        document["placement_order_authority"] = placement_order_authority
    path.write_text(
        json.dumps(document),
        encoding="utf-8",
    )
    return path


def test_legacy_template_resolves_as_two_host_topology() -> None:
    nodes = builder._topology_nodes(_template(), None)

    assert [node["node_id"] for node in nodes] == ["node-0", "node-1"]
    assert [node["runtime_backend"] for node in nodes] == ["mlx", "numpy"]
    assert [node["endpoint_id"] for node in nodes] == ["endpoint-0", "endpoint-1"]


def test_explicit_topology_accepts_three_ordered_physical_hosts(tmp_path: Path) -> None:
    topology = _write_topology(
        tmp_path / "topology.json",
        [
            _topology_node(0, backend="mlx"),
            _topology_node(1, backend="mlx"),
            _topology_node(2, backend="numpy"),
        ],
    )

    nodes = builder._topology_nodes(_template(), topology)

    assert len(nodes) == 3
    assert len({node["ssh_target"] for node in nodes}) == 3
    assert [node["runtime_backend"] for node in nodes] == [
        "mlx",
        "mlx",
        "numpy",
    ]


def test_m14_measured_cycle_can_define_noncanonical_physical_order(
    tmp_path: Path,
) -> None:
    topology = _write_topology(
        tmp_path / "m14-topology.json",
        [
            _topology_node(0, backend="mlx"),
            _topology_node(2, backend="numpy"),
            _topology_node(1, backend="mlx"),
        ],
        placement_order_authority="m14_measured_cycle",
    )

    nodes = builder._topology_nodes(_template(), topology)

    assert [node["node_id"] for node in nodes] == ["node-0", "node-2", "node-1"]


def test_m14_topology_exclusions_do_not_enter_m13_node_exclusions() -> None:
    assert (
        builder._placement_exclusions(
            {
                "protocol": "mycelium.m14_physical_candidate.v1",
                "exclusions": ["path_transition_not_observed_within_budget"],
            }
        )
        == []
    )


def test_model_preparation_authorization_owns_exact_stage_plan(tmp_path: Path) -> None:
    model_id = "Qwen/Qwen3-8B"
    revision = "b" * 40
    topology = [
        _topology_node(0, backend="mlx"),
        _topology_node(1, backend="numpy"),
    ]
    document = {
        "protocol": "mycelium.model_preparation_authorization.v1",
        "model_id": model_id,
        "revision": revision,
        "catalog_generation": 3,
        "operation_digest": "sha256:" + "a" * 64,
        "feasibility_digest": "sha256:" + "c" * 64,
        "representation_digest": "sha256:" + "d" * 64,
        "serving_quantization": "int8-weight-only",
        "evidence_generation": 2,
        "evidence_valid_until_unix_ms": 9_999_999_999_999,
        "stages": [
            {
                "stage_index": 0,
                "node_id": "node-0",
                "start_layer": 0,
                "end_layer_exclusive": 30,
                "backend": "mlx",
                "decode_mode": "complete_context_replay",
                "assignment_files": ["model-1.safetensors"],
                "assignment_artifact_bytes": 100,
            },
            {
                "stage_index": 1,
                "node_id": "node-1",
                "start_layer": 30,
                "end_layer_exclusive": 36,
                "backend": "numpy",
                "decode_mode": "complete_context_replay",
                "assignment_files": ["model-2.safetensors"],
                "assignment_artifact_bytes": 40,
            },
        ],
        "download_authorized": False,
    }
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    actual, ranges = builder._preparation_authorization(
        path,
        model_id=model_id,
        resolved_commit=revision,
        topology=topology,
    )

    assert actual == document
    assert [(item.start, item.stop) for item in ranges or ()] == [(0, 30), (30, 36)]

    document["stages"][1]["backend"] = "mlx"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(RuntimeError, match="model_preparation_authorization_invalid"):
        builder._preparation_authorization(
            path,
            model_id=model_id,
            resolved_commit=revision,
            topology=topology,
        )


def test_m18_kv_admission_covers_model_context_not_only_startup_prompt() -> None:
    assert (
        builder._m18_runtime_kv_bytes(
            {"max_position_embeddings": 32_768},
            qualification_token_count=76,
            track_membership_count=1,
        )
        == 1_048_576
    )


@pytest.mark.parametrize(
    ("nodes", "error"),
    [
        ([_topology_node(0, backend="mlx")], "topology_requires_two_or_more_nodes"),
        (
            [
                _topology_node(0, backend="mlx"),
                _topology_node(0, backend="numpy"),
            ],
            "topology_node_id_duplicate",
        ),
        (
            [
                _topology_node(1, backend="mlx"),
                _topology_node(0, backend="numpy"),
            ],
            "topology_order_must_match_node_ids",
        ),
        (
            [
                _topology_node(0, backend="mlx"),
                _topology_node(1, backend="cuda"),
            ],
            "topology_runtime_backend_unsupported",
        ),
        (
            [
                _topology_node(0, backend="mlx"),
                {
                    **_topology_node(1, backend="numpy"),
                    "process_transport": "local",
                },
            ],
            "topology_requires_one_local_node",
        ),
    ],
)
def test_topology_rejects_unsafe_route_shapes(
    tmp_path: Path, nodes: list[dict], error: str
) -> None:
    topology = _write_topology(tmp_path / "topology.json", nodes)

    with pytest.raises(RuntimeError, match=f"^{error}$"):
        builder._topology_nodes(_template(), topology)


def test_startup_challenge_executes_every_stage_for_every_token(monkeypatch) -> None:
    mlx_calls: list[str] = []
    numpy_calls: list[str] = []
    codec = SimpleNamespace(encode=lambda _prompt: (11, 12))

    def execute_mlx(stage, *, token_ids=None, hidden_states=None):
        mlx_calls.append(stage)
        if token_ids is not None:
            return builder.mx.ones((1, len(token_ids), 2), dtype=builder.mx.float32)
        return hidden_states + 1

    monkeypatch.setattr(builder, "execute_loaded_stage", execute_mlx)

    def execute_numpy(stage, *, hidden_states):
        numpy_calls.append(stage)
        return np.asarray([[[0.0, 1.0, 2.0]]])

    monkeypatch.setattr(builder, "execute_loaded_numpy_stage", execute_numpy)
    monkeypatch.setattr(builder, "quantized_greedy_token_id", lambda _logits: 2)

    prompt, output = builder._challenge(
        codec,
        ["first", "middle", "last"],
        ["mlx", "mlx", "numpy"],
    )

    assert prompt == (11, 12)
    assert output == (2, 2, 2, 2)
    assert mlx_calls == ["first", "middle"] * 4
    assert numpy_calls == ["last"] * 4


def test_large_candidate_challenge_loads_only_one_stage_at_a_time(monkeypatch) -> None:
    alive: list[str] = []
    maximum = 0

    class Loaded:
        def __init__(self, name: str) -> None:
            nonlocal maximum
            self.name = name
            alive.append(name)
            maximum = max(maximum, len(alive))

        def __del__(self) -> None:
            if self.name in alive:
                alive.remove(self.name)

    monkeypatch.setattr(
        builder,
        "load_assignment_stage",
        lambda assignment, _report, **_kwargs: Loaded(assignment["node_id"]),
    )
    monkeypatch.setattr(builder, "_release_runtime_memory", lambda: None)
    monkeypatch.setattr(
        builder,
        "execute_loaded_stage",
        lambda _loaded, token_ids=None, hidden_states=None: (
            builder.mx.ones((1, len(token_ids), 3), dtype=builder.mx.float32)
            if token_ids is not None
            else hidden_states
        ),
    )
    monkeypatch.setattr(
        builder,
        "execute_loaded_numpy_stage",
        lambda _loaded, hidden_states: np.asarray(hidden_states),
    )
    monkeypatch.setattr(builder, "quantized_greedy_token_id", lambda _logits: 2)
    codec = SimpleNamespace(encode=lambda _prompt: (11, 12))

    prompt, output = builder._streaming_challenge(
        codec,
        [{"node_id": "node-0"}, {"node_id": "node-1"}],
        [{}, {}],
        ["mlx", "numpy"],
    )

    assert prompt == (11, 12)
    assert output == (2,)
    assert maximum == 1


def test_remote_identity_program_matches_controller_derivation_locally() -> None:
    run_id = "m7-identity-portability-test"
    completed = subprocess.run(
        [sys.executable, "-c", builder._remote_identity_program(run_id)],
        check=True,
        capture_output=True,
        text=True,
    )

    assert tuple(json.loads(completed.stdout)) == derive_local_run_scoped_identity(
        run_id
    )


def test_runtime_closure_includes_transitive_planner_packages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    bundle = tmp_path / "bundle"
    (repo / "mycelium_capacity_profiles").mkdir(parents=True)
    (repo / "mycelium_gossip").mkdir()
    (repo / "mycelium_layer_planner").mkdir()
    for relative in (
        "physical_inference_node.py",
        "weight_quantization.py",
        "mycelium_capacity_profiles/__init__.py",
        "mycelium_capacity_profiles/status.py",
        "mycelium_gossip/__init__.py",
        "mycelium_gossip/signed_bundle.py",
        "mycelium_layer_planner/__init__.py",
        "mycelium_layer_planner/gossip_adapter.py",
    ):
        path = repo / relative
        path.write_text(f"# {relative}\n", encoding="utf-8")
    template = {
        "controller": {
            "transfer_manifest": {"files": [{"path": "physical_inference_node.py"}]}
        }
    }

    builder._copy_runtime_closure(repo, bundle, template)

    assert (bundle / "mycelium_capacity_profiles/status.py").is_file()
    assert (bundle / "mycelium_gossip/signed_bundle.py").is_file()
    assert (bundle / "mycelium_layer_planner/gossip_adapter.py").is_file()


def test_target_proof_uses_assignment_control_plane_binding() -> None:
    binding = {
        "protocol": "mycelium.control_plane_binding.v1",
        "swarm_id": "live-swarm",
        "deployment_id": "deployment-0",
        "deployment_epoch": 1,
        "snapshot_generation": 8,
        "evidence_bundle_digest": "sha256:" + "1" * 64,
        "planner_snapshot_digest": "sha256:" + "2" * 64,
    }
    proof = builder._target_proof(
        {
            "assignment_id": "fixture-assignment",
            "node_id": "fixture-node",
            "control_plane_binding": {"fixture": True},
        },
        {
            "assignment_id": "assignment-0",
            "node_id": "node-0",
            "control_plane_binding": binding,
        },
        {"stage_pack_digest": "sha256:" + "3" * 64},
        {"stage_pack_verification_digest": "sha256:" + "4" * 64},
    )

    assert proof["control_plane_binding"] == binding


@pytest.mark.parametrize("label", ["M8", "m8_cached", "m8/route", "8m"])
def test_route_label_rejects_unsafe_identifiers(label: str) -> None:
    with pytest.raises(RuntimeError, match="^route_label_invalid$"):
        builder._route_label(SimpleNamespace(route_label=label))


def test_membership_snapshot_uses_custom_route_label() -> None:
    assignments = [
        {
            "node_id": "node-0",
            "deployment_id": "deployment-0",
            "deployment_epoch": 1,
            "assignment_id": "assignment-0",
        }
    ]
    packs = [{"stage_pack_digest": "sha256:" + "1" * 64}]

    snapshot = builder._membership_snapshot(
        assignments=assignments,
        packs=packs,
        graph_document={"protocol": "graph.v1", "stages": []},
        endpoint_ids={"node-0": "endpoint-0"},
        now=1_000.0,
        route_label="m8-cached",
    )

    message = snapshot["assignment_offers"][0]["message"]
    assert snapshot["swarm_id"] == "mycelium-m8-cached-qwen-multi-host"
    assert message["message_id"] == "m8-cached-qwen-offer-0"
    assert message["incarnation"] == "m8-cached-qwen-incarnation"


def test_membership_snapshot_preserves_planner_v2_provenance() -> None:
    snapshot = builder._membership_snapshot(
        assignments=[
            {
                "node_id": "node-0",
                "deployment_id": "deployment-0",
                "deployment_epoch": 1,
                "assignment_id": "assignment-0",
            }
        ],
        packs=[{"stage_pack_digest": "sha256:" + "1" * 64}],
        graph_document={"protocol": "graph.v1", "stages": []},
        endpoint_ids={"node-0": "endpoint-0"},
        now=1_000.0,
        route_label="m13",
        placement_provenance="planner_v2",
    )

    assert (
        snapshot["assignment_offers"][0]["message"]["placement_provenance"]
        == "planner_v2"
    )


def test_membership_snapshot_sorts_peer_records_independently_of_route_order() -> None:
    assignments = [
        {
            "node_id": node_id,
            "deployment_id": "deployment-0",
            "deployment_epoch": 1,
            "assignment_id": f"assignment-{node_id}",
        }
        for node_id in ("node-0", "node-2", "node-1")
    ]
    snapshot = builder._membership_snapshot(
        assignments=assignments,
        packs=[
            {"stage_pack_digest": "sha256:" + str(index + 1) * 64} for index in range(3)
        ],
        graph_document={"protocol": "graph.v1", "stages": []},
        endpoint_ids={
            node_id: f"endpoint-{node_id}" for node_id in ("node-0", "node-1", "node-2")
        },
        now=1_000.0,
        route_label="m14",
        placement_provenance="planner_v2",
    )

    by_recipient = {
        envelope["message"]["recipient_node_id"]: envelope["message"]
        for envelope in snapshot["assignment_offers"]
    }
    assert [
        record["node_id"] for record in by_recipient["node-0"]["peer_endpoint_records"]
    ] == ["node-1", "node-2"]


def test_m13_control_plane_extracts_exact_track_ranges(tmp_path: Path) -> None:
    document = {
        "protocol": "mycelium.m13_physical_candidate.v1",
        "signed_evidence_bundle": {
            "evidence_bundle": {"deployment": {"deployment_id": "deployment-0"}}
        },
        "planner_snapshot": {},
        "route_plan": {
            "placements": [
                {
                    "placement_id": "placement-a",
                    "node_id": "node-0",
                    "primary": True,
                    "layer_range": {"start": 0, "end": 15},
                },
                {
                    "placement_id": "placement-b",
                    "node_id": "node-1",
                    "primary": True,
                    "layer_range": {"start": 15, "end": 24},
                },
            ],
            "legal_tracks": [
                {
                    "placement_ids": ["placement-a", "placement-b"],
                }
            ],
        },
        "model": {},
        "workload": {},
        "policy": {},
        "quantization": "int8-weight-only",
        "ab_deltas": [],
        "exclusions": [],
    }
    path = tmp_path / "m13.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    loaded, ranges = builder._m13_control_plane(
        path,
        node_ids=("node-0", "node-1"),
        deployment_id="deployment-0",
    )

    assert loaded == document
    assert ranges == (range(0, 15), range(15, 24))

    with pytest.raises(RuntimeError, match="m13_control_plane_node_order_mismatch"):
        builder._m13_control_plane(
            path,
            node_ids=("node-1", "node-0"),
            deployment_id="deployment-0",
        )


def test_live_route_reissues_membership_against_current_clock(tmp_path: Path) -> None:
    snapshot = builder._membership_snapshot(
        assignments=[
            {
                "node_id": "node-0",
                "deployment_id": "deployment-0",
                "deployment_epoch": 1,
                "assignment_id": "assignment-0",
            }
        ],
        packs=[{"stage_pack_digest": "sha256:" + "1" * 64}],
        graph_document={"protocol": "graph.v1", "stages": []},
        endpoint_ids={"node-0": "endpoint-0"},
        now=1_000.0,
        route_label="m11-live",
    )

    signer_path = tmp_path / "seed" / "identity.key"
    signer = load_or_create_node_signer(signer_path)
    members = (
        {
            "node_id": "node-0",
            "endpoint_id": "coordinator-endpoint-0",
            "generation": 7,
            "lease_expires_at": 14_000.0,
            "peer_class": "mac_mlx_iroh",
            "runtime_capability": {
                "runtime_backend": "mlx",
                "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
        },
    )
    refreshed = _refresh_membership_snapshot(
        snapshot,
        now=10_000.0,
        signer=signer,
        seed_node_id="seed-node",
        members=members,
    )
    envelope = refreshed["assignment_offers"][0]

    assert envelope["message"]["issued_at"] == 10_000.0
    assert envelope["message"]["expires_at"] == 13_600.0
    assert refreshed["seed_key_digest"] == signer.verification_key_digest
    assert envelope["message"]["generation"] == 7
    assert (
        verify_membership_message(
            envelope,
            now=10_001.0,
            expected_key_digest=refreshed["seed_key_digest"],
            expected_protocol=ASSIGNMENT_OFFER_PROTOCOL,
            expected_swarm_id=refreshed["swarm_id"],
            expected_recipient_node_id="node-0",
        )["recipient_node_id"]
        == "node-0"
    )

    restarted_signer = load_or_create_node_signer(signer_path)
    restarted = _refresh_membership_snapshot(
        snapshot,
        now=20_000.0,
        signer=restarted_signer,
        seed_node_id="seed-node",
        members=({**members[0], "lease_expires_at": 24_000.0},),
    )
    assert restarted["seed_key_digest"] == refreshed["seed_key_digest"]
    assert restarted_signer.endpoint_id == signer.endpoint_id


def test_live_route_replaces_stale_plan_peer_records_from_membership_authority(
    tmp_path: Path,
) -> None:
    snapshot = builder._membership_snapshot(
        assignments=[
            {
                "node_id": f"node-{index}",
                "deployment_id": "deployment-0",
                "deployment_epoch": 1,
                "assignment_id": f"assignment-{index}",
            }
            for index in range(2)
        ],
        packs=[
            {"stage_pack_digest": "sha256:" + str(index + 1) * 64} for index in range(2)
        ],
        graph_document={"protocol": "graph.v1", "stages": []},
        endpoint_ids={"node-0": "stale-0", "node-1": "stale-1"},
        now=1_000.0,
        route_label="m12-live",
    )
    signer = load_or_create_node_signer(tmp_path / "seed" / "identity.key")
    capability = {
        "runtime_backend": "mlx",
        "transport": "iroh",
        "activation_protocol": "mycelium.router_wire.v1",
    }

    refreshed = _refresh_membership_snapshot(
        snapshot,
        now=2_000.0,
        signer=signer,
        seed_node_id="seed-node",
        members=(
            {
                "node_id": "node-0",
                "endpoint_id": "current-0",
                "generation": 11,
                "lease_expires_at": 2_600.0,
                "peer_class": "mac_mlx_iroh",
                "runtime_capability": capability,
            },
            {
                "node_id": "node-1",
                "endpoint_id": "current-1",
                "generation": 13,
                "lease_expires_at": 2_700.0,
                "peer_class": "mac_mlx_iroh",
                "runtime_capability": capability,
            },
        ),
    )

    by_recipient = {
        offer["message"]["recipient_node_id"]: offer["message"]
        for offer in refreshed["assignment_offers"]
    }
    assert by_recipient["node-0"]["generation"] == 11
    assert by_recipient["node-0"]["peer_endpoint_records"] == [
        {
            "node_id": "node-1",
            "endpoint_id": "current-1",
            "deployment_epoch": 1,
            "membership_generation": 13,
            "valid_from": 2_000.0,
            "valid_until": 2_600.0,
        }
    ]
    assert "stale-1" not in json.dumps(refreshed)


def test_live_route_rejects_plan_placement_without_current_member(
    tmp_path: Path,
) -> None:
    snapshot = builder._membership_snapshot(
        assignments=[
            {
                "node_id": "node-0",
                "deployment_id": "deployment-0",
                "deployment_epoch": 1,
                "assignment_id": "assignment-0",
            }
        ],
        packs=[{"stage_pack_digest": "sha256:" + "1" * 64}],
        graph_document={"protocol": "graph.v1", "stages": []},
        endpoint_ids={"node-0": "stale-0"},
        now=1_000.0,
        route_label="m12-live",
    )
    signer = load_or_create_node_signer(tmp_path / "seed" / "identity.key")

    with pytest.raises(ValueError, match="^membership_member_missing$"):
        _refresh_membership_snapshot(
            snapshot,
            now=2_000.0,
            signer=signer,
            seed_node_id="seed-node",
            members=(),
        )


def test_model_identity_accepts_pinned_qwen_variant() -> None:
    assert builder._model_identity(
        SimpleNamespace(
            model_id="Qwen/Qwen2.5-1.5B-Instruct",
            resolved_commit="a" * 40,
        )
    ) == ("Qwen/Qwen2.5-1.5B-Instruct", "a" * 40, "qwen2-5-1-5b-instruct")


@pytest.mark.parametrize(
    ("model_id", "resolved_commit", "error"),
    [
        ("Qwen", "a" * 40, "model_id_invalid"),
        ("Qwen/Model", "main", "resolved_commit_invalid"),
    ],
)
def test_model_identity_rejects_unpinned_source(
    model_id: str, resolved_commit: str, error: str
) -> None:
    with pytest.raises(RuntimeError, match=f"^{error}$"):
        builder._model_identity(
            SimpleNamespace(model_id=model_id, resolved_commit=resolved_commit)
        )
