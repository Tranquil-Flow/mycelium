# SPDX-License-Identifier: AGPL-3.0-only
"""Executable acceptance tests for the nine frozen A9 platform-neutral contracts.

Deterministic, in-process, serial.  These tests discharge the contract half of
the plan's S2 lane (the spec's canonical-encoding, bounds, unknown-enum,
nullability, source-kind separation, signature/generation-mismatch, and privacy
rules) and the fail-closed negatives that A9 freezes in its acceptance
inventory.  Nothing here is a qualification, an eligibility grant, a placement,
or a readiness claim: no physical round has been executed by this lane.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mycelium_qualification.signing import generate_ed25519_signer
from mycelium_platform_neutral import (
    FROZEN_A9_PROTOCOLS,
    PlatformNeutralContractError,
    acceleration_profile,
    activation_codec,
    capability_report,
    class_qualification,
    eligibility,
    migration,
    peer_profile,
    primitives,
    runtime_efficiency,
    stage_performance,
)

ROOT = Path(__file__).resolve().parents[2]
SPECIFICATION = (
    ROOT
    / "docs/superpowers/specs/2026-08-18-mycelium-a9-platform-neutral-peer-capability.md"
)
NOW = 1_000_000.0
LATER = NOW + 600.0


def D(char: str) -> str:
    return "sha256:" + char * 64


def transport_mapping(**overrides):
    base = {
        "family": "iroh",
        "build_digest": D("b"),
        "wire_protocol_versions": ["mycelium.router_wire.v1"],
        "endpoint_id_digest": D("e"),
        "connection_roles": ["initiator", "responder"],
        "direct_observed": None,
        "relay_observed": None,
        "persistent_reuse": True,
        "lifecycle_availability": "service_managed",
        "max_frame_bytes": 1 << 20,
        "max_concurrency": 4,
    }
    base.update(overrides)
    return base


def runtime_mapping(**overrides):
    base = {
        "backend_id": "mlx",
        "build_digest": D("c"),
        "model_families": ["qwen2"],
        "representations": ["int8_weights"],
        "precisions": ["int8", "float16"],
        "decode_operations": ["prefill", "decode"],
        "max_context_tokens": 32768,
        "max_concurrent_requests": 2,
        "kv_schema": "mycelium.kv.stage_local.v1",
        "artifact_formats": ["safetensors"],
        "cancellation_supported": True,
        "cleanup_supported": True,
    }
    base.update(overrides)
    return base


def profile_mapping(**overrides):
    base = {
        "protocol": peer_profile.PROTOCOL,
        "profile_id": "profile-001",
        "swarm_id": "swarm-demo",
        "node_id": "node-a",
        "incarnation": "incarnation-002",
        "membership_generation": 3,
        "identity_kind": "native_peer",
        "platform_family": "macos",
        "platform_variant": None,
        "architecture": "arm64",
        "software_build": {"version": "0.9.0", "digest": D("a")},
        "transports": [transport_mapping()],
        "runtimes": [runtime_mapping()],
        "lease_expires_at": LATER,
        "issued_at": NOW - 60.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def profile(**overrides) -> peer_profile.PeerProfile:
    return peer_profile.PeerProfile.from_mapping(profile_mapping(**overrides))


FAMILY_VALUES = {
    "memory": {
        "sustainable_bytes": 8 * (1 << 30),
        "available_bytes": 4 * (1 << 30),
        "workspace_bytes": 1 << 30,
        "kv_headroom_bytes": 1 << 29,
    },
    "storage": {"assignment_local_bytes": 1 << 40, "free_bytes": 1 << 40},
    "limits": {
        "supported_context_tokens": 32768,
        "active_requests": 2,
        "batch_size": 8,
        "prefill_tokens": 4096,
        "decode_tokens": 1,
        "multi_position_verify": 4,
        "speculative_draft": 4,
    },
    "thermal": {
        "state": "nominal",
        "sustained_class": "sustained",
        "observation_duration_s": 600.0,
        "throttle_behavior": "none",
    },
    "power": {
        "source": "ac",
        "battery_class": None,
        "battery_level": None,
        "low_power_state": False,
        "minimum_contribution": "unrestricted",
        "drain_deadline_s": None,
    },
    "lifecycle": {
        "state": "background",
        "background_execution": True,
        "suspend_behavior": "resume_safe",
        "restart_semantics": "new_incarnation",
        "max_background_grace_s": 300.0,
    },
    "network": {
        "activation_protocol": "mycelium.router_wire.v1",
        "transport_observations": ["direct"],
        "network_loss_behavior": "reconnect",
        "link_measurement_available": True,
        "freshness_s": 30.0,
    },
}


def report_mapping(
    subject: peer_profile.PeerProfile,
    *,
    source_kind: str = "native_observed",
    populated: bool = True,
    families: dict | None = None,
    **overrides,
):
    def family(name: str) -> dict:
        values = dict(FAMILY_VALUES[name]) if populated else {key: None for key in FAMILY_VALUES[name]}
        if families and name in families:
            values.update(families[name])
        return {"source_kind": source_kind, "observed_at": NOW - 30.0, "values": values}

    base = {
        "protocol": capability_report.PROTOCOL,
        "report_id": f"report-{subject.node_id}",
        "swarm_id": subject.swarm_id,
        "node_id": subject.node_id,
        "incarnation": subject.incarnation,
        "membership_generation": subject.membership_generation,
        "profile_digest": subject.digest,
        "evidence_generation": 2,
        "observations": {
            name: family(name)
            for name in capability_report.CAPABILITY_FAMILIES
        },
        "issued_at": NOW - 30.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def report(
    subject: peer_profile.PeerProfile, **overrides
) -> capability_report.CapabilityReport:
    return capability_report.CapabilityReport.from_mapping(
        report_mapping(subject, **overrides)
    )


def qualification_mapping(
    subject: peer_profile.PeerProfile,
    *,
    role: str = "qualified_model_stage",
    decision: str = "qualified",
    **overrides,
):
    base = {
        "protocol": class_qualification.PROTOCOL,
        "qualification_id": "qualification-001",
        "role": role,
        "subject": {
            "node_id": subject.node_id,
            "profile_digest": subject.digest,
            "incarnation": subject.incarnation,
            "membership_generation": subject.membership_generation,
        },
        "exact_class": {
            "platform_family": "macos",
            "architecture": "arm64",
            "transport_family": "iroh",
            "runtime_backend_id": "mlx",
            "build_digest": D("c"),
        },
        "decision": decision,
        "blockers": [] if decision == "qualified" else ["physical_class_not_qualified"],
        "evidence": {
            "profile_digest": subject.digest,
            "evidence_generation": 2,
            "positive_digests": [D("1")] if decision == "qualified" else [],
            "negative_digests": [D("2")] if decision == "qualified" else [],
        },
        "constraints": ["stage_local_kv", "context_8192"],
        "issued_at": NOW - 10.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def qualification(
    subject: peer_profile.PeerProfile, **overrides
) -> class_qualification.ClassQualification:
    return class_qualification.ClassQualification.from_mapping(
        qualification_mapping(subject, **overrides)
    )


def observation_mapping(**overrides):
    per_component = 10.0
    components = {
        key: {"time_ms": per_component, "bytes": 1024, "samples": 24}
        for key in stage_performance.COMPONENT_KEYS
    }
    base = {
        "protocol": stage_performance.PROTOCOL,
        "observation_id": "observation-001",
        "node_id": "node-a",
        "profile_digest": D("f"),
        "provider": {
            "backend_id": "mlx",
            "build_digest": D("c"),
            "device_class": "apple_arm64_gpu",
            "driver_runtime_digest": D("d"),
        },
        "phase": "decode",
        "components": components,
        "end_to_end_ms": per_component * len(stage_performance.COMPONENT_KEYS),
        "materialization": {
            "full_weight_materializations": 0,
            "packed_state_bytes": 1 << 20,
        },
        "thermal": "nominal",
        "concurrency": 1,
        "bucket": {"context_tokens": 8192, "output_tokens": 512},
        "sample_count": 24,
        "uncertainty_ms": 5.0,
        "warm": True,
        "issued_at": NOW - 5.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def observation(**overrides) -> stage_performance.StagePerformanceObservation:
    return stage_performance.StagePerformanceObservation.from_mapping(
        observation_mapping(**overrides)
    )


def efficiency_mapping(**overrides):
    base = {
        "protocol": runtime_efficiency.PROTOCOL,
        "qualification_id": "efficiency-001",
        "provider_binding": {
            "backend_id": "mlx",
            "build_digest": D("c"),
            "device_class": "apple_arm64_gpu",
            "driver_runtime_digest": D("d"),
        },
        "model_binding": {
            "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
            "revision": "7ae557604adf67be50417f59c2c2f167def9a775",
            "representation": "int8_weights",
            "profile_digest": D("f"),
        },
        "benchmark": {
            "manifest_digest": D("0"),
            "paired_windows": 3,
            "requests_per_mode": 24,
            "prompt_buckets": 2,
            "output_buckets": 2,
            "generated_tokens_per_mode": 512,
            "thresholds": dict(runtime_efficiency.THRESHOLDS),
            "thermal_state": "nominal",
            "measured_at": NOW - 60.0,
        },
        "results": {
            "median_decode_ratio": 2.4,
            "paired_confidence_lower_bound": 1.8,
            "median_decode_tokens_per_second": 12.0,
            "p95_ttft_ms": 900.0,
        },
        "reliability": {"failures": 0, "cancellations": 0, "timeouts": 0, "excluded": 0},
        "cleanup": {"request_scoped_cleanup": True, "stage_local_kv": True},
        "materialization_proof": {
            "warm_decode_full_weight_materializations": 0,
            "warm_decode_windows": 3,
        },
        "planner_result": {
            "vocabulary_head_cost_explicit": True,
            "unknown_component_costs": [],
            "prediction_error_ms": 4.0,
        },
        "evidence_generation": 2,
        "issued_at": NOW - 30.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def efficiency_qualification(**overrides) -> runtime_efficiency.RuntimeEfficiencyQualification:
    return runtime_efficiency.decide_efficiency(efficiency_mapping(**overrides))


def acceleration_mapping(**overrides):
    base = {
        "protocol": acceleration_profile.PROTOCOL,
        "profile_id": "provider-001",
        "backend_id": "mlx",
        "registry_stage": "implemented",
        "implementation_family": "metal_performance_shaders",
        "execution_provider": "metal",
        "build_digest": D("c"),
        "device_class": "apple_arm64_gpu",
        "driver_runtime": {"identity": "macos_metal", "version_digest": D("d")},
        "supported_operations": sorted(acceleration_profile.REQUIRED_OPERATIONS),
        "kernels": ["int8_gemv", "int8_gemm"],
        "precision_policy": {
            "weight_precision": "int8",
            "activation_precisions": ["float16"],
            "per_decode_full_weight_cast": False,
        },
        "materialization_policy": {
            "persistent_state_boundary": "warmup",
            "decode_time_full_weight_materializations": 0,
            "packed_state_bounded_bytes": 1 << 20,
        },
        "stage_local_kv": True,
        "workspace_bounded_bytes": 1 << 30,
        "cancellation_supported": True,
        "cleanup_supported": True,
        "issued_at": NOW - 10.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def codec_mapping(**overrides):
    base = {
        "protocol": activation_codec.PROTOCOL,
        "qualification_id": "codec-001",
        "dtype": "float16",
        "sender": {
            "runtime_build_digest": D("c"),
            "transport_family": "iroh",
            "device_class": "apple_arm64_gpu",
        },
        "receiver": {
            "runtime_build_digest": D("b"),
            "transport_family": "iroh",
            "device_class": "apple_arm64_gpu",
        },
        "generation_binding": 4,
        "shapes": {"hidden_size": 896, "max_sequence": 8192, "batch": 1},
        "parity": {
            "output_token_parity": True,
            "sample_count": 512,
            "parity_digest": D("3"),
        },
        "validation": {
            "shape_checked": True,
            "dtype_checked": True,
            "finite_values_checked": True,
            "byte_reduction_measured": True,
        },
        "byte_reduction": {
            "bytes_before": 1 << 20,
            "bytes_after": 1 << 19,
            "samples": 512,
            "counter_digest": D("4"),
        },
        "negatives": {
            name: {"rejected": True, "evidence_digest": D("56789ab"[index])}
            for index, name in enumerate(activation_codec.REQUIRED_NEGATIVES)
        },
        "withdrawal": {"state": "current", "reason": None},
        "fallback": {"dtype": None, "qualification_digest": None},
        "issued_at": NOW - 10.0,
        "expires_at": LATER,
    }
    base.update(overrides)
    return base


def codec(**overrides) -> activation_codec.ActivationCodecQualification:
    return activation_codec.ActivationCodecQualification.from_mapping(
        codec_mapping(**overrides)
    )


def rejection(code: str, call, exc=PlatformNeutralContractError) -> None:
    with pytest.raises(exc) as error:
        call()
    assert error.value.code == code


def test_the_nine_frozen_contracts_match_the_specification_inventory() -> None:
    specification = SPECIFICATION.read_text("utf-8")
    assert len(FROZEN_A9_PROTOCOLS) == 9
    assert len(set(FROZEN_A9_PROTOCOLS)) == 9
    modules = (
        peer_profile,
        capability_report,
        class_qualification,
        eligibility,
        migration,
        acceleration_profile,
        stage_performance,
        activation_codec,
        runtime_efficiency,
    )
    assert tuple(module.PROTOCOL for module in modules) == FROZEN_A9_PROTOCOLS
    for protocol in FROZEN_A9_PROTOCOLS:
        assert f"`{protocol}`" in specification


def test_records_are_canonical_and_round_trip_with_identical_digests() -> None:
    subject = profile()
    report_ = report(subject)
    assert (
        peer_profile.PeerProfile.from_mapping(subject.to_mapping()).digest
        == subject.digest
    )
    assert (
        capability_report.CapabilityReport.from_mapping(report_.to_mapping()).digest
        == report_.digest
    )
    assert subject.canonical_bytes() == primitives.canonical_bytes(
        subject.to_mapping(), "peer_profile_noncanonical"
    )


def test_peer_profile_rejects_unknown_fields_and_unknown_enum_values() -> None:
    extra = profile_mapping()
    extra["observed_evidence"] = D("9")
    rejection("peer_profile_invalid", lambda: peer_profile.PeerProfile.from_mapping(extra))
    bad_platform = profile_mapping(platform_family="plan9")
    rejection(
        "peer_profile_platform_invalid",
        lambda: peer_profile.PeerProfile.from_mapping(bad_platform),
    )
    bad_kind = profile_mapping(identity_kind="mac_mlx_iroh")
    rejection(
        "peer_profile_identity_kind_invalid",
        lambda: peer_profile.PeerProfile.from_mapping(bad_kind),
    )


def test_peer_profile_platform_variant_requires_the_registered_unknown_family() -> None:
    future = profile_mapping(
        platform_family="unknown",
        architecture="unknown",
        platform_variant="vendor-future-os",
    )
    assert peer_profile.PeerProfile.from_mapping(future).is_unknown_platform is True
    rejection(
        "peer_profile_platform_variant_requires_unknown_family",
        lambda: peer_profile.PeerProfile.from_mapping(
            profile_mapping(platform_variant="vendor-future-os")
        ),
    )


def test_peer_profile_rejects_unregistered_runtime_backends() -> None:
    rejection(
        "runtime_backend_not_registered",
        lambda: peer_profile.PeerProfile.from_mapping(
            profile_mapping(runtimes=[runtime_mapping(backend_id="pixel-stdlib")])
        ),
    )
    # The registered sentinel stays visible and ineligible rather than rejected.
    future_runtime = profile_mapping(
        runtimes=[runtime_mapping(backend_id="unknown", precisions=[])]
    )
    assert peer_profile.PeerProfile.from_mapping(future_runtime).runtime_for("unknown")


def test_peer_profile_rejects_duplicates_non_finite_and_stale_validity() -> None:
    rejection(
        "peer_profile_runtimes_duplicate",
        lambda: peer_profile.PeerProfile.from_mapping(
            profile_mapping(runtimes=[runtime_mapping(), runtime_mapping()])
        ),
    )
    rejection(
        "peer_profile_issued_at_invalid",
        lambda: peer_profile.PeerProfile.from_mapping(
            profile_mapping(issued_at=float("nan"))
        ),
    )
    rejection(
        "peer_profile_validity_invalid",
        lambda: peer_profile.PeerProfile.from_mapping(
            profile_mapping(expires_at=NOW - 60.0)
        ),
    )


def test_signed_profile_binds_the_member_key_and_rejects_tampering() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    subject = profile()
    envelope = peer_profile.sign_peer_profile(signer=signer, profile=subject)
    verified = peer_profile.verify_peer_profile(
        envelope,
        now=NOW,
        expected_key_digest=signer.verification_key_digest,
        expected_node_id=subject.node_id,
    )
    assert verified.digest == subject.digest
    tampered = dict(envelope)
    tampered["profile"] = {**envelope["profile"], "membership_generation": 4}
    rejection(
        "peer_profile_signature_invalid",
        lambda: peer_profile.verify_peer_profile(tampered, now=NOW),
    )
    rejection(
        "peer_profile_key_continuity_mismatch",
        lambda: peer_profile.verify_peer_profile(
            envelope, now=NOW, expected_key_digest=D("9")
        ),
    )
    rejection(
        "peer_profile_expired",
        lambda: peer_profile.verify_peer_profile(envelope, now=LATER + 1.0),
    )


def test_capability_report_requires_every_family_and_its_own_source_kind() -> None:
    subject = profile()
    partial = report_mapping(subject)
    partial["observations"].pop("thermal")
    rejection(
        "capability_families_invalid",
        lambda: capability_report.CapabilityReport.from_mapping(partial),
    )
    no_source = report_mapping(subject)
    no_source["observations"]["thermal"] = {
        "observed_at": None,
        "values": {key: None for key in FAMILY_VALUES["thermal"]},
    }
    rejection(
        "capability_family_invalid",
        lambda: capability_report.CapabilityReport.from_mapping(no_source),
    )
    bad_source = report_mapping(subject, source_kind="assumed_native")
    rejection(
        "capability_source_kind_invalid",
        lambda: capability_report.CapabilityReport.from_mapping(bad_source),
    )


def test_unknown_source_kind_must_be_null_and_never_zero_or_healthy() -> None:
    subject = profile()
    rejection(
        "capability_unknown_family_must_be_null",
        lambda: capability_report.CapabilityReport.from_mapping(
            report_mapping(subject, source_kind="unknown", populated=True)
        ),
    )
    unknown_report = report(subject, source_kind="unknown", populated=False)
    assert unknown_report.family("thermal").is_unknown() is True
    assert unknown_report.family("thermal").satisfies("native_observed") is False


def test_synthetic_conformance_cannot_satisfy_a_native_observed_gate() -> None:
    subject = profile()
    synthetic = report(subject, source_kind="synthetic_conformance")
    assert synthetic.family("memory").satisfies("synthetic_conformance") is True
    assert synthetic.family("memory").satisfies("native_observed") is False
    observed = report(subject)
    assert observed.family("memory").satisfies("native_observed") is True
    assert observed.family("memory").satisfies("synthetic_conformance") is False


def test_capability_report_binds_profile_digest_generation_and_validity() -> None:
    subject = profile()
    other = profile(profile_id="profile-002", node_id="node-b", incarnation="incarnation-003")
    rejection(
        "capability_report_profile_mismatch",
        lambda: report(subject).bound_to(other),
    )
    mismatched = report_mapping(subject)
    mismatched["incarnation"] = "incarnation-999"
    rejection(
        "capability_report_profile_mismatch",
        lambda: capability_report.CapabilityReport.from_mapping(
            mismatched
        ).bound_to(subject),
    )
    rejection(
        "capability_report_evidence_generation_invalid",
        lambda: capability_report.CapabilityReport.from_mapping(
            report_mapping(subject, evidence_generation=0)
        ),
    )
    rejection(
        "capability_report_validity_invalid",
        lambda: capability_report.CapabilityReport.from_mapping(
            report_mapping(subject, expires_at=NOW - 100.0)
        ),
    )


def test_capability_report_rejects_non_finite_and_unbounded_values() -> None:
    subject = profile()
    rejection(
        "capability_value_invalid:memory:sustainable_bytes",
        lambda: capability_report.CapabilityReport.from_mapping(
            report_mapping(subject, families={"memory": {"sustainable_bytes": float("inf")}})
        ),
    )
    rejection(
        "capability_value_invalid:limits:batch_size",
        lambda: capability_report.CapabilityReport.from_mapping(
            report_mapping(subject, families={"limits": {"batch_size": -1}})
        ),
    )
    rejection(
        "capability_values_invalid",
        lambda: capability_report.CapabilityReport.from_mapping(
            report_mapping(subject, families={"memory": {"unknown_field": 1}})
        ),
    )


def test_signed_capability_report_rejects_a_foreign_key() -> None:
    signer = generate_ed25519_signer(endpoint_id="endpoint-node-a")
    subject = profile()
    envelope = capability_report.sign_capability_report(
        signer=signer, report=report(subject)
    )
    verified = capability_report.verify_capability_report(
        envelope, now=NOW, expected_key_digest=signer.verification_key_digest
    )
    assert verified.digest == report(subject).digest
    rejection(
        "capability_report_key_mismatch",
        lambda: capability_report.verify_capability_report(
            envelope, now=NOW, expected_key_digest=D("7")
        ),
    )


def test_class_qualification_requires_executed_positive_and_negative_evidence() -> None:
    subject = profile()
    without_positive = qualification_mapping(subject)
    without_positive["evidence"]["positive_digests"] = []
    rejection(
        "qualification_requires_executed_positive_and_negative_evidence",
        lambda: class_qualification.ClassQualification.from_mapping(without_positive),
    )
    without_negative = qualification_mapping(subject)
    without_negative["evidence"]["negative_digests"] = []
    rejection(
        "qualification_requires_executed_positive_and_negative_evidence",
        lambda: class_qualification.ClassQualification.from_mapping(without_negative),
    )
    not_qualified = qualification(subject, decision="not_qualified")
    assert not_qualified.grants("qualified_model_stage") is False


def test_class_qualification_is_role_specific_and_cannot_grant_another_role() -> None:
    subject = profile()
    stage = qualification(subject, role="qualified_model_stage")
    assert stage.grants("qualified_model_stage") is True
    for other in (
        "probe_contributor",
        "artifact_source",
        "speculative_draft_worker",
        "performance_ready_model_stage",
    ):
        assert stage.grants(other) is False


def test_class_qualification_rejects_later_authority_and_unknown_classes() -> None:
    subject = profile()
    with_route_ready = qualification_mapping(subject)
    with_route_ready["route_ready"] = True
    rejection(
        "qualification_must_not_carry_later_authority",
        lambda: class_qualification.ClassQualification.from_mapping(with_route_ready),
    )
    unknown_class = qualification_mapping(subject)
    unknown_class["exact_class"]["architecture"] = "unknown"
    rejection(
        "qualification_unknown_class_cannot_be_qualified",
        lambda: class_qualification.ClassQualification.from_mapping(unknown_class),
    )
    partial_evidence = qualification_mapping(subject)
    partial_evidence["decision"] = "not_qualified"
    partial_evidence["blockers"] = []
    rejection(
        "qualification_requires_blockers",
        lambda: class_qualification.ClassQualification.from_mapping(partial_evidence),
    )


def full_ladder_inputs(subject: peer_profile.PeerProfile, **overrides) -> eligibility.LadderInputs:
    base = dict(
        node_id=subject.node_id,
        membership_generation=subject.membership_generation,
        lease_expires_at=subject.lease_expires_at,
        revoked=False,
        identity_kind=subject.identity_kind,
        consent=True,
        profile=subject,
        report=report(subject),
        qualification=qualification(subject),
        probe_gate=eligibility.ProbeGate(integrity_verified=True, fresh=True),
        assignment_grant=eligibility.ArtifactAssignmentGrant(grant_id="grant-001"),
        a11_closed=False,
        now=NOW,
    )
    base.update(overrides)
    return eligibility.LadderInputs(**base)


def test_current_member_with_no_evidence_is_ineligible() -> None:
    subject = profile()
    bare = eligibility.LadderInputs(
        node_id=subject.node_id,
        membership_generation=subject.membership_generation,
        lease_expires_at=subject.lease_expires_at,
        revoked=False,
        identity_kind=subject.identity_kind,
        consent=True,
        now=NOW,
    )
    assert eligibility.derive_eligibility("signed_member", bare, now=NOW).state == "eligible"
    probe = eligibility.derive_eligibility("probe_contributor", bare, now=NOW)
    assert probe.state == "ineligible"
    assert "capability_profile_absent" in probe.blockers
    stage = eligibility.derive_eligibility("qualified_model_stage", bare, now=NOW)
    assert stage.state == "ineligible"
    assert "class_qualification_absent" in stage.blockers


def test_unknown_membership_facts_are_unknown_not_eligible() -> None:
    indeterminate = eligibility.LadderInputs(
        node_id="node-a",
        membership_generation=None,
        lease_expires_at=None,
        revoked=None,
        identity_kind=None,
        consent=None,
        now=NOW,
    )
    result = eligibility.derive_eligibility("signed_member", indeterminate, now=NOW)
    assert result.state == "unknown"
    assert "membership_generation_unknown" in result.blockers


def test_unknown_platform_or_architecture_stays_visible_and_ineligible() -> None:
    future = profile(
        platform_family="unknown",
        architecture="unknown",
        platform_variant="vendor-future-os",
        transports=[transport_mapping(family="unknown")],
        runtimes=[runtime_mapping(backend_id="unknown")],
    )
    result = eligibility.derive_eligibility(
        "qualified_model_stage", full_ladder_inputs(future), now=NOW
    )
    assert result.state == "ineligible"
    assert "unknown_platform_ineligible" in result.blockers


def test_synthetic_evidence_cannot_close_a_stage_role() -> None:
    subject = profile()
    synthetic = report(subject, source_kind="synthetic_conformance")
    result = eligibility.derive_eligibility(
        "qualified_model_stage",
        full_ladder_inputs(subject, report=synthetic),
        now=NOW,
    )
    assert result.state == "ineligible"
    assert any(
        blocker.startswith("native_gate_requires_native_evidence:")
        for blocker in result.blockers
    )


def test_artifact_eligibility_cannot_grant_chunks_without_an_assignment_grant() -> None:
    subject = profile()
    artifact_qualification = qualification(subject, role="artifact_source")
    artifact = eligibility.derive_eligibility(
        "artifact_source",
        full_ladder_inputs(
            subject, qualification=artifact_qualification, assignment_grant=None
        ),
        now=NOW,
    )
    assert artifact.state == "ineligible"
    assert "artifact_assignment_grant_missing" in artifact.blockers
    granted = eligibility.derive_eligibility(
        "artifact_source",
        full_ladder_inputs(subject, qualification=artifact_qualification),
        now=NOW,
    )
    assert granted.state == "eligible"
    assert granted.grants_artifact_chunks() is False
    assert granted.grants_placement() is False
    rejection(
        "eligibility_must_not_carry_later_authority",
        lambda: eligibility.PeerEligibility.from_mapping(
            {**granted.to_mapping(), "route_ready": True}
        ),
    )


def test_authority_dimension_independence_rejects_new_evidence_generation() -> None:
    """A new report cannot borrow a prior generation's stage qualification."""
    subject = profile()
    old = qualification(subject)
    newer = report(subject, evidence_generation=old.evidence_generation + 1)
    stage = eligibility.derive_eligibility(
        "qualified_model_stage",
        full_ladder_inputs(subject, report=newer, qualification=old),
        now=NOW,
    )
    assert stage.state == "ineligible"
    assert "class_qualification_evidence_generation_stale" in stage.blockers
    member = eligibility.derive_eligibility(
        "signed_member", full_ladder_inputs(subject, report=newer, qualification=old), now=NOW
    )
    assert member.state == "eligible"
    assert not stage.grants_placement()


