from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import threading
import time

import pytest

from mycelium_live.artifact_provisioner import (
    ArtifactAcquisitionStore,
    ArtifactProvisioningError,
    SwarmArtifactProvisioner,
)
from mycelium_swarm_artifacts import (
    MANIFEST_PROTOCOL,
    POLICY_PROTOCOL,
    canonical_digest,
    merkle_proofs,
    merkle_root,
)


REVISION = "a" * 40
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64
DIGEST_D = "sha256:" + "d" * 64
CHUNK_SIZE = 65_536


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _pack() -> tuple[bytes, dict, dict]:
    payload = b"a" * CHUNK_SIZE + b"b" * CHUNK_SIZE + b"tail"
    contents = [
        payload[:CHUNK_SIZE],
        payload[CHUNK_SIZE : 2 * CHUNK_SIZE],
        payload[2 * CHUNK_SIZE :],
    ]
    digests = [_digest(content) for content in contents]
    proofs = merkle_proofs(digests)
    manifest = {
        "protocol": MANIFEST_PROTOCOL,
        "manifest_id": "manifest-1",
        "manifest_digest": DIGEST_A,
        "model_id": "Qwen/Qwen3-8B",
        "model_revision": REVISION,
        "model_artifact_digest": DIGEST_B,
        "source_quantization": "bfloat16",
        "serving_dtype": "float32",
        "serving_quantization": "bfloat16",
        "representation_digest": DIGEST_C,
        "owner_decision_digest": DIGEST_D,
        "feasibility_digest": DIGEST_A,
        "evidence_generation": 7,
        "assignment_id": "assignment-1",
        "assignment_digest": DIGEST_B,
        "graph_digest": DIGEST_C,
        "recipient_member_id": "member-3",
        "recipient_membership_generation": 9,
        "placement_id": "placement-2",
        "stage_id": "stage-2",
        "layer_start": 24,
        "layer_end_exclusive": 36,
        "component_scope": ["final_norm", "lm_head", "transformer_layers"],
        "tensor_scope_digest": DIGEST_D,
        "pack_format": "mycelium.stage_pack_stream.v1",
        "files": [
            {
                "relative_path": "layers/layers-24-35.safetensors",
                "components": ["transformer_layers"],
                "offset_bytes": 0,
                "size_bytes": CHUNK_SIZE,
                "content_digest": _digest(payload[:CHUNK_SIZE]),
            },
            {
                "relative_path": "final_norm.safetensors",
                "components": ["final_norm"],
                "offset_bytes": CHUNK_SIZE,
                "size_bytes": CHUNK_SIZE,
                "content_digest": _digest(payload[CHUNK_SIZE : 2 * CHUNK_SIZE]),
            },
            {
                "relative_path": "lm_head.safetensors",
                "components": ["lm_head"],
                "offset_bytes": 2 * CHUNK_SIZE,
                "size_bytes": 4,
                "content_digest": _digest(payload[2 * CHUNK_SIZE :]),
            },
        ],
        "stage_pack_digest": _digest(payload),
        "chunk_size_bytes": CHUNK_SIZE,
        "total_size_bytes": len(payload),
        "merkle_root": merkle_root(digests),
        "chunks": [
            {
                "index": index,
                "offset_bytes": index * CHUNK_SIZE,
                "size_bytes": len(content),
                "content_digest": digests[index],
                "merkle_proof": list(proofs[index]),
            }
            for index, content in enumerate(contents)
        ],
        "issued_at_unix_ms": 1_000,
        "expires_at_unix_ms": 2_000,
        "owner_provenance": "owner-approved-exact-representation",
    }
    manifest["manifest_digest"] = canonical_digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"}
    )
    binding_fields = {
        "model_id",
        "model_revision",
        "representation_digest",
        "owner_decision_digest",
        "feasibility_digest",
        "evidence_generation",
        "assignment_id",
        "assignment_digest",
        "graph_digest",
        "recipient_member_id",
        "recipient_membership_generation",
        "placement_id",
        "stage_id",
        "layer_start",
        "layer_end_exclusive",
        "component_scope",
        "tensor_scope_digest",
    }
    binding = {field: copy.deepcopy(manifest[field]) for field in binding_fields}
    return payload, manifest, binding


