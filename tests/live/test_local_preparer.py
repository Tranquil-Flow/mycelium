from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import subprocess

import pytest

from mycelium_live.local_preparer import (
    LocalCandidatePreparer,
    MemberStagePackPromotion,
    _authorization_started_within_lease,
    _bind_member_promotions,
    _directory_identity,
    _exact_candidate_authority_matches,
    _file_digest,
    _read_private_document,
    _resumable_authority_digest,
    _new_attempt_identity,
    _topology_from_operation_file,
    _topology_from_template,
    _validate_preparation_authorization,
    _write_durable_private_document,
)
from mycelium_live.preparation import ModelPreparationError
from mycelium_swarm_artifacts import ACQUISITION_PROTOCOL, canonical_digest


def _digest(character: str) -> str:
    return "sha256:" + character * 64


def _write_storage_preflight(
    attempt: Path,
    authorization: dict,
    *,
    measured_rate: int = 500_000,
) -> None:
    ttl_ms = LocalCandidatePreparer._stage_pack_authority_ttl_ms(
        authorization,
        measured_rate,
    )
    path = attempt / "storage-link-preflight.json"
    path.write_text(
        json.dumps(
            {
                "protocol": "mycelium.private_storage_link_preflight.v1",
                "state": "passed",
                "authority_digest": _resumable_authority_digest(authorization),
                "required_transfer_bytes": sum(
                    stage["assignment_artifact_bytes"]
                    for stage in authorization["stages"]
                ),
                "measured_effective_bytes_per_second": measured_rate,
                "lease_ttl_ms": ttl_ms,
                "last_progress_at_unix_ms": 1_000,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


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


def test_bound_preparation_workspace_disappearance_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "preparation"
    workspace.mkdir(mode=0o700)
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace
    preparer._workspace_identity = _directory_identity(workspace)
    preparer._temporary_root = None
    preparer._temporary_root_identity = None
    preparer._repo = tmp_path

    def disconnect_during_run(*_args, **_kwargs):
        workspace.rmdir()
        return subprocess.CompletedProcess([], 1, "", "source vanished")

    preparer._run = disconnect_during_run

    with pytest.raises(
        ModelPreparationError,
        match="^model_preparation_workspace_unavailable$",
    ):
        preparer._execute(
            ["builder"],
            "model_candidate_build_failed",
            diagnostic_path=workspace / "attempt" / "build-command.json",
        )


def test_bound_preparation_workspace_substitution_fails_closed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "preparation"
    workspace.mkdir(mode=0o700)
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace
    preparer._workspace_identity = _directory_identity(workspace)
    preparer._temporary_root = None
    preparer._temporary_root_identity = None
    workspace.rmdir()
    workspace.mkdir(mode=0o700)

    with pytest.raises(
        ModelPreparationError,
        match="^model_preparation_workspace_unavailable$",
    ):
        preparer._require_bound_storage()


def test_phase_checkpoint_is_atomic_private_and_tamper_evident(tmp_path: Path) -> None:
    destination = tmp_path / "phase-checkpoint.json"

    _write_durable_private_document(destination, {"phase": "candidate_challenged"})

    assert _read_private_document(destination) == {"phase": "candidate_challenged"}
    assert destination.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []
    destination.chmod(0o644)
    with pytest.raises(
        ModelPreparationError, match="model_preparation_checkpoint_invalid"
    ):
        _read_private_document(destination)


def test_acquired_checkpoint_recovers_only_its_exact_private_bound_plan(
    tmp_path: Path,
) -> None:
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    plan_path = attempt / "bound-operator-plan-a.json"
    plan = {
        "controller": {
            "prepositioned_artifacts": {
                "protocol": "mycelium.controller_prepositioned_artifacts.v1"
            }
        }
    }
    receipt = {"state": "ready", "acquisition_id": "acquisition-a"}
    _write_durable_private_document(plan_path, plan)
    checkpoint = {
        "phase_evidence": {
            "artifacts_acquired": {
                "bound_plan_digest": _file_digest(plan_path),
                "receipt_digests": [canonical_digest(receipt)],
            }
        }
    }
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._artifact_store = SimpleNamespace(
        ledger=lambda: {"history": [receipt]}
    )

    recovered_path, recovered = preparer._checkpoint_bound_plan(attempt, checkpoint)

    assert recovered_path == plan_path
    assert recovered == plan
    _write_durable_private_document(plan_path, {"controller": {}})
    with pytest.raises(
        ModelPreparationError, match="model_preparation_checkpoint_invalid"
    ):
        preparer._checkpoint_bound_plan(attempt, checkpoint)


def test_recovery_authority_requires_exact_candidate_and_valid_start() -> None:
    frozen = _authorization(valid_until=10_000)
    frozen.update(
        {
            "protocol": "mycelium.model_preparation_authorization.v2",
            "authorized_at_unix_ms": 9_000,
            "source_artifact_digest": _digest("8"),
            "quantizer": "mycelium.rowwise_symmetric_int8.v1",
            "stages": [
                {
                    "stage_index": 0,
                    "node_id": "node-0",
                    "start_layer": 0,
                    "end_layer_exclusive": 4,
                    "backend": "mlx",
                    "decode_mode": "complete_context_replay",
                    "assignment_files": ["stage-0.safetensors"],
                    "assignment_artifact_bytes": 10,
                }
            ],
        }
    )
    refreshed = deepcopy(frozen)
    refreshed["feasibility_digest"] = _digest("9")
    refreshed["evidence_generation"] = 8

    assert _authorization_started_within_lease(frozen) is True
    assert _exact_candidate_authority_matches(frozen, refreshed) is True

    expired_start = deepcopy(refreshed)
    expired_start["authorized_at_unix_ms"] = 10_001
    assert _authorization_started_within_lease(expired_start) is False
    stage_drift = deepcopy(refreshed)
    stage_drift["stages"][0]["end_layer_exclusive"] = 3
    assert _exact_candidate_authority_matches(frozen, stage_drift) is False
    owner_drift = deepcopy(refreshed)
    owner_drift["owner_decision_digest"] = _digest("0")
    assert _exact_candidate_authority_matches(frozen, owner_drift) is False


def test_large_candidate_lease_uses_measured_bounded_artifact_rate() -> None:
    policy = LocalCandidatePreparer._acquisition_policy(
        4 * 1024 * 1024,
        32_000_000,
    )

    assert policy["per_source_bytes_per_second"] == 32_000_000
    assert policy["aggregate_bytes_per_second"] == 32_000_000
    assert policy["serving_traffic_reserve_ratio"] == 0.4

    authorization = {
        "stages": [
            {"assignment_artifact_bytes": 11_674_894_879},
            {"assignment_artifact_bytes": 7_421_104_759},
        ]
    }
    ttl_ms = LocalCandidatePreparer._stage_pack_authority_ttl_ms(
        authorization,
        32_000_000,
    )
    assert 15 * 60 * 1_000 < ttl_ms <= 6 * 60 * 60 * 1_000
    assert LocalCandidatePreparer._stage_pack_chunk_size_bytes(authorization) == (
        12 * 1024 * 1024
    )


def test_transfer_that_cannot_fit_maximum_lease_fails_instead_of_clamping() -> None:
    exact_limit = {
        "stages": [
            {"assignment_artifact_bytes": 2_587_500},
        ]
    }
    assert LocalCandidatePreparer._stage_pack_authority_ttl_ms(
        exact_limit,
        1_000,
    ) == 6 * 60 * 60 * 1_000

    authorization = {
        "stages": [
            {"assignment_artifact_bytes": 2_587_501},
        ]
    }

    with pytest.raises(
        ModelPreparationError,
        match="^artifact_transfer_exceeds_maximum_lease$",
    ):
        LocalCandidatePreparer._stage_pack_authority_ttl_ms(
            authorization,
            1_000,
        )


def test_stage_pack_chunk_size_rejects_an_unbounded_manifest() -> None:
    authorization = {
        "stages": [
            {"assignment_artifact_bytes": 65 * 1024 * 1024 * 1024},
        ]
    }

    with pytest.raises(
        ModelPreparationError,
        match="stage_pack_manifest_too_large",
    ):
        LocalCandidatePreparer._stage_pack_chunk_size_bytes(authorization)


def test_candidate_is_bound_to_live_seed_before_staging(tmp_path: Path) -> None:
    generated = tmp_path / "generated.json"
    generated.write_text('{"controller":{}}', encoding="utf-8")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    seed = tmp_path / "seed"
    seed.mkdir()
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._python = "/usr/bin/python3"
    preparer._seed_state_root = seed
    calls: list[tuple[list[str], str, Path]] = []

    def execute(command: list[str], code: str, *, diagnostic_path: Path):
        calls.append((command, code, diagnostic_path))
        output = Path(command[command.index("--output") + 1])
        output.write_text(
            json.dumps(
                {
                    "controller": {
                        "membership_snapshot": {
                            "seed_key_digest": _digest("a"),
                            "swarm_id": "live-swarm",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, "", "")

    preparer._execute = execute

    path, plan = preparer._bind_candidate_to_seed(generated, attempt=attempt)

    assert path.parent == attempt
    assert path.name.startswith("bound-operator-plan-")
    assert path.suffix == ".json"
    assert plan["controller"]["membership_snapshot"] == {
        "seed_key_digest": _digest("a"),
        "swarm_id": "live-swarm",
    }
    command, code, diagnostic = calls[0]
    assert command == [
        "/usr/bin/python3",
        "scripts/bind_operator_plan_to_seed.py",
        "--operator-plan",
        str(generated),
        "--seed-state-root",
        str(seed),
        "--output",
        str(path),
    ]
    second_path, _ = preparer._bind_candidate_to_seed(generated, attempt=attempt)
    assert second_path != path
    assert second_path.name.startswith("bound-operator-plan-")
    assert calls[1][0][calls[1][0].index("--output") + 1] == str(second_path)
    assert code == "model_candidate_seed_binding_failed"
    assert diagnostic.parent == attempt
    assert diagnostic.name.startswith("seed-binding-command-")
    assert diagnostic.suffix == ".json"


def test_candidate_seed_binding_rejects_non_document(tmp_path: Path) -> None:
    generated = tmp_path / "generated.json"
    generated.write_text("{}", encoding="utf-8")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._python = "/usr/bin/python3"
    preparer._seed_state_root = tmp_path / "seed"

    def execute(command: list[str], _code: str, *, diagnostic_path: Path):
        del diagnostic_path
        output = Path(command[command.index("--output") + 1])
        output.write_text("[]", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    preparer._execute = execute

    with pytest.raises(
        ModelPreparationError,
        match="model_candidate_seed_binding_invalid",
    ):
        preparer._bind_candidate_to_seed(generated, attempt=attempt)


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


def test_exact_completed_stage_shards_are_reusable_across_authority_refresh(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "preparation"
    candidate = workspace / "old-attempt" / "candidate"
    deployment = candidate / "transfer-bundle" / "deployment"
    control = candidate / "transfer-bundle" / "control"
    deployment.mkdir(parents=True)
    control.mkdir()
    authorization = _authorization()
    authorization["stages"] = [
        {
            "stage_index": 0,
            "node_id": "node-0",
            "start_layer": 0,
            "end_layer_exclusive": 2,
            "backend": "mlx",
            "decode_mode": "complete_context_replay",
            "assignment_files": ["config.json", "model-a.safetensors"],
            "assignment_artifact_bytes": 10,
        },
        {
            "stage_index": 1,
            "node_id": "node-1",
            "start_layer": 2,
            "end_layer_exclusive": 4,
            "backend": "mlx",
            "decode_mode": "complete_context_replay",
            "assignment_files": ["config.json", "model-b.safetensors"],
            "assignment_artifact_bytes": 10,
        },
    ]
    (deployment / "config.json").write_text("{}", encoding="utf-8")
    (deployment / "model.safetensors.index.json").write_text("{}", encoding="utf-8")
    (deployment / "model-a.safetensors").write_bytes(b"weights")
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(authorization), encoding="utf-8"
    )
    (candidate / "build-report.json").write_text(
        json.dumps(
            {
                "model_id": authorization["model_id"],
                "resolved_commit": authorization["revision"],
                "quantization": authorization["serving_quantization"],
                "representation_digest": authorization["representation_digest"],
                "layer_ranges": [
                    {
                        "start_layer": 0,
                        "end_layer_exclusive": 2,
                        "layer_count": 2,
                    },
                    {
                        "start_layer": 2,
                        "end_layer_exclusive": 4,
                        "layer_count": 2,
                    },
                ],
                "stage_sharding": {"protocol": "test.stage_sharding.v1"},
            }
        ),
        encoding="utf-8",
    )
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace

    refreshed = deepcopy(authorization)
    refreshed["protocol"] = "mycelium.model_preparation_authorization.v2"
    refreshed["authorized_at_unix_ms"] = 9_000
    refreshed["evidence_valid_until_unix_ms"] = 20_000
    refreshed["feasibility_digest"] = _digest("9")
    refreshed["source_artifact_digest"] = _digest("8")
    refreshed["quantizer"] = "mycelium.rowwise_symmetric_int8.v1"
    refreshed["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": refreshed["feasibility_digest"],
            "owner_decision_digest": refreshed["owner_decision_digest"],
            "quantizer": refreshed["quantizer"],
            "representation_digest": refreshed["representation_digest"],
            "source_artifact_digest": refreshed["source_artifact_digest"],
        }
    )

    assert preparer._reusable_stage_sharded_root(refreshed) == deployment.resolve()

    # A builder that reached a late remote-peer failure has no terminal report, but
    # its exact v2 authorization and private deployment remain safe reuse inputs.
    (candidate / "build-report.json").unlink()
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(refreshed), encoding="utf-8"
    )
    assert preparer._reusable_stage_sharded_root(refreshed) == deployment.resolve()

    # The deployment contains the complete immutable representation, so a newly
    # authorized allocation may move its layer boundary without rebuilding bytes.
    refreshed["stages"][0]["end_layer_exclusive"] = 3
    refreshed["stages"][1]["start_layer"] = 3
    assert preparer._reusable_stage_sharded_root(refreshed) == deployment.resolve()

    refreshed["representation_digest"] = _digest("different-representation")
    assert preparer._reusable_stage_sharded_root(refreshed) is None


def test_cold_local_acquisition_disk_rejects_before_model_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization()
    authorization["evidence_generation"] = 7
    authorization["feasibility_digest"] = _digest("feasibility")
    authorization["stages"] = [
        {
            "node_id": "node-0",
            "start_layer": 0,
            "end_layer_exclusive": 4,
            "assignment_artifact_bytes": 10_000,
        },
        {
            "node_id": "node-1",
            "start_layer": 4,
            "end_layer_exclusive": 8,
            "assignment_artifact_bytes": 10_000,
        },
    ]
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._artifact_store = SimpleNamespace(
        root=tmp_path,
        ledger=lambda: {"history": []},
    )
    preparer._clock = lambda: 1_000
    monkeypatch.setattr(
        "mycelium_live.local_preparer.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=1),
    )
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    with pytest.raises(ModelPreparationError, match="^insufficient_disk$"):
        preparer._preflight_local_acquisition_storage(
            authorization,
            {
                "nodes": [
                    {"node_id": "node-0", "process_transport": "local"},
                    {"node_id": "node-1", "process_transport": "ssh"},
                ]
            },
            attempt=attempt,
        )
    report = json.loads((attempt / "storage-link-preflight.json").read_text())
    assert report["state"] == "failed"
    assert report["blocker"] == "insufficient_disk"
    assert "owner-approved artifact volume" in report["recommended_recovery"]


def test_exact_warm_local_acquisition_does_not_require_cold_disk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization()
    authorization["evidence_generation"] = 7
    authorization["feasibility_digest"] = _digest("feasibility")
    authorization["stages"] = [
        {
            "node_id": "node-0",
            "start_layer": 0,
            "end_layer_exclusive": 4,
            "assignment_artifact_bytes": 10_000,
        },
        {
            "node_id": "node-1",
            "start_layer": 4,
            "end_layer_exclusive": 8,
            "assignment_artifact_bytes": 10_000,
        },
    ]
    warm = {
        "state": "ready",
        "model_id": authorization["model_id"],
        "model_revision": authorization["revision"],
        "representation_digest": authorization["representation_digest"],
        "feasibility_digest": authorization["feasibility_digest"],
        "evidence_generation": 7,
        "layer_start": 0,
        "layer_end_exclusive": 4,
        "total_bytes": 10_000,
        "promotion_digest": _digest("promotion"),
    }
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._artifact_store = SimpleNamespace(
        root=tmp_path,
        ledger=lambda: {"history": [warm]},
    )
    monkeypatch.setattr(
        "mycelium_live.local_preparer.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=0),
    )
    preparer._clock = lambda: 1_000
    preparer._artifact_transfer_rate_cap = None
    preparer._storage_link_probe = lambda _root: {
        "probe_bytes": 8 * 1024 * 1024,
        "write_bytes_per_second": 10_000_000,
        "read_bytes_per_second": 8_000_000,
    }
    attempt = tmp_path / "attempt"
    attempt.mkdir()

    report = preparer._preflight_local_acquisition_storage(
        authorization,
        {
            "nodes": [
                {"node_id": "node-0", "process_transport": "local"},
                {"node_id": "node-1", "process_transport": "ssh"},
            ]
        },
        attempt=attempt,
    )
    assert report["required_storage_bytes"] == 0
    assert report["required_transfer_bytes"] == 10_000
    assert report["measured_effective_bytes_per_second"] == 8_000_000


def test_storage_preflight_runs_before_local_model_snapshot_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "preparation"
    workspace.mkdir(mode=0o700)
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")
    topology = {
        "protocol": "mycelium.qwen_live_topology.v1",
        "nodes": [
            {"node_id": "node-0", "process_transport": "local"},
            {"node_id": "node-1", "process_transport": "ssh"},
        ],
    }
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace
    preparer._template = template
    preparer._execution_topology = None
    preparer._preflight_local_acquisition_storage = (
        lambda _authorization, _topology, **_kwargs: (_ for _ in ()).throw(
            ModelPreparationError("insufficient_disk")
        )
    )
    preparer._snapshot = lambda *_args: pytest.fail(
        "snapshot scan ran before storage preflight"
    )
    monkeypatch.setattr(
        "mycelium_live.local_preparer._topology_from_template",
        lambda *_args, **_kwargs: topology,
    )

    with pytest.raises(ModelPreparationError, match="^insufficient_disk$"):
        preparer(_authorization(), lambda *_args: None)


def test_storage_preflight_measures_rate_and_persists_lease_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authorization = _authorization()
    authorization["stages"] = [
        {"node_id": "node-0", "assignment_artifact_bytes": 2_000_000},
        {"node_id": "node-1", "assignment_artifact_bytes": 3_000_000},
    ]
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._artifact_store = SimpleNamespace(
        root=tmp_path,
        ledger=lambda: {"history": []},
    )
    preparer._clock = lambda: 5_000
    preparer._artifact_transfer_rate_cap = None
    preparer._storage_link_probe = lambda _root: {
        "probe_bytes": 8 * 1024 * 1024,
        "write_bytes_per_second": 12_000_000,
        "read_bytes_per_second": 9_000_000,
    }
    monkeypatch.setattr(
        "mycelium_live.local_preparer.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=2_000_000_000),
    )

    report = preparer._preflight_local_acquisition_storage(
        authorization,
        {
            "nodes": [
                {"node_id": "node-0", "process_transport": "local"},
                {"node_id": "node-1", "process_transport": "ssh"},
            ]
        },
        attempt=attempt,
    )

    persisted = json.loads((attempt / "storage-link-preflight.json").read_text())
    assert persisted == report
    assert report["state"] == "passed"
    assert report["summary"] == (
        "Measured storage throughput fits the bounded acquisition lease"
    )
    assert report["measured_effective_bytes_per_second"] == 9_000_000
    assert report["required_transfer_bytes"] == 5_000_000
    assert report["lease_ttl_ms"] == 904_445
    assert report["lease_scope"] == "stage_pack_acquisition"
    assert report["lease_expires_at_unix_ms"] is None
    assert (attempt / "storage-link-preflight.json").stat().st_mode & 0o777 == 0o600


def test_slow_storage_fails_maximum_lease_before_snapshot_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "preparation"
    workspace.mkdir(mode=0o700)
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")
    authorization = _authorization()
    authorization["stages"] = [
        {"node_id": "node-0", "assignment_artifact_bytes": 2_000_000_000},
        {"node_id": "node-1", "assignment_artifact_bytes": 2_000_000_000},
    ]
    topology = {
        "nodes": [
            {"node_id": "node-0", "process_transport": "local"},
            {"node_id": "node-1", "process_transport": "ssh"},
        ]
    }
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace
    preparer._template = template
    preparer._execution_topology = None
    preparer._artifact_store = SimpleNamespace(
        root=tmp_path,
        ledger=lambda: {"history": []},
    )
    preparer._clock = lambda: 5_000
    preparer._artifact_transfer_rate_cap = None
    preparer._storage_link_probe = lambda _root: {
        "probe_bytes": 8 * 1024 * 1024,
        "write_bytes_per_second": 500_000,
        "read_bytes_per_second": 500_000,
    }
    preparer._snapshot = lambda *_args: pytest.fail(
        "snapshot scan ran before maximum-lease rejection"
    )
    monkeypatch.setattr(
        "mycelium_live.local_preparer.shutil.disk_usage",
        lambda _root: SimpleNamespace(free=10_000_000_000),
    )
    monkeypatch.setattr(
        "mycelium_live.local_preparer._topology_from_template",
        lambda *_args, **_kwargs: topology,
    )

    with pytest.raises(
        ModelPreparationError,
        match="^artifact_transfer_exceeds_maximum_lease$",
    ):
        preparer(authorization, lambda *_args: None)

    report_path = next(workspace.glob("*/storage-link-preflight.json"))
    report = json.loads(report_path.read_text())
    assert report["state"] == "failed"
    assert report["blocker"] == "artifact_transfer_exceeds_maximum_lease"
    assert "faster owner-approved storage link" in report["recommended_recovery"]


def test_preparation_resumes_acquired_build_after_staging_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "preparation"
    candidates = tmp_path / "candidates"
    workspace.mkdir(mode=0o700)
    candidates.mkdir(mode=0o700)
    template = tmp_path / "template.json"
    template.write_text("{}", encoding="utf-8")
    authorization = _authorization(valid_until=20_000)
    authorization.update(
        {
            "protocol": "mycelium.model_preparation_authorization.v2",
            "authorized_at_unix_ms": 1_000,
            "source_artifact_digest": _digest("8"),
            "quantizer": "mycelium.rowwise_symmetric_int8.v1",
            "evidence_generation": 7,
            "catalog_generation": 9,
            "operation_digest": _digest("6"),
            "stages": [
                {
                    "stage_index": index,
                    "node_id": f"node-{index}",
                    "start_layer": index,
                    "end_layer_exclusive": index + 1,
                    "backend": "mlx",
                    "decode_mode": "complete_context_replay",
                    "assignment_files": [f"stage-{index}.safetensors"],
                    "assignment_artifact_bytes": 10,
                }
                for index in range(2)
            ],
        }
    )
    authorization["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": authorization["feasibility_digest"],
            "owner_decision_digest": authorization["owner_decision_digest"],
            "quantizer": authorization["quantizer"],
            "representation_digest": authorization["representation_digest"],
            "source_artifact_digest": authorization["source_artifact_digest"],
        }
    )
    topology = {
        "protocol": "mycelium.qwen_live_topology.v1",
        "nodes": [
            {"node_id": "node-0", "process_transport": "local"},
            {"node_id": "node-1", "process_transport": "ssh"},
        ],
    }
    monkeypatch.setattr(
        "mycelium_live.local_preparer._topology_from_template",
        lambda *_args, **_kwargs: topology,
    )
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace
    preparer._workspace_identity = _directory_identity(workspace)
    preparer._temporary_root = None
    preparer._temporary_root_identity = None
    preparer._template = template
    preparer._execution_topology = None
    preparer._candidates = candidates
    preparer._python = "/usr/bin/python3"
    preparer._preflight_local_acquisition_storage = lambda *_args, **_kwargs: None
    preparer._snapshot = lambda *_args: (tmp_path, "bfloat16")
    preparer._reusable_stage_sharded_root = lambda *_args: None
    build_calls = []
    staging_calls = []

    def execute(command, code, *, diagnostic_path):
        del diagnostic_path
        if "scripts/build_qwen_live_route.py" in command:
            build_calls.append(command)
            output_root = Path(command[command.index("--output-root") + 1])
            bundle = output_root / "transfer-bundle"
            control = bundle / "control"
            control.mkdir(parents=True)
            frozen = json.loads(
                Path(
                    command[command.index("--model-preparation-authorization") + 1]
                ).read_text("utf-8")
            )
            (control / "model-preparation-authorization.json").write_text(
                json.dumps(frozen), encoding="utf-8"
            )
            generated = output_root / "operator-plan.json"
            generated.write_text(
                json.dumps(
                    {
                        "controller": {
                            "source_root": str(bundle),
                            "run_plan": {"deployment_id": "candidate-1"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (output_root / "build-report.json").write_text(
                json.dumps(
                    {
                        "model_id": frozen["model_id"],
                        "resolved_commit": frozen["revision"],
                        "source_artifact_digest": frozen["source_artifact_digest"],
                        "representation_digest": frozen["representation_digest"],
                        "quantizer": frozen["quantizer"],
                        "owner_decision_digest": frozen["owner_decision_digest"],
                        "layer_ranges": [
                            {
                                "start_layer": stage["start_layer"],
                                "end_layer_exclusive": stage["end_layer_exclusive"],
                                "layer_count": stage["end_layer_exclusive"]
                                - stage["start_layer"],
                            }
                            for stage in frozen["stages"]
                        ],
                        "transfer_bytes": 20,
                    }
                ),
                encoding="utf-8",
            )
            (output_root / "local-challenge-checkpoint.json").write_text(
                json.dumps(
                    {
                        "protocol": "mycelium.local_candidate_challenge.v1",
                        "state": "passed",
                        "deployment_id": "candidate-1",
                        "model_id": frozen["model_id"],
                        "resolved_commit": frozen["revision"],
                        "source_artifact_digest": frozen["source_artifact_digest"],
                        "representation_digest": frozen["representation_digest"],
                        "preparation_binding_digest": frozen[
                            "preparation_binding_digest"
                        ],
                        "challenge_output_token_ids": [11, 12],
                        "assignments": [
                            {"node_id": stage["node_id"]} for stage in frozen["stages"]
                        ],
                    }
                ),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(
                command, 0, json.dumps({"operator_plan": str(generated)}), ""
            )
        assert code == "model_candidate_staging_failed"
        staging_calls.append(command)
        if len(staging_calls) == 1:
            raise ModelPreparationError("model_candidate_staging_failed")
        return subprocess.CompletedProcess(command, 0, "", "")

    preparer._execute = execute
    bind_calls = []

    def bind(generated, *, attempt):
        bind_calls.append(generated)
        path = attempt / f"bound-operator-plan-{len(bind_calls)}.json"
        plan = {
            "controller": {
                "source_root": str(generated.parent / "transfer-bundle"),
                "prepositioned_artifacts": {
                    "protocol": "mycelium.controller_prepositioned_artifacts.v1"
                },
                "run_plan": {"deployment_id": "candidate-1"},
            }
        }
        _write_durable_private_document(path, plan)
        return path, plan

    preparer._bind_candidate_to_seed = bind
    receipt = {"state": "ready", "acquisition_id": "acquisition-1"}
    acquisition_calls = []

    def acquire(**kwargs):
        acquisition_calls.append(kwargs)
        return (receipt,)

    preparer._acquire_stage_packs = acquire
    preparer._artifact_store = SimpleNamespace(
        ledger=lambda: {"history": [receipt]}
    )

    with pytest.raises(
        ModelPreparationError, match="model_candidate_staging_failed"
    ):
        preparer(authorization, lambda *_args: None)
    checkpoint_path = next(workspace.glob("*/phase-checkpoint.json"))
    assert _read_private_document(checkpoint_path)["completed_phase"] == (
        "artifacts_acquired"
    )

    refreshed = deepcopy(authorization)
    refreshed["authorized_at_unix_ms"] = 2_000
    refreshed["feasibility_digest"] = _digest("9")
    refreshed["evidence_generation"] = 8
    refreshed["operation_digest"] = _digest("7")
    refreshed["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": refreshed["feasibility_digest"],
            "owner_decision_digest": refreshed["owner_decision_digest"],
            "quantizer": refreshed["quantizer"],
            "representation_digest": refreshed["representation_digest"],
            "source_artifact_digest": refreshed["source_artifact_digest"],
        }
    )
    preparer._clock = lambda: 2_100
    result = preparer(refreshed, lambda *_args: None)

    assert result.candidate_id == "candidate-1"
    assert len(build_calls) == 1
    assert len(bind_calls) == 4
    assert len(acquisition_calls) == 1
    assert len(staging_calls) == 2
    assert bind_calls[2].name == "bound-operator-plan-2.json"
    assert bind_calls[3].name == "bound-operator-plan-3.json"
    assert _read_private_document(checkpoint_path)["completed_phase"] == (
        "candidate_published"
    )
    recovery_paths = list(checkpoint_path.parent.glob("recovery-authorization-*.json"))
    assert len(recovery_paths) == 1
    recovery = _read_private_document(recovery_paths[0])
    assert recovery["protocol"] == (
        "mycelium.private_model_preparation_recovery_authorization.v1"
    )
    assert (
        recovery["source_preparation_binding_digest"]
        == authorization["preparation_binding_digest"]
    )
    assert recovery["source_authority_digest"] == canonical_digest(
        recovery["source_authority"]
    )
    assert (
        recovery["recovery_authority"]["feasibility_digest"]
        == refreshed["feasibility_digest"]
    )
    assert recovery["source_completed_phase"] == "artifacts_acquired"
    assert recovery["resume_from_phase"] == "artifacts_acquired"


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


def test_explicit_execution_topology_binds_dynamic_planner_order() -> None:
    document = {
        "protocol": "mycelium.qwen_live_topology.v1",
        "nodes": [
            {
                "node_id": node_id,
                "process_transport": "local" if index == 0 else "ssh",
                "ssh_target": "local" if index == 0 else f"user@host-{index}",
                "ssh_identity_file": None if index == 0 else f"/keys/{index}",
                "staging_root": f"/srv/{node_id}",
                "python_executable": "/usr/bin/python3",
                "sidecar_binary": "/bin/sidecar",
                "endpoint_secret_file": f"/keys/{node_id}.key",
                "endpoint_id": f"endpoint-{index}",
                "membership_generation": index + 1,
                "runtime_backend": backend,
            }
            for index, (node_id, backend) in enumerate(
                (("node-0", "mlx"), ("node-2", "numpy"), ("reviewer", "mlx"))
            )
        ],
    }
    authorization = {
        "stages": [
            {"node_id": "node-0", "backend": "mlx"},
            {"node_id": "node-2", "backend": "numpy"},
            {"node_id": "reviewer", "backend": "mlx"},
        ]
    }

    topology = _topology_from_operation_file(
        document, authorization, route_label="modelprep-abc123"
    )

    assert topology["placement_order_authority"] == "m14_measured_cycle"
    assert [node["node_id"] for node in topology["nodes"]] == [
        "node-0",
        "node-2",
        "reviewer",
    ]
    assert topology["nodes"][2]["staging_root"].endswith("-candidate-modelprep-abc123")
    drift = deepcopy(authorization)
    drift["stages"][1]["backend"] = "mlx"
    with pytest.raises(ModelPreparationError, match="topology_invalid"):
        _topology_from_operation_file(document, drift, route_label="modelprep-abc123")


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


def test_warm_receipts_require_new_exact_zero_transfer_records() -> None:
    receipts = []
    for index in range(2):
        receipt = _ready_status(
            {
                "recipient_member_id": f"node-{index}",
                "manifest_digest": _digest(str(index + 1)),
                "assignment_digest": _digest(str(index + 3)),
                "representation_digest": _digest("8"),
            }
        )
        receipt.update(
            {
                "acquisition_id": f"warm-{index}",
                "total_bytes": 10,
                "cached_verified_bytes": 10,
                "transferred_verified_bytes": 0,
                "duplicate_bytes_prevented": 10,
                "sources": [],
            }
        )
        receipts.append(receipt)

    totals = LocalCandidatePreparer._warm_receipt_totals(
        tuple(receipts),
        expected_stage_count=2,
        prior_acquisition_ids={"cold-0", "cold-1"},
    )

    assert totals == {"cached_verified_bytes": 20, "total_bytes": 20}
    drift = deepcopy(receipts)
    drift[1]["cached_verified_bytes"] = 9
    drift[1]["transferred_verified_bytes"] = 1
    drift[1]["sources"] = [
        {"source_ref": "source-000000000001", "state": "rotated", "verified_bytes": 1}
    ]
    with pytest.raises(
        ModelPreparationError, match="warm_reacquisition_not_zero_transfer"
    ):
        LocalCandidatePreparer._warm_receipt_totals(
            tuple(drift),
            expected_stage_count=2,
            prior_acquisition_ids=set(),
        )
    with pytest.raises(
        ModelPreparationError, match="warm_reacquisition_not_zero_transfer"
    ):
        LocalCandidatePreparer._warm_receipt_totals(
            tuple(receipts),
            expected_stage_count=2,
            prior_acquisition_ids={"warm-0"},
        )


def test_warm_candidate_keeps_build_authority_but_requires_fresh_exact_placement(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "preparation"
    candidates = tmp_path / "candidates"
    output = workspace / "attempt" / "candidate"
    bundle = output / "transfer-bundle"
    control = bundle / "control"
    deployment = bundle / "deployment"
    control.mkdir(parents=True)
    deployment.mkdir()
    candidates.mkdir(mode=0o700)
    candidate_id = "candidate-1"
    frozen = _authorization(valid_until=20_000)
    frozen.update(
        {
            "protocol": "mycelium.model_preparation_authorization.v2",
            "authorized_at_unix_ms": 1_000,
            "source_artifact_digest": _digest("8"),
            "quantizer": "mycelium.rowwise_symmetric_int8.v1",
            "catalog_generation": 1,
            "operation_digest": _digest("6"),
            "evidence_generation": 2,
            "stages": [
                {
                    "stage_index": index,
                    "node_id": f"node-{index}",
                    "start_layer": index * 2,
                    "end_layer_exclusive": (index + 1) * 2,
                    "backend": "mlx",
                    "decode_mode": "complete_context_replay",
                    "assignment_files": [f"stage-{index}.safetensors"],
                    "assignment_artifact_bytes": 10,
                }
                for index in range(2)
            ],
        }
    )
    frozen["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": frozen["feasibility_digest"],
            "owner_decision_digest": frozen["owner_decision_digest"],
            "quantizer": frozen["quantizer"],
            "representation_digest": frozen["representation_digest"],
            "source_artifact_digest": frozen["source_artifact_digest"],
        }
    )
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(frozen), encoding="utf-8"
    )
    plan = {
        "controller": {
            "source_root": str(bundle),
            "run_plan": {"deployment_id": candidate_id},
        }
    }
    (output / "operator-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output / "build-report.json").write_text(
        json.dumps(
            {
                "model_id": frozen["model_id"],
                "resolved_commit": frozen["revision"],
                "representation_digest": frozen["representation_digest"],
                "source_artifact_digest": frozen["source_artifact_digest"],
                "quantizer": frozen["quantizer"],
                "owner_decision_digest": frozen["owner_decision_digest"],
                "layer_ranges": [
                    {
                        "start_layer": stage["start_layer"],
                        "end_layer_exclusive": stage["end_layer_exclusive"],
                        "layer_count": stage["end_layer_exclusive"]
                        - stage["start_layer"],
                    }
                    for stage in frozen["stages"]
                ],
                "transfer_bytes": 20,
            }
        ),
        encoding="utf-8",
    )
    (output / "local-challenge-checkpoint.json").write_text(
        json.dumps(
            {
                "protocol": "mycelium.local_candidate_challenge.v1",
                "state": "passed",
                "model_id": frozen["model_id"],
                "resolved_commit": frozen["revision"],
                "deployment_id": candidate_id,
                "source_artifact_digest": frozen["source_artifact_digest"],
                "representation_digest": frozen["representation_digest"],
                "preparation_binding_digest": frozen["preparation_binding_digest"],
                "challenge_output_token_ids": [11, 12],
                "assignments": [
                    {"node_id": stage["node_id"]} for stage in frozen["stages"]
                ],
            }
        ),
        encoding="utf-8",
    )
    published = candidates / f"{candidate_id}.json"
    published.write_text(json.dumps(plan), encoding="utf-8")
    published.chmod(0o600)
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._workspace = workspace
    preparer._candidates = candidates

    refreshed = deepcopy(frozen)
    refreshed["authorized_at_unix_ms"] = 10_000
    refreshed["feasibility_digest"] = _digest("9")
    refreshed["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": refreshed["feasibility_digest"],
            "owner_decision_digest": refreshed["owner_decision_digest"],
            "quantizer": refreshed["quantizer"],
            "representation_digest": refreshed["representation_digest"],
            "source_artifact_digest": refreshed["source_artifact_digest"],
        }
    )

    generated, _output, report, original, _evidence = preparer._verified_candidate(
        candidate_id, refreshed
    )
    assert generated == output / "operator-plan.json"
    assert report["transfer_bytes"] == 20
    assert original["feasibility_digest"] == frozen["feasibility_digest"]
    assert refreshed["feasibility_digest"] != original["feasibility_digest"]

    drift = deepcopy(refreshed)
    drift["stages"][1]["start_layer"] = 3
    with pytest.raises(
        ModelPreparationError, match="verified_candidate_authority_mismatch"
    ):
        preparer._verified_candidate(candidate_id, drift)


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
    authorization["stages"] = [{"assignment_artifact_bytes": 10}]
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(authorization), encoding="utf-8"
    )
    (bundle / "stage-pack.json").write_text(
        '{"assignment_id":"assignment-a","node_id":"node-0"}',
        encoding="utf-8",
    )
    captured: list[tuple[int, int, Path, bool]] = []

    def build_source(**kwargs):
        captured.append(
            (
                kwargs["issued_at_unix_ms"],
                kwargs["expires_at_unix_ms"],
                kwargs["output_root"],
                kwargs["resume_existing"],
            )
        )
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
    interrupted = attempt / "artifact-sources-interrupted"
    interrupted.mkdir(mode=0o700)
    (interrupted / "node-0").mkdir(mode=0o700)
    _write_storage_preflight(attempt, authorization)
    preparer._acquire_stage_packs(
        attempt=attempt,
        authorization=authorization,
        plan=plan,
    )

    assert captured == [
        (9_000, 909_001, attempt / "artifact-sources" / "node-0", True)
    ]

    ambiguous_attempt = tmp_path / "ambiguous-attempt"
    ambiguous_attempt.mkdir()
    (ambiguous_attempt / "artifact-sources-one").mkdir(mode=0o700)
    (ambiguous_attempt / "artifact-sources-two").mkdir(mode=0o700)
    _write_storage_preflight(ambiguous_attempt, authorization)
    with pytest.raises(
        ModelPreparationError, match="stage_pack_source_checkpoint_ambiguous"
    ):
        preparer._acquire_stage_packs(
            attempt=ambiguous_attempt,
            authorization=authorization,
            plan=plan,
        )


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
            authorization={
                "protocol": "mycelium.model_preparation_authorization.v1",
                "evidence_valid_until_unix_ms": 9_000,
            },
            plan=plan,
        )


def test_stage_acquisition_accepts_v2_authority_fresh_at_preparation_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._clock = lambda: 20_000
    captured: list[int] = []

    def reached_validation(_bundle, _authorization) -> None:
        captured.append(1)
        raise RuntimeError("reached_authorization_validation")

    monkeypatch.setattr(
        "mycelium_live.local_preparer._validate_preparation_authorization",
        reached_validation,
    )
    preparer._artifact_store = object()
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

    authorization = {
        "protocol": "mycelium.model_preparation_authorization.v2",
        "authorized_at_unix_ms": 8_000,
        "evidence_valid_until_unix_ms": 9_000,
        "stages": [{"assignment_artifact_bytes": 10}],
    }
    _write_storage_preflight(attempt, authorization)

    with pytest.raises(RuntimeError, match="reached_authorization_validation"):
        preparer._acquire_stage_packs(
            attempt=attempt,
            authorization=authorization,
            plan=plan,
        )

    assert captured == [1]


def test_stage_acquisition_rejects_v2_authority_started_after_expiry(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    preparer = object.__new__(LocalCandidatePreparer)
    preparer._clock = lambda: 20_000
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
            authorization={
                "protocol": "mycelium.model_preparation_authorization.v2",
                "authorized_at_unix_ms": 9_001,
                "evidence_valid_until_unix_ms": 9_000,
            },
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


def test_v2_preparation_authorization_freezes_source_artifact_and_quantizer(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "bundle"
    control = bundle / "control"
    control.mkdir(parents=True)
    frozen = _authorization()
    frozen.update(
        {
            "protocol": "mycelium.model_preparation_authorization.v2",
            "authorized_at_unix_ms": 9_000,
            "source_artifact_digest": _digest("8"),
            "quantizer": "mycelium.rowwise_symmetric_int8.v1",
        }
    )
    frozen["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": frozen["feasibility_digest"],
            "owner_decision_digest": frozen["owner_decision_digest"],
            "quantizer": frozen["quantizer"],
            "representation_digest": frozen["representation_digest"],
            "source_artifact_digest": frozen["source_artifact_digest"],
        }
    )
    (control / "model-preparation-authorization.json").write_text(
        json.dumps(frozen), encoding="utf-8"
    )

    _validate_preparation_authorization(bundle, frozen)

    drift = deepcopy(frozen)
    drift["source_artifact_digest"] = _digest("9")
    drift["preparation_binding_digest"] = canonical_digest(
        {
            "feasibility_digest": drift["feasibility_digest"],
            "owner_decision_digest": drift["owner_decision_digest"],
            "quantizer": drift["quantizer"],
            "representation_digest": drift["representation_digest"],
            "source_artifact_digest": drift["source_artifact_digest"],
        }
    )
    with pytest.raises(ModelPreparationError, match="model_representation_drift"):
        _validate_preparation_authorization(bundle, drift)
