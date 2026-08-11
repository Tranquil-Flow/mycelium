from __future__ import annotations

import copy

import pytest

from mycelium_m21_heterogeneous import (
    build_heterogeneous_evidence,
    endpoint_identity_digest,
    pseudonymous_member_id,
    validate_heterogeneous_evidence,
)


DIGEST = "sha256:" + "a" * 64


def _member(
    node: str,
    peer_class: str,
    runtime: str,
    *,
    eligible: bool,
    route: bool,
    external: bool = False,
) -> dict[str, object]:
    return {
        "member_id": pseudonymous_member_id(node, salt="test-salt"),
        "peer_class": peer_class,
        "runtime_backend": runtime,
        "trust_state": "invited",
        "generation": 2,
        "incarnation": f"{node}-incarnation",
        "freshness": "fresh",
        "revocation_state": "active",
        "activation_eligible": eligible,
        "route_participant": route,
        "eligibility_reason": "eligible" if eligible else "activation_protocol_unavailable",
        "connectivity": "direct" if route else "unknown",
        "external_network": external,
        "endpoint_identity_digest": endpoint_identity_digest(f"{node}-endpoint"),
    }


def _evidence() -> dict[str, object]:
    members = [
        _member("mac", "mac_mlx_iroh", "mlx", eligible=True, route=True),
        _member(
            "linux",
            "linux_numpy_iroh",
            "numpy",
            eligible=True,
            route=True,
            external=True,
        ),
        _member("browser", "browser_http", "browser", eligible=False, route=False),
    ]
    return build_heterogeneous_evidence(
        generated_at_unix_ms=1_000,
        binding={
            "swarm_id": "swarm-a",
            "seed_key_digest": DIGEST,
            "seed_node_id": "seed-a",
            "deployment_id": "deployment-a",
            "model_id": "Qwen/model",
            "model_revision": "revision-a",
            "membership_generation": 2,
        },
        policy={
            "invitation_ownership": "owner_only",
            "operator_approval": "required",
            "maximum_invite_ttl_seconds": 600,
            "single_use": True,
            "request_quota_per_hour": 60,
            "byte_quota_per_hour": 1_073_741_824,
            "audit_retention_days": 30,
            "revocation_supported": True,
            "credential_rotation_supported": True,
            "abuse_response": "revoke_then_rotate",
            "permissionless_participation": False,
            "byzantine_resistance": False,
            "malicious_worker_confidentiality": False,
        },
        members=members,
        paths=(
            {
                "source_member_id": members[0]["member_id"],
                "destination_member_id": members[1]["member_id"],
                "path_class": "direct",
                "relay_region": None,
                "cold_rtt_ms": 20.0,
                "warm_rtt_ms": 12.0,
                "jitter_ms": 1.0,
                "loss_ratio": 0.0,
                "goodput_bytes_per_second": 30_000.0,
                "reconnect_count": 0,
                "connection_generation": 1,
                "selected_path_changes": 1,
                "sample_count": 5,
            },
        ),
        route={
            "physical": True,
            "route_alive": True,
            "heterogeneous": True,
            "participant_count": 2,
            "runtime_class_count": 2,
            "frame_count_before": 10,
            "frame_count_after": 20,
            "latest_output_token_count": 3,
            "tailscale_product_dependency": False,
            "activation_transport": "endpointid_authenticated_iroh",
            "operator_staging_transport": "ssh_or_tailscale_optional",
        },
    )


def test_heterogeneous_route_and_ineligible_probe_qualify() -> None:
    evidence = validate_heterogeneous_evidence(_evidence())
    assert evidence["gate_state"] == "qualified"
    assert {member["runtime_backend"] for member in evidence["members"] if member["route_participant"]} == {"mlx", "numpy"}
    browser = next(member for member in evidence["members"] if member["peer_class"] == "browser_http")
    assert browser["activation_eligible"] is False
    assert browser["route_participant"] is False
    assert evidence["route"]["tailscale_product_dependency"] is False


def test_ineligible_member_cannot_be_route_participant() -> None:
    evidence = _evidence()
    evidence["members"][2]["route_participant"] = True
    with pytest.raises(ValueError, match="m21_evidence_invalid"):
        validate_heterogeneous_evidence(evidence)


def test_one_runtime_class_withholds_gate() -> None:
    evidence = _evidence()
    linux = evidence["members"][1]
    linux["route_participant"] = False
    linux["connectivity"] = "unknown"
    evidence["route"]["participant_count"] = 1
    evidence["route"]["runtime_class_count"] = 1
    evidence["route"]["heterogeneous"] = False
    del evidence["evidence_digest"]
    rebuilt = build_heterogeneous_evidence(
        generated_at_unix_ms=evidence["generated_at_unix_ms"],
        binding=evidence["binding"],
        policy=evidence["policy"],
        members=evidence["members"],
        paths=evidence["paths"],
        route=evidence["route"],
        exclusions=evidence["exclusions"],
    )
    assert rebuilt["gate_state"] == "withheld"


def test_private_unknown_and_identity_leaks_fail_closed() -> None:
    evidence = _evidence()
    private = copy.deepcopy(evidence)
    private["members"][0]["endpoint_id"] = "raw-endpoint"
    with pytest.raises(ValueError, match="m21_evidence_invalid"):
        validate_heterogeneous_evidence(private)
    unknown = copy.deepcopy(evidence)
    unknown["invite_token"] = "secret"
    with pytest.raises(ValueError, match="m21_evidence_invalid"):
        validate_heterogeneous_evidence(unknown)


def test_class_spoof_and_cross_principal_collision_are_rejected() -> None:
    evidence = _evidence()
    spoof = copy.deepcopy(evidence)
    spoof["members"][2]["peer_class"] = "linux_numpy_iroh"
    spoof["members"][2]["activation_eligible"] = True
    spoof["members"][2]["route_participant"] = True
    spoof["members"][2]["runtime_backend"] = "browser"
    with pytest.raises(ValueError, match="m21_evidence_invalid"):
        validate_heterogeneous_evidence(spoof)
    collision = copy.deepcopy(evidence)
    collision["members"][2]["member_id"] = collision["members"][0]["member_id"]
    with pytest.raises(ValueError, match="m21_evidence_invalid"):
        validate_heterogeneous_evidence(collision)


@pytest.mark.parametrize(
    ("mutation"),
    (
        lambda evidence: evidence["route"].update(participant_count=99),
        lambda evidence: evidence["route"].update(runtime_class_count=1),
        lambda evidence: evidence["route"].update(heterogeneous=False),
        lambda evidence: evidence.update(gate_state="withheld"),
        lambda evidence: evidence["paths"][0].update(loss_ratio=1.01),
        lambda evidence: evidence["paths"][0].update(
            destination_member_id=evidence["paths"][0]["source_member_id"]
        ),
    ),
)
def test_derived_route_and_gate_claims_fail_closed(mutation) -> None:
    evidence = _evidence()
    mutation(evidence)
    with pytest.raises(ValueError, match="m21_evidence_invalid"):
        validate_heterogeneous_evidence(evidence)
