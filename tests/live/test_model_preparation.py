from __future__ import annotations

import threading

import pytest

from mycelium_live.preparation import (
    LocalModelPreparation,
    ModelPreparationError,
    PreparationResult,
)


MODEL_ID = "Qwen/Qwen3-8B"
REVISION = "b" * 40


def _operation(*, valid_until: int = 2_000, state: str = "feasible") -> dict:
    return {
        "protocol": "mycelium.model_operation.v1",
        "operation_digest": "sha256:" + "a" * 64,
        "catalog_generation": 9,
        "entries": [
            {
                "model_id": MODEL_ID,
                "revision": REVISION,
                "state": "compatible",
            }
        ],
        "feasibility_reports": [
            {
                "protocol": "mycelium.model_feasibility.v1",
                "model_id": MODEL_ID,
                "revision": REVISION,
                "state": state,
                "provisioning_authorized": state == "feasible",
                "evidence_valid_until_unix_ms": valid_until,
                "evidence_generation": 8,
                "feasibility_digest": "sha256:" + "c" * 64,
                "representation_digest": "sha256:" + "d" * 64,
                "source_quantization": "bfloat16",
                "serving_dtype": "float32",
                "serving_quantization": "int8-weight-only",
                "stages": [
                    {
                        "stage_index": 0,
                        "node_id": "node-0",
                        "start_layer": 0,
                        "end_layer_exclusive": 30,
                        "backend": "mlx",
                        "decode_mode": "complete_context_replay",
                        "assignment_files": ["config.json", "model-1.safetensors"],
                        "assignment_artifact_bytes": 100,
                    },
                    {
                        "stage_index": 1,
                        "node_id": "node-1",
                        "start_layer": 30,
                        "end_layer_exclusive": 36,
                        "backend": "numpy",
                        "decode_mode": "complete_context_replay",
                        "assignment_files": ["config.json", "model-2.safetensors"],
                        "assignment_artifact_bytes": 40,
                    },
                ],
            }
        ],
    }


def _decision(**changes: object) -> dict:
    decision = {
        "protocol": "mycelium.model_representation_decision.v1",
        "model_id": MODEL_ID,
        "revision": REVISION,
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": "int8-weight-only",
        "representation_digest": "sha256:" + "d" * 64,
        "conversion_authorized": True,
    }
    decision.update(changes)
    return decision


def test_preparation_freezes_fresh_authority_and_publishes_only_after_success() -> None:
    entered = threading.Event()
    release = threading.Event()
    published: list[bool] = []
    captured = []

    def prepare(authorization, progress):
        captured.append(authorization)
        progress("compiling_assignments", None, None)
        entered.set()
        assert release.wait(timeout=5)
        progress("staging_peers", 140, 140)
        progress("publishing_candidate", 140, 140)
        return PreparationResult("candidate-1", 2, 140, 140)

    service = LocalModelPreparation(
        operation_source=_operation,
        preparer=prepare,
        on_candidate_published=lambda: published.append(True),
        clock_unix_ms=lambda: 1_000,
    )
    started = service.start(_decision())
    assert started["state"] == "preparing"
    assert started["transfer_bytes"] == 140
    assert started["download_authorized"] is False
    assert entered.wait(timeout=5)
    assert published == []
    assert service.start(_decision())["state"] == "preparing"
    with pytest.raises(ModelPreparationError, match="model_preparation_busy"):
        service.start(_decision(model_id="Qwen/other"))
    release.set()
    service.close()

    status = service.status()
    assert status["state"] == "succeeded"
    assert status["candidate_id"] == "candidate-1"
    assert status["verified_bytes"] == 140
    assert status["activation_started"] is False
    assert published == [True]
    assert captured[0]["download_authorized"] is False
    assert captured[0]["owner_decision_digest"].startswith("sha256:")
    assert captured[0]["preparation_binding_digest"].startswith("sha256:")
    assert [
        (item["start_layer"], item["end_layer_exclusive"])
        for item in captured[0]["stages"]
    ] == [(0, 30), (30, 36)]


