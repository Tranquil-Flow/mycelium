from __future__ import annotations

import copy

import pytest

from mycelium_capacity_profiles import (
    CapacityObservation,
    CapacityProfileKey,
    CapacityProfilePolicy,
    compile_capacity_profile,
)
from mycelium_gossip.registry import VersionedRecordStore
from mycelium_gossip.schema import RecordKind
from mycelium_gossip.service import GossipService
from mycelium_gossip.signed_bundle import (
    SignedEvidenceBundleError,
    seal_evidence_bundle,
    validate_signed_evidence_bundle,
)
from mycelium_gossip.transport import InMemoryMesh, InMemoryTransport, LivenessEvent, LivenessKind
from mycelium_qualification.signing import generate_ed25519_signer
from tests.gossip.helpers import make_record


def _bundle():
    store = VersionedRecordStore("swarm-a", monotonic=lambda: 1.0)
    service = GossipService(
        swarm_id="swarm-a",
        node_id="seed",
        incarnation=1,
        boot_id="boot-seed-1",
        transport=InMemoryTransport(InMemoryMesh(monotonic=lambda: 1.0), "seed"),
        registry=store,
        monotonic=lambda: 1.0,
    )
    for node_id in ("node-a", "node-b"):
        for kind in (RecordKind.PROFILE, RecordKind.STATUS, RecordKind.MEMBERSHIP):
            store.apply(make_record(kind, node_id=node_id, ttl_ms=10_000))
        service.submit_liveness(
            LivenessEvent(
                LivenessKind.PUT,
                "swarm-a",
                node_id,
                1,
                f"boot-{node_id}-1",
                1.0,
            )
        )
    service.drain()
    return service.capture_evidence_bundle(
        deployment_id="12345678-1234-5678-9234-abcdefabcdef",
        deployment_epoch=3,
        model_id="org/model",
        num_layers=4,
        manifest_digest="sha256:" + "a" * 64,
        resolved_commit="b" * 40,
    )


def _sealed():
    signer = generate_ed25519_signer(endpoint_id="seed-endpoint")
    profile = compile_capacity_profile(
        CapacityProfileKey(
            model_digest="sha256:" + "a" * 64,
            source_evidence_digest="sha256:" + "c" * 64,
            quantization="none",
            backend="mlx",
            runtime_build="mlx-1",
            hardware_class="apple-silicon",
            power_mode="ac",
            context_bucket="interactive-4k",
            kv_mode="stage_local_kv",
        ),
        (
            CapacityObservation(
                concurrency=1,
                sample_count=3,
                p95_ttft_ms=10.0,
                p95_tpot_ms=5.0,
                aggregate_output_tps=2.0,
                peak_memory_bytes=100,
                memory_budget_bytes=1_000,
            ),
        ),
        CapacityProfilePolicy(
            ttft_p95_slo_ms=1_000.0,
            tpot_p95_slo_ms=1_000.0,
            min_samples=3,
        ),
    )
    document = seal_evidence_bundle(
        _bundle(),
        signer=signer,
        captured_at_unix_ms=1_000,
        valid_for_ms=5_000,
        authority_generation=2,
        capacity_profiles={"node-a": profile, "node-b": profile},
    )
    return signer, document


def test_signed_bundle_binds_authority_generation_source_and_fresh_records() -> None:
    signer, document = _sealed()

    validated = validate_signed_evidence_bundle(
        document,
        expected_verification_key_digest=signer.verification_key_digest,
        now_unix_ms=2_000,
    )

    bundle = validated.bundle
    assert bundle.snapshot_generation > 0
    assert set(validated.capacity_profiles) == {"node-a", "node-b"}
    assert document["statement"]["authority_generation"] == 2
    assert document["statement"]["evidence_bundle_digest"] == bundle.evidence_bundle_digest


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["statement"].__setitem__("deployment_epoch", 4),
        lambda value: value["evidence_bundle"]["model"].__setitem__("num_layers", 5),
        lambda value: value["signature"].__setitem__("signature", "AAAA"),
    ],
)
def test_signed_bundle_rejects_tampering(mutation) -> None:
    signer, document = _sealed()
    mutation(document)

    with pytest.raises(SignedEvidenceBundleError):
        validate_signed_evidence_bundle(
            document,
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=2_000,
        )


def test_signed_bundle_rejects_expiry_untrusted_key_and_stale_record() -> None:
    signer, document = _sealed()
    with pytest.raises(SignedEvidenceBundleError, match="not current"):
        validate_signed_evidence_bundle(
            document,
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=6_000,
        )
    with pytest.raises(SignedEvidenceBundleError, match="not trusted"):
        validate_signed_evidence_bundle(
            document,
            expected_verification_key_digest="sha256:" + "f" * 64,
            now_unix_ms=2_000,
        )

    stale = copy.deepcopy(document)
    stale["statement"]["captured_at_unix_ms"] = 20_000
    stale["statement"]["valid_until_unix_ms"] = 21_000
    stale["signature"] = signer.sign(stale["statement"])
    with pytest.raises(SignedEvidenceBundleError, match="stale records"):
        validate_signed_evidence_bundle(
            stale,
            expected_verification_key_digest=signer.verification_key_digest,
            now_unix_ms=20_000,
        )
