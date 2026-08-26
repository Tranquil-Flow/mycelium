# SPDX-License-Identifier: AGPL-3.0-or-later
"""Peer-required physical-case execution paths (spec §11).

These pins prove the runner can execute the peer-required cases for real
when genuine peer inputs exist, and still fails closed with
``peer_required`` when they do not. The peer here is a loopback stand-in:
that is enough to pin the execution path, and is never itself gate
evidence. A sealed physical result still requires the unrelated-network
peer described in ``docs/handover/A8_INFRA_REQUIREMENTS.md``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Any

import pytest

from mycelium_node.identity import load_or_create_node_signer
from mycelium_node.membership import NodeMembershipSession
from mycelium_internet.activation import RelayProjector
from mycelium_internet.contracts import validate_internet_native_qualification
from mycelium_internet.physical import (
    PeerRequired,
    PhysicalGateError,
    _browser_signature_statement,
    _verified_browser_observation,
    _verified_transport_reports,
    execute_case as _execute_case,
    seal_qualification,
)
from mycelium_qualification.signing import generate_ed25519_signer

from .test_physical_seed_side_cases import ORIGIN, SeedEnv, _ids

TEST_SOURCE_BINDING = json.loads(
    (Path(__file__).with_name("inventory.v1.json")).read_text("utf-8")
)["physical_execution"]["source_digest"]


def execute_case(case_id: str, **kwargs: Any) -> dict[str, Any]:
    return _execute_case(
        case_id,
        spec_digest=TEST_SOURCE_BINDING,
        source_digest=TEST_SOURCE_BINDING,
        **kwargs,
    )

CLEAN_PEER_NETWORK = {
    "tailscale_binary_present": False,
    "tailnet_interface_present": False,
    "tailnet_addresses": [],
}
CLEAN_PROCESS_AUDIT = {
    "ssh_invocations": 0,
    "ssh_client_present": False,
    "ssh_server_present": False,
}


def _revocation_connection_before(node_id: str) -> dict[str, object]:
    return {
        "protocol": "mycelium.a8_revocation_connection_before.v1",
        "case_id": "revoked_active_member",
        "member_id": node_id,
        "spec_digest": TEST_SOURCE_BINDING,
        "source_digest": TEST_SOURCE_BINDING,
        "observed_at_unix_ms": int(time.time() * 1_000),
        "probe_session_id": "revocation-session-test",
        "transport": "iroh",
        "authenticated_connection": True,
        "activation_attempts": 1,
        "activation_admissions": 1,
        "path_class": "direct",
        "connection_generation": 1,
    }


def _revocation_connection_after(
    node_id: str, before_report: dict[str, object]
) -> dict[str, object]:
    before_digest = "sha256:" + hashlib.sha256(
        json.dumps(
            before_report,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "protocol": "mycelium.a8_revocation_connection_after.v1",
        "case_id": "revoked_active_member",
        "member_id": node_id,
        "spec_digest": TEST_SOURCE_BINDING,
        "source_digest": TEST_SOURCE_BINDING,
        "observed_at_unix_ms": int(time.time() * 1_000),
        "probe_session_id": before_report["probe_session_id"],
        "before_evidence_digest": before_digest,
        "transport": "iroh",
        "activation_attempts": 1,
        "activation_admissions": 0,
        "connection_state": "closed",
        "incident_detail": "bounded",
    }

def _membership_visibility_probe(node_id: str) -> dict[str, object]:
    return {
        "protocol": "mycelium.a8_membership_visibility_probe.v1",
        "case_id": "unrelated_https_invite_without_tailscale",
        "member_id": node_id,
        "observed_at_unix_ms": int(time.time() * 1_000),
        "member_visible": True,
        "activation_eligible": False,
    }


def _endpoint_admission_observation(
    node_id: str,
    expected_endpoint_id: str,
    dialed_endpoint_id: str,
    *,
    challenge: str,
    identity_rejections: int,
    admitted_frames: int,
) -> dict[str, object]:
    observed_at = int(time.time() * 1_000)
    observation = {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": "inbound_admission_snapshot",
        "monotonic_ns": observed_at * 1_000_000,
        "run_id": "run-a8",
        "deployment_id": "deployment-a8",
        "node_id": "receiver-node",
        "host_id": "receiver-host",
        "process_id": 123,
        "endpoint_id": "endpoint-a",
        "peer_generation": 1,
        "state": "RUNNING",
        "route_ready": False,
        "details": {
            "protocol": "mycelium.physical_node.inbound_admission_evidence.v1",
            "case_id": "endpoint_identity_mismatch",
            "member_id": node_id,
            "spec_digest": TEST_SOURCE_BINDING,
            "source_digest": TEST_SOURCE_BINDING,
            "sidecar_binary_digest": TEST_SOURCE_BINDING,
            "challenge": challenge,
            "expected_endpoint_id": expected_endpoint_id,
            "dialed_endpoint_id": dialed_endpoint_id,
            "expected_peer_path_class": "unknown",
            "admission": {
                "protocol": "mycelium.iroh_sidecar.inbound_admission.v1",
                "inbound_identity_rejections": identity_rejections,
                "inbound_frames_admitted": admitted_frames,
                "candidate_identity_rejections": identity_rejections,
                "measured_at_unix_ms": observed_at,
            },
        },
    }
    return {
        "observation": observation,
        "signature": TRANSPORT_SIGNER.sign(observation),
        "verification_key": TRANSPORT_SIGNER.public_key_record(),
    }


def _endpoint_activation_probe(
    node_id: str, expected_endpoint_id: str, dialed_endpoint_id: str
) -> dict[str, object]:
    challenge = "a8-endpoint-mismatch-challenge"
    return {
        "protocol": "mycelium.a8_endpoint_activation_probe.v2",
        "case_id": "endpoint_identity_mismatch",
        "member_id": node_id,
        "spec_digest": TEST_SOURCE_BINDING,
        "source_digest": TEST_SOURCE_BINDING,
        "sidecar_binary_digest": TEST_SOURCE_BINDING,
        "challenge": challenge,
        "before": _endpoint_admission_observation(
            node_id,
            expected_endpoint_id,
            dialed_endpoint_id,
            challenge=challenge,
            identity_rejections=3,
            admitted_frames=7,
        ),
        "after": _endpoint_admission_observation(
            node_id,
            expected_endpoint_id,
            dialed_endpoint_id,
            challenge=challenge,
            identity_rejections=4,
            admitted_frames=7,
        ),
    }


def _supported_path_probe(node_id: str, case_id: str) -> dict[str, object]:
    return {
        "protocol": "mycelium.a8_supported_path_probe.v1",
        "case_id": case_id,
        "member_id": node_id,
        "observed_at_unix_ms": int(time.time() * 1_000),
        "enrollment_completed": True,
        "artifact_manifest_verified": True,
        "activation_completed": True,
        "serving_requests_completed": 1,
        "transport_path_class": "direct",
        "tailscale_used": False,
        "ssh_used": False,
        "artifact_transport": "signed_product_path",
    }
TRANSPORT_SIGNER = generate_ed25519_signer(endpoint_id="endpoint-a")
BROWSER_SIGNER = generate_ed25519_signer(endpoint_id="a8-browser-collector")


def _browser_authority(
    case_id: str = "direct_path_qualified_browser_inference",
    request_count: int = 1,
) -> dict[str, object]:
    challenge_id = "sha256:" + hashlib.sha256(
        f"{case_id}:{request_count}:browser-challenge".encode()
    ).hexdigest()
    now = int(time.time() * 1_000)
    return {
        "protocol": "mycelium.a8_browser_observation_authority.v2",
        "signer_id": "a8-browser-collector",
        "verification_keys": [BROWSER_SIGNER.public_key_record()],
        "challenge_id": challenge_id,
        "case_id": case_id,
        "origin": ORIGIN,
        "deployment_id": "deployment-a8",
        "spec_digest": TEST_SOURCE_BINDING,
        "source_digest": TEST_SOURCE_BINDING,
        "request_count": request_count,
        "issued_at_unix_ms": now - 1_000,
        "expires_at_unix_ms": now + 299_000,
    }


@pytest.fixture()
def env(tmp_path: Path):
    environment = SeedEnv(tmp_path)
    yield environment
    environment.close()


def _enrolled(env: SeedEnv, node_id: str):
    """Join one member over the live boundary and return its pieces."""

    adapter = env.adapter()
    node = env.node(node_id)
    join_envelope = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=[f"https://{node_id}/control"],
    )
    adapter.preflight(now=time.time())
    return adapter, node, join_envelope


def _impostor_node(env: SeedEnv, node_id: str) -> NodeMembershipSession:
    """Same node_id and swarm, different endpoint identity and signing key.

    This is the shape the endpoint-mismatch gate is about: a well-formed
    control envelope for an accepted member, signed by an identity the seed
    never accepted for it.
    """

    return NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(
            env.tmp_path / f"{node_id}-impostor.key",
            endpoint_id=f"{node_id}-impostor-endpoint",
        ),
        incarnation=f"{node_id}-impostor-incarnation",
        software_version="mycelium-test",
        peer_class="mac_mlx_iroh",
        runtime_capability={
            "runtime_backend": "mlx",
            "transport": "iroh",
            "activation_protocol": "mycelium.router_wire.v1",
        },
        clock=time.time,
        id_source=_ids(f"{node_id}-impostor"),
    )


def _unqualified_node(env: SeedEnv, node_id: str) -> NodeMembershipSession:
    """An enrollable browser member with no activation authority."""

    return NodeMembershipSession(
        node_id=node_id,
        swarm_id="swarm-a8",
        seed_node_id="seed-node",
        signer=load_or_create_node_signer(
            env.tmp_path / f"{node_id}-unqualified.key",
            endpoint_id=f"{node_id}-unqualified-endpoint",
        ),
        incarnation=f"{node_id}-unqualified-incarnation",
        software_version="mycelium-test",
        peer_class="browser_http",
        runtime_capability={
            "runtime_backend": "browser",
            "transport": "http",
            "activation_protocol": None,
        },
        clock=time.time,
        id_source=_ids(f"{node_id}-unqualified"),
    )


def _unqualified_authority_report(node_id: str) -> dict[str, object]:
    return {
        "protocol": "mycelium.unqualified_member_authority_probe.v1",
        "member_id": node_id,
        "member_visible": True,
        "activation_eligible": False,
        "authority_attempts": {
            "artifact": "rejected",
            "placement": "rejected",
            "activation": "rejected",
            "selection": "rejected",
            "inference": "rejected",
        },
        "forbidden_side_effects": {
            "artifact_disclosed": False,
            "placement_created": False,
            "deployment_selected": False,
        },
        "prompt_deliveries": 0,
    }


def _transport_observation(
    *,
    path_class: str,
    generation: int,
    measured_at: int,
    relay_identity: str | None = None,
    reconnect_count: int = 0,
    selected_path_changes: int = 0,
) -> dict[str, object]:
    return {
        "protocol": "mycelium.transport_path_observation.v1",
        "local_node_id": "node-a",
        "local_endpoint_id": "endpoint-a",
        "remote_node_id": "node-b",
        "remote_endpoint_id": "endpoint-b",
        "connection_generation": generation,
        "path_class": path_class,
        "relay_identity": relay_identity,
        "relay_region": "unknown" if relay_identity is not None else None,
        "cold_rtt_ms": 31,
        "warm_rtt_ms": 19,
        "observed_goodput_Bps": 12_000,
        "jitter_ms": 2,
        "loss_ratio": 0.0,
        "sample_count": 8,
        "connections_opened": 1 + reconnect_count,
        "frames_sent": 8 + reconnect_count,
        "reconnect_count": reconnect_count,
        "selected_path_changes": selected_path_changes,
        "measurement_source": "iroh_activation_plane",
        "measured_at_unix_ms": measured_at,
        "fresh_until_unix_ms": measured_at + 7_200_000,
        "exclusions": [],
    }


def _signed_swarm_report(
    path: dict[str, object], *, signer=TRANSPORT_SIGNER
) -> dict[str, object]:
    observation = {
        "protocol": "mycelium.physical_node_observation.v1",
        "event": "snapshot",
        "monotonic_ns": 1,
        "run_id": "run-a8",
        "deployment_id": "deployment-a8",
        "node_id": "node-a",
        "host_id": "host-a",
        "process_id": 123,
        "endpoint_id": "endpoint-a",
        "peer_generation": 1,
        "state": "RUNNING",
        "route_ready": False,
        "details": {"transport": {"transport_path_observations": [path]}},
    }
    return {
        "protocol": "mycelium.live_swarm_resource_observations.v1",
        "captured_at_unix_ms": path["measured_at_unix_ms"],
        "deployment_id": "deployment-a8",
        "model_id": "model-a8",
        "resolved_commit": "a" * 40,
        "placement": {"nodes": [{"node_id": "node-a"}, {"node_id": "node-b"}]},
        "topology": None,
        "signed_snapshots": [{
            "observation": observation,
            "signature": signer.sign(observation),
            "verification_key": signer.public_key_record(),
        }],
        "route_ready": False,
    }


def _transport_authority() -> dict[str, object]:
    return {
        "protocol": "mycelium.a8_transport_authority.v1",
        "deployment_id": "deployment-a8",
        "endpoints": [
            {
                "endpoint_id": "endpoint-a",
                "verification_key_digest": TRANSPORT_SIGNER.public_key_record()[
                    "verification_key_digest"
                ],
            },
            {
                "endpoint_id": "endpoint-b",
                "verification_key_digest": "sha256:" + "b" * 64,
            },
        ],
    }


def _projected_metric(value: object) -> int:
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return max(0, int(round(float(value))))


def _activation(path: dict[str, object]) -> dict[str, object]:
    endpoint_pseudonym = "sha256:" + hashlib.sha256(b"endpoint-b").hexdigest()
    observed_at = int(path["measured_at_unix_ms"])
    return {
        "protocol": "mycelium.internet_activation_observation.v1",
        "observation_id": f"observation-{path['connection_generation']}",
        "connection_generation": path["connection_generation"],
        "connection_reuse": int(path["frames_sent"]) - int(path["connections_opened"]),
        "path_class": path["path_class"],
        "path_source": "bound_live_connection",
        "endpoint_pseudonym": endpoint_pseudonym,
        "observed_at_unix_ms": observed_at,
        "freshness": "current",
        "evidence_lifetime_until_unix_ms": observed_at + 90_000,
        "metrics": {
            "rtt_ms": _projected_metric(path["cold_rtt_ms"]),
            "warm_rtt_ms": _projected_metric(path["warm_rtt_ms"]),
            "jitter_ms": _projected_metric(path["jitter_ms"]),
            "goodput_bytes_per_second": _projected_metric(
                path["observed_goodput_Bps"]
            ),
            "loss_ratio": path["loss_ratio"],
            "sample_count": path["sample_count"],
            "measured_zero": path["loss_ratio"] == 0.0,
        },
    }


def _browser_report(
    *,
    paths: list[dict[str, object]],
    projection_key: bytes,
    case_id: str = "direct_path_qualified_browser_inference",
) -> dict[str, object]:
    current = paths[-1]
    relay_projection = None
    if current["path_class"] == "relay":
        relay_projection = {
            "protocol": "mycelium.relay_projection.v1",
            "relay_reference": RelayProjector(
                projection_key=projection_key
            ).reference(str(current["relay_identity"])),
            "region": "unknown",
            "projection_generation": current["connection_generation"],
            "stable": True,
            "observed_at_unix_ms": current["measured_at_unix_ms"],
        }
    authority = _browser_authority(case_id, len(paths))
    transport_reports = [_signed_swarm_report(path) for path in paths]
    observation = {
        "protocol": "mycelium.a8_product_browser_observation.v2",
        "origin": ORIGIN,
        "challenge_id": authority["challenge_id"],
        "case_id": case_id,
        "deployment_id": "deployment-a8",
        "spec_digest": TEST_SOURCE_BINDING,
        "source_digest": TEST_SOURCE_BINDING,
        "observed_at_unix_ms": current["measured_at_unix_ms"],
        "passed": True,
        "browser_failures": 0,
        "completed_requests": len(paths),
        "request_ids": [f"request-{index}" for index in range(1, len(paths) + 1)],
        "terminal_states": ["completed" for _ in paths],
        "transport_report_digests": [
            "sha256:" + hashlib.sha256(
                json.dumps(
                    report,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest()
            for report in transport_reports
        ],
        "workspaces": [
            "lab", "network", "plans", "incidents",
            "readiness", "nodes", "settings", "inference",
        ],
        "public_projection": {
            "activation_observation": _activation(current),
            "activation_history": [_activation(path) for path in paths],
            "relay_projection": relay_projection,
        },
    }
    return {
        "protocol": "mycelium.a8_product_browser_observation_envelope.v2",
        "observation": observation,
        "signature": BROWSER_SIGNER.sign(_browser_signature_statement(observation)),
    }


def _verify_browser_envelope(
    envelope: dict[str, object],
    authority: dict[str, object],
    *,
    case_id: str,
    transport_digests: list[str],
    now_unix_ms: int,
) -> dict[str, Any]:
    return _verified_browser_observation(
        envelope,
        authority,
        expected_case_id=case_id,
        expected_origin=ORIGIN,
        expected_deployment_id="deployment-a8",
        expected_spec_digest=TEST_SOURCE_BINDING,
        expected_source_digest=TEST_SOURCE_BINDING,
        expected_transport_report_digests=transport_digests,
        now_unix_ms=now_unix_ms,
    )


def test_browser_observation_rejects_replay_under_fresh_challenge() -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(path_class="direct", generation=1, measured_at=now)
    envelope = _browser_report(paths=[path], projection_key=b"k" * 32)
    authority = _browser_authority()
    observation = envelope["observation"]
    assert isinstance(observation, dict)
    digests = observation["transport_report_digests"]
    assert isinstance(digests, list)
    fresh_authority = dict(authority)
    fresh_authority["challenge_id"] = "sha256:" + "f" * 64

    with pytest.raises(PhysicalGateError) as exc_info:
        _verify_browser_envelope(
            envelope,
            fresh_authority,
            case_id="direct_path_qualified_browser_inference",
            transport_digests=digests,
            now_unix_ms=now,
        )

    assert exc_info.value.code == "browser_observation_invalid"


def test_browser_observation_rejects_transport_digest_substitution() -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(path_class="direct", generation=1, measured_at=now)
    envelope = _browser_report(paths=[path], projection_key=b"k" * 32)

    with pytest.raises(PhysicalGateError) as exc_info:
        _verify_browser_envelope(
            envelope,
            _browser_authority(),
            case_id="direct_path_qualified_browser_inference",
            transport_digests=["sha256:" + "e" * 64],
            now_unix_ms=now,
        )

    assert exc_info.value.code == "browser_observation_invalid"


def test_browser_observation_rejects_duplicate_request_ids() -> None:
    now = int(time.time() * 1_000)
    paths = [
        _transport_observation(path_class="direct", generation=1, measured_at=now),
        _transport_observation(
            path_class="relay",
            generation=2,
            measured_at=now + 1,
            relay_identity="https://relay.invalid",
            reconnect_count=1,
            selected_path_changes=1,
        ),
    ]
    envelope = _browser_report(
        paths=paths,
        projection_key=b"k" * 32,
        case_id="observed_path_transition_and_reconnect",
    )
    observation = envelope["observation"]
    assert isinstance(observation, dict)
    observation["request_ids"] = ["request-1", "request-1"]
    envelope["signature"] = BROWSER_SIGNER.sign(
        _browser_signature_statement(observation)
    )
    digests = observation["transport_report_digests"]
    assert isinstance(digests, list)

    with pytest.raises(PhysicalGateError) as exc_info:
        _verify_browser_envelope(
            envelope,
            _browser_authority("observed_path_transition_and_reconnect", 2),
            case_id="observed_path_transition_and_reconnect",
            transport_digests=digests,
            now_unix_ms=now,
        )

    assert exc_info.value.code == "browser_observation_invalid"


def test_browser_observation_rejects_v1_envelope_downgrade() -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(path_class="direct", generation=1, measured_at=now)
    envelope = _browser_report(paths=[path], projection_key=b"k" * 32)
    envelope["protocol"] = "mycelium.a8_product_browser_observation_envelope.v1"
    observation = envelope["observation"]
    assert isinstance(observation, dict)
    digests = observation["transport_report_digests"]
    assert isinstance(digests, list)

    with pytest.raises(PhysicalGateError) as exc_info:
        _verify_browser_envelope(
            envelope,
            _browser_authority(),
            case_id="direct_path_qualified_browser_inference",
            transport_digests=digests,
            now_unix_ms=now,
        )

    assert exc_info.value.code == "browser_observation_signature_invalid"


# -- fail-closed without peer inputs ---------------------------------------


@pytest.mark.parametrize(
    "case_id",
    [
        "unrelated_https_invite_without_tailscale",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "tailscale_unavailable",
        "ssh_unavailable",
    ],
)
def test_peer_cases_still_fail_closed_without_inputs(
    env: SeedEnv, case_id: str
) -> None:
    with pytest.raises(PeerRequired) as exc_info:
        execute_case(
            case_id,
            origin=ORIGIN,
            evidence_root=None,
            adapter=env.adapter(),
            case_inputs=None,
        )
    assert exc_info.value.code == "peer_required"


@pytest.mark.parametrize(
    "case_id",
    [
        "unrelated_https_invite_without_tailscale",
        "endpoint_identity_mismatch",
        "tailscale_unavailable",
        "ssh_unavailable",
    ],
)
def test_peer_claim_case_requires_live_case_probe(
    env: SeedEnv, case_id: str
) -> None:
    adapter, node, join_envelope = _enrolled(env, f"peer-no-probe-{case_id}")
    case_inputs: dict[str, object] = {
        "node": node,
        "join_envelope": join_envelope,
    }
    if case_id == "unrelated_https_invite_without_tailscale":
        case_inputs["peer_network"] = CLEAN_PEER_NETWORK
    elif case_id == "endpoint_identity_mismatch":
        case_inputs["mismatched_node"] = _impostor_node(env, node.node_id)
    elif case_id == "tailscale_unavailable":
        case_inputs["peer_network_before"] = CLEAN_PEER_NETWORK
        case_inputs["peer_network_after"] = CLEAN_PEER_NETWORK
    else:
        case_inputs["peer_process_audit"] = CLEAN_PROCESS_AUDIT

    with pytest.raises(PeerRequired) as exc_info:
        execute_case(
            case_id,
            origin=ORIGIN,
            evidence_root=None,
            adapter=adapter,
            case_inputs=case_inputs,
        )
    assert exc_info.value.code == "peer_required"


# -- positive gate 1 --------------------------------------------------------


def test_unrelated_invite_without_tailscale_executes_with_peer_inputs(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-positive")
    document = execute_case(
        "unrelated_https_invite_without_tailscale",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _membership_visibility_probe(node.node_id),
            "peer_network": CLEAN_PEER_NETWORK,
        },
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["gate_kind"] == "physical_positive"
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "seed_pin_verified" in outcomes
    assert "invite_redeemed" in outcomes
    assert "membership_renewed" in outcomes
    assert "signed_member_visible_but_ineligible" in outcomes
    assert "https_bootstrap_succeeds" in outcomes
    assert "no_tailnet_path_observed" in outcomes
    assert len(document["evidence_digests"]) == 1


def test_unrelated_invite_fails_when_a_tailnet_path_is_present(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-tainted")
    document = execute_case(
        "unrelated_https_invite_without_tailscale",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _membership_visibility_probe(node.node_id),
            "peer_network": {
                "tailscale_binary_present": True,
                "tailnet_interface_present": True,
                "tailnet_addresses": ["100.64.0.1"],
            },
        },
    )
    assert document["result"] == "failed"
    assert "tailnet_path_present" in document["public_projection"]["outcomes"]


# -- revoked_active_member --------------------------------------------------


def test_revoked_active_member_refuses_control_after_revocation(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-revoked")

    def revoke() -> None:
        member = env.coordinator.members()[0]
        env.coordinator.advance_member_generation(
            node_id=member["node_id"],
            expected_generation=int(member["generation"]),
            lifecycle_state="STOPPED",
        )

    document = execute_case(
        "revoked_active_member",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "revoke": revoke,
            "case_probe_before": lambda: _revocation_connection_before(node.node_id),
            "case_probe_after": lambda before: _revocation_connection_after(
                node.node_id, before
            ),
        },
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "member_enrolled_before_revocation" in outcomes
    assert "control_refused_after_revocation" in outcomes
    assert "revoked_control_rejected" in outcomes
    assert "revoked_connection_removed_from_activation_admission" in outcomes
    assert "revocation_incident_bounded" in outcomes
    assert len(document["evidence_digests"]) == 2


def test_revoked_active_member_brackets_revocation_with_live_activation_probes(
    env: SeedEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    monkeypatch.setattr(
        physical, "_verify_default_source_binding", lambda expected: expected
    )
    adapter, node, join_envelope = _enrolled(env, "peer-node-revoked-lifecycle")
    events: list[str] = []
    probe_session_id = "revocation-session-1"

    def probe_before() -> dict[str, object]:
        events.append("activation_before")
        return {
            "protocol": "mycelium.a8_revocation_connection_before.v1",
            "case_id": "revoked_active_member",
            "member_id": node.node_id,
            "spec_digest": TEST_SOURCE_BINDING,
            "source_digest": TEST_SOURCE_BINDING,
            "observed_at_unix_ms": int(time.time() * 1_000),
            "probe_session_id": probe_session_id,
            "transport": "iroh",
            "authenticated_connection": True,
            "activation_attempts": 1,
            "activation_admissions": 1,
            "path_class": "direct",
            "connection_generation": 1,
        }

    def revoke() -> None:
        assert events == ["activation_before"]
        events.append("revoked")
        member = env.coordinator.members()[0]
        env.coordinator.advance_member_generation(
            node_id=member["node_id"],
            expected_generation=int(member["generation"]),
            lifecycle_state="STOPPED",
        )

    def probe_after(before_report: dict[str, object]) -> dict[str, object]:
        assert events == ["activation_before", "revoked"]
        events.append("activation_after")
        return {
            "protocol": "mycelium.a8_revocation_connection_after.v1",
            "case_id": "revoked_active_member",
            "member_id": node.node_id,
            "spec_digest": TEST_SOURCE_BINDING,
            "source_digest": TEST_SOURCE_BINDING,
            "observed_at_unix_ms": int(time.time() * 1_000),
            "probe_session_id": probe_session_id,
            "before_evidence_digest": "sha256:"
            + hashlib.sha256(
                json.dumps(
                    before_report,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            "transport": "iroh",
            "activation_attempts": 1,
            "activation_admissions": 0,
            "connection_state": "closed",
            "incident_detail": "bounded",
        }

    document = execute_case(
        "revoked_active_member",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "revoke": revoke,
            "case_probe_before": probe_before,
            "case_probe_after": probe_after,
        },
    )

    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    assert events == ["activation_before", "revoked", "activation_after"]
    assert len(document["evidence_digests"]) == 2


def test_revoked_active_member_requires_live_connection_probe(env: SeedEnv) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-revoked-no-probe")

    def revoke() -> None:
        member = env.coordinator.members()[0]
        env.coordinator.advance_member_generation(
            node_id=member["node_id"],
            expected_generation=int(member["generation"]),
            lifecycle_state="STOPPED",
        )

    with pytest.raises(PeerRequired) as exc_info:
        execute_case(
            "revoked_active_member",
            origin=ORIGIN,
            evidence_root=None,
            adapter=adapter,
            case_inputs={
                "join_envelope": join_envelope,
                "node": node,
                "revoke": revoke,
            },
        )
    assert exc_info.value.code == "peer_required"


def test_revoked_member_awaits_out_of_band_revocation(env: SeedEnv) -> None:
    """The peer cannot reach the seed's admin plane, so it waits for the
    revocation instead of causing it. Here the revocation lands mid-window."""

    adapter, node, join_envelope = _enrolled(env, "peer-node-awaited")

    def revoke_later() -> None:
        time.sleep(0.3)
        member = env.coordinator.members()[0]
        env.coordinator.advance_member_generation(
            node_id=member["node_id"],
            expected_generation=int(member["generation"]),
            lifecycle_state="STOPPED",
        )

    worker = threading.Thread(target=revoke_later, daemon=True)
    worker.start()
    document = execute_case(
        "revoked_active_member",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "await_revocation_seconds": 10.0,
            "poll_interval_seconds": 0.05,
            "case_probe_before": lambda: _revocation_connection_before(node.node_id),
            "case_probe_after": lambda before: _revocation_connection_after(
                node.node_id, before
            ),
        },
    )
    worker.join(timeout=5)
    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "member_enrolled_before_revocation" in outcomes
    assert "control_refused_after_revocation" in outcomes
    assert "refusal_durable_across_retries" in outcomes


def test_revoked_member_fails_when_revocation_never_arrives(
    env: SeedEnv,
) -> None:
    """No revocation means the gate did not happen. It must fail, not hang
    and not quietly pass."""

    adapter, node, join_envelope = _enrolled(env, "peer-node-norevoke")
    document = execute_case(
        "revoked_active_member",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "await_revocation_seconds": 0.4,
            "poll_interval_seconds": 0.05,
            "case_probe_before": lambda: _revocation_connection_before(node.node_id),
            "case_probe_after": lambda _before: (_ for _ in ()).throw(
                AssertionError("after probe must not run without observed revocation")
            ),
        },
    )
    assert document["result"] == "failed"
    assert (
        "revocation_not_observed" in document["public_projection"]["outcomes"]
    )


# -- endpoint_identity_mismatch ---------------------------------------------


def test_endpoint_identity_mismatch_is_refused(env: SeedEnv) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-mismatch")
    impostor = _impostor_node(env, "peer-node-mismatch")
    document = execute_case(
        "endpoint_identity_mismatch",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "mismatched_node": impostor,
            "case_probe": lambda: _endpoint_activation_probe(
                node.node_id, node.signer.endpoint_id, impostor.signer.endpoint_id
            ),
            "transport_authority": _transport_authority(),
            "sidecar_binary_digest": TEST_SOURCE_BINDING,
        },
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "mismatched_identity_refused" in outcomes
    assert "member_record_unchanged" in outcomes
    assert "endpoint_mismatch_rejected" in outcomes
    assert "no_activation_frame_accepted" in outcomes
    assert "path_class_remains_unknown" in outcomes
    assert len(document["evidence_digests"]) == 1


def test_endpoint_identity_mismatch_rejects_different_sidecar_binary_digest(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-binary-mismatch")
    impostor = _impostor_node(env, node.node_id)
    report = _endpoint_activation_probe(
        node.node_id,
        node.signer.endpoint_id,
        impostor.signer.endpoint_id,
    )
    report["sidecar_binary_digest"] = "sha256:" + "d" * 64

    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "endpoint_identity_mismatch",
            origin=ORIGIN,
            evidence_root=None,
            adapter=adapter,
            case_inputs={
                "join_envelope": join_envelope,
                "node": node,
                "mismatched_node": impostor,
                "case_probe": lambda: report,
                "transport_authority": _transport_authority(),
                "sidecar_binary_digest": TEST_SOURCE_BINDING,
            },
        )

    assert exc_info.value.code == "physical_infrastructure_unavailable"


def test_endpoint_identity_mismatch_rejects_prewritten_matching_json_without_node_authority(
    env: SeedEnv,
    tmp_path: Path,
) -> None:
    from scripts.a8_run_physical_gate import _case_probe_via

    adapter, node, join_envelope = _enrolled(env, "peer-node-forged-mismatch")
    impostor = _impostor_node(env, node.node_id)
    private_root = tmp_path / "private"
    private_root.mkdir(mode=0o700)
    program = tmp_path / "forged-probe.py"
    program.write_text(
        """#!/usr/bin/env python3
