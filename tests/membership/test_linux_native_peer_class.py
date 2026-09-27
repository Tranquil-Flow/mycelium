"""The A13 Linux product peer class ``linux_native_iroh``.

The A13 Linux target onboarding speaks this class.  It is registered so a seed
admits its join, but it is NOT the ``linux_numpy_iroh`` stage runtime: the Linux
product package binds no member runtime (its service argv is caller supplied and
``mycelium_node`` does not accept this class), so it stays activation-ineligible
with the named ``linux_native_member_runtime_unbound`` refusal until that binding
exists.  The declaration is exact: it cannot borrow the numpy runtime or promote
itself by declaring the activation protocol.
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
    "runtime_backend": "tbd",
    "transport": "iroh",
    "activation_protocol": None,
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


def test_linux_native_iroh_is_refused_activation_with_a_named_reason() -> None:
    assert "linux_native_iroh" not in _ACTIVATION_ELIGIBLE_PEER_CLASSES
    assert not peer_runtime_is_activation_eligible("linux_native_iroh", LINUX_NATIVE)
    assert (
        peer_runtime_activation_refusal("linux_native_iroh")
        == "linux_native_member_runtime_unbound"
    )


def test_linux_numpy_iroh_keeps_its_eligibility_without_a_refusal() -> None:
    """Negative control: the existing Linux stage-runtime class is unchanged."""

    assert peer_runtime_is_activation_eligible("linux_numpy_iroh", LINUX_NUMPY)
    assert peer_runtime_activation_refusal("linux_numpy_iroh") is None


@pytest.mark.parametrize(
    "capability",
    [
        # Cannot borrow the numpy stage runtime under the product label.
        LINUX_NUMPY,
        {**LINUX_NATIVE, "runtime_backend": "numpy"},
        # Cannot promote itself by declaring the activation protocol.
        {**LINUX_NATIVE, "activation_protocol": ROUTER_ACTIVATION_PROTOCOL},
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