@pytest.mark.parametrize(
    "change",
    [
        lambda original: {"platform_family": "linux"},
        lambda original: {"transports": [transport_mapping(build_digest=D("9"))]},
        lambda original: {"runtimes": [runtime_mapping(build_digest=D("8"))]},
    ],
)
def test_authority_dimension_independence_profile_change_withdraws_stage_only(change) -> None:
    original = profile()
    changed = profile(**change(original))
    old = qualification(original)
    stage = eligibility.derive_eligibility(
        "qualified_model_stage",
        full_ladder_inputs(changed, report=report(changed), qualification=old), now=NOW,
    )
    assert stage.state == "ineligible"
    assert "class_qualification_profile_stale" in stage.blockers
    member = eligibility.derive_eligibility(
        "signed_member", full_ladder_inputs(changed, report=report(changed), qualification=old), now=NOW
    )
    assert member.state == "eligible"
    assert stage.grants_placement() is False


def test_role_independence_and_performance_failure_keeps_other_roles() -> None:
    subject = profile()
    draft = eligibility.derive_eligibility(
        "speculative_draft_worker", full_ladder_inputs(subject), now=NOW
    )
    assert draft.state == "ineligible"
    assert "a11_not_closed" in draft.blockers
    mismatched = eligibility.derive_eligibility(
        "performance_ready_model_stage",
        full_ladder_inputs(
            subject,
            qualification=qualification(subject, role="qualified_model_stage"),
            efficiency=efficiency_qualification(
                results={
                    "median_decode_ratio": 1.2,
                    "paired_confidence_lower_bound": 1.1,
                    "median_decode_tokens_per_second": 6.0,
                    "p95_ttft_ms": 1800.0,
                }
            ),
        ),
        now=NOW,
    )
    assert mismatched.state == "ineligible"
    assert "performance_qualification_not_ready" in mismatched.blockers
    # A failed performance gate never revokes unrelated membership or probe roles.
    probe = eligibility.derive_eligibility(
        "probe_contributor", full_ladder_inputs(subject), now=NOW
    )
    assert probe.state == "eligible"