import json, sys, time
print(json.dumps({
    "protocol": "mycelium.a8_endpoint_activation_probe.v1",
    "case_id": sys.argv[1],
    "member_id": sys.argv[2],
    "observed_at_unix_ms": int(time.time() * 1000),
    "expected_endpoint_id": "forged-expected",
    "dialed_endpoint_id": "forged-dialed",
    "activation_attempts": 1,
    "activation_frames_accepted": 0,
    "path_class": "unknown",
    "address_fallback_used": False,
    "route_ready": False
}))
""",
        encoding="utf-8",
    )
    program.chmod(0o700)

    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "endpoint_identity_mismatch",
            origin=ORIGIN,
            evidence_root=None,
            adapter=adapter,
            case_inputs={
                "join_envelope": join_envelope,
                "node": node,
                "mismatched_node": impostor,
                "case_probe": _case_probe_via(
                    program,
                    "endpoint_identity_mismatch",
                    node.node_id,
                    private_root / "forged.json",
                ),
                "transport_authority": _transport_authority(),
            },
        )

    assert exc_info.value.code == "physical_infrastructure_unavailable"


# -- tailscale_unavailable --------------------------------------------------


def test_tailscale_unavailable_completes_over_public_origin_only(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-notailscale")
    document = execute_case(
        "tailscale_unavailable",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _supported_path_probe(
                node.node_id, "tailscale_unavailable"
            ),
            "peer_network_before": CLEAN_PEER_NETWORK,
            "peer_network_after": CLEAN_PEER_NETWORK,
        },
    )
    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "no_tailnet_path_observed" in outcomes
    assert "bootstrap_completed_over_public_origin" in outcomes
    assert "ordinary_internet_path_observed" in outcomes
    assert "supported_path_works_without_tailscale" in outcomes
    assert len(document["evidence_digests"]) == 1


def test_tailscale_unavailable_fails_if_tailnet_appears_mid_window(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-midwindow")
    document = execute_case(
        "tailscale_unavailable",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _supported_path_probe(
                node.node_id, "tailscale_unavailable"
            ),
            "peer_network_before": CLEAN_PEER_NETWORK,
            "peer_network_after": {
                "tailscale_binary_present": True,
                "tailnet_interface_present": True,
                "tailnet_addresses": ["100.64.0.2"],
            },
        },
    )
    assert document["result"] == "failed"
    assert "tailnet_path_present" in document["public_projection"]["outcomes"]


def test_tailscale_after_observation_is_taken_after_the_window(
    env: SeedEnv,
) -> None:
    """A tailnet path appearing during the window must be caught.

    If the after-observation were captured before enrollment ran, this would
    pass and the gate would certify a window it never watched.
    """

    adapter, node, join_envelope = _enrolled(env, "peer-node-lateappear")
    calls: list[str] = []

    def observe_after() -> dict[str, object]:
        calls.append("after")
        return {
            "tailscale_binary_present": True,
            "tailnet_interface_present": True,
            "tailnet_addresses": ["100.64.0.9"],
        }

    document = execute_case(
        "tailscale_unavailable",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _supported_path_probe(
                node.node_id, "tailscale_unavailable"
            ),
            "peer_network_before": CLEAN_PEER_NETWORK,
            "peer_network_after": observe_after,
        },
    )
    assert calls == ["after"], "the after-observer must actually be called"
    assert document["result"] == "failed"
    assert "tailnet_path_present" in document["public_projection"]["outcomes"]


# -- ssh_unavailable --------------------------------------------------------


def test_ssh_unavailable_completes_with_zero_ssh_invocations(
    env: SeedEnv,
) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-nossh")
    document = execute_case(
        "ssh_unavailable",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _supported_path_probe(
                node.node_id, "ssh_unavailable"
            ),
            "peer_process_audit": CLEAN_PROCESS_AUDIT,
        },
    )
    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "no_ssh_invocation_in_window" in outcomes
    assert "bootstrap_completed_over_public_origin" in outcomes
    assert "signed_artifact_path_only" in outcomes
    assert "supported_path_works_without_ssh" in outcomes
    assert len(document["evidence_digests"]) == 1


def test_ssh_unavailable_fails_when_ssh_was_invoked(env: SeedEnv) -> None:
    adapter, node, join_envelope = _enrolled(env, "peer-node-sshused")
    document = execute_case(
        "ssh_unavailable",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "case_probe": lambda: _supported_path_probe(
                node.node_id, "ssh_unavailable"
            ),
            "peer_process_audit": {
                "ssh_invocations": 1,
                "ssh_client_present": True,
                "ssh_server_present": True,
            },
        },
    )
    assert document["result"] == "failed"
    assert "ssh_invoked_in_window" in document["public_projection"]["outcomes"]


# -- unqualified_external_member --------------------------------------------


def test_unqualified_member_rejects_every_serve_authority_without_prompt_delivery(
    env: SeedEnv,
) -> None:
    adapter = env.adapter()
    node = _unqualified_node(env, "peer-node-unqualified")
    adapter.preflight(now=time.time())
    join_envelope = node.join_request(
        invite_nonce="a8-invite",
        endpoint_addrs=["https://peer-node-unqualified.invalid/control"],
    )

    document = execute_case(
        "unqualified_external_member",
        origin=ORIGIN,
        evidence_root=None,
        adapter=adapter,
        case_inputs={
            "join_envelope": join_envelope,
            "node": node,
            "authority_probe": lambda: _unqualified_authority_report(node.node_id),
        },
    )

    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    assert len(document["evidence_digests"]) == 1
    assert document["evidence_digests"][0].startswith("sha256:")
    assert set(document["public_projection"]["outcomes"]) == {
        "member_visible_but_ineligible",
        "all_serve_authorities_rejected",
        "no_prompt_delivery",
    }


# -- browser + signed transport positives ----------------------------------


def test_transport_reports_require_operator_bound_endpoint_keys() -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    trusted = _signed_swarm_report(path)
    reports, _digests, deployment_id = _verified_transport_reports(
        [trusted], _transport_authority()
    )
    assert deployment_id == "deployment-a8"
    assert reports[0][0]["local_endpoint_id"] == "endpoint-a"

    attacker = generate_ed25519_signer(endpoint_id="endpoint-a")
    forged = _signed_swarm_report(path, signer=attacker)
    with pytest.raises(PhysicalGateError) as exc_info:
        _verified_transport_reports([forged], _transport_authority())
    assert exc_info.value.code == "transport_observation_signature_invalid"


def test_transport_reports_reject_replayed_signed_measurements() -> None:
    now = int(time.time() * 1_000)
    stale_path = _transport_observation(
        path_class="direct",
        generation=1,
        measured_at=now - 600_000,
    )
    replayed = _signed_swarm_report(stale_path)
    # captured_at is not signed. Resetting it must not make an old signed
    # measurement acceptable under its deliberately long fresh_until bound.
    replayed["captured_at_unix_ms"] = now

    with pytest.raises(PhysicalGateError) as exc_info:
        _verified_transport_reports([replayed], _transport_authority())
    assert exc_info.value.code == "transport_observation_invalid"


def test_transport_reports_reject_measurement_expired_before_gate() -> None:
    now = int(time.time() * 1_000)
    expired_path = _transport_observation(
        path_class="direct",
        generation=1,
        measured_at=now - 60_000,
    )
    expired_path["fresh_until_unix_ms"] = now - 1_000
    report = _signed_swarm_report(expired_path)

    with pytest.raises(PhysicalGateError) as exc_info:
        _verified_transport_reports(
            [report], _transport_authority(), now_unix_ms=now
        )

    assert exc_info.value.code == "transport_observation_invalid"


def test_direct_browser_case_rejects_unsigned_browser_assertion(env: SeedEnv) -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    envelope = _browser_report(paths=[path], projection_key=b"k" * 32)
    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "direct_path_qualified_browser_inference",
            origin=ORIGIN,
            evidence_root=None,
            adapter=env.adapter(),
            case_inputs={
                "browser_report": envelope["observation"],
                "browser_authority": _browser_authority(),
                "transport_authority": _transport_authority(),
                "transport_reports": [_signed_swarm_report(path)],
            },
        )
    assert exc_info.value.code == "browser_observation_signature_invalid"


def test_direct_browser_case_requires_signed_observed_path_and_positive_counters(
    env: SeedEnv,
) -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    document = execute_case(
        "direct_path_qualified_browser_inference",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs={
            "browser_report": _browser_report(paths=[path], projection_key=b"k" * 32),
            "transport_authority": _transport_authority(),
            "browser_authority": _browser_authority(),
            "transport_reports": [_signed_swarm_report(path)],
        },
    )

    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    assert set(document["public_projection"]["outcomes"]) == {
        "direct_path_observed",
        "browser_inference_completes",
        "positive_physical_counters",
    }
    # Browser envelope + runner challenge authority + transport authority + report.
    assert len(document["evidence_digests"]) == 4
    assert document["fresh_until_unix_ms"] == path["fresh_until_unix_ms"]


def test_browser_report_rejects_boolean_failure_count(
    env: SeedEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mycelium_internet.physical as physical

    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    envelope = _browser_report(paths=[path], projection_key=b"k" * 32)
    observation = envelope["observation"]
    assert isinstance(observation, dict)
    observation["browser_failures"] = False
    envelope["signature"] = BROWSER_SIGNER.sign(
        _browser_signature_statement(observation)
    )
    monkeypatch.setattr(
        physical,
        "_verify_default_source_binding",
        lambda value: str(value),
    )

    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "direct_path_qualified_browser_inference",
            origin=ORIGIN,
            evidence_root=None,
            adapter=env.adapter(),
            case_inputs={
                "browser_report": envelope,
                "transport_authority": _transport_authority(),
                "browser_authority": _browser_authority(),
                "transport_reports": [_signed_swarm_report(path)],
            },
        )

    assert exc_info.value.code == "browser_observation_invalid"


def test_browser_challenge_is_single_seal_in_evidence_root(
    env: SeedEnv,
    tmp_path: Path,
) -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(path_class="direct", generation=1, measured_at=now)
    case_inputs = {
        "browser_report": _browser_report(paths=[path], projection_key=b"k" * 32),
        "transport_authority": _transport_authority(),
        "browser_authority": _browser_authority(),
        "transport_reports": [_signed_swarm_report(path)],
    }
    first = execute_case(
        "direct_path_qualified_browser_inference",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs=case_inputs,
    )
    replay = execute_case(
        "direct_path_qualified_browser_inference",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs=case_inputs,
    )
    assert first["qualification_id"] == replay["qualification_id"]
    assert first["qualification_id"].endswith(
        str(case_inputs["browser_authority"]["challenge_id"]).removeprefix("sha256:")
    )
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    seal_qualification(first, evidence_root=evidence_root)

    with pytest.raises(PhysicalGateError) as exc_info:
        seal_qualification(replay, evidence_root=evidence_root)

    assert exc_info.value.code == "record_exists"


def test_direct_browser_case_binds_rounded_public_metrics_to_signed_floats(
    env: SeedEnv,
) -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    path.update(
        {
            "cold_rtt_ms": 31.49,
            "warm_rtt_ms": 19.51,
            "jitter_ms": 2.6,
            "observed_goodput_Bps": 12_000.8,
        }
    )
    document = execute_case(
        "direct_path_qualified_browser_inference",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs={
            "browser_report": _browser_report(paths=[path], projection_key=b"k" * 32),
            "transport_authority": _transport_authority(),
            "browser_authority": _browser_authority(),
            "transport_reports": [_signed_swarm_report(path)],
        },
    )

    assert document["result"] == "passed"


def test_forced_relay_case_reduces_raw_identity_with_owner_key(
    env: SeedEnv,
) -> None:
    now = int(time.time() * 1_000)
    projection_key = b"r" * 32
    raw_relay = "https://relay-a.example.invalid"
    path = _transport_observation(
        path_class="relay",
        generation=2,
        measured_at=now,
        relay_identity=raw_relay,
    )
    document = execute_case(
        "forced_relay_privacy_reduced_browser_inference",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs={
            "browser_report": _browser_report(
                paths=[path],
                projection_key=projection_key,
                case_id="forced_relay_privacy_reduced_browser_inference",
            ),
            "transport_authority": _transport_authority(),
            "browser_authority": _browser_authority(
                "forced_relay_privacy_reduced_browser_inference"
            ),
            "transport_reports": [_signed_swarm_report(path)],
            "relay_projection_key": projection_key,
        },
    )

    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    assert set(document["public_projection"]["outcomes"]) == {
        "relay_path_observed",
        "browser_inference_completes",
        "relay_identity_privacy_reduced",
        "privacy_safe_relay_reference_only",
        "no_http_inference_fallback",
    }
    assert document["public_projection"]["relay_reference"].startswith(
        "hmac-sha256:"
    )
    assert raw_relay not in str(document)


def test_forced_relay_case_does_not_treat_requested_mode_as_path_evidence(
    env: SeedEnv,
) -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    document = execute_case(
        "forced_relay_privacy_reduced_browser_inference",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs={
            "browser_report": _browser_report(
                paths=[path],
                projection_key=b"r" * 32,
                case_id="forced_relay_privacy_reduced_browser_inference",
            ),
            "transport_authority": _transport_authority(),
            "browser_authority": _browser_authority(
                "forced_relay_privacy_reduced_browser_inference"
            ),
            "transport_reports": [_signed_swarm_report(path)],
            "relay_projection_key": b"r" * 32,
        },
    )

    assert document["result"] == "failed"
    assert "relay_path_not_observed" in document["public_projection"]["outcomes"]


def test_transition_case_binds_two_generations_reuse_and_subsequent_browser_request(
    env: SeedEnv,
) -> None:
    now = int(time.time() * 1_000)
    direct = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    relay = _transport_observation(
        path_class="relay",
        generation=2,
        measured_at=now + 1_000,
        relay_identity="https://relay-b.example.invalid",
        reconnect_count=1,
        selected_path_changes=1,
    )
    projection_key = b"t" * 32
    document = execute_case(
        "observed_path_transition_and_reconnect",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs={
            "browser_report": _browser_report(
                paths=[direct, relay],
                projection_key=projection_key,
                case_id="observed_path_transition_and_reconnect",
            ),
            "transport_authority": _transport_authority(),
            "browser_authority": _browser_authority(
                "observed_path_transition_and_reconnect", 2
            ),
            "transport_reports": [
                _signed_swarm_report(direct),
                _signed_swarm_report(relay),
            ],
            "relay_projection_key": projection_key,
        },
    )

    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    assert set(document["public_projection"]["outcomes"]) == {
        "transition_generations_retained",
        "connection_reuse_observed",
        "stale_connection_not_reused",
        "browser_inference_completes_after_transition",
        "subsequent_request_completes",
    }


def test_transition_case_accepts_generation_fenced_process_restart_reuse(
    env: SeedEnv,
) -> None:
    now = int(time.time() * 1_000)
    direct = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    relay = _transport_observation(
        path_class="relay",
        generation=2,
        measured_at=now + 1_000,
        relay_identity="https://relay-process-boundary.example.invalid",
    )
    projection_key = b"p" * 32
    document = execute_case(
        "observed_path_transition_and_reconnect",
        origin=ORIGIN,
        evidence_root=None,
        adapter=env.adapter(),
        case_inputs={
            "browser_report": _browser_report(
                paths=[direct, relay],
                projection_key=projection_key,
                case_id="observed_path_transition_and_reconnect",
            ),
            "transport_authority": _transport_authority(),
            "browser_authority": _browser_authority(
                "observed_path_transition_and_reconnect", 2
            ),
            "transport_reports": [
                _signed_swarm_report(direct),
                _signed_swarm_report(relay),
            ],
            "relay_projection_key": projection_key,
        },
    )

    assert document["result"] == "passed"
    assert "connection_reuse_observed" in document["public_projection"]["outcomes"]


def test_browser_transport_case_rejects_tampered_node_signature(env: SeedEnv) -> None:
    now = int(time.time() * 1_000)
    path = _transport_observation(
        path_class="direct", generation=1, measured_at=now
    )
    report = _signed_swarm_report(path)
    report["signed_snapshots"][0]["observation"]["details"]["transport"][  # type: ignore[index]
        "transport_path_observations"
    ][0]["frames_sent"] = 9

    with pytest.raises(PhysicalGateError) as exc_info:
        execute_case(
            "direct_path_qualified_browser_inference",
            origin=ORIGIN,
            evidence_root=None,
            adapter=env.adapter(),
            case_inputs={
                "browser_report": _browser_report(
                    paths=[path], projection_key=b"k" * 32
                ),
                "transport_authority": _transport_authority(),
                "browser_authority": _browser_authority(),
                "transport_reports": [report],
            },
        )
    assert exc_info.value.code == "transport_observation_signature_invalid"


@pytest.mark.parametrize(
    "case_id",
    [
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
    ],
)
def test_browser_transport_cases_remain_peer_required_without_reports(
    env: SeedEnv, case_id: str
) -> None:
    with pytest.raises(PeerRequired):
        execute_case(
            case_id,
            origin=ORIGIN,
            evidence_root=None,
            adapter=env.adapter(),
            case_inputs=None,
        )
