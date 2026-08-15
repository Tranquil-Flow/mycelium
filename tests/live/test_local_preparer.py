from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_live.local_preparer import (
    LocalCandidatePreparer,
    MemberStagePackPromotion,
    _bind_member_promotions,
    _new_attempt_identity,
    _topology_from_template,
    _validate_preparation_authorization,
)
from mycelium_live.preparation import ModelPreparationError
from mycelium_swarm_artifacts import ACQUISITION_PROTOCOL, canonical_digest


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def test_each_preparation_attempt_gets_a_distinct_candidate_route_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = iter(
        [
            SimpleNamespace(hex="1" * 32),
            SimpleNamespace(hex="2" * 32),
        ]
    )
    monkeypatch.setattr("mycelium_live.local_preparer.uuid.uuid4", lambda: next(values))

    first = _new_attempt_identity("a" * 40)
    second = _new_attempt_identity("a" * 40)

    assert first == ("aaaaaaaaaaaa-111111111111", "modelprep-111111111111")
    assert second == ("aaaaaaaaaaaa-222222222222", "modelprep-222222222222")
    assert first != second


def _authorization(valid_until: int = 10_000) -> dict:
    document = {
        "protocol": "mycelium.model_preparation_authorization.v1",
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "revision": "a" * 40,
        "representation_digest": _digest("3"),
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": "int8-weight-only",
        "conversion_authorized": True,
        "owner_decision_digest": _digest("4"),
        "feasibility_digest": _digest("5"),
        "download_authorized": False,
        "stages": [],
        "evidence_valid_until_unix_ms": valid_until,
    }
    document["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": document["feasibility_digest"],
            "owner_decision_digest": document["owner_decision_digest"],
            "representation_digest": document["representation_digest"],
        }
    )
    return document


def test_preparation_topology_preserves_exact_signed_membership_generations(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    runtime_nodes = []
    peers = []
    stages = []
    for index, (node_id, backend, generation) in enumerate(
        (("node-0", "mlx", 25), ("node-2", "numpy", 39))
    ):
        assignment_path = f"control/{node_id}-assignment.json"
        path = source_root / assignment_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"runtime": {"backend": backend}}), encoding="utf-8")
        runtime_nodes.append(
            {
                "node_id": node_id,
                "python_executable": "/usr/bin/python3",
                "sidecar_binary": f"/srv/{node_id}/sidecar",
                "endpoint_secret_file": f"/srv/{node_id}/identity.key",
                "configure": {"assignment_file": assignment_path},
            }
        )
        peers.append(
            {
                "node_id": node_id,
                "process_transport": "local" if index == 0 else "ssh",
                "ssh_target": "local" if index == 0 else "surface",
                "staging_root": f"/srv/{node_id}",
            }
        )
        stages.append({"node_id": node_id, "backend": backend})
    template = {
        "controller": {
            "source_root": str(source_root),
            "peers": peers,
            "run_plan": {"nodes": runtime_nodes},
            "membership_snapshot": {
                "assignment_offers": [
                    {
                        "message": {
                            "recipient_node_id": "node-0",
                            "recipient_endpoint_id": "endpoint-0",
                            "generation": 25,
                            "peer_endpoint_records": [
                                {
                                    "node_id": "node-2",
                                    "endpoint_id": "endpoint-2",
                                    "membership_generation": 39,
                                }
                            ],
                        }
                    },
                    {
                        "message": {
                            "recipient_node_id": "node-2",
                            "recipient_endpoint_id": "endpoint-2",
                            "generation": 39,
                            "peer_endpoint_records": [
                                {
                                    "node_id": "node-0",
                                    "endpoint_id": "endpoint-0",
                                    "membership_generation": 25,
                                }
                            ],
                        }
                    },
                ]
            },
        }
    }

    topology = _topology_from_template(
        template, {"stages": stages}, route_label="modelprep-abc123"
    )

    assert [node["membership_generation"] for node in topology["nodes"]] == [25, 39]
    assert [node["staging_root"] for node in topology["nodes"]] == [
        "/srv/node-0-candidate-modelprep-abc123",
        "/srv/node-2-candidate-modelprep-abc123",
    ]