def _policy(**changes: object) -> dict:
    policy = {
        "protocol": POLICY_PROTOCOL,
        "chunk_size_bytes": CHUNK_SIZE,
        "maximum_sources": 3,
        "per_source_concurrency": 1,
        "aggregate_concurrency": 2,
        "maximum_retries_per_chunk": 0,
        "maximum_source_rotations": 4,
        "partial_state_ttl_seconds": 3_600,
        "disk_reserve_bytes": 0,
        "per_source_bytes_per_second": 10_000_000,
        "aggregate_bytes_per_second": 20_000_000,
        "serving_traffic_reserve_ratio": 0.4,
        "multi_source_threshold_bytes": CHUNK_SIZE,
        "minimum_predicted_improvement_ratio": 0.2,
        "allow_redundant_hedging": False,
        "thermal_classes_allowed": ["fair", "nominal"],
        "power_classes_allowed": ["battery_ok", "external_power"],
    }
    policy.update(changes)
    return policy


def _grant(manifest: dict, **changes: object) -> dict:
    grant = {
        "manifest_digest": manifest["manifest_digest"],
        "assignment_digest": manifest["assignment_digest"],
        "representation_digest": manifest["representation_digest"],
        "feasibility_digest": manifest["feasibility_digest"],
        "recipient_member_id": manifest["recipient_member_id"],
        "recipient_membership_generation": manifest["recipient_membership_generation"],
        "allowed_chunk_digests": sorted(
            chunk["content_digest"] for chunk in manifest["chunks"]
        ),
        "authorized_source_member_ids": ["member-a", "member-b"],
        "origin_fallback_allowed": False,
        "maximum_total_bytes": manifest["total_size_bytes"],
        "maximum_concurrency": 2,
        "maximum_bytes_per_second": 20_000_000,
    }
    grant.update(changes)
    return grant


def _advertisements(manifest: dict) -> list[dict]:
    chunks = [item["content_digest"] for item in manifest["chunks"]]
    return [
        {
            "source_member_id": "member-a",
            "manifest_digest": manifest["manifest_digest"],
            "available_chunk_digests": sorted(chunks[:2]),
            "max_bytes_per_second": 20_000_000,
            "serving_priority": 2,
            "transfer_health": "healthy",
        },
        {
            "source_member_id": "member-b",
            "manifest_digest": manifest["manifest_digest"],
            "available_chunk_digests": sorted(chunks[1:]),
            "max_bytes_per_second": 15_000_000,
            "serving_priority": 1,
            "transfer_health": "healthy",
        },
    ]


def _content(payload: bytes, manifest: dict) -> dict[str, bytes]:
    return {
        item["content_digest"]: payload[
            item["offset_bytes"] : item["offset_bytes"] + item["size_bytes"]
        ]
        for item in manifest["chunks"]
    }


def _reader(contents: dict[str, bytes], calls: list[tuple]) -> object:
    def read(source: str, digest: str, offset: int, length: int, grant: dict):
        assert digest in grant["allowed_chunk_digests"]
        calls.append((source, digest, offset, length))
        selected = contents[digest][offset : offset + length]
        midpoint = max(1, len(selected) // 2)
        yield selected[:midpoint]
        if selected[midpoint:]:
            yield selected[midpoint:]

    return read


def test_multi_source_acquisition_promotes_and_warm_reuse_transfers_zero(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    calls: list[tuple] = []
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    provisioner = SwarmArtifactProvisioner(
        store, clock_unix_ms=lambda: 1_500, disk_free_bytes=lambda _path: 10**9
    )
    result = provisioner.acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, calls),
        predicted_improvement_ratio=0.5,
    )
    assert result["state"] == "ready"
    assert result["transferred_verified_bytes"] == len(payload)
    assert result["origin_bytes"] == 0
    assert {source for source, *_ in calls} == {"member-a", "member-b"}
    promoted_files = store.root / "promoted" / manifest["manifest_id"] / "files"
    for record in manifest["files"]:
        assert (promoted_files / record["relative_path"]).read_bytes() == payload[
            record["offset_bytes"] : record["offset_bytes"] + record["size_bytes"]
        ]
    assert store.ledger()["history"][-1] == result

    calls.clear()
    warm = provisioner.acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, calls),
        predicted_improvement_ratio=0.5,
    )
    assert warm["state"] == "ready"
    assert warm["cached_verified_bytes"] == len(payload)
    assert warm["transferred_verified_bytes"] == 0
    assert warm["duplicate_bytes_prevented"] == len(payload)
    assert warm["resumed_chunk_count"] == len(manifest["chunks"])
    assert calls == []
    assert len(store.ledger()["history"]) == 2

    public = store.public_ledger()
    assert public == store.ledger()
    store._history_path.write_text("not-json", encoding="utf-8")
    assert store.public_ledger() == public
    with pytest.raises(ArtifactProvisioningError, match="artifact_history_corrupt"):
        store.ledger()