def test_stage_role_is_eligible_only_with_exact_current_class_qualification() -> None:
    subject = profile()
    eligible = eligibility.derive_eligibility(
        "qualified_model_stage", full_ladder_inputs(subject), now=NOW
    )
    assert eligible.state == "eligible"
    assert eligible.blockers == ()
    stale = eligibility.derive_eligibility(
        "qualified_model_stage",
        full_ladder_inputs(
            subject, qualification=qualification(subject, role="performance_ready_model_stage")
        ),
        now=NOW,
    )
    assert stale.state == "ineligible"
    assert "class_qualification_role_mismatch" in stale.blockers
    expired = eligibility.derive_eligibility(
        "qualified_model_stage",
        full_ladder_inputs(
            subject, qualification=qualification(subject, expires_at=NOW - 1.0)
        ),
        now=NOW,
    )
    assert "class_qualification_expired" in expired.blockers


def test_performance_role_requires_current_runtime_efficiency_qualification() -> None:
    subject = profile()
    ready = eligibility.derive_eligibility(
        "performance_ready_model_stage",
        full_ladder_inputs(subject, efficiency=efficiency_qualification()),
        now=NOW,
    )
    assert ready.state == "eligible"
    portable = eligibility.derive_eligibility(
        "performance_ready_model_stage",
        full_ladder_inputs(
            subject,
            efficiency=efficiency_qualification(
                provider_binding={
                    "backend_id": "numpy",
                    "build_digest": D("c"),
                    "device_class": "portable_cpu",
                    "driver_runtime_digest": D("d"),
                }
            ),
        ),
        now=NOW,
    )
    assert portable.state == "ineligible"
    assert "portable_backend_cannot_be_performance_ready" in portable.blockers


