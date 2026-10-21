# SPDX-License-Identifier: AGPL-3.0-only
"""The A13 Linux product peer class ``linux_native_iroh``.

The A13 Linux target onboarding speaks this class.  Under the owner parity ruling
(2026-10-07: Windows, macOS and Linux devices can all do everything; A13 covers
all three platforms) the packaged Linux app binds the NumPy stage runtime, so the
class declares exactly that runtime with the router activation protocol and is
activation-eligible, like ``windows_native_iroh``.  The declaration stays exact:
any other runtime, transport or protocol under this label is refused.
"""

from __future__ import annotations

import pytest

from mycelium_membership import (
    JOIN_REQUEST_PROTOCOL,
    PEER_CLASSES,
    ROUTER_ACTIVATION_PROTOCOL,
    MembershipContractError,
    peer_runtime_activation_refusal,
    peer_runtime_is_activation_eligible,
    validate_membership_message,
    validate_peer_runtime_capability,
)
from mycelium_membership.contracts import _ACTIVATION_ELIGIBLE_PEER_CLASSES

LINUX_NATIVE = {
    "runtime_backend": "numpy",
    "transport": "iroh",
    "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
}
LINUX_NUMPY = {
    "runtime_backend": "numpy",
    "transport": "iroh",
    "activation_protocol": ROUTER_ACTIVATION_PROTOCOL,
}


def _join_request(peer_class: str, runtime_capability: dict) -> dict:
    return {
        "protocol": JOIN_REQUEST_PROTOCOL,
        "message_id": "message-1",
        "swarm_id": "swarm-a",
        "sender_node_id": "node-a",
        "sender_endpoint_id": "endpoint-a",
        "recipient_node_id": "seed-node",
        "incarnation": "incarnation-a",
        "generation": 0,
        "issued_at": 1_000.0,
        "expires_at": 1_060.0,
        "invite_nonce": "invite-a",
        "endpoint_addr": {
            "id": "endpoint-a",
            "addrs": ["iroh-relay://relay.example/node-a"],
        },
        "software_version": "mycelium-test",
        "peer_class": peer_class,
        "runtime_capability": dict(runtime_capability),
    }


def test_linux_native_iroh_is_a_registered_member_class() -> None:
    assert "linux_native_iroh" in PEER_CLASSES
    assert validate_peer_runtime_capability("linux_native_iroh", LINUX_NATIVE) == (
        LINUX_NATIVE
    )
    validate_membership_message(_join_request("linux_native_iroh", LINUX_NATIVE))


def test_linux_native_iroh_is_activation_eligible_under_the_parity_ruling() -> None:
    assert "linux_native_iroh" in _ACTIVATION_ELIGIBLE_PEER_CLASSES
    assert peer_runtime_is_activation_eligible("linux_native_iroh", LINUX_NATIVE)
    assert peer_runtime_activation_refusal("linux_native_iroh") is None


def test_linux_numpy_iroh_keeps_its_eligibility_without_a_refusal() -> None:
    """Negative control: the existing Linux stage-runtime class is unchanged."""

    assert peer_runtime_is_activation_eligible("linux_numpy_iroh", LINUX_NUMPY)
    assert peer_runtime_activation_refusal("linux_numpy_iroh") is None


@pytest.mark.parametrize(
    "capability",
    [
        # Exactly the NumPy runtime over iroh with the router protocol: nothing else.
        {**LINUX_NATIVE, "runtime_backend": "tbd"},
        {**LINUX_NATIVE, "runtime_backend": "mlx"},
        {**LINUX_NATIVE, "activation_protocol": None},
        {**LINUX_NATIVE, "transport": "none"},
    ],
)
def test_linux_native_capability_mismatch_is_refused(capability: dict) -> None:
    with pytest.raises(MembershipContractError) as refused:
        validate_membership_message(_join_request("linux_native_iroh", capability))
    assert refused.value.code == "membership_runtime_capability_mismatch"
    with pytest.raises(MembershipContractError):
        peer_runtime_is_activation_eligible("linux_native_iroh", capability)


def test_an_unlisted_linux_class_is_still_refused() -> None:
    with pytest.raises(MembershipContractError) as unknown:
        validate_membership_message(_join_request("linux_future_iroh", LINUX_NATIVE))
    assert unknown.value.code == "membership_peer_class_invalid"