def _ready_status(manifest: dict) -> dict:
    return {
        "protocol": ACQUISITION_PROTOCOL,
        "generation": 1,
        "acquisition_id": "acquisition-" + manifest["recipient_member_id"],
        "state": "ready",
        "phase": None,
        "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
        "model_revision": "a" * 40,
        "representation": "bfloat16",
        "assignment_id": "assignment-" + manifest["recipient_member_id"],
        "placement_id": "placement-" + manifest["recipient_member_id"],
        "stage_id": "stage-" + manifest["recipient_member_id"],
        "layer_start": 0,
        "layer_end_exclusive": 1,
        "total_bytes": 10,
        "cached_verified_bytes": 0,
        "transferred_verified_bytes": 10,
        "missing_bytes": 0,
        "quarantined_bytes": 0,
        "duplicate_bytes_prevented": 0,
        "eligible_source_count": 1,
        "active_source_count": 0,
        "sources": [
            {
                "source_ref": "source-000000000001",
                "state": "rotated",
                "verified_bytes": 10,
            }
        ],
        "origin_bytes": 0,
        "aggregate_bytes_per_second": 10.0,
        "eta_seconds": 0.0,
        "chunk_count": 1,
        "verified_chunk_count": 1,
        "resumed_chunk_count": 0,
        "source_rotation_count": 0,
        "manifest_digest": manifest["manifest_digest"],
        "assignment_digest": manifest["assignment_digest"],
        "representation_digest": manifest["representation_digest"],
        "feasibility_digest": _digest("f"),
        "evidence_generation": 1,
        "promotion_digest": _digest("9"),
        "reason_code": None,
        "retryable": False,
        "started_at_unix_ms": 1_000,
        "updated_at_unix_ms": 1_100,
        "terminal_at_unix_ms": 1_100,
    }


def _fixture() -> tuple[dict, list[tuple[dict, MemberStagePackPromotion]]]:
    script = {
        "path": "physical_inference_node.py",
        "size_bytes": 100,
        "content_digest": _digest("1"),
    }
    artifacts = {
        "node-0": {
            "path": "deployment/stage-0.safetensors",
            "size_bytes": 10,
            "content_digest": _digest("2"),
        },
        "node-1": {
            "path": "deployment/stage-1.safetensors",
            "size_bytes": 10,
            "content_digest": _digest("3"),
        },
    }
    controller = {
        "peers": [
            {"node_id": "node-0", "process_transport": "local"},
            {"node_id": "node-1", "process_transport": "ssh"},
        ],
        "transfer_manifest": {
            "protocol": "mycelium.controller_transfer_manifest.v1",
            "files": [script, artifacts["node-0"], artifacts["node-1"]],
        },
        "node_transfer_manifests": {
            "protocol": "mycelium.controller_node_transfer_manifests.v1",
            "manifests": {
                node_id: {
                    "protocol": "mycelium.controller_transfer_manifest.v1",
                    "files": [script, record],
                }
                for node_id, record in artifacts.items()
            },
        },
    }
    promotions = []
    for index, (node_id, record) in enumerate(artifacts.items()):
        manifest = {
            "recipient_member_id": node_id,
            "manifest_digest": _digest(str(4 + index)),
            "assignment_digest": _digest(str(6 + index)),
            "representation_digest": _digest("8"),
            "files": [
                {
                    "relative_path": record["path"],
                    "size_bytes": record["size_bytes"],
                    "content_digest": record["content_digest"],
                }
            ],
        }
        promotions.append(
            (
                manifest,
                MemberStagePackPromotion(
                    member_id=node_id,
                    files_root=f"/private/mycelium/{node_id}/promoted/files",
                    status=_ready_status(manifest),
                ),
            )
        )
    return controller, promotions


def test_member_promotions_replace_coordinator_model_transfers() -> None:
    controller, promotions = _fixture()

    _bind_member_promotions(controller, promotions)

    assert all(
        [record["path"] for record in manifest["files"]]
        == ["physical_inference_node.py"]
        for manifest in controller["node_transfer_manifests"]["manifests"].values()
    )
    prepositioned = controller["prepositioned_artifacts"]
    assert prepositioned["protocol"] == (
        "mycelium.controller_prepositioned_artifacts.v1"
    )
    assert prepositioned["members"]["node-1"][0] == {
        "destination_path": "deployment/stage-1.safetensors",
        "source_path": (
            "/private/mycelium/node-1/promoted/files/deployment/stage-1.safetensors"
        ),
        "size_bytes": 10,
        "content_digest": _digest("3"),
    }


def test_member_promotion_rejects_recipient_or_digest_substitution() -> None:
    controller, promotions = _fixture()
    wrong_member = list(promotions)
    manifest, promotion = wrong_member[1]
    wrong_member[1] = (
        manifest,
        MemberStagePackPromotion(
            member_id="node-0",
            files_root=promotion.files_root,
            status=promotion.status,
        ),
    )
    with pytest.raises(
        ModelPreparationError,
        match="member_artifact_recipient_binding_invalid",
    ):
        _bind_member_promotions(deepcopy(controller), wrong_member)

    wrong_digest = deepcopy(promotions)
    wrong_digest[0][1].status["manifest_digest"] = _digest("0")
    with pytest.raises(
        ModelPreparationError,
        match="member_artifact_promotion_binding_invalid",
    ):
        _bind_member_promotions(deepcopy(controller), wrong_digest)