def test_enumerated_protocol_map_converts_only_what_the_spec_replaces() -> None:
    migration.assert_no_blanket_suffix_rewrite()
    assert migration.CONTRACT_CONVERSION_MAP == {
        "mycelium.membership.capability_report.v1": capability_report.PROTOCOL
    }
    assert (
        migration.convert_protocol("mycelium.membership.capability_report.v1")
        == capability_report.PROTOCOL
    )
    for protocol in migration.UNCHANGED_PROTOCOLS:
        assert migration.convert_protocol(protocol) == protocol
    # The B1 forbidden pattern: a blanket `.v1` -> `.v2` suffix rewrite.
    rejection(
        "contract_not_in_migration_map",
        lambda: migration.convert_protocol("mycelium.membership.heartbeat.v2"),
    )
    rejection(
        "contract_not_in_migration_map",
        lambda: migration.convert_protocol("mycelium.membership.lease_renewal.v2"),
    )


def test_every_legacy_compound_class_is_enumerated_and_decomposed() -> None:
    assert set(migration.LEGACY_PEER_CLASS_DECOMPOSITION) == set(
        migration.legacy_peer_classes()
    )
    mac = migration.decompose_legacy_peer_class("mac_mlx_iroh")
    assert (mac.identity_kind, mac.platform_family, mac.transports, mac.runtimes) == (
        "native_peer",
        "macos",
        ("iroh",),
        ("mlx",),
    )
    assert mac.architecture == "unknown"
    assert mac.declared_ineligible is False
    android = migration.decompose_legacy_peer_class("android_termux_iroh")
    assert android.declared_ineligible is True
    rejection(
        "legacy_peer_class_unmapped",
        lambda: migration.decompose_legacy_peer_class("windows_onnx_iroh"),
    )


