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

from pathlib import Path
import threading
import time

import pytest

from mycelium_node.identity import load_or_create_node_signer
from mycelium_node.membership import NodeMembershipSession
from mycelium_internet.contracts import validate_internet_native_qualification
from mycelium_internet.physical import (
    PeerRequired,
    execute_case,
)

from .test_physical_seed_side_cases import ORIGIN, SeedEnv, _ids

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
    assert "activation_unknown_not_zero" in outcomes
    assert "no_tailnet_path_observed" in outcomes


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
        },
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "member_enrolled_before_revocation" in outcomes
    assert "control_refused_after_revocation" in outcomes
    assert "no_serving_after_revocation" in outcomes


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
        },
    )
    validate_internet_native_qualification(document)
    assert document["executed"] is True
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "mismatched_identity_refused" in outcomes
    assert "member_record_unchanged" in outcomes


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
            "peer_network_before": CLEAN_PEER_NETWORK,
            "peer_network_after": CLEAN_PEER_NETWORK,
        },
    )
    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "no_tailnet_path_observed" in outcomes
    assert "bootstrap_completed_over_public_origin" in outcomes


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
            "peer_process_audit": CLEAN_PROCESS_AUDIT,
        },
    )
    validate_internet_native_qualification(document)
    assert document["result"] == "passed"
    outcomes = document["public_projection"]["outcomes"]
    assert "no_ssh_invocation_in_window" in outcomes
    assert "bootstrap_completed_over_public_origin" in outcomes


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
            "peer_process_audit": {
                "ssh_invocations": 1,
                "ssh_client_present": True,
                "ssh_server_present": True,
            },
        },
    )
    assert document["result"] == "failed"
    assert "ssh_invoked_in_window" in document["public_projection"]["outcomes"]


# -- cases that remain blocked ----------------------------------------------


@pytest.mark.parametrize(
    "case_id",
    [
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
        "unqualified_external_member",
    ],
)
def test_ui_dependent_cases_remain_peer_required(
    env: SeedEnv, case_id: str
) -> None:
    """These additionally require the A8 UI mounted in the product spine,
    which is blocked on the A4 lane. They must keep failing closed."""

    with pytest.raises(PeerRequired):
        execute_case(
            case_id,
            origin=ORIGIN,
            evidence_root=None,
            adapter=env.adapter(),
            case_inputs={"join_envelope": {}, "node": None},
        )