def test_stage_manifest_uses_current_preparation_clock_not_plan_build_time(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    control = bundle / "control"
    control.mkdir()
    authorization = _authorization()
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(authorization), encoding="utf-8"
    )
    (bundle / "stage-pack.json").write_text(
        '{"assignment_id":"assignment-a","node_id":"node-0"}',
        encoding="utf-8",
    )
    captured: list[tuple[int, int]] = []

    def build_source(**kwargs):
        captured.append((kwargs["issued_at_unix_ms"], kwargs["expires_at_unix_ms"]))
        return (
            {
                "manifest_id": "manifest-a",
                "manifest_digest": _digest("1"),
                "assignment_digest": _digest("2"),
                "representation_digest": _digest("3"),
                "feasibility_digest": _digest("4"),
                "recipient_member_id": "node-0",
                "recipient_membership_generation": 1,
                "total_size_bytes": 10,
                "chunks": [{"content_digest": _digest("5")}],
            },
            {},
        )

    class FakeProvisioner:
        def __init__(self, _store):
            pass

        def acquire(self, **_kwargs):
            return {"state": "ready", "reason_code": None}

    monkeypatch.setattr(
        "mycelium_live.local_preparer.build_stage_pack_source", build_source
    )
    monkeypatch.setattr(
        "mycelium_live.local_preparer.SwarmArtifactProvisioner", FakeProvisioner
    )
    monkeypatch.setattr(
        "mycelium_live.local_preparer._bind_member_promotions",
        lambda *_args: None,
    )
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._artifact_store = SimpleNamespace(root=tmp_path / "artifacts")
    preparer._member_stage_pack_acquirer = None
    preparer._clock = lambda: 9_000
    plan = {
        "now_unix_ms": 1_000,
        "controller": {
            "source_root": str(bundle),
            "run_plan": {
                "nodes": [
                    {
                        "node_id": "node-0",
                        "configure": {
                            "graph": {},
                            "stage_pack_file": "stage-pack.json",
                        },
                    }
                ]
            },
            "membership_snapshot": {
                "assignment_offers": [{"message": {"assignment_id": "assignment-a"}}]
            },
            "peers": [{"node_id": "node-0", "process_transport": "local"}],
        },
    }

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    preparer._acquire_stage_packs(
        attempt=attempt,
        authorization=authorization,
        plan=plan,
    )

    assert captured == [(9_000, 909_000)]


def test_stage_acquisition_rejects_capacity_stale_at_attempt_start(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._clock = lambda: 9_001

    attempt = tmp_path / "attempt"
    attempt.mkdir()
    plan = {
        "controller": {
            "source_root": str(bundle),
            "run_plan": {"nodes": []},
            "membership_snapshot": {"assignment_offers": []},
            "peers": [],
        }
    }

    with pytest.raises(ModelPreparationError, match="model_capacity_stale"):
        preparer._acquire_stage_packs(
            attempt=attempt,
            authorization={"evidence_valid_until_unix_ms": 9_000},
            plan=plan,
        )


def test_preparation_authorization_rejects_binding_representation_and_assignment_drift(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    control = bundle / "control"
    control.mkdir(parents=True)
    frozen = _authorization()
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(frozen), encoding="utf-8"
    )

    binding_drift = deepcopy(frozen)
    binding_drift["feasibility_digest"] = _digest("6")
    with pytest.raises(
        ModelPreparationError, match="model_preparation_authorization_invalid"
    ):
        _validate_preparation_authorization(bundle, binding_drift)

    representation_drift = deepcopy(frozen)
    representation_drift["representation_digest"] = _digest("7")
    representation_drift["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": representation_drift["feasibility_digest"],
            "owner_decision_digest": representation_drift["owner_decision_digest"],
            "representation_digest": representation_drift["representation_digest"],
        }
    )
    with pytest.raises(ModelPreparationError, match="model_representation_drift"):
        _validate_preparation_authorization(bundle, representation_drift)

    assignment_drift = deepcopy(frozen)
    assignment_drift["stages"] = [{"node_id": "unassigned"}]
    with pytest.raises(ModelPreparationError, match="model_assignment_drift"):
        _validate_preparation_authorization(bundle, assignment_drift)