def test_acceleration_profile_requires_exact_operation_evidence() -> None:
    partial = acceleration_mapping(supported_operations=["embedding", "decoder_range"])
    rejection(
        "operation_evidence_missing:final_norm,kv_update,token_selection,vocab_head",
        lambda: acceleration_profile.AccelerationProfile.from_mapping(partial),
    )
    profile_ = acceleration_profile.AccelerationProfile.from_mapping(acceleration_mapping())
    profile_.assert_acceleration_claim()
    assert profile_.correctness_only is False


def test_numpy_and_per_decode_cast_are_correctness_only_never_acceleration() -> None:
    numpy_profile = acceleration_profile.AccelerationProfile.from_mapping(
        acceleration_mapping(backend_id="numpy", execution_provider="cpu")
    )
    rejection(
        "correctness_only_backend_cannot_accelerate",
        lambda: numpy_profile.assert_acceleration_claim(),
    )
    casting = acceleration_profile.AccelerationProfile.from_mapping(
        acceleration_mapping(
            precision_policy={
                "weight_precision": "int8",
                "activation_precisions": ["float32"],
                "per_decode_full_weight_cast": True,
            }
        )
    )
    assert casting.correctness_only is True
    rejection(
        "per_decode_full_weight_cast_is_correctness_only",
        lambda: casting.assert_acceleration_claim(),
    )
    # Windows acceleration is an exact device claim, never an operating-system inference.
    rejection(
        "windows_acceleration_requires_native_x86_64_device_class",
        lambda: acceleration_profile.assert_accelerated_windows_claim(
            numpy_profile, platform_family="windows"
        ),
    )
    rejection(
        "not_a_windows_profile",
        lambda: acceleration_profile.assert_accelerated_windows_claim(
            acceleration_profile.AccelerationProfile.from_mapping(acceleration_mapping()),
            platform_family="macos",
        ),
    )