def test_preparation_preserves_approved_immutable_representation_authority() -> None:
    operation = _operation()
    original_owner_decision_digest = "sha256:" + "e" * 64
    operation["feasibility_reports"][0]["representation_authority"] = {
        "kind": "approved_existing_immutable_representation",
        "owner_decision_digest": original_owner_decision_digest,
        "prior_feasibility_digest": "sha256:" + "f" * 64,
    }
    captured = []
    service = LocalModelPreparation(
        operation_source=lambda: operation,
        preparer=lambda authorization, _progress: (
            captured.append(authorization)
            or PreparationResult("candidate-1", 2, 140, 140)
        ),
        clock_unix_ms=lambda: 1_000,
    )

    service.start(_decision())
    service.close()

    assert captured[0]["owner_decision_digest"] == original_owner_decision_digest
    assert captured[0]["feasibility_digest"] == "sha256:" + "c" * 64


def test_preparation_rejects_malformed_inherited_representation_authority() -> None:
    operation = _operation()
    operation["feasibility_reports"][0]["representation_authority"] = {
        "kind": "approved_existing_immutable_representation",
        "owner_decision_digest": "not-a-digest",
    }
    service = LocalModelPreparation(
        operation_source=lambda: operation,
        preparer=lambda *_args: None,  # type: ignore[arg-type,return-value]
        clock_unix_ms=lambda: 1_000,
    )

    with pytest.raises(ModelPreparationError, match="model_operation_invalid"):
        service.start(_decision())


@pytest.mark.parametrize(
    ("operation", "reason"),
    [
        (_operation(valid_until=999), "model_capacity_stale"),
        (_operation(state="infeasible"), "model_does_not_fit"),
    ],
)
def test_preparation_rejects_before_preparer(operation: dict, reason: str) -> None:
    called = []
    service = LocalModelPreparation(
        operation_source=lambda: operation,
        preparer=lambda *_args: called.append(True),  # type: ignore[arg-type,return-value]
        clock_unix_ms=lambda: 1_000,
    )
    with pytest.raises(ModelPreparationError, match=reason):
        service.start(_decision())
    assert called == []
    assert service.status()["state"] == "idle"


@pytest.mark.parametrize(
    "field",
    [
        "representation_digest",
        "source_quantization",
        "serving_dtype",
        "serving_quantization",
    ],
)
def test_preparation_rejects_unbound_serving_representation(field: str) -> None:
    operation = _operation()
    del operation["feasibility_reports"][0][field]
    service = LocalModelPreparation(
        operation_source=lambda: operation,
        preparer=lambda *_args: None,  # type: ignore[arg-type,return-value]
        clock_unix_ms=lambda: 1_000,
    )

    with pytest.raises(ModelPreparationError, match="model_operation_invalid"):
        service.start(_decision())

    assert service.status()["state"] == "idle"


@pytest.mark.parametrize(
    ("decision", "reason"),
    [
        (
            _decision(conversion_authorized=False),
            "model_representation_conversion_not_authorized",
        ),
        (
            _decision(representation_digest="sha256:" + "e" * 64),
            "model_representation_decision_mismatch",
        ),
        (
            {
                key: value
                for key, value in _decision().items()
                if key != "conversion_authorized"
            },
            "model_representation_decision_invalid",
        ),
        ({**_decision(), "extra": True}, "model_representation_decision_invalid"),
    ],
)
def test_preparation_requires_exact_owner_representation_decision(
    decision: dict, reason: str
) -> None:
    called = []
    service = LocalModelPreparation(
        operation_source=_operation,
        preparer=lambda *_args: called.append(True),  # type: ignore[arg-type,return-value]
        clock_unix_ms=lambda: 1_000,
    )
    with pytest.raises(ModelPreparationError, match=reason):
        service.start(decision)
    assert called == []


def test_preparation_failure_is_bounded_and_does_not_publish() -> None:
    published = []

    def fail(_authorization, _progress):
        raise RuntimeError("model_candidate_staging_failed:private detail")

    service = LocalModelPreparation(
        operation_source=_operation,
        preparer=fail,
        on_candidate_published=lambda: published.append(True),
        clock_unix_ms=lambda: 1_000,
    )
    service.start(_decision())
    service.close()
    assert service.status()["state"] == "failed"
    assert service.status()["reason_code"] == "model_candidate_staging_failed"
    assert published == []