def test_remote_terminal_status_is_rebased_into_product_ledger(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    remote_store = ArtifactAcquisitionStore(tmp_path / "remote")
    remote = SwarmArtifactProvisioner(
        remote_store,
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, []),
        predicted_improvement_ratio=0.5,
    )
    remote["generation"] = 41

    product_store = ArtifactAcquisitionStore(tmp_path / "product")
    imported = product_store.import_member_terminal(remote)

    assert imported["generation"] == 1
    assert imported["acquisition_id"] == remote["acquisition_id"]
    assert imported["transferred_verified_bytes"] == len(payload)
    assert product_store.ledger()["history"] == [imported]

    with pytest.raises(
        ArtifactProvisioningError, match="artifact_member_status_replay"
    ):
        product_store.import_member_terminal(remote)


def test_remote_nonterminal_status_is_not_imported(tmp_path: Path) -> None:
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    _payload, manifest, _binding = _pack()
    status = SwarmArtifactProvisioner(store)._initial_status(
        manifest=manifest,
        generation=1,
        acquisition_id="acquisition-remote",
        cached_bytes=0,
        cached_chunks=0,
        sources=("member-a",),
    )

    with pytest.raises(
        ArtifactProvisioningError, match="artifact_terminal_status_invalid"
    ):
        store.import_member_terminal(status)

    assert store.ledger()["history"] == []


def test_zero_length_partial_left_by_failed_attempt_is_retried(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    target = manifest["chunks"][0]
    partial = store.partial_path(target["content_digest"])
    partial.write_bytes(b"")

    result = SwarmArtifactProvisioner(
        store,
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, []),
        predicted_improvement_ratio=0.5,
    )

    assert result["state"] == "ready"
    assert result["origin_bytes"] == 0
    assert result["verified_chunk_count"] == len(manifest["chunks"])