def test_stage_observation_requires_explicit_vocabulary_head_and_reconciliation() -> None:
    good = observation()
    good.assert_reconciles()
    assert good.vocabulary_head_cost_ms() == 10.0
    drifted = observation_mapping(
        components={
            key: {"time_ms": 10.0, "bytes": 1024, "samples": 24}
            for key in stage_performance.COMPONENT_KEYS
        }
        | {"vocab_head": {"time_ms": 90.0, "bytes": 1024, "samples": 24}}
    )
    drifted["end_to_end_ms"] = 100.0
    drifted["uncertainty_ms"] = 1.0
    rejection(
        "component_totals_do_not_reconcile",
        lambda: stage_performance.StagePerformanceObservation.from_mapping(
            drifted
        ).assert_reconciles(),
    )
    missing_head = observation_mapping()
    missing_head["components"].pop("vocab_head")
    rejection(
        "stage_observation_components_invalid",
        lambda: stage_performance.StagePerformanceObservation.from_mapping(missing_head),
    )


def test_unknown_component_cost_is_never_zero_cost() -> None:
    rejection(
        "component_cost_unknown_must_be_null:vocab_head",
        lambda: stage_performance.StagePerformanceObservation.from_mapping(
            observation_mapping(
                components={
                    key: {"time_ms": 10.0, "bytes": 1024, "samples": 24}
                    for key in stage_performance.COMPONENT_KEYS
                }
                | {"vocab_head": {"time_ms": 0.0, "bytes": 0, "samples": 0}}
            )
        ),
    )
    unknown = stage_performance.StagePerformanceObservation.from_mapping(
        observation_mapping(
            components={
                key: {"time_ms": 10.0, "bytes": 1024, "samples": 24}
                for key in stage_performance.COMPONENT_KEYS
            }
            | {"vocab_head": {"time_ms": None, "bytes": None, "samples": 0}}
        )
    )
    assert unknown.unknown_components() == ("vocab_head",)
    rejection(
        "component_cost_unknown_not_zero:vocab_head",
        lambda: unknown.component_total_ms(),
    )


