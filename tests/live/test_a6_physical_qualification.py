# SPDX-License-Identifier: AGPL-3.0-only
"""A6 exact successor qualification for the ordinary physical serve."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from mycelium_m16_runtime import path_manifest_document_digest
from mycelium_a6_gateway import CommittedDeliverySnapshot, freeze_replay_checkpoint
from mycelium_live.a6_recovery import LiveA6RecoveryQualifier, tokenizer_artifact_digest
from mycelium_router.contracts import PathHop, PathManifest


_DIGEST = "sha256:" + "a" * 64


def _checkpoint():
    return freeze_replay_checkpoint(
        CommittedDeliverySnapshot(
            request_id="request-a6-physical",
            encoded_prompt_token_ids=(1, 2, 3),
            generation_settings={
                "protocol": "mycelium.request_gateway.v2",
                "max_new_tokens": 8,
                "workload_profile_id": "interactive_chat_v1",
                "qos_class": "interactive",
            },
            committed_token_ids=(4, 5),
            delivery_cursor=2,
            publisher_generation=1,
            session_id="session-a6-physical",
            source_attempt=1,
            path_id="source-path",
            path_digest="sha256:" + "b" * 64,
            path_attempt=0,
            deployment_id="deployment-a6",
            deployment_epoch=7,
            topology_generation=11,
            source_qualification_digest="sha256:" + "c" * 64,
            model_id="model-a6",
            representation_digest="sha256:" + "d" * 64,
            tokenizer_digest="sha256:" + "e" * 64,
        ),
        store_identity="test-a6-physical",
        created_at_unix_ms=1_000,
        expiry_unix_ms=60_000,
    )


def _manifest() -> PathManifest:
    return PathManifest(
        path_id="successor-path",
        path_attempt=1,
        request_id="request-a6-physical",
        deployment_id="deployment-a6",
        deployment_epoch=7,
        topology_version=11,
        manifest_digest="sha256:" + "f" * 64,
        ordered_hops=(PathHop("stage-0", "successor-placement", "reservation-1", 1e15),),
        loopback_edge_id="loopback-0",
    )


def test_tokenizer_digest_binds_names_and_exact_bytes(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "config.json").write_text('{"model_type":"qwen2"}\n')
    (deployment / "tokenizer.json").write_bytes(b'{"version":"1.0"}\n')
    first = tokenizer_artifact_digest(deployment)
    (deployment / "tokenizer.json").write_bytes(b'{"version":"1.1"}\n')
    second = tokenizer_artifact_digest(deployment)
    assert first.startswith("sha256:")
    assert first != second


def test_qualifier_binds_exact_reserved_path_and_derived_physical_challenge(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "config.json").write_text('{"model_type":"qwen2","hidden_size":16}\n')
    (deployment / "tokenizer.json").write_text('{"version":"1.0","added_tokens":[]}\n')
    checkpoint = _checkpoint()
    observed: dict[str, object] = {}

    def challenge(*, request_id, placement_ids, absolute_deadline_unix_ms):
        observed.update(
            request_id=request_id,
            placement_ids=placement_ids,
            absolute_deadline_unix_ms=absolute_deadline_unix_ms,
        )
        return {
            "passed": True,
            "parity_policy_digest": "sha256:" + "1" * 64,
            "workload_envelope_digest": "sha256:" + "2" * 64,
        }

    graph = SimpleNamespace(
        deployment_id="deployment-a6",
        deployment_epoch=7,
        topology_version=11,
        model_id="model-a6",
        resolved_commit="immutable-model-revision",
        manifest_digest=checkpoint.representation_digest,
    )
    qualifier = LiveA6RecoveryQualifier(
        graph=graph,
        deployment_dir=deployment,
        tokenizer_digest=checkpoint.tokenizer_digest,
        physical_challenge=challenge,
        clock_ms=lambda: 2_000,
    )
    source, qualification = qualifier(
        checkpoint.request_id,
        checkpoint,
        {
            "request_id": checkpoint.request_id,
            "request_attempt": 2,
            "path_id": "successor-path",
            "path_attempt": 1,
            "path_manifest_digest": path_manifest_document_digest(_manifest()),
            "deployment_id": "deployment-a6",
            "deployment_epoch": 7,
            "topology_generation": 11,
        },
        _manifest(),
    )

    assert observed == {
        "request_id": checkpoint.request_id,
        "placement_ids": ("successor-placement",),
        "absolute_deadline_unix_ms": checkpoint.expiry_unix_ms,
    }
    assert source == qualification["compatibility"]
    assert qualification["physical_challenge"] == "passed"
    assert qualification["compatible"] is True
    assert qualification["request_binding"]["checkpoint_digest"] == checkpoint.checkpoint_digest()
    assert qualification["successor_binding"]["path_id"] == "successor-path"
    assert qualification["successor_binding"]["placement_ids"] == ["successor-placement"]


def test_qualifier_never_turns_failed_challenge_into_eligibility(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "config.json").write_text('{"model_type":"qwen2"}\n')
    (deployment / "tokenizer.json").write_text('{"version":"1.0"}\n')
    checkpoint = _checkpoint()
    graph = SimpleNamespace(
        deployment_id="deployment-a6",
        deployment_epoch=7,
        topology_version=11,
        model_id="model-a6",
        resolved_commit="immutable-model-revision",
        manifest_digest=checkpoint.representation_digest,
    )
    qualifier = LiveA6RecoveryQualifier(
        graph=graph,
        deployment_dir=deployment,
        tokenizer_digest=checkpoint.tokenizer_digest,
        physical_challenge=lambda **_kwargs: {
            "passed": False,
            "parity_policy_digest": "sha256:" + "1" * 64,
            "workload_envelope_digest": "sha256:" + "2" * 64,
        },
        clock_ms=lambda: 2_000,
    )
    _source, qualification = qualifier(
        checkpoint.request_id,
        checkpoint,
        {
            "request_id": checkpoint.request_id,
            "request_attempt": 2,
            "path_id": "successor-path",
            "path_attempt": 1,
            "path_manifest_digest": path_manifest_document_digest(_manifest()),
            "deployment_id": "deployment-a6",
            "deployment_epoch": 7,
            "topology_generation": 11,
        },
        _manifest(),
    )
    assert qualification["physical_challenge"] == "failed"
    assert qualification["compatible"] is False
    assert qualification["reason_codes"] == ["successor_physical_challenge_failed"]


def test_qualifier_rejects_foreign_successor_identity_before_challenge(tmp_path: Path) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    (deployment / "config.json").write_text('{"model_type":"qwen2"}\n')
    (deployment / "tokenizer.json").write_text('{"version":"1.0"}\n')
    checkpoint = _checkpoint()
    qualifier = LiveA6RecoveryQualifier(
        graph=SimpleNamespace(
            deployment_id="deployment-a6",
            deployment_epoch=7,
            topology_version=11,
            model_id="model-a6",
            resolved_commit="immutable-model-revision",
            manifest_digest=checkpoint.representation_digest,
        ),
        deployment_dir=deployment,
        tokenizer_digest=checkpoint.tokenizer_digest,
        physical_challenge=lambda **_kwargs: pytest.fail("challenge must not run"),
        clock_ms=lambda: 2_000,
    )
    with pytest.raises(ValueError, match="successor_identity_mismatch"):
        qualifier(
            checkpoint.request_id,
            checkpoint,
            {
                "request_id": "foreign-request",
                "request_attempt": 2,
                "path_id": "successor-path",
                "path_attempt": 1,
                "path_manifest_digest": path_manifest_document_digest(_manifest()),
                "deployment_id": "deployment-a6",
                "deployment_epoch": 7,
                "topology_generation": 11,
            },
            _manifest(),
        )