def test_transfers_from_distinct_sources_concurrently_with_policy_bounds(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    condition = threading.Condition()
    active = 0
    maximum_active = 0
    release = threading.Event()

    def read(source: str, digest: str, offset: int, length: int, _grant: dict):
        nonlocal active, maximum_active
        with condition:
            active += 1
            maximum_active = max(maximum_active, active)
            if maximum_active >= 2:
                release.set()
        assert release.wait(timeout=5)
        try:
            yield contents[digest][offset : offset + length]
        finally:
            with condition:
                active -= 1

    result = SwarmArtifactProvisioner(
        ArtifactAcquisitionStore(tmp_path / "artifacts"),
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=read,
        predicted_improvement_ratio=0.5,
    )
    assert result["state"] == "ready"
    assert maximum_active == 2


def test_transfer_enforces_byte_rate_budget_without_an_initial_full_chunk_burst(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    advertisement = {
        **_advertisements(manifest)[0],
        "available_chunk_digests": sorted(contents),
    }
    started = time.monotonic()
    result = SwarmArtifactProvisioner(
        ArtifactAcquisitionStore(tmp_path / "artifacts"),
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(
            manifest,
            authorized_source_member_ids=["member-a"],
            maximum_concurrency=1,
            maximum_bytes_per_second=1_000_000,
        ),
        advertisements=[advertisement],
        policy=_policy(
            maximum_sources=1,
            aggregate_concurrency=1,
            per_source_bytes_per_second=1_000_000,
            aggregate_bytes_per_second=1_000_000,
        ),
        reader=_reader(contents, []),
    )
    elapsed = time.monotonic() - started
    assert result["state"] == "ready"
    assert elapsed >= len(payload) / 1_000_000 * 0.8


def test_interrupted_partial_rotates_source_and_resumes_without_widening(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    target = manifest["chunks"][0]
    partial = store.partial_path(target["content_digest"])
    partial.write_bytes(contents[target["content_digest"]][: CHUNK_SIZE // 4])
    calls: list[tuple] = []

    def read(source: str, digest: str, offset: int, length: int, grant: dict):
        assert digest in grant["allowed_chunk_digests"]
        calls.append((source, digest, offset, length))
        selected = contents[digest][offset : offset + length]
        if source == "member-a" and digest == target["content_digest"] and offset:
            yield selected[:1024]
            raise ConnectionError("source lost")
        yield selected

    result = SwarmArtifactProvisioner(
        store,
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=[
            {
                **item,
                "available_chunk_digests": sorted(
                    chunk["content_digest"] for chunk in manifest["chunks"]
                ),
            }
            for item in _advertisements(manifest)
        ],
        policy=_policy(),
        reader=read,
        predicted_improvement_ratio=0.5,
    )
    assert result["state"] == "ready"
    assert result["source_rotation_count"] >= 1
    assert result["resumed_chunk_count"] >= 1
    assert result["duplicate_bytes_prevented"] >= CHUNK_SIZE // 4
    assert any(
        call[0] == "member-b" and call[1] == target["content_digest"] for call in calls
    )


def test_corrupt_cache_and_source_are_quarantined_before_rotation(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    target = manifest["chunks"][0]
    store.object_path(target["content_digest"]).write_bytes(b"x" * target["size_bytes"])

    def read(source: str, digest: str, offset: int, length: int, _grant: dict):
        if source == "member-a" and digest == target["content_digest"]:
            yield b"z" * length
        else:
            yield contents[digest][offset : offset + length]

    result = SwarmArtifactProvisioner(
        store,
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=[
            {
                **item,
                "available_chunk_digests": sorted(
                    chunk["content_digest"] for chunk in manifest["chunks"]
                ),
            }
            for item in _advertisements(manifest)
        ],
        policy=_policy(),
        reader=read,
        predicted_improvement_ratio=0.5,
    )
    assert result["state"] == "ready"
    assert result["quarantined_bytes"] >= 2 * target["size_bytes"]
    assert result["source_rotation_count"] >= 1
    assert len(list((store.root / "quarantine").iterdir())) >= 2


def test_terminal_corrupt_source_preserves_quarantine_accounting(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")

    def corrupt(_source: str, _digest: str, _offset: int, length: int, _grant: dict):
        yield b"z" * length

    result = SwarmArtifactProvisioner(
        store,
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=[
            {
                **item,
                "available_chunk_digests": sorted(
                    chunk["content_digest"] for chunk in manifest["chunks"]
                ),
            }
            for item in _advertisements(manifest)
        ],
        policy=_policy(
            aggregate_concurrency=1,
            per_source_concurrency=1,
            maximum_retries_per_chunk=0,
        ),
        reader=corrupt,
    )

    assert result["state"] == "failed"
    assert result["reason_code"] == "bounded_retry_exhaustion"
    assert result["quarantined_bytes"] >= manifest["chunks"][0]["size_bytes"]
    assert result["promotion_digest"] is None


def test_no_source_cancellation_and_interrupted_restart_are_durable(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    provisioner = SwarmArtifactProvisioner(
        store, clock_unix_ms=lambda: 1_500, disk_free_bytes=lambda _path: 10**9
    )
    no_source = provisioner.acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=[],
        policy=_policy(),
        reader=_reader(contents, []),
    )
    assert no_source["state"] == "failed"
    assert no_source["reason_code"] == "no_authorized_source"

    cancelled = threading.Event()
    cancelled.set()
    result = provisioner.acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, []),
        predicted_improvement_ratio=0.5,
        cancel=cancelled,
    )
    assert result["state"] == "cancelled"
    assert result["reason_code"] is None
    assert len(store.ledger()["history"]) == 2


def test_insufficient_disk_authorization_drift_and_concurrent_writer_fail_closed(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    insufficient = SwarmArtifactProvisioner(
        store, clock_unix_ms=lambda: 1_500, disk_free_bytes=lambda _path: 1
    )
    with pytest.raises(ArtifactProvisioningError, match="insufficient_disk"):
        insufficient.acquire(
            manifest=manifest,
            expected_binding=binding,
            grant=_grant(manifest),
            advertisements=_advertisements(manifest),
            policy=_policy(),
            reader=_reader(contents, []),
        )
    assert store.ledger()["history"] == []

    drifted = _grant(manifest, representation_digest=DIGEST_D)
    with pytest.raises(ArtifactProvisioningError, match="authorization_drift"):
        SwarmArtifactProvisioner(
            store, clock_unix_ms=lambda: 1_500, disk_free_bytes=lambda _path: 10**9
        ).acquire(
            manifest=manifest,
            expected_binding=binding,
            grant=drifted,
            advertisements=_advertisements(manifest),
            policy=_policy(),
            reader=_reader(contents, []),
        )

    descriptor = store.acquire_writer()
    try:
        with pytest.raises(
            ArtifactProvisioningError, match="concurrent_staging_conflict"
        ):
            SwarmArtifactProvisioner(
                store,
                clock_unix_ms=lambda: 1_500,
                disk_free_bytes=lambda _path: 10**9,
            ).acquire(
                manifest=manifest,
                expected_binding=binding,
                grant=_grant(manifest),
                advertisements=_advertisements(manifest),
                policy=_policy(),
                reader=_reader(contents, []),
            )
    finally:
        store.release_writer(descriptor)


def test_promotion_storage_failure_is_terminal_and_never_strands_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    provisioner = SwarmArtifactProvisioner(
        store, clock_unix_ms=lambda: 1_500, disk_free_bytes=lambda _path: 10**9
    )

    def fail_promotion(**_kwargs: object) -> str:
        raise OSError("simulated storage exhaustion")

    monkeypatch.setattr(provisioner, "_promote", fail_promotion)
    result = provisioner.acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=_grant(manifest),
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, []),
        predicted_improvement_ratio=0.5,
    )

    assert result["state"] == "failed"
    assert result["reason_code"] == "artifact_storage_failure"
    assert result["promotion_digest"] is None
    ledger = store.ledger()
    assert ledger["current"] is None
    assert ledger["history"][-1] == result


def test_signed_grant_is_consumed_once_and_replay_survives_restart(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store_root = tmp_path / "artifacts"
    grant = _grant(
        manifest,
        grant_id="grant-one",
        nonce="nonce-one",
        expires_at_unix_ms=2_000,
    )
    first = SwarmArtifactProvisioner(
        ArtifactAcquisitionStore(store_root),
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    ).acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=grant,
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, []),
    )
    assert first["state"] == "ready"

    restarted = SwarmArtifactProvisioner(
        ArtifactAcquisitionStore(store_root),
        clock_unix_ms=lambda: 1_500,
        disk_free_bytes=lambda _path: 10**9,
    )
    with pytest.raises(ArtifactProvisioningError, match="artifact_grant_replay"):
        restarted.acquire(
            manifest=manifest,
            expected_binding=binding,
            grant=grant,
            advertisements=_advertisements(manifest),
            policy=_policy(),
            reader=_reader(contents, []),
        )

    fresh_grant = {**grant, "grant_id": "grant-two", "nonce": "nonce-two"}
    warm = restarted.acquire(
        manifest=manifest,
        expected_binding=binding,
        grant=fresh_grant,
        advertisements=_advertisements(manifest),
        policy=_policy(),
        reader=_reader(contents, []),
    )
    assert warm["state"] == "ready"
    assert warm["transferred_verified_bytes"] == 0


def test_refresh_observes_current_progress_and_second_store_recovers_interruption(
    tmp_path: Path,
) -> None:
    payload, manifest, binding = _pack()
    contents = _content(payload, manifest)
    store = ArtifactAcquisitionStore(tmp_path / "artifacts")
    entered = threading.Event()
    release = threading.Event()

    def read(_source: str, digest: str, offset: int, length: int, _grant: dict):
        entered.set()
        assert release.wait(timeout=5)
        yield contents[digest][offset : offset + length]

    result: list[dict] = []
    worker = threading.Thread(
        target=lambda: result.append(
            SwarmArtifactProvisioner(
                store,
                clock_unix_ms=lambda: 1_500,
                disk_free_bytes=lambda _path: 10**9,
            ).acquire(
                manifest=manifest,
                expected_binding=binding,
                grant=_grant(manifest),
                advertisements=_advertisements(manifest),
                policy=_policy(),
                reader=read,
                predicted_improvement_ratio=0.5,
            )
        )
    )
    worker.start()
    assert entered.wait(timeout=5)
    current = store.ledger()["current"]
    assert current is not None
    assert current["state"] == "transferring"
    release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert result[0]["state"] == "ready"

    interrupted_root = tmp_path / "interrupted"
    interrupted = ArtifactAcquisitionStore(interrupted_root)
    status = copy.deepcopy(result[0])
    status.update(
        generation=1,
        acquisition_id="acquisition-interrupted",
        state="transferring",
        phase="transferring",
        promotion_digest=None,
        terminal_at_unix_ms=None,
        reason_code=None,
        retryable=False,
    )
    interrupted.write_current(status)
    recovered = ArtifactAcquisitionStore(interrupted_root).ledger()
    assert recovered["current"] is None
    assert recovered["history"][-1]["reason_code"] == "interrupted_transfer"
    assert recovered["history"][-1]["retryable"] is True