def test_warm_decode_materialization_must_stay_zero_and_counters_monotonic() -> None:
    stage_performance.assert_zero_decode_materialization(observation())
    rejection(
        "decode_time_full_weight_materialization_detected",
        lambda: stage_performance.assert_zero_decode_materialization(
            observation(
                materialization={
                    "full_weight_materializations": 1,
                    "packed_state_bytes": 1 << 20,
                }
            )
        ),
    )
    previous = observation()
    grown = observation(
        bucket={"context_tokens": 8192, "output_tokens": 4096},
        materialization={"full_weight_materializations": 0, "packed_state_bytes": 1 << 21},
    )
    rejection(
        "packed_state_grows_with_output_tokens",
        lambda: stage_performance.assert_monotonic_materialization(previous, grown),
    )


def test_activation_codec_rejection_matrix_and_no_silent_coercion() -> None:
    for name in activation_codec.REQUIRED_NEGATIVES:
        not_rejected = codec_mapping()
        not_rejected["negatives"][name]["rejected"] = False
        rejection(
            f"codec_negative_not_rejected:{name}",
            lambda mapping=not_rejected: activation_codec.ActivationCodecQualification.from_mapping(
                mapping
            ),
        )
    rejection(
        "codec_output_token_parity_required",
        lambda: activation_codec.ActivationCodecQualification.from_mapping(
            codec_mapping(parity={"output_token_parity": False, "sample_count": 512, "parity_digest": D("3")})
        ),
    )
    rejection(
        "codec_byte_reduction_not_present",
        lambda: activation_codec.ActivationCodecQualification.from_mapping(
            codec_mapping(
                byte_reduction={
                    "bytes_before": 1 << 20,
                    "bytes_after": 1 << 20,
                    "samples": 10,
                    "counter_digest": D("4"),
                }
            )
        ),
    )
    rejection(
        "codec_fallback_requires_independent_qualification",
        lambda: activation_codec.ActivationCodecQualification.from_mapping(
            codec_mapping(fallback={"dtype": "bfloat16", "qualification_digest": None})
        ),
    )


def test_codec_negotiation_is_generation_bound_and_fail_closed() -> None:
    current = codec()
    chosen = activation_codec.negotiate_activation_codec(
        (current,), required_dtype="float16", generation=4, now=NOW
    )
    assert chosen.digest == current.digest
    withdrawn = codec(withdrawal={"state": "withdrawn", "reason": "byte_counters_failed"})
    rejection(
        "activation_codec_unavailable",
        lambda: activation_codec.negotiate_activation_codec(
            (withdrawn,), required_dtype="float16", generation=4, now=NOW
        ),
        exc=activation_codec.CodecUnavailable,
    )
    rejection(
        "activation_codec_unavailable",
        lambda: activation_codec.negotiate_activation_codec(
            (current,), required_dtype="float16", generation=5, now=NOW
        ),
        exc=activation_codec.CodecUnavailable,
    )
    fallback = codec(dtype="bfloat16")
    selected = activation_codec.negotiate_activation_codec(
        (withdrawn, fallback), required_dtype="float16", generation=4, now=NOW
    )
    assert selected.dtype == "bfloat16"


def test_runtime_efficiency_thresholds_are_frozen_and_decisions_are_published() -> None:
    ready = efficiency_qualification()
    assert ready.decision == "performance_ready"
    assert ready.blockers == ()
    weakened = efficiency_mapping(
        benchmark={
            **efficiency_mapping()["benchmark"],
            "thresholds": {
                **dict(runtime_efficiency.THRESHOLDS),
                "min_median_decode_ratio": 1.2,
            },
        }
    )
    rejection(
        "efficiency_threshold_not_frozen:min_median_decode_ratio",
        lambda: runtime_efficiency.decide_efficiency(weakened),
    )
    below_floor = efficiency_qualification(
        results={
            "median_decode_ratio": 1.4,
            "paired_confidence_lower_bound": 1.2,
            "median_decode_tokens_per_second": 7.0,
            "p95_ttft_ms": 1700.0,
        }
    )
    assert below_floor.decision == "not_performance_ready"
    assert "below_single_request_floor" in below_floor.blockers
    assert "interactive_ttft_above_limit" in below_floor.blockers


def test_runtime_efficiency_negatives_fail_closed() -> None:
    assert (
        efficiency_qualification(
            reliability={"failures": 1, "cancellations": 0, "timeouts": 0, "excluded": 0}
        ).decision
        == "not_performance_ready"
    )
    assert (
        efficiency_qualification(
            materialization_proof={
                "warm_decode_full_weight_materializations": 1,
                "warm_decode_windows": 3,
            }
        ).decision
        == "not_performance_ready"
    )
    assert (
        efficiency_qualification(
            results={
                "median_decode_ratio": 2.4,
                "paired_confidence_lower_bound": 1.5,
                "median_decode_tokens_per_second": 12.0,
                "p95_ttft_ms": 900.0,
            }
        ).decision
        == "not_performance_ready"
    )
    assert (
        efficiency_qualification(
            benchmark={
                **efficiency_mapping()["benchmark"],
                "generated_tokens_per_mode": 128,
            }
        ).decision
        == "not_performance_ready"
    )
    assert (
        efficiency_qualification(
            benchmark={**efficiency_mapping()["benchmark"], "thermal_state": "critical"}
        ).decision
        == "not_performance_ready"
    )
    rejection(
        "excluded_failures_not_allowed",
        lambda: efficiency_qualification(
            reliability={"failures": 0, "cancellations": 0, "timeouts": 0, "excluded": 2}
        ),
    )


def test_efficiency_decision_cannot_be_declared_without_the_readings() -> None:
    declared = efficiency_mapping()
    declared["decision"] = {"state": "performance_ready", "blockers": []}
    declared["results"] = {
        "median_decode_ratio": 1.1,
        "paired_confidence_lower_bound": 1.0,
        "median_decode_tokens_per_second": 5.0,
        "p95_ttft_ms": 2000.0,
    }
    rejection(
        "efficiency_decision_incoherent",
        lambda: runtime_efficiency.RuntimeEfficiencyQualification.from_mapping(declared),
    )


def test_efficiency_readiness_withdraws_on_provider_change() -> None:
    ready = efficiency_qualification()
    assert (
        ready.is_current(
            now=NOW,
            evidence_generation=2,
            provider_build_digest=D("c"),
            driver_runtime_digest=D("d"),
            device_class="apple_arm64_gpu",
        )
        is True
    )
    assert (
        ready.is_current(
            now=NOW,
            evidence_generation=3,
            provider_build_digest=D("c"),
            driver_runtime_digest=D("d"),
            device_class="apple_arm64_gpu",
        )
        is False
    )
    assert (
        ready.is_current(
            now=NOW,
            evidence_generation=2,
            provider_build_digest=D("8"),
            driver_runtime_digest=D("d"),
            device_class="apple_arm64_gpu",
        )
        is False
    )
